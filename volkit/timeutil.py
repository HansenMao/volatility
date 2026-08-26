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

_TENOR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([dwmy])\s*$", re.IGNORECASE)

# Calendar-accurate unit lengths in days, used only when a tenor must be
# turned into a year fraction without reference to an actual date.
_UNIT_DAYS = {"d": 1.0, "w": 7.0, "m": DAYS_IN_YEAR / 12.0, "y": DAYS_IN_YEAR}


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
    if not m:
        raise TenorError(
            f"cannot parse tenor {tenor!r}; expected forms like '1W', '3M', '18M', '2Y', '5D'"
        )
    return float(m.group(1)), m.group(2).lower()


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
        return parse_datetime(value)
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
)


def parse_datetime(text: str) -> datetime:
    """Parse a date/datetime string: the tabular formats, then ISO 8601.

    The explicit formats are tried first and unchanged, so nothing a workbook
    or a paste already parses moves.  ISO 8601 is the fallback because it is
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
        raise ValueError(
            f"cannot parse datetime {text!r}; tried {len(_DATETIME_FORMATS)} formats "
            f"and ISO 8601") from None


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
