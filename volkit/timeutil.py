"""Time, day-count and tenor handling.

The legacy code used six different year-lengths (365, 365.2425, 31536000,
31556952, 52.0345, 12.0079726) depending on which function you happened to
call, and read the wall clock via ``datetime.utcnow()`` from deep inside the
model.  That made results irreproducible: a term structure built over a few
seconds of computation was not internally consistent.

Here there is exactly one year length, one clock, and the clock is injected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date, time

# Mean Gregorian year.  This is the single day-count authority for the package.
DAYS_IN_YEAR = 365.2425
SECONDS_IN_YEAR = DAYS_IN_YEAR * 24 * 60 * 60
SECONDS_IN_DAY = 24 * 60 * 60

UTC = timezone.utc

# A tenor is a number and a unit, and the unit may be spelled out.  A desk
# writes "1wk" as readily as "1W" and "3mth" as readily as "3M", and a box
# that takes one and refuses the other makes somebody translate their own
# shorthand before they can price.  Every alternative begins with the letter
# it means, so the unit is the word's first letter and there is still exactly
# one unit vocabulary below.  Longest spelling first, so "days" is not read
# as "d" with a tail left over.
_TENOR_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*-?\s*"
    r"(days|day|d"
    r"|weeks|week|wks|wk|w"
    r"|months|month|mths|mth|mos|mon|mo|m"
    r"|years|year|yrs|yr|y)\s*$",
    re.IGNORECASE)

# Calendar-accurate unit lengths in days, used only when a tenor must be
# turned into a year fraction without reference to an actual date.
_UNIT_DAYS = {"d": 1.0, "w": 7.0, "m": DAYS_IN_YEAR / 12.0, "y": DAYS_IN_YEAR}

# The short-date codes the market writes instead of a number and a unit.  A
# broker does not quote "1D" at the front of a curve: it quotes O/N, T/N, S/N
# and S/W, and an option desk asks for the overnight by name.  Each is read as
# the numeric tenor it *is*, so that everything downstream -- sort keys, year
# fractions, the calendar construction in ``calendars.fx_dates`` -- has one
# shape to handle rather than a special case per screen.
#
# The counts are from the *trade* date, which is what a day tenor means: O/N
# is one business day (expiry tomorrow, delivering from tomorrow's own spot),
# T/N two and S/N three.  S/W is the spot week and is a *week* tenor, not a
# day one, because it settles a week after spot like every other weekly
# pillar.
_CODE_TENORS: dict[str, tuple[float, str]] = {
    "ON": (1.0, "d"), "OVERNIGHT": (1.0, "d"),
    "TN": (2.0, "d"), "TOMNEXT": (2.0, "d"), "TOMORROWNEXT": (2.0, "d"),
    "SN": (3.0, "d"), "SPOTNEXT": (3.0, "d"),
    "SW": (1.0, "w"), "SPOTWEEK": (1.0, "w"),
}

# How each code is spelled back out.  ``normalise_tenor`` is the one place a
# tenor is given its canonical spelling, so ``o/n`` and ``ON`` reach a
# dictionary key as one thing and a panel that lists a tenor cannot list it
# twice under two spellings.
_CODE_SPELLING: dict[str, str] = {
    "ON": "O/N", "OVERNIGHT": "O/N",
    "TN": "T/N", "TOMNEXT": "T/N", "TOMORROWNEXT": "T/N",
    "SN": "S/N", "SPOTNEXT": "S/N",
    "SW": "S/W", "SPOTWEEK": "S/W",
}


def _code_key(text: str) -> str:
    """A short-date code stripped to its letters: ``o/n``, ``O N`` -> ``ON``."""
    return re.sub(r"[^A-Za-z]", "", str(text)).upper()


class TenorError(ValueError):
    """Raised when a tenor string cannot be parsed."""


def parse_tenor(tenor: str) -> tuple[float, str]:
    """Parse ``"3M"`` into ``(3.0, "m")``.

    The legacy ``get_years_time`` silently mishandled anything that was not
    weeks or months: ``"1D"`` returned 1.0, i.e. one *year*.  Unknown units
    now raise.
    """
    if not isinstance(tenor, str):
        raise TenorError(f"tenor must be a string, got {type(tenor).__name__}: {tenor!r}")
    m = _TENOR_RE.match(tenor)
    if m:
        # Every spelling of a unit starts with the letter it means: "wk",
        # "week" and "weeks" are all "w".
        return float(m.group(1)), m.group(2)[0].lower()
    code = _CODE_TENORS.get(_code_key(tenor)) if tenor.strip() else None
    if code is not None:
        return code
    raise TenorError(
        f"cannot parse tenor {tenor!r}; expected forms like '1W', '3M', '18M', '2Y', '5D', "
        f"a spelled-out unit ('1wk', '3mth', '2yr', '10 days'), "
        f"or a short-date code (O/N, T/N, S/N, S/W)"
    )


def normalise_tenor(tenor: str) -> str:
    """The canonical spelling of a tenor: ``o/n`` -> ``O/N``, ``3m`` -> ``3M``.

    A tenor arrives spelled however the person or the file spelled it and is
    then used as a dictionary key -- of marks, of spread-table pillars, of the
    expiries a market-maker run mentions.  Two spellings of one tenor are two
    pillars, and the panel shows one expiry twice.  ``1D`` and ``O/N`` are
    deliberately *not* merged: they resolve to the same dates, but this is a
    spelling function and not a synonym table, and a desk that asked for one
    did not ask for the other.
    """
    text = str(tenor).strip()
    key = _code_key(text)
    if key in _CODE_SPELLING and not _TENOR_RE.match(text):
        return _CODE_SPELLING[key]
    n, unit = parse_tenor(text)
    return f"{n:g}{unit.upper()}"


def tenor_to_years(tenor: str) -> float:
    """Approximate year fraction for a tenor string, with no reference date."""
    n, unit = parse_tenor(tenor)
    return n * _UNIT_DAYS[unit] / DAYS_IN_YEAR


def tenor_sort_key(tenor: str) -> float:
    return tenor_to_years(tenor)


def add_tenor(anchor: date | datetime, tenor: str) -> date:
    """Add a tenor to a date using real calendar arithmetic.

    Months roll by calendar month (with end-of-month clamping) rather than by
    a fractional-year approximation, so 1M from 31-Jan lands on 28-Feb.
    """
    n, unit = parse_tenor(tenor)
    d = anchor.date() if isinstance(anchor, datetime) else anchor
    if unit == "d":
        return d + timedelta(days=int(n))
    if unit == "w":
        return d + timedelta(days=int(n * 7))
    months = int(n) if unit == "m" else int(n) * 12
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # clamp to end of month: 31-Jan + 1M -> 28/29-Feb
    if month == 12:
        last = 31
    else:
        last = (date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def as_utc(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one to UTC.

    The legacy code passed naive datetimes around while commenting that they
    were GMT.  Everything here is explicitly tz-aware so that comparison and
    subtraction cannot silently mix conventions.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_datetime(value, clock: "Clock | None" = None) -> datetime:
    """Coerce a year-fraction, date, datetime or date-string to a UTC datetime."""
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time()).replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        if clock is None:
            raise ValueError("a Clock is required to interpret a year fraction as a date")
        return clock.datetime_from_years(float(value))
    if isinstance(value, str):
        # The clock, where there is one, is what says which year a date
        # written without one means.
        return parse_datetime(value, today=clock.now.date() if clock is not None else None)
    raise TypeError(f"cannot interpret {value!r} as a datetime")


_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d-%b-%y",
    "%d-%b-%Y",
    # The spellings a person types into an expiry box, appended so that every
    # format above is still tried first and nothing that already parsed moves.
    # Each of these carries a month *name* or leads with the year, so none of
    # them can be read two ways: the one genuinely ambiguous form, a
    # day-first "05/06/2026", is deliberately absent -- "%m/%d/%Y" above
    # already claims it, and a second slash format that only caught the days
    # past the 12th would read half a column one way and half the other.
    "%d%b%y", "%d%b%Y",                       # 15Sep26, 15Sep2026
    "%d %b %y", "%d %b %Y",                   # 15 Sep 26
    "%d-%B-%y", "%d-%B-%Y", "%d %B %Y",       # 15-September-2026
    "%b %d %Y", "%b %d, %Y",                  # Sep 15 2026
    "%B %d %Y", "%B %d, %Y",                  # September 15, 2026
    "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d",  # 2026/09/15, 2026.09.15
    "%Y%m%d",                                 # 20260915
)


#: Date spellings with no year in them.  A person writing an expiry in a box
#: writes "06 Nov", not "06 Nov 2026": the year is obvious to them and typing
#: it is friction.  Only the forms carrying a month *name* are here -- a
#: bare "06/11" is a day and a month in one country and a month and a day in
#: another, and there is nothing in the string to say which, so it is refused
#: rather than read one of the two ways.  Resolved by :func:`next_matching`.
_YEARLESS_DATE_FORMATS = tuple(
    fmt + suffix
    for fmt in ("%d%b", "%d %b", "%d-%b", "%d.%b", "%d/%b",
                "%d%B", "%d %B", "%d-%B",
                "%b%d", "%b %d", "%b-%d", "%b %d,", "%B %d", "%B-%d", "%B %d,")
    for suffix in ("", " %H:%M", " %H:%M:%S")
)


def next_matching(month: int, day: int, today: date) -> date:
    """The first ``day``/``month`` on or after ``today``.

    A date typed without a year means the next one of it: "06 Nov" on the
    first of September is this November, and "06 Aug" is next August.  The
    horizon is the coming year, which is what every such date on a desk is --
    an expiry, an event, a settlement -- and it is why the *first* match is
    taken rather than the nearest one in either direction.

    The search runs a few years out rather than exactly one so that "29 Feb"
    resolves at all: it is the one day whose next occurrence can be three
    years away, and refusing it would be a worse answer than a date outside
    the nominal horizon.
    """
    for year in range(today.year, today.year + 5):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # 29 February in a common year
        if candidate >= today:
            return candidate
    raise ValueError(f"no date matches day {day} of month {month} in the coming years")


def _parse_yearless(text: str, today: date | None) -> datetime | None:
    """A year-less date resolved forward from ``today``, or ``None``.

    ``today`` is the book's clock, passed in like every other reference to
    now in this package: a date box that reads "06 Nov" as a different day
    depending on when the machine's wall clock is asked is not reproducible,
    and the whole point of the injected ``Clock`` is that it is.  With no
    reference date the caller is told what is missing rather than given the
    wall clock quietly.
    """
    for fmt in _YEARLESS_DATE_FORMATS:
        try:
            # Parsed into a leap year, because ``strptime`` with no year in
            # the format defaults to 1900 and refuses 29 February outright.
            parsed = datetime.strptime(f"{text} 2000", f"{fmt} %Y")
        except ValueError:
            continue
        if today is None:
            raise ValueError(
                f"{text!r} has no year in it, and no reference date was given to say which "
                f"one it means; write the year. (A caller with a clock reads it as the next "
                f"one of it: pass today=clock.now.date().)")
        d = next_matching(parsed.month, parsed.day, today)
        return datetime.combine(d, parsed.time()).replace(tzinfo=UTC)
    return None


def parse_datetime(text: str, *, today: date | None = None) -> datetime:
    """Parse a date/datetime string: the tabular formats, then ISO 8601.

    The explicit formats are tried first and unchanged, so nothing a workbook
    or a paste already parses moves.  The typed spellings at the end of the
    list were added for the pricing screen's expiry box, which takes a date
    as readily as a tenor and should not make somebody translate ``15Sep26``
    -- the very form this package *prints* in a leg label -- by hand.

    ISO 8601 is the fallback because it is
    the form the tool itself *writes* -- the valuation stamp in
    ``/api/state``, the timestamps in a session file, the value a browser's
    ``datetime-local`` field carries -- and reading back what you printed
    should not need a translation step at every call site.

    Three callers had each written that step for themselves, and they had
    written it differently: the listed panel understood a trailing ``Z`` and
    an offset, the events route and the session loader understood neither, so
    one string parsed on one screen and failed on the next.  Worse, dropping
    an offset and then stamping the result UTC reads ``19:00+09:00`` as
    19:00Z -- a nine-hour error in an expiry, arrived at silently.  An offset
    is *converted* here, not discarded.

    A naive string is still UTC, which is what every caller means: these are
    market timestamps and this package has one time zone inside it.

    ``today`` is the reference date a year-less spelling is resolved against
    (``next_matching``): "06 Nov" is the coming sixth of November.  It is
    passed in, never taken from the wall clock, so that a screen reading the
    same box twice reads it the same way; without it a year-less string is
    refused by name rather than guessed at.
    """
    text = str(text).strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return as_utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    dated = _parse_yearless(text, today)
    if dated is not None:
        return dated
    raise ValueError(
        f"cannot parse datetime {text!r}; tried {len(_DATETIME_FORMATS)} formats, "
        f"ISO 8601, and the year-less spellings ('06 Nov', '6Nov')")


@dataclass(frozen=True)
class Clock:
    """A fixed valuation instant.

    Every year fraction in the package is measured from ``now``.  Freezing it
    at construction is what makes a whole surface build self-consistent and
    what makes the regression tests deterministic.
    """

    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", as_utc(self.now))

    @classmethod
    def utcnow(cls) -> "Clock":
        return cls(datetime.now(UTC))

    def years_to(self, dt: datetime) -> float:
        """Year fraction from the valuation instant to ``dt`` (may be negative)."""
        return (as_utc(dt) - self.now).total_seconds() / SECONDS_IN_YEAR

    def datetime_from_years(self, t: float) -> datetime:
        return self.now + timedelta(seconds=t * SECONDS_IN_YEAR)

    def coerce_years(self, value) -> float:
        """Accept a year fraction, date, datetime or string and return years."""
        if isinstance(value, (int, float)):
            return float(value)
        return self.years_to(to_datetime(value, self))

    def coerce_datetime(self, value) -> datetime:
        return to_datetime(value, self)
