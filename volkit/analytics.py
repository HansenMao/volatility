"""Rolldown / carry and the indication pricer.

Both of these are broken in the legacy code on any current pandas:

* ``RV.calc`` assigned results with ``self.rv_matrix[col].iloc[i] = value``.
  Chained indexing like that writes into a temporary under copy-on-write, so
  the matrix silently stayed empty.
* ``run_indication`` opened an ``.xlsx`` with ``xlrd.open_workbook`` (xlrd 2.0
  dropped xlsx support), referenced an undefined ``FILE_PATH``, and wrote back
  with ``writer.book = book`` / ``writer.save()``, both removed in pandas 2.0.

The pricing logic is preserved; only the plumbing is rewritten.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import black
from .surface import VolSurface
from .timeutil import Clock, parse_datetime, tenor_to_years

DEFAULT_LADDER = (-0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20)


@dataclass
class ForwardCurve:
    """Outright forwards by tenor."""

    points: dict[str, float]

    @classmethod
    def from_excel(cls, path: str | Path, sheet: str, column: str = "fwd") -> "ForwardCurve":
        df = pd.read_excel(pd.ExcelFile(path), sheet, index_col=0)
        cols = {str(c).strip().lower(): c for c in df.columns}
        if column.lower() not in cols:
            raise ValueError(
                f"sheet {sheet!r} has no {column!r} column; found {list(df.columns)}"
            )
        series = df[cols[column.lower()]].dropna()
        return cls({str(k).strip(): float(v) for k, v in series.items()})

    def tenors(self) -> list[str]:
        return sorted(self.points, key=tenor_to_years)

    def __getitem__(self, tenor: str) -> float:
        try:
            return self.points[tenor]
        except KeyError:
            raise KeyError(f"no forward for tenor {tenor!r}; have {self.tenors()}") from None


@dataclass
class RollDown:
    """Vega and delta carry from rolling down the surface.

    For each pair of adjacent tenors the position is revalued at the shorter
    expiry with the strike held fixed, and the difference is annualised.
    """

    surface: VolSurface
    forwards: ForwardCurve
    ladder: tuple[float, ...] = DEFAULT_LADDER
    method: str | None = None

    def vol_rolldown(self, strike: float, t_near: float, t_far: float,
                     f_near: float, f_far: float) -> float:
        """Change in implied vol from rolling a fixed strike down the surface."""
        v_far = float(self.surface.vol(strike / f_far, t_far, self.method))
        v_near = float(self.surface.vol(strike / f_near, t_near, self.method))
        return v_near - v_far

    def pv_rolldown(self, strike: float, t_near: float, t_far: float,
                    f_near: float, f_far: float, *, measure: str = "vega") -> float:
        """Carry in premium terms.

        ``vega``  -- vol move valued at the average vega of the two points.
        ``vol``   -- the raw vol move.
        ``delta`` -- forward move valued at the average delta.
        """
        v_far = float(self.surface.vol(strike / f_far, t_far, self.method))
        v_near = float(self.surface.vol(strike / f_near, t_near, self.method))
        if measure == "vol":
            return v_near - v_far
        if measure == "vega":
            vega_far = float(black.vega(f_far, strike, v_far, t_far))
            vega_near = float(black.vega(f_near, strike, v_near, t_near))
            return 0.5 * (vega_far + vega_near) * (v_near - v_far)
        if measure == "delta":
            is_call = strike >= f_far
            d_far = float(black.delta(f_far, strike, v_far, t_far, is_call, self.surface.conv))
            d_near = float(black.delta(f_near, strike, v_near, t_near, is_call, self.surface.conv))
            return 0.5 * (d_far + d_near) * (f_near - f_far)
        raise ValueError(f"unknown measure {measure!r}; expected 'vega', 'vol' or 'delta'")

    def matrix(self, measure: str = "vega", annualise: bool = True) -> pd.DataFrame:
        """Rolldown for every strike in the ladder against every tenor step."""
        tenors = self.forwards.tenors()
        if len(tenors) < 2:
            raise ValueError(f"need at least 2 forward tenors to roll, got {tenors}")
        spot = self.forwards[tenors[0]]
        strikes = [spot * (1.0 + x) for x in self.ladder]
        rows, index = [], []
        for near_tenor, far_tenor in zip(tenors[:-1], tenors[1:]):
            t_near, t_far = tenor_to_years(near_tenor), tenor_to_years(far_tenor)
            f_near, f_far = self.forwards[near_tenor], self.forwards[far_tenor]
            scale = 1.0 / (t_far - t_near) if annualise and t_far > t_near else 1.0
            rows.append([
                self.pv_rolldown(K, t_near, t_far, f_near, f_far, measure=measure) * scale
                for K in strikes
            ])
            index.append(far_tenor)
        return pd.DataFrame(rows, index=index,
                            columns=[f"{x:+.0%}" for x in self.ladder])


@dataclass
class Indication:
    """One row of an indication request."""

    pair: str
    expiry: date | str
    forward_tenor: str
    strike_offset: float          # in pips against spot
    option_type: str = "A"        # C, P, or A for automatic
    rounding: int | None = None


@dataclass
class IndicationResult:
    pair: str
    expiry: date
    strike: float
    forward: float
    vol: float
    premium: float
    is_call: bool


def price_indications(
    book,
    rows: list[Indication],
    spots: dict[str, float],
    swaps: dict[tuple[str, str], float],
    spreads: dict[tuple[str, str], float] | None = None,
    *,
    pip: float = 10000.0,
    method: str = "SVI",
) -> list[IndicationResult]:
    """Price a list of indications off the book.

    ``swaps`` and ``spreads`` are keyed by ``(pair, forward_tenor)``; the swap
    is in pips and the spread is in vol points, matching the legacy sheet.
    """
    spreads = spreads or {}
    out: list[IndicationResult] = []
    for row in rows:
        surface = book[row.pair]
        if row.pair not in spots:
            raise KeyError(f"no spot supplied for {row.pair!r}")
        spot = spots[row.pair]
        key = (row.pair, row.forward_tenor.upper())
        if key not in swaps:
            raise KeyError(f"no forward swap supplied for {row.pair} {row.forward_tenor}")
        forward = spot + swaps[key] / pip
        strike = spot + row.strike_offset / pip
        if row.rounding is not None:
            strike = round(strike, row.rounding)

        expiry = row.expiry
        if isinstance(expiry, str):
            expiry = book.calendars.expiry_date(row.pair, expiry, book.clock.now.date()) \
                if len(expiry) <= 4 else parse_datetime(expiry).date()

        vol = float(surface.vol(strike / forward, expiry, method)) - spreads.get(key, 0.0)
        t = book.clock.years_to(datetime.combine(expiry, datetime.min.time()).replace(
            tzinfo=book.clock.now.tzinfo))
        kind = row.option_type.upper()
        is_call = kind == "C" if kind in ("C", "P") else strike > forward
        premium = float(black.price(forward, strike, vol, t, is_call, foreign_premium=True))
        out.append(IndicationResult(row.pair, expiry, strike, forward, vol, premium, is_call))
    return out


def indications_to_frame(results: list[IndicationResult]) -> pd.DataFrame:
    return pd.DataFrame([{
        "pair": r.pair,
        "expiry": r.expiry.strftime("%d-%b-%y"),
        "strike": r.strike,
        "forward": r.forward,
        "vol": r.vol * 100.0,
        "premium_pct": r.premium * 100.0,
        "type": "C" if r.is_call else "P",
    } for r in results])


def write_frame(df: pd.DataFrame, path: str | Path, sheet: str,
                startrow: int = 1, startcol: int = 6) -> None:
    """Write a frame into an existing workbook without destroying other sheets.

    The legacy code used the ``writer.book = book`` / ``writer.save()`` idiom,
    which pandas removed in 2.0.  ``mode='a'`` with ``if_sheet_exists='overlay'``
    is the supported replacement.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"workbook not found: {path}")
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="overlay") as writer:
        df.to_excel(writer, sheet_name=sheet, header=False, index=False,
                    startrow=startrow, startcol=startcol)
