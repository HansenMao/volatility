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

from . import black, moments
from .cross import CrossAtmCurve
from .numerics import ConvergenceError
from .history import Realized, SeriesStats, implied_stats, realized
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


# ===========================================================================
# The analysis screen
# ===========================================================================
#
# Four questions, asked of the whole tenor grid at once rather than of one
# expiry.  The first two need the marks and a forward curve; the third also
# needs a history of what the market did; the fourth needs a cross and its two
# legs.  Each section is built independently and reports its own reason for
# being unavailable, so a missing feed does not take the realized statistics
# down with it.

TARGETS: dict[str, str] = {
    "atm": "at-the-money",
    "25dc": "25 delta call", "25dp": "25 delta put",
    "10dc": "10 delta call", "10dp": "10 delta put",
    "rr25": "25 delta risk reversal", "fly25": "25 delta butterfly",
    "rr10": "10 delta risk reversal", "fly10": "10 delta butterfly",
}
COMBINATIONS = ("rr25", "fly25", "rr10", "fly10")

# A surface cannot quote an expiry inside today's volatility day -- there are
# no whole volatility days in it, so the ATM comes back zero and Black rejects
# it (see the known limitations in MIGRATION.md).  Rolling a tenor to within
# that window is the same problem arriving from the other direction, so it is
# caught here with a message that says which horizon caused it rather than
# surfacing as "ATM volatility is zero".
MIN_ROLLED_DAYS = 2.0


def _target_legs(target: str) -> list[tuple[float, float, bool]]:
    """A target as signed delta legs: ``(weight, delta, is_call)``.

    The at-the-money leg is marked with a delta of zero and resolved against
    the delta-neutral straddle strike, which is where this book quotes it.
    """
    t = target.lower()
    if t == "atm":
        return [(1.0, 0.0, True)]
    if t in ("25dc", "10dc", "25dp", "10dp"):
        d = float(t[:2]) / 100.0
        return [(1.0, d, t.endswith("c"))]
    if t in ("rr25", "rr10"):
        d = float(t[2:]) / 100.0
        return [(1.0, d, True), (-1.0, d, False)]
    if t in ("fly25", "fly10"):
        d = float(t[3:]) / 100.0
        return [(0.5, d, True), (0.5, d, False), (-1.0, 0.0, True)]
    raise ValueError(f"unknown target {target!r}; expected one of {sorted(TARGETS)}")


def _forward_at(book, pair: str, t: float) -> tuple[float, bool, str]:
    """Outright forward at ``t`` years, and whether it really came from a feed."""
    feed = getattr(book, "feed", None)
    if feed is None or pair.upper() not in getattr(feed, "pairs", {}):
        return 1.0, False, "no forward feed for this pair"
    q = feed.quote(pair, t)
    note = ""
    if q["extrapolated"]:
        note = f"the forward at {t:.4f}y is outside the quoted pillars and was held flat"
    return float(q["forward"]), True, note


@dataclass(frozen=True)
class CarryRow:
    """Carry and rolldown of one tenor, for one target."""

    tenor: str
    t: float
    t_rolled: float
    expiry: str
    forward: float
    forward_rolled: float
    strike: float                 # absolute; equals K/F when there is no feed
    level: float                  # the target's volatility (or spread) today
    level_rolled: float
    roll: float                   # level_rolled - level, over the horizon
    roll_term: float              # the part from the term structure alone
    roll_smile: float             # the part from the forward moving under the strike
    roll_annual: float
    atm: float
    ratio_atm: float
    ratio_target: float | None
    vega: float | None
    pnl: float | None
    forward_carry: float          # forward_rolled - forward, in price terms
    warnings: tuple[str, ...] = ()


