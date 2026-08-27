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

It also reads the same run written as **columns**, which is how a run pasted
out of a chat window or a spreadsheet usually arrives:

    09:15, 1M, ATM,  8.20/8.60
    09:15, 3M, 7.75, 8.10/8.50
    09:41, 1M, ATM,  8.25/8.65
    09:42, 2M, 25d,  8.00/8.40

``expiry, strike, bid/offer``, optionally with a timestamp in front of it.  The
middle column is a **strike specification** in the same vocabulary the pricing
screen takes: ``ATM``, an absolute strike, or a delta (``25d``, ``25dp``,
``-25d``).  The two shapes are read by one parser rather than two, because a
run that mixes them -- and they do -- must not depend on which line came first.

Five things are easy to get wrong and are therefore handled explicitly.

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

*Which number is the price.*  A **comma is a column boundary and a price never
straddles one**, so ``3M, 7.75, 8.30`` is a choice price at the 7.75 strike
while ``3M 7.75 8.30`` -- no columns -- is the two-way at-the-money it has
always been.  Without commas the old rule still applies: three numbers on a
line means the first is a strike.  Thousands separators are removed before any
of this, so ``1,000mm`` is a size and not a column.

*Timestamps.*  A run is a conversation, and the same tenor is quoted again when
the market moves.  A leading ``09:15``, ``2024-02-28 09:15`` or
``2024-02-28T09:15Z`` is read as the time the quote was given, and when two
lines quote **the same thing** the later timestamp wins.  The one it replaced
is not dropped -- it is returned in ``ParsedRun.superseded`` with the line that
beat it, because a quote that vanished between the paste and the screen is a
silent zero with a better disguise.  Lines with no timestamp fall back to the
order they were written in, which is the only information they carry.  A
time-only stamp takes the last date seen above it; when no line gives a date at
all the times are read as one day, and a run that crosses midnight will
therefore order wrongly -- it says so rather than pretending otherwise.

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


def _expiry_label(expiry) -> str:
    """A tenor as written, a date as a date.

    ``str()`` on a parsed date expiry gives ``2024-05-28 00:00:00+00:00``, and
    the midnight and the offset are noise in a quote sheet -- neither was on
    the line, and the time of day an expiry is priced at comes from the cut.
    """
    if expiry is None:
        return ""
    if hasattr(expiry, "strftime"):
        return expiry.strftime("%Y-%m-%d")
    return str(expiry)


class _Instrument:
    """What an instrument *is*, apart from what it is worth.

    A broker's quote and a request to be quoted name the same things --
    ``3M 25d RR``, ``1M ATM in 100mm`` -- and differ only in whether a price
    came with it.  The naming lives here so the two cannot drift apart: a
    label that read one way on the market sheet and another on the quote
    sheet would be two names for one instrument on one screen.
    """

    def describe(self) -> str:
        """A short human label, used in tables and error messages."""
        base = _expiry_label(self.expiry)
        if self.instrument == "spread":
            leg = {"atm": "ATM", "rr": f"{int(round((self.delta or 0) * 100))}d RR",
                   "fly": f"{int(round((self.delta or 0) * 100))}d fly"}.get(
                       self.leg or "atm", (self.leg or "").upper())
            return f"{_expiry_label(self.expiry_far)} less {base} {leg}".strip()
        if self.instrument == "atm":
            return f"{base} ATM"
        if self.instrument in ("rr", "fly"):
            return f"{base} {int(round((self.delta or 0) * 100))}d {self.instrument.upper()}"
        if self.strike is not None:
            # A volatility at an absolute strike is one number whichever side
            # it is quoted from, so a strike-column quote is allowed to leave
            # the side out and must not be described as a put by default.
            if self.is_call is None:
                return f"{base} {self.strike:g}"
            return f"{base} {self.strike:g} {'call' if self.is_call else 'put'}"
        side = "call" if self.is_call else "put"
        return f"{base} {int(round((self.delta or 0) * 100))}d {side}"


@dataclass(frozen=True)
class MarketQuote(_Instrument):
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
    #: When the quote was given, resolved for ordering.  ``timestamp_text`` is
    #: what was actually written: a run with no date in it is ordered on a
    #: nominal day, and showing that day back to somebody would be a date the
    #: paste never contained.
    timestamp: object | None = None
    timestamp_text: str = ""
    #: Set only on a quote in :attr:`ParsedRun.superseded`: the line that beat it.
    replaced_by: int | None = None
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

