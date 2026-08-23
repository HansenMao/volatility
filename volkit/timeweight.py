"""Intraday, weekend and holiday volatility weighting.

The legacy ``CVol.getTimeWeight`` was a scalar function called from inside
``scipy.integrate.quad``, and it encoded holiday effects as a hand-tuned
6 x 24 matrix with one row per *combination* of closed centres (LDN, NY, TOK,
LDN+NY, NY+TOK, LDN+TOK).  That does not extend: adding a Chinese or Hong Kong
calendar would need new rows, and the rows were not consistent with each other.

This version models the three trading centres by their share of each hour's
activity, so any combination of closed calendars -- including ones the legacy
matrix had no row for -- is computed rather than enumerated.

The whole weight profile is precomputed onto hourly buckets once and then read
with ``searchsorted``, which is what makes the term-structure integration
vectorisable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .calendars import CalendarSet, DEFAULT_CALENDARS
from .timeutil import Clock, SECONDS_IN_YEAR, UTC

# Empirical intraday activity profile by UTC hour, carried over unchanged from
# the legacy model -- this is the user's calibration, not something to invent.
DEFAULT_HOURLY_WEIGHT: tuple[float, ...] = (
    0.83, 0.95, 1.01, 0.79, 0.67, 0.78, 0.85, 1.35, 1.20, 1.13, 0.99, 1.05,
    1.17, 1.24, 1.36, 1.55, 1.37, 1.20, 0.92, 0.88, 0.75, 0.68, 0.65, 0.63,
)

# Trading hours by centre, in UTC.  Together these cover all 24 hours.
DEFAULT_SESSION_HOURS: dict[str, tuple[int, ...]] = {
    "TOK": (22, 23, 0, 1, 2, 3, 4, 5, 6),
    "LDN": (7, 8, 9, 10, 11, 12, 13, 14, 15, 16),
    "NYC": (13, 14, 15, 16, 17, 18, 19, 20, 21),
}

# Which trading centre a national calendar shuts down.
DEFAULT_CENTRE_OF_COUNTRY: dict[str, str] = {
    "US": "NYC", "CA": "NYC",
    "UK": "LDN", "EU": "LDN",
    "JP": "TOK", "CN": "TOK", "HK": "TOK", "AU": "TOK", "NZ": "TOK", "SG": "TOK",
}


def session_shares(session_hours: dict[str, tuple[int, ...]]) -> dict[str, np.ndarray]:
    """Each centre's share of each hour, normalised so the shares sum to one.

    An hour worked by two centres gives each a half share, so shutting one of
    them removes half that hour's activity -- and shutting all of them removes
    all of it, which is what makes the all-holiday case collapse cleanly to
    ``holiday_weight``.
    """
    active = {name: np.isin(np.arange(24), hours).astype(float) for name, hours in session_hours.items()}
    total = np.sum(list(active.values()), axis=0)
    total[total == 0] = 1.0
    return {name: a / total for name, a in active.items()}


@dataclass
class TimeWeighting:
    """Piecewise-constant hourly volatility weight for one currency pair."""

    pair: str
    calendars: CalendarSet = field(default_factory=lambda: DEFAULT_CALENDARS)
    hourly_weight: tuple[float, ...] = DEFAULT_HOURLY_WEIGHT
    weekend_weight: float = 0.35
    holiday_weight: float = 0.5
    session_hours: dict[str, tuple[int, ...]] = field(default_factory=lambda: dict(DEFAULT_SESSION_HOURS))
    centre_of_country: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CENTRE_OF_COUNTRY))
    # The FX week is defined by New York, not by UTC: it closes Friday 17:00
    # New York and reopens Sunday 17:00 New York.  In UTC that is 22:00 in
    # winter and 21:00 in summer, so a fixed UTC hour is an hour wrong for
    # roughly half the year -- the model would treat the market as open for an
    # hour after it had shut, every summer Friday.
    week_tz: str = "America/New_York"
    week_close_weekday: int = 4   # Friday, in the week time zone
    week_close_hour: int = 17
    week_open_weekday: int = 6    # Sunday
    week_open_hour: int = 17
    dst_aware_week: bool = True
    # Fixed-UTC fallback, used when dst_aware_week is off.
    week_close_hour_utc: int = 22
    week_open_hour_utc: int = 22
    enabled: bool = True

    def __post_init__(self) -> None:
        if len(self.hourly_weight) != 24:
            raise ValueError(f"hourly_weight must have 24 entries, got {len(self.hourly_weight)}")
        self._shares = session_shares(self.session_hours)
        self._cache: dict[tuple[float, float], tuple[np.ndarray, np.ndarray]] = {}

    def is_closed(self, dt: datetime) -> bool:
        """True inside the weekly market closure."""
        if self.dst_aware_week:
            local = dt.astimezone(ZoneInfo(self.week_tz))
            wd, hr = local.weekday(), local.hour
            close_h, open_h = self.week_close_hour, self.week_open_hour
        else:
            wd, hr = dt.weekday(), dt.hour
            close_h, open_h = self.week_close_hour_utc, self.week_open_hour_utc
        after_close = wd > self.week_close_weekday or (wd == self.week_close_weekday and hr >= close_h)
        before_open = wd < self.week_open_weekday or (wd == self.week_open_weekday and hr < open_h)
        return after_close and before_open

    def weight_at_datetime(self, dt: datetime) -> float:
        """Weight for the hour containing ``dt``."""
        if not self.enabled:
            return 1.0
        if self.is_closed(dt):
            return self.weekend_weight
        w = self.hourly_weight[dt.hour]
        closed = self.calendars.holiday_countries(self.pair, dt)
        if not closed:
            return w
        centres = {self.centre_of_country.get(c) for c in closed} - {None}
        share = sum(float(self._shares[c][dt.hour]) for c in centres if c in self._shares)
        return w * (1.0 - min(share, 1.0) * (1.0 - self.holiday_weight))

    def profile(self, clock: Clock, t_end: float, t_start: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Hourly bucket edges (in years) and the weight inside each bucket.

        Returned edges are also the breakpoints the integrator splits on, so
        every panel it sees is smooth.
        """
        key = (round(t_start, 12), round(t_end, 12))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        start_dt = clock.datetime_from_years(min(t_start, t_end)).replace(minute=0, second=0, microsecond=0)
        end_dt = clock.datetime_from_years(max(t_start, t_end))
        n_hours = max(int((end_dt - start_dt).total_seconds() // 3600) + 2, 2)
        hours = [start_dt + timedelta(hours=i) for i in range(n_hours + 1)]
        edges = np.array([(h - clock.now).total_seconds() / SECONDS_IN_YEAR for h in hours])
        weights = np.array([self.weight_at_datetime(h) for h in hours[:-1]])
        self._cache[key] = (edges, weights)
        return edges, weights

    def weights_on(self, clock: Clock, t: np.ndarray, t_end: float, t_start: float = 0.0) -> np.ndarray:
        """Vectorised weight lookup for an array of year fractions."""
        if not self.enabled:
            return np.ones_like(np.asarray(t, dtype=float))
        edges, weights = self.profile(clock, t_end, t_start)
        idx = np.clip(np.searchsorted(edges, np.asarray(t, dtype=float), side="right") - 1, 0, len(weights) - 1)
        return weights[idx]

    def clear_cache(self) -> None:
        self._cache.clear()
