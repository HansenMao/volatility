"""Holiday calendars and FX date rolling.

The legacy ``isholiday`` constructed five ``holidays.US()``-style objects on
*every call*, and was itself called inside a date-rolling loop.  It also left
``# manually add Chinese holidays`` as a standing TODO, because adding a date
meant editing code.

Here calendars are built once and cached, the optional ``holidays`` package is
used when installed but is not required, and extra dates load from a file or a
worksheet -- so a CNY calendar is data, not a code change.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from . import paths
from .timeutil import add_tenor, parse_datetime

try:  # optional dependency; the built-in rules below are the fallback
    import holidays as _holidays_pkg
except ImportError:  # pragma: no cover - depends on environment
    _holidays_pkg = None


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous / Meeus-Jones-Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of a month; ``n = -1`` means the last."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    nxt = date(year + month // 12, month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """US federal observation: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _observed_forward(dates: list[date]) -> set[date]:
    """UK/Commonwealth observation: a weekend holiday moves *forward*.

    Applying the US rule here is wrong and matters at year end: when Christmas
    falls on a Saturday the UK substitute days are the following Monday and
    Tuesday (27th and 28th), not the preceding Friday.  Collisions are resolved
    by pushing to the next free weekday, which is what produces the 27th/28th
    pair rather than both landing on the 27th.
    """
    taken: set[date] = set()
    for d in sorted(dates):
        out = d
        while out.weekday() >= 5 or out in taken:
            out += timedelta(days=1)
        taken.add(out)
    return taken


@functools.lru_cache(maxsize=256)
def _builtin_holidays(country: str, year: int) -> frozenset[date]:
    """Rule-based fallback calendars for the majors.

    Deliberately covers the dates that move FX liquidity rather than every
    statutory holiday.  Anything missing is added through overrides.
    """
    out: set[date] = set()
    e = easter(year)
    good_friday = e - timedelta(days=2)
    easter_monday = e + timedelta(days=1)

    if country == "US":
        out |= {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),      # MLK
            _nth_weekday(year, 2, 0, 3),      # Presidents
            _nth_weekday(year, 5, 0, -1),     # Memorial
            _observed(date(year, 6, 19)),     # Juneteenth
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),      # Labor
            _nth_weekday(year, 11, 3, 4),     # Thanksgiving
            _observed(date(year, 12, 25)),
        }
    elif country == "UK":
        out |= {good_friday, easter_monday,
                _nth_weekday(year, 5, 0, 1),      # Early May
                _nth_weekday(year, 5, 0, -1),     # Spring
                _nth_weekday(year, 8, 0, -1)}     # Summer
        out |= _observed_forward([date(year, 1, 1), date(year, 12, 25), date(year, 12, 26)])
    elif country == "JP":
        out |= {
            date(year, 1, 1), date(year, 1, 2), date(year, 1, 3),
            _nth_weekday(year, 1, 0, 2),      # Coming of Age
            date(year, 2, 11), date(year, 2, 23),
            date(year, 4, 29), date(year, 5, 3), date(year, 5, 4), date(year, 5, 5),
            _nth_weekday(year, 7, 0, 3),      # Marine
            date(year, 8, 11),
            _nth_weekday(year, 9, 0, 3),      # Respect for the Aged
            _nth_weekday(year, 10, 0, 2),     # Sports
            date(year, 11, 3), date(year, 11, 23),
        }
        # Japanese substitute holidays: a Sunday holiday rolls to Monday.
        out |= {d + timedelta(days=1) for d in list(out) if d.weekday() == 6}
    elif country == "SG":
        # Solar-fixed Singapore holidays.  Chinese New Year, Hari Raya, Deepavali
        # and Vesak are lunar and must come from overrides.
        out |= _observed_forward([date(year, 1, 1), date(year, 5, 1), date(year, 8, 9),
                                  date(year, 12, 25)])
    elif country == "CA":
        out |= {
            _observed(date(year, 1, 1)), good_friday,
            _nth_weekday(year, 9, 0, 1), _observed(date(year, 7, 1)),
            _nth_weekday(year, 10, 0, 2), _observed(date(year, 12, 25)),
            _observed(date(year, 12, 26)),
        }
    elif country == "NZ":
        out |= {
            _observed(date(year, 1, 1)), _observed(date(year, 1, 2)),
            date(year, 2, 6), good_friday, easter_monday, date(year, 4, 25),
            _nth_weekday(year, 6, 0, 1), _nth_weekday(year, 10, 0, 4),
            _observed(date(year, 12, 25)), _observed(date(year, 12, 26)),
        }
    elif country == "AU":
        out |= {
            _observed(date(year, 1, 1)), date(year, 1, 26), good_friday,
            easter_monday, date(year, 4, 25), _observed(date(year, 12, 25)),
            _observed(date(year, 12, 26)),
        }
    elif country == "EU":
        out |= {
            date(year, 1, 1), good_friday, easter_monday, date(year, 5, 1),
            date(year, 12, 25), date(year, 12, 26),
        }
    elif country == "CN":
        # Lunar dates change every year and are published by the State
        # Council, so only the solar-fixed ones are hard-coded.  Chinese New
        # Year must come from overrides; see CalendarSet.add_overrides.
        out |= {
            date(year, 1, 1), date(year, 5, 1), date(year, 10, 1),
            date(year, 10, 2), date(year, 10, 3),
        }
    elif country == "HK":
        out |= {
            date(year, 1, 1), good_friday, e + timedelta(days=1),
            date(year, 5, 1), date(year, 7, 1), date(year, 10, 1),
            date(year, 12, 25), date(year, 12, 26),
        }
    return frozenset(out)


