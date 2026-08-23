"""A book of volatility surfaces, assembled from a market data source.

Replaces the legacy ``Vols``.  Beyond the parsing fixes in ``marketdata``,
two behaviours change:

* Crosses are built **after** their legs, in dependency order.  The legacy
  ``load_vol_all`` iterated a dictionary, so whether a cross saw calibrated
  legs or not depended on insertion order.
* ``Vols.__init__`` set ``self.tenor_points`` inside the loop over crosses, so
  a workbook with no crosses left the attribute undefined and ``print_tenor``
  died with ``AttributeError``.  Tenor points now live on the book.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from datetime import datetime, timedelta
from pathlib import Path

from .atm import AtmCurve, BackboneParams
from .black import DeltaConvention
from .banded import Band, load_bands
from .calendars import CalendarSet, DEFAULT_CALENDARS
from .cross import CorrelationCurve, CrossAtmCurve, infer_leg_signs
from .events import EventSchedule
from .econ import EconCalendar
from .feed import MarketFeed
from .marketdata import ExcelSource, MarketData, MarketDataError
from .surface import VolSurface
from .timeutil import Clock
from .timeweight import TimeWeighting


@dataclass
class Book:
    """Every surface in the workbook, keyed by pair."""

    data: MarketData
    clock: Clock = field(default_factory=Clock.utcnow)
    calendars: CalendarSet = field(default_factory=lambda: DEFAULT_CALENDARS)
    surfaces: dict[str, VolSurface] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Reproduce the legacy cross triangle, which always used -2*rho regardless
    # of how the two legs are quoted. Kept so the change can be A/B tested
    # against existing marks without editing the workbook -- see MIGRATION.md.
    legacy_cross_sign: bool = False
    econ: EconCalendar = field(default_factory=EconCalendar.load)
    # Optional spot / forward feed.  When present, a pricing leg that leaves
    # spot blank is filled from it, with forward points interpolated to the
    # leg's own expiry rather than snapped to a standard tenor.
    feed: MarketFeed | None = None
    # Managed / pegged trading bands, by pair.  A banded pair needs a
    # bounded-support smile: a lognormal one prices strikes outside the band.
    bands: dict[str, Band] = field(default_factory=dict)

    @classmethod
    def from_excel(cls, path: str | Path, clock: Clock | None = None, *,
                   legacy_cross_sign: bool = False,
                   econ: EconCalendar | None = None, **kw) -> "Book":
        return cls(data=ExcelSource(path, **kw).load(), clock=clock or Clock.utcnow(),
                   legacy_cross_sign=legacy_cross_sign,
                   econ=econ if econ is not None else EconCalendar.load())

    # -- construction -----------------------------------------------------
    def build_order(self) -> list[str]:
        """Pairs ordered so every cross comes after both of its legs."""
        done: list[str] = []
        seen: set[str] = set()

        def visit(name: str, trail: tuple[str, ...] = ()) -> None:
            if name in seen:
                return
            if name in trail:
                raise MarketDataError(
                    f"circular cross definition: {' -> '.join(trail + (name,))}"
                )
            spec = self.data.pairs.get(name)
            if spec is None:
                return
            for leg in spec.legs:
                visit(leg, trail + (name,))
            seen.add(name)
            done.append(name)

        for name in self.data.pairs:
            visit(name)
        return done

    def required_pairs(self, pairs: list[str]) -> set[str]:
        """Expand a request to include the legs every cross depends on."""
        need: set[str] = set()
        stack = list(pairs)
        while stack:
            name = stack.pop()
            if name in need:
                continue
            need.add(name)
            spec = self.data.pairs.get(name)
            if spec is not None:
                stack.extend(spec.legs)
        return need

    def build(self, pairs: list[str] | None = None) -> "Book":
        """Build the ATM curves (and cross curves) for the requested pairs.

        A cross cannot be built without its legs, so the request is expanded
        to include them; asking for AUDJPY alone silently produced nothing in
        the legacy code.
        """
        wanted = self.required_pairs(pairs) if pairs else None
        for name in self.build_order():
            if wanted and name not in wanted:
                continue
            try:
                self.surfaces[name] = self._build_surface(name)
            except (ValueError, MarketDataError) as exc:
                self.warnings.append(f"{name}: could not build curve ({exc})")
        return self

    def _build_surface(self, name: str) -> VolSurface:
        spec = self.data.pairs[name]
        params = self.data.params.get(name)
        if params is None:
            raise MarketDataError(f"no parameters loaded for {name!r}")
        weighting = TimeWeighting(name, calendars=self.calendars)
        events = EventSchedule()
        for when, bump in params.events:
            if when <= self.clock.now:
                # A past event cannot be calibrated: its volatility day has
                # already elapsed, so the inversion would integrate the
                # backbone backwards in time.
                self.warnings.append(
                    f"{name}: event {when:%Y-%m-%d %H:%M}Z is before the valuation time "
                    f"{self.clock.now:%Y-%m-%d %H:%M}Z and was skipped"
                )
                continue
            events.add(when, bump, label=when.strftime("%d%b %H:%M"))

        common = dict(
            pair=name, clock=self.clock, weighting=weighting, events=events,
            tenor_points=tuple(self.data.tenor_points),
        )

        if spec.is_cross:
            missing = [l for l in spec.legs if l not in self.surfaces]
            if missing:
                raise MarketDataError(f"leg(s) {missing} must be built before the cross {name!r}")
            leg_a, leg_b = (self.surfaces[l].atm for l in spec.legs)
            atm = CrossAtmCurve(
                params=BackboneParams(
                    initial_vol=1e-8, long_term_vol=1e-8, mean_reversion=1.0,
                    short_addon=params.short_addon, short_decay=params.short_decay,
                ),
                leg_a=leg_a, leg_b=leg_b,
                correlation=CorrelationCurve(params.initial, params.long_term, params.mean_reversion),
                leg_signs=(1, 1) if self.legacy_cross_sign else infer_leg_signs(name, *spec.legs),
                **common,
            )
        else:
            bb = BackboneParams(
                initial_vol=params.initial, long_term_vol=params.long_term,
                mean_reversion=params.mean_reversion, short_addon=params.short_addon,
                short_decay=params.short_decay, rate_vol=params.rate_vol,
                rate_corr=params.rate_corr,
            )
            issues = bb.validate()
            if issues:
                raise MarketDataError(f"{name}: " + "; ".join(issues))
            atm = AtmCurve(params=bb, **common)

        if events.events:
            problems = atm.calibrate_events()
            self.warnings.extend(f"{name}: {p}" for p in problems)
            self.warnings.extend(f"{name}: {w}" for w in atm.event_leakage_warnings())

        return VolSurface(
            pair=name, atm=atm,
            conv=DeltaConvention(spec.resolved_premium_adjusted()),
        )

    # -- calibration ------------------------------------------------------
    def calibrate_smiles(self, pairs: list[str] | None = None) -> "Book":
        """Fit smiles for every pair that has quotes."""
        for name, surface in self.surfaces.items():
            if pairs and name not in pairs:
                continue
            marks = self.data.marks.get(name)
            if not marks:
                continue
            surface.calibrate(marks)
            self.warnings.extend(surface.warnings)
            surface.warnings.clear()
            for fit in surface.fits:
                if not fit.ok:
                    self.warnings.append(f"{name} {fit.tenor}: {fit.message}")
        return self

    def load_all(self, pairs: list[str] | None = None) -> "Book":
        # Legs are built so the cross has a curve, but only the pairs actually
        # asked for need their smiles fitted.
        return self.build(pairs).calibrate_smiles(pairs)

    def reload(self, path: str | Path | None = None) -> "Book":
        """Re-read the source and rebuild, keeping the same valuation clock."""
        source = ExcelSource(path or self.data.source)
        self.data = source.load()
        self.surfaces.clear()
        self.warnings.clear()
        return self.load_all()

    # -- access -----------------------------------------------------------
    def load_bands(self, path: str | Path) -> "Book":
        """Attach managed-band definitions and hand them to their surfaces."""
        self.bands = {k: v for k, v in load_bands(path).items() if v.upper > v.lower > 0}
        for name, surface in self.surfaces.items():
            surface.band = self.bands.get(name.upper())
        return self

    def __getitem__(self, pair: str) -> VolSurface:
        try:
            return self.surfaces[pair]
        except KeyError:
            raise KeyError(
                f"{pair!r} is not built; available: {sorted(self.surfaces)}"
            ) from None

    def __contains__(self, pair: str) -> bool:
        return pair in self.surfaces

    @property
    def pairs(self) -> list[str]:
        return sorted(self.surfaces)

    def all_problems(self) -> list[str]:
        return list(self.data.problems) + list(self.warnings)
