"""Market quotes as a trader writes them, turned into something the book can price.

The other paste-driven screen, :mod:`volkit.listed`, reads a *table*: columns
of strikes and volatilities out of an exchange feed.  A broker run does not
arrive as a table.  It arrives as lines of shorthand English --

    1M ATM 8.20/8.60 in 100mm vega
    3M 25d RR 0.35/0.55 eur call over
    2M 25d fly 0.20/0.28
    6M 1.1000 call 7.90/8.40
    1M/3M ATM spread 0.30/0.55

-- and every one of those lines carries conventions that a naive reader gets
wrong.  This module reads them, and its whole design principle is that
**every inference is reported and nothing ambiguous is guessed**.

Three things are easy to get wrong and are therefore handled explicitly.

*Units.*  ``8.20`` is a volatility in points and ``0.0820`` is the same
volatility as a decimal.  The unit is decided **once for the whole paste**
from its at-the-money and outright lines, never per line -- a small risk
reversal read on its own looks exactly like a decimal at-the-money and comes
back a hundred times too large.  That is the same rule §9 states for a
historical sheet, for the same reason.  A paste with no level quote in it
cannot decide, so it does not: the caller's ``vol_unit`` settles it and a note
says the paste was silent.

*Risk-reversal direction.*  volkit's risk reversal is the base currency's call
volatility less its put volatility.  ``EUR call over`` on EURUSD is positive in
that convention; ``JPY call over`` on USDJPY is a *dollar put* over and is
negative.  A direction word is resolved against the pair.  A line with no
direction word is read in the book's own convention and the paste says so once
-- getting the sign product right while getting a sign wrong is exactly how the
cross-triangle bug in §5 happened.

*Butterflies.*  The interbank ``25d fly`` is normally the market strangle --
the number the workbook stores in ``st_25`` -- and not the smile butterfly
``(call + put)/2 - atm``.  The two differ by the strangle margin, which is
small for a flat smile and not small for a skewed one.  A quote may say which
it is (``strangle`` forces the market reading, ``smile fly`` the other); when
it does not, the caller's default applies and the quote records that it was
inherited rather than stated.

Volatilities coming out of here are **decimals** (0.0820), like everywhere else
in the package.  Sizes are in millions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .timeutil import TenorError, parse_datetime, parse_tenor

INSTRUMENTS = ("atm", "rr", "fly", "outright", "spread")
VOL_UNITS = ("auto", "percent", "decimal")
FLY_CONVENTIONS = ("market", "smile")
SIZE_BASES = ("vega", "notional", "unspecified")

# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketQuote:
    """One line of a broker run, in the book's own conventions.

    ``bid`` and ``ask`` are decimals and are always ordered, whatever order the
    line was written in; ``inverted`` records when applying a direction word
    reordered them, so a reordering that a sign explains is distinguishable
    from one that looks like a typo.
    """

    instrument: str
    expiry: object                       # tenor string, date or datetime
    bid: float
    ask: float
    expiry_far: object | None = None     # the second leg of a calendar spread
    leg: str | None = None               # what a spread is a spread *of*
    delta: float | None = None           # 0.25, 0.10 ... for rr / fly / delta outrights
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None          # 'market' or 'smile' when the line said so
    size: float | None = None            # millions
    size_basis: str = "unspecified"
    weight: float = 1.0
    label: str = ""
    line: int = 0
    raw: str = ""
    inverted: bool = False
    direction: str | None = None         # the 'EUR call over' word, when there was one
    notes: tuple[str, ...] = ()

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def is_choice(self) -> bool:
        """A single number rather than a two-way price."""
        return self.ask <= self.bid

    def describe(self) -> str:
        """A short human label, used in tables and error messages."""
        base = str(self.expiry)
        if self.instrument == "spread":
            leg = {"atm": "ATM", "rr": f"{int(round((self.delta or 0) * 100))}d RR",
                   "fly": f"{int(round((self.delta or 0) * 100))}d fly"}.get(
                       self.leg or "atm", (self.leg or "").upper())
            return f"{self.expiry_far} less {base} {leg}".strip()
        if self.instrument == "atm":
            return f"{base} ATM"
        if self.instrument in ("rr", "fly"):
            return f"{base} {int(round((self.delta or 0) * 100))}d {self.instrument.upper()}"
        side = "call" if self.is_call else "put"
        if self.strike is not None:
            return f"{base} {self.strike:g} {side}"
        return f"{base} {int(round((self.delta or 0) * 100))}d {side}"


@dataclass(frozen=True)
class ParsedRun:
    """Everything a paste produced, including what it could not use."""

    quotes: tuple[MarketQuote, ...]
    vol_unit: str
    unit_evidence: str
    notes: tuple[str, ...] = ()
    skipped: tuple[tuple[int, str, str], ...] = ()   # line number, text, reason


class QuoteError(ValueError):
    """Raised when a paste as a whole cannot be read."""


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

_ATM = ("atm", "atmf", "atmv", "atmvol", "ato")
_RR = ("rr", "riskreversal", "riskreversals", "riskie", "riskies", "reversal")
_FLY = ("fly", "flies", "bf", "butterfly", "butterflies")
_STRANGLE = ("strangle", "strangles", "stgl", "stg")
_SMILE_FLY = ("smilefly", "smilebutterfly", "smilebf")
_CALL = ("call", "calls", "c")
_PUT = ("put", "puts", "p")
_SPREAD = ("spread", "spreads", "cal", "calendar", "vs", "versus")
_MID = ("mid", "mids", "choice", "chc")
_DROP = ("vol", "vols", "volatility", "in", "on", "of", "the", "for", "at", "px", "prices",
         "quote", "quoted", "market", "mkt", "size", "please", "pls", "level", "abt", "around")

_TENOR = re.compile(r"^(\d+(?:\.\d+)?)([dwmy])$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DELTA = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:d|delta|dl)$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mm|mio|mln|m|k|bn|b)$")
_NUMBER = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)$")
_STRIKE_WORD = re.compile(r"^(?:k|strike|struck)[=:]?$")

# ``/`` and ``@`` are always separators.  A bare hyphen is one only when it is
# spaced, so ``-0.4/-0.15`` keeps both signs while ``0.20 - 0.28`` splits.
_SEP = re.compile(r"\s+[-–—]\s+|[/@]|\s+x\s+|\s+by\s+")


def _norm(text: str) -> str:
    """Lower-case, unify punctuation, and give every separator its own space."""
    s = text.strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = s.replace(",", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _squash(token: str) -> str:
    return re.sub(r"[^a-z0-9.+-]", "", token)


def _as_expiry(token: str):
    """A tenor string or an ISO date, or ``None`` if it is neither."""
    if _DATE.match(token):
        return parse_datetime(token)
    if _TENOR.match(token):
        try:
            parse_tenor(token)
        except TenorError:
            return None
        return token.upper()
    return None


# ---------------------------------------------------------------------------
# one line
# ---------------------------------------------------------------------------


@dataclass
class _Line:
    """Working state while a single line is consumed token by token."""

    instrument: str | None = None
    expiries: list = field(default_factory=list)
    delta: float | None = None
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None
    size: float | None = None
    size_basis: str = "unspecified"
    over: str | None = None              # the currency (or 'call'/'put') said to be over
    explicit_spread: bool = False
    literal_order: bool = False          # 'A-B' rather than 'A/B'
    numbers: list = field(default_factory=list)
    label: str = ""
    notes: list = field(default_factory=list)


def _consume(line: str, state: _Line) -> None:
    """Pull every recognised token out of the line, leaving only the price."""
    # A label in square brackets is kept verbatim and taken out of the way.
    m = re.search(r"\[([^\]]*)\]", line)
    if m:
        state.label = m.group(1).strip()
        line = line[:m.start()] + " " + line[m.end():]

    # A two-legged tenor written without spaces: 1m/3m, 3m-1m, 1mx3m.
    def take_pair(mt):
        a, b = _as_expiry(mt.group(1)), _as_expiry(mt.group(2))
        if a is None or b is None:
            return mt.group(0)
        state.expiries.extend([a, b])
        state.explicit_spread = True
        state.literal_order = mt.group(0).count("-") == 1
        return " "

    line = re.sub(r"(\d+(?:\.\d+)?[dwmy])\s*(?:/|-|x|vs)\s*(\d+(?:\.\d+)?[dwmy])",
                  take_pair, line)

    tokens = line.split()
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        word = _squash(tok)
        nxt = _squash(tokens[i + 1]) if i + 1 < len(tokens) else ""

        # 25d, 25 delta, 10dRR, RR25
        md = _DELTA.match(word) or (_DELTA.match(word + nxt) if nxt in ("d", "delta", "dl") else None)
        if md is None:
            joined = re.match(r"^(\d+(?:\.\d+)?)d(rr|fly|bf|c|p|call|put)$", word)
            if joined:
                md = _DELTA.match(joined.group(1) + "d")
                tokens.insert(i + 1, joined.group(2))
        if md:
            value = float(md.group(1))
            state.delta = value / 100.0 if value > 1.0 else value
            if _DELTA.match(word + nxt) and not _DELTA.match(word):
                i += 1
            i += 1
            continue

        exp = _as_expiry(word)
        if exp is not None:
            state.expiries.append(exp)
            i += 1
            continue

        ms = _SIZE.match(word)
        if ms is None and nxt in ("mm", "mio", "mln", "m", "k", "bn", "b") and _NUMBER.match(word):
            ms = _SIZE.match(word + nxt)
            if ms:
                i += 1
        if ms:
            scale = {"k": 1e-3, "m": 1.0, "mm": 1.0, "mio": 1.0, "mln": 1.0, "bn": 1e3, "b": 1e3}
            state.size = float(ms.group(1)) * scale[ms.group(2)]
            i += 1
            continue

        if word in ("vega", "veg"):
            state.size_basis = "vega"
            i += 1
            continue
        if word in ("notional", "not", "ccy"):
            state.size_basis = "notional"
            i += 1
            continue

        if _STRIKE_WORD.match(word):
            if i + 1 < len(tokens) and _NUMBER.match(_squash(tokens[i + 1])):
                state.strike = float(_squash(tokens[i + 1]))
                i += 2
                continue
            i += 1
            continue

        if word in _ATM:
            state.instrument = "atm"
            i += 1
            continue
        if word in _RR:
            state.instrument = "rr"
            i += 1
            continue
        if word in _SMILE_FLY:
            state.instrument, state.fly_kind = "fly", "smile"
            i += 1
            continue
        if word in _FLY:
            state.instrument = "fly"
            if word in ("bf", "butterfly", "butterflies") and nxt in ("smile",):
                state.fly_kind = "smile"
            i += 1
            continue
        if word in _STRANGLE:
            state.instrument, state.fly_kind = "fly", "market"
            i += 1
            continue
        if word == "smile" and nxt in _FLY:
            state.fly_kind = "smile"
            i += 1
            continue
        if word in _SPREAD:
            state.explicit_spread = True
            i += 1
            continue
        if word in _CALL:
            if state.instrument in (None, "outright"):
                state.instrument = "outright"
                state.is_call = True
            state.over = state.over or ("call" if nxt == "over" else None)
            i += 1
            continue
        if word in _PUT:
            if state.instrument in (None, "outright"):
                state.instrument = "outright"
                state.is_call = False
            state.over = state.over or ("put" if nxt == "over" else None)
            i += 1
            continue
        if word == "over":
            # "eur call over" / "eur over" -- the currency is the token before.
            for back in (rest[-1] if rest else "", _squash(tokens[i - 1]) if i else ""):
                if re.fullmatch(r"[a-z]{3}", back):
                    state.over = back
                    if rest and rest[-1] == back:
                        rest.pop()
                    break
            i += 1
            continue
        if re.fullmatch(r"[a-z]{3}", word) and i + 2 < len(tokens) and \
                _squash(tokens[i + 2]) == "over" and _squash(tokens[i + 1]) in _CALL + _PUT:
            state.over = word
            i += 1
            continue
        if word in _MID:
            state.notes.append("written as a single mid, so there is no market width in it")
            i += 1
            continue
        if word in _DROP or word == "":
            i += 1
            continue
        rest.append(tok)
        i += 1

    # Whatever is left has to be the price.
    remainder = " ".join(rest)
    parts = [w for chunk in _SEP.split(remainder) for w in chunk.split()]
    for p in parts:
        p = _squash(p)
        if _NUMBER.match(p):
            state.numbers.append(float(p))
        elif p:
            state.notes.append(f"ignored {p!r}")


def _resolve_sign(state: _Line, pair: str | None, notes: list[str]) -> float:
    """+1 or -1 for a risk reversal, from the direction word and the pair."""
    if state.over is None:
        return 1.0
    over = state.over
    if over in ("call", "put"):
        return 1.0 if over == "call" else -1.0
    if not pair or len(pair) < 6:
        raise ValueError(
            f"the line says {over.upper()} over but no currency pair was given, so which side "
            f"that is cannot be decided; pass the pair or drop the direction word")
    base, quote = pair[:3].lower(), pair[3:6].lower()
    said_call = state.is_call if state.is_call is not None else True
    if over == base:
        return 1.0 if said_call else -1.0
    if over == quote:
        # A quote-currency call is a base-currency put.
        return -1.0 if said_call else 1.0
    raise ValueError(
        f"{over.upper()} is not a leg of {pair.upper()}, so '{over} over' cannot be resolved")


def _build(state: _Line, pair: str | None, default_fly: str, line_no: int,
           raw: str) -> MarketQuote:
    notes = list(state.notes)
    if not state.numbers:
        raise ValueError("no price on the line")
    if state.instrument is None:
        state.instrument = "atm" if state.delta is None and state.strike is None else "outright"
        notes.append(f"no instrument word, read as {state.instrument}")
    if state.explicit_spread and len(state.expiries) == 2:
        instrument = "spread"
    else:
        instrument = state.instrument

    numbers = list(state.numbers)
    # An outright can carry its strike as a bare number ahead of the price.
    if instrument == "outright" and state.strike is None and state.delta is None:
        if len(numbers) == 3:
            state.strike = numbers.pop(0)
            notes.append(f"leading number read as the strike ({state.strike:g})")
        elif len(numbers) == 2:
            raise ValueError(
                "an outright needs a strike or a delta as well as a price; "
                f"{numbers[0]:g} and {numbers[1]:g} were read as a two-way price")
    if len(numbers) > 2:
        raise ValueError(f"{len(numbers)} numbers left after the tenor and the instrument "
                         f"({', '.join(f'{n:g}' for n in numbers)}); the line is ambiguous")

    if len(numbers) == 1:
        bid = ask = numbers[0]
        notes.append("one number, taken as a choice price with no width")
    else:
        bid, ask = numbers[0], numbers[1]
        # A level quote cannot offer below its own bid.  Written that way it is
        # either a typo or the desk shorthand that drops the leading digits of
        # the offer -- '8.2/6' for 8.20/8.60.  Either way it is refused rather
        # than repaired, because repairing it means inventing the digits.
        if instrument in ("atm", "outright") and ask < bid:
            raise ValueError(
                f"'{bid:g}/{ask:g}' offers below its own bid; that is a typo, or the desk "
                f"shorthand that drops the leading digits of the offer. Write the offer in full")

    if instrument == "rr" or (instrument == "spread" and state.instrument == "rr"):
        sign = _resolve_sign(state, pair, notes)
        if sign < 0:
            bid, ask = sign * bid, sign * ask
            notes.append(f"{(state.over or '').upper()} over on {pair or '?'}: sign flipped into "
                         f"the book's convention (base-currency call over is positive)")
    inverted = ask < bid
    if inverted:
        bid, ask = ask, bid

    if instrument in ("rr", "fly") and state.delta is None:
        raise ValueError(f"a {instrument} needs a delta, e.g. '25d'")
    if instrument == "outright" and state.strike is None and state.delta is None:
        raise ValueError("an outright needs a strike or a delta")
    if instrument == "outright" and state.is_call is None:
        raise ValueError("an outright needs 'call' or 'put'")
    if not state.expiries:
        raise ValueError("no tenor or expiry date on the line")
    if instrument == "spread" and len(state.expiries) != 2:
        raise ValueError("a spread needs exactly two tenors")
    if instrument != "spread" and len(state.expiries) > 1:
        raise ValueError(f"{len(state.expiries)} tenors on a line that is not a spread")

    near, far = state.expiries[0], (state.expiries[1] if len(state.expiries) > 1 else None)
    if instrument == "spread":
        if state.literal_order:
            notes.append(f"'{near}-{far}' read literally: {near} less {far}")
            near, far = far, near      # stored as far-less-near below
        else:
            notes.append(f"'{near}/{far}' read as the calendar convention: {far} less {near}")

    return MarketQuote(
        instrument=instrument, expiry=near, expiry_far=far,
        leg=(state.instrument if instrument == "spread" else None),
        bid=bid, ask=ask, delta=state.delta, strike=state.strike, is_call=state.is_call,
        fly_kind=state.fly_kind or (default_fly if instrument == "fly" or
                                    state.instrument == "fly" else None),
        size=state.size, size_basis=state.size_basis, label=state.label,
        line=line_no, raw=raw.strip(), inverted=inverted, direction=state.over,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# the paste
# ---------------------------------------------------------------------------


def _decide_unit(quotes: list[MarketQuote], forced: str) -> tuple[str, str]:
    """One volatility unit for the whole paste, from its level quotes only.

    A risk reversal cannot vote.  ``0.35`` is an entirely ordinary risk
    reversal in points and an entirely ordinary at-the-money in decimals, so
    letting one decide would turn a 0.35 point skew into 35 points.
    """
    if forced in ("percent", "decimal"):
        return forced, f"forced to {forced} by the caller"
    levels = [abs(q.mid) for q in quotes if q.instrument in ("atm", "outright")]
    if not levels:
        return "percent", ("the paste has no at-the-money or outright level in it, so it could "
                           "not decide its own unit; read as percent")
    lo, hi = min(levels), max(levels)
    if hi < 1.0:
        return "decimal", f"every level quote is below 1.0 (largest {hi:.4g})"
    if lo >= 1.0:
        return "percent", f"every level quote is at or above 1.0 (smallest {lo:.4g})"
    raise QuoteError(
        f"the level quotes straddle 1.0 ({lo:.4g} to {hi:.4g}), so the paste is percent in one "
        f"place and decimal in another. Fix the paste or set the volatility unit explicitly "
        f"rather than have it guessed line by line")


def parse_quotes(text: str, *, pair: str | None = None, vol_unit: str = "auto",
                 fly_convention: str = "market") -> ParsedRun:
    """Read a broker run.  Volatilities come back as decimals.

    Nothing is dropped quietly: every line that cannot be used is returned in
    ``skipped`` with the reason, and every inference the reader made is in
    ``notes`` or on the quote itself.
    """
    if vol_unit not in VOL_UNITS:
        raise ValueError(f"unknown volatility unit {vol_unit!r}; expected one of {VOL_UNITS}")
    if fly_convention not in FLY_CONVENTIONS:
        raise ValueError(
            f"unknown butterfly convention {fly_convention!r}; expected one of {FLY_CONVENTIONS}")

    raw_quotes: list[MarketQuote] = []
    skipped: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0]
        if not body.strip():
            continue
        state = _Line()
        try:
            _consume(_norm(body), state)
            raw_quotes.append(_build(state, pair, fly_convention, n, raw))
        except (ValueError, TenorError) as exc:
            skipped.append((n, raw.strip(), str(exc)))

    unit, evidence = _decide_unit(raw_quotes, vol_unit)
    scale = 0.01 if unit == "percent" else 1.0
    quotes = tuple(
        MarketQuote(**{**vars(q), "bid": q.bid * scale, "ask": q.ask * scale,
                       "strike": q.strike})
        for q in raw_quotes
    )

    notes = [f"volatility unit: {unit} ({evidence})"]
    unsigned = [q for q in quotes
                if (q.instrument == "rr" or (q.instrument == "spread" and q.leg == "rr"))
                and q.direction is None]
    if unsigned:
        notes.append(
            f"{len(unsigned)} risk reversal(s) carried no direction word and were read in the "
            f"book's own convention: a base-currency call over is positive. Write "
            f"'{(pair or 'EURUSD')[:3].upper()} call over' or "
            f"'{(pair or 'EURUSD')[3:6].upper()} call over' to say it outright")
    flies = [q for q in quotes if q.instrument == "fly"]
    inherited = [q for q in flies if q.fly_kind == fly_convention and
                 not any("strangle" in x for x in q.notes)]
    if flies:
        notes.append(
            f"{len(flies)} butterfly quote(s); {len(inherited)} of them did not say which "
            f"butterfly they are and were read as the {fly_convention} convention"
            + (" (the market strangle, which is what the workbook marks)"
               if fly_convention == "market" else " (the smile butterfly)"))
    odd = [q for q in quotes if q.inverted and q.instrument != "rr"]
    if odd:
        notes.append(f"{len(odd)} line(s) were written high side first and were reordered: "
                     + ", ".join(f"line {q.line}" for q in odd))
    return ParsedRun(quotes=quotes, vol_unit=unit, unit_evidence=evidence,
                     notes=tuple(notes), skipped=tuple(skipped))


def parse_vega_profile(text: str) -> tuple[dict[str, float], tuple[str, ...], tuple[tuple[int, str, str], ...]]:
    """Read ``tenor value`` lines into an existing-position vega profile.

    The unit is whatever the desk keeps its position in -- only the ratio to
    the axe scale on the panel is ever used, and that ratio is dimensionless.
    A positive number is a long vega position.
    """
    profile: dict[str, float] = {}
    notes: list[str] = []
    skipped: list[tuple[int, str, str]] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0].replace(",", " ").replace(":", " ").strip()
        if not body:
            continue
        bits = body.split()
        if len(bits) < 2:
            skipped.append((n, raw.strip(), "expected a tenor and a number"))
            continue
        key = _as_expiry(_squash(bits[0].lower()))
        if key is None:
            skipped.append((n, raw.strip(), f"{bits[0]!r} is not a tenor"))
            continue
        try:
            value = float(_squash(bits[1]))
        except ValueError:
            skipped.append((n, raw.strip(), f"{bits[1]!r} is not a number"))
            continue
        tenor = str(key).upper()
        if tenor in profile:
            notes.append(f"{tenor} appears more than once; the entries were added together")
            profile[tenor] += value
        else:
            profile[tenor] = value
    return profile, tuple(notes), tuple(skipped)
