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
screen takes: ``ATM``, an absolute strike (``7.75``, and ``7.75c`` or
``7.75p`` with the side glued on), or a delta (``25d``, ``25dp``, ``-25d``).
The two shapes are read by one parser rather than two, because a run that
mixes them -- and they do -- must not depend on which line came first.  A
strike and a delta on one line is a strike quote: the strike names the option
exactly and the delta only names it through the marks, so the delta is
dropped and the line says so.

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
from datetime import date

from .timeutil import TenorError, normalise_tenor, parse_datetime, parse_tenor

INSTRUMENTS = ("atm", "rr", "fly", "outright", "spread", "structure")
QUOTE_KINDS = ("vol", "premium")
PREMIUM_UNITS = ("pips", "pct", "price")
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


def _leg_text(kind, expiry, delta, strike, is_call) -> str:
    """One leg's name: the tenor, then what it is."""
    base = _expiry_label(expiry)
    if kind == "atm":
        return f"{base} ATM"
    if kind in ("rr", "fly"):
        return f"{base} {int(round((delta or 0) * 100))}d {kind.upper()}"
    if strike is not None:
        # A volatility at an absolute strike is one number whichever side
        # it is quoted from, so a strike quote is allowed to leave the side
        # out and must not be described as a put by default.
        if is_call is None:
            return f"{base} {strike:g}"
        return f"{base} {strike:g} {'call' if is_call else 'put'}"
    side = "call" if is_call else "put"
    return f"{base} {int(round((delta or 0) * 100))}d {side}"


_PREMIUM_LABEL = {"pips": "premium in pips", "pct": "premium in % of base",
                  "price": "premium"}


@dataclass(frozen=True)
class QuoteLeg:
    """One leg of a structure, with its signed weight.

    A structure is anything the five plain instruments and the two-tenor
    calendar spread do not cover: a strike spread at one tenor, a calendar
    across three tenors, a call against a put.  Its value is the weighted sum
    of its legs, ``sum(weight * value(leg))``, so ``+1`` is a leg bought and
    ``-1`` a leg sold, and ``-2`` the middle of a fly.  Every leg is a whole
    instrument on its own: a tenor and what it is at that tenor.
    """

    kind: str                            # atm / rr / fly / outright
    expiry: object
    weight: float = 1.0                  # in the book's convention: what the model sums
    delta: float | None = None
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None
    #: The sign and size as written -- ``sell 3M 25d RR JPY call over`` is
    #: ``-1`` here and ``+1`` in ``weight`` on USDJPY, because the direction
    #: word is folded into the weight.  What is shown is what was written.
    quoted_weight: float | None = None
    direction: str | None = None

    def describe(self) -> str:
        w = self.weight if self.quoted_weight is None else self.quoted_weight
        sign = "+" if w >= 0 else "-"
        size = "" if abs(abs(w) - 1.0) < 1e-12 else f"{abs(w):g}x "
        text = sign + size + _leg_text(self.kind, self.expiry, self.delta, self.strike,
                                       self.is_call)
        if self.direction and self.direction not in ("call", "put"):
            text += f" ({self.direction.upper()} call over)"
        return text


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
        if self.instrument == "structure":
            text = " ".join(leg.describe() for leg in self.legs)
        elif self.instrument == "spread":
            leg = {"atm": "ATM", "rr": f"{int(round((self.delta or 0) * 100))}d RR",
                   "fly": f"{int(round((self.delta or 0) * 100))}d fly"}.get(
                       self.leg or "atm", (self.leg or "").upper())
            text = f"{_expiry_label(self.expiry_far)} less {base} {leg}".strip()
        else:
            text = _leg_text(self.instrument, self.expiry, self.delta, self.strike,
                             self.is_call)
        if getattr(self, "quote_kind", "vol") == "premium":
            text += f" ({_PREMIUM_LABEL.get(self.premium_unit, 'premium')})"
        return text

    def expiries(self) -> tuple:
        """Every expiry the instrument names, each once, in leg order."""
        out: list = []
        for x in (*(leg.expiry for leg in self.legs), self.expiry, self.expiry_far):
            if x is not None and not any(str(x) == str(y) for y in out):
                out.append(x)
        return tuple(out)


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
    #: The legs of a ``structure``, and nothing for any other instrument.
    legs: tuple = ()
    #: ``vol`` or ``premium``.  A premium quote's ``bid`` and ``ask`` are in
    #: ``premium_unit`` -- pips of the term currency, per cent of the base
    #: notional, or a price in the term currency per unit of base -- and are
    #: never scaled by the paste's volatility unit.
    quote_kind: str = "vol"
    premium_unit: str | None = None
    #: The pair the line (or the heading above it) named, if any.
    pair: str | None = None
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
    #: Lines that quote another pair: read, understood, and left out on
    #: purpose, with the pair they named.  Not in ``skipped``, because nothing
    #: was wrong with them.
    ignored: tuple[tuple[int, str, str], ...] = ()

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
    legs: tuple = ()
    quote_kind: str = "vol"
    premium_unit: str | None = None
    pair: str | None = None
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
    ignored: tuple[tuple[int, str, str], ...] = ()   # lines naming another pair


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
_SPREAD = ("spread", "spreads", "cal", "calendar")
#: A word that separates the legs of a structure.  ``vs`` used to be a
#: synonym of ``spread``; it is now a boundary, and what is on each side of it
#: is read as a leg of its own.
_LEG_SEP = ("vs", "versus", "against", "v")
_BUY = ("buy", "buys", "bought", "long", "own", "+")
_SELL = ("sell", "sells", "sold", "short", "-")
_WEIGHT = re.compile(r"^(?:(\d+(?:\.\d+)?)[x*]|[x*](\d+(?:\.\d+)?))$")
#: Words that say the price on the line is a premium and not a volatility.
#: ``live`` is the desk's word for an option dealt without its delta hedge,
#: which is always dealt on a premium.
_PREMIUM = ("prem", "premium", "premia", "live", "cash")
_PIPS = ("pips", "pip")
_PCT = ("pct", "percent")
_CCY = frozenset((
    "usd", "eur", "jpy", "gbp", "chf", "aud", "nzd", "cad", "sek", "nok", "dkk", "cnh",
    "cny", "hkd", "sgd", "krw", "twd", "inr", "idr", "myr", "php", "thb", "vnd", "mxn",
    "brl", "clp", "cop", "pen", "ars", "zar", "try", "rub", "pln", "huf", "czk", "ils",
    "ron", "isk", "sar", "aed", "kwd", "qar", "egp", "ngn", "kes", "xau", "xag",
))
_PAIR = re.compile(r"^([a-z]{3})[/\-]?([a-z]{3}):?$")
#: A whole line that is nothing but a pair: a heading over the lines below it.
_PAIR_LINE = re.compile(r"^([a-z]{3})\s*[/\-]?\s*([a-z]{3})\s*:?$")
_STRADDLE = ("straddle", "straddles", "strad", "dn", "deltaneutral")
_MID = ("mid", "mids", "choice", "chc")
_DROP = ("vol", "vols", "volatility", "in", "on", "of", "the", "for", "at", "px", "prices",
         "quote", "quoted", "market", "mkt", "size", "please", "pls", "level", "abt", "around",
         "bid", "offer", "ofr", "ask", "and", "with", "a", "an", "to", "is", "are", "we", "i",
         "show", "showing", "shown", "see", "seeing", "have", "has", "there", "here", "now",
         "pay", "paying", "paid", "expiry", "exp", "maturity", "tenor", "strikes", "option",
         "options", "opt", "contract", "contracts", "twoway", "two", "way", "either", "each")