def carry_table(book, pair: str, *, horizon_days: float = 30.0, target: str = "atm",
                method: str | None = None, cut: str = "NY",
                tenors=None) -> list[CarryRow]:
    """Roll every tenor down the surface and report what it costs to hold.

    The position is revalued at a **fixed absolute strike** after the horizon,
    which is the only revaluation a trader can actually run: the option you
    own keeps its strike while both the maturity and the forward move under
    it.  The result splits into the slide along the term structure (same
    moneyness, shorter maturity) and the slide across the smile (same
    maturity, forward moved) so the forward curve's contribution is separable
    rather than buried in one number.
    """
    if target.lower() not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {sorted(TARGETS)}")
    surface = book[pair]
    clock = book.clock
    h = float(horizon_days) / 365.2425
    if h <= 0:
        raise ValueError(f"the horizon must be positive, got {horizon_days!r} days")
    legs = _target_legs(target)
    rows: list[CarryRow] = []

    def skipped(tenor: str, t: float, why: str) -> CarryRow:
        nan = float("nan")
        return CarryRow(tenor=tenor, t=t, t_rolled=nan, expiry="", forward=nan,
                        forward_rolled=nan, strike=nan, level=nan, level_rolled=nan,
                        roll=nan, roll_term=nan, roll_smile=nan, roll_annual=nan,
                        atm=nan, ratio_atm=nan, ratio_target=None, vega=None, pnl=None,
                        forward_carry=nan, warnings=(why,))

    for tenor in (tenors or book.data.tenor_points):
        t = tenor_to_years(tenor)
        warn: list[str] = []
        t2 = t - h
        if t2 * 365.2425 < MIN_ROLLED_DAYS:
            rows.append(skipped(tenor, t, (
                f"a {horizon_days:g}-day horizon leaves {max(t2, 0.0) * 365.2425:.1f} days on a "
                f"{tenor} option, which is inside the window this model cannot quote; "
                f"shorten the horizon to roll this tenor"
            )))
            continue
        expiry, expiry2 = clock.datetime_from_years(t), clock.datetime_from_years(t2)
        f1, from_feed, note = _forward_at(book, pair, t)
        f2, _, note2 = _forward_at(book, pair, t2)
        for n in (note, note2):
            if n:
                warn.append(n)
        if not from_feed:
            warn.append(
                "without a forward feed the strike is held in moneyness rather than in price, "
                "so the smile slide is zero by construction and only the term structure rolls"
            )

        try:
            atm_now = float(surface.atm_vol(expiry, cut))
            level = level_rolled = level_term = 0.0
            strike_abs = float("nan")
            vega = 0.0
            for weight, delta, is_call in legs:
                if delta == 0.0:
                    k_ratio = float(black.dns_strike(1.0, atm_now, t, surface.conv))
                    v_now = float(surface.vol(k_ratio, expiry, method, cut))
                else:
                    k_ratio, v_now = surface.delta_strike(expiry, delta, is_call, method, cut)
                    k_ratio = float(k_ratio)
                k_abs = k_ratio * f1
                level += weight * v_now
                level_term += weight * float(surface.vol(k_ratio, expiry2, method, cut))
                level_rolled += weight * float(surface.vol(k_abs / f2, expiry2, method, cut))
                vega += weight * float(black.vega(f1, k_abs, v_now, t))
                if len(legs) == 1:
                    strike_abs = k_abs
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            rows.append(skipped(tenor, t, f"{tenor} could not be rolled: {exc}"))
            continue

        roll = level_rolled - level
        roll_term = level_term - level
        combo = target.lower() in COMBINATIONS
        ratio_target = None
        if abs(level) > 1e-6:
            ratio_target = (roll / h) / level
        rows.append(CarryRow(
            tenor=tenor, t=t, t_rolled=t2, expiry=expiry.isoformat(),
            forward=f1, forward_rolled=f2, strike=strike_abs,
            level=level, level_rolled=level_rolled, roll=roll,
            roll_term=roll_term, roll_smile=roll - roll_term, roll_annual=roll / h,
            atm=atm_now, ratio_atm=(roll / h) / atm_now if atm_now > 0 else float("nan"),
            ratio_target=ratio_target,
            vega=None if combo else vega,
            pnl=None if combo else vega * roll,
            forward_carry=f2 - f1, warnings=tuple(warn),
        ))
    return rows


