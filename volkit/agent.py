"""The quoting agent: read everything, work it out, make a price, show your working.

The market-maker screen answers "what do I show against *this* market" -- it
needs a paste to fit to.  A request does not come with a market attached.  It
comes as "where are you on the 1 month at-the-money in a hundred million", and
answering it means putting together four things that live in four places:

1. **the marked surface** -- the desk's own mid, out of the workbook and
   whatever this session has re-marked on it;
2. **the knowledge bank** -- widths, floors and shifts a person wrote down;
3. **the archive** -- what the market has actually been shown at, what has
   printed, and what became of the prices we made, all of it age-weighted by
   ``synthesis.py``;
4. **the leans** -- the fair-value richness against realized volatility and
   the position on the book, the same two the market-maker screen uses and
   with the same caps on them.

The output is a **decision**, and a decision here is not a number with a
sentence attached.  It is an ordered list of ingredients, each with its value,
its unit and where it came from, which *sum to* the bid and the offer.  The
English explanation is generated from that list, and so is the local model's
paragraph when there is one.  That ordering is the whole design: a story
written first and reconciled to the numbers afterwards is a story that stays
plausible when the numbers are wrong.

Four things this refuses to do, each of them the tempting version:

**It does not quote the archive back at the market.**  The recent market level
is computed, shown, and compared to the mark -- and never applied to it.  A
market maker whose mid follows the last thing it was shown is a market maker
being led, and the market leading it is the one it is about to trade with.
A gap between the mark and the archive is a *flag*, and the answer to a flag
is to re-mark the surface deliberately, which is what the marking screen is
for.

**It does not invent a width.**  The ladder is: a bank rule, then archive
evidence with enough behind it, then a fallback the caller typed, then no
price at all.  Every row says which rung it stood on, and a row that reaches
the bottom shows no bid and no offer -- exactly as ``knowledge.py`` does,
because a width nobody can trace back to a source is a width nobody can argue
with.

**It does not turn the hit rate into a shift.**  What became of our prices is
the most interesting thing in the archive and the easiest to over-read: a run
of lifted offers is sometimes a mid that is too low and sometimes a week of
being the only one showing.  The record is put in front of the person in
words, with the counts, and it moves no number by itself.

**It does not let the model near the arithmetic.**  Everything above is
computed before anything is asked to describe it, and the description is
refused whole if it contains a number the decision does not (``llm.py``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import archive as arch
from . import synthesis as syn
from .knowledge import KnowledgeBank, PairKnowledge, Rule
from .marketmaker import Evaluator, resolve_expiries, skew_for
from .quotes import (MarketQuote, QuoteError, _ATM, _CALL, _DELTA, _DROP, _FLY,
                     _MID, _PUT, _RR, _SIZE, _SMILE_FLY, _SPREAD, _STRANGLE,
                     _norm, _squash)
from .timeutil import TenorError, tenor_to_years

DAYS_IN_YEAR = 365.2425

#: How the width was arrived at, in the order it is tried.  The order is the
#: policy: a person's rule beats a statistic, a statistic beats a number typed
#: into a box, and nothing beats saying so.
WIDTH_SOURCES = ("bank", "archive", "fallback", "none")


class AgentError(Exception):
    """A request the agent cannot answer at all."""


# --------------------------------------------------------------------------
# What was asked for
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ask:
    """One instrument to make a price on."""

    instrument: str
    tenor: str
    tenor_far: str | None = None
    delta: float | None = None
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None
    leg: str | None = None
    size: float | None = None
    size_basis: str = "unspecified"
    raw: str = ""
    line: int = 0
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        what = self.instrument.upper()
        if self.instrument in ("rr", "fly") and self.delta is not None:
            what = f"{self.delta * 100:g}d {self.instrument.upper()}"
        elif self.instrument == "outright" and self.strike is not None:
            side = "call" if self.is_call else "put" if self.is_call is False else ""
            what = f"{self.strike:g} {side}".strip()
        elif self.instrument == "spread":
            what = f"{self.tenor}/{self.tenor_far} spread"
            return (what + (f" in {self.size:g}mm {self.size_basis}"
                            if self.size else "")).strip()
        size = f" in {self.size:g}mm {self.size_basis}" if self.size else ""
        return f"{self.tenor} {what}{size}"

    def as_quote(self) -> MarketQuote:
        """The request as the thing the pricing evaluator already understands.

        The bid and the offer are ``nan`` on purpose and are never read: this
        is a request, there is no market on it, and a zero in those fields
        would be a market of zero -- which every hinge, width and richness
        check downstream would happily take at face value.
        """
        nan = float("nan")
        return MarketQuote(
            instrument=self.instrument, expiry=self.tenor, bid=nan, ask=nan,
            expiry_far=self.tenor_far, leg=self.leg, delta=self.delta,
            strike=self.strike, is_call=self.is_call, fly_kind=self.fly_kind,
            size=self.size, size_basis=self.size_basis, label=self.describe(),
            line=self.line, raw=self.raw)


#: A tenor with a unit that cannot be anything else.
_TENOR_RE = re.compile(r"^\d+(?:\.\d+)?[wmy]$", re.I)
#: ``25d``.  A 25-day expiry or a 25 delta, and the line has to say which --
#: see :func:`parse_asks`.  Reading it as a tenor unconditionally turned
#: "3M 25d RR" into a risk reversal with no delta on it and no expiry the
#: caller asked for.
_AMBIGUOUS_D_RE = re.compile(r"^\d+(?:\.\d+)?d$", re.I)
#: ``25 delta`` written with the space in it.
_SPACED_DELTA = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s+(delta|dl)\b", re.I)
#: ``25 delta``, ``25dl`` -- a delta and nothing else.
_PLAIN_DELTA_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:delta|dl)$", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)$")


def parse_asks(text: str, *, fly_convention: str = "market") -> tuple[list[Ask], list[str],
                                                                     list[tuple[int, str, str]]]:
    """Read a list of things to price.  Returns (asks, notes, skipped).

    The vocabulary is imported from ``quotes.py`` rather than restated, so a
    synonym a broker uses that was taught to the paste parser is understood
    here too.  Two parsers with two private word lists is two places for
    ``riskie`` to mean something, and one of them to be missing it.

    ``25d`` is the one genuinely ambiguous token: a 25-day expiry and a 25
    delta are written the same way.  It is resolved the way a person resolves
    it -- by what else is on the line.  A line naming a risk reversal or a
    butterfly needs a delta and takes the last such token as one; every other
    line reads them all as expiries.  Whichever way it goes is reported, and
    ``25 delta`` is never ambiguous, so a line that matters can be written
    unambiguously.

    What this does *not* accept is a price.  A line with a two-way on it is a
    market somebody showed, and that belongs on the market-maker screen where
    it can be fitted to; taking it here would silently ignore the market and
    quote over the top of it.
    """
    asks: list[Ask] = []
    notes: list[str] = []
    skipped: list[tuple[int, str, str]] = []
    for n, original in enumerate(str(text or "").splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        # "25 delta" is one token written as two.  Joined before splitting so
        # the word list holds what a person meant rather than a bare number
        # followed by a word this parser would have to call an error.
        words = _SPACED_DELTA.sub(r"\1\2", _norm(line)).split()
        if not words:
            continue
        instrument = None
        tenors: list[str] = []
        ambiguous: list[str] = []
        delta = strike = size = None
        is_call = fly_kind = None
        size_basis = "unspecified"
        leg = None
        prices = 0
        bad = ""

        for word in words:
            squashed = _squash(word)
            if squashed in _DROP or squashed in _MID:
                continue
            if _TENOR_RE.match(word) or _DATE_RE.match(word):
                tenors.append(word.upper())
                continue
            if _AMBIGUOUS_D_RE.match(squashed):
                ambiguous.append(word.upper())
                continue
            m = _PLAIN_DELTA_RE.match(squashed)
            if m:
                delta = float(m.group(1)) / 100.0
                continue
            m = _SIZE.match(squashed)
            if m:
                size = float(m.group(1))
                unit = m.group(2)
                size = size * (1000.0 if unit in ("bn", "b") else
                               0.001 if unit == "k" else 1.0)
                continue
            if squashed in _ATM:
                instrument = "atm"
                continue
            if squashed in _RR:
                instrument = "rr"
                continue
            if squashed in _SMILE_FLY:
                instrument, fly_kind = "fly", "smile"
                continue
            if squashed in _FLY or squashed in _STRANGLE:
                instrument = "fly"
                fly_kind = fly_kind or ("market" if squashed in _STRANGLE else None)
                continue
            if squashed in _SPREAD:
                instrument = "spread"
                continue
            if squashed in _CALL:
                is_call = True
                continue
            if squashed in _PUT:
                is_call = False
                continue
            if squashed in ("vega", "notional"):
                size_basis = squashed
                continue
            if "/" in word and all(_TENOR_RE.match(p) or _AMBIGUOUS_D_RE.match(p)
                                   or _DATE_RE.match(p)
                                   for p in word.split("/") if p):
                tenors.extend(p.upper() for p in word.split("/") if p)
                instrument = instrument or "spread"
                continue
            if "/" in word:
                prices += 1
                continue
            if _NUMBER_RE.match(word):
                # A bare number after everything else is an absolute strike.
                # It cannot be a level: a request has no level on it, which is
                # the whole reason it is a request.
                strike = float(word)
                continue
            bad = f"{word!r} is not a tenor, an instrument, a delta, a strike or a size"

        # The ambiguous tokens, resolved now that the whole line has been seen.
        if ambiguous:
            if instrument in ("rr", "fly") and delta is None:
                delta = float(ambiguous[-1][:-1]) / 100.0
                tenors.extend(ambiguous[:-1])
                if len(ambiguous) > 1:
                    notes.append(f"line {n}: {ambiguous[-1]} was read as a delta and the other "
                                 f"'d' token(s) as expiries, because the line names a "
                                 f"{instrument}")
            else:
                tenors.extend(ambiguous)

        if prices:
            skipped.append((n, "a price was written on the line; a request has no price on it "
                               "-- a market somebody showed belongs on the market-maker screen",
                            original))
            continue
        if bad:
            skipped.append((n, bad, original))
            continue
        if not tenors:
            skipped.append((n, "no expiry on the line", original))
            continue
        if instrument is None:
            instrument = "outright" if strike is not None else "atm"
        if instrument == "spread" and len(tenors) < 2:
            skipped.append((n, "a calendar spread needs two expiries", original))
            continue
        if instrument in ("rr", "fly") and delta is None:
            skipped.append((n, f"a {instrument} needs a delta on it, e.g. 25d", original))
            continue
        if instrument == "fly" and fly_kind is None:
            fly_kind = fly_convention
            notes.append(f"line {n} did not say which butterfly it is and was read as the "
                         f"{fly_convention} convention")
        if instrument == "outright" and strike is None:
            skipped.append((n, "an outright needs a strike", original))
            continue
        if instrument == "spread":
            leg = "atm"
        for extra in tenors[2:]:
            notes.append(f"line {n}: the extra expiry {extra} was not used")
        asks.append(Ask(instrument=instrument, tenor=tenors[0],
                        tenor_far=tenors[1] if instrument == "spread" else None,
                        delta=delta, strike=strike, is_call=is_call, fly_kind=fly_kind,
                        leg=leg, size=size, size_basis=size_basis, raw=original, line=n))
    return asks, notes, skipped


# --------------------------------------------------------------------------
# What was decided
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ingredient:
    """One input to a price, with where it came from."""

    name: str
    value: float | None
    unit: str = "vol points"
    source: str = ""
    detail: str = ""
    applied: bool = True

    def line(self) -> str:
        if self.value is None:
            body = self.detail or "not available"
            return f"{self.name}: {body}" + ("" if self.applied else " (not applied)")
        shown = f"{self.value:+.3f}" if self.name.startswith(("shading", "lean", "shift")) \
            else f"{self.value:.3f}"
        tail = f" -- {self.detail}" if self.detail else ""
        mark = "" if self.applied else "  (not applied)"
        return f"{self.name}: {shown} {self.unit}, {self.source}{tail}{mark}"


@dataclass
class Decision:
    """One price, and everything that set it."""

    ask: Ask
    pair: str
    model_mid: float | None = None
    mid: float | None = None
    bid: float | None = None
    offer: float | None = None
    width: float | None = None
    width_source: str = "none"
    floor: float | None = None
    shading: float = 0.0
    trace: list[Ingredient] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    narration: str = ""
    narration_why_not: str = ""

    @property
    def priced(self) -> bool:
        return self.bid is not None and self.offer is not None

    def quote_text(self) -> str:
        if not self.priced:
            return "no price"
        return f"{self.bid:.3f}/{self.offer:.3f}"

    def facts(self) -> list[str]:
        """The decision as lines.  This is the explanation, and the model's source.

        Everything the narration is allowed to say is here, which is what
        makes the numeric guard in ``llm.narrate`` meaningful: the set of
        permitted numbers is exactly the set of numbers a person reading this
        list would see.
        """
        out = [f"{self.pair} {self.ask.describe()}: showing {self.quote_text()}"]
        out += [f"  {ing.line()}" for ing in self.trace]
        out += [f"  flag: {f}" for f in self.flags]
        out += [f"  advice: {a}" for a in self.advice]
        out += [f"  warning: {w}" for w in self.warnings]
        return out

    def explain(self) -> str:
        return "\n".join(self.facts())

    def to_json(self) -> dict:
        return {
            "pair": self.pair, "ask": self.ask.describe(), "raw": self.ask.raw,
            "instrument": self.ask.instrument, "tenor": self.ask.tenor,
            "tenor_far": self.ask.tenor_far, "delta": self.ask.delta,
            "strike": self.ask.strike, "is_call": self.ask.is_call,
            "size": self.ask.size, "size_basis": self.ask.size_basis,
            "model_mid": self.model_mid, "mid": self.mid, "bid": self.bid,
            "offer": self.offer, "width": self.width, "width_source": self.width_source,
            "floor": self.floor, "shading": self.shading,
            "quote": self.quote_text(), "priced": self.priced,
            "trace": [{"name": i.name, "value": i.value, "unit": i.unit,
                       "source": i.source, "detail": i.detail, "applied": i.applied}
                      for i in self.trace],
            "advice": list(self.advice), "flags": list(self.flags),
            "warnings": list(self.warnings),
            "narration": self.narration, "narration_why_not": self.narration_why_not,
        }


# --------------------------------------------------------------------------
@dataclass
class Request:
    """What to price, and with how much of the machinery turned on."""

    pair: str
    text: str = ""                      # the things to price, one per line
    cut: str = "NY"
    method: str | None = None
    fly_convention: str = "market"

    # the archive
    use_archive_width: bool = True
    half_life: float = syn.DEFAULT_HALF_LIFE
    min_effective: float = syn.DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 90.0
    include_model_read: bool = True

    # the leans, exactly as the market-maker screen names them
    fair_weight: float = 0.25
    axe_weight: float = 0.5
    skew_cap: float = 1.0
    horizon_days: float = 30.0
    hist_lookback_days: float | None = None
    vega_text: str = ""
    vega_scale: float = 0.0

    fallback_spread: float | None = None
    stale_days: float = 5.0             # older than this and the archive says so
    narrate: bool = True


@dataclass
class AgentRun:
    """A whole answer: the decisions, the evidence behind them, and the notes."""

    pair: str
    decisions: list[Decision] = field(default_factory=list)
    synthesis: syn.Synthesis | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""
    valuation: str = ""

    def to_json(self) -> dict:
        return {
            "pair": self.pair, "valuation": self.valuation, "model": self.model,
            "rows": [d.to_json() for d in self.decisions],
            "evidence": (self.synthesis.lines() if self.synthesis else []),
            "proposed_rules": ([{"describe": r.describe(), "text": r.text}
                                for r in self.synthesis.proposed_rules()]
                               if self.synthesis else []),
            "notes": list(self.notes),
            "skipped": [{"line": n, "why": w, "text": t} for n, w, t in self.skipped],
            "warnings": list(self.warnings),
        }

    def text(self) -> str:
        out = [f"{self.pair}  valued {self.valuation}"]
        for d in self.decisions:
            out.append("")
            out.append(d.explain())
            if d.narration:
                out.append(f"  -- {d.narration}")
            elif d.narration_why_not:
                out.append(f"  -- no written explanation: {d.narration_why_not}")
        if self.synthesis:
            out += ["", "evidence"] + ["  " + x for x in self.synthesis.lines()]
        if self.skipped:
            out += ["", "not read"] + [f"  line {n} ({w}): {t}" for n, w, t in self.skipped]
        if self.notes:
            out += ["", "notes"] + [f"  {x}" for x in self.notes]
        return "\n".join(out)


# --------------------------------------------------------------------------
def run(request: Request, *, book, archive: arch.Archive,
        bank: KnowledgeBank | None = None, hist=None, model=None) -> AgentRun:
    """Make a price on everything asked for, and say how each one was made."""
    pair = request.pair.upper()
    if book is None:
        raise AgentError("the agent needs a loaded book")
    if pair not in book:
        raise AgentError(f"{pair} is not built in this book; it holds {', '.join(book.pairs)}")

    clock = book.clock
    out = AgentRun(pair=pair, valuation=clock.now.isoformat(timespec="seconds"))
    surface = book[pair]
    method = request.method or surface.method

    asks, notes, skipped = parse_asks(request.text, fly_convention=request.fly_convention)
    out.notes.extend(notes)
    out.skipped.extend(skipped)
    if not asks:
        out.warnings.append("nothing was asked for that this build could read")
        return out

    # -- the archive, worked out once for the whole request -----------------
    out.synthesis = syn.synthesize(
        archive, pair, asof=clock.now, half_life=request.half_life,
        min_effective=request.min_effective, lookback_days=request.lookback_days,
        include_model_read=request.include_model_read)
    out.notes.extend(out.synthesis.notes)

    pk: PairKnowledge = (bank or KnowledgeBank()).for_pair(pair)

    quotes = [a.as_quote() for a in asks]
    try:
        expiries = resolve_expiries(clock, quotes)
    except (ValueError, TenorError, QuoteError) as exc:
        raise AgentError(f"the expiries could not be resolved: {exc}") from None
    stale = [k for k, (_, t) in expiries.items() if t <= 0]
    if stale:
        raise AgentError(f"{', '.join(stale)} is not in the future at the valuation time "
                         f"{clock.now:%Y-%m-%d %H:%M}Z")

    forwards, forward_notes = _forwards(book, pair, expiries)
    out.notes.extend(forward_notes)
    rich_at, fair_note = _fair(book, pair, hist, method, request)
    if fair_note:
        out.notes.append(fair_note)
    axe_at, axe_note = _axe(request)
    if axe_note:
        out.notes.append(axe_note)

    evaluator = Evaluator(surface, method, request.cut)
    for a, q in zip(asks, quotes):
        out.decisions.append(_decide(
            a, q, pair=pair, evaluator=evaluator, expiries=expiries, forwards=forwards,
            pk=pk, synthesis=out.synthesis, request=request, rich_at=rich_at,
            axe_at=axe_at, method=method))

    if request.narrate and model is not None:
        from . import llm
        out.model = getattr(getattr(model, "config", None), "model", "")
        for d in out.decisions:
            text, why = llm.narrate(model, d.facts())
            d.narration, d.narration_why_not = text, why
    elif request.narrate:
        for d in out.decisions:
            d.narration_why_not = "no local model was configured for this run"
    return out


def _forwards(book, pair: str, expiries) -> tuple[dict, list[str]]:
    """The same forward the market-maker screen uses, through the same function."""
    from .analytics import _forward_at
    forwards, notes, said = {}, [], set()
    for key, (_, t) in expiries.items():
        fwd, real, note = _forward_at(book, pair, t)
        forwards[key] = fwd if real else None
        # Said once -- see the market-maker screen's own ``_forwards``.
        for part in (x.strip() for x in note.split(";")) if real and note else ():
            if part and part not in said:
                said.add(part)
                notes.append(f"{key}: {part}")
    if not any(v is not None for v in forwards.values()):
        notes.append(f"there is no forward feed for {pair}, so anything written against an "
                     f"absolute strike cannot be turned into a moneyness and is not priced")
    return forwards, notes


def _fair(book, pair: str, hist, method: str, request: Request):
    """Richness against realized volatility, or nothing and the reason."""
    if hist is None:
        return None, ("no historical workbook is loaded, so nothing shades the mid toward or "
                      "away from fair value")
    from .analytics import fair_value_table
    try:
        rows = fair_value_table(book, pair, hist, horizon_days=request.horizon_days,
                                lookback_days=request.hist_lookback_days, method=method,
                                cut=request.cut)
    except Exception as exc:                      # noqa: BLE001 - one section, one failure
        return None, f"the fair value table could not be built, so nothing shades the mid: {exc}"
    live = [r for r in rows if r.richness is not None]
    if not live:
        return None, (f"the historical workbook has no realized volatility for {pair}, so "
                      f"nothing shades the mid toward fair value")
    ts = [r.t for r in live]
    vals = [r.richness for r in live]
    return (lambda t: _interp(ts, vals, t)), ""


def _axe(request: Request):
    """The position leaning the mid, read exactly as the market-maker screen reads it."""
    if not request.vega_text.strip():
        return None, ""
    from .quotes import parse_vega_profile
    profile, notes, skipped = parse_vega_profile(request.vega_text)
    if not profile:
        return None, "every line of the vega profile was rejected, so no position leans the mid"
    if not request.vega_scale or request.vega_scale <= 0:
        return None, ("a vega profile was given but the axe scale is not set, so there is "
                      "nothing to measure the position against")
    ts = sorted(tenor_to_years(k) for k in profile)
    vals = [profile[k] / request.vega_scale for k in sorted(profile, key=tenor_to_years)]
    return (lambda t: _interp(ts, vals, t)), (
        f"the position leaning the mid covers {len(profile)} tenor(s) and is held flat outside "
        f"them" + (f"; {len(skipped)} line(s) of it were not read" if skipped else ""))


def _interp(xs, ys, x: float) -> float:
    """Flat outside, linear inside.  The same rule ``marketmaker._interp`` uses."""
    if not xs:
        return 0.0
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            span = xs[i] - xs[i - 1]
            if span <= 0:
                return ys[i]
            w = (x - xs[i - 1]) / span
            return ys[i - 1] * (1 - w) + ys[i] * w
    return ys[-1]


def _key(expiry) -> str:
    return expiry if isinstance(expiry, str) else str(expiry)


def _decide(a: Ask, q: MarketQuote, *, pair: str, evaluator: Evaluator, expiries: dict,
            forwards: dict, pk: PairKnowledge, synthesis: syn.Synthesis, request: Request,
            rich_at, axe_at, method: str) -> Decision:
    """One price.  Every branch here appends to the trace, including the dead ends."""
    d = Decision(ask=a, pair=pair)
    key = _key(a.tenor_far if a.instrument == "spread" else a.tenor)
    _, t = expiries[key]
    days = t * DAYS_IN_YEAR

    # -- 1. the mark ----------------------------------------------------------
    try:
        model_vol = evaluator.value(q, expiries, forwards)
    except Exception as exc:                      # noqa: BLE001 - one row, one failure
        d.warnings.append(f"the marked surface could not price this: "
                          f"{type(exc).__name__}: {exc}")
        d.trace.append(Ingredient("model mid", None, source="the marked surface",
                                  detail=str(exc)))
        return d
    d.model_mid = model_vol * 100.0
    d.trace.append(Ingredient(
        "model mid", d.model_mid, source=f"the marked surface ({method}, {request.cut} cut)",
        detail=f"{a.tenor} is {days:.1f} days"))

    # -- 2. what the market has been, as a check and not as an input ----------
    level = synthesis.level_for(instrument=a.instrument, tenor=a.tenor, delta=a.delta)
    if level is None:
        d.trace.append(Ingredient(
            "market level", None, source="the archive", applied=False,
            detail="nothing in the archive quotes this exact instrument and tenor"))
    else:
        gap, why = level.gap_to(d.model_mid)
        d.trace.append(Ingredient(
            "market level", level.typical if level.enough else None, source="the archive",
            applied=False, detail=level.describe()))
        if level.enough and not math.isnan(gap) and abs(gap) > max(
                0.10, 0.02 * max(1.0, abs(level.typical))):
            d.flags.append(why)
        if level.enough and level.newest_days > request.stale_days:
            d.flags.append(f"the newest archived quote for this is {level.newest_days:.0f} days "
                           f"old, so the level check behind it is stale")

    # -- 3. the width ---------------------------------------------------------
    overlay = pk.overlay(instrument=a.instrument, days=days, tenor=a.tenor, size=a.size,
                         size_basis=a.size_basis, delta=a.delta, fallback=None)
    evidence = synthesis.width_for(instrument=a.instrument, days=days, delta=a.delta)
    width = width_source = None
    if overlay.spread is not None:
        width, width_source = overlay.spread, "bank"
        d.trace.append(Ingredient("width", width, source=f"the bank: {overlay.spread_rule}"))
    elif request.use_archive_width and evidence is not None and evidence.enough:
        width, width_source = evidence.median, "archive"
        d.trace.append(Ingredient(
            "width", width, source="the archive (no bank rule matched)",
            detail=evidence.describe()))
        d.advice.append(f"this width came from the archive and not from the bank; "
                        f"'agent learn --save' writes it in as a rule")
    elif request.fallback_spread not in (None, ""):
        width, width_source = float(request.fallback_spread), "fallback"
        d.trace.append(Ingredient("width", width, source="the fallback typed on this panel",
                                  detail="no bank rule matched and the archive is too thin"))
    else:
        width_source = "none"
        why = "no bank rule matched"
        if evidence is not None and not evidence.enough:
            why += f", and {evidence.why_not}"
        elif evidence is None:
            why += ", and the archive holds no width for this instrument at this tenor"
        d.trace.append(Ingredient("width", None, source="nothing", detail=why))
        d.warnings.append(f"no width: {why}. There is no built-in default, so there is no price")
    d.width, d.width_source = width, width_source

    if overlay.floor is not None:
        d.floor = overlay.floor
        if width is not None and width < overlay.floor:
            d.trace.append(Ingredient("floor", overlay.floor,
                                      source=f"the bank: {overlay.floor_rule}",
                                      detail=f"the width was {width:.3f} and is held at the floor"))
            width = overlay.floor
            d.width = width
        else:
            d.trace.append(Ingredient("floor", overlay.floor, applied=False,
                                      source=f"the bank: {overlay.floor_rule}",
                                      detail="the width is already at or above it"))
    d.advice.extend(overlay.notes)
    if overlay.reason:
        # The bank's own sentence about why it matched nothing.  A warning
        # only when nothing else filled the gap: with a width from the
        # archive on the row, "so it has no bid or offer" is a warning that
        # contradicts the price printed above it, and a screen that argues
        # with itself is a screen that gets ignored.
        if width_source == "none":
            d.warnings.append(overlay.reason)
        else:
            d.trace.append(Ingredient(
                "bank", None, applied=False, source="the bank", detail=overlay.reason))
    for beaten in overlay.beaten:
        d.trace.append(Ingredient("beaten rule", None, applied=False, source="the bank",
                                  detail=beaten))

    # -- 4. the leans ---------------------------------------------------------
    half = None if width is None else width / 200.0        # decimals, as skew_for wants
    richness = None if rich_at is None else rich_at(t)
    axe = None if axe_at is None else axe_at(t)
    if a.instrument == "spread":
        t_near = expiries[_key(a.tenor)][1]
        richness = None if rich_at is None else rich_at(t) - rich_at(t_near)
        axe = None if axe_at is None else axe_at(t) - axe_at(t_near)
        d.advice.append("the width and the shading are taken across the spread: the bank rule "
                        f"is matched on the {a.tenor_far} leg and the richness and the axe are "
                        f"the {a.tenor_far} figure less the {a.tenor} one")
    skew = skew_for(q, t, half_width=half, richness=richness, axe=axe,
                    fair_weight=request.fair_weight, axe_weight=request.axe_weight,
                    cap_ratio=request.skew_cap, bank_shift=(overlay.shift or 0.0) / 100.0)
    d.trace.append(Ingredient(
        "shading, fair value", skew.fair * 100.0, source="implied against realized",
        detail=("nothing shades this row" if richness is None else
                f"the mark is {richness * 100.0:+.3f} rich to fair value at this tenor")))
    d.trace.append(Ingredient(
        "shading, position", skew.axe * 100.0, source="the vega profile",
        detail=("no position was given" if axe is None else
                f"the position at this tenor is {axe:+.2f} of a full axe")))
    d.trace.append(Ingredient(
        "shift, bank", skew.bank * 100.0,
        source=f"the bank: {overlay.shift_rule}" if overlay.shift_rule else "the bank",
        detail="no shift rule matched" if not overlay.shift_rule else ""))
    if skew.capped:
        d.trace.append(Ingredient(
            "shading, capped", skew.total * 100.0, source="the cap on this panel",
            detail=f"the total lean was held to {request.skew_cap:g} of a half width; "
                   f"{skew.reason}"))
    d.shading = skew.total * 100.0

    # -- 5. the record, in words and moving nothing ---------------------------
    record = synthesis.outcome_for(instrument=a.instrument, days=days)
    if record is not None and record.enough:
        d.trace.append(Ingredient("our record", None, applied=False, source="the archive",
                                  detail=record.describe()))
        which, why = record.lean()
        if which:
            d.flags.append(why + " -- shown here, and applied to nothing")

    # -- 6. the price ---------------------------------------------------------
    if width is None:
        d.mid = d.model_mid
        return d
    d.mid = d.model_mid + d.shading
    d.bid = d.mid - width / 2.0
    d.offer = d.mid + width / 2.0
    d.trace.append(Ingredient(
        "mid", d.mid, source="the mark plus the shading",
        detail=f"{d.model_mid:.3f} {d.shading:+.3f}"))
    d.trace.append(Ingredient(
        "bid / offer", None, source=f"the mid, {width:.3f} wide",
        detail=f"{d.bid:.3f} / {d.offer:.3f}"))

    if evidence is not None and evidence.enough and evidence.model_read:
        d.flags.append(f"{evidence.model_read} of {evidence.observations} observation(s) behind "
                       f"this width were transcribed by a language model and checked by the "
                       f"quote parser")
    return d




# --------------------------------------------------------------------------
# The card inside the market-maker tab
# --------------------------------------------------------------------------
# The tab already answers "what do I show against this market".  What it
# cannot answer on its own is "and is that width the one this thing actually
# trades at" -- the bank holds what somebody decided, and the archive holds
# what the market has been doing since.  This panel puts the two beside each
# other, per quoted row, and says nothing else: it proposes, and the width on
# the quote sheet does not move until a rule is written.
#
# It deliberately does *not* fit anything.  A width comparison needs the
# paste, the bank and the archive and no surface at all, so the card answers
# without touching the curve, the wings or the marks -- which is what lets it
# sit on its own button beside a fit that takes a second and a half.

#: How far apart the bank and the archive have to be before it is worth
#: saying so, as a fraction of the archived width.  Below this the two agree:
#: a ladder written at 0.40 against a market that has been 0.41 is a ladder
#: that is right, and a screen that says otherwise is a screen with an
#: opinion about every row, which is a screen nobody reads.
DEFAULT_TOLERANCE = 0.10

#: The narrowest gap worth reporting whatever the fraction says.  Without it
#: a 0.08 butterfly width would be "disagreeing" over four thousandths.
MIN_GAP = 0.02

VERDICTS = ("agrees", "tight", "wide", "no rule", "thin", "not read")


@dataclass
class SuggestPanel:
    """One run of the quoting-agent card: the paste, against the archive."""

    pair: str
    text: str = ""
    fly_convention: str = "market"
    vol_unit: str = "auto"
    fallback_spread: float | None = None
    half_life: float = syn.DEFAULT_HALF_LIFE
    min_effective: float = syn.DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 90.0
    include_model_read: bool = True
    tolerance: float = DEFAULT_TOLERANCE

    def run(self, book, archive: arch.Archive, bank: KnowledgeBank | None = None) -> dict:
        from .quotes import parse_quotes
        pair = self.pair.upper()
        if book is None:
            raise AgentError("the quoting agent needs a loaded book for its valuation time")
        clock = book.clock
        out: dict = {
            "pair": pair, "valuation": clock.now.isoformat(timespec="seconds"),
            "half_life": self.half_life, "min_effective": self.min_effective,
            "lookback_days": self.lookback_days,
            "include_model_read": bool(self.include_model_read),
            "tolerance": self.tolerance,
            "archive": _archive_block(archive, pair, clock.now),
            "widths": [], "rows": [], "notes": [], "skipped": [], "warnings": [],
        }
        for problem in archive.problems:
            out["warnings"].append(problem)

        synthesis = syn.synthesize(
            archive, pair, asof=clock.now, half_life=self.half_life,
            min_effective=self.min_effective, lookback_days=self.lookback_days,
            include_model_read=self.include_model_read)
        out["notes"].extend(synthesis.notes)
        out["widths"] = [_width_json(w) for w in synthesis.widths]

        if not str(self.text or "").strip():
            out["notes"].append("nothing is pasted, so there is nothing to compare; the widths "
                                "above are what the archive holds for this pair")
            return out

        try:
            run_ = parse_quotes(self.text, pair=pair, vol_unit=self.vol_unit,
                                fly_convention=self.fly_convention)
        except QuoteError as exc:
            out["warnings"].append(str(exc))
            return out
        out["notes"].extend(run_.notes)
        out["skipped"] = [{"line": n, "why": why, "text": text}
                          for n, why, text in run_.skipped]

        pk: PairKnowledge = (bank or KnowledgeBank()).for_pair(pair)
        fallback = (None if self.fallback_spread in (None, "")
                    else float(self.fallback_spread))
        # Every quote in the run, superseded ones included: one tenor quoted
        # twice is one live price and two observations of how wide it is
        # shown, and the width question is about the second thing.
        for q in run_.all_quotes:
            out["rows"].append(_suggest_row(q, pk=pk, synthesis=synthesis, clock=clock,
                                            fallback=fallback, tolerance=self.tolerance))
        disagreeing = [r for r in out["rows"] if r["verdict"] in ("tight", "wide")]
        if disagreeing:
            out["notes"].append(
                f"{len(disagreeing)} row(s) are shown at a width the archive does not support; "
                f"nothing has been changed -- write a rule if you agree with it")
        return out


def _archive_block(archive: arch.Archive, pair: str, now: datetime) -> dict:
    """What the file holds for this pair, and how fresh it is."""
    rows = [r for r in archive.summary() if r["pair"] == pair.upper()]
    block = {"path": archive.path, "records": 0, "quote": 0, "trade": 0, "shown": 0,
             "outcome": 0, "last": "", "first": "", "model_read": 0, "age_days": None,
             "pairs": len(archive.pairs())}
    if rows:
        block.update(rows[0])
        newest = arch.parse_time(block.get("last") or "")
        if newest is not None:
            block["age_days"] = round(
                max(0.0, (now - newest).total_seconds() / 86400.0), 3)
    return block


def _rounded(value, places: int = 6):
    """A float the page can print and two runs can compare.

    The screen formats to three places anyway; what this is really for is the
    JSON, which is read by people and diffed by tests, and where
    ``0.39999999999999947`` is the same number wearing a disguise.
    """
    if value is None:
        return None
    try:
        out = round(float(value), places)
    except (TypeError, ValueError):
        return None
    return None if out != out else out          # nan carries no information here


def _width_json(w: syn.WidthEvidence) -> dict:
    return {"instrument": w.instrument, "bucket": w.bucket, "delta": w.delta,
            "observations": w.observations, "effective": _rounded(w.effective, 3),
            "sources": w.sources, "median": _rounded(w.median), "low": _rounded(w.low),
            "high": _rounded(w.high), "tightest": _rounded(w.tightest),
            "widest": _rounded(w.widest), "newest_days": _rounded(w.newest_days, 3),
            "model_read": w.model_read, "enough": w.enough, "why_not": w.why_not,
            "describe": w.describe()}


def _suggest_row(q: MarketQuote, *, pk: PairKnowledge, synthesis: syn.Synthesis,
                 clock, fallback: float | None, tolerance: float) -> dict:
    """One quoted row: what it was shown at, what we would show, what the market has."""
    tenor = _tenor_of(q.expiry_far if q.instrument == "spread" else q.expiry)
    days = syn.days_of(tenor, asof=clock.now)
    row = {
        "line": q.line, "describe": q.describe(), "label": q.label,
        "instrument": q.instrument, "tenor": tenor, "delta": q.delta,
        "size": q.size, "size_basis": q.size_basis,
        "superseded": bool(q.replaced_by), "days": _rounded(days, 3),
        # A choice price is a real thing to be shown and it is a width of
        # zero, not a missing width.  It is displayed as it was and left out
        # of the archive's own statistics upstream.
        "market_width": _rounded(q.spread * 100.0),
        "bank_width": None, "bank_rule": None, "width_source": "none",
        "archive_width": None, "archive_low": None, "archive_high": None,
        "archive_observations": 0, "archive_sources": 0, "archive_newest_days": None,
        "archive_model_read": 0, "archive_enough": False,
        "gap": None, "verdict": "not read", "note": "",
    }
    if days is None:
        row["note"] = (f"the tenor {tenor!r} could not be turned into days, so no archived "
                       f"width could be matched to it")
        return row

    overlay = pk.overlay(instrument=q.instrument, days=days, tenor=tenor, size=q.size,
                         size_basis=q.size_basis, delta=q.delta, fallback=fallback)
    row["bank_width"] = _rounded(overlay.spread)
    row["bank_rule"] = overlay.spread_rule
    row["width_source"] = ("bank" if overlay.spread_rule else
                           "fallback" if overlay.spread is not None else "none")

    evidence = synthesis.width_for(instrument=q.instrument, days=days, delta=q.delta)
    if evidence is None:
        row["verdict"] = "thin"
        row["note"] = ("the archive holds no width for this instrument at this tenor; "
                       "nothing to compare the bank against")
        return row
    row.update({
        "archive_width": _rounded(evidence.median) if evidence.enough else None,
        "archive_low": _rounded(evidence.low), "archive_high": _rounded(evidence.high),
        "archive_observations": evidence.observations,
        "archive_sources": evidence.sources,
        "archive_newest_days": _rounded(evidence.newest_days, 3),
        "archive_model_read": evidence.model_read,
        "archive_enough": evidence.enough,
    })
    if not evidence.enough:
        row["verdict"] = "thin"
        row["note"] = f"not enough behind a width here: {evidence.why_not}"
        return row

    archived = evidence.median
    if overlay.spread is None:
        row["verdict"] = "no rule"
        row["note"] = (f"no bank rule matches this, and the archive has it "
                       f"{archived:.3f} wide over {evidence.observations} observation(s) "
                       f"from {evidence.sources} source(s)")
        return row

    gap = overlay.spread - archived
    row["gap"] = _rounded(gap)
    threshold = max(MIN_GAP, abs(archived) * max(0.0, tolerance))
    if abs(gap) <= threshold:
        row["verdict"] = "agrees"
        row["note"] = (f"the {row['width_source']} width and the archive agree to within "
                       f"{threshold:.3f}")
        return row
    row["verdict"] = "tight" if gap < 0 else "wide"
    side = "tighter" if gap < 0 else "wider"
    age = ("today" if evidence.newest_days < 1 else f"{evidence.newest_days:.0f} days ago")
    row["note"] = (
        f"the {row['width_source']} would show {overlay.spread:.3f}, which is {abs(gap):.3f} "
        f"{side} than the {archived:.3f} this has been shown over "
        f"{evidence.observations} observation(s) from {evidence.sources} source(s), "
        f"newest {age}")
    return row


def _tenor_of(expiry) -> str:
    if expiry is None:
        return ""
    if isinstance(expiry, str):
        return expiry.upper()
    if hasattr(expiry, "isoformat"):
        return expiry.isoformat()[:10]
    return str(expiry)


def panel_from_request(payload: dict) -> SuggestPanel:
    """The quoting-agent card as the browser posts it.

    Same shape as ``marketmaker.panel_from_request`` and for the same reason:
    the browser owns the panel and posts it whole, so a card set up on the
    screen and the same card run from a shell produce identical numbers.  A
    field the browser sends that this does not read is a setting that silently
    does nothing, and a test pins the two lists against each other.
    """
    def number(name, default=None):
        raw = payload.get(name, None)
        if raw in (None, "", "none"):
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise AgentError(f"{name} must be a number, not {raw!r}") from None

    def flag(name, default=True):
        raw = payload.get(name, default)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("", "0", "no", "off", "false")
        return bool(raw)

    pair = str(payload.get("pair") or "").strip().upper()
    if not pair:
        raise AgentError("the quoting agent needs a pair")
    return SuggestPanel(
        pair=pair, text=str(payload.get("text") or ""),
        fly_convention=str(payload.get("fly_convention") or "market"),
        vol_unit=str(payload.get("vol_unit") or "auto"),
        fallback_spread=number("fallback_spread"),
        half_life=number("half_life", syn.DEFAULT_HALF_LIFE),
        min_effective=number("min_effective", syn.DEFAULT_MIN_EFFECTIVE),
        lookback_days=number("lookback_days", 90.0),
        include_model_read=flag("include_model_read", True),
        tolerance=number("tolerance", DEFAULT_TOLERANCE))


def file_paste(archive: arch.Archive, payload: dict, *, clock,
               counterparty: str = "") -> dict:
    """Put the run currently on the screen into the archive.

    The timestamp a line with no clock on it gets is **the start of the
    valuation day**, not the instant the button was pressed.  The id is a hash
    of the content, so "now" would give the same run a new id every time it
    was filed and a morning double-clicked would count twice in every width
    it touches.  Midnight of the valuation day makes filing a run twice in a
    day file it once, and the day is all the resolution an age weight
    measured in days can use anyway.
    """
    from .quotes import parse_quotes
    panel = panel_from_request(payload)
    if not str(panel.text or "").strip():
        raise AgentError("there is nothing pasted to file")
    run_ = parse_quotes(panel.text, pair=panel.pair, vol_unit=panel.vol_unit,
                        fly_convention=panel.fly_convention)
    day = clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
    observations = arch.from_quotes(
        run_, pair=panel.pair, source="chat", origin="pasted on the market-maker screen",
        counterparty=counterparty, via="hand", default_time=day)
    # The broker's name is part of what makes an observation distinct -- the
    # same width from three brokers is stronger evidence than three quotes
    # from one -- so filing the same run again under a different name is a
    # genuinely new record and not a duplicate.  It is also the obvious way
    # to double a width by accident, so it is counted and said out loud.
    anonymous = {replace(o, counterparty="").id
                 for o in archive.query(pair=panel.pair, kinds="quote")}
    under_another_name = sum(1 for o in observations
                             if o.id not in archive._ids
                             and replace(o, counterparty="").id in anonymous)
    added, refused = archive.extend(observations)
    written = archive.flush()
    notes = list(run_.notes)
    if under_another_name:
        notes.append(
            f"{under_another_name} of these quote(s) are already in the archive under a "
            f"different broker name and have been filed again; that is right when two brokers "
            f"really showed the same market, and doubles the evidence behind a width when it "
            f"was the same run filed twice")
    return {
        "pair": panel.pair, "read": len(observations), "added": added,
        "already_held": len(observations) - added - len(refused),
        "under_another_name": under_another_name,
        "refused": refused, "written": written, "path": archive.path,
        "notes": notes,
        "skipped": [{"line": n, "why": why, "text": text}
                    for n, why, text in run_.skipped],
    }


# --------------------------------------------------------------------------
def record_shown(archive: arch.Archive, run_: AgentRun, *, counterparty: str = "",
                 at: datetime | None = None) -> list[arch.Observation]:
    """Write every price this run made into the archive, so it can be answered later.

    This is what closes the loop.  A price that is shown and not recorded can
    never become evidence about whether we were right, and the moment to
    record it is the moment it was made -- with the mid the model had *then*,
    which is why ``model_mid`` goes on the record rather than being looked up
    when the outcome arrives.
    """
    when = at or datetime.now(timezone.utc)
    written = []
    for d in run_.decisions:
        if not d.priced:
            continue
        obs = arch.shown(
            d.pair, instrument=d.ask.instrument, tenor=d.ask.tenor,
            tenor_far=d.ask.tenor_far, bid=d.bid, ask=d.offer, delta=d.ask.delta,
            strike=d.ask.strike, is_call=d.ask.is_call, fly_kind=d.ask.fly_kind,
            size=d.ask.size, size_basis=d.ask.size_basis, counterparty=counterparty,
            model_mid=d.model_mid,
            model_note=f"width {d.width:.3f} from the {d.width_source}, "
                       f"shading {d.shading:+.3f}",
            at=when, notes=tuple(d.flags))
        ok, why = archive.add(obs)
        if ok:
            written.append(obs)
        else:
            run_.warnings.append(f"the price {d.quote_text()} was not recorded: {why}")
    return written