@dataclass(frozen=True)
class ParsedRun:
    """Everything a paste produced, including what it could not use."""

    quotes: tuple[MarketQuote, ...]
    vol_unit: str
    unit_evidence: str
    notes: tuple[str, ...] = ()
    skipped: tuple[tuple[int, str, str], ...] = ()   # line number, text, reason
    #: Quotes replaced by a later one for the same thing.  Kept rather than
    #: dropped: a line that was read, understood and then silently discarded
    #: is the failure this module exists to remove.
    superseded: tuple[MarketQuote, ...] = ()

    @property
    def all_quotes(self) -> tuple[MarketQuote, ...]:
        """Every quote the paste contained, live and superseded, in line order.

        What a superseded quote is still good for is **measuring the market's
        width**.  A tenor quoted at 09:15 and again at 09:41 is one live price
        and two observations of how wide this broker shows it, and throwing the
        first away would learn the ladder off half the evidence.  What it is
        not good for is the fit -- an old mid pulling against the new one is
        the whole reason the later quote replaced it -- so the fit reads
        ``quotes`` and only the bank reads this.
        """
        return tuple(sorted(self.quotes + self.superseded, key=lambda q: q.line))


@dataclass(frozen=True)
class QuoteRequest(_Instrument):
    """An instrument somebody has asked for a price in, with no price on it.

    The market paste says where the market is; this says what is being asked
    for.  They are the same grammar with one thing taken out, so they are read
    by the same tokeniser -- and the *absence* of a price is enforced rather
    than ignored, because a broker run pasted into the request box would
    otherwise read as a list of strikes and be quoted at levels nobody asked
    about.
    """

    instrument: str
    expiry: object
    expiry_far: object | None = None
    leg: str | None = None
    delta: float | None = None
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None
    size: float | None = None            # millions
    size_basis: str = "unspecified"
    label: str = ""
    line: int = 0
    raw: str = ""
    #: ``+1`` when the request is asked in the book's own convention and
    #: ``-1`` when it is the other side of it -- ``JPY call over`` on USDJPY.
    #: The model works in the book's convention throughout and this is applied
    #: once, where the row is built, so a price quoted back the way it was
    #: asked for is never a second place for a sign to live.  §5's first entry
    #: is what a second place for a sign costs.
    sign: float = 1.0
    direction: str | None = None
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        base = super().describe()
        if self.sign < 0 and self.direction:
            return f"{base} ({self.direction.upper()} call over)"
        return base


@dataclass(frozen=True)
class ParsedRequests:
    """Everything the request box produced, including what it could not use."""

    requests: tuple[QuoteRequest, ...]
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
# A clock time, and a date and time in one token.  Both are matched on the raw
# token rather than the squashed one: ``_squash`` strips the colon, which is
# the only thing that tells 09:15 from the number 915.
_TIME = re.compile(r"^(\d{1,2}):([0-5]\d)(?::([0-5]\d)(?:\.\d+)?)?z?$")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[t ]?(\d{1,2}:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?)z?$")
# 1,000mm is a size and not two columns.  Removed before commas mean anything.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DELTA = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:d|delta|dl)$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mm|mio|mln|m|k|bn|b)$")
_NUMBER = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)$")
_STRIKE_WORD = re.compile(r"^(?:k|strike|struck)[=:]?$")

#: Words a column header is made of.  A pasted run out of a spreadsheet brings
#: one, and a header reported as a line that could not be read is noise on top
#: of a paste that worked -- it is recognised and reported as what it is.
_HEADER_WORDS = frozenset((
    "time", "timestamp", "stamp", "when", "expiry", "expiries", "tenor", "maturity",
    "strike", "strikes", "k", "bid", "offer", "ask", "mid", "vol", "vols", "volatility",
    "instrument", "delta", "size", "quote", "price", "prices", "bidoffer", "bidask",
    "broker", "label", "note", "notes", "ccy", "pair",
))


def _is_header(line: str) -> bool:
    """A column header rather than a quote: no numbers, and names its columns.

    Two header words at least: a header names more than one column, and one
    stray word is more likely a line that was meant to be a quote and failed.
    """
    if any(ch.isdigit() for ch in line):
        return False
    words = [_squash(w) for w in line.replace(",", " ").replace("/", " ").split()]
    return sum(1 for w in words if w in _HEADER_WORDS) >= 2