@dataclass(frozen=True)
class FairValueRow:
    """Implied against what was realized, once the roll is paid for."""

    tenor: str
    t: float
    implied: float
    realized: float | None
    realized_window_days: float | None
    roll: float
    roll_multiplier: float
    roll_value: float             # roll * multiplier, in volatility points
    forward_value: float          # the part of roll_value the forward curve caused
    fair: float | None
    richness: float | None
    warnings: tuple[str, ...] = ()


def fair_value_table(book, pair: str, hist=None, *,
                     horizon_days: float = 30.0, lookback_days: float | None = None,
                     method: str | None = None, cut: str = "NY",
                     annualisation: str = "weighted") -> list[FairValueRow]:
    """What the implied volatility would have to be to break even.

    Hold the ``T`` at-the-money option for the horizon ``h`` and delta hedge
    it.  Two things happen.  The mark slides by ``roll`` volatility points,
    worth ``vega(T-h) * roll``.  And the gamma against theta earns roughly the
    fraction ``h/T`` of the option's whole life at the difference between what
    was realized and what was paid, worth ``(h/T) * vega(T) * (sigma_R -
    sigma_I)``.  Setting the two to cancel:

        sigma_I = sigma_R + roll * (T/h) * vega(T-h) / vega(T)

    The multiplier is computed from the actual vegas rather than the
    ``sqrt(T)`` proxy, because the strike is not the same distance from the
    two forwards once the forward curve has any slope in it.  ``richness`` is
    the implied volatility less that fair level: positive means the market is
    charging more than the realized volatility and the carry together justify.

    This is a first-order identity, not a valuation.  It ignores the
    convexity of the gamma P&L in the realized volatility, assumes the
    surface itself does not move, and inherits every assumption in the
    realized number it is handed.

    The roll used here is always the **at-the-money** roll, taken from a
    carry table this function builds itself rather than from whatever target
    the carry screen happens to be showing.
    """
    surface = book[pair]
    h = float(horizon_days) / 365.2425
    out: list[FairValueRow] = []
    # The roll is taken at the money whatever the carry screen is displaying.
    # Feeding this a risk-reversal roll and an at-the-money implied would mix
    # two different positions into one break-even, which is exactly the kind
    # of quiet mismatch this project exists to remove.
    by_tenor = {r.tenor: r for r in carry_table(
        book, pair, horizon_days=horizon_days, target="atm", method=method, cut=cut)}
    for tenor in book.data.tenor_points:
        row = by_tenor.get(tenor)
        t = tenor_to_years(tenor)
        warn: list[str] = []
        if row is None or not math.isfinite(row.roll):
            continue
        expiry, expiry2 = book.clock.datetime_from_years(t), book.clock.datetime_from_years(t - h)
        implied = float(surface.atm_vol(expiry, cut))
        k_ratio = float(black.dns_strike(1.0, implied, t, surface.conv))
        k_abs = k_ratio * row.forward
        v2 = float(surface.vol(k_abs / row.forward_rolled, expiry2, method, cut))
        vega_now = float(black.vega(row.forward, k_abs, implied, t))
        vega_then = float(black.vega(row.forward_rolled, k_abs, v2, t - h))
        multiplier = (t / h) * (vega_then / vega_now) if vega_now > 0 else float("nan")

        rv = None
        window = None
        if hist is not None:
            window = float(lookback_days) if lookback_days else t * 365.2425
            try:
                stats = realized(hist, window, annualisation=annualisation)
                rv = stats.vol
                warn.extend(stats.warnings)
            except Exception as exc:  # noqa: BLE001 - reported per tenor
                warn.append(f"no realized volatility for a {window:.0f}-day window: {exc}")

        if math.isfinite(multiplier) and multiplier > 20.0:
            warn.append(
                f"the roll is multiplied by {multiplier:.0f} to reach a {tenor} break-even from a "
                f"{horizon_days:g}-day horizon. That is the arithmetic, but it also multiplies any "
                f"interpolation error in the roll by the same factor; lengthen the horizon to "
                f"measure this tenor more robustly"
            )
        roll_value = row.roll * multiplier
        fwd_value = row.roll_smile * multiplier
        fair = None if rv is None else rv + roll_value
        out.append(FairValueRow(
            tenor=tenor, t=t, implied=implied, realized=rv, realized_window_days=window,
            roll=row.roll, roll_multiplier=multiplier, roll_value=roll_value,
            forward_value=fwd_value, fair=fair,
            richness=None if fair is None else implied - fair,
            warnings=tuple(warn),
        ))
    return out


