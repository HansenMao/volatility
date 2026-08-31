"""Spot and forward points from a file, with interpolation between the pillars.

A desk feed publishes spot plus swap points.  An option rarely expires exactly
on a published pillar, so the tool interpolates.

**A pillar is named two ways and both are read.**  A *tenor* (``1W``, ``3M``)
is the standard broker column and carries the swap points from the spot date
out to that tenor.  A *date* is the same statement made exactly, and it is
what a feed built off a bank's own forward file looks like -- and the front of
such a file is not on standard tenors at all.  It is the overnight and the
tom-next, each quoted as *one day* of points rather than as points from spot,
because points from spot would be zero at spot and there is nowhere else to
put them.  So a dated row is read by where it lands:

* an end date **after** the spot date carries the swap points from spot to
  that date, exactly as a tenor pillar does;
* an end date **on or before** the spot date carries the points for the single
  day ending on it -- the interval ``(end - 1, end]`` -- which is what O/N and
  T/N are;
* an end date on or before the valuation date is history and is passed over
  with the reason.  A forward that has already delivered is not a forward.

The curve that comes out is one function of the delivery date either way: zero
at the spot date, the cumulative points at every quoted date, and a straight
line between the two nearest quoted dates for every date in between.  On the
near side it is the *daily rates* that are interpolated and then accumulated
back from spot, because a rate is what those rows state; the cumulative curve
they produce is therefore not a straight line through them, and should not be.

Requests outside the quoted range are answered by holding the nearest knot
flat and are flagged as extrapolated rather than silently trended -- running a
linear fit off the end of a swap curve is how a 5y forward ends up negative.

Placing a dated row needs a spot date, and a spot date needs a valuation date.
This module never asks the operating system for one: it takes ``today`` from
its caller (the book's clock), or from the file's own ``asof`` line, or -- best
of all -- takes the spot date the file states for itself.  Given none of the
three it refuses the dated rows and says so, rather than guessing a date that
would move every point on the near side.  The clock is injected here as it is
everywhere else in this package.

One axis, and it is the one every expiry in this package is measured on: ``t``
years, zero at the spot date.  A tenor pillar sits at ``tenor_to_years`` and a
dated pillar at the days from spot over 365.2425, which is the same number for
the same pillar -- so nothing that already priced off a tenor feed moves when
a dated one is loaded beside it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from . import paths
from .calendars import DEFAULT_CALENDARS, CalendarSet
from .cross import dollar_legs, infer_leg_signs
from .timeutil import DAYS_IN_YEAR, TenorError, parse_datetime, parse_tenor, tenor_to_years

# Term-currency pip divisor: JPY-quoted pairs move in 0.01, most others 0.0001.
PIP_DIVISORS: dict[str, float] = {"JPY": 100.0, "KRW": 1.0, "CLP": 1.0, "HUF": 100.0}
DEFAULT_PIP = 10000.0

SPOT_KEYS = {"spot", "s/n", "sn", "0d"}
# A file may state its own spot date rather than leave it to be derived.  A
# publisher knows its own holidays and this tool's calendar may disagree with
# them, and the near side of the curve is placed against that date: getting it
# wrong by a day moves the tom-next onto the overnight.
SPOT_DATE_KEYS = {"spotdate", "spot date", "spot_date", "valuedate", "value date",
                  "settlement", "settle date"}


def pip_divisor(pair: str) -> float:
    return PIP_DIVISORS.get(pair[3:6].upper(), DEFAULT_PIP)


class FeedError(ValueError):
    """Raised when the feed file cannot be interpreted."""


def _as_date(value) -> date:
    """A date from a date, a datetime or any spelling ``parse_datetime`` reads."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_datetime(str(value)).date()


