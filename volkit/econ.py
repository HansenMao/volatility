"""Scheduled economic events, so an event schedule can be populated rather than typed.

Two sources are combined:

* **Rules** for releases whose timing is defined by a rule that does not
  change -- US non-farm payrolls is the first Friday of the month at 08:30 New
  York, and that has been true for decades.  These are generated for any year.
* **A dated table** for central bank decisions, whose dates are set by
  committee and published a year or two ahead.  These cannot be derived, so
  they live in an editable CSV.

Release times are stored in the *local* time zone of the releasing body and
converted through ``zoneinfo``, because the UTC time of a US release moves by
an hour twice a year: 08:30 New York is 13:30 UTC in winter and 12:30 UTC in
summer.  Putting an event on the wrong side of a volatility-day boundary is
exactly the sort of error a hand-maintained UTC list produces.

Every generated event is a *suggestion* carrying a default bump.  Nothing is
applied to a curve until the user accepts it, and the ``approximate`` flag
marks the ones whose date is a rule of thumb rather than a published date.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .timeutil import UTC, parse_datetime

# Where each releasing body publishes from, and at what local time.
RELEASE_TIMES: dict[str, tuple[str, str, int, int]] = {
    # key: (currency, tz, hour, minute)
    "FOMC":    ("USD", "America/New_York", 14, 0),
    "NFP":     ("USD", "America/New_York", 8, 30),
    "US CPI":  ("USD", "America/New_York", 8, 30),
    "US GDP":  ("USD", "America/New_York", 8, 30),
    "US PCE":  ("USD", "America/New_York", 8, 30),
    "ECB":     ("EUR", "Europe/Berlin", 14, 15),
    "EZ CPI":  ("EUR", "Europe/Berlin", 11, 0),
    "BOE":     ("GBP", "Europe/London", 12, 0),
    "UK CPI":  ("GBP", "Europe/London", 7, 0),
    "BOJ":     ("JPY", "Asia/Tokyo", 12, 0),
    "RBA":     ("AUD", "Australia/Sydney", 14, 30),
    "RBNZ":    ("NZD", "Pacific/Auckland", 14, 0),
    "BOC":     ("CAD", "America/Toronto", 9, 45),
    "SNB":     ("CHF", "Europe/Zurich", 9, 30),
    "CN PMI":  ("CNH", "Asia/Shanghai", 9, 30),
}

# Starting point for the volatility bump, in vol points, by event.  These are
# defaults to be marked over, not estimates of anything.
DEFAULT_BUMPS: dict[str, float] = {
    "FOMC": 1.5, "NFP": 1.5, "US CPI": 1.5, "US GDP": 0.5, "US PCE": 0.75,
    "ECB": 1.0, "EZ CPI": 0.5, "BOE": 1.0, "UK CPI": 0.75, "BOJ": 1.5,
    "RBA": 1.0, "RBNZ": 1.0, "BOC": 0.75, "SNB": 0.75, "CN PMI": 0.5,
}


@dataclass
class EconEvent:
    """One scheduled release."""

    name: str
    currency: str
    when: datetime            # UTC
    bump: float               # vol points
    approximate: bool = False
    source: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} {self.when:%d%b %H:%M}Z"

    def as_dict(self) -> dict:
        return {"name": self.name, "currency": self.currency,
                "when": self.when.isoformat(), "bump": self.bump,
                "approximate": self.approximate, "source": self.source,
                "label": self.label}


def _localise(key: str, day: date) -> datetime:
    """Attach the release's local time and convert to UTC (DST-aware)."""
    _, tz, hh, mm = RELEASE_TIMES[key]
    local = datetime.combine(day, time(hh, mm)).replace(tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def generate_nfp(year: int) -> list[EconEvent]:
    """US non-farm payrolls: first Friday of the month, 08:30 New York.

    When that Friday is a US holiday -- 1 January falls on a Friday every few
    years -- the BLS releases the following Friday instead, so the rule shifts
    a week rather than putting an event on a day the market is shut.
    """
    from .calendars import DEFAULT_CALENDARS
    out = []
    for month in range(1, 13):
        day = nth_weekday(year, month, 4, 1)
        shifted = False
        if DEFAULT_CALENDARS.is_holiday("USD", day):
            day = day + timedelta(days=7)
            shifted = True
        out.append(EconEvent(
            "NFP", "USD", _localise("NFP", day), DEFAULT_BUMPS["NFP"],
            source="rule: first Friday" + (" (shifted a week: holiday)" if shifted else ""),
        ))
    return out


def generate_us_cpi(year: int) -> list[EconEvent]:
    """US CPI: no fixed rule -- BLS publishes mid-month, usually the second
    full week.  The second Wednesday is a decent proxy but is genuinely
    approximate, so it is flagged and should be checked against the BLS
    schedule before it is relied on."""
    return [EconEvent("US CPI", "USD", _localise("US CPI", nth_weekday(year, m, 2, 2)),
                      DEFAULT_BUMPS["US CPI"], approximate=True,
                      source="rule of thumb: second Wednesday, verify against BLS")
            for m in range(1, 13)]


RULE_GENERATORS = {"NFP": generate_nfp, "US CPI": generate_us_cpi}


@dataclass
class EconCalendar:
    """Scheduled events from a dated table plus rule-based generators."""

    dated: list[tuple[str, date]] = field(default_factory=list)
    use_rules: bool = True
    rules: tuple[str, ...] = ("NFP",)   # US CPI is off by default: date is a guess
    source: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None, **kw) -> "EconCalendar":
        """Read the dated central-bank table.  A missing file is not an error."""
        cal = cls(**kw)
        if path is None:
            path = Path(__file__).parent / "data" / "econ_events.csv"
        path = Path(path)
        cal.source = str(path)
        if not path.exists():
            return cal
        with path.open() as fh:
            for row in csv.reader(fh):
                if not row or row[0].lstrip().startswith("#"):
                    continue
                if len(row) < 2:
                    raise ValueError(f"bad econ event row {row!r}: expected 'EVENT,YYYY-MM-DD'")
                key = row[0].strip().upper()
                if key not in RELEASE_TIMES:
                    raise ValueError(
                        f"unknown event {key!r} in {path}; known: {sorted(RELEASE_TIMES)}"
                    )
                cal.dated.append((key, parse_datetime(row[1].strip()).date()))
        return cal

    def all_events(self, start: datetime, end: datetime) -> list[EconEvent]:
        """Every known event in the window, sorted."""
        out: list[EconEvent] = []
        for key, day in self.dated:
            when = _localise(key, day)
            if start <= when <= end:
                out.append(EconEvent(key, RELEASE_TIMES[key][0], when,
                                     DEFAULT_BUMPS.get(key, 1.0), source="published calendar"))
        if self.use_rules:
            for year in range(start.year, end.year + 1):
                for rule in self.rules:
                    gen = RULE_GENERATORS.get(rule)
                    if gen is None:
                        continue
                    out.extend(e for e in gen(year) if start <= e.when <= end)
        out.sort(key=lambda e: e.when)
        return out

    def for_pair(self, pair: str, start: datetime, end: datetime,
                 *, include_approximate: bool = True) -> list[EconEvent]:
        """Events whose currency appears in the pair.

        A USDJPY event schedule wants the Fed and the BoJ, not the ECB.
        """
        pair = pair.upper()
        wanted = {pair[:3], pair[3:6]}
        # CNY and CNH share a calendar for this purpose.
        if "CNH" in wanted or "CNY" in wanted:
            wanted |= {"CNH", "CNY"}
        return [e for e in self.all_events(start, end)
                if e.currency in wanted and (include_approximate or not e.approximate)]

    def known_events(self) -> list[str]:
        return sorted(RELEASE_TIMES)