#: The unit of a tenor, in every spelling ``timeutil.parse_tenor`` reads: a
#: run says "1wk" and "3mth" as readily as "1W" and "3M".  Longest first, so
#: "wks" is not read as "w" with a tail.
_UNIT_WORD = (r"(?:days|day|d|weeks|week|wks|wk|w"
              r"|months|month|mths|mth|mos|mon|mo|m|years|year|yrs|yr|y)")
_TENOR = re.compile(r"^(\d+(?:\.\d+)?)" + _UNIT_WORD + r"$")
# The short-date codes, **only in their slashed spelling**.  ``timeutil``
# reads the bare ones too, and this deliberately does not: a broker run is
# English, and "on" and "sn" are words a sentence has in it -- "6M 1.10 call
# vs 1.15 call 0.35/0.55 on the offer" would otherwise acquire an overnight
# leg.  Nobody writes an overnight without the slash on a run sheet.
_SHORT_DATES = {"O/N", "T/N", "S/N", "S/W"}
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
#: A strike with its side glued on: ``7.77c``, ``1.1000p``, ``7.77call``.  A
#: run writes the effective strike this way as readily as with a space, and
#: without this it is neither a number nor a word and lands in "ignored" --
#: leaving a line that named its strike to be quoted off a delta instead.
#: The delta branches above run first, so ``25d`` and ``25dc`` are untouched.
_STRIKE_SIDE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)(c|calls?|p|puts?)$")
#: A date in one token that is not ISO: 30sep26, 30-Sep-2026, 2026/09/30,
#: 09/30/2026.  Gated by shape so that ``parse_datetime`` is only asked about
#: something that could be a date, and a number is never one.
#: The year-less shapes are here too -- ``06Nov``, ``Nov06`` -- and are
#: resolved forward from the caller's date; with no date behind them they
#: come back as "not an expiry" rather than as a guess.
_DATEISH = re.compile(
    r"^(?:\d{1,2}[-./]?[a-z]{3,9}[-./]?\d{2,4}|\d{4}[-./]\d{1,2}[-./]\d{1,2}"
    r"|\d{1,2}/\d{1,2}/\d{4}|[a-z]{3,9}[-./ ]?\d{1,2}[-./,]?\d{4}"
    r"|\d{1,2}[-./]?[a-z]{3,9}|[a-z]{3,9}[-./]?\d{1,2})$")

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


def _as_expiry(token: str, today=None):
    """A tenor string or a date, or ``None`` if it is neither.

    Takes the token as written: ``30-Sep-26`` and ``2026/09/30`` carry
    punctuation that :func:`_squash` would remove, and are dates all the same.

    ``today`` is the date a year-less token (``06Nov``) is resolved forward
    from.  Without one the token is not an expiry rather than a guess, which
    is what a run read with no clock behind it should say.
    """
    tok = token.strip().strip("[]()").rstrip(",;:")
    word = _squash(tok)
    if tok.upper() in _SHORT_DATES:
        return tok.upper()
    if _DATE.match(word):
        return parse_datetime(word)
    if _TENOR.match(word):
        try:
            parse_tenor(word)
        except TenorError:
            return None
        return normalise_tenor(word)
    if _DATEISH.match(tok) and not _NUMBER.match(tok) and not _SIZE.match(word):
        try:
            return parse_datetime(tok, today=today)
        except (ValueError, TenorError):
            return None
    return None


