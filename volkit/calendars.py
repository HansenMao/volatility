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

from .timeutil import UTC, add_tenor, normalise_tenor, parse_datetime, parse_tenor

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


#: Currencies whose calendar must be open on any FX settlement date, whether
#: or not they are in the pair.  Every FX trade settles through New York, so a
#: US holiday cannot be a value date for EURJPY any more than for EURUSD.
#:
#: It does *not* stop the count.  The market convention is precise about this
#: and the two halves are easy to conflate: for a pair with no dollar in it
#: the two business days to spot are counted on the two currencies' own
#: calendars, and US holidays are simply not looked at -- but the date that
#: count lands on must then also be good in USD, and is rolled forward until
#: it is.  Counting US holidays as well would push EURJPY spot out a day every
#: Thanksgiving, which is not what the market does.
SETTLEMENT_CURRENCIES: tuple[str, ...] = ("USD",)


@dataclass(frozen=True)
class FxDates:
    """The four dates an FX option tenor resolves to, and how it got there.

    A tenor is not a length of time, it is a **settlement date**: the market
    adds the tenor to the spot date, adjusts that to a good value date, and
    the option's expiry is then the spot lag back from it.  Both dates travel
    together because a screen that shows one and computes the other from a
    year fraction is a screen with two answers to one question -- and because
    the *forward* an option is priced against is the forward to ``delivery``,
    not to ``expiry`` (§4).

    ``trade`` is the valuation date the whole construction hangs off, and it
    comes from the book's clock, never from the machine.
    """

    pair: str
    tenor: str          # canonically spelled, or the date as written
    trade: date         # the valuation date
    spot: date          # where a spot trade dealt today settles
    expiry: date        # when the option expires
    delivery: date      # when the option settles: the spot lag after expiry
    rule: str = ""      # which construction produced it, in words