# ``/`` and ``@`` are always separators.  A bare hyphen is one only when it is
# spaced, so ``-0.4/-0.15`` keeps both signs while ``0.20 - 0.28`` splits.
_SEP = re.compile(r"\s+[-–—]\s+|[/@]|\s+x\s+|\s+by\s+")


def _norm(text: str) -> str:
    """Lower-case, unify punctuation, and give every separator its own space.

    Commas survive: they are the column boundary of a pasted run, and turning
    them into spaces is what made ``3M, 7.75, 8.30`` indistinguishable from the
    two-way at-the-money ``3M 7.75 8.30``.  Thousands separators go first, so
    the only commas left are structural.
    """
    s = text.strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = _THOUSANDS.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _as_timestamp(token: str) -> tuple[str | None, str | None] | None:
    """``(date, time)`` for a timestamp token, or None if it is not one.

    Either half may be missing: a bare ``09:15`` has no date, and a date on its
    own is an expiry rather than a timestamp -- which is why a lone date is not
    matched here.
    """
    tok = token.strip().strip("[]()")
    m = _STAMP.match(tok)
    if m:
        return m.group(1), m.group(2)
    if _TIME.match(tok):
        return None, tok.rstrip("zZ")
    return None


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
    #: ``(value, column)`` for every number left after the words were taken
    #: out.  The column is which comma-separated field it came from, which is
    #: what tells a strike column from the price beside it.
    numbers: list = field(default_factory=list)
    stamp_date: str | None = None
    stamp_time: str | None = None
    columns: int = 1
    label: str = ""
    notes: list = field(default_factory=list)