def _pair_of(token: str) -> str | None:
    """``EURUSD`` for a token that names a currency pair, else ``None``."""
    m = _PAIR.match(token.strip().strip("[]()"))
    if m and m.group(1) in _CCY and m.group(2) in _CCY:
        return (m.group(1) + m.group(2)).upper()
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
    #: The legs of a structure, each consumed on its own, when the line was
    #: split on ``vs``.  Empty for a plain line.
    legs: list = field(default_factory=list)
    sign: float | None = None            # +1 bought, -1 sold, None unsaid
    weight: float = 1.0                  # '2x'
    quote_kind: str = "vol"
    pips_seen: bool = False
    pct_seen: bool = False
    premium_ccy: str | None = None       # the currency word after a premium
    pair: str | None = None
    #: ``(value, column)`` for every number left after the words were taken
    #: out.  The column is which comma-separated field it came from, which is
    #: what tells a strike column from the price beside it.
    numbers: list = field(default_factory=list)
    stamp_date: str | None = None
    stamp_time: str | None = None
    columns: int = 1
    label: str = ""
    notes: list = field(default_factory=list)
    #: The date a year-less expiry ("06Nov") is resolved forward from.  It
    #: comes from the caller's clock -- a run read twice must read the same
    #: way -- and with none given such a token is simply not an expiry.
    today: date | None = None


def _consume(line: str, state: _Line) -> None:
    """Pull every recognised token out of the line, leaving only the price.

    A line with ``vs`` in it is split there and each side is read as a leg of
    its own; what a leg does not say is then borrowed from the legs that did
    (:func:`_merge_legs`), so ``1M vs 3M 25d RR`` reads as two risk reversals
    and ``6M 1.10 call vs 1.15 call`` as two options at one tenor.
    """
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
        a, b = _as_expiry(mt.group(1), state.today), _as_expiry(mt.group(2), state.today)
        if a is None or b is None:
            return mt.group(0)
        state.expiries.extend([a, b])
        state.explicit_spread = True
        state.literal_order = mt.group(0).count("-") == 1
        return " "

    # Spaces are allowed around '/' only: '1M - 25d' is a tenor and a put's
    # delta with a spaced separator between them, not a 1M/25-day spread.
    line = re.sub(r"(\d+(?:\.\d+)?" + _UNIT_WORD + r")(?:\s*/\s*|-|x)"
                  r"(\d+(?:\.\d+)?" + _UNIT_WORD + r")(?![a-z0-9])",
                  take_pair, line)

    # Tokens carry the comma column they came from.  Everything below works on
    # the token exactly as before; the column is only consulted at the end,
    # when what is left has to be sorted into a strike and a price.
    tokens: list[list] = []
    for column, chunk in enumerate(line.split(",")):
        tokens.extend([tok, column] for tok in chunk.split())
    columns = 1 + max((t[1] for t in tokens), default=0)

    # The legs.  'vs' is a boundary and not a word, so the tokens on each side
    # of it are consumed separately and never see each other.
    segments: list[list[list]] = [[]]
    for tok, column in tokens:
        word = _squash(tok)
        if word in _LEG_SEP:
            segments.append([])
            state.explicit_spread = True
        elif word in _BUY + _SELL and word not in ("+", "-") and segments[-1]:
            # 'buy 1M atm sell 3M atm': the second word starts the second leg.
            segments.append([[tok, column]])
            state.explicit_spread = True
        else:
            segments[-1].append([tok, column])
    if len(segments) == 1:
        _consume_tokens(tokens, columns, state)
        return
    if state.expiries:
        raise ValueError(
            "the line writes one spread as '1M/3M' and another with 'vs'; write the legs one "
            "way, as '1M vs 3M' or as '1M/3M'")
    for seg in segments:
        if not seg:
            raise ValueError("'vs' with nothing on one side of it; every leg needs a tenor "
                             "or an instrument")
        leg = _Line(today=state.today)
        _consume_tokens(seg, columns, leg)
        state.legs.append(leg)
    _merge_legs(state)


