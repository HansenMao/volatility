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

from . import paths
from .events import leg_weights, pair_bump
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

# Starting point for the volatility bump, in vol points, by event, on the
# currency that releases it.  These are defaults to be marked over, not
# estimates of anything.
DEFAULT_BUMPS: dict[str, float] = {
    "FOMC": 1.5, "NFP": 1.5, "US CPI": 1.5, "US GDP": 0.5, "US PCE": 0.75,
    "ECB": 1.0, "EZ CPI": 0.5, "BOE": 1.0, "UK CPI": 0.75, "BOJ": 1.5,
    "RBA": 1.0, "RBNZ": 1.0, "BOC": 0.75, "SNB": 0.75, "CN PMI": 0.5,
}

# The weight an event puts on each *currency*, in vol points: the releasing
# currency's from DEFAULT_BUMPS, any other currency's from the weights file.
# A pair's bump is its two legs' weights superposed (``events.superpose``)
# plus whatever the pair marks on top, so FOMC weighted 1.5 on USD and 0.3
# on JPY is 1.8 on USDJPY and 1.5 on EURUSD before any adjustment.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    key: {RELEASE_TIMES[key][0]: bump} for key, bump in DEFAULT_BUMPS.items()
}


def weights_path(events_path: str | Path | None = None) -> Path:
    """The weights file beside the dated table it extends."""
    base = Path(events_path) if events_path is not None else Path(__file__).parent / "data" / "econ_events.csv"
    return base.with_name("event_weights.csv")


def load_weights(path: str | Path | None = None) -> dict[str, dict[str, float]]:
    """``EVENT,CCY,WEIGHT`` rows over the defaults.  A missing file is the defaults.

    A row names an event ``RELEASE_TIMES`` knows and a three-letter currency;
    the weight is in vol points and may be zero, which *removes* a default.
    Anything else is refused with the row, because a weight that was typed
    and silently not applied is a bump nobody can account for.
    """
    table = {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()}
    if path is None:
        path = weights_path()
    path = Path(path)
    if not path.exists():
        return table
    with paths.open_text(path) as fh:
        for n, row in enumerate(csv.reader(fh), start=1):
            if not row or row[0].lstrip().startswith("#"):
                continue
            if len(row) < 3:
                raise ValueError(f"{path} line {n}: expected 'EVENT,CCY,WEIGHT', got {row!r}")
            key = row[0].strip().upper()
            if key not in RELEASE_TIMES:
                raise ValueError(
                    f"{path} line {n}: unknown event {key!r}; known: {sorted(RELEASE_TIMES)}"
                )
            ccy = row[1].strip().upper()
            if len(ccy) != 3 or not ccy.isalpha():
                raise ValueError(f"{path} line {n}: {row[1]!r} is not a currency")
            try:
                w = float(row[2])
            except ValueError:
                raise ValueError(f"{path} line {n}: weight {row[2]!r} is not a number") from None
            table.setdefault(key, {})[ccy] = w
    return table


@dataclass
class EconEvent:
    """One scheduled release."""

    name: str
    currency: str             # the releasing currency
    when: datetime            # UTC
    bump: float               # vol points: on the releasing currency, or,
                              # once ``for_pair`` has placed it, on the pair
    approximate: bool = False
    source: str = ""
    #: Per-currency weights in vol points.  ``all_events`` carries the whole
    #: table row; ``for_pair`` narrows it to the pair's two legs.
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} {self.when:%d%b %H:%M}Z"

    def bump_for(self, pair: str) -> float:
        """The pair's bump before any adjustment: both legs superposed, in points."""
        # ``leg_weights`` and ``superpose`` are unit-agnostic, so the points
        # stay points and no decimal round trip lands a digit off.
        return pair_bump(self.weights, pair)

    def on_pair(self, pair: str) -> "EconEvent":
        """This event as a pair sees it: its legs' weights and their total."""
        return EconEvent(self.name, self.currency, self.when, self.bump_for(pair),
                         self.approximate, self.source, leg_weights(self.weights, pair))

    def as_dict(self) -> dict:
        return {"name": self.name, "currency": self.currency,
                "when": self.when.isoformat(), "bump": self.bump,
                "approximate": self.approximate, "source": self.source,
                "label": self.label, "weights": dict(self.weights)}


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
            weights=dict(DEFAULT_WEIGHTS["NFP"]),
        ))
    return out