def _consume(line: str, state: _Line) -> None:
    """Pull every recognised token out of the line, leaving only the price."""
    # A label in square brackets is kept verbatim and taken out of the way.
    m = re.search(r"\[([^\]]*)\]", line)
    if m:
        inside = m.group(1).strip()
        stamp = _as_timestamp(inside)
        if stamp is not None:
            # '[09:15]' is a time somebody bracketed, not a label called 09:15.
            state.stamp_date, state.stamp_time = stamp[0] or state.stamp_date, stamp[1]
        else:
            state.label = inside
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

    # Tokens carry the comma column they came from.  Everything below works on
    # the token exactly as before; the column is only consulted at the end,
    # when what is left has to be sorted into a strike and a price.
    tokens: list[list] = []
    for column, chunk in enumerate(line.split(",")):
        tokens.extend([tok, column] for tok in chunk.split())
    columns = 1 + max((t[1] for t in tokens), default=0)

    rest: list[list] = []
    i = 0
    while i < len(tokens):
        tok, column = tokens[i]
        word = _squash(tok)
        nxt = _squash(tokens[i + 1][0]) if i + 1 < len(tokens) else ""

        # 25d, 25 delta, 10dRR, RR25
        md = _DELTA.match(word) or (_DELTA.match(word + nxt) if nxt in ("d", "delta", "dl") else None)
        if md is None:
            joined = re.match(r"^(\d+(?:\.\d+)?)d(rr|fly|bf|c|p|call|put)$", word)
            if joined:
                md = _DELTA.match(joined.group(1) + "d")
                tokens.insert(i + 1, [joined.group(2), column])
        if md:
            value = float(md.group(1))
            state.delta = value / 100.0 if value > 1.0 else value
            if _DELTA.match(word + nxt) and not _DELTA.match(word):
                i += 1
            i += 1
            continue

        # A timestamp, before anything else looks at it: a date on its own is
        # an expiry, and the same date followed by a time is the moment the
        # quote was given.  Reading the first as the second (or the reverse)
        # would move a quote to a tenor nobody asked for.
        stamp = _as_timestamp(tok)
        if stamp is not None:
            state.stamp_date = stamp[0] or state.stamp_date
            state.stamp_time = stamp[1]
            i += 1
            continue
        if _DATE.match(word) and i + 1 < len(tokens) and \
                _as_timestamp(tokens[i + 1][0]) is not None:
            state.stamp_date = word
            state.stamp_time = _as_timestamp(tokens[i + 1][0])[1]
            i += 2
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
            if i + 1 < len(tokens) and _NUMBER.match(_squash(tokens[i + 1][0])):
                state.strike = float(_squash(tokens[i + 1][0]))
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
            for back in (_squash(rest[-1][0]) if rest else "",
                         _squash(tokens[i - 1][0]) if i else ""):
                if re.fullmatch(r"[a-z]{3}", back):
                    state.over = back
                    if rest and _squash(rest[-1][0]) == back:
                        rest.pop()
                    break
            i += 1
            continue
        if re.fullmatch(r"[a-z]{3}", word) and i + 2 < len(tokens) and \
                _squash(tokens[i + 2][0]) == "over" and _squash(tokens[i + 1][0]) in _CALL + _PUT:
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
        rest.append([tok, column])
        i += 1

    # Whatever is left has to be the price, and possibly a strike in an earlier
    # column.  Joined and re-split per column: a spaced separator is a token of
    # its own ('0.20 - 0.28'), and by the rule above a price never straddles a
    # comma, so joining within a column loses nothing.
    by_column: dict[int, list[str]] = {}
    for tok, column in rest:
        by_column.setdefault(column, []).append(tok)
    for column in sorted(by_column):
        remainder = " ".join(by_column[column])
        parts = [w for chunk in _SEP.split(remainder) for w in chunk.split()]
        for p in parts:
            p = _squash(p)
            if _NUMBER.match(p):
                state.numbers.append((float(p), column))
            elif p:
                state.notes.append(f"ignored {p!r}")
    state.columns = columns


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
    # Whether the line named its instrument is remembered here and reported
    # further down, once the columns have been sorted out: a line that reads
    # as an at-the-money on the words alone can still turn out to carry a
    # strike column, and saying "read as atm" about a quote that came back as
    # an outright is worse than saying nothing.
    assumed = state.instrument is None
    if assumed:
        state.instrument = "atm" if state.delta is None and state.strike is None else "outright"
    if state.explicit_spread and len(state.expiries) == 2:
        instrument = "spread"
    else:
        instrument = state.instrument

    # -- which number is the price, and which is a strike ------------------
    # A comma is a column boundary and a price never straddles one, so with
    # columns the price is whatever is in the last column that has numbers and
    # anything earlier is a strike.  Without them the old rule stands: three
    # numbers on a line means the first is the strike.  This is the whole
    # difference between '3M, 7.75, 8.30' (a choice at 7.75) and '3M 7.75 8.30'
    # (a two-way at-the-money), which without the comma cannot be told apart.
    price_column = max((c for _, c in state.numbers), default=0)
    numbers = [v for v, c in state.numbers if c == price_column]
    early = [v for v, c in state.numbers if c != price_column]
    if early:
        if state.strike is not None or state.delta is not None:
            raise ValueError(
                f"the line already names a {'strike' if state.strike is not None else 'delta'} "
                f"and there {'is' if len(early) == 1 else 'are'} still "
                f"{', '.join(f'{n:g}' for n in early)} in an earlier column")
        if len(early) > 1:
            raise ValueError(
                f"{len(early)} numbers ({', '.join(f'{n:g}' for n in early)}) sit before the "
                f"price column; a strike column holds one strike")
        state.strike = early[0]
        instrument = state.instrument = "outright"
        notes.append(f"column {price_column} read as the price, so {state.strike:g} in the "
                     f"column before it is the strike")
    elif state.columns == 1 and len(numbers) == 3 and \
            state.strike is None and state.delta is None and \
            state.instrument in (None, "outright"):
        state.strike = numbers.pop(0)
        instrument = state.instrument = "outright"
        notes.append(f"leading number read as the strike ({state.strike:g})")
    elif instrument == "outright" and state.strike is None and state.delta is None:
        raise ValueError(
            "an outright needs a strike or a delta as well as a price; "
            + ", ".join(f"{n:g}" for n in numbers) + " were read as the price")
    if len(numbers) > 2:
        raise ValueError(f"{len(numbers)} numbers left after the tenor and the instrument "
                         f"({', '.join(f'{n:g}' for n in numbers)}); the line is ambiguous")
    if assumed:
        notes.append(f"no instrument word, read as {instrument}")

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
    if instrument == "outright" and state.is_call is None and state.strike is None:
        # A delta names two different strikes, one on each wing, so a bare
        # '25d' has to pick one.  It picks the call, which is what the pricing
        # screen's own strike box does with a bare '25d', and it says so --
        # write '25dp' or '-25d' for the put.  An *absolute* strike needs no
        # side at all: the volatility there is one number.
        state.is_call = True
        notes.append("a bare delta does not say which wing; read as the call, as the pricing "
                     "screen reads a bare '25d'. Write '25dp' or '-25d' for the put")
    if not state.expiries:
        raise ValueError("no tenor or expiry date on the line")
    if instrument == "spread" and len(state.expiries) != 2:
        raise ValueError("a spread needs exactly two tenors")
    if instrument != "spread" and len(state.expiries) > 1:
        raise ValueError(f"{len(state.expiries)} tenors on a line that is not a spread")

    stamp_text = " ".join(x for x in (state.stamp_date, state.stamp_time) if x)
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
        timestamp_text=stamp_text, notes=tuple(notes),
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


