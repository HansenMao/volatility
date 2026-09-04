"""What the tape has been doing: paid against given, weighted by vega.

The dissemination file publishes what printed and never who bought.  There is
no aggressor flag, no buyer, no seller -- the data is anonymised by design --
so the direction of a trade is not in the file and cannot be read out of it.
It can only be **inferred**, and this module is the one place that inference
lives, so there is exactly one answer to "why does the screen think that was
paid" and it can be argued with.

The inference is the one a desk makes by hand: a print above our mid was paid
and one below it was given.  Three things make that honest rather than glib:

**It is judged against a named mark.**  Every classified print carries the
volatility we had at that strike and that expiry, so a print called "paid"
against a mark that was too low can be found afterwards and argued about.  The
mark is the surface *as it stands now*, not as it stood that morning -- the
archive does not keep the curve of every past day -- which is said on the read
and is the reason the default half-life is a working week.

**A print near the mark is not evidence.**  Inside a tolerance the trade is
counted as ``unclear`` rather than pushed to whichever side it fell on.  The
tolerance is half the market's own width when the archive knows one for that
bucket, and a fraction of the mark when it does not, because a print two
hundredths of a point over a mid on a pair that trades a quarter wide says
nothing about demand.

**Size is vega, not notional.**  A hundred million of a one-week option and a
hundred million of a two-year one are not the same amount of buying, and the
number a desk leans on is the vega.  It is reported in the **base currency per
volatility point**, which is the unit a vega axe is typed in on the quote
panel, so the two can be compared without conversion.

What this module does *not* do is decide what to do about any of it.  It
produces a signed, age-weighted vega per tenor bucket and stops; whether that
leans a quote, and how far, is `marketmaker.skew_for`'s decision and is capped
there like every other lean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from . import black
from .archive import Archive
from .synthesis import (DEFAULT_HALF_LIFE, DEFAULT_MIN_EFFECTIVE, Synthesis, bucket_of,
                        invert_trades, tape_forwards)

#: How far from the mark a print has to be before it is evidence of anything,
#: when the archive has no width for that bucket to measure against.  Three
#: per cent of the mark: a quarter of a point on an eight-vol pair, which is
#: about the width such a pair is shown in.
DEFAULT_TOLERANCE = 0.03

#: The net vega, in the base currency per volatility point, that counts as a
#: full lean.  Deliberately large: the flow lean should be a nudge inside the
#: width on an ordinary day and reach its cap only when the tape has been one
#: way all week.  A whole street's reported tape is bigger than one desk's
#: book -- five days of G10 dissemination nets into the millions per point --
#: so this is calibrated against the card's own net vega column and not
#: against a position.
DEFAULT_SCALE = 5_000_000.0


@dataclass(frozen=True)
class FlowPrint:
    """One printed trade, and which side of our mark it printed on."""

    at: str
    days: float
    bucket: str
    strike: float
    is_call: bool
    vol: float                  # volatility points, inverted from the premium
    mark: float | None          # our own volatility at that strike, in points
    tolerance: float            # how far from the mark it had to be to count
    notional: float             # base currency
    vega: float                 # base currency per volatility point
    side: str                   # paid | given | unclear | unmarked
    forward: str                # where the forward came from
    why: str

    def describe(self) -> str:
        side = "call" if self.is_call else "put"
        gap = "" if self.mark is None else f" against a mark of {self.mark:.3f}"
        return (f"{self.bucket} {self.strike:g} {side} {self.side} at {self.vol:.3f}"
                f"{gap}, {self.vega / 1e3:,.0f}k vega")


@dataclass(frozen=True)
class FlowEvidence:
    """One tenor bucket's tape, as a number a quote could lean on."""

    bucket: str
    prints: int
    paid: int
    given: int
    unclear: int
    calls: int
    puts: int
    capped: int                 # prints that could not be sized at all
    paid_vega: float            # age-weighted, base currency per vol point
    given_vega: float
    effective: float            # age-weighted count of classified prints
    newest_days: float
    enough: bool
    why_not: str

    @property
    def net_vega(self) -> float:
        """Positive when the tape has been paying for volatility."""
        return self.paid_vega - self.given_vega

    @property
    def gross_vega(self) -> float:
        return self.paid_vega + self.given_vega

    def net(self, scale: float) -> float | None:
        """The net, as a fraction of the vega that counts as a full axe."""
        if not self.enough or not scale or scale <= 0:
            return None
        return max(-1.0, min(1.0, self.net_vega / float(scale)))

    def describe(self) -> str:
        if not self.prints:
            return f"{self.bucket}: nothing printed"
        way = ("paid" if self.net_vega > 0 else "given" if self.net_vega < 0 else "two-way")
        return (f"{self.bucket}: {self.prints} print(s), {self.paid} paid / {self.given} given"
                f" / {self.unclear} unclear, {self.calls} call / {self.puts} put, net "
                f"{self.net_vega / 1e3:,.0f}k vega {way}, newest {self.newest_days:.0f} days ago"
                + (f" -- {self.why_not}" if not self.enough else ""))