@functools.lru_cache(maxsize=256)
def _package_holidays(country: str, year: int) -> frozenset[date]:
    """Dates from the ``holidays`` package, if it is installed."""
    if _holidays_pkg is None:
        return frozenset()
    try:
        return frozenset(_holidays_pkg.country_holidays(country, years=[year]).keys())
    except (KeyError, NotImplementedError, AttributeError):  # pragma: no cover
        return frozenset()


# Which national calendars each currency observes.
CURRENCY_CALENDARS: dict[str, tuple[str, ...]] = {
    "USD": ("US",), "GBP": ("UK",), "EUR": ("EU",), "JPY": ("JP",),
    "CAD": ("CA",), "NZD": ("NZ",), "AUD": ("AU",), "CHF": ("EU",),
    "CNH": ("CN",), "CNY": ("CN",), "HKD": ("HK",), "SGD": ("SG",),
}


@dataclass
class CalendarSet:
    """Holiday lookup for currencies and pairs, with user overrides.

    ``use_package`` unions in the ``holidays`` package when available; the
    built-in rules always apply, so behaviour is identical with or without
    the optional dependency for the dates that matter.
    """

    use_package: bool = True
    overrides: dict[str, set[date]] = field(default_factory=dict)
    removals: dict[str, set[date]] = field(default_factory=dict)

    def _country_dates(self, country: str, year: int) -> frozenset[date]:
        dates = set(_builtin_holidays(country, year))
        if self.use_package:
            dates |= _package_holidays(country, year)
        dates |= {d for d in self.overrides.get(country, ()) if d.year == year}
        dates -= {d for d in self.removals.get(country, ()) if d.year == year}
        return frozenset(dates)

    def countries_for(self, ccy_or_pair: str) -> tuple[str, ...]:
        """Calendars touched by a currency or a 6-letter pair."""
        s = ccy_or_pair.upper()
        out: list[str] = []
        for ccy in ({s[:3], s[3:6]} if len(s) >= 6 else {s[:3]}):
            out.extend(CURRENCY_CALENDARS.get(ccy, ()))
        return tuple(dict.fromkeys(out))

    def is_holiday(self, ccy_or_pair: str, d: date | datetime) -> bool:
        d = d.date() if isinstance(d, datetime) else d
        return any(d in self._country_dates(c, d.year) for c in self.countries_for(ccy_or_pair))

    def holiday_countries(self, ccy_or_pair: str, d: date | datetime) -> tuple[str, ...]:
        """Which calendars are shut on ``d`` -- drives the intraday weighting."""
        d = d.date() if isinstance(d, datetime) else d
        return tuple(c for c in self.countries_for(ccy_or_pair) if d in self._country_dates(c, d.year))

    def is_business_day(self, ccy_or_pair: str, d: date | datetime) -> bool:
        d = d.date() if isinstance(d, datetime) else d
        return d.weekday() < 5 and not self.is_holiday(ccy_or_pair, d)

    def roll(self, ccy_or_pair: str, d: date, convention: str = "modified_following") -> date:
        """Roll a date to a good business day.

        Default is modified following, the FX market standard.  The legacy
        code rolled weekends *backwards* unconditionally, which is the
        'preceding' convention; pass ``convention='preceding'`` to reproduce it.
        """
        if convention == "preceding":
            step, modified = -1, False
        elif convention == "following":
            step, modified = 1, False
        elif convention == "modified_following":
            step, modified = 1, True
        else:
            raise ValueError(f"unknown roll convention {convention!r}")
        out = d
        for _ in range(400):
            if self.is_business_day(ccy_or_pair, out):
                break
            out += timedelta(days=step)
        else:  # pragma: no cover
            raise RuntimeError(f"could not roll {d} onto a business day for {ccy_or_pair}")
        if modified and out.month != d.month:
            return self.roll(ccy_or_pair, d, "preceding")
        return out

    def add_business_days(self, ccy_or_pair: str, d: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        out = d
        for _ in range(abs(n)):
            out += timedelta(days=step)
            while not self.is_business_day(ccy_or_pair, out):
                out += timedelta(days=step)
        return out

    def spot_lag(self, pair: str) -> int:
        """Settlement lag in business days.  USDCAD is T+1, the rest T+2."""
        return 1 if pair.upper() in {"USDCAD", "CADUSD"} else 2

    def spot_date(self, pair: str, today: date) -> date:
        return self.add_business_days(pair, today, self.spot_lag(pair))

    def expiry_date(self, pair: str, tenor: str, today: date,
                    convention: str = "modified_following") -> date:
        """Option expiry for a tenor, derived from the delivery date.

        Spot is rolled forward, the tenor is added by calendar month, the
        delivery date is rolled to a good business day, and the expiry is the
        spot lag back from delivery -- the standard FX construction.
        """
        spot = self.spot_date(pair, today)
        delivery = self.roll(pair, add_tenor(spot, tenor), convention)
        return self.add_business_days(pair, delivery, -self.spot_lag(pair))

    def add_overrides(self, country: str, dates, remove: bool = False) -> None:
        """Add (or suppress) calendar dates for a country at runtime."""
        target = self.removals if remove else self.overrides
        parsed = {
            (d.date() if isinstance(d, datetime) else d) if isinstance(d, (date, datetime))
            else parse_datetime(str(d)).date()
            for d in dates
        }
        target.setdefault(country.upper(), set()).update(parsed)

    def load_overrides_csv(self, path: str | Path) -> int:
        """Load ``country,date[,remove]`` rows.  Blank lines and ``#`` ignored."""
        count = 0
        for line in paths.read_text(path).splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                raise ValueError(f"bad holiday override row {line!r}: expected 'country,date'")
            remove = len(parts) > 2 and parts[2].lower() in {"1", "true", "remove", "y", "yes"}
            self.add_overrides(parts[0], [parts[1]], remove=remove)
            count += 1
        return count


DEFAULT_CALENDARS = CalendarSet()