#: The day a run with no date in it is ordered on.  Never shown: the quotes
#: keep the text that was actually written, and a note says the run was timed
#: without a date.
_NOMINAL_DAY = "1970-01-01"


def instrument_key(q) -> tuple:
    """What makes two lines quotes of *the same thing*.

    Everything that would send the two to different points on the surface is
    in the key, so an update replaces its own quote and nothing else.  A market
    strangle and a smile butterfly at the same delta are different instruments
    and are deliberately not collapsed into one.
    """
    def ekey(x):
        return None if x is None else str(x).upper()

    return (q.instrument, ekey(q.expiry), ekey(q.expiry_far), q.leg,
            q.delta, q.strike, q.is_call, q.fly_kind)


#: The historical spelling, kept because everything inside this module reads
#: like the rule it states: two lines are the same quote when this matches.
_conflict_key = instrument_key


def _resolve_stamps(quotes: list[MarketQuote], notes: list[str]) -> list[MarketQuote]:
    """Turn the timestamp text on each line into something orderable.

    A time-only stamp takes the last date seen above it, which is how a run
    that gives the date once and then times is meant to read.  When no line
    gives a date at all the times are ordered on a nominal day and the run is
    told so: that ordering is wrong across midnight, and it is better to say
    which assumption was made than to have an 00:05 update lose to a 23:50
    quote in silence.
    """
    stamped = [q for q in quotes if q.timestamp_text]
    if not stamped:
        return quotes
    dated = any("-" in q.timestamp_text for q in stamped)
    out: list[MarketQuote] = []
    last_date: str | None = None
    inherited = 0
    for q in quotes:
        if not q.timestamp_text:
            out.append(q)
            continue
        text = q.timestamp_text
        if "-" in text:
            last_date = text.split()[0]
            when = text
        else:
            if last_date is not None:
                inherited += 1
            when = f"{last_date or _NOMINAL_DAY} {text}"
        try:
            resolved = parse_datetime(when)
        except (ValueError, TenorError) as exc:  # pragma: no cover - the regex is stricter
            out.append(MarketQuote(**{**vars(q), "notes": q.notes + (
                f"the timestamp {text!r} could not be read ({exc}), so this line is ordered "
                f"by where it was written",), "timestamp_text": ""}))
            continue
        out.append(MarketQuote(**{**vars(q), "timestamp": resolved}))
    if not dated:
        notes.append(
            f"{len(stamped)} line(s) are timed but no line gives a date, so the times are "
            f"ordered as one day; a run that crosses midnight will order wrongly")
    elif inherited:
        notes.append(f"{inherited} timed line(s) gave no date and took the last date above them")
    return out