@dataclass(frozen=True)
class RealizedRow:
    """Realized against implied, at one tenor, over one lookback."""

    tenor: str
    t: float
    window_days: float
    observations: int
    realized: float
    realized_calendar: float
    realized_count: float
    implied: float
    premium: float                        # implied - realized
    realized_skew: float                  # of the daily returns
    realized_skew_scaled: float           # projected onto this tenor
    realized_kurtosis: float
    realized_kurtosis_scaled: float
    skew_se: float
    kurtosis_se: float
    implied_skew: float | None            # from the marked smile's own density
    implied_kurtosis: float | None
    implied_vol_of_density: float | None
    history: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def realized_table(book, pair: str, hist, *, lookback_days: float | None = None,
                   method: str | None = None, cut: str = "NY",
                   annualisation: str = "weighted", with_moments: bool = True) -> list[RealizedRow]:
    """Realized volatility, skew and kurtosis against what the surface implies.

    ``lookback_days`` of ``None`` means *match the tenor*, which is the only
    like-for-like comparison there is: a one-month implied volatility is a
    forecast of one month, and holding it up against a year of realized data
    compares two different horizons.

    Skew and kurtosis need the same care in the other direction.  The realized
    numbers are computed from daily returns; the numbers the smile implies are
    for the whole return to expiry.  Under independence skewness falls as
    ``1/sqrt(n)`` and excess kurtosis as ``1/n`` in the number of steps, so
    the daily figures are projected onto each tenor before being compared, and
    both the raw and the projected values are reported.
    """
    surface = book[pair]
    out: list[RealizedRow] = []
    for tenor in book.data.tenor_points:
        t = tenor_to_years(tenor)
        window = float(lookback_days) if lookback_days else t * 365.2425
        warn: list[str] = []
        try:
            stats = realized(hist, window, annualisation=annualisation)
        except Exception as exc:  # noqa: BLE001 - one bad tenor must not kill the table
            # Emitting the row with a reason beats dropping it: a tenor that
            # quietly vanishes from the table looks like one that was never
            # asked for.
            nan = float("nan")
            out.append(RealizedRow(
                tenor=tenor, t=t, window_days=window, observations=0,
                realized=nan, realized_calendar=nan, realized_count=nan,
                implied=float(surface.atm_vol(book.clock.datetime_from_years(t), cut)),
                premium=nan, realized_skew=nan, realized_skew_scaled=nan,
                realized_kurtosis=nan, realized_kurtosis_scaled=nan,
                skew_se=nan, kurtosis_se=nan, implied_skew=None,
                implied_kurtosis=None, implied_vol_of_density=None,
                history={}, warnings=(str(exc),)))
            continue
        expiry = book.clock.datetime_from_years(t)
        implied = float(surface.atm_vol(expiry, cut))

        imp_skew = imp_kurt = imp_vol = None
        if with_moments:
            try:
                dist = moments.distribution_from_surface(surface, expiry, method=method, cut=cut)
                m = dist.moments()
                imp_skew, imp_kurt = m.skew, m.excess_kurtosis
                imp_vol = m.annualised_vol(t)
                warn.extend(dist.warnings)
            except (ValueError, ArithmeticError) as exc:
                warn.append(f"{tenor}: the marked smile has no usable density ({exc})")

        history = {}
        for name, field_name, delta in (("atm", "atm", 25), ("rr25", "rr", 25),
                                        ("fly25", "bf", 25), ("rr10", "rr", 10),
                                        ("fly10", "bf", 10)):
            current = implied if name == "atm" else None
            st = implied_stats(hist, window, field_name, tenor, delta=delta, current=current)
            if st is not None:
                history[name] = {"n": st.n, "last": st.last, "mean": st.mean,
                                 "low": st.low, "high": st.high, "percentile": st.percentile}

        out.append(RealizedRow(
            tenor=tenor, t=t, window_days=window, observations=stats.observations,
            realized=stats.vol, realized_calendar=stats.vol_calendar,
            realized_count=stats.vol_count, implied=implied, premium=implied - stats.vol,
            realized_skew=stats.skew, realized_skew_scaled=stats.scaled_skew(t),
            realized_kurtosis=stats.excess_kurtosis,
            realized_kurtosis_scaled=stats.scaled_excess_kurtosis(t),
            skew_se=stats.skew_se, kurtosis_se=stats.kurtosis_se,
            implied_skew=imp_skew, implied_kurtosis=imp_kurt, implied_vol_of_density=imp_vol,
            history=history, warnings=tuple(warn) + stats.warnings,
        ))
    return out