@dataclass
class PairFeed:
    """Spot and a term structure of forward points for one pair.

    ``tenors`` and ``points`` are the far side: a label (a tenor, or a date
    written out) and the swap points from the spot date to it.  ``times`` is
    the year fraction of each label, and is ``None`` when every label is a
    tenor -- which keeps the original contract exactly, since the years then
    come from the tenor and nothing else has to be supplied.

    ``daily`` is the near side, in the shape those rows are quoted: an end
    date and the points for the single day ending on it.  It is turned into
    the same cumulative curve by ``_near_knots``, and needs ``spot_date``,
    which is the one date the whole curve is anchored to.
    """

    pair: str
    spot: float
    tenors: list[str] = field(default_factory=list)
    points: list[float] = field(default_factory=list)
    pip: float = DEFAULT_PIP
    times: list[float] | None = None
    daily: list[tuple[date, float]] = field(default_factory=list)
    spot_date: date | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        years = (list(self.times) if self.times is not None
                 else [tenor_to_years(x) for x in self.tenors])
        order = list(np.argsort(years)) if years else []
        self.tenors = [self.tenors[i] for i in order]
        self.points = [float(self.points[i]) for i in order]
        years = [years[i] for i in order]
        if self.times is not None:
            self.times = years
        self.daily = sorted(((_as_date(d), float(v)) for d, v in self.daily),
                            key=lambda r: r[0])
        if self.daily and self.spot_date is None:
            raise FeedError(f"{self.pair}: day rates need a spot date to accumulate back from")
        late = [d for d, _ in self.daily if d > self.spot_date] if self.daily else []
        if late:
            # A day rate is the interval ending on its own date, so one after
            # the spot date is not on the near side at all -- it is a pillar
            # that has been handed in wearing the wrong units, and there is no
            # reading of it that is not a guess.
            raise FeedError(f"{self.pair}: {late[0]} is after the spot date {self.spot_date}, "
                            f"so it is points from spot and not a day rate")
        knots: dict[float, float] = dict(zip(years, self.points))
        knots.update(self._near_knots())
        if knots:
            # The spot date is the one knot every curve has -- zero points, by
            # definition -- and it is what scales the front pillar down toward
            # spot instead of holding it flat across the very short end.  It is
            # added only where there is a curve for it to anchor: a feed of
            # spot and nothing else still answers 0 for every tenor and calls
            # none of them extrapolated, exactly as it always did.
            knots.setdefault(0.0, 0.0)
        ordered = sorted(knots)
        self._ts = np.array(ordered, dtype=float)
        self._ps = np.array([knots[t] for t in ordered], dtype=float)

    # -- the near side -----------------------------------------------------

    def _years_to(self, d: date) -> float:
        """Years from the spot date to a delivery date; negative before it."""
        if self.spot_date is None:
            raise FeedError(f"{self.pair}: no spot date, so a delivery date cannot be placed")
        return (d - self.spot_date).days / DAYS_IN_YEAR

    def _near_knots(self) -> dict[float, float]:
        """Cumulative points at every date from the earliest quoted day to spot.

        Each row states the points for the single day ending on its date, so
        the curve is built by *accumulating* them backwards from the spot
        date, where the points are zero by definition.  A day between two
        quoted days takes a linearly interpolated rate, and one outside them
        takes the nearest quoted rate held flat -- the same refusal to trend
        off the end that the far side makes.
        """
        if not self.daily or self.spot_date is None:
            return {}
        xs = np.array([d.toordinal() for d, _ in self.daily], dtype=float)
        ys = np.array([v for _, v in self.daily], dtype=float)
        first = self.daily[0][0]
        out = {0.0: 0.0}
        cum = 0.0
        d = self.spot_date
        while d >= first:
            cum -= float(np.interp(float(d.toordinal()), xs, ys))
            out[self._years_to(d - timedelta(days=1))] = cum
            d -= timedelta(days=1)
        return out

    # -- reading the curve -------------------------------------------------

    def forward_points(self, t: float) -> tuple[float, bool]:
        """Interpolated swap points at ``t`` years, and whether it extrapolated."""
        if self._ts.size == 0:
            return 0.0, False
        lo, hi = float(self._ts[0]), float(self._ts[-1])
        if self._ts.size == 1:
            return float(self._ps[0]), bool(t != lo)
        outside = bool(t < lo - 1e-12 or t > hi + 1e-12)
        # np.interp holds the end knots flat outside the range, which is the
        # behaviour wanted; ``outside`` is what says it happened.
        return float(np.interp(t, self._ts, self._ps)), outside

    def forward(self, t: float) -> tuple[float, bool]:
        pts, extrap = self.forward_points(t)
        return self.spot + pts / self.pip, extrap

    def points_on(self, when) -> tuple[float, bool]:
        """The swap points to a delivery *date*, and whether it extrapolated.

        The near side of the curve is only reachable this way: a delivery
        before the spot date is a negative time, which no expiry produces.
        """
        return self.forward_points(self._years_to(_as_date(when)))

    def forward_on(self, when) -> tuple[float, bool]:
        pts, extrap = self.points_on(when)
        return self.spot + pts / self.pip, extrap

    @property
    def knots(self) -> list[tuple[float, float]]:
        """Every knot the curve interpolates between: years from spot, points."""
        return [(float(t), float(p)) for t, p in zip(self._ts, self._ps)]