def _resolve_conflicts(quotes: list[MarketQuote],
                       notes: list[str]) -> tuple[list[MarketQuote], list[MarketQuote]]:
    """Collapse repeated quotes of the same thing, latest first.

    A run is a conversation and the same tenor is quoted again when the market
    moves, so two lines for one thing are an update rather than two
    observations -- left alone, the older one would go into the fit beside the
    newer and pull it backwards.

    **A later timestamp wins.**  When the two cannot be compared on time -- one
    or both untimed, or the same time twice -- the later line wins, which is
    the only ordering an untimed line carries.  The loser is returned rather
    than dropped, with the line that beat it.

    The survivor keeps the *first* position, so an updated run reads in the
    order it was written rather than reshuffling itself as quotes arrive.
    """
    best: dict[tuple, int] = {}
    kept: list[MarketQuote | None] = []
    superseded: list[MarketQuote] = []
    by_time = 0
    for q in quotes:
        key = _conflict_key(q)
        at = best.get(key)
        if at is None:
            best[key] = len(kept)
            kept.append(q)
            continue
        prev = kept[at]
        if q.timestamp is not None and prev.timestamp is not None \
                and q.timestamp != prev.timestamp:
            newer, older = (q, prev) if q.timestamp > prev.timestamp else (prev, q)
            by_time += 1
        else:
            newer, older = q, prev          # later line, the only other ordering
        kept[at] = newer
        superseded.append(MarketQuote(**{**vars(older), "replaced_by": newer.line}))
    if superseded:
        notes.append(
            f"{len(superseded)} quote(s) were replaced by a later quote of the same thing"
            + (f", {by_time} of them on the timestamp and the rest on the order they were "
               f"written in" if by_time else " (on the order they were written in; none of "
               "the pairs carried two different timestamps)")
            + ": " + "; ".join(f"line {o.line} by line {o.replaced_by}" for o in superseded[:6])
            + (" ..." if len(superseded) > 6 else ""))
    return [q for q in kept if q is not None], superseded


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
    headers: list[int] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0]
        if not body.strip():
            continue
        if _is_header(body):
            headers.append(n)
            continue
        state = _Line()
        try:
            _consume(_norm(body), state)
            raw_quotes.append(_build(state, pair, fly_convention, n, raw))
        except (ValueError, TenorError) as exc:
            skipped.append((n, raw.strip(), str(exc)))

    unit, evidence = _decide_unit(raw_quotes, vol_unit)
    scale = 0.01 if unit == "percent" else 1.0
    scaled = [
        MarketQuote(**{**vars(q), "bid": q.bid * scale, "ask": q.ask * scale,
                       "strike": q.strike})
        for q in raw_quotes
    ]

    notes = [f"volatility unit: {unit} ({evidence})"]
    if headers:
        notes.append(f"line{'s' if len(headers) > 1 else ''} "
                     f"{', '.join(str(n) for n in headers)} read as a column header and "
                     f"passed over")
    # Timestamps first, then the conflicts they decide.  Both run on the whole
    # run rather than line by line, because both are questions about the run:
    # which day a bare time belongs to, and which of two quotes for one thing
    # is the live one.
    scaled = _resolve_stamps(scaled, notes)
    live, superseded = _resolve_conflicts(scaled, notes)
    quotes = tuple(live)
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
                     notes=tuple(notes), skipped=tuple(skipped),
                     superseded=tuple(superseded))


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


# ---------------------------------------------------------------------------
# the request: what is being asked for, with no price on it
# ---------------------------------------------------------------------------


def _build_request(state: _Line, pair: str | None, default_fly: str, line_no: int,
                   raw: str) -> QuoteRequest:
    """One request line, from the tokens :func:`_consume` left behind.

    The same validation :func:`_build` does, minus the price and plus a
    refusal in its place: whatever numbers are left have to be a strike, and
    anything else is a market that has been pasted into the wrong box.
    """
    notes = list(state.notes)
    assumed = state.instrument is None
    if assumed:
        state.instrument = "atm" if state.delta is None and state.strike is None else "outright"
    if state.explicit_spread and len(state.expiries) == 2:
        instrument = "spread"
    else:
        instrument = state.instrument

    if state.numbers:
        values = [v for v, _ in state.numbers]
        # One number, on a line that has not already said what it is struck
        # at, is a strike -- '6M 1.1000 call'.  Anything else is a price, and a
        # price in this box is a market that belongs in the one above it.
        # Read as a strike it would quote a level nobody asked about, which is
        # the silent-wrong-answer this project exists to remove.
        if len(values) == 1 and state.strike is None and state.delta is None \
                and (assumed or state.instrument == "outright"):
            state.strike = values[0]
            instrument = state.instrument = "outright"
            notes.append(f"the only number on the line read as the strike ({state.strike:g})")
        else:
            raise ValueError(
                "this box holds what is being asked for, not what it is worth: "
                + ", ".join(f"{v:g}" for v in values)
                + " reads as a price. Paste the market in the market box; write here the "
                  "instrument alone, as in '1M ATM in 100mm' or '3M 25d RR'")

    if instrument in ("rr", "fly") and state.delta is None:
        raise ValueError(f"a {instrument} needs a delta, e.g. '25d'")
    if instrument == "outright" and state.strike is None and state.delta is None:
        raise ValueError("an outright needs a strike or a delta")
    if instrument == "outright" and state.is_call is None and state.strike is None:
        state.is_call = True
        notes.append("a bare delta does not say which wing; read as the call, as the pricing "
                     "screen reads a bare '25d'. Write '25dp' or '-25d' for the put")
    if not state.expiries:
        raise ValueError("no tenor or expiry date on the line")
    if instrument == "spread" and len(state.expiries) != 2:
        raise ValueError("a spread needs exactly two tenors")
    if instrument != "spread" and len(state.expiries) > 1:
        raise ValueError(f"{len(state.expiries)} tenors on a line that is not a spread")

    sign = 1.0
    if instrument == "rr" or (instrument == "spread" and state.instrument == "rr"):
        sign = _resolve_sign(state, pair, notes)
        if sign < 0:
            notes.append(
                f"{(state.over or '').upper()} over on {pair or '?'} is the other side of the "
                f"book's risk reversal, so every number on this row is negated: it is quoted "
                f"the way it was asked for, not the way the book marks it")

    near, far = state.expiries[0], (state.expiries[1] if len(state.expiries) > 1 else None)
    if instrument == "spread":
        if state.literal_order:
            notes.append(f"'{near}-{far}' read literally: {near} less {far}")
            near, far = far, near
        else:
            notes.append(f"'{near}/{far}' read as the calendar convention: {far} less {near}")

    return QuoteRequest(
        instrument=instrument, expiry=near, expiry_far=far,
        leg=(state.instrument if instrument == "spread" else None),
        delta=state.delta, strike=state.strike, is_call=state.is_call,
        fly_kind=state.fly_kind or (default_fly if instrument == "fly" or
                                    state.instrument == "fly" else None),
        size=state.size, size_basis=state.size_basis, label=state.label,
        line=line_no, raw=raw.strip(), sign=sign, direction=state.over,
        notes=tuple(notes),
    )


