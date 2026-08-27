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
from .feed import MarketFeed, pip_divisor
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
                   legacy_cross_sign: bool = False, bands: str | Path | None = None,
                   econ: EconCalendar | None = None, **kw) -> "Book":
        book = cls(data=ExcelSource(path, **kw).load(), clock=clock or Clock.utcnow(),
                   legacy_cross_sign=legacy_cross_sign,
                   econ=econ if econ is not None else EconCalendar.load())
        book.bands = book._default_bands(bands)
        return book

    def _default_bands(self, path: str | Path | None) -> dict[str, Band]:
        """The managed bands to hand the surfaces, from ``bands.csv``.

        Loaded here rather than left to the caller because a peg is a property
        of the pair, not of the screen looking at it: a build where the
        pricing tab knew about the band and the analysis tab did not would be
        the same silent half-answer as a swallowed error.  A file that cannot
        be read is a warning, not a failed book -- the rest of the marks are
        unaffected by it.
        """
        from .paths import find_data_file
        found = Path(path) if path else find_data_file("bands.csv", "files/bands.csv")
        if found is None:
            return {}
        try:
            return {k: v for k, v in load_bands(found).items() if v.upper > v.lower > 0}
        except (OSError, ValueError) as exc:
            self.warnings.append(f"managed bands: {found} could not be read ({exc})")
            return {}

    def market_level(self, pair: str, t: float) -> dict:
        """Spot and the outright forward at ``t`` years, from the feed.

        ``feed`` is False and the levels are ``None`` when there is nothing to
        read -- never a fallback level.  The band model would place a hard
        barrier in the wrong place with a guessed one, and the screens that
        put a strike axis in absolute terms would be naming levels nobody
        published.  One function for both, so a strike a chart shows and a
        band edge the model places can never come from different forwards.

        A **cross the feed does not quote is built from its legs**, which it
        does quote: a published EURUSD and USDJPY are a published EURJPY, by
        the same triangle the surface itself is built on, and refusing one
        while pricing the other off the same file is a feed that is loaded and
        cannot be seen.  ``derived`` says it happened and ``via`` names the
        legs, because a level that came out of an identity and one that was
        published must not read the same.
        """
        out = {"spot": None, "forward": None, "points": None, "pip": None,
               "feed": False, "extrapolated": False, "derived": False, "via": ""}
        level = self._feed_level(pair, t)
        if level is None:
            return out
        out.update(spot=level["spot"], forward=level["forward"],
                   points=level["points"], pip=level["pip"], feed=True,
                   extrapolated=level["extrapolated"],
                   derived=bool(level["via"]), via=level["via"])
        return out

    def _feed_level(self, pair: str, t: float,
                    trail: tuple[str, ...] = ()) -> dict | None:
        """One pair's spot and outright forward, quoted or composed from legs.

        ``None`` when neither is possible -- no feed, no quote for the pair and
        no pair of legs that has one.  The composition is exact rather than a
        convenience: a cross outright is the product of its legs' outrights in
        the right orientation, which is triangular arbitrage and not a model.
        ``trail`` is what stops a cross of a cross walking in a circle.
        """
        key = pair.upper()
        feed = self.feed
        if feed is None:
            return None
        if key in getattr(feed, "pairs", {}):
            quote = feed.quote(key, t)
            return {"spot": float(quote["spot"]), "forward": float(quote["forward"]),
                    "points": float(quote["points"]), "pip": float(quote["pip"]),
                    "extrapolated": bool(quote["extrapolated"]), "via": ""}
        if key in trail:
            return None
        spec = self.data.pairs.get(key)
        legs = tuple(getattr(spec, "legs", ()) or ())
        if len(legs) != 2:
            return None
        try:
            sign_a, sign_b = infer_leg_signs(key, legs[0], legs[1])
        except ValueError:
            return None
        a = self._feed_level(legs[0], t, trail + (key,))
        b = self._feed_level(legs[1], t, trail + (key,))
        if a is None or b is None:
            return None
        if min(a["spot"], a["forward"], b["spot"], b["forward"]) <= 0:
            return None

        def compose(x_a: float, x_b: float) -> float:
            # The first leg carries the base currency and the second the term,
            # and each is turned the right way up before they meet.  The signs
            # are the triangle's own (``infer_leg_signs``), read here as
            # quotation rather than as correlation: +1 on the first leg means
            # it already reads (base)/(common), +1 on the second means it reads
            # (term)/(common) and so enters inverted.  EURJPY is EURUSD *
            # USDJPY; EURGBP is EURUSD / GBPUSD.
            first = x_a if sign_a > 0 else 1.0 / x_a
            second = 1.0 / x_b if sign_b > 0 else x_b
            return first * second

        spot = compose(a["spot"], b["spot"])
        forward = compose(a["forward"], b["forward"])
        # The points are the composed outright less the composed spot, in the
        # cross's own pips.  They are never the legs' points added: a point of
        # EURUSD and a point of USDJPY are different amounts of money, and the
        # sum of them is not a number anybody quotes.
        pip = pip_divisor(key)
        return {"spot": spot, "forward": forward,
                "points": (forward - spot) * pip, "pip": pip,
                "extrapolated": bool(a["extrapolated"] or b["extrapolated"]),
                "via": f"{legs[0]} and {legs[1]}"}

    def forward_at(self, pair: str, t: float) -> float | None:
        """The outright forward from the feed, or None when there is no feed."""
        return self.market_level(pair, t)["forward"]

    def _attach_band(self, name: str, surface: VolSurface) -> None:
        """Give a surface its band and a way to place it.

        The lookup is a closure over the book rather than a captured feed, so
        a feed loaded *after* the book was built -- which is what ``serve``
        and the analysis CLI both do -- is picked up on the next query.
        """
        surface.band = self.bands.get(name.upper())
        if surface.band is not None:
            surface.forward_lookup = lambda t, p=name: self.forward_at(p, t)

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

        surface = VolSurface(
            pair=name, atm=atm,
            conv=DeltaConvention(spec.resolved_premium_adjusted()),
        )
        self._attach_band(name, surface)
        return surface

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
            self._attach_band(name, surface)
        return self

    def banded_pairs(self) -> list[str]:
        """Built pairs that have a managed band, in the order they were built."""
        return [n for n, s in self.surfaces.items() if s.band is not None]

    def __getitem__(self, pair: str) -> VolSurface:
        try:
            return self.surfaces[pair]
        except KeyError:
            pass
        # Which list to name depends on why it is missing.  ``load_all`` may
        # have been narrowed to a few pairs -- ``volkit band USDHKD`` narrows
        # it to one -- and when that one is not in the workbook nothing is
        # built at all, so "available: []" said the workbook was empty when
        # what was actually wrong is that it does not carry this pair.  A pair
        # the workbook has never heard of is told what the workbook holds.
        known = sorted(self.data.pairs)
        if pair not in self.data.pairs:
            raise KeyError(
                f"{pair!r} is not in {self.data.source or 'the workbook'}; it holds "
                f"{', '.join(known) if known else 'no pairs'}"
            ) from None
        raise KeyError(
            f"{pair!r} is in the workbook but is not built in this book; built: "
            f"{', '.join(self.pairs) if self.pairs else 'nothing'}"
        ) from None

    def __contains__(self, pair: str) -> bool:
        return pair in self.surfaces

    @property
    def pairs(self) -> list[str]:
        return sorted(self.surfaces)

    def all_problems(self) -> list[str]:
        return list(self.data.problems) + list(self.warnings)