def _consume_tokens(tokens: list[list], columns: int, state: _Line) -> None:
    """Read one leg's (or one plain line's) tokens into ``state``."""
    rest: list[list] = []
    i = 0
    first = True
    while i < len(tokens):
        tok, column = tokens[i]
        raw_tok = tok.strip()
        # '1.25%' is a number that said it is a per cent.  Remembered, because
        # on a premium line that is the unit; on a volatility line it is noise.
        if "%" in raw_tok:
            state.pct_seen = True
            tok = raw_tok.replace("%", "")
        word = _squash(tok)
        nxt = _squash(tokens[i + 1][0]) if i + 1 < len(tokens) else ""
        prev_raw = tokens[i - 1][0].replace("%", "") if i else ""
        at_start, first = first, False

        # The pair the line is about.  'eur/usd', 'EURUSD', 'usdjpy:'.
        named = _pair_of(raw_tok)
        if named is not None:
            state.pair = named
            i += 1
            continue

        # A leg's sign and weight.  A bare '+' or '-' is a sign only at the
        # start of a leg: in the middle of one it is the spaced separator of
        # '0.20 - 0.28', which belongs to the price.
        if word in _BUY or word in _SELL:
            if word in ("+", "-") and not at_start:
                rest.append([tok, column])
                i += 1
                continue
            state.sign = 1.0 if word in _BUY else -1.0
            i += 1
            continue
        mw = _WEIGHT.match(word)
        if mw:
            state.weight = float(mw.group(1) or mw.group(2))
            i += 1
            continue
        # '-2x' and '-1m': a sign glued to what follows it.
        if word[:1] in "+-" and len(word) > 1 and at_start and (
                _WEIGHT.match(word[1:]) or _as_expiry(word[1:], state.today) is not None
                or word[1:] in _ATM) and not _DELTA.match(word[1:]):
            state.sign = 1.0 if word[0] == "+" else -1.0
            tokens[i] = [tok.lstrip("+-"), column]
            first = True
            continue

        # -25d is the put's delta, +25d the call's.
        if word[:1] in "+-" and _DELTA.match(word[1:]):
            md = _DELTA.match(word[1:])
            value = float(md.group(1))
            state.delta = value / 100.0 if value > 1.0 else value
            if state.instrument in (None, "outright"):
                state.instrument = "outright"
                state.is_call = word[0] == "+"
            i += 1
            continue
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

        exp = _as_expiry(tok, state.today)
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

        # '7.77c' -- the strike and the side in one token.  It is a strike
        # like any other, so by _settle_side it beats a delta on the same
        # line: the strike names the option exactly, the delta only through
        # the marks.
        mk = _STRIKE_SIDE.match(word)
        if mk:
            value = float(mk.group(1))
            if state.strike is not None and state.strike != value:
                raise ValueError(f"the line names a strike twice "
                                 f"({state.strike:g} and {value:g})")
            state.strike = value
            if state.instrument in (None, "outright"):
                state.instrument = "outright"
                state.is_call = mk.group(2)[0] == "c"
            i += 1
            continue

        if _STRIKE_WORD.match(word):
            if i + 1 < len(tokens) and _NUMBER.match(_squash(tokens[i + 1][0])):
                state.strike = float(_squash(tokens[i + 1][0]))
                i += 2
                continue
            i += 1
            continue

        # The price is a premium, not a volatility.
        if word in _PREMIUM:
            state.quote_kind = "premium"
            if word == "live":
                state.notes.append("'live' read as an option dealt without its delta hedge, "
                                   "so the price is a premium")
            i += 1
            continue
        if word in _PIPS:
            state.quote_kind = "premium"
            state.pips_seen = True
            i += 1
            continue
        if word in _PCT:
            state.pct_seen = True
            i += 1
            continue

        if word in _ATM or word in _STRADDLE:
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
        # A currency straight after a number is the currency the price is in,
        # which makes the price a premium: '0.0125/0.0135 usd'.  Only straight
        # after a number, so 'in 100mm eur' stays the size's currency.
        if word in _CCY and nxt != "over" and prev_raw and \
                _NUMBER.match(_squash(_SEP.split(prev_raw)[-1].strip() or "x")):
            state.quote_kind = "premium"
            state.premium_ccy = word
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


def _merge_legs(state: _Line) -> None:
    """Lift what belongs to the line off its legs, and fill each leg's gaps.

    The size, the stamp, the label, the pair and whether the price is a
    premium are properties of the line and are taken from whichever leg said
    them.  A leg that did not say its tenor takes the tenor of the leg before
    it (or after it, for the first); a leg that did not say what it is takes
    the instrument and the delta of the first leg that did.  Every borrowing
    is a note on the line, so a leg that was filled in and one that was
    written out do not read the same.
    """
    legs = state.legs
    for leg in legs:
        if leg.size is not None:
            state.size, state.size_basis = leg.size, leg.size_basis
        elif leg.size_basis != "unspecified":
            state.size_basis = leg.size_basis
        state.stamp_date = leg.stamp_date or state.stamp_date
        state.stamp_time = leg.stamp_time or state.stamp_time
        state.label = leg.label or state.label
        state.pair = leg.pair or state.pair
        if leg.quote_kind == "premium":
            state.quote_kind = "premium"
        state.pips_seen = state.pips_seen or leg.pips_seen
        state.pct_seen = state.pct_seen or leg.pct_seen
        state.premium_ccy = leg.premium_ccy or state.premium_ccy
        state.notes.extend(leg.notes)
        leg.notes = []
    for j, leg in enumerate(legs):
        if not leg.expiries:
            src = next((l for l in reversed(legs[:j]) if l.expiries), None) \
                or next((l for l in legs[j + 1:] if l.expiries), None)
            if src is None:
                raise ValueError("no tenor or expiry date on the line")
            leg.expiries = list(src.expiries)
            state.notes.append(f"leg {j + 1} gave no tenor and took {_expiry_label(src.expiries[-1])} "
                               f"from the leg beside it")
        said = leg.instrument is not None or leg.delta is not None or leg.strike is not None \
            or bool(leg.numbers)
        if not said:
            src = next((l for l in legs if l.instrument is not None or l.delta is not None), None)
            if src is not None:
                leg.instrument, leg.delta, leg.fly_kind = src.instrument, src.delta, src.fly_kind
                leg.is_call = src.is_call
                state.notes.append(f"leg {j + 1} said only its tenor and was read as the same "
                                   f"instrument as the leg that said one")
        elif leg.delta is None and leg.strike is None and not leg.numbers \
                and leg.instrument in ("rr", "fly", "outright"):
            src = next((l for l in legs if l.delta is not None and l.instrument == leg.instrument),
                       None)
            if src is not None:
                leg.delta = src.delta
                state.notes.append(f"leg {j + 1} gave no delta and took {src.delta * 100:g}d "
                                   f"from another leg")
        if leg.instrument == "fly" and leg.fly_kind is None:
            leg.fly_kind = next((l.fly_kind for l in legs if l.fly_kind), None)