def parse_requests(text: str, *, pair: str | None = None,
                   fly_convention: str = "market") -> ParsedRequests:
    """Read a list of instruments to be quoted.  No prices, and none accepted.

    Nothing is dropped quietly, exactly as :func:`parse_quotes` drops nothing:
    a line that cannot be used comes back in ``skipped`` with the reason, and
    every inference is on the request itself.

    There is no volatility unit to decide here, because there is no volatility
    on the page: a strike is an absolute price and a delta is a fraction.  That
    is one fewer thing to get wrong than the market box has, and it is why the
    two are read by two functions rather than one with a flag on it.
    """
    if fly_convention not in FLY_CONVENTIONS:
        raise ValueError(
            f"unknown butterfly convention {fly_convention!r}; expected one of {FLY_CONVENTIONS}")

    requests: list[QuoteRequest] = []
    skipped: list[tuple[int, str, str]] = []
    headers: list[int] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0]
        if not body.strip():
            continue
        if _is_header(body):
            headers.append(n)
            continue
        state = _Line()
        try:
            _consume(_norm(body), state)
            requests.append(_build_request(state, pair, fly_convention, n, raw))
        except (ValueError, TenorError) as exc:
            skipped.append((n, raw.strip(), str(exc)))

    notes: list[str] = []
    if headers:
        notes.append(f"line{'s' if len(headers) > 1 else ''} "
                     f"{', '.join(str(n) for n in headers)} read as a column header and "
                     f"passed over")
    unsigned = [q for q in requests
                if (q.instrument == "rr" or (q.instrument == "spread" and q.leg == "rr"))
                and q.direction is None]
    if unsigned:
        notes.append(
            f"{len(unsigned)} risk reversal(s) carried no direction word and are quoted in the "
            f"book's own convention: a base-currency call over is positive. Write "
            f"'{(pair or 'EURUSD')[:3].upper()} call over' or "
            f"'{(pair or 'EURUSD')[3:6].upper()} call over' to be quoted the other way round")
    flies = [q for q in requests if q.instrument == "fly"]
    if flies:
        notes.append(
            f"{len(flies)} butterfly request(s), quoted as the {fly_convention} convention"
            + (" (the market strangle, which is what the workbook marks)"
               if fly_convention == "market" else " (the smile butterfly)"))
    # Duplicates are left alone.  A request box is a list of things to price
    # and the same instrument asked for twice, in two sizes, is two questions;
    # the market box's later-wins rule is about a conversation, and this is not
    # one.
    return ParsedRequests(requests=tuple(requests), notes=tuple(notes), skipped=tuple(skipped))
