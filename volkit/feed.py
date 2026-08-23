"""Spot and forward points from a file, with interpolation between standard tenors.

A desk feed publishes spot plus swap points at the standard tenors.  An option
rarely expires exactly on one of them, so the tool interpolates.

Swap points are interpolated linearly in time, which is the market convention
for the short end and is what a broker screen implies between quoted pillars.
Requests outside the quoted range are answered by holding the nearest pillar
flat and are flagged as extrapolated rather than silently trended -- running a
linear fit off the end of a swap curve is how a 5y forward ends up negative.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np

from .timeutil import TenorError, parse_tenor, tenor_to_years

# Term-currency pip divisor: JPY-quoted pairs move in 0.01, most others 0.0001.
PIP_DIVISORS: dict[str, float] = {"JPY": 100.0, "KRW": 1.0, "CLP": 1.0, "HUF": 100.0}
DEFAULT_PIP = 10000.0

SPOT_KEYS = {"spot", "s/n", "sn", "0d"}


def pip_divisor(pair: str) -> float:
    return PIP_DIVISORS.get(pair[3:6].upper(), DEFAULT_PIP)


class FeedError(ValueError):
    """Raised when the feed file cannot be interpreted."""


@dataclass
class PairFeed:
    """Spot and a term structure of forward points for one pair."""

    pair: str
    spot: float
    tenors: list[str] = field(default_factory=list)
    points: list[float] = field(default_factory=list)
    pip: float = DEFAULT_PIP

    def __post_init__(self) -> None:
        order = np.argsort([tenor_to_years(x) for x in self.tenors])
        self.tenors = [self.tenors[i] for i in order]
        self.points = [self.points[i] for i in order]
        self._ts = np.array([tenor_to_years(x) for x in self.tenors])
        self._ps = np.array(self.points, dtype=float)

    def forward_points(self, t: float) -> tuple[float, bool]:
        """Interpolated swap points at ``t`` years, and whether it extrapolated."""
        if self._ts.size == 0:
            return 0.0, False
        if self._ts.size == 1:
            return float(self._ps[0]), bool(t != float(self._ts[0]))
        outside = bool(t < self._ts[0] - 1e-12 or t > self._ts[-1] + 1e-12)
        if t <= self._ts[0]:
            # Points go to zero at zero time, so scale the front pillar down
            # rather than holding it flat across the very short end.
            if t >= 0:
                return float(self._ps[0] * t / self._ts[0]), False
            return float(self._ps[0]), True
        if t >= self._ts[-1]:
            return float(self._ps[-1]), True
        return float(np.interp(t, self._ts, self._ps)), bool(outside)

    def forward(self, t: float) -> tuple[float, bool]:
        pts, extrap = self.forward_points(t)
        return self.spot + pts / self.pip, extrap


@dataclass
class MarketFeed:
    """Spot and forward points for every pair in a feed file."""

    pairs: dict[str, PairFeed] = field(default_factory=dict)
    source: str = ""
    asof: str = ""
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "MarketFeed":
        """Read a ``pair,tenor,value`` CSV.

        ``tenor`` is ``SPOT`` for the spot rate, otherwise a tenor whose value
        is the forward points for that pillar.
        """
        path = Path(path)
        if not path.exists():
            raise FeedError(f"feed file not found: {path}")
        feed = cls(source=str(path))
        spots: dict[str, float] = {}
        pillars: dict[str, list[tuple[str, float]]] = {}
        for lineno, row in enumerate(csv.reader(path.open()), start=1):
            if not row or row[0].lstrip().startswith("#"):
                if row and "asof" in row[0].lower():
                    feed.asof = row[0].split(":", 1)[-1].strip()
                continue
            if len(row) < 3:
                feed.problems.append(f"line {lineno}: expected 'pair,tenor,value', got {row!r}")
                continue
            pair, tenor, raw = row[0].strip().upper(), row[1].strip(), row[2].strip()
            try:
                value = float(raw)
            except ValueError:
                feed.problems.append(f"line {lineno}: {pair} {tenor} value {raw!r} is not a number")
                continue
            if tenor.lower() in SPOT_KEYS:
                if value <= 0:
                    feed.problems.append(f"line {lineno}: {pair} spot must be positive, got {value}")
                    continue
                spots[pair] = value
                continue
            try:
                parse_tenor(tenor)
            except TenorError as exc:
                feed.problems.append(f"line {lineno}: {exc}")
                continue
            pillars.setdefault(pair, []).append((tenor, value))

        for pair, spot in spots.items():
            rows = pillars.pop(pair, [])
            feed.pairs[pair] = PairFeed(pair=pair, spot=spot,
                                        tenors=[r[0] for r in rows],
                                        points=[r[1] for r in rows],
                                        pip=pip_divisor(pair))
        for pair in pillars:
            feed.problems.append(f"{pair}: forward points supplied but no SPOT row")
        return feed

    def __contains__(self, pair: str) -> bool:
        return pair.upper() in self.pairs

    def quote(self, pair: str, t: float) -> dict:
        """Spot, points and forward for a pair at ``t`` years."""
        key = pair.upper()
        if key not in self.pairs:
            raise FeedError(f"no feed for {pair!r}; have {sorted(self.pairs)}")
        pf = self.pairs[key]
        points, extrapolated = pf.forward_points(t)
        return {"pair": key, "spot": pf.spot, "points": points, "pip": pf.pip,
                "forward": pf.spot + points / pf.pip, "extrapolated": extrapolated,
                "pillars": pf.tenors}

    def summary(self) -> list[dict]:
        return [{"pair": p.pair, "spot": p.spot, "pillars": len(p.tenors),
                 "range": f"{p.tenors[0]}–{p.tenors[-1]}" if p.tenors else "—"}
                for p in self.pairs.values()]