def _settle_expiries(state: _Line, notes: list[str], *, spread_ok: bool = True) -> None:
    """A date beats a tenor; more than one of a kind is refused.

    ``1M 30sep26 ATM`` is a tenor and the date it stands for, and only the
    date is exact -- the tenor would be rolled through the calendar and could
    land a day off the one written down.  The date is kept and the line says
    the tenor was passed over.  Two tenors on a line that is not a spread stay
    a refusal, because nothing on the line says which one is meant.
    """
    if spread_ok and state.explicit_spread and len(state.expiries) == 2:
        return
    if len(state.expiries) <= 1:
        return
    dates = [e for e in state.expiries if not isinstance(e, str)]
    tenors = [e for e in state.expiries if isinstance(e, str)]
    if dates and tenors and len(dates) == 1:
        notes.append(f"both a tenor ({', '.join(tenors)}) and a date "
                     f"({_expiry_label(dates[0])}) are on the line; the date is used")
        state.expiries = dates


def _settle_side(state: _Line, notes: list[str]) -> None:
    """Which of strike, delta and side name the option, and which are noise.

    A strike beats a delta: both name one option, and the strike names it
    exactly while the delta names it through the marks, so a line with both is
    a strike quote and says the delta was dropped.  A call and a put at one
    absolute strike are one volatility, so on a volatility quote the side is
    dropped too -- and the two lines become one quote of one thing.  On a
    **premium** the side is the whole difference between two prices, and is
    required, as is the strike: a premium is dealt on a strike.
    """
    if state.instrument != "outright":
        return
    if state.strike is not None and state.delta is not None:
        notes.append(f"both a strike ({state.strike:g}) and a delta "
                     f"({state.delta * 100:g}d) name the option; the strike is used and the "
                     f"delta is dropped")
        state.delta = None
    if state.strike is None and state.delta is None:
        raise ValueError("an outright needs a strike or a delta")
    if state.quote_kind == "premium":
        if state.strike is None:
            raise ValueError("a premium is dealt on a strike, so the line needs one; a delta "
                             "only names a strike through the marks")
        if state.is_call is None:
            raise ValueError("a premium needs the side: a call and a put at one strike are "
                             "two different prices. Write 'call' or 'put'")
        return
    if state.strike is not None:
        if state.is_call is not None:
            notes.append("a volatility at an absolute strike is one number whether the option "
                         "is the call or the put, so the side is dropped; it matters with a "
                         "delta, or on a premium")
            state.is_call = None
    elif state.is_call is None:
        # A delta names two different strikes, one on each wing, so a bare
        # '25d' has to pick one.  It picks the call, which is what the pricing
        # screen's own strike box does with a bare '25d', and it says so --
        # write '25dp' or '-25d' for the put.
        state.is_call = True
        notes.append("a bare delta does not say which wing; read as the call, as the pricing "
                     "screen reads a bare '25d'. Write '25dp' or '-25d' for the put")


def _premium_unit(state: _Line, pair: str | None, notes: list[str]) -> tuple[str | None, float]:
    """The unit a premium was written in, and a factor to bring it to that unit.

    ``pips`` when the line said pips; per cent of the base notional when it
    said ``%``; otherwise a price in the term currency per unit of base.  A
    currency word after the price decides between the last two -- the base
    currency is a fraction of the base notional and is turned into a per cent
    here, said on the line -- and a currency that is neither leg is refused.
    """
    if state.quote_kind != "premium":
        return None, 1.0
    if state.pips_seen:
        return "pips", 1.0
    ccy = state.premium_ccy
    if ccy is not None and pair and len(pair) >= 6:
        base, term = pair[:3].lower(), pair[3:6].lower()
        if ccy == term:
            return "price", 1.0
        if ccy == base:
            if state.pct_seen:
                return "pct", 1.0
            notes.append(f"a premium in {ccy.upper()} is a fraction of the base notional; "
                         f"read as a per cent of it")
            return "pct", 100.0
        raise ValueError(f"the premium is in {ccy.upper()}, which is not a leg of "
                         f"{pair.upper()}")
    if ccy is not None:
        raise ValueError(f"the premium is in {ccy.upper()} and no pair was given to say "
                         f"which leg that is")
    if state.pct_seen:
        return "pct", 1.0
    notes.append("a premium with no unit is read as a price in the term currency per unit of "
                 "base; write 'pips' or '%' to say otherwise")
    return "price", 1.0


def _price_numbers(state: _Line, notes: list[str], *, assumed: bool = False) -> list[float]:
    """Which numbers are the price, and which is a strike.

    A comma is a column boundary and a price never straddles one, so with
    columns the price is whatever is in the last column that has numbers and
    anything earlier is a strike.  Without them the old rule stands: three
    numbers on a line means the first is the strike.  This is the whole
    difference between '3M, 7.75, 8.30' (a choice at 7.75) and '3M 7.75 8.30'
    (a two-way at-the-money), which without the comma cannot be told apart.
    """
    price_column = max((c for _, c in state.numbers), default=0)
    numbers = [v for v, c in state.numbers if c == price_column]
    early = [v for v, c in state.numbers if c != price_column]
    if early:
        if state.strike is not None:
            raise ValueError(
                f"the line already names a strike and there {'is' if len(early) == 1 else 'are'} "
                f"still {', '.join(f'{n:g}' for n in early)} in an earlier column")
        if len(early) > 1:
            raise ValueError(
                f"{len(early)} numbers ({', '.join(f'{n:g}' for n in early)}) sit before the "
                f"price column; a strike column holds one strike")
        state.strike = early[0]
        state.instrument = "outright"
        notes.append(f"column {price_column} read as the price, so {state.strike:g} in the "
                     f"column before it is the strike")
    elif state.columns == 1 and len(numbers) == 3 and state.strike is None and \
            (assumed or state.instrument in (None, "outright")):
        state.strike = numbers.pop(0)
        state.instrument = "outright"
        notes.append(f"leading number read as the strike ({state.strike:g})")
    if len(numbers) > 2:
        raise ValueError(f"{len(numbers)} numbers left after the tenor and the instrument "
                         f"({', '.join(f'{n:g}' for n in numbers)}); the line is ambiguous")
    return numbers