@dataclass
class FlowRead:
    """Everything the tape says about one pair, at one moment."""

    pair: str
    asof: datetime
    half_life: float = DEFAULT_HALF_LIFE
    min_effective: float = DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 30.0
    tolerance: float = DEFAULT_TOLERANCE
    buckets: list[FlowEvidence] = field(default_factory=list)
    prints: list[FlowPrint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def for_days(self, days: float) -> FlowEvidence | None:
        """The bucket a tenor falls in, or ``None`` when it holds nothing."""
        _, label = bucket_of(days)
        for ev in self.buckets:
            if ev.bucket == label:
                return ev
        return None

    def net_at(self, days: float, scale: float) -> tuple[float | None, str]:
        """The lean for one tenor, and the sentence that explains it."""
        ev = self.for_days(days)
        if ev is None:
            return None, f"the tape printed nothing in the {bucket_of(days)[1]} bucket"
        value = ev.net(scale)
        if value is None:
            return None, ev.why_not or "not enough on the tape to lean on"
        return value, ev.describe()

    def lines(self) -> list[str]:
        return [ev.describe() for ev in self.buckets]


def _tolerance_for(synthesis: Synthesis | None, instrument: str, days: float,
                   mark: float, relative: float) -> tuple[float, str]:
    """How far from the mark a print must be before it counts as a side.

    Half the market's own width where the archive knows one -- a print through
    the mid by less than half a width is a print inside the market, and every
    market has a mid somebody disagrees with -- and a fraction of the mark
    where it does not.
    """
    if synthesis is not None:
        ev = synthesis.width_for(instrument=instrument, days=days)
        if ev is not None and ev.enough and ev.median > 0:
            return 0.5 * ev.median, f"half the archived width of {ev.median:.3f}"
    return abs(relative * mark), f"{relative * 100:.0f}% of the mark"


def read_flow(archive: Archive, pair: str, *, asof: datetime, mark_vol=None,
              hist_pair=None, synthesis: Synthesis | None = None,
              half_life: float = DEFAULT_HALF_LIFE,
              min_effective: float = DEFAULT_MIN_EFFECTIVE,
              lookback_days: float = 30.0, tolerance: float = DEFAULT_TOLERANCE,
              discount_rate: float | None = None, limit: int = 2000) -> FlowRead:
    """The tape for one pair: what printed, which way, and how much vega of it.

    ``mark_vol(days, strike, is_call, forward)`` is our own volatility at that
    strike, in points, or ``None`` where the surface cannot say.  Without it
    nothing is classified and the read is a census of what printed -- which is
    still worth having, and is what a desk gets on a pair whose surface is not
    built.
    """
    out = FlowRead(pair=pair.upper(), asof=asof, half_life=half_life,
                   min_effective=min_effective, lookback_days=lookback_days,
                   tolerance=tolerance)
    tape = tape_forwards(archive, pair, asof=asof, lookback_days=lookback_days)
    vols, notes = invert_trades(archive, pair, asof=asof, hist_pair=hist_pair,
                                lookback_days=lookback_days, discount_rate=discount_rate,
                                tape=tape, limit=limit)
    out.notes.extend(notes)
    if len(tape):
        out.notes.append(f"{len(tape)} forward print(s) on the tape stood in for a forward "
                         f"curve where the historical workbook had none")
    if not vols:
        out.notes.append("no printed trade could be turned into a volatility, so there is "
                         "nothing to take a side on")
        return out

    buckets: dict[str, list] = {}
    for tv in vols:
        _, label = bucket_of(tv.days)
        mark = None if mark_vol is None else mark_vol(tv.days, tv.strike, tv.is_call, tv.forward)
        years = tv.days / 365.2425
        try:
            unit = float(black.vega(tv.forward, tv.strike, tv.vol / 100.0, years))
        except (ValueError, ArithmeticError):
            unit = 0.0
        # Vega in the base currency per volatility *point*: the price is in
        # the quote currency per unit of base, so the forward puts it back on
        # the base leg, and the hundred turns a point into the unit `black`
        # works in.  This is the unit the quote panel's own axe is typed in.
        vega = abs(tv.notional) * unit / max(tv.forward, 1e-12) / 100.0
        if mark is None:
            side, tol = "unmarked", 0.0
            why = "the surface cannot price this strike, so the print takes no side"
        else:
            tol, how = _tolerance_for(synthesis, "outright", tv.days, mark, tolerance)
            if tv.vol > mark + tol:
                side = "paid"
            elif tv.vol < mark - tol:
                side = "given"
            else:
                side = "unclear"
            why = (f"{tv.vol:.3f} against a mark of {mark:.3f}, with a tolerance of "
                   f"{tol:.3f} ({how})")
        out.prints.append(FlowPrint(
            at=tv.at, days=tv.days, bucket=label, strike=tv.strike, is_call=tv.is_call,
            vol=tv.vol, mark=mark, tolerance=tol, notional=tv.notional, vega=vega,
            side=side, forward=tv.source, why=why))
        buckets.setdefault(label, []).append(out.prints[-1])

    for label, prints in sorted(buckets.items(), key=lambda kv: _order(kv[0])):
        weights = {p.at: _age_weight(p.at, asof, half_life) for p in prints}
        paid = [p for p in prints if p.side == "paid"]
        given = [p for p in prints if p.side == "given"]
        effective = sum(weights[p.at] for p in paid + given)
        ages = [_age_days(p.at, asof) for p in prints]
        enough = effective >= min_effective
        out.buckets.append(FlowEvidence(
            bucket=label, prints=len(prints),
            paid=len(paid), given=len(given),
            unclear=sum(1 for p in prints if p.side in ("unclear", "unmarked")),
            calls=sum(1 for p in prints if p.is_call),
            puts=sum(1 for p in prints if not p.is_call),
            capped=0,
            paid_vega=sum(weights[p.at] * p.vega for p in paid),
            given_vega=sum(weights[p.at] * p.vega for p in given),
            effective=effective,
            newest_days=min(ages) if ages else 0.0,
            enough=enough,
            why_not="" if enough else (
                f"the age-weighted count of prints that took a side is {effective:.1f} "
                f"against a floor of {min_effective:g}")))

    out.notes.append("the file publishes no buyer and no seller, so every side above is "
                     "inferred from where the print sat against our own mark")
    out.notes.append("the mark is the surface as it stands now and not as it stood when the "
                     "trade printed, which is why the weighting has a short half-life")
    return out


def _order(label: str) -> float:
    from .synthesis import BUCKETS
    for i, (_, name) in enumerate(BUCKETS):
        if name == label:
            return i
    return len(BUCKETS)


def _age_days(at: str, asof: datetime) -> float:
    from .archive import parse_time
    when = parse_time(at)
    if when is None:
        return float(DEFAULT_HALF_LIFE)
    return max(0.0, (asof - when).total_seconds() / 86400.0)


def _age_weight(at: str, asof: datetime, half_life: float) -> float:
    if not half_life or half_life <= 0:
        return 1.0
    return 0.5 ** (_age_days(at, asof) / float(half_life))