@dataclass(frozen=True)
class TriangleRow:
    """The cross's own marks against the two legs put together."""

    tenor: str
    t: float
    rho: float
    coefficients: tuple[int, int]
    marked: dict[str, float]
    triangle: dict[str, float]
    difference: dict[str, float]
    noise: dict[str, float]               # what the machinery gets wrong on the legs alone
    variance_triangle_atm: float
    smile_convexity: float                # distribution triangle less variance triangle
    leg_atm: tuple[float, float]
    implied_correlation: float | None
    warnings: tuple[str, ...] = ()


def triangle_table(book, pair: str, *, method: str | None = None, cut: str = "NY",
                   deltas=(0.10, 0.25), with_noise: bool = True,
                   tenors=None) -> list[TriangleRow]:
    """Compare a cross's marked smile with the one its two legs imply.

    The at-the-money row has an exact answer and gets one: the variance
    triangle, the same expression ``cross.py`` uses to build the curve in the
    first place.  The risk reversal and the butterfly have no exact answer
    from two marginals and a correlation, so the legs' whole distributions are
    tied together with a Gaussian copula and the cross's smile is integrated
    out of the result -- see ``moments.py`` for what that assumes.

    ``noise`` is the same machinery run on each leg alone, where it should
    reproduce the input exactly.  Whatever it gets wrong there it is also
    getting wrong here, so a difference smaller than the noise is not a
    difference.

    The two at-the-money triangles do not agree, and should not.  The variance
    triangle uses each leg's *at-the-money* volatility; the distribution
    triangle uses each leg's whole density, whose variance is larger by the
    convexity of its own smile.  ``smile_convexity`` is that gap, reported so
    it is not mistaken for a marking error -- it is typically a fifth of a
    volatility point and it is what the book's own construction leaves out.
    """
    spec = book.data.pairs.get(pair)
    if spec is None or not spec.is_cross:
        raise ValueError(f"{pair} is not a cross in this workbook, so it has no triangle")
    surface = book[pair]
    curve = surface.atm
    if not isinstance(curve, CrossAtmCurve):
        raise ValueError(f"{pair} is marked as a cross but its ATM curve is not a cross curve")
    leg_a, leg_b = spec.legs
    for leg in (leg_a, leg_b):
        if leg not in book:
            raise ValueError(f"{pair} needs {leg}, which is not built in this book")
        # ``Book.load_all`` deliberately builds a cross's legs but fits smiles
        # only for the pairs asked for -- the cross carries its own quotes, so
        # nothing else needs the legs' smiles.  The triangle does, and it is
        # the only thing that does, so it arranges them here rather than
        # slowing every other call down.
        if not book[leg].fits:
            marks = book.data.marks.get(leg)
            if not marks:
                raise ValueError(
                    f"{leg} has no smile quotes in the workbook, so there is nothing to "
                    f"build a {pair} triangle out of"
                )
            book[leg].calibrate(marks)
    ca, cb = moments.triangle_coefficients(pair, leg_a, leg_b)

    rows: list[TriangleRow] = []
    for tenor in (tenors or book.data.tenor_points):
        t = tenor_to_years(tenor)
        expiry = book.clock.datetime_from_years(t)
        warn: list[str] = []
        rho = float(np.asarray(curve.correlation(t)))
        try:
            da = moments.distribution_from_surface(book[leg_a], expiry, method=method,
                                                   cut=cut, label=leg_a)
            db = moments.distribution_from_surface(book[leg_b], expiry, method=method,
                                                   cut=cut, label=leg_b)
            comb = moments.combine(da, db, (ca, cb), rho, surface.conv)
            got = comb.table(deltas)
            warn.extend(comb.warnings)
        except (ValueError, ArithmeticError, ZeroDivisionError) as exc:
            # The row is kept, empty, with the reason on it.  Dropping it here
            # left the whole table silently short -- and when every tenor
            # failed, silently empty.
            nan = float("nan")
            rows.append(TriangleRow(
                tenor=tenor, t=t, rho=rho, coefficients=(ca, cb),
                marked={}, triangle={}, difference={}, noise={},
                variance_triangle_atm=nan, smile_convexity=nan, leg_atm=(nan, nan),
                implied_correlation=None,
                warnings=tuple(warn) + (f"the triangle could not be built: {exc}",)))
            continue

        table = surface.smile_table(expiry, deltas=tuple(deltas), method=method, cut=cut)
        by = {r["label"]: r["vol"] for r in table}
        marked = {"atm": float(surface.atm_vol(expiry, cut))}
        for d in deltas:
            tag = f"{int(round(d * 100))}"
            c, p = by.get(f"{tag}d call"), by.get(f"{tag}d put")
            if c is None or p is None:
                continue
            marked[f"rr{tag}"] = c - p
            marked[f"fly{tag}"] = 0.5 * (c + p) - by["ATM"]

        triangle = {k: float(got[k]) for k in marked if k in got}
        difference = {k: triangle[k] - marked[k] for k in triangle}

        noise: dict[str, float] = {}
        if with_noise:
            for leg, dist in ((leg_a, da), (leg_b, db)):
                ref = _leg_reference(book[leg], expiry, method, cut, deltas)
                err = moments.reconstruction_error(dist, book[leg].conv, ref, deltas)
                for key, value in err.items():
                    noise[key] = max(noise.get(key, 0.0), abs(value))

        va = float(book[leg_a].atm_vol(expiry, cut))
        vb = float(book[leg_b].atm_vol(expiry, cut))
        var = va * va + vb * vb + 2.0 * ca * cb * rho * va * vb
        implied_rho = None
        cross_atm = marked["atm"]
        if va > 0 and vb > 0:
            implied_rho = (cross_atm * cross_atm - va * va - vb * vb) / (2.0 * ca * cb * va * vb)
        var_atm = math.sqrt(max(var, 0.0))
        rows.append(TriangleRow(
            tenor=tenor, t=t, rho=rho, coefficients=(ca, cb),
            marked=marked, triangle=triangle, difference=difference, noise=noise,
            variance_triangle_atm=var_atm,
            smile_convexity=triangle.get("atm", float("nan")) - var_atm,
            leg_atm=(va, vb), implied_correlation=implied_rho, warnings=tuple(warn),
        ))
    return rows


def _leg_reference(surface, expiry, method, cut, deltas) -> dict[str, float]:
    table = surface.smile_table(expiry, deltas=tuple(deltas), method=method, cut=cut)
    by = {r["label"]: r["vol"] for r in table}
    out = {"atm": by["ATM"]}
    for d in deltas:
        tag = f"{int(round(d * 100))}"
        c, p = by.get(f"{tag}d call"), by.get(f"{tag}d put")
        if c is None or p is None:
            continue
        out[f"rr{tag}"] = c - p
        out[f"fly{tag}"] = 0.5 * (c + p) - by["ATM"]
    return out