def _leg_strike(leg: _Line, j: int) -> None:
    """A leg before the price carries at most its own strike."""
    vals = [v for v, _ in leg.numbers]
    leg.numbers = []
    if len(vals) > 1:
        raise ValueError(f"leg {j + 1} carries {len(vals)} numbers "
                         f"({', '.join(f'{v:g}' for v in vals)}); a leg before the price "
                         f"carries at most its strike, and the price goes last")
    if vals:
        if leg.strike is not None:
            raise ValueError(f"leg {j + 1} names a strike twice ({leg.strike:g} and {vals[0]:g})")
        leg.strike = vals[0]
        leg.instrument = "outright"


def _finish_leg(leg: _Line, default_fly: str, notes: list[str], quote_kind: str) -> None:
    """The validation every leg gets, whichever builder is asking."""
    if leg.instrument is None:
        leg.instrument = "atm" if leg.delta is None and leg.strike is None else "outright"
    if leg.instrument in ("rr", "fly") and leg.delta is None:
        raise ValueError(f"a {leg.instrument} needs a delta, e.g. '25d'")
    leg.quote_kind = quote_kind
    _settle_side(leg, notes)
    if leg.instrument == "fly" and leg.fly_kind is None:
        leg.fly_kind = default_fly
    if not leg.expiries:
        raise ValueError("no tenor or expiry date on the line")
    _settle_expiries(leg, notes, spread_ok=False)
    if len(leg.expiries) > 1:
        raise ValueError(f"{len(leg.expiries)} tenors on one leg")


def _collapse_legs(state: _Line, notes: list[str]) -> bool:
    """Two legs that are one instrument at two tenors are the calendar spread
    the tool has always read -- '1M vs 3M ATM' is '1M/3M ATM spread' -- and are
    folded back into the plain line so they are built, keyed and priced exactly
    as before.  Anything else is a structure."""
    legs = state.legs
    for j, leg in enumerate(legs[:-1]):
        _leg_strike(leg, j)
    if len(legs) != 2:
        return False
    a, b = legs
    if any(l.strike is not None or l.sign is not None or l.weight != 1.0 for l in legs):
        return False
    if len(b.numbers) > 2 or len({c for _, c in b.numbers}) > 1:
        return False
    for leg in legs:
        if leg.instrument is None:
            leg.instrument = "atm" if leg.delta is None else "outright"
    if a.instrument != b.instrument or a.delta != b.delta or a.instrument == "outright":
        return False
    if (a.fly_kind or b.fly_kind) and a.fly_kind != b.fly_kind and a.fly_kind and b.fly_kind:
        return False
    for leg in legs:
        _settle_expiries(leg, notes, spread_ok=False)
        if len(leg.expiries) != 1:
            return False
    if str(a.expiries[0]) == str(b.expiries[0]):
        return False
    state.instrument = a.instrument
    state.delta = a.delta
    state.fly_kind = a.fly_kind or b.fly_kind
    state.is_call = a.is_call if a.is_call is not None else b.is_call
    state.over = a.over or b.over
    state.expiries = [a.expiries[0], b.expiries[0]]
    state.numbers = list(b.numbers)
    state.columns = b.columns
    state.explicit_spread, state.literal_order = True, False
    state.legs = []
    return True


def _structure_legs(state: _Line, pair: str | None, default_fly: str, notes: list[str],
                    *, priced: bool) -> tuple[tuple[QuoteLeg, ...], list[float]]:
    """The legs of a structure as records, and the numbers left for the price.

    Signs are the legs' own when any leg carried one (a leg that did not is
    taken as bought, and said); with none at all two legs are read as the
    second less the first, which is the calendar convention read across
    strikes, and three or more are refused -- there is no convention that
    says what ``1M vs 3M vs 6M`` is, and a fly guessed one way is a fly
    priced upside down.  A risk reversal leg's direction word is folded
    into its weight, so the structure's value is in the convention it was
    written in.
    """
    legs = state.legs
    last = legs[-1]
    if priced:
        numbers = _price_numbers(last, notes, assumed=last.instrument is None)
    else:
        _leg_strike(last, len(legs) - 1)
        numbers = []
    for leg in legs:
        _finish_leg(leg, default_fly, notes, state.quote_kind)
    said = [leg.sign for leg in legs]
    if any(s is not None for s in said):
        signs = [1.0 if s is None else s for s in said]
        if any(s is None for s in said):
            notes.append("a leg with no sign beside legs with one is read as bought")
    elif len(legs) == 2:
        signs = [-1.0, 1.0]
        notes.append("read as the second leg less the first; write '+' and '-' (or buy and "
                     "sell) on the legs to say it")
    else:
        raise ValueError(
            f"a structure of {len(legs)} legs needs a sign on each, as in "
            f"'+1M ATM vs -2x 3M ATM vs +6M ATM'; nothing says which legs are bought")
    # One note per thing said, not one per leg that said it.
    seen: set = set()
    notes[:] = [n for n in notes if not (n in seen or seen.add(n))]
    out = []
    for leg, sign in zip(legs, signs):
        quoted = sign * leg.weight
        weight = quoted
        if leg.instrument == "rr":
            weight *= _resolve_sign(leg, pair, notes)
        out.append(QuoteLeg(kind=leg.instrument, expiry=leg.expiries[0], weight=weight,
                            delta=leg.delta, strike=leg.strike, is_call=leg.is_call,
                            fly_kind=leg.fly_kind if leg.instrument == "fly" else None,
                            quoted_weight=quoted,
                            direction=leg.over if leg.instrument == "rr" else None))
    return tuple(out), numbers


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
    if state.legs and not _collapse_legs(state, notes):
        return _build_structure(state, pair, default_fly, line_no, raw, notes)
    if not state.numbers:
        raise ValueError("no price on the line")
    _settle_expiries(state, notes)
    # Whether the line named its instrument is remembered here and reported
    # further down, once the columns have been sorted out: a line that reads
    # as an at-the-money on the words alone can still turn out to carry a
    # strike column, and saying "read as atm" about a quote that came back as
    # an outright is worse than saying nothing.
    assumed = state.instrument is None
    if assumed:
        state.instrument = "atm" if state.delta is None and state.strike is None else "outright"
    numbers = _price_numbers(state, notes, assumed=assumed)
    if state.explicit_spread and len(state.expiries) == 2:
        instrument = "spread"
    else:
        instrument = state.instrument
    if instrument == "outright" and state.strike is None and state.delta is None:
        raise ValueError(
            "an outright needs a strike or a delta as well as a price; "
            + ", ".join(f"{n:g}" for n in numbers) + " were read as the price")
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
    if state.quote_kind == "premium" and instrument != "outright":
        raise ValueError(f"a premium is a price on an option, and this line is a{'n' if instrument == 'atm' else ''} "
                         f"{instrument}; quote it in volatility, or write the strike and the side")
    _settle_side(state, notes)
    premium_unit, factor = _premium_unit(state, pair, notes)
    if factor != 1.0:
        bid, ask = bid * factor, ask * factor
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
        quote_kind=state.quote_kind, premium_unit=premium_unit, pair=state.pair,
        timestamp_text=stamp_text, notes=tuple(notes),
    )


