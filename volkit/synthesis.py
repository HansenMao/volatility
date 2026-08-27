"""What the archive knows, worked out: widths, levels, and whether we were right.

The archive is a pile of observations.  A price needs three numbers out of it,
and each is a different question:

* **How wide is this shown?**  Every two-way in the archive that matches this
  instrument, weighted by how long ago it was seen.
* **Where has it been quoted?**  The market's own recent level, which is not a
  substitute for the model's mid but is the thing to check it against.  A
  model mid outside every market seen this week is either a re-mark nobody
  told the surface about or a mistake, and both are worth a sentence on the
  screen before a price goes out.
* **Were we right?**  The prices we showed, and what became of them.  This is
  the only evidence in the file about *us*, and it is the only thing that can
  say we are systematically a touch tight on the offer side -- which no amount
  of looking at other people's markets will ever reveal.

Four decisions, made once here:

**Age is a weight, not a filter.**  A quote from three weeks ago is worth less
than this morning's, and worth more than nothing.  Everything is weighted by
``0.5 ** (age / half_life)``, and the default half-life is five business-ish
days -- a working week, after which a quote counts half.  A cutoff instead
would make the statistics jump the day a good observation crossed the line;
the desk would see a width move for no reason anybody could point at.

**Thin evidence produces no number at all.**  The weights sum to an *effective*
count, and below the floor the answer is "not enough", named, with what there
is.  This is the same rule the knowledge bank runs on and it exists for the
same reason: a width computed from one quote is a width with a false pedigree,
and a false pedigree is worse than a blank, because a blank gets questioned.

**One observation is one observation, however many times it was seen.**  The
archive's content ids do that job upstream, and nothing here re-counts.  What
*is* counted separately is how many distinct sources a width came from: the
same width from three brokers is stronger than three quotes from one, and the
report says which it had.

**Nothing here changes a mark.**  This module reads and computes.  Turning any
of it into a width the tool will actually quote is a knowledge-bank rule, and
a rule is proposed here and saved by a person -- the two-step ``suggest_rules``
already uses.  A statistic that silently became a price would be a number on a
screen with no author.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .archive import Archive, Observation
from .history import forward_series
from .knowledge import Rule
from .timeutil import TenorError, tenor_to_years

DAYS_IN_YEAR = 365.2425

#: A quote counts half after this many days.  Five days: a working week.
DEFAULT_HALF_LIFE = 5.0

#: Below this effective count, no number is produced.  Two: one observation is
#: an anecdote, and the second is what makes it a width.
DEFAULT_MIN_EFFECTIVE = 2.0

#: The same tenor buckets the knowledge bank writes rules against, so a
#: proposal here lands on a rule the bank can express.  A private bucketing
#: would produce evidence that no rule could be written from.
BUCKETS = ((7.0, "out to a week"), (31.0, "out to a month"),
           (93.0, "out to three months"), (366.0, "out to a year"),
           (float("inf"), "beyond a year"))


class SynthesisError(Exception):
    """Evidence that was asked for in a way that cannot be answered."""


def bucket_of(days: float) -> tuple[float, str]:
    for edge, label in BUCKETS:
        if days <= edge:
            return edge, label
    return BUCKETS[-1]


def days_of(tenor: str, *, asof: datetime | None = None) -> float | None:
    """Days to a tenor, whether it is written ``3M`` or as a date.

    ``None`` when it is neither.  Not an exception: an archive holds whatever
    a broker typed, and one unreadable tenor must not stop a morning's
    statistics from being computed.
    """
    if not tenor:
        return None
    text = str(tenor).strip().upper()
    try:
        return tenor_to_years(text) * DAYS_IN_YEAR
    except (TenorError, ValueError):
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            when = datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        base = asof or datetime.now(timezone.utc)
        return max(0.0, (when - base).total_seconds() / 86400.0)
    return None


def _weight(obs: Observation, asof: datetime, half_life: float) -> float:
    """How much this observation counts, by age.

    An observation with no readable time counts as one half-life old rather
    than as new or as nothing: it is real evidence with an unknown date, and
    both of the tidier answers -- treat it as current, drop it -- are wrong in
    a way that shows up as a width nobody can explain.
    """
    when = obs.when
    if when is None:
        return 0.5
    age = max(0.0, (asof - when).total_seconds() / 86400.0)
    return 0.5 ** (age / max(0.25, half_life))


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    """The ``q`` quantile of (value, weight) pairs, interpolated.

    Written out rather than reached for in numpy because it is six lines and
    because the interpolation convention matters: the desk reads the median
    width off this and a step function would make it jump between two quoted
    widths as a third observation aged.
    """
    if not pairs:
        return float("nan")
    rows = sorted(pairs)
    total = sum(w for _, w in rows)
    if total <= 0:
        return float("nan")
    target, running = q * total, 0.0
    for i, (value, w) in enumerate(rows):
        nxt = running + w
        if nxt >= target:
            if i == 0 or w <= 0:
                return value
            prev_value, prev_w = rows[i - 1]
            span = w
            frac = 0.0 if span <= 0 else min(1.0, max(0.0, (target - running) / span))
            return prev_value + (value - prev_value) * frac
        running = nxt
    return rows[-1][0]


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WidthEvidence:
    """How wide a thing has been shown, and how much that is worth."""

    instrument: str
    bucket: str
    delta: float | None
    observations: int
    effective: float            # the weights summed: an age-discounted count
    sources: int                # how many distinct brokers or files it came from
    median: float               # volatility points
    low: float                  # the 25th percentile
    high: float                 # the 75th
    tightest: float
    widest: float
    newest_days: float
    oldest_days: float
    model_read: int             # how many came through the language model
    enough: bool
    why_not: str = ""

    def describe(self) -> str:
        what = self.instrument.upper()
        if self.delta is not None:
            what = f"{self.delta * 100:g}d {what}"
        if not self.enough:
            return (f"{what} {self.bucket}: not enough to set a width -- {self.why_not} "
                    f"({self.observations} observation(s))")
        age = ("today" if self.newest_days < 1 else f"{self.newest_days:.0f} days ago")
        return (f"{what} {self.bucket}: shown {self.median:.3f} wide "
                f"({self.low:.3f} to {self.high:.3f}), {self.observations} observation(s) from "
                f"{self.sources} source(s), newest {age}")

    def as_rule(self) -> Rule:
        """The bank rule this evidence supports.  Proposed, never saved here."""
        edge = next(e for e, label in BUCKETS if label == self.bucket)
        prev = 0.0
        for e, label in BUCKETS:
            if label == self.bucket:
                break
            prev = e
        return Rule(
            kind="spread", value=round(self.median, 4), instrument=self.instrument,
            min_days=prev or None, max_days=None if edge == float("inf") else edge,
            delta=self.delta,
            text=(f"the median width of {self.observations} observation(s) in the archive "
                  f"from {self.sources} source(s), age-weighted to an effective "
                  f"{self.effective:.1f}; quartiles {self.low:.3f} to {self.high:.3f}, "
                  f"newest {self.newest_days:.1f} days old"))


@dataclass(frozen=True)
class LevelEvidence:
    """Where the market has been, as a check on the mid -- never as the mid."""

    instrument: str
    tenor: str
    delta: float | None
    observations: int
    effective: float
    typical: float              # age-weighted median of the quoted mids
    newest: float
    newest_days: float
    low: float
    high: float
    enough: bool
    why_not: str = ""

    def describe(self) -> str:
        what = f"{self.tenor} {self.instrument.upper()}"
        if not self.enough:
            return f"{what}: no usable level in the archive -- {self.why_not}"
        return (f"{what}: last quoted {self.newest:.3f} "
                f"({'today' if self.newest_days < 1 else f'{self.newest_days:.0f} days ago'}), "
                f"typically {self.typical:.3f} over {self.observations} observation(s), "
                f"range {self.low:.3f} to {self.high:.3f}")

    def gap_to(self, model_mid: float) -> tuple[float, str]:
        """Our mid less the market's recent level, and what to make of it.

        Returned, never applied.  Moving a mid onto the archive's level would
        make the tool quote the market back to itself, which is the one thing
        a market maker's model must not do -- the market it would be quoting
        back is the market it is about to trade against.
        """
        if not self.enough:
            return float("nan"), "there is no archived level to compare against"
        gap = model_mid - self.typical
        if abs(gap) <= max(0.10, 0.02 * max(1.0, abs(self.typical))):
            return gap, "the mark sits where the market has been"
        side = "above" if gap > 0 else "below"
        return gap, (f"the mark is {abs(gap):.3f} {side} where this has been quoted "
                     f"({self.typical:.3f} over {self.observations} observation(s)); worth "
                     f"knowing before the price goes out, and not applied to it")


@dataclass(frozen=True)
class OutcomeEvidence:
    """What became of the prices we made.  The only evidence about us."""

    instrument: str
    bucket: str
    shown: int
    answered: int
    traded_bid: int
    traded_ask: int
    passed: int
    missed: int
    done_away: int
    away_gap: float | None      # their level less our nearer side, average
    enough: bool
    why_not: str = ""

    @property
    def hit_rate(self) -> float | None:
        return None if not self.answered else (self.traded_bid + self.traded_ask) / self.answered

    def describe(self) -> str:
        what = f"{self.instrument.upper()} {self.bucket}"
        if not self.enough:
            return f"{what}: {self.why_not}"
        bits = [f"{self.shown} price(s) shown", f"{self.answered} answered"]
        if self.hit_rate is not None:
            bits.append(f"{self.hit_rate * 100:.0f}% traded")
        if self.traded_bid or self.traded_ask:
            bits.append(f"{self.traded_bid} on the bid, {self.traded_ask} on the offer")
        if self.done_away and self.away_gap is not None:
            side = "through" if self.away_gap < 0 else "outside"
            bits.append(f"{self.done_away} done away, on average {abs(self.away_gap):.3f} "
                        f"{side} our nearer side")
        return f"{what}: " + ", ".join(bits)

    def lean(self) -> tuple[str, str]:
        """What the record suggests, in words, with nothing applied.

        Deliberately prose and not a number.  A hit rate is a statistic about
        the desk's whole behaviour -- what it was axed to do, what it was shown
        by whom -- and turning it into an automatic shift would be reading a
        market-making record as if it were a pricing error.  It is put in
        front of the person who can tell the difference.
        """
        if not self.enough or self.answered < 4:
            return "", ""
        one_sided = abs(self.traded_bid - self.traded_ask)
        if self.hit_rate is not None and self.hit_rate < 0.15:
            return ("wide", f"{self.hit_rate * 100:.0f}% of the prices answered here traded; "
                            f"the width may be doing the work of a pass")
        if one_sided >= 3 and self.traded_ask > self.traded_bid:
            return ("lifted", f"{self.traded_ask} of {self.answered} answered prices were lifted "
                              f"against {self.traded_bid} hit; the offer may be the cheap side")
        if one_sided >= 3 and self.traded_bid > self.traded_ask:
            return ("hit", f"{self.traded_bid} of {self.answered} answered prices were hit "
                           f"against {self.traded_ask} lifted; the bid may be the rich side")
        if self.done_away >= 3 and self.away_gap is not None and self.away_gap < 0:
            return ("through", f"{self.done_away} trade(s) went away inside our price, on "
                               f"average by {abs(self.away_gap):.3f}")
        return "", ""


@dataclass(frozen=True)
class TradeEvidence:
    """What printed, out of the dissemination file."""

    bucket: str
    trades: int
    notional: float
    capped: int
    calls: int
    puts: int
    newest_days: float

    def describe(self) -> str:
        size = (f"{self.notional / 1e6:,.0f}mm" if self.notional else "size not published")
        more = " (some sizes are capped, so this is a lower bound)" if self.capped else ""
        return (f"{self.bucket}: {self.trades} trade(s) printed, {size}{more}, "
                f"{self.calls} call / {self.puts} put, newest {self.newest_days:.0f} days ago")


# --------------------------------------------------------------------------
@dataclass
class Synthesis:
    """Everything the archive says about one pair, at one moment."""

    pair: str
    asof: datetime
    half_life: float = DEFAULT_HALF_LIFE
    min_effective: float = DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 90.0
    widths: list[WidthEvidence] = field(default_factory=list)
    levels: list[LevelEvidence] = field(default_factory=list)
    outcomes: list[OutcomeEvidence] = field(default_factory=list)
    trades: list[TradeEvidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    counted: int = 0

    # ----------------------------------------------------------------------
    def width_for(self, *, instrument: str, days: float,
                  delta: float | None = None) -> WidthEvidence | None:
        """The width evidence that matches, most specific first.

        A delta-specific entry beats the instrument's general one, and neither
        falls back to another instrument: an at-the-money width is not a
        risk-reversal width, and quietly using one for the other is how a wing
        ends up shown as tight as a level.
        """
        _, label = bucket_of(days)
        exact = [w for w in self.widths if w.instrument == instrument and w.bucket == label
                 and w.delta is not None and delta is not None
                 and abs(w.delta - delta) < 1e-9]
        general = [w for w in self.widths if w.instrument == instrument and w.bucket == label
                   and w.delta is None]
        for candidates in (exact, general):
            usable = [w for w in candidates if w.enough]
            if usable:
                return max(usable, key=lambda w: w.effective)
            if candidates:
                return candidates[0]        # returned so the row can say why not
        return None

    def level_for(self, *, instrument: str, tenor: str,
                  delta: float | None = None) -> LevelEvidence | None:
        for lv in self.levels:
            if (lv.instrument == instrument and lv.tenor.upper() == str(tenor).upper()
                    and ((delta is None and lv.delta is None)
                         or (delta is not None and lv.delta is not None
                             and abs(lv.delta - delta) < 1e-9))):
                return lv
        return None

    def outcome_for(self, *, instrument: str, days: float) -> OutcomeEvidence | None:
        _, label = bucket_of(days)
        for oc in self.outcomes:
            if oc.instrument == instrument and oc.bucket == label:
                return oc
        return None

    def proposed_rules(self) -> list[Rule]:
        """Every width with enough behind it, as a bank rule.  Not saved."""
        return [w.as_rule() for w in self.widths if w.enough]

    def lines(self) -> list[str]:
        """The whole synthesis as text, in the order a person reads it."""
        out = [f"{self.pair}: {self.counted} observation(s) in the last "
               f"{self.lookback_days:.0f} days, half-life {self.half_life:g} days"]
        out += ["  width   " + w.describe() for w in self.widths]
        out += ["  level   " + lv.describe() for lv in self.levels]
        out += ["  record  " + oc.describe() for oc in self.outcomes]
        out += ["  printed " + t.describe() for t in self.trades]
        out += ["  note    " + n for n in self.notes]
        return out


def synthesize(archive: Archive, pair: str, *, asof: datetime | None = None,
               half_life: float = DEFAULT_HALF_LIFE,
               min_effective: float = DEFAULT_MIN_EFFECTIVE,
               lookback_days: float = 90.0,
               include_model_read: bool = True) -> Synthesis:
    """Work the archive into evidence for one pair.

    ``include_model_read`` decides whether observations a language model
    transcribed count.  It is switchable and it is *reported* either way,
    because a desk that has not yet convinced itself of the extraction should
    be able to see both numbers and compare them -- which is a better answer
    to "do you trust it" than any assurance in a docstring.
    """
    now = asof or datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    syn = Synthesis(pair=pair.upper(), asof=now, half_life=half_life,
                    min_effective=min_effective, lookback_days=lookback_days)

    # Nothing after the valuation instant.  A run priced as of a past date
    # must not be able to see what the market did afterwards, and an archive
    # that quietly did would make every backward-looking check on this tool
    # flatter than the tool deserves.
    horizon = now + timedelta(days=1)
    rows = archive.query(pair=pair, since=since, until=horizon)
    everything = archive.query(pair=pair)
    ahead = [o for o in everything if o.when is not None and o.when > horizon]
    behind = [o for o in everything
              if o.when is not None and o.when < since]
    undated = [o for o in everything if o.when is None]
    rows += undated
    if ahead:
        syn.notes.append(f"{len(ahead)} observation(s) in the archive are later than the "
                         f"valuation time and were not used")
    if behind:
        syn.notes.append(f"{len(behind)} observation(s) are older than the {lookback_days:.0f} "
                         f"day window and were not used")
    if not include_model_read:
        dropped = [o for o in rows if o.via.startswith("model")]
        rows = [o for o in rows if not o.via.startswith("model")]
        if dropped:
            syn.notes.append(f"{len(dropped)} observation(s) the language model transcribed "
                             f"were left out of these figures")
    syn.counted = len(rows)
    if not rows:
        syn.notes.append(f"the archive holds nothing for {pair.upper()} in this window")
        return syn

    model_read = sum(1 for o in rows if o.via.startswith("model"))
    if model_read and include_model_read:
        syn.notes.append(f"{model_read} of {len(rows)} observation(s) were transcribed by a "
                         f"language model and checked by the quote parser")

    _widths(syn, rows, now)
    _levels(syn, rows, now)
    _outcomes(syn, archive, rows)
    _trades(syn, rows, now)
    return syn


def _widths(syn: Synthesis, rows, now: datetime) -> None:
    buckets: dict[tuple, list] = {}
    unbucketed = 0
    for o in rows:
        if o.kind != "quote" or o.width is None:
            continue
        if o.width <= 0:
            # A choice price is a real thing to be shown and it is not a
            # width.  Averaging a zero in would quietly tighten the ladder --
            # the same reason ``knowledge.suggest_rules`` excludes them.
            continue
        days = days_of(o.tenor, asof=now)
        if days is None:
            unbucketed += 1
            continue
        _, label = bucket_of(days)
        # Every quote lands in its instrument's general bucket, and a quote
        # with a delta *also* lands in that delta's own.  The general one is
        # what a rule with no delta on it matches, so it has to see the wings
        # too; ``width_for`` prefers the specific bucket when there is one.
        buckets.setdefault((o.instrument, label, None), []).append(o)
        if o.delta is not None:
            buckets.setdefault((o.instrument, label, o.delta), []).append(o)
    if unbucketed:
        syn.notes.append(f"{unbucketed} quote(s) carry a tenor this build cannot turn into "
                         f"days and were left out of the widths")

    for (instrument, label, delta), observations in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0)):
        weighted = [(o.width, _weight(o, now, syn.half_life)) for o in observations]
        effective = sum(w for _, w in weighted)
        sources = len({(o.counterparty or o.origin) for o in observations})
        ages = [max(0.0, (now - o.when).total_seconds() / 86400.0)
                for o in observations if o.when]
        enough = effective >= syn.min_effective
        why = "" if enough else (
            f"the age-weighted count is {effective:.1f} against a floor of "
            f"{syn.min_effective:g}")
        syn.widths.append(WidthEvidence(
            instrument=instrument, bucket=label, delta=delta,
            observations=len(observations), effective=effective, sources=sources,
            median=_weighted_quantile(weighted, 0.5),
            low=_weighted_quantile(weighted, 0.25),
            high=_weighted_quantile(weighted, 0.75),
            tightest=min(w for w, _ in weighted), widest=max(w for w, _ in weighted),
            newest_days=min(ages) if ages else float("nan"),
            oldest_days=max(ages) if ages else float("nan"),
            model_read=sum(1 for o in observations if o.via.startswith("model")),
            enough=enough, why_not=why))


def _levels(syn: Synthesis, rows, now: datetime) -> None:
    buckets: dict[tuple, list] = {}
    for o in rows:
        if o.kind != "quote" or o.mid is None:
            continue
        buckets.setdefault((o.instrument, o.tenor.upper(), o.delta), []).append(o)
    for (instrument, tenor, delta), observations in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0)):
        weighted = [(o.mid, _weight(o, now, syn.half_life)) for o in observations]
        effective = sum(w for _, w in weighted)
        dated = [o for o in observations if o.when]
        newest = max(dated, key=lambda o: o.when) if dated else observations[-1]
        age = (0.0 if newest.when is None
               else max(0.0, (now - newest.when).total_seconds() / 86400.0))
        # A level needs less behind it than a width does.  One quote is a
        # perfectly good answer to "where was this last shown"; it is not an
        # answer to "how wide is it usually shown".
        enough = effective >= min(1.0, syn.min_effective)
        syn.levels.append(LevelEvidence(
            instrument=instrument, tenor=tenor, delta=delta,
            observations=len(observations), effective=effective,
            typical=_weighted_quantile(weighted, 0.5), newest=newest.mid or float("nan"),
            newest_days=age,
            low=min(m for m, _ in weighted), high=max(m for m, _ in weighted),
            enough=enough,
            why_not="" if enough else f"the age-weighted count is only {effective:.1f}"))


def _outcomes(syn: Synthesis, archive: Archive, rows) -> None:
    shown = {o.id: o for o in rows if o.kind == "shown"}
    if not shown:
        return
    answers: dict[str, list] = {}
    for o in archive.query(pair=syn.pair, kinds="outcome"):
        if o.ref in shown:
            answers.setdefault(o.ref, []).append(o)
    buckets: dict[tuple, list] = {}
    for ident, price in shown.items():
        days = days_of(price.tenor, asof=syn.asof)
        label = bucket_of(days)[1] if days is not None else "tenor not understood"
        buckets.setdefault((price.instrument, label), []).append((price, answers.get(ident, [])))

    for (instrument, label), pairs in sorted(buckets.items()):
        counts = {r: 0 for r in ("traded_bid", "traded_ask", "passed", "missed",
                                 "pulled", "done_away")}
        gaps: list[float] = []
        answered = 0
        for price, given in pairs:
            for ans in given:
                if ans.result in counts:
                    counts[ans.result] += 1
                if ans.result != "pulled":
                    answered += 1
                if ans.result == "done_away" and ans.away_level is not None:
                    # Signed so the sign carries the meaning: negative means
                    # the trade went *inside* our price, which is the finding
                    # that matters and the one an absolute value would hide.
                    if price.bid is not None and ans.away_level <= price.bid:
                        gaps.append(ans.away_level - price.bid)
                    elif price.ask is not None and ans.away_level >= price.ask:
                        gaps.append(ans.away_level - price.ask)
                    else:
                        gaps.append(-min(abs(ans.away_level - (price.bid or ans.away_level)),
                                         abs(ans.away_level - (price.ask or ans.away_level))))
        enough = bool(pairs)
        syn.outcomes.append(OutcomeEvidence(
            instrument=instrument, bucket=label, shown=len(pairs), answered=answered,
            traded_bid=counts["traded_bid"], traded_ask=counts["traded_ask"],
            passed=counts["passed"], missed=counts["missed"], done_away=counts["done_away"],
            away_gap=(sum(gaps) / len(gaps)) if gaps else None,
            enough=enough,
            why_not="" if enough else "no prices shown here yet"))


def _trades(syn: Synthesis, rows, now: datetime) -> None:
    buckets: dict[str, list] = {}
    for o in rows:
        if o.kind != "trade":
            continue
        days = days_of(o.expiry_date or o.tenor, asof=now)
        label = bucket_of(days)[1] if days is not None else "expiry not understood"
        buckets.setdefault(label, []).append(o)
    for label, observations in buckets.items():
        ages = [max(0.0, (now - o.when).total_seconds() / 86400.0)
                for o in observations if o.when]
        syn.trades.append(TradeEvidence(
            bucket=label, trades=len(observations),
            notional=sum(o.notional or 0.0 for o in observations),
            capped=sum(1 for o in observations if o.notional_capped),
            calls=sum(1 for o in observations if o.is_call is True),
            puts=sum(1 for o in observations if o.is_call is False),
            newest_days=min(ages) if ages else float("nan")))


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeVol:
    """A volatility a printed trade implies. Derived, with its inputs named."""

    at: str
    external_id: str
    days: float
    tenor: str
    strike: float
    is_call: bool
    vol: float                  # volatility points
    forward: float
    discount: float
    moneyness: float            # K / F
    notional: float
    premium: float
    premium_ccy: str
    source: str                 # where the forward came from
    why: str                    # the whole provenance, in one sentence

    def describe(self) -> str:
        side = "call" if self.is_call else "put"
        return (f"{self.tenor} {self.strike:g} {side} traded at {self.vol:.3f} "
                f"(K/F {self.moneyness:.4f}, {self.notional / 1e6:,.0f}mm)")


#: How far before a trade the historical sheet's nearest row may be.  A row
#: from the Friday is a fine forward for a Monday trade; a row from two years
#: ago is not, and the "last row on or before" rule would take it without a
#: word.  That is the same silent substitution this module refuses to make
#: with the live feed, so it is refused here too.
MAX_STALE_DAYS = 7.0


def _forward_for_trade(hist_pair, when: datetime, days: float,
                       max_stale_days: float = MAX_STALE_DAYS) -> tuple[float | None, str]:
    """The forward at the trade's own date, out of the historical workbook.

    The live feed is deliberately not a fallback for a trade that printed
    three weeks ago.  Inverting last month's premium against this morning's
    forward produces a volatility that is wrong by the whole of the carry
    since, and it produces it silently -- which is the one failure mode this
    module exists to avoid.  A trade whose forward cannot be found is refused
    and says so.
    """
    if hist_pair is None:
        return None, "no historical workbook is loaded, so there is no forward for that date"
    dates = getattr(hist_pair, "dates", None)
    if not dates:
        return None, f"the historical sheet holds no dates for {getattr(hist_pair, 'pair', '')}"
    want = when.date()
    idx = None
    for i, d in enumerate(dates):
        if d <= want:
            idx = i
        else:
            break
    if idx is None:
        return None, (f"the historical sheet starts at {dates[0].isoformat()}, after the trade "
                      f"on {want.isoformat()}")
    used = dates[idx]
    stale = (want - used).days
    if stale > max_stale_days:
        return None, (f"the nearest row in the historical sheet is {used.isoformat()}, "
                      f"{stale} days before the trade on {want.isoformat()}; that forward is "
                      f"too old to invert against")
    tenor = f"{max(1, int(round(days)))}D"
    series, note = forward_series(hist_pair, tenor)
    if series is None or idx >= len(series):
        return None, "the historical sheet holds no forwards for this pair"
    value = float(series[idx])
    if not (value > 0) or value != value:
        return None, f"the forward on {used.isoformat()} is not a number"
    where = f"the {used.isoformat()} row of the historical sheet"
    if used != want:
        where += f" (the trade is dated {want.isoformat()}; that row is the last one before it)"
    if note:
        where += f", {note}"
    return value, where


def implied_from_trade(obs: Observation, *, pair: str, forward: float,
                       discount: float = 1.0, years: float,
                       forward_source: str = "") -> tuple[float | None, str]:
    """The volatility a printed trade implies, and what it took to get there.

    Kept apart from the reader and apart from the statistics because it is the
    one number in this package **derived from marks that can change**.  A
    trade's premium is a fact; the volatility it implies is a fact about the
    premium *and* the forward used to invert it, so it arrives with those
    inputs attached or nobody can reconcile it later.

    Two things decide whether the arithmetic is even well posed, and both are
    checked rather than assumed:

    *Which currency the size is in.*  ``premium / notional`` is a price per
    unit of the notional currency.  Black-76 here wants domestic per unit of
    base, so a notional in the base currency with the premium in the quote
    currency is the straightforward case; a premium in the *base* currency is
    a foreign-currency premium and is multiplied by the forward first.  A
    notional on the quote-currency side is refused: recovering the base amount
    needs the strike and the convention the trade was struck under, and
    guessing which is how a whole tenor's history acquires a bias.

    *Whether there is a discount curve.*  There is not -- ``pricing.py`` says
    so, and this package has never carried one.  With no ``discount`` given
    the premium is inverted as an **undiscounted forward value**, which makes
    the volatility come out *too low* by roughly the discount over the life of
    the option: about 4% of the volatility on a one-year option at 4% rates,
    and negligible inside a month.  It is said on every row rather than
    quietly absorbed, and a rate can be supplied to remove it.

    Returns ``(None, why)`` rather than a number whenever any of that fails.
    """
    from . import black
    if obs.premium is None:
        return None, "no premium was published for this trade"
    if obs.strike is None:
        return None, "no strike was published for this trade"
    if not obs.notional:
        return None, "no notional was published, so the premium cannot be put per unit"
    if obs.notional_capped:
        return None, ("the notional is the dissemination cap and not the trade's size, so a "
                      "premium per unit would be wrong by however much the cap hid")
    if obs.is_call is None:
        return None, "the trade does not say whether it is a call or a put"
    if years <= 0:
        return None, "the trade had already expired at the valuation time"
    if not (forward > 0):
        return None, "there is no usable forward for that date"

    base, quote = pair[:3].upper(), pair[3:6].upper()
    notional_ccy = (obs.notional_ccy or base).upper()
    premium_ccy = (obs.premium_ccy or quote).upper()
    per_unit = obs.premium / obs.notional
    convention = ""
    if notional_ccy != base:
        return None, (f"the notional is in {notional_ccy} and this pair's base is {base}; "
                      f"recovering the base amount needs the convention the trade was struck "
                      f"under, and it is not published")
    if premium_ccy == quote:
        convention = f"premium in {premium_ccy} per unit of {base}"
    elif premium_ccy == base:
        # A premium paid in the base currency is a fraction of the notional;
        # multiplying by the forward puts it back in domestic terms, which is
        # what Black-76 is written in here.
        per_unit = per_unit * forward
        convention = (f"premium in {premium_ccy}, the base currency, so it was multiplied by "
                      f"the forward to put it in {quote} per unit of {base}")
    else:
        return None, (f"the premium is in {premium_ccy}, which is neither leg of {pair}; "
                      f"converting it needs a rate this tool does not hold")

    target = per_unit / discount if discount else per_unit
    try:
        vol = black.implied_vol(target, forward, obs.strike, years, obs.is_call,
                                lo=1e-4, hi=5.0)
    except Exception as exc:                    # numerics raises its own type
        return None, (f"the premium could not be inverted to a volatility: {exc}. "
                      f"A premium that is below intrinsic or above the forward cannot come "
                      f"from any volatility, and usually means the size or the strike was "
                      f"published on a different basis")
    said = (f"inverted from a premium of {obs.premium:,.0f} {premium_ccy} on a notional of "
            f"{obs.notional:,.0f} {notional_ccy} ({convention}), against a forward of "
            f"{forward:.6g}"
            + (f" from {forward_source}" if forward_source else ""))
    said += (", undiscounted -- this package carries no rate curve, so the volatility is a "
             "touch low" if discount == 1.0
             else f", discounted at {discount:.6g}")
    return vol * 100.0, said


def invert_trades(archive: Archive, pair: str, *, asof: datetime, hist_pair=None,
                  lookback_days: float = 90.0, discount_rate: float | None = None,
                  max_stale_days: float = MAX_STALE_DAYS,
                  limit: int = 500) -> tuple[list[TradeVol], list[str]]:
    """Every printed trade this build can turn into a volatility, and why the rest could not.

    The refusals are counted by reason rather than listed one by one: a day of
    dissemination holds thousands of rows and "1,180 had a capped notional" is
    the useful shape of that, not 1,180 lines saying so.
    """
    since = asof - timedelta(days=lookback_days)
    rows = [o for o in archive.query(pair=pair, kinds="trade", since=since, until=asof)
            if o.action.upper() not in ("CANC", "EROR", "TERM")]
    out: list[TradeVol] = []
    refused: dict[str, int] = {}
    for obs in rows[-int(limit):] if limit else rows:
        when = obs.when
        if when is None:
            refused["the execution time could not be read"] = \
                refused.get("the execution time could not be read", 0) + 1
            continue
        days = days_of(obs.expiry_date or obs.tenor, asof=when)
        if days is None or days <= 0:
            refused["the expiry could not be read"] = refused.get("the expiry could not be read", 0) + 1
            continue
        years = days / DAYS_IN_YEAR
        forward, source = _forward_for_trade(hist_pair, when, days, max_stale_days)
        if forward is None:
            refused[source] = refused.get(source, 0) + 1
            continue
        discount = 1.0 if discount_rate in (None, "") else math.exp(-float(discount_rate) * years)
        vol, why = implied_from_trade(obs, pair=pair, forward=forward, discount=discount,
                                      years=years, forward_source=source)
        if vol is None:
            refused[why] = refused.get(why, 0) + 1
            continue
        out.append(TradeVol(
            at=obs.at, external_id=obs.external_id, days=days,
            tenor=f"{int(round(days))}D", strike=obs.strike, is_call=bool(obs.is_call),
            vol=vol, forward=forward, discount=discount,
            moneyness=obs.strike / forward, notional=obs.notional or 0.0,
            premium=obs.premium or 0.0, premium_ccy=obs.premium_ccy,
            source=source, why=why))
    notes = [f"{n} trade(s) could not be inverted: {why}"
             for why, n in sorted(refused.items(), key=lambda kv: -kv[1])]
    if out:
        notes.insert(0, f"{len(out)} of {len(rows)} printed trade(s) were turned into a "
                        f"volatility; every one carries the forward it used")
        # Said once rather than on every row.  The dissemination file publishes
        # an expiry *date* and no cut, so the expiry is taken at midnight UTC
        # -- earlier than a real 10am New York cut by up to fifteen hours,
        # which shortens the life and lifts the volatility.  Negligible at
        # three months, worth knowing on a one-week trade.
        notes.insert(1, "expiries are taken at midnight UTC because the file publishes a date "
                        "and not a cut, so a short-dated volatility here reads a touch high")
    return out, notes
