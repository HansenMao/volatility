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
from datetime import date, datetime, timedelta
from pathlib import Path

from .atm import AtmCurve, BackboneParams
from .black import DeltaConvention
from .banded import Band, load_bands
from .calendars import CalendarSet, DEFAULT_CALENDARS
from .cross import CorrelationCurve, CrossAtmCurve, infer_leg_signs
from .events import EventBook, EventSchedule
from .feed import MarketFeed
from .marketdata import ExcelSource, MarketData, MarketDataError
from .surface import VolSurface, WingRatio, load_wing_ratios
from .vegaweights import VegaWeights, load_vega_weights
from .timeutil import DAYS_IN_YEAR, Clock, parse_datetime
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
    #: The session's event table: the workbook's EVENTS sheet as loaded, then
    #: whatever the screens have marked on it.  Every pair's schedule is
    #: derived from it, so a currency weight moves every pair that has that
    #: currency and cannot be one number here and another there.
    events: EventBook = field(default_factory=EventBook)
    # Optional spot / forward feed.  When present, a pricing leg that leaves
    # spot blank is filled from it, with forward points interpolated to the
    # leg's own expiry rather than snapped to a standard tenor.
    feed: MarketFeed | None = None
    # Managed / pegged trading bands, by pair.  A banded pair needs a
    # bounded-support smile: a lognormal one prices strikes outside the band.
    bands: dict[str, Band] = field(default_factory=dict)
    # The ``WING_RATIOS`` tab, by pair: how each tenor's 10-delta wings follow
    # its 25-delta ones.  Read once for the book so a surface built later
    # cannot get a different answer from one built now.
    wing_ratios: dict[str, dict[str, WingRatio]] = field(default_factory=dict)
    #: The ``Vega Weights`` tab: how far each tenor moves when the anchor
    #: moves one vol point.  Not part of any surface -- nothing prices
    #: differently for it -- but read with the book so the marking screen and
    #: the command line share one answer about what a workbook says.
    vega_weights: VegaWeights = field(default_factory=VegaWeights)

    @classmethod
    def from_excel(cls, path: str | Path, clock: Clock | None = None, *,
                   legacy_cross_sign: bool = False, bands: str | Path | None = None,
                   calendars: CalendarSet | None = None, **kw) -> "Book":
        book = cls(data=ExcelSource(path, **kw).load(), clock=clock or Clock.utcnow(),
                   legacy_cross_sign=legacy_cross_sign)
        book.events = book.data.events.copy()
        book.bands = book._default_bands(bands or path)
        book.wing_ratios = book._default_wing_ratios(path)
        book.vega_weights = book._default_vega_weights(path)
        book.calendars = calendars if calendars is not None else book._default_calendars(path)
        return book

    def _default_bands(self, path: str | Path | None) -> dict[str, Band]:
        """The managed bands to hand the surfaces, from the ``PEG_BANDS`` tab.

        Loaded here rather than left to the caller because a peg is a property
        of the pair, not of the screen looking at it: a build where the
        pricing tab knew about the band and the analysis tab did not would be
        the same silent half-answer as a swallowed error.  A tab that is not
        there, or cannot be read, is a warning and no bands -- the rest of the
        marks are unaffected by it, and a warning is how a desk finds out that
        the BAND method has nothing to work with.
        """
        found = Path(path) if path else None
        if found is None:
            return {}
        try:
            return {k: v for k, v in load_bands(found).items() if v.upper > v.lower > 0}
        except (OSError, ValueError) as exc:
            self.warnings.append(f"managed bands: {exc}")
            return {}

    def _default_wing_ratios(self, path: str | Path | None) -> dict[str, dict]:
        """The ``WING_RATIOS`` tab, or nothing if the workbook has not got it.

        Not having it is the ordinary case for a workbook that predates the
        tab: every wing is then quoted in its own right, which is what every
        workbook did before. A tab that cannot be *read* is a different thing
        and is a warning, because a desk that wrote one meant it to apply.
        """
        if path is None:
            return {}
        try:
            return load_wing_ratios(path)
        except (OSError, ValueError) as exc:
            self.warnings.append(f"wing ratios: {exc}")
            return {}

    def _default_vega_weights(self, path: str | Path | None) -> VegaWeights:
        """The ``Vega Weights`` tab, or an absent one.

        A workbook without it is the ordinary case and costs nothing: the
        marking screen's bump says there is no shape to share a move out by
        and every other number is unaffected.  A tab that is there and cannot
        be read is a warning for the same reason the wing ratios are -- a desk
        that wrote one meant it to apply.
        """
        if path is None:
            return VegaWeights()
        try:
            return load_vega_weights(path)
        except (OSError, ValueError) as exc:
            self.warnings.append(f"vega weights: {exc}")
            return VegaWeights()

    def _default_calendars(self, path: str | Path | None) -> CalendarSet:
        """The shared calendars plus whatever the ``HOLIDAYS`` tab adds.

        A copy, never the shared default: a lunar holiday belongs to the
        workbook that lists it, and a book that quietly added dates to the
        process-wide calendar would change the expiry of every other book
        loaded after it.  A workbook with no such tab gets the shared set
        itself, unchanged, so nothing pays for a tab it has not got.
        """
        if path is None:
            return self.calendars
        cal = CalendarSet(
            use_package=DEFAULT_CALENDARS.use_package,
            overrides={k: set(v) for k, v in DEFAULT_CALENDARS.overrides.items()},
            removals={k: set(v) for k, v in DEFAULT_CALENDARS.removals.items()},
        )
        try:
            added = cal.load_overrides_sheet(path)
        except (OSError, ValueError) as exc:
            self.warnings.append(f"holidays: {exc}")
            return DEFAULT_CALENDARS
        if added is None:
            return DEFAULT_CALENDARS
        if added:
            self.data.notes.append(
                f"HOLIDAYS: {added} date(s) read from the workbook")
        return cal

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

        The arithmetic itself lives in ``feed.MarketFeed.level``, because that
        is where the legs' two spot rates and two swap points are and because
        there must be exactly one of it -- a second copy is a second place for
        a sign to be written upside down, which is what §5's first entry cost.
        What this adds is the *workbook's* opinion about which legs a cross
        has: a sheet that names them is not second-guessed by a convention,
        and a cross nobody named still has the one sensible pair of dollar
        legs the market quotes.
        """
        feed = self.feed
        if feed is None:
            return None
        def declared(name: str):
            spec = self.data.pairs.get(name)
            return tuple(getattr(spec, "legs", ()) or ()) or None
        return feed.level(pair, t, declared, trail)

    def spot_date(self, pair: str) -> date:
        """Where a spot trade in ``pair`` dealt today settles, on this clock."""
        return self.calendars.spot_date(pair, self.clock.now.date())

    def stated_date(self, value) -> date:
        """A date a caller stated in words, on this book's clock.

        ``timeutil.parse_datetime`` is the one timestamp reader, and the year
        a date written without one means comes from the **book's** clock and
        not from the machine -- the same rule the expiry box is read under.
        """
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return parse_datetime(str(value), today=self.clock.now.date()).date()

    def settlement_date(self, pair: str, expiry) -> date:
        """Where an option expiring on ``expiry`` settles, on the pair's calendar."""
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        return self.calendars.delivery_from_expiry(pair, expiry)

    def settlement_years(self, pair: str, expiry, settle=None) -> float:
        """Where an option's settlement date sits on the feed's axis.

        The feed's one axis is **years from the spot date** (``feed`` module
        docstring), and this puts a settlement date on it: the days from this
        book's own spot date to the option's, over the year length.  A 1M
        pillar is placed by the feed the same way -- from its spot date to its
        1M delivery date -- so a 1M option lands *on* the pillar rather than
        between two of them.

        An offset and not an absolute date, deliberately.  A feed file carries
        its own spot date, and a file written last Tuesday and priced today
        has one a few days behind this book's.  Read as an absolute date, a
        3M option then asks the curve for a date three months past the file's
        own three-month pillar -- or, with a file stamped a year out, falls
        off the end of the curve entirely and comes back at spot.  Read as an
        offset it asks for "the three-month point", which is what the pillar
        is, and the staleness stays what it is: a note on the feed, not a
        forward silently held flat.

        ``settle`` is the settlement date **stated** rather than derived -- a
        broken date, or a trade the desk has agreed to settle somewhere the
        calendar would not have put it.  The placement is unchanged: it is
        still this one piece of arithmetic and still an offset, which is the
        point of the paragraph above.  What the caller may hold a second
        opinion about is the *date*, never where a date lands.
        """
        d = self.settlement_date(pair, expiry) if settle is None else self.stated_date(settle)
        return (d - self.spot_date(pair)).days / DAYS_IN_YEAR

    def market_level_for(self, pair: str, expiry, settle=None) -> dict:
        """The level an option expiring on ``expiry`` is priced against.

        **This is what a screen should call.**  ``market_level`` reads the
        curve at a time; this reads it where the option's own *settlement*
        date sits, which is the date a forward is actually a price for.  The
        two differ by the spot lag -- two business days of swap points, which
        on a one-week option is a fifth of them.

        ``spot_date`` and ``settle`` travel back with the level, because a
        screen that shows a forward should be able to say what date it is a
        forward to.  It takes the **expiry** and no year fraction: where a
        settlement date lands on the feed's axis is not something a caller
        gets to hold a second opinion about.

        ``settle`` states the settlement date instead of deriving it, for the
        one case the calendar cannot answer: a trade settling on a broken
        date.  It is the date that moves, not the placement -- the level is
        still read through ``settlement_years``, still as an offset from the
        book's own spot date -- and it is a *date*, not a year fraction, so
        there is still exactly one way a forward is placed.  ``settle`` comes
        back in the answer either way, so a screen shows the date the level
        was actually read on.
        """
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        elif not isinstance(expiry, date):
            raise TypeError(f"expiry must be a date or datetime, got {expiry!r}")
        settle = (self.settlement_date(pair, expiry) if settle is None
                  else self.stated_date(settle))
        out = self.market_level(pair, self.settlement_years(pair, expiry, settle))
        out["expiry"] = expiry.isoformat()
        out["settle"] = settle.isoformat()
        out["spot_date"] = self.spot_date(pair).isoformat()
        return out

    def tenor_years(self, pair: str, tenor: str) -> float:
        """Years to a tenor's calendar expiry -- the one tenor-to-time reading."""
        return self.calendars.expiry_years(pair, tenor, self.clock)

    def fx_dates(self, pair: str, tenor: str):
        """Trade, spot, expiry and settlement dates for a tenor on this book's clock."""
        return self.calendars.fx_dates(pair, tenor, self.clock.now.date())

    def forward_at(self, pair: str, t: float, expiry=None) -> float | None:
        """The outright forward from the feed, or None when there is no feed.

        ``expiry`` is the option's expiry date, and given one the forward is
        the one to that option's settlement date.  Without one the curve is
        read at ``t`` years, which is the same axis placed nominally.
        """
        if expiry is not None:
            return self.market_level_for(pair, expiry)["forward"]
        return self.market_level(pair, t)["forward"]

    def _attach_band(self, name: str, surface: VolSurface) -> None:
        """Give a surface its band and a way to place it.

        The lookup is a closure over the book rather than a captured feed, so
        a feed loaded *after* the book was built -- which is what ``serve``
        and the analysis CLI both do -- is picked up on the next query.
        """
        surface.band = self.bands.get(name.upper())
        if surface.band is not None:
            # A band is an absolute price range, so the forward that places it
            # is the forward the option settles at -- the same one the strike
            # axis is scaled by.  ``t`` comes back from a slice, so the expiry
            # date it stands for is read off the same clock the slice was.
            surface.forward_lookup = lambda t, p=name: self.forward_at(
                p, t, expiry=self.clock.datetime_from_years(t).date())

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
        for entry in self.events.for_pair(name, touching_only=True):
            when = entry.when
            if when <= self.clock.now:
                # A past event cannot be calibrated: its volatility day has
                # already elapsed, so the inversion would integrate the
                # backbone backwards in time.
                self.warnings.append(
                    f"{name}: event {when:%Y-%m-%d %H:%M}Z is before the valuation time "
                    f"{self.clock.now:%Y-%m-%d %H:%M}Z and was skipped"
                )
                continue
            events.add(when, entry.bump, label=entry.label or when.strftime("%d%b %H:%M"),
                       weights=entry.weights, adjust=entry.adjust)

        common = dict(
            pair=name, clock=self.clock, weighting=weighting, events=events,
            tenor_points=tuple(self.data.tenor_points), calendars=self.calendars,
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
            wing_ratios=dict(self.wing_ratios.get(name, {})),
        )
        self._attach_band(name, surface)
        # The marks a session wrote into the workbook (the ``atm 1m`` /
        # ``shift rho25`` rows and the BANDS sheet) go on through the same
        # function that puts a session file on, so the two cannot disagree
        # about what a cell means.  The band is attached first because the
        # treatment is about it.
        block = self.data.overlays.get(name)
        if block:
            from .session import apply_block
            self.warnings.extend(f"{name}: workbook session mark: {p}"
                                 for p in apply_block(surface, block))
        return surface

    # -- calibration ------------------------------------------------------
    def calibrate_smiles(self, pairs: list[str] | None = None) -> "Book":
        """Fit smiles for every pair that has quotes."""
        for name, surface in self.surfaces.items():
            if pairs and name not in pairs:
                continue
            marks = self.data.marks.get(name)
            # A pair whose only quotes were typed on the marking screen still
            # has a smile to fit: the sheet is where quotes usually come from,
            # not where they have to come from.
            if not marks and not surface.quote_overwrites:
                # A pair asked for by name and left uncalibrated is the one
                # case worth saying out loud: every later call on it raises
                # "no smile term structure", which reads like a caller that
                # forgot to calibrate rather than a workbook with no quotes.
                # A pair swept up by ``calibrate_smiles()`` with no argument
                # is not -- ``load_all`` builds a cross's legs on purpose and
                # the reader has already reported any sheet that is missing.
                if pairs:
                    self.warnings.append(
                        f"{name}: no smile quotes in the workbook, so its smile was not "
                        f"fitted; every smile on this pair will refuse"
                    )
                continue
            surface.calibrate(marks or [])
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
        # A reload is the workbook's own table again, marks and all: the
        # session's edits went with the surfaces they were made on.
        self.events = self.data.events.copy()
        self.surfaces.clear()
        self.warnings.clear()
        return self.load_all()

    def apply_events(self, pairs=None) -> list[str]:
        """Put the event table back onto the pairs it moves and re-solve.

        A currency weight is shared, so a row edited on one screen reaches
        every pair with that currency; the caller does not get to choose
        which, only to narrow the work to pairs already built.  Returns the
        problems the curves raised, each named by its pair.
        """
        names = list(self.surfaces) if pairs is None else \
            [str(p).upper() for p in pairs if str(p).upper() in self.surfaces]
        problems: list[str] = []
        for name in names:
            surface = self.surfaces[name]
            wanted = self.events.for_pair(name, touching_only=True)
            # A weight reaches every pair with that currency, but most of them
            # by nothing at all.  Re-solving a schedule that has not moved
            # costs a calibration per event and buys nothing.
            have = [(e.when, e.bump, e.label) for e in surface.atm.events.events]
            if have == [(e.when, e.bump, e.label) for e in wanted
                        if e.when > self.clock.now]:
                continue
            for msg in surface.atm.set_events(wanted):
                problems.append(f"{name}: {msg}")
            surface.invalidate()
        return problems

    # -- access -----------------------------------------------------------
    def load_bands(self, path: str | Path | None = None) -> "Book":
        """Attach managed-band definitions and hand them to their surfaces.

        ``path`` is a workbook whose ``PEG_BANDS`` tab holds them; the book's
        own workbook when nothing else is named.
        """
        source = path or self.data.source or None
        self.bands = {k: v for k, v in load_bands(source).items() if v.upper > v.lower > 0}
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