def _build_structure(state: _Line, pair: str | None, default_fly: str, line_no: int,
                     raw: str, notes: list[str]) -> MarketQuote:
    """A priced structure: the legs, and the price on the whole of it."""
    legs, numbers = _structure_legs(state, pair, default_fly, notes, priced=True)
    if not numbers:
        raise ValueError("no price on the line")
    if len(numbers) == 1:
        bid = ask = numbers[0]
        notes.append("one number, taken as a choice price with no width")
    else:
        bid, ask = numbers[0], numbers[1]
    inverted = ask < bid
    if inverted:
        bid, ask = ask, bid
    premium_unit, factor = _premium_unit(state, pair, notes)
    if factor != 1.0:
        bid, ask = bid * factor, ask * factor
    stamp_text = " ".join(x for x in (state.stamp_date, state.stamp_time) if x)
    near, far = legs[0].expiry, legs[-1].expiry
    return MarketQuote(
        instrument="structure", expiry=near,
        expiry_far=(None if str(far) == str(near) else far), leg=None,
        bid=bid, ask=ask, legs=legs, size=state.size, size_basis=state.size_basis,
        label=state.label, line=line_no, raw=raw.strip(), inverted=inverted,
        quote_kind=state.quote_kind, premium_unit=premium_unit, pair=state.pair,
        timestamp_text=stamp_text, notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# the paste
# ---------------------------------------------------------------------------


def _decide_unit(quotes: list[MarketQuote], forced: str) -> tuple[str, str]:
    """One volatility unit for the whole paste, and never the level's doing.

    A volatility is read as the number it was written as: 8.20 is 8.20
    volatility points and 0.35 is 0.35 of a point.  The level is not evidence
    of the unit -- that is the same rule §4 states for a historical sheet, and
    for the same reason: a managed pair marks its at-the-money at a third of a
    point, and a reader that sniffed the magnitude turned that into 35 points.

    ``vol_unit='decimal'`` is how a paste in decimals is read, and it is
    something a person says.  It used to be inferred, which meant an
    unremarkable USDHKD run came back a hundred times too large and a paste
    with one small level in it was refused outright.
    """
    if forced in ("percent", "decimal"):
        return forced, f"forced to {forced} by the caller"
    levels = [abs(q.mid) for q in quotes
              if q.instrument in ("atm", "outright") and q.quote_kind == "vol"]
    hi = max(levels, default=0.0)
    if levels and hi < 1.0:
        # Said once, because it is the one reading a person might have meant
        # the other way; never guessed at.
        return "percent", (
            f"read as volatility points, as written; the levels sit at or below {hi:.4g}, "
            f"which is low for points but is what a managed pair marks. Set the volatility "
            f"unit to decimal if the paste really is in decimals")
    return "percent", "read as volatility points, as written"


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

    legs = tuple((l.kind, ekey(l.expiry), l.weight, l.delta, l.strike, l.is_call, l.fly_kind)
                 for l in getattr(q, "legs", ()))
    # The pair is in the key so that, with no pair given, two pairs' 1M ATM
    # are two quotes; with one given every line carries it and it is inert.
    return (q.instrument, ekey(q.expiry), ekey(q.expiry_far), q.leg,
            q.delta, q.strike, q.is_call, q.fly_kind, legs, getattr(q, "pair", None))


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
                 fly_convention: str = "market", today: date | None = None) -> ParsedRun:
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
    blocks = _Blocks(pair)
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0]
        if not body.strip():
            continue
        norm = _norm(body)
        if blocks.heading(n, norm):
            continue
        if _is_header(body):
            headers.append(n)
            continue
        state = _Line(today=today)
        try:
            _consume(norm, state)
            if blocks.foreign(n, raw, state):
                continue
            raw_quotes.append(_build(state, pair, fly_convention, n, raw))
        except (ValueError, TenorError) as exc:
            skipped.append((n, raw.strip(), str(exc)))

    unit, evidence = _decide_unit(raw_quotes, vol_unit)
    scale = 0.01 if unit == "percent" else 1.0
    # A premium is not a volatility and is never scaled by the paste's unit.
    scaled = [
        MarketQuote(**{**vars(q), "bid": q.bid * (scale if q.quote_kind == "vol" else 1.0),
                       "ask": q.ask * (scale if q.quote_kind == "vol" else 1.0),
                       "strike": q.strike})
        for q in raw_quotes
    ]

    notes = [f"volatility unit: {unit} ({evidence})"]
    notes.extend(blocks.notes())
    if headers:
        notes.append(f"line{'s' if len(headers) > 1 else ''} "
                     f"{', '.join(str(n) for n in headers)} read as a column header and "
                     f"passed over")
    premiums = [q for q in scaled if q.quote_kind == "premium"]
    if premiums:
        notes.append(f"{len(premiums)} line(s) are premiums rather than volatilities and are "
                     f"turned into volatilities against the forward when the fit reads them: "
                     + ", ".join(f"line {q.line}" for q in premiums[:6]))
    structures = [q for q in scaled if q.instrument == "structure"]
    if structures:
        notes.append(f"{len(structures)} line(s) are multi-leg structures, valued as the signed "
                     f"sum of their legs: " + "; ".join(q.describe() for q in structures[:4]))
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
                     superseded=tuple(superseded), ignored=tuple(blocks.ignored))