#: The workbook tab extra holiday dates are maintained on.
HOLIDAYS_SHEET = "HOLIDAYS"


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

    # -- which calendars ---------------------------------------------------
    def countries_for(self, ccy_or_pair: str) -> tuple[str, ...]:
        """Calendars touched by a currency or a 6-letter pair.

        These are the *pair's own* calendars, and they are what decides
        whether a day counts: the two business days to spot, the business
        days back from delivery to expiry, and whether an option can expire
        on a given date at all.  The settlement calendars are a superset --
        see :meth:`settlement_countries`.
        """
        s = ccy_or_pair.upper()
        out: list[str] = []
        for ccy in ({s[:3], s[3:6]} if len(s) >= 6 else {s[:3]}):
            out.extend(CURRENCY_CALENDARS.get(ccy, ()))
        return tuple(dict.fromkeys(out))

    def settlement_countries(self, ccy_or_pair: str) -> tuple[str, ...]:
        """The pair's calendars plus the ones every value date must clear.

        A settlement date has to be a day the money can actually move, which
        for FX means New York is open whatever the pair is.  The distinction
        from :meth:`countries_for` is the whole of the spot-date convention
        and is easy to lose: US holidays do not *stop the count*, they only
        rule out the date the count lands on.  See ``SETTLEMENT_CURRENCIES``.
        """
        out = list(self.countries_for(ccy_or_pair))
        for ccy in SETTLEMENT_CURRENCIES:
            out.extend(CURRENCY_CALENDARS.get(ccy, ()))
        return tuple(dict.fromkeys(out))

    # -- open days, on an explicit set of calendars ------------------------
    #
    # Everything below is written once against a tuple of countries and then
    # exposed twice: on the pair's calendars for the counting, on the
    # settlement calendars for the value dates.  Two copies of a roll loop is
    # two places for a holiday rule to be applied to the wrong set.

    def _is_open(self, countries: tuple[str, ...], d: date | datetime) -> bool:
        d = d.date() if isinstance(d, datetime) else d
        if d.weekday() >= 5:
            return False
        return not any(d in self._country_dates(c, d.year) for c in countries)

    def _roll_open(self, countries: tuple[str, ...], d: date,
                   convention: str = "modified_following") -> date:
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
            if self._is_open(countries, out):
                break
            out += timedelta(days=step)
        else:  # pragma: no cover
            raise RuntimeError(f"could not roll {d} onto an open day for {countries}")
        if modified and out.month != d.month:
            return self._roll_open(countries, d, "preceding")
        return out

    def _add_open_days(self, countries: tuple[str, ...], d: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        out = d
        for _ in range(abs(int(n))):
            out += timedelta(days=step)
            while not self._is_open(countries, out):
                out += timedelta(days=step)
        return out

    # -- the pair's own calendars ------------------------------------------
    def is_holiday(self, ccy_or_pair: str, d: date | datetime) -> bool:
        d = d.date() if isinstance(d, datetime) else d
        return any(d in self._country_dates(c, d.year) for c in self.countries_for(ccy_or_pair))

    def holiday_countries(self, ccy_or_pair: str, d: date | datetime) -> tuple[str, ...]:
        """Which calendars are shut on ``d`` -- drives the intraday weighting."""
        d = d.date() if isinstance(d, datetime) else d
        return tuple(c for c in self.countries_for(ccy_or_pair)
                     if d in self._country_dates(c, d.year))

    def is_business_day(self, ccy_or_pair: str, d: date | datetime) -> bool:
        return self._is_open(self.countries_for(ccy_or_pair), d)

    def is_settlement_day(self, ccy_or_pair: str, d: date | datetime) -> bool:
        """Whether money can settle on ``d``: the pair's calendars **and** USD."""
        return self._is_open(self.settlement_countries(ccy_or_pair), d)

    def roll(self, ccy_or_pair: str, d: date, convention: str = "modified_following") -> date:
        """Roll a date to a good business day on the pair's own calendars.

        Default is modified following, the FX market standard.  The legacy
        code rolled weekends *backwards* unconditionally, which is the
        'preceding' convention; pass ``convention='preceding'`` to reproduce it.
        """
        return self._roll_open(self.countries_for(ccy_or_pair), d, convention)

    def roll_settlement(self, ccy_or_pair: str, d: date,
                        convention: str = "following") -> date:
        """Roll a date to a good *value* date -- the pair's calendars and USD."""
        return self._roll_open(self.settlement_countries(ccy_or_pair), d, convention)

    def add_business_days(self, ccy_or_pair: str, d: date, n: int) -> date:
        return self._add_open_days(self.countries_for(ccy_or_pair), d, n)

    # -- the FX date construction ------------------------------------------
    def spot_lag(self, pair: str) -> int:
        """Settlement lag in business days.  USDCAD is T+1, the rest T+2."""
        return 1 if pair.upper() in {"USDCAD", "CADUSD"} else 2

    def spot_date(self, pair: str, today: date) -> date:
        """Where a spot deal struck on ``today`` settles.

        The count is on the pair's own calendars and the date it lands on is
        then rolled forward to a day USD can settle too -- the two halves of
        the market convention, in that order.  Doing it the other way round
        (counting US holidays as business days for the pair) pushes EURJPY
        spot out a day every Thanksgiving, which is not what the market does.
        """
        today = today.date() if isinstance(today, datetime) else today
        landed = self._add_open_days(self.countries_for(pair), today, self.spot_lag(pair))
        return self.roll_settlement(pair, landed, "following")

    def last_settlement_day(self, pair: str, year: int, month: int) -> date:
        """The last day of a month that ``pair`` can settle on."""
        first_next = date(year + month // 12, month % 12 + 1, 1)
        return self.roll_settlement(pair, first_next - timedelta(days=1), "preceding")

    def is_month_end(self, pair: str, d: date) -> bool:
        """Whether ``d`` is the last settlement day of its own month.

        This is the trigger for the end-of-month rule: a tenor added to a spot
        date that is the month's last good day settles on the *last good day*
        of the target month, not on the same day number rolled.  Without it a
        1M dealt off a 28-Feb spot settles 28-Mar while the market settles
        31-Mar, and the option's expiry is then a business day early.
        """
        return d == self.last_settlement_day(pair, d.year, d.month)

    def delivery_from_expiry(self, pair: str, expiry: date) -> date:
        """The value date of an option expiring on ``expiry``: the spot lag on."""
        expiry = expiry.date() if isinstance(expiry, datetime) else expiry
        landed = self._add_open_days(self.countries_for(pair), expiry, self.spot_lag(pair))
        return self.roll_settlement(pair, landed, "following")

    def expiry_from_delivery(self, pair: str, delivery: date) -> date:
        """The expiry of an option settling on ``delivery``: the spot lag back."""
        delivery = delivery.date() if isinstance(delivery, datetime) else delivery
        return self._add_open_days(self.countries_for(pair), delivery, -self.spot_lag(pair))

    def fx_dates(self, pair: str, tenor: str, today: date,
                 convention: str = "modified_following") -> FxDates:
        """The trade, spot, expiry and delivery dates a tenor resolves to.

        **The settlement date is derived first and the expiry comes from it**,
        which is the market construction and the reason this returns four
        dates rather than one.  Two branches, because the market has two:

        * A **day** tenor (``1D``, and so ``O/N``, ``T/N``, ``S/N``) is an
          *expiry* offset: the overnight expires on the next business day and
          settles from that day's own spot.  Adding calendar days to the spot
          date instead -- which is what this used to do -- collapses distinct
          tenors onto one date, because the two business days subtracted at
          the end swallow the weekend the addition just crossed.  Dealt on a
          Wednesday, ``1D`` and ``2D`` both came back Thursday.

        * A **week, month or year** tenor is a *delivery* offset: the tenor is
          added to the spot date by calendar arithmetic, adjusted modified
          following on the value-date calendars, and the expiry is the spot
          lag back from it on the pair's own.  Month and year tenors also take
          the **end-of-month rule** (:meth:`is_month_end`).

        ``today`` is the book's valuation date.  Nothing here reads the
        machine clock (§4).
        """
        pair = pair.upper()
        today = today.date() if isinstance(today, datetime) else today
        spot = self.spot_date(pair, today)
        n, unit = parse_tenor(tenor)
        label = normalise_tenor(tenor)

        if unit == "d" and float(n).is_integer():
            expiry = self._add_open_days(self.countries_for(pair), today, int(n))
            if int(n) == 0:
                expiry = self._roll_open(self.countries_for(pair), today, "following")
            delivery = self.delivery_from_expiry(pair, expiry)
            rule = (f"{int(n)} business day(s) after {today}, settling "
                    f"{self.spot_lag(pair)} business day(s) later")
            return FxDates(pair, label, today, spot, expiry, delivery, rule)

        target = add_tenor(spot, tenor)
        if unit in ("m", "y") and self.is_month_end(pair, spot):
            delivery = self.last_settlement_day(pair, target.year, target.month)
            rule = (f"end-of-month: {spot} is the last value date of its month, so {label} "
                    f"settles on the last value date of {target:%B %Y}")
        else:
            delivery = self.roll_settlement(pair, target, convention)
            rule = (f"{label} on the spot date {spot} is {target}, "
                    f"{convention.replace('_', ' ')} to {delivery}")
        expiry = self.expiry_from_delivery(pair, delivery)
        return FxDates(pair, label, today, spot, expiry, delivery, rule)

    def dates_for_expiry(self, pair: str, expiry: date, today: date) -> FxDates:
        """The same bundle for an expiry given as a **date** rather than a tenor.

        A desk types both into one box, and what comes out of it has to be the
        same shape either way -- above all the delivery date, because that is
        the date the forward is read on.
        """
        pair = pair.upper()
        today = today.date() if isinstance(today, datetime) else today
        expiry = expiry.date() if isinstance(expiry, datetime) else expiry
        return FxDates(pair, expiry.isoformat(), today, self.spot_date(pair, today),
                       expiry, self.delivery_from_expiry(pair, expiry),
                       f"expiry as given; settles {self.spot_lag(pair)} business day(s) later")

    def expiry_date(self, pair: str, tenor: str, today: date,
                    convention: str = "modified_following") -> date:
        """Option expiry for a tenor -- :meth:`fx_dates` for the whole bundle."""
        return self.fx_dates(pair, tenor, today, convention).expiry

    def delivery_date(self, pair: str, tenor: str, today: date,
                      convention: str = "modified_following") -> date:
        """Settlement date for a tenor -- what the forward to it is quoted to."""
        return self.fx_dates(pair, tenor, today, convention).delivery

    def expiry_years(self, pair: str, tenor: str, clock) -> float:
        """Years from the valuation instant to a tenor's **calendar** expiry.

        This is the one reading of "how long is a 1M option", and it goes
        through the calendar rather than through ``timeutil.tenor_to_years``:
        a 1M is however many days the pair's own holidays make it, and the
        volatility it is marked at has to be the volatility it is priced at.
        ``tenor_to_years`` survives as what it always was -- a sort key and a
        nominal length, needing no pair and no clock.
        """
        d = self.expiry_date(pair, tenor, clock.now.date())
        return clock.years_to(datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC))

    def add_overrides(self, country: str, dates, remove: bool = False) -> None:
        """Add (or suppress) calendar dates for a country at runtime."""
        target = self.removals if remove else self.overrides
        parsed = {
            (d.date() if isinstance(d, datetime) else d) if isinstance(d, (date, datetime))
            else parse_datetime(str(d)).date()
            for d in dates
        }
        target.setdefault(country.upper(), set()).update(parsed)

    def load_overrides_sheet(self, path: str | Path | None = None) -> int | None:
        """Load the workbook's ``HOLIDAYS`` tab: ``country, date, remove`` rows.

        Lunar-calendar holidays are published a year at a time and cannot be
        derived from any rule, so they are listed rather than computed, and
        they are listed **in the workbook** -- the same file as the marks
        whose expiries they move.  A tab that is not there is not an error:
        ``None`` says so, and a desk whose currencies are all rule-derived
        never needs one.

        Returns the number of rows applied.
        """
        from . import configsheets

        book = Path(path) if path else configsheets.default_workbook()
        rows = configsheets.read_rows(book, HOLIDAYS_SHEET, required=("country", "date"))
        if rows is None:
            return None
        count = 0
        for row in rows:
            country = row.text("country").upper()
            if not country:
                continue
            day = row.day("date")
            if day is None:
                raise ValueError(f"{HOLIDAYS_SHEET} row {row.number}: {country} has no date")
            self.add_overrides(country, [day], remove=row.flag("remove"))
            count += 1
        return count


DEFAULT_CALENDARS = CalendarSet()