def compose_level(pair: str, sign_a: int, sign_b: int, legs, a: dict, b: dict) -> dict | None:
    """One cross level out of its two legs': the implied rate, and nothing else.

    The first leg carries the cross's base currency and the second its term,
    and each is turned the right way up before they meet.  The signs are the
    triangle's own (:func:`cross.infer_leg_signs`), read here as *quotation*
    rather than as correlation: +1 on the first leg means it already reads
    (base)/(common), +1 on the second means it reads (term)/(common) and so
    enters inverted.  EURJPY is EURUSD x USDJPY; EURGBP is EURUSD / GBPUSD.

    Spot and the outright are composed **separately**, each from the legs'
    own, which is what "implied from the two spot rates and the two swap
    points" means.  The cross's points are then the composed outright less the
    composed spot, in the cross's own pips -- never the legs' points added: a
    point of EURUSD and a point of USDJPY are different amounts of money and
    their sum is not a number anybody quotes.
    """
    if min(a["spot"], a["forward"], b["spot"], b["forward"]) <= 0:
        return None

    def compose(x_a: float, x_b: float) -> float:
        first = x_a if sign_a > 0 else 1.0 / x_a
        second = 1.0 / x_b if sign_b > 0 else x_b
        return first * second

    spot = compose(a["spot"], b["spot"])
    forward = compose(a["forward"], b["forward"])
    pip = pip_divisor(pair)
    return {"pair": pair, "spot": spot, "forward": forward,
            "points": (forward - spot) * pip, "pip": pip,
            "extrapolated": bool(a["extrapolated"] or b["extrapolated"]),
            "via": f"{legs[0]} and {legs[1]}", "derived": True}