class _Blocks:
    """Which pair each line is about, and which lines are somebody else's.

    A line names its pair itself (``EURUSD 1M ATM 8.2/8.6``) or sits under a
    heading that is nothing but a pair.  With a ``pair`` given, a line that
    names another one is **ignored**: read, understood, and left out with the
    pair it named -- not an error, because nothing on it was wrong, and not
    a quote, because a broker's EURUSD run pasted under USDJPY would
    otherwise move USDJPY's marks.  With no pair given, every line is read
    and carries the pair it named.
    """

    def __init__(self, pair: str | None):
        self.pair = pair.upper()[:6] if pair else None
        self.block: str | None = None
        self.headings: list[tuple[int, str]] = []
        self.ignored: list[tuple[int, str, str]] = []

    def heading(self, n: int, norm: str) -> bool:
        m = _PAIR_LINE.match(norm)
        if m and m.group(1) in _CCY and m.group(2) in _CCY:
            self.block = (m.group(1) + m.group(2)).upper()
            self.headings.append((n, self.block))
            return True
        return False

    def foreign(self, n: int, raw: str, state: _Line) -> bool:
        named = state.pair or self.block
        if named and self.pair and named != self.pair:
            self.ignored.append((n, raw.strip(), f"quotes {named}, not {self.pair}"))
            return True
        state.pair = named or self.pair
        return False

    def notes(self) -> list[str]:
        out = []
        if self.headings:
            out.append("pair heading" + ("s" if len(self.headings) > 1 else "") + ": "
                       + ", ".join(f"line {n} ({p})" for n, p in self.headings)
                       + "; the lines under each are read as that pair's")
        if self.ignored:
            counts: dict[str, int] = {}
            for _, _, why in self.ignored:
                counts[why.split()[1].rstrip(",")] = counts.get(why.split()[1].rstrip(","), 0) + 1
            out.append(f"{len(self.ignored)} line(s) quote another pair and were passed over: "
                       + ", ".join(f"{p} ({c})" for p, c in counts.items()))
        return out


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
    if state.legs and not _collapse_legs(state, notes):
        legs, _ = _structure_legs(state, pair, default_fly, notes, priced=False)
        premium_unit, _ = _premium_unit(state, pair, notes)
        near, far = legs[0].expiry, legs[-1].expiry
        return QuoteRequest(
            instrument="structure", expiry=near,
            expiry_far=(None if str(far) == str(near) else far), legs=legs,
            size=state.size, size_basis=state.size_basis, label=state.label,
            line=line_no, raw=raw.strip(), quote_kind=state.quote_kind,
            premium_unit=premium_unit, pair=state.pair, notes=tuple(notes))
    _settle_expiries(state, notes)
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
        if len(values) == 1 and state.strike is None \
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
    if state.quote_kind == "premium" and instrument != "outright":
        raise ValueError(f"a premium is a price on an option, and this line is a{'n' if instrument == 'atm' else ''} "
                         f"{instrument}; ask for it in volatility, or write the strike and the side")
    _settle_side(state, notes)
    premium_unit, _ = _premium_unit(state, pair, notes)
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
        quote_kind=state.quote_kind, premium_unit=premium_unit, pair=state.pair,
        notes=tuple(notes),
    )


def parse_requests(text: str, *, pair: str | None = None,
                   fly_convention: str = "market",
                   today: date | None = None) -> ParsedRequests:
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
    blocks = _Blocks(pair)
    for n, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#")[0].split("//")[0]
        if not body.strip():
            continue
        norm = _norm(body)
        if blocks.heading(n, norm):
            continue
        if _is_header(body):
            headers.append(n)
            continue
        state = _Line(today=today)
        try:
            _consume(norm, state)
            if blocks.foreign(n, raw, state):
                continue
            requests.append(_build_request(state, pair, fly_convention, n, raw))
        except (ValueError, TenorError) as exc:
            skipped.append((n, raw.strip(), str(exc)))

    notes: list[str] = list(blocks.notes())
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
    return ParsedRequests(requests=tuple(requests), notes=tuple(notes), skipped=tuple(skipped),
                          ignored=tuple(blocks.ignored))