def generate_us_cpi(year: int) -> list[EconEvent]:
    """US CPI: no fixed rule -- BLS publishes mid-month, usually the second
    full week.  The second Wednesday is a decent proxy but is genuinely
    approximate, so it is flagged and should be checked against the BLS
    schedule before it is relied on."""
    return [EconEvent("US CPI", "USD", _localise("US CPI", nth_weekday(year, m, 2, 2)),
                      DEFAULT_BUMPS["US CPI"], approximate=True,
                      source="rule of thumb: second Wednesday, verify against BLS",
                      weights=dict(DEFAULT_WEIGHTS["US CPI"]))
            for m in range(1, 13)]


RULE_GENERATORS = {"NFP": generate_nfp, "US CPI": generate_us_cpi}


@dataclass
class EconCalendar:
    """Scheduled events from a dated table plus rule-based generators."""

    dated: list[tuple[str, date]] = field(default_factory=list)
    use_rules: bool = True
    rules: tuple[str, ...] = ("NFP",)   # US CPI is off by default: date is a guess
    source: str = ""
    #: event -> currency -> vol points.  The one table every suggestion reads
    #: its weights from; ``set_weight`` marks it for the session.
    weights: dict[str, dict[str, float]] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_WEIGHTS.items()})
    weights_source: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None, *, weights: str | Path | None = None,
             **kw) -> "EconCalendar":
        """Read the dated central-bank table and the weights beside it.

        A missing file is not an error, either of them.
        """
        cal = cls(**kw)
        if path is None:
            path = Path(__file__).parent / "data" / "econ_events.csv"
        path = Path(path)
        cal.source = str(path)
        wpath = Path(weights) if weights is not None else weights_path(path)
        cal.weights = load_weights(wpath)
        cal.weights_source = str(wpath) if wpath.exists() else ""
        if not path.exists():
            return cal
        with paths.open_text(path) as fh:
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

    def set_weight(self, event: str, currency: str, weight: float) -> None:
        """Mark one event's weight on one currency, in vol points, for this session."""
        key = event.strip().upper()
        if key not in RELEASE_TIMES:
            raise ValueError(f"unknown event {key!r}; known: {sorted(RELEASE_TIMES)}")
        ccy = currency.strip().upper()
        if len(ccy) != 3 or not ccy.isalpha():
            raise ValueError(f"{currency!r} is not a currency")
        self.weights.setdefault(key, {})[ccy] = float(weight)

    def set_weights(self, table: dict) -> None:
        """Replace the whole table, validated first so a bad cell leaves it untouched."""
        fresh = EconCalendar()
        for key, row in (table or {}).items():
            if not isinstance(row, dict):
                raise ValueError(f"{key!r}: weights must be a currency -> number object")
            for ccy, w in row.items():
                try:
                    fresh.set_weight(str(key), str(ccy), float(w))
                except (TypeError, ValueError) as exc:
                    if "not a number" in str(exc) or "could not convert" in str(exc):
                        raise ValueError(f"{key} {ccy}: {w!r} is not a number") from None
                    raise
        self.weights = fresh.weights

    def table(self) -> dict[str, dict[str, float]]:
        """Every known event's weights, the releasing currency's first."""
        return {key: self.weights_for(key) for key in self.known_events()}

    def weights_for(self, key: str) -> dict[str, float]:
        """An event's per-currency weights, the releasing currency's first."""
        row = dict(self.weights.get(key) or {})
        ccy = RELEASE_TIMES[key][0]
        row.setdefault(ccy, DEFAULT_BUMPS.get(key, 1.0))
        return {ccy: row.pop(ccy), **row}

    def all_events(self, start: datetime, end: datetime) -> list[EconEvent]:
        """Every known event in the window, sorted, each carrying its weights."""
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
        for e in out:
            # The calendar's table is the one place a weight is read from, so
            # a weight marked with ``set_weight`` reaches a rule-generated
            # event as well as a dated one.
            e.weights = self.weights_for(e.name)
            e.bump = e.weights[e.currency]
        out.sort(key=lambda e: e.when)
        return out

    def for_pair(self, pair: str, start: datetime, end: datetime,
                 *, include_approximate: bool = True) -> list[EconEvent]:
        """Events weighted on either of the pair's currencies, as the pair sees them.

        A USDJPY event schedule wants the Fed and the BoJ, not the ECB -- and
        it wants the ECB the day a desk gives the ECB a weight on JPY.  Each
        event comes back with its two legs' weights and their superposition
        as its bump; the pair's own adjustment is not known here.
        """
        out = []
        for e in self.all_events(start, end):
            if not include_approximate and e.approximate:
                continue
            placed = e.on_pair(pair)
            if any(w != 0.0 for w in placed.weights.values()):
                out.append(placed)
        return out

    def known_events(self) -> list[str]:
        return sorted(RELEASE_TIMES)