@dataclass
class MarketFeed:
    """Spot and forward points for every pair in a feed file."""

    pairs: dict[str, PairFeed] = field(default_factory=dict)
    source: str = ""
    asof: str = ""
    problems: list[str] = field(default_factory=list)
    # What was read that was neither wrong nor obvious: a spot date derived
    # rather than stated, a row passed over as history, a file written on
    # another day from the one being priced.  Reported, never silent.
    notes: list[str] = field(default_factory=list)
    today: date | None = None

    @classmethod
    def load(cls, path: str | Path, today=None,
             calendars: CalendarSet | None = None) -> "MarketFeed":
        """Read a ``pair,label,value`` CSV.

        ``label`` is ``SPOT`` for the spot rate, ``SPOT DATE`` for the file's
        own settlement date, a tenor whose value is the forward points at that
        pillar, or a **date** -- read as points from spot when it falls after
        the spot date and as that single day's points when it falls on or
        before it.  See the module docstring.

        ``today`` is the valuation date the dated rows are placed against; it
        comes from the caller's clock and is never taken from the machine.
        """
        path = Path(path)
        if not path.exists():
            raise FeedError(f"feed file not found: {path}")
        feed = cls(source=str(path))
        cal = calendars if calendars is not None else DEFAULT_CALENDARS
        spots: dict[str, float] = {}
        stated: dict[str, date] = {}
        pillars: dict[str, list[tuple[str, float]]] = {}
        dated: dict[str, list[tuple[date, float]]] = {}
        # Read whole, and closed: this file is published onto a desk share
        # and is often open in Excel at the other end.  A reader left alive
        # holds it there -- the same lock the workbooks had.
        with paths.open_text(path, newline="") as fh:
            rows = list(csv.reader(fh))
        for lineno, row in enumerate(rows, start=1):
            if not row or row[0].lstrip().startswith("#"):
                if row and "asof" in row[0].lower():
                    feed.asof = row[0].split(":", 1)[-1].strip()
                continue
            if len(row) < 3:
                feed.problems.append(f"line {lineno}: expected 'pair,tenor,value', got {row!r}")
                continue
            pair, label, raw = row[0].strip().upper(), row[1].strip(), row[2].strip()
            key = label.lower()
            if key in SPOT_DATE_KEYS:
                # This one's value is a date and not a number, so it is
                # dispatched before anything tries to read it as one.
                try:
                    stated[pair] = _as_date(raw)
                except ValueError:
                    feed.problems.append(
                        f"line {lineno}: {pair} spot date {raw!r} is not a date")
                continue
            try:
                value = float(raw)
            except ValueError:
                feed.problems.append(f"line {lineno}: {pair} {label} value {raw!r} is not a number")
                continue
            if key in SPOT_KEYS:
                if value <= 0:
                    feed.problems.append(f"line {lineno}: {pair} spot must be positive, got {value}")
                    continue
                spots[pair] = value
                continue
            # A tenor first, so that nothing a tenor feed already reads moves,
            # and so that "1M" can never be read as a day of the month.
            try:
                parse_tenor(label)
            except TenorError as exc:
                tenor_problem = exc
            else:
                pillars.setdefault(pair, []).append((label, value))
                continue
            try:
                when = _as_date(label)
            except ValueError:
                feed.problems.append(f"line {lineno}: {tenor_problem}, and it is not a date either")
                continue
            dated.setdefault(pair, []).append((when, value))

        feed.today, stamp_note = cls._valuation_date(feed, today)
        for pair, spot in spots.items():
            feed.pairs[pair] = feed._build(pair, spot, pillars.pop(pair, []),
                                           dated.pop(pair, []), stated.get(pair), cal)
        # Said only where it changes something.  A feed of tenor pillars is
        # placed identically whatever day it was written on, so reporting the
        # gap there is a line printed on every run that nothing turns on -- the
        # quiet case has to stay quiet or the noisy one stops being read.  With
        # a dated row in the file the gap moves the spot date, and with it the
        # whole near side.
        if stamp_note and any(p.spot_date for p in feed.pairs.values()):
            feed.notes.insert(0, stamp_note)
        for pair in sorted(set(pillars) | set(dated)):
            feed.problems.append(f"{pair}: forward points supplied but no SPOT row")
        return feed

    @staticmethod
    def _valuation_date(feed: "MarketFeed", today) -> tuple[date | None, str]:
        """The date the dated rows are placed against, and anything odd about it.

        The caller's clock first and the file's own ``asof`` line second: a
        valuation in the past is an ordinary thing to ask this tool for, and
        it is the model's clock that every other time in the package is
        measured from.  Neither leaves the dated rows unplaceable in silence
        -- ``_build`` refuses them by name.
        """
        stamped = None
        if feed.asof:
            try:
                stamped = _as_date(feed.asof)
            except ValueError:
                stamped = None
        if today is None:
            return stamped, ""
        given = _as_date(today)
        if stamped is not None and stamped != given:
            # A feed published on another day than the one being priced is a
            # fact about the market, and is said rather than absorbed into a
            # spot date nobody can check.
            return given, (f"the file is written as of {stamped}, "
                           f"and is being priced as of {given}")
        return given, ""

    def _build(self, pair: str, spot: float, tenor_rows: list[tuple[str, float]],
               date_rows: list[tuple[date, float]], stated: date | None,
               cal: CalendarSet) -> PairFeed:
        """One pair's curve: the tenor pillars, and the dated rows placed."""
        notes: list[str] = []
        spot_date = stated
        if date_rows and spot_date is None:
            if self.today is None:
                self.problems.append(
                    f"{pair}: {len(date_rows)} dated row(s) cannot be placed without a "
                    f"valuation date; state one as '{pair},SPOT DATE,<date>', add an "
                    f"'# asof: YYYY-MM-DD' line, or load the feed with a clock")
                date_rows = []
            else:
                spot_date = cal.spot_date(pair, self.today)
                notes.append(f"spot date {spot_date}, {cal.spot_lag(pair)} business days "
                             f"after {self.today} on the {pair} calendar")
        elif date_rows:
            notes.append(f"spot date {spot_date}, as the file states it")
            if self.today is None:
                # Nothing to measure "already delivered" against, so nothing
                # is passed over.  Said, because a stale row left on the curve
                # drags its front end back onto a date that has been and gone.
                notes.append("no valuation date, so no row was passed over as history")

        far: list[tuple[date, float]] = []
        near: list[tuple[date, float]] = []
        seen: set[date] = set()
        for when, value in sorted(date_rows):
            if when in seen:
                self.problems.append(f"{pair}: {when} is quoted twice")
                continue
            seen.add(when)
            if self.today is not None and when <= self.today:
                notes.append(f"{when} is on or before the valuation date {self.today} "
                             f"and was passed over")
                continue
            (far if when > spot_date else near).append((when, value))

        labels = [r[0] for r in tenor_rows]
        values = [float(r[1]) for r in tenor_rows]
        if len(set(labels)) != len(labels):
            self.problems.append(f"{pair}: a tenor pillar is quoted twice ({', '.join(labels)})")
        times = None
        if far:
            # Once a dated pillar is in, every label carries its own year
            # fraction: a date has no tenor to be read back out of it.
            times = [tenor_to_years(x) for x in labels]
            for when, value in far:
                labels.append(when.isoformat())
                values.append(value)
                times.append((when - spot_date).days / DAYS_IN_YEAR)
        if near:
            notes.append(f"{len(near)} day rate(s) to the spot date: "
                         + ", ".join(f"{d.isoformat()} {v:+g}" for d, v in near))
        self.notes.extend(f"{pair}: {n}" for n in notes)
        return PairFeed(pair=pair, spot=spot, tenors=labels, points=values,
                        pip=pip_divisor(pair), times=times, daily=near,
                        spot_date=spot_date, notes=notes)

    def __contains__(self, pair: str) -> bool:
        return pair.upper() in self.pairs

    # -- levels, quoted and implied ----------------------------------------
    #
    # A file that quotes EURUSD and USDJPY is quoting EURJPY, and refusing the
    # cross by name while pricing both of its legs off the same file is a feed
    # that is loaded and cannot be seen.  So a pair the file does not hold is
    # *composed* from the two dollar pairs the market does quote -- their two
    # spot rates and their two swap points, which is all an implied cross rate
    # has ever been.  It is triangular arbitrage and not a model.
    #
    # This lives here rather than a level above because this is where the two
    # legs' spots and points are, and because there must be exactly one such
    # arithmetic: ``Book._feed_level`` calls it and adds only the workbook's
    # opinion about which legs a cross has.  ``derived`` and ``via`` travel
    # with every composed level, because a level that came out of an identity
    # and one that was published must not read the same.

    def level(self, pair: str, t: float, legs_for=None,
              trail: tuple[str, ...] = ()) -> dict | None:
        """Spot and the outright forward at ``t`` years, quoted or implied."""
        return self._level(pair, lambda pf: pf.forward_points(t), legs_for, trail)

    def level_on(self, pair: str, when, legs_for=None,
                 trail: tuple[str, ...] = ()) -> dict | None:
        """The same, to a delivery *date* rather than to a time.

        Each leg is read on its **own** spot date, which is the exact reading
        and the one the near side needs: a cross of a T+1 pair and a T+2 pair
        has two different dates at one ``t``, and the tom-next is a day wide.
        """
        return self._level(pair, lambda pf: pf.points_on(when), legs_for, trail)

    def _level(self, pair: str, ask, legs_for, trail: tuple[str, ...]) -> dict | None:
        """One pair's level, quoted or composed.  ``None`` when neither works.

        ``ask`` is how a pair that *is* quoted is read -- at a time or on a
        date -- and is the only difference between the two entry points, so
        the triangle is written once.  ``legs_for`` is the caller's opinion
        about which legs a cross has (the workbook names some); without one,
        or when it has none for this pair, the legs are the two dollar pairs
        the market quotes.  ``trail`` is what stops a cross of a cross walking
        in a circle.
        """
        key = pair.upper()
        if key in self.pairs:
            pf = self.pairs[key]
            points, extrapolated = ask(pf)
            return {"pair": key, "spot": float(pf.spot),
                    "forward": float(pf.spot + points / pf.pip),
                    "points": float(points), "pip": float(pf.pip),
                    "extrapolated": bool(extrapolated), "via": "", "derived": False}
        if key in trail:
            return None
        legs = tuple((legs_for(key) if legs_for is not None else None) or ())
        if len(legs) != 2:
            try:
                legs = dollar_legs(key)
            except ValueError:
                # Not a cross, or not a pair at all: there is no triangle to
                # try, and a dollar pair the file does not quote is simply not
                # quoted.
                return None
        try:
            sign_a, sign_b = infer_leg_signs(key, legs[0], legs[1])
        except ValueError:
            return None
        a = self._level(legs[0], ask, legs_for, trail + (key,))
        b = self._level(legs[1], ask, legs_for, trail + (key,))
        # Half a triangle is still a refusal: no NZDUSD in the file, no
        # GBPNZD forward, and it says so rather than reaching for one leg.
        if a is None or b is None:
            return None
        return compose_level(key, sign_a, sign_b, legs, a, b)

    def quote(self, pair: str, t: float, legs_for=None) -> dict:
        """Spot, points and forward for a pair at ``t`` years.

        A pair the file does not quote is composed from its legs; only a
        triangle that cannot be completed is a refusal, and it says which
        half is missing.
        """
        return self._quote(pair, self.level(pair, t, legs_for))

    def quote_on(self, pair: str, when, legs_for=None) -> dict:
        """The same, to a delivery date -- the only way to the near side."""
        return self._quote(pair, self.level_on(pair, when, legs_for))

    def _quote(self, pair: str, level: dict | None) -> dict:
        key = pair.upper()
        if level is None:
            raise FeedError(self._why_not(key))
        pf = self.pairs.get(key)
        out = dict(level)
        out["pillars"] = pf.tenors if pf is not None else []
        out["spot_date"] = (pf.spot_date.isoformat() if pf is not None and pf.spot_date
                            else "")
        out["notes"] = list(pf.notes) if pf is not None else []
        return out

    def _why_not(self, key: str) -> str:
        """Why a pair has no level: not quoted, and which leg is missing."""
        base = f"no feed for {key!r}; have {sorted(self.pairs)}"
        try:
            legs = dollar_legs(key)
        except ValueError:
            return base
        missing = [x for x in legs if x not in self.pairs]
        if not missing:
            return base
        return (f"{base}. It would be implied from {legs[0]} and {legs[1]}, "
                f"but the file does not quote {' or '.join(missing)}")

    def summary(self) -> list[dict]:
        return [{"pair": p.pair, "spot": p.spot, "pillars": len(p.tenors),
                 "range": f"{p.tenors[0]}–{p.tenors[-1]}" if p.tenors else "—",
                 "days": len(p.daily),
                 "spot_date": p.spot_date.isoformat() if p.spot_date else ""}
                for p in self.pairs.values()]


def load_for(book, path: str | Path) -> MarketFeed:
    """Read a feed against a book's own clock and calendars.

    The valuation date a dated row is placed against is the *model's*, not the
    machine's: one clock per book, and the same clock everywhere (§4).  Every
    caller that has a book goes through here, so a dated feed cannot be placed
    one way on a screen and another way in the batch command beside it.
    """
    return MarketFeed.load(path, today=book.clock.now.date(),
                           calendars=getattr(book, "calendars", None))
