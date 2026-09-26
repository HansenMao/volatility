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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import black, moments, sabr
from .cross import CrossAtmCurve
from .numerics import ConvergenceError
from .marketdata import open_workbook
from .history import (CORR_VOL_DAYS, DYNAMICS_DAYS, HistoryError, Realized, SeriesStats, implied_stats,
                      realized, realized_corr_spot, realized_corr_vol, realized_correlation,
                      realized_vol_vol, vol_dynamics)
from .surface import VolSurface, smile_points
from .timeutil import Clock, TenorError, parse_datetime, parse_tenor, tenor_to_years

DEFAULT_LADDER = (-0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20)


@dataclass
class ForwardCurve:
    """Outright forwards by tenor."""

    points: dict[str, float]

    @classmethod
    def from_excel(cls, path: str | Path, sheet: str, column: str = "fwd") -> "ForwardCurve":
        # Through open_workbook, so a forward sheet the user has open in
        # Excel can still be saved: see marketdata.open_workbook.
        with open_workbook(path) as xls:
            df = pd.read_excel(xls, sheet, index_col=0)
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
        ``delta`` -- forward move valued at the average **smile** delta, which
        includes the volatility's own reaction to the forward.
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
            # The smile's own delta, not Black-Scholes': this is a fixed
            # strike sliding under a moving forward, so the volatility it is
            # marked at moves too, and a delta that held it still would value
            # the move short by the skew's contribution.
            is_call = strike >= f_far
            d_far = float(self.surface.smile_delta(f_far, strike, t_far, is_call, self.method))
            d_near = float(self.surface.smile_delta(f_near, strike, t_near, is_call, self.method))
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
            t_near = self.surface.tenor_years(near_tenor)
            t_far = self.surface.tenor_years(far_tenor)
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
            # A tenor is whatever ``parse_tenor`` reads, not whatever is short
            # enough to look like one: "1week" and "10 days" are tenors and
            # counting their characters called them dates.
            try:
                parse_tenor(expiry)
            except TenorError:
                expiry = parse_datetime(expiry, today=book.clock.now.date()).date()
            else:
                expiry = book.calendars.expiry_date(row.pair, expiry, book.clock.now.date())

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


def _forward_at(book, pair: str, t: float, expiry=None) -> tuple[float, bool, str]:
    """Outright forward for an expiry, and whether it really came from a feed.

    Through ``Book.market_level_for`` and not the feed directly, so every
    screen that needs a level gets the same one -- including a cross the feed
    does not quote but whose legs it does, which this used to refuse while the
    marking screen's chart was scaling its axis by the very same triangle.

    ``expiry`` is the option's expiry **date**, and given one the level is
    read on that option's settlement date, which is the date a forward is
    actually a price for.  A caller with only a year fraction -- the rolled
    leg of a carry row, which is a horizon and not an expiry -- passes none
    and gets the curve read at ``t``, which is the same axis placed nominally.
    """
    level = (book.market_level(pair, t) if expiry is None
             else book.market_level_for(pair, expiry))
    if not level["feed"]:
        return 1.0, False, "no forward feed for this pair"
    notes = []
    if level["derived"]:
        notes.append(f"the feed does not quote {pair.upper()} itself, so the forward is "
                     f"the {level['via']} triangle")
    if level["extrapolated"]:
        where = level.get("settle") or f"{t:.4f}y"
        notes.append(f"the forward to {where} is outside the quoted pillars and was held flat")
    return float(level["forward"]), True, "; ".join(notes)


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
    #: The forward curve's own carry, and what the position earns from it.
    #: ``carry_rate`` is the annualised proportional roll-down of the forward
    #: -- the rate differential the swap points are quoting.  ``delta`` is the
    #: position's ``dV/dF`` at the fixed strike, ``carry_pnl`` the exact
    #: revaluation of the position at the rolled forward with volatility and
    #: maturity held, and ``carry_vols`` that P&L divided by the position's own
    #: vega, which is the form that can be compared with ``roll``.
    carry_rate: float = float("nan")
    #: ``delta`` is the Black-Scholes delta: the closed-form sensitivity at
    #: the option's own volatility with that volatility *held fixed* as the
    #: forward moves.  ``smile_delta`` is the same position's ``dV/dF`` with
    #: the smile allowed to react -- the forward slides under a fixed strike,
    #: the option's moneyness changes and so does the volatility it is marked
    #: at -- which is what an FX desk actually hedges.  ``skew_delta`` is the
    #: difference and is exactly ``vega * dsigma/dF``, the skew's own
    #: contribution.  Both are ``dV/dF`` in the **term** currency, matching
    #: ``carry_pnl``, which is what makes ``delta * (f2 - f1)`` the carry and
    #: ``smile_delta * (f2 - f1)`` the whole of what the forward move does.
    #: They are deliberately *not* the premium-adjusted quoted delta on a pair
    #: that quotes one: that is a hedge ratio in the other currency, and
    #: multiplying it by a move in the forward does not give money.
    delta: float | None = None
    smile_delta: float | None = None
    skew_delta: float | None = None
    carry_pnl: float | None = None
    carry_vols: float | None = None
    #: ``carry_pnl`` with the position's own first-order exposure to the
    #: forward taken out: ``carry_pnl - delta * (forward_rolled - forward)``.
    #: That is what the forward's roll is worth to a **delta-hedged** book,
    #: which is the only reading of it that is a statement about the
    #: *volatility* rather than about the direction.  ``carry_pnl`` itself is
    #: the right number for the position -- a spot-hedged book keeps the whole
    #: of it -- and the wrong one for a break-even, because at a strike with
    #: any delta on it the first-order term is the forward move times that
    #: delta and it is equal and opposite for a call and a put at one strike.
    #: What is left here is the gamma over the move, which is call/put
    #: symmetric by put-call parity: ``C - P = F - K`` in price and
    #: ``delta_c - delta_p = 1`` in delta, so the two cancel exactly.
    carry_hedged: float | None = None
    total_pnl: float | None = None
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

    The forward curve pays twice, and the two are reported separately because
    they are different risks:

    * through the **mark** -- the forward slides out from under a fixed
      strike, the option's moneyness changes, and the volatility it is marked
      at changes with it.  That is ``roll_smile``, in volatility points.
    * through the **price** -- the option is worth
      ``V(F, K, sigma, tau)`` and ``F`` itself has moved from ``forward`` to
      ``forward_rolled``.  That is ``carry_pnl``, in premium terms, and it is
      what a desk means by the carry on an option position.

    The second one depends on how the delta is hedged, and the convention is
    worth stating because it is the whole of the number.  Hedged in the
    **outright forward to the option's own expiry**, the hedge rolls down the
    curve exactly as the option does and the two cancel: ``carry_pnl`` is
    earned and paid away, and the position has no forward carry.  Hedged in
    **spot**, which is what an FX options desk actually does, nothing rolls on
    the hedge side and the position keeps it.  So ``carry_pnl`` is the carry
    of a spot-hedged book, and the same number is the cost of *not* hedging in
    the forward.  It is computed by full revaluation rather than as
    ``delta * (F2 - F1)``, so the gamma over the move is in it; ``delta`` is
    reported beside it as the first-order reading.

    ``carry_hedged`` is the same move with that first-order reading taken out.
    A break-even volatility is a property of a **strike** and cannot depend on
    whether the option at it is written as a call or a put, so anything asking
    what the forward's roll is worth to the *mark* has to read that column and
    not ``carry_pnl``: at a 25 delta strike the two differ by a quarter of the
    forward move, with the sign of the option's direction.

    Without a forward feed there is no curve to roll down, and both are left
    unavailable rather than reported as zero.
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

    for tenor in (tenors or book.data.tenors_for(pair)):
        dates = book.fx_dates(pair, tenor)
        t = surface.tenor_years(tenor)
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
        # **Both forwards on one axis.**  The row's whole content is the
        # difference between them, so reading one on the option's settlement
        # date and the other at a year fraction contaminates that difference
        # with the gap between the two conventions -- a third of a pip here,
        # which is the same order as the gamma carry being measured, and it
        # turned a flat carry profile into a ragged one.  So the settlement
        # date is put on the feed's axis once and the horizon is taken off it:
        # the rolled leg is this same option a week later, not another option.
        ts = book.settlement_years(pair, dates.expiry)
        f1, from_feed, note = _forward_at(book, pair, ts, dates.expiry)
        f2, _, note2 = _forward_at(book, pair, ts - h)
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
            vega = gross_vega = 0.0
            pos_delta = pos_smile_delta = carry_pnl = 0.0
            smile_delta_ok = True
            for weight, delta, is_call in legs:
                if delta == 0.0:
                    k_ratio = float(black.atm_strike(1.0, atm_now, t, surface.conv))
                    v_now = float(surface.vol(k_ratio, expiry, method, cut))
                else:
                    k_ratio, v_now = surface.delta_strike(expiry, delta, is_call, method, cut)
                    k_ratio = float(k_ratio)
                k_abs = k_ratio * f1
                level += weight * v_now
                level_term += weight * float(surface.vol(k_ratio, expiry2, method, cut))
                level_rolled += weight * float(surface.vol(k_abs / f2, expiry2, method, cut))
                leg_vega = float(black.vega(f1, k_abs, v_now, t))
                vega += weight * leg_vega
                gross_vega += abs(weight) * leg_vega
                # A leg quoted at the at-the-money is a *straddle* on this
                # desk, so its delta is what makes the delta-neutral strike
                # delta-neutral: zero.  Reading it as the call alone would
                # hand the at-the-money row half a unit of forward carry that
                # nobody is running.  Half a straddle is used so its vega is a
                # single option's and the existing ``vega`` column does not
                # move.
                sides = ((0.5, True), (0.5, False)) if delta == 0.0 else ((1.0, is_call),)
                for share, call in sides:
                    pos_delta += weight * share * float(
                        black.delta(f1, k_abs, v_now, t, call))
                    # The delta the desk runs.  The whole of this table is a
                    # fixed strike sliding under a moving forward, and the
                    # volatility that strike is marked at moves with it: the
                    # Black-Scholes delta holds that volatility still and is
                    # wrong about the position by the skew.  Same sticky
                    # moneyness the roll itself uses, so the two agree about
                    # what the forward does to the mark.
                    if smile_delta_ok:
                        try:
                            pos_smile_delta += weight * share * float(
                                surface.smile_delta(f1, k_abs, expiry, call, method,
                                                    cut, conv=False))
                        except (ValueError, ArithmeticError, ConvergenceError) as exc:
                            smile_delta_ok = False
                            warn.append(
                                f"no smile delta at {tenor}: {exc}. The Black-Scholes delta "
                                f"is still reported, but it holds the volatility fixed as "
                                f"the forward moves and is short of the skew's contribution")
                    carry_pnl += weight * share * float(
                        black.price(f2, k_abs, v_now, t, call)
                        - black.price(f1, k_abs, v_now, t, call))
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

        # No feed means no curve to roll down.  ``f1`` and ``f2`` are both 1.0
        # there, so every carry figure would come out an exact zero -- the
        # silent zero this project exists to remove -- and they are left
        # unavailable instead, under the warning already on the row.
        carry_rate = float("nan")
        carry_vols: float | None = None
        delta_out: float | None = None
        smile_delta_out: float | None = None
        skew_delta_out: float | None = None
        carry_out: float | None = None
        carry_hedged: float | None = None
        total_pnl: float | None = None
        if from_feed:
            carry_rate = (f2 - f1) / (f1 * h)
            delta_out, carry_out = pos_delta, carry_pnl
            # The same move with the position's own delta taken out.  See the
            # field's own note: this is the half of the forward's roll that
            # says something about the volatility, and it is the one a
            # break-even has to be built on.
            carry_hedged = carry_pnl - pos_delta * (f2 - f1)
            if smile_delta_ok:
                smile_delta_out = pos_smile_delta
                skew_delta_out = pos_smile_delta - pos_delta
            # A risk reversal has almost no net vega, so dividing its carry by
            # that vega is a division by nearly nothing; the row says the carry
            # in premium and declines to translate it.
            if abs(vega) > 0.05 * gross_vega:
                carry_vols = carry_pnl / vega
            if not combo:
                total_pnl = vega * roll + carry_pnl
        rows.append(CarryRow(
            tenor=tenor, t=t, t_rolled=t2, expiry=expiry.isoformat(),
            forward=f1, forward_rolled=f2, strike=strike_abs,
            level=level, level_rolled=level_rolled, roll=roll,
            roll_term=roll_term, roll_smile=roll - roll_term, roll_annual=roll / h,
            atm=atm_now, ratio_atm=(roll / h) / atm_now if atm_now > 0 else float("nan"),
            ratio_target=ratio_target,
            vega=None if combo else vega,
            pnl=None if combo else vega * roll,
            forward_carry=f2 - f1,
            carry_rate=carry_rate, delta=delta_out, smile_delta=smile_delta_out,
            skew_delta=skew_delta_out, carry_pnl=carry_out,
            carry_vols=carry_vols, carry_hedged=carry_hedged, total_pnl=total_pnl,
            warnings=tuple(warn),
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
    #: The forward curve's price-side carry on the position, and the break-even
    #: volatility it is worth.  ``carry_rate`` is the annualised proportional
    #: roll-down of the forward, ``carry_pnl`` the straddle's revaluation at
    #: the rolled forward, ``carry_hedged`` that revaluation with the
    #: straddle's own delta taken out -- the gamma over the move, which is
    #: what a delta-hedged break-even is actually paid -- and ``carry_value``
    #: *that* P&L expressed as the volatility it takes to pay for it, on the
    #: same footing as ``roll_value``.
    carry_rate: float = float("nan")
    carry_pnl: float | None = None
    carry_hedged: float | None = None
    carry_value: float = 0.0
    #: What the realized volatility was measured on: ``spot``, or the forward
    #: to this tenor.  An implied volatility is a volatility of the forward.
    realized_basis: str = "spot"
    realized_spot: float | None = None
    warnings: tuple[str, ...] = ()


def fair_value_table(book, pair: str, hist=None, *,
                     horizon_days: float = 30.0, lookback_days: float | None = None,
                     method: str | None = None, cut: str = "NY",
                     annualisation: str = "weighted",
                     realized_basis: str = "auto") -> list[FairValueRow]:
    """What the implied volatility would have to be to break even.

    Hold the ``T`` at-the-money straddle for the horizon ``h`` and delta hedge
    it in spot.  Three things happen.  The mark slides by ``roll`` volatility
    points, worth ``vega(T-h) * roll``.  The forward rolls down its own curve
    under a fixed strike, worth ``carry_pnl`` in premium.  And the gamma
    against theta earns roughly the fraction ``h/T`` of the option's whole
    life at the difference between what was realized and what was paid, worth
    ``(h/T) * vega(T) * (sigma_R - sigma_I)``.  Setting them to cancel:

        sigma_I = sigma_R + (T/h) * [ roll * vega(T-h) + carry_pnl ] / vega(T)

    -- the ``roll_value`` and ``carry_value`` columns.  The multiplier is
    computed from the actual vegas rather than the ``sqrt(T)`` proxy, because
    the strike is not the same distance from the two forwards once the forward
    curve has any slope in it.  ``richness`` is the implied volatility less
    that fair level: positive means the market is charging more than the
    realized volatility and the carry together justify.

    The forward curve reaches this break-even twice and the two are separated
    on purpose.  Through the **mark** it is ``forward_value``, the part of
    ``roll_value`` the smile slide caused, and it is first order in the shape
    of the curve.  Through the **price** it is ``carry_value``, and for a
    *delta-neutral* straddle -- which is what the at-the-money is on this desk
    -- it is second order: the position starts with no delta, so the forward
    moving under it earns only the gamma over the move.  It is computed rather
    than assumed to be zero, because it is not zero for a steep curve or a
    long horizon, and because a number reported as an exact zero should have
    been measured.

    This is a first-order identity, not a valuation.  It ignores the
    convexity of the gamma P&L in the realized volatility, assumes the
    surface itself does not move, and inherits every assumption in the
    realized number it is handed.

    ``realized_basis`` is passed through to ``history.realized``: on the
    default ``auto`` the realized volatility is measured on the **forward** to
    each tenor wherever the historical sheet quotes the swap points, which is
    the like-for-like comparison -- the implied volatility being broken even
    against is a volatility of that forward, not of spot.

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
    for tenor in book.data.tenors_for(pair):
        row = by_tenor.get(tenor)
        t = surface.tenor_years(tenor)
        warn: list[str] = []
        if row is None or not math.isfinite(row.roll):
            continue
        expiry, expiry2 = book.clock.datetime_from_years(t), book.clock.datetime_from_years(t - h)
        implied = float(surface.atm_vol(expiry, cut))
        k_ratio = float(black.atm_strike(1.0, implied, t, surface.conv))
        k_abs = k_ratio * row.forward
        v2 = float(surface.vol(k_abs / row.forward_rolled, expiry2, method, cut))
        vega_now = float(black.vega(row.forward, k_abs, implied, t))
        vega_then = float(black.vega(row.forward_rolled, k_abs, v2, t - h))
        multiplier = (t / h) * (vega_then / vega_now) if vega_now > 0 else float("nan")

        # The forward slide, at fixed strike, fixed volatility and fixed
        # maturity, so it is the forward's contribution alone and not a second
        # helping of the roll.  A straddle rather than one side of it, and
        # **delta hedged**: what pays for a break-even is the gamma over the
        # move, and the first-order piece is the hedge's, not the option's.
        # The at-the-money straddle is delta neutral in the pair's own quoted
        # convention, so on a pair that quotes an unadjusted delta the hedge
        # is exactly zero and nothing here moves; on a premium-adjusted pair
        # the delta-neutral strike is neutral in *that* convention and this
        # ``dV/dF`` is not quite zero, which is the small correction.
        carry_rate = float("nan")
        carry_pnl = None
        carry_hedged = None
        carry_value = 0.0
        if math.isfinite(row.forward_rolled) and row.forward_rolled != row.forward:
            f1, f2 = row.forward, row.forward_rolled
            carry_rate = (f2 - f1) / (f1 * h)
            carry_pnl = sum(
                float(black.price(f2, k_abs, implied, t, call)
                      - black.price(f1, k_abs, implied, t, call))
                for call in (True, False))
            carry_delta = sum(
                float(black.delta(f1, k_abs, implied, t, call)) for call in (True, False))
            carry_hedged = carry_pnl - carry_delta * (f2 - f1)
            if vega_now > 0:
                carry_value = carry_hedged * (t / h) / (2.0 * vega_now)

        rv = None
        window = None
        basis = "spot"
        rv_spot = None
        if hist is not None:
            window = float(lookback_days) if lookback_days else t * 365.2425
            try:
                stats = realized(hist, window, annualisation=annualisation,
                                 basis=realized_basis, basis_tenor=tenor)
                rv = stats.vol
                basis = stats.basis
                rv_spot = stats.vol_spot
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
        fair = None if rv is None else rv + roll_value + carry_value
        out.append(FairValueRow(
            tenor=tenor, t=t, implied=implied, realized=rv, realized_window_days=window,
            roll=row.roll, roll_multiplier=multiplier, roll_value=roll_value,
            forward_value=fwd_value, fair=fair,
            richness=None if fair is None else implied - fair,
            carry_rate=carry_rate, carry_pnl=carry_pnl, carry_hedged=carry_hedged,
            carry_value=carry_value,
            realized_basis=basis, realized_spot=rv_spot,
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
    #: What the realized figures were measured on, and the swap points' part
    #: in it.  An implied volatility is a volatility of the forward, so on the
    #: forward basis the swap points *moving* is realized volatility too.
    realized_basis: str = "spot"
    realized_spot: float = float("nan")
    points_vol: float | None = None
    points_correlation: float | None = None
    carry_rate: float | None = None
    #: The same wings said in SABR's two words: the spot/volatility
    #: correlation a risk reversal is paid for, and the volatility of
    #: volatility a butterfly is paid for.  ``implied_*`` is read off the
    #: marked smile, ``realized_*`` measured from the history, and the
    #: difference is the risk reversal and the butterfly compared with what
    #: actually happened -- which the moments above cannot do, because a
    #: quoted spread is not a moment.
    sabr_delta: float | None = None
    implied_rho: float | None = None
    implied_nu: float | None = None
    implied_shape_error: float | None = None
    marked_rr: float | None = None
    marked_fly: float | None = None
    realized_rho: float | None = None
    realized_nu: float | None = None
    realized_rho_se: float | None = None
    realized_nu_se: float | None = None
    rho_difference: float | None = None
    nu_difference: float | None = None
    dynamics_source: str | None = None
    #: The window ``(rho, nu)`` were measured over, which is deliberately not
    #: ``window_days``.  See :data:`history.DYNAMICS_DAYS`.
    dynamics_days: float | None = None
    history: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def realized_table(book, pair: str, hist, *, lookback_days: float | None = None,
                   method: str | None = None, cut: str = "NY",
                   annualisation: str = "weighted", with_moments: bool = True,
                   realized_basis: str = "auto", with_sabr: bool = False,
                   sabr_delta: float = 0.25,
                   dynamics_days: float | None = None) -> list[RealizedRow]:
    """Realized volatility, skew and kurtosis against what the surface implies.

    ``lookback_days`` of ``None`` means *match the tenor*, which is the only
    like-for-like comparison there is: a one-month implied volatility is a
    forecast of one month, and holding it up against a year of realized data
    compares two different horizons.

    ``realized_basis`` decides what "realized" means.  On the default ``auto``
    each tenor is measured on the **forward** to that tenor wherever the
    historical sheet quotes the swap points, and on spot where it does not,
    saying which it did.  A quoted volatility is a volatility of the forward:
    the swap points moving is part of what the option delivered, and on a
    high-carry or managed pair it is a large part.  ``spot`` restores the
    older reading.

    Skew and kurtosis need care in the other direction.  The realized numbers
    are computed from daily returns; the numbers the smile implies are for the
    whole return to expiry.  Under independence skewness falls as
    ``1/sqrt(n)`` and excess kurtosis as ``1/n`` in the number of steps, so
    the daily figures are projected onto each tenor before being compared, and
    both the raw and the projected values are reported.

    ``with_sabr`` answers the same question about the risk reversal and the
    butterfly, which the moments above cannot: a quoted spread is not a
    moment, and a realized third moment is not a risk reversal.  What the two
    do share is the pair of numbers a SABR smile is built from -- the
    spot/volatility correlation and the volatility of volatility.  So the
    marked wings are read as the ``(rho, nu)`` that would produce them
    (``sabr.fit_smile_shape``) and the history is measured for the same two
    (``history.vol_dynamics``), and the difference between them is the wings
    against what happened.  Both are approximations of a surface that is not
    SABR, and the fit reports its own residual so a smile SABR cannot reach
    says so instead of quietly returning the nearest thing.

    The measured half of that comparison is **not** taken over
    ``lookback_days``.  A realized volatility is matched to the tenor because
    a one-month implied volatility forecasts one month; a spot/volatility
    correlation and a vol of vol are properties of the process and want as
    much data as there is.  They also need more observations than a realized
    volatility does, so on a short lookback every tenor's realized figure came
    back and every tenor's ``(rho, nu)`` was blank -- the whole column group,
    both difference columns included, on a table whose other columns were
    fine.  ``dynamics_days`` is that window (:data:`history.DYNAMICS_DAYS` by
    default) and is never shorter than the realized lookback; every row
    reports the window it was measured on.
    """
    surface = book[pair]
    out: list[RealizedRow] = []
    for tenor in book.data.tenors_for(pair):
        t = surface.tenor_years(tenor)
        window = float(lookback_days) if lookback_days else t * 365.2425
        dyn_window = max(window, float(DYNAMICS_DAYS if dynamics_days is None
                                       else dynamics_days))
        warn: list[str] = []
        expiry = book.clock.datetime_from_years(t)
        implied = float(surface.atm_vol(expiry, cut))

        # The wings as a SABR shape are built before the realized statistics
        # and do not depend on them: the marked half needs no history at all
        # and the measured half is taken over its own, longer window.  Built
        # after them, a one-week row -- which can never have seven days' worth
        # of returns in a seven-day window -- lost the whole column group to a
        # failure that had nothing to do with it, and did so at every tenor
        # whose realized window came up short.
        shape = _sabr_shape(surface, expiry, t, implied, sabr_delta, method, cut, warn) \
            if with_sabr else None
        dyn = _measured_dynamics(hist, dyn_window, tenor, warn) if with_sabr else None
        sabr_group = dict(
            sabr_delta=sabr_delta if with_sabr else None,
            implied_rho=(shape[0].rho if shape else None),
            implied_nu=(shape[0].nu if shape else None),
            implied_shape_error=(shape[0].max_error if shape else None),
            marked_rr=(shape[1] if shape else None),
            marked_fly=(shape[2] if shape else None),
            realized_rho=(dyn.rho if dyn else None),
            realized_nu=(dyn.nu if dyn else None),
            realized_rho_se=(dyn.rho_se if dyn else None),
            realized_nu_se=(dyn.nu_se if dyn else None),
            rho_difference=(None if not (shape and dyn) else shape[0].rho - dyn.rho),
            nu_difference=(None if not (shape and dyn) else shape[0].nu - dyn.nu),
            dynamics_source=(dyn.source if dyn else None),
            dynamics_days=(dyn_window if with_sabr else None),
        )

        try:
            stats = realized(hist, window, annualisation=annualisation,
                             basis=realized_basis, basis_tenor=tenor)
        except Exception as exc:  # noqa: BLE001 - one bad tenor must not kill the table
            # Emitting the row with a reason beats dropping it: a tenor that
            # quietly vanishes from the table looks like one that was never
            # asked for.
            nan = float("nan")
            out.append(RealizedRow(
                tenor=tenor, t=t, window_days=window, observations=0,
                realized=nan, realized_calendar=nan, realized_count=nan,
                implied=implied,
                premium=nan, realized_skew=nan, realized_skew_scaled=nan,
                realized_kurtosis=nan, realized_kurtosis_scaled=nan,
                skew_se=nan, kurtosis_se=nan, implied_skew=None,
                implied_kurtosis=None, implied_vol_of_density=None,
                history={}, warnings=(str(exc),) + tuple(warn), **sabr_group))
            continue

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
            realized_basis=stats.basis, realized_spot=stats.vol_spot,
            points_vol=stats.points_vol, points_correlation=stats.points_correlation,
            carry_rate=stats.carry_rate, **sabr_group,
            history=history, warnings=tuple(warn) + stats.warnings,
        ))
    return out


def _sabr_shape(surface, expiry, t: float, atm: float, delta: float,
                method, cut: str, warn: list[str]):
    """The marked wings at ``delta``, and the ``(rho, nu)`` that would show them.

    Returns ``(shape, rr, fly)`` or ``None``, appending the reason to ``warn``.
    The failure is reported rather than raised: the risk reversal and the
    butterfly are one column group on a table whose other columns are fine.
    """
    tag = f"{int(round(delta * 100))}"
    try:
        by = smile_points(surface.smile_table(expiry, deltas=(delta,), method=method,
                                              cut=cut))
        call, put = by.get(f"{tag}d call"), by.get(f"{tag}d put")
        if call is None or put is None:
            raise ValueError(f"the marked smile has no {tag} delta wings")
        rr = float(call) - float(put)
        fly = 0.5 * (float(call) + float(put)) - float(by["ATM"])
        shape = sabr.fit_smile_shape(atm, rr, fly, delta, t, surface.slice_conv(t))
        warn.extend(shape.warnings)
        return shape, rr, fly
    except (ValueError, ArithmeticError, ConvergenceError, KeyError) as exc:
        warn.append(f"no SABR shape could be read off the marked smile: {exc}")
        return None


def _measured_dynamics(hist, window: float, tenor: str, warn: list[str]):
    """Realized spot/volatility correlation and vol of vol, or the reason there is none.

    ``window`` is the dynamics window and not the realized lookback, and it is
    named in the failure: "not enough observations" is a different sentence
    depending on how much of the sheet was asked for.
    """
    try:
        dyn = vol_dynamics(hist, window, tenor)
    except Exception as exc:  # noqa: BLE001 - one column group, reported in place
        warn.append(f"no measured volatility dynamics at {tenor} over {window:.0f} days: {exc}")
        return None
    warn.extend(dyn.warnings)
    return dyn


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
    #: How a unit of at-the-money vega on the cross lands on the two legs, and
    #: on the correlation.  ``leg_vega`` is (d sigma_cross / d sigma_a,
    #: d sigma_cross / d sigma_b) and ``rho_vega`` is d sigma_cross / d rho.
    leg_vega: tuple[float, float] = (float("nan"), float("nan"))
    rho_vega: float = float("nan")
    warnings: tuple[str, ...] = ()
    #: The dependence marked at this tenor on the ``CROSS_DEPENDENCE`` tab
    #: (``moments.Dependence``): ``None`` and ``0`` where nothing is marked,
    #: which is the Gaussian copula.
    vol_vol: float | None = None
    corr_vol: float = 0.0
    corr_spot: float = 0.0
    #: The correlation the copula ran at: ``rho`` for the Gaussian copula, and
    #: under a marked dependence the one that holds the combined ATM where the
    #: Gaussian copula puts it (``moments.combine_holding_atm``).
    copula_rho: float = float("nan")
    #: What the Gaussian copula gives, keyed like ``triangle``, on a row where a
    #: dependence is marked -- so what the marking added is on the row -- and
    #: empty where it is not, because ``triangle`` already is that.
    gaussian: dict[str, float] = field(default_factory=dict)
    #: The vol-vol correlation at which the legs give the cross's marked
    #: 25-delta butterfly, holding the marked correlation vol -- the number the
    #: ``CROSS_DEPENDENCE`` tab is marked against -- or ``None`` with the reason.
    implied_vol_vol: float | None = None
    implied_vol_vol_note: str = ""
    #: Its twin for the risk reversal: the correlation-spot correlation at which
    #: the legs give the cross's marked 25-delta risk reversal, holding the
    #: marked vol-vol correlation and correlation vol -- or ``None`` with the
    #: reason.  Searched only where a correlation vol is marked, because it has
    #: nothing to lean without one.
    implied_corr_spot: float | None = None
    implied_corr_spot_note: str = ""
    #: Where the dependence the legs were tied with came from: the
    #: ``CROSS_DEPENDENCE`` tab, the Gaussian copula, or what the caller named
    #: (the relative-value grid's measured one says what it measured).
    dependence_source: str = ""
    #: How far the triangle moves across the caller's band of dependences --
    #: the estimation uncertainty of a measured one -- keyed like ``triangle``.
    #: Kept apart from ``noise``, which is the grid's error and nothing else.
    dependence_noise: dict[str, float] = field(default_factory=dict)


#: ``_leg_law``'s marker for "read the dependence off the book".
_FROM_BOOK = object()


def _leg_law(book, pair: str, da, db, coefficients, rho: float, conv, t: float,
             dependence=_FROM_BOOK):
    """The cross's law out of its two legs at one expiry, and the Gaussian one.

    ``(law, gaussian, dependence)``.  With nothing marked on
    ``CROSS_DEPENDENCE`` the law *is* the Gaussian copula at the curve's
    correlation.  With a dependence marked it is that dependence, at the
    copula correlation that keeps the combined ATM where the Gaussian copula
    puts it -- so the marking moves the wings and never the level.  The one
    place the triangle and the implied quotes build a law, so they cannot
    tie one cross's legs together two ways.  A ``dependence`` given (``None``
    included, which is the Gaussian copula) is used instead of the book's --
    how the relative-value grid ties the legs at what their history shows.
    """
    gaussian = moments.combine(da, db, coefficients, rho, conv)
    if dependence is _FROM_BOOK:
        dependence = book.dependence_at(pair, t)
    if dependence is None:
        return gaussian, gaussian, None
    law = moments.combine_holding_atm(da, db, coefficients, rho, conv, dependence,
                                      target_atm=gaussian.atm_vol()[0])
    return law, gaussian, dependence


def _cross_legs(book, pair: str):
    """A cross's surface, its curve, its two legs with their smiles fitted, and
    the signs the legs enter its log return with.

    The one set-up the triangle and the implied quotes share, so the two
    cannot build one cross out of different legs.
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
    return surface, curve, (leg_a, leg_b), moments.triangle_coefficients(pair, leg_a, leg_b)


def triangle_table(book, pair: str, *, method: str | None = None, cut: str = "NY",
                   deltas=(0.10, 0.25), with_noise: bool = True,
                   tenors=None, implied_vol_vol: bool = True, dependence=None,
                   dependence_band=None, dependence_source=None,
                   implied_corr_spot: bool = True) -> list[TriangleRow]:
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

    A dependence marked on ``CROSS_DEPENDENCE`` (``Book.dependence_at``) is
    what the legs are tied together with instead, holding the same combined
    ATM (:func:`_leg_law`); the row carries it, and the Gaussian copula's
    answer beside it.  ``implied_vol_vol`` backs out, on every row, the
    vol-vol correlation that gives the marked 25-delta butterfly -- the
    expensive part of the table, and the only reason to switch it off.
    ``implied_corr_spot`` does the same for the marked 25-delta risk reversal
    where a correlation vol is marked, and only when ``implied_vol_vol`` is on.

    ``dependence`` (``{tenor: Dependence or None}``) replaces the book's at the
    tenors it names, with ``dependence_source`` (``{tenor: str}``) saying what
    it is; ``dependence_band`` (``{tenor: (Dependence, ...)}``) is evaluated
    around it and how far the triangle moves across it is the row's
    ``dependence_noise``.  That is how the relative-value grid prices the
    triangle on the legs' measured dependence and its uncertainty.
    """
    surface, curve, (leg_a, leg_b), (ca, cb) = _cross_legs(book, pair)

    rows: list[TriangleRow] = []
    for tenor in (tenors or book.data.tenors_for(pair)):
        t = surface.tenor_years(tenor)
        expiry = book.clock.datetime_from_years(t)
        warn: list[str] = []
        rho = float(np.asarray(curve.correlation(t)))
        conv = surface.slice_conv(t)
        dep = None
        given = dependence is not None and tenor in dependence
        try:
            dep = dependence[tenor] if given else book.dependence_at(pair, t)
            source = ((dependence_source or {}).get(tenor, "") if given
                      else "CROSS_DEPENDENCE" if dep is not None else "")
            source = source or ("Gaussian copula" if dep is None else "given")
            da = moments.distribution_from_surface(book[leg_a], expiry, method=method,
                                                   cut=cut, label=leg_a)
            db = moments.distribution_from_surface(book[leg_b], expiry, method=method,
                                                   cut=cut, label=leg_b)
            comb, gauss, dep = _leg_law(book, pair, da, db, (ca, cb), rho, conv, t,
                                        dependence=dep)
            got = comb.table(deltas)
            gauss_got = got if dep is None else gauss.table(deltas)
            warn.extend(comb.warnings)
        except (ValueError, ArithmeticError, ZeroDivisionError, ConvergenceError) as exc:
            # The row is kept, empty, with the reason on it.  Dropping it here
            # left the whole table silently short -- and when every tenor
            # failed, silently empty.
            nan = float("nan")
            rows.append(TriangleRow(
                tenor=tenor, t=t, rho=rho, coefficients=(ca, cb),
                marked={}, triangle={}, difference={}, noise={},
                variance_triangle_atm=nan, smile_convexity=nan, leg_atm=(nan, nan),
                implied_correlation=None, leg_vega=(nan, nan), rho_vega=nan,
                warnings=tuple(warn) + (f"the triangle could not be built: {exc}",),
                vol_vol=None if dep is None else dep.vol_vol,
                corr_vol=0.0 if dep is None else dep.corr_vol,
                corr_spot=0.0 if dep is None else dep.corr_spot))
            continue

        by = smile_points(surface.smile_table(expiry, deltas=tuple(deltas), method=method,
                                              cut=cut))
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
        gaussian = ({} if dep is None
                    else {k: float(gauss_got[k]) for k in marked if k in gauss_got})
        implied_vv, implied_vv_note = None, ""
        if implied_vol_vol and "fly25" in marked:
            implied_vv, implied_vv_note = moments.implied_vol_vol(
                da, db, (ca, cb), rho, conv, marked["fly25"],
                corr_vol=0.0 if dep is None else dep.corr_vol,
                corr_spot=0.0 if dep is None else dep.corr_spot,
                target_atm=float(gauss_got["atm"]))
        implied_cs, implied_cs_note = None, ""
        if (implied_vol_vol and implied_corr_spot and "rr25" in marked
                and dep is not None and dep.corr_vol > 0.0):
            implied_cs, implied_cs_note = moments.implied_corr_spot(
                da, db, (ca, cb), rho, conv, marked["rr25"], vol_vol=dep.vol_vol,
                corr_vol=dep.corr_vol, target_atm=float(gauss_got["atm"]))

        dep_noise: dict[str, float] = {}
        band = (dependence_band or {}).get(tenor) or ()
        if band:
            # The band and its centre on one grid, so the spread is the
            # dependence's and not the difference between two grids.
            try:
                def held(d):
                    return moments.combine_holding_atm(
                        da, db, (ca, cb), rho, conv, d, target_atm=float(gauss_got["atm"]),
                        **moments.IMPLIED_GRID).table(deltas)
                centre = held(dep)
                for alt in band:
                    moved = held(alt)
                    for k in marked:
                        if k in moved and k in centre:
                            dep_noise[k] = max(dep_noise.get(k, 0.0), abs(moved[k] - centre[k]))
            except (ValueError, ArithmeticError, ConvergenceError) as exc:
                warn.append(f"the dependence's uncertainty could not be priced, so it is not in "
                            f"the noise floor: {exc}")

        noise: dict[str, float] = {}
        if with_noise:
            for leg, dist in ((leg_a, da), (leg_b, db)):
                ref = _leg_reference(book[leg], expiry, method, cut, deltas)
                err = moments.reconstruction_error(dist, book[leg].slice_conv(t), ref, deltas)
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
        dva, dvb, drho = _vega_split(va, vb, rho, ca, cb, var_atm)
        rows.append(TriangleRow(
            tenor=tenor, t=t, rho=rho, coefficients=(ca, cb),
            marked=marked, triangle=triangle, difference=difference, noise=noise,
            variance_triangle_atm=var_atm,
            smile_convexity=triangle.get("atm", float("nan")) - var_atm,
            leg_atm=(va, vb), implied_correlation=implied_rho,
            leg_vega=(dva, dvb), rho_vega=drho, warnings=tuple(warn),
            vol_vol=None if dep is None else dep.vol_vol,
            corr_vol=0.0 if dep is None else dep.corr_vol,
            corr_spot=0.0 if dep is None else dep.corr_spot,
            copula_rho=comb.rho, gaussian=gaussian,
            implied_vol_vol=implied_vv, implied_vol_vol_note=implied_vv_note,
            implied_corr_spot=implied_cs, implied_corr_spot_note=implied_cs_note,
            dependence_source=source, dependence_noise=dep_noise,
        ))
    return rows


@dataclass(frozen=True)
class ImpliedQuoteRow:
    """The four quotes a cross's two legs and its correlation imply at one tenor.

    ``quotes`` is keyed by the pair sheet's own fields (``rr_25``, ``st_25``,
    ...) and holds what the sheet holds: risk reversals and **market**
    strangles, in decimals.  Empty, with ``error`` saying why, when the tenor
    could not be built -- the row keeps its place either way.
    """

    tenor: str
    t: float
    rho: float
    quotes: dict[str, float]
    #: The combined smile's own at-the-money, which the strangles are measured
    #: from, beside the cross's marked one.  They differ by the legs' smile
    #: convexity (``TriangleRow.smile_convexity``), which no quote carries.
    atm: float
    cross_atm: float
    leg_atm: tuple[float, float]
    warnings: tuple[str, ...] = ()
    error: str = ""
    #: The dependence marked at this tenor and the copula correlation it ran
    #: at -- :class:`TriangleRow`'s four, for the same law.
    vol_vol: float | None = None
    corr_vol: float = 0.0
    corr_spot: float = 0.0
    copula_rho: float = float("nan")


def implied_cross_quotes(book, pair: str, *, method: str | None = None, cut: str = "NY",
                         tenors=None) -> list[ImpliedQuoteRow]:
    """The risk reversals and strangles a cross's dollar legs imply, tenor by tenor.

    For a cross too illiquid to have a broker run of its own: the two legs'
    marked smiles are tied together at the cross's own correlation curve --
    the one its ATM is already built from -- by the Gaussian copula of
    :func:`triangle_table`, and the cross's smile is read off the result in
    the sheet's convention.  The risk reversal is the smile's; the strangle is
    the **market** strangle (``Combined.market_strangle``), because that is
    what the sheet's ``ST`` columns hold and what the fit reads them as.

    Read, never written: the marking screen writes what it is handed as
    ordinary quote overwrites.  The ATM is not among them -- the cross's curve
    already is the variance triangle of its legs.  The assumptions are the
    triangle's (``moments.py``, CLAUDE.md §7): a Gaussian copula unless the
    ``CROSS_DEPENDENCE`` tab marks the cross otherwise (:func:`_leg_law`, the
    triangle's own), and no change of measure between the legs' domestic
    currencies and the cross's.
    """
    surface, curve, (leg_a, leg_b), (ca, cb) = _cross_legs(book, pair)
    rows: list[ImpliedQuoteRow] = []
    nan = float("nan")
    for tenor in (tenors or book.data.tenors_for(pair)):
        t = surface.tenor_years(tenor)
        expiry = book.clock.datetime_from_years(t)
        rho = float(np.asarray(curve.correlation(t)))
        warn: list[str] = []
        dependence = None
        try:
            dependence = book.dependence_at(pair, t)
            cross_atm = float(surface.atm_vol(expiry, cut))
            leg_atm = (float(book[leg_a].atm_vol(expiry, cut)),
                       float(book[leg_b].atm_vol(expiry, cut)))
            da = moments.distribution_from_surface(book[leg_a], expiry, method=method,
                                                   cut=cut, label=leg_a)
            db = moments.distribution_from_surface(book[leg_b], expiry, method=method,
                                                   cut=cut, label=leg_b)
            warn.extend(f"{d.label}: {w}" for d in (da, db) for w in d.warnings)
            comb, _, dependence = _leg_law(book, pair, da, db, (ca, cb), rho,
                                           surface.slice_conv(t), t)
            warn.extend(comb.warnings)
            got = comb.table((0.10, 0.25))
            quotes = {"rr_25": float(got["rr25"]), "rr_10": float(got["rr10"]),
                      "st_25": comb.market_strangle(0.25, got["atm"]),
                      "st_10": comb.market_strangle(0.10, got["atm"])}
            bad = [f for f in ("st_25", "st_10") if not quotes[f] > 0.0]
            if bad:
                # A sheet refuses a strangle that is not positive, and so does
                # the quote box; saying so on the tenor's own row keeps a write
                # of the whole table from failing on it.
                raise ValueError(
                    "the legs imply a market strangle that is not positive ("
                    + ", ".join(f"{f} {quotes[f] * 100:.4g}" for f in bad)
                    + "), which a sheet cannot hold")
            rows.append(ImpliedQuoteRow(tenor=tenor, t=t, rho=rho, quotes=quotes,
                                        atm=float(got["atm"]), cross_atm=cross_atm,
                                        leg_atm=leg_atm, warnings=tuple(warn),
                                        vol_vol=None if dependence is None else dependence.vol_vol,
                                        corr_vol=0.0 if dependence is None else dependence.corr_vol,
                                        corr_spot=0.0 if dependence is None else dependence.corr_spot,
                                        copula_rho=comb.rho))
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            # The row is kept, empty, with the reason on it -- the triangle's
            # own rule, for the same reason.
            rows.append(ImpliedQuoteRow(tenor=tenor, t=t, rho=rho, quotes={}, atm=nan,
                                        cross_atm=nan, leg_atm=(nan, nan),
                                        warnings=tuple(warn), error=str(exc),
                                        vol_vol=None if dependence is None else dependence.vol_vol,
                                        corr_vol=0.0 if dependence is None else dependence.corr_vol,
                                        corr_spot=0.0 if dependence is None else dependence.corr_spot))
    return rows


@dataclass(frozen=True)
class CorrelationRow:
    """A cross's correlation at one tenor: as marked, and as it was realized.

    ``marked`` is the cross curve's correlation read at the tenor's expiry --
    the number its ATM is built from there, interpolated off the curve's
    initial / final / decay -- and needs no history.  ``realized`` is the
    legs' realized correlation over ``window_days`` and is ``None``, with the
    reason in ``error``, when it could not be measured.
    """

    tenor: str
    t: float
    marked: float
    window_days: float
    realized: float | None = None
    realized_se: float | None = None
    difference: float | None = None       # marked - realized
    observations: int = 0
    basis: str | None = None
    start: str | None = None
    end: str | None = None
    warnings: tuple[str, ...] = ()
    error: str = ""
    #: The legs' realized vol-vol correlation and correlation vol at this
    #: tenor (:func:`measure_dependence`) -- what ``CROSS_DEPENDENCE`` is
    #: measured against.  ``None`` without a history.
    dependence: "MeasuredDependence | None" = None


@dataclass(frozen=True)
class MeasuredDependence:
    """A cross's dependence at one tenor as its two legs' history shows it.

    Physical, not priced: the market charges for dependence breaking down in a
    stress, and a history does not.  ``notes`` carries what could not be
    measured and why, each part independently -- a sheet with no ATM column
    still has a correlation vol, and a short history still has a vol-vol.
    The correlation-spot correlation is the one part read off the market's
    own quotes rather than realized returns alone, and needs the cross's sheet
    (:func:`history.realized_corr_spot`).
    """

    vol_vol: float | None = None
    vol_vol_se: float | None = None
    vol_vol_observations: int = 0
    vol_vol_tenors: tuple[str, ...] = ()
    corr_vol: float | None = None
    corr_vol_se: float | None = None
    corr_vol_raw: float | None = None        # the windows' scatter as measured
    corr_vol_noise: float | None = None      # what sampling alone gives
    corr_vol_windows: float | None = None    # independent windows in the lookback
    corr_spot: float | None = None
    corr_spot_se: float | None = None
    corr_spot_raw: float | None = None       # before the quotes' noise was taken out
    corr_spot_observations: int = 0
    corr_spot_tenor: str | None = None       # the ATM tenor all three sheets were read on
    window_days: float = 0.0
    basis: str | None = None
    notes: tuple[str, ...] = ()

    def band(self, dependence: "moments.Dependence", rho: float):
        """The dependences one standard error either side of ``dependence``.

        The vol-vol correlation moved by its own standard error, and the
        correlation vol by its own, each kept inside what the model can hold
        at ``rho`` -- the uncertainty a measured dependence carries into a
        triangle, and so into a noise floor.  The correlation-spot correlation
        is moved by its own too, where it was measured.
        """
        out = []
        vv, cv, cs = dependence.vol_vol, dependence.corr_vol, dependence.corr_spot
        if vv is not None and self.vol_vol_se:
            for step in (-self.vol_vol_se, self.vol_vol_se):
                out.append(moments.Dependence(min(max(vv + step, -1.0), 1.0), cv,
                                              dependence.corr_spot))
        if self.corr_vol_se:
            cap = max(1.0 - abs(rho) - 1e-6, 0.0)
            for step in (-self.corr_vol_se, self.corr_vol_se):
                alt = min(max(cv + step, 0.0), cap)
                if alt != cv:
                    out.append(moments.Dependence(vv, alt, dependence.corr_spot))
        if self.corr_spot_se and cv > 0.0:
            for step in (-self.corr_spot_se, self.corr_spot_se):
                out.append(moments.Dependence(vv, cv, min(max(cs + step, -1.0), 1.0)))
        return tuple(d for d in out if d != dependence)


def measure_dependence(history, leg_a: str, leg_b: str, tenor: str, t: float, *,
                       window_days: float | None = None,
                       basis: str = "auto",
                       corr_vol_lookback_days: float | None = None,
                       pair: str | None = None) -> MeasuredDependence:
    """The legs' realized vol-vol correlation and correlation vol at one tenor.

    The vol-vol correlation is :func:`history.realized_vol_vol` at the tenor
    over its own year; the correlation vol is :func:`history.realized_corr_vol`
    on windows the tenor long (or ``window_days``), on the basis the realized
    correlation beside it is measured on, across ``corr_vol_lookback_days``
    (``history.CORR_VOL_DAYS`` when not given).  The lookback is what bounds
    the longest tenor with a correlation vol: it has to hold three independent
    windows the tenor long.  The correlation-spot correlation is
    :func:`history.realized_corr_spot` at the tenor over its own year, and
    needs ``pair`` -- the cross -- to have a sheet with its at-the-money on it.
    Any part that cannot be measured is ``None`` with its reason in ``notes``.
    """
    window = float(window_days) if window_days else t * 365.2425
    notes: list[str] = []
    missing = [leg for leg in (leg_a, leg_b) if history is None or leg not in history]
    if missing:
        return MeasuredDependence(window_days=window, notes=(
            ("no historical workbook is loaded" if history is None else
             f"the historical workbook has no sheet for {' or '.join(missing)}")
            + ", so the legs' dependence cannot be measured",))
    vv = cv = None
    try:
        vv = realized_vol_vol(history[leg_a], history[leg_b], tenor)
        notes.extend(f"vol-vol: {w}" for w in vv.warnings)
    except (HistoryError, ValueError, ArithmeticError) as exc:
        notes.append(f"vol-vol correlation not measured: {exc}")
    try:
        cv = realized_corr_vol(history[leg_a], history[leg_b], window,
                               corr_vol_lookback_days or CORR_VOL_DAYS, basis=basis,
                               basis_tenor=tenor)
        notes.extend(f"correlation vol: {w}" for w in cv.warnings)
    except (HistoryError, ValueError, ArithmeticError) as exc:
        notes.append(f"correlation vol not measured: {exc}")
    cs = None
    if pair is not None:
        try:
            if pair not in history:
                raise HistoryError(f"the historical workbook has no sheet for {pair}, so its "
                                   f"implied correlation cannot be read")
            cs = realized_corr_spot(history[leg_a], history[leg_b], history[pair],
                                    moments.triangle_coefficients(pair, leg_a, leg_b), tenor)
            notes.extend(f"corr spot: {w}" for w in cs.warnings)
        except (HistoryError, ValueError, ArithmeticError) as exc:
            notes.append(f"correlation-spot correlation not measured: {exc}")
    return MeasuredDependence(
        vol_vol=None if vv is None else vv.rho,
        vol_vol_se=None if vv is None else vv.rho_se,
        vol_vol_observations=0 if vv is None else vv.observations,
        vol_vol_tenors=() if vv is None else vv.tenors,
        corr_vol=None if cv is None else cv.corr_vol,
        corr_vol_se=None if cv is None else cv.corr_vol_se,
        corr_vol_raw=None if cv is None else cv.raw_sd,
        corr_vol_noise=None if cv is None else cv.noise_sd,
        corr_vol_windows=None if cv is None else cv.independent_windows,
        corr_spot=None if cs is None else cs.corr_spot,
        corr_spot_se=None if cs is None else cs.corr_spot_se,
        corr_spot_raw=None if cs is None else cs.raw,
        corr_spot_observations=0 if cs is None else cs.observations,
        corr_spot_tenor=None if cs is None else cs.tenor,
        window_days=window, basis=None if cv is None else cv.basis, notes=tuple(notes))


@dataclass(frozen=True)
class CorrelationTable:
    pair: str
    legs: tuple[str, str]
    lookback_days: float | None           # None: matched to each tenor
    rows: list[CorrelationRow]
    #: Why no row has a realized figure, when the reason is the same for all
    #: of them (no history, or no sheet for a leg); empty otherwise.
    unavailable: str = ""


def correlation_table(book, pair: str, history=None, *, lookback_days: float | None = None,
                      realized_basis: str = "auto", tenors=None) -> CorrelationTable:
    """A cross's correlation term structure beside the legs' realized correlation.

    The marked half is the cross's own curve at each tenor and is built with or
    without a history, so no historical workbook never empties it.  The
    realized half is :func:`history.realized_correlation` on the two legs'
    sheets, windowed the way :func:`realized_table` windows a realized
    volatility: ``lookback_days`` of ``None`` matches each tenor -- a one-month
    correlation against one month of returns -- and a number holds every tenor
    to that one window.  On ``auto`` each tenor is measured on the forward to
    it wherever both legs' sheets can build one, which is the realized
    volatility's own basis.  A tenor that cannot be measured keeps its row and
    its reason.
    """
    spec = book.data.pairs.get(pair)
    if spec is None or not spec.is_cross:
        raise ValueError(f"{pair} is not a cross in this workbook, so it has no correlation")
    surface = book[pair]
    curve = surface.atm
    if not isinstance(curve, CrossAtmCurve):
        raise ValueError(f"{pair} is marked as a cross but its ATM curve is not a cross curve")
    leg_a, leg_b = spec.legs
    unavailable = ""
    if history is None:
        unavailable = ("no historical workbook is loaded, so there is no realized correlation "
                       "to compare against")
    else:
        missing = [leg for leg in (leg_a, leg_b) if leg not in history]
        if missing:
            unavailable = (f"the historical workbook has no sheet for {' or '.join(missing)}, "
                           f"so the legs' realized correlation cannot be measured; it holds "
                           f"{', '.join(sorted(history.pairs)) or 'no readable sheets'}")
    rows: list[CorrelationRow] = []
    for tenor in (tenors or book.data.tenors_for(pair)):
        t = surface.tenor_years(tenor)
        marked = float(np.asarray(curve.correlation(t)))
        window = float(lookback_days) if lookback_days else t * 365.2425
        if unavailable:
            rows.append(CorrelationRow(tenor=tenor, t=t, marked=marked, window_days=window))
            continue
        measured = measure_dependence(history, leg_a, leg_b, tenor, t, window_days=window,
                                      basis=realized_basis, pair=pair)
        try:
            rc = realized_correlation(history[leg_a], history[leg_b], window,
                                      basis=realized_basis, basis_tenor=tenor)
        except Exception as exc:  # noqa: BLE001 - one bad tenor must not kill the table
            rows.append(CorrelationRow(tenor=tenor, t=t, marked=marked, window_days=window,
                                       error=str(exc), dependence=measured))
            continue
        rows.append(CorrelationRow(
            tenor=tenor, t=t, marked=marked, window_days=window, realized=rc.rho,
            realized_se=rc.rho_se, difference=marked - rc.rho,
            observations=rc.observations, basis=rc.basis, start=rc.start.isoformat(),
            end=rc.end.isoformat(), warnings=rc.warnings, dependence=measured))
    return CorrelationTable(pair=pair, legs=(leg_a, leg_b), lookback_days=lookback_days,
                            rows=rows, unavailable=unavailable)


#: The correlation curve's coefficients by the names the marking card ticks,
#: in the order they are shown.  The same three names the fit to the ATM
#: overwrites frees on a cross; the short add-on is not here because a
#: realized correlation says nothing about a volatility add-on.
CORRELATION_FIT_DOF = ("initial", "final", "decay")
_CORR_KNOB = {"initial": "corr_initial", "final": "corr_final", "decay": "corr_decay"}
#: The decay is not here: it is fitted in the marking agent's mean-reversion
#: range (``marketmaker.MEAN_REVERSION_RANGE``, or the range typed on its
#: card), so one judgement bounds how fast any curve on the desk may turn.
_CORR_BOUNDS = {"initial": (-0.999, 0.999), "final": (-0.999, 0.999)}
#: A fitted decay past this many e-foldings by the shortest target puts the
#: initial correlation where no window measured it, and is said.
_CORR_DECAY_SPAN = 1.0
#: A standard error is never allowed below this in the weights: a realized
#: correlation near one has a vanishing ``(1 - rho^2)/sqrt(n)``, and a weight
#: without a floor would let one tenor pin the whole curve.
_CORR_SE_FLOOR = 0.01


@dataclass(frozen=True)
class CorrelationFitRow:
    """One tenor of :func:`fit_correlation_curve`.

    ``before`` / ``after`` are the curve's correlation at the tenor, ``miss``
    is fitted less realized and ``z`` the same in standard errors.  The ATM
    columns are the cross's curve volatility (decimals, overwrites ignored)
    under each curve, so the move a fit makes is read in volatility as well
    as in correlation.  A tenor that is not a target keeps its row with
    ``used`` false and the reason.
    """

    tenor: str
    t: float
    before: float
    after: float
    realized: float | None = None
    realized_se: float | None = None
    miss: float | None = None
    z: float | None = None
    atm_before: float | None = None
    atm_after: float | None = None
    used: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CorrelationFit:
    pair: str
    legs: tuple[str, str]
    lookback_days: float | None
    free: tuple[str, ...]
    before: dict[str, float]
    after: dict[str, float]
    rows: list[CorrelationFitRow]
    rmse: float
    max_error: float
    max_error_tenor: str
    #: The weighted misses' root mean square, in standard errors: about one
    #: is a curve the history cannot tell from the realized ladder.
    rms_z: float
    converged: bool
    message: str
    #: The range the decay was fitted in, and whether it is the house one.
    reversion_range: tuple[float, float] = (0.0, 0.0)
    reversion_house: bool = True
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def fit_correlation_curve(book, pair: str, history, *, free=CORRELATION_FIT_DOF,
                          lookback_days: float | None = None, realized_basis: str = "auto",
                          tenors=None,
                          reversion_range: tuple[float, float] | None = None) -> CorrelationFit:
    """A cross's correlation curve put through its legs' realized correlations.

    The targets are :func:`correlation_table`'s realized column -- the same
    window and basis as the marking card shows -- inside the pair's fit cutoff
    (``marketmaker.split_at_fit_cutoff``, like every other curve fit), each
    weighted by one over its standard error so a one-month correlation off
    twenty returns does not count as much as a year's.  The coefficients not
    in ``free`` are pinned at what the curve holds now.  Nothing is written:
    the fit runs on the coefficients alone and ``after`` is what the caller
    puts on the curve when it wants to keep it.

    The decay is fitted inside ``reversion_range`` -- the marking agent's
    mean-reversion range, ``None`` for the house one -- read by the same
    ``marketmaker.check_reversion_range``, so the correlation cannot turn
    faster or slower than the desk lets a backbone.

    Refused rather than half-answered: a pair that is not a cross, a curve
    that is not three coefficients, no history, a name that is not a
    coefficient, and fewer measured tenors than free coefficients.
    """
    import copy
    from scipy.optimize import least_squares
    from .cross import CorrelationCurve
    from .marketmaker import (MEAN_REVERSION_RANGE, check_reversion_range, reversion_nodes,
                              split_at_fit_cutoff)

    house = reversion_range is None
    rev = MEAN_REVERSION_RANGE if house else check_reversion_range(reversion_range)

    wanted = [str(d).strip().lower() for d in (free or ()) if str(d).strip()]
    unknown = [d for d in wanted if d not in CORRELATION_FIT_DOF]
    if unknown:
        raise ValueError(f"{', '.join(unknown)} is not a coefficient of the correlation curve; "
                         f"expected {', '.join(CORRELATION_FIT_DOF)}")
    free = tuple(d for d in CORRELATION_FIT_DOF if d in wanted)
    if not free:
        raise ValueError("no coefficient of the correlation curve was left free, so there "
                         "is nothing to fit")
    table = correlation_table(book, pair, history, lookback_days=lookback_days,
                              realized_basis=realized_basis, tenors=tenors)
    if table.unavailable:
        raise ValueError(table.unavailable)
    surface = book[pair]
    atm = surface.atm
    if not isinstance(atm.correlation, CorrelationCurve):
        raise ValueError(f"{pair}'s correlation is not an initial / final / decay curve, so "
                         f"there are no coefficients to fit")
    c = atm.correlation
    before = {"initial": float(c.initial), "final": float(c.final), "decay": float(c.decay)}

    measured = [r for r in table.rows if r.realized is not None and not r.error]
    targets, dropped, cut_note = split_at_fit_cutoff(surface, measured)
    notes: list[str] = []
    warnings: list[str] = []
    if cut_note:
        (warnings if not targets else notes).append(cut_note)
    if len(targets) < len(free):
        raise ValueError(
            f"{len(targets)} tenor(s) with a realized correlation cannot determine "
            f"{len(free)} free coefficient(s) ({', '.join(free)})"
            + (f"; {cut_note}" if cut_note and not targets else "")
            + "; free fewer coefficients or lengthen the lookback")
    targets = sorted(targets, key=lambda r: r.t)
    ts = np.array([r.t for r in targets], dtype=float)
    goals = np.array([r.realized for r in targets], dtype=float)
    se = np.array([max(r.realized_se or 0.0, _CORR_SE_FLOOR) for r in targets], dtype=float)

    def curve_at(values: dict[str, float], t) -> np.ndarray:
        final, initial = values["final"], values["initial"]
        return final - (final - initial) * np.exp(-values["decay"] * np.asarray(t, dtype=float))

    def residuals(x: np.ndarray) -> np.ndarray:
        values = {**before, **dict(zip(free, (float(v) for v in x)))}
        return (curve_at(values, ts) - goals) / se

    bounds = {**_CORR_BOUNDS, "decay": rev}
    range_name = (f"the house mean-reversion range {rev[0]:g}-{rev[1]:g}" if house else
                  f"the marking agent's mean-reversion range {rev[0]:g}-{rev[1]:g}, as set rather than "
                  f"the house one")
    if "decay" in free:
        notes.append(f"the decay is fitted in {range_name}")
        if not rev[0] <= before["decay"] <= rev[1]:
            notes.append(f"the curve's decay of {before['decay']:g} is outside that range, so "
                         f"a fit cannot give it back")
    lo = np.array([bounds[k][0] for k in free], dtype=float)
    hi = np.array([bounds[k][1] for k in free], dtype=float)
    # Levels read straight off the shortest and longest targets; the decay has
    # no such reading and is swept before any polishing, so a local minimum in
    # it is not where the fit starts.
    level = {"initial": float(goals[0]), "final": float(goals[-1])}
    seeds = []
    for decay in (reversion_nodes(rev) if "decay" in free else (before["decay"],)):
        seed = {**before, **{k: v for k, v in {**level, "decay": decay}.items() if k in free}}
        seeds.append(np.clip([seed[k] for k in free], lo + 1e-12, hi - 1e-12))
    best = min(seeds, key=lambda x: float(np.sum(residuals(x) ** 2)))
    try:
        sol = least_squares(residuals, best, bounds=(lo, hi), xtol=1e-13, ftol=1e-13,
                            gtol=1e-13, max_nfev=400)
        x, converged = sol.x, bool(sol.success)
        message = "converged" if converged else f"least-squares stopped: {sol.message}"
    except Exception as exc:  # noqa: BLE001 - fall back to the sweep, but say so
        x, converged = best, False
        message = (f"polish failed ({type(exc).__name__}: {exc}); reporting the best "
                   f"sweep node")
    after = {**before, **dict(zip(free, (float(v) for v in x)))}
    if not converged:
        warnings.append(message)
    for k in free:
        if min(abs(after[k] - bounds[k][0]), abs(after[k] - bounds[k][1])) < 1e-6:
            warnings.append(
                f"the fitted {k} sits on its bound [{bounds[k][0]:g}, {bounds[k][1]:g}]"
                + (f" ({range_name})" if k == "decay" else "")
                + "; the realized ladder wants a curve this one cannot draw")
    if "decay" in free and after["decay"] * float(ts[0]) > _CORR_DECAY_SPAN:
        warnings.append(f"the fitted decay is more than one e-folding by the shortest target "
                        f"({targets[0].tenor}), so the initial correlation is set where no "
                        f"window measured it")
    span = float(ts[-1])
    if "decay" in free and abs(after["final"] - after["initial"]) < 0.02:
        notes.append("the fitted initial and final are within 0.02 of each other, so the "
                     "decay is barely determined by the data")
    elif {"final", "decay"} <= set(free) and after["decay"] * span < 0.1:
        warnings.append(f"the fitted decay is so slow that the longest target "
                        f"({targets[-1].tenor}) is barely a tenth of the way to the final, so "
                        f"the final is not determined by the history; pin the decay or the final")
    if table.lookback_days:
        warnings.append(f"every tenor is measured over the same {table.lookback_days:g}-day "
                        f"window, so the targets differ only by basis and carry no term "
                        f"structure; 'match' gives each tenor its own window")

    # The cross's ATM under each curve, off a copy: the book is not touched.
    rows_atm: dict[str, tuple[float | None, float | None]] = {}
    try:
        work = copy.deepcopy(atm)
        problems = work.set_correlation(after["initial"], after["final"], after["decay"])
        if problems:
            raise ValueError("; ".join(problems))
        for r in table.rows:
            rows_atm[r.tenor] = (atm.curve_vol(r.t), work.curve_vol(r.t))
    except Exception as exc:  # noqa: BLE001 - the fit stands without the ATM columns
        warnings.append(f"the cross's ATM under the fitted curve could not be read: {exc}")

    used = {r.tenor for r in targets}
    beyond = set(dropped)
    rows: list[CorrelationFitRow] = []
    for r in table.rows:
        a_before, a_after = rows_atm.get(r.tenor, (None, None))
        fitted = float(curve_at(after, r.t))
        miss = None if r.realized is None else fitted - r.realized
        z = None if miss is None else miss / max(r.realized_se or 0.0, _CORR_SE_FLOOR)
        reason = ("" if r.tenor in used else
                  "beyond the fit cutoff" if r.tenor in beyond else
                  (r.error or "no realized correlation"))
        rows.append(CorrelationFitRow(
            tenor=r.tenor, t=r.t, before=float(curve_at(before, r.t)), after=fitted,
            realized=r.realized, realized_se=r.realized_se, miss=miss, z=z,
            atm_before=a_before, atm_after=a_after, used=r.tenor in used, reason=reason))
    misses = [(row.miss, row.tenor) for row in rows if row.used]
    worst = max(misses, key=lambda m: abs(m[0]))
    z_used = [row.z for row in rows if row.used]
    notes.append("the targets are realized correlations -- physical, not priced; matched "
                 "windows overlap, so neighbouring tenors are not independent evidence")
    return CorrelationFit(
        pair=table.pair, legs=table.legs, lookback_days=table.lookback_days, free=free,
        before=before, after=after, rows=rows,
        rmse=math.sqrt(sum(m * m for m, _ in misses) / len(misses)),
        max_error=worst[0], max_error_tenor=worst[1],
        rms_z=math.sqrt(sum(v * v for v in z_used) / len(z_used)),
        converged=converged, message=message, reversion_range=tuple(rev),
        reversion_house=house, warnings=tuple(warnings), notes=tuple(notes))


#: The premium sources a dependence suggestion can take: the cross's own marked
#: butterfly, none at all, or -- anything else -- another cross's, named.
PREMIUM_OWN, PREMIUM_NONE = "own", "none"


@dataclass(frozen=True)
class DependenceRow:
    """One tenor of :func:`dependence_table`: measured, implied, and suggested."""

    tenor: str
    t: float
    rho: float
    measured: MeasuredDependence
    #: What ``CROSS_DEPENDENCE`` marks here now (``None`` / 0 for nothing).
    marked_vol_vol: float | None = None
    marked_corr_vol: float = 0.0
    marked_corr_spot: float = 0.0
    marked_fly25: float | None = None
    marked_rr25: float | None = None
    #: The vol-vol correlation the marked 25-delta fly asks for, holding the
    #: correlation vol the suggestion marks here, and the premium it carries
    #: over the measured vol-vol correlation.  The premium is put on the
    #: vol-vol correlation alone: one butterfly identifies one number.
    implied_vol_vol: float | None = None
    implied_note: str = ""
    #: The correlation vol that was held: the measured one, or -- where none
    #: was measured -- what the book reads there off the tenors that were
    #: (``implied_corr_vol_from`` names which), 0 where no tenor was.
    implied_corr_vol: float = 0.0
    implied_corr_vol_from: str = ""
    premium: float | None = None
    #: The premium the suggestion uses and where it came from.
    premium_used: float | None = None
    premium_source: str = PREMIUM_OWN
    suggested_vol_vol: float | None = None
    suggested_corr_vol: float | None = None
    #: The risk reversal's twin of the vol-vol correlation's four: the
    #: correlation-spot correlation the marked 25-delta risk reversal asks for,
    #: holding the suggested vol-vol correlation and correlation vol; its
    #: premium over the measured one; the premium used (from the same source);
    #: and measured plus that.  Searched only where a correlation vol is held.
    implied_corr_spot: float | None = None
    implied_corr_spot_note: str = ""
    corr_spot_premium: float | None = None
    corr_spot_premium_used: float | None = None
    suggested_corr_spot: float | None = None
    #: Where a suggestion that could not be made at this tenor was filled from
    #: the tenors that have one -- ``"interpolated between 6m and 1y"``, ``"held
    #: flat from 6m"`` -- or empty where the suggestion is this tenor's own.
    #: ``reason`` still says why it could not be.
    vol_vol_filled: str = ""
    corr_vol_filled: str = ""
    corr_spot_filled: str = ""
    reason: str = ""
    corr_spot_reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependenceTable:
    pair: str
    legs: tuple[str, str]
    premium: str
    rows: list[DependenceRow]
    unavailable: str = ""
    #: The lookback the correlation vol was measured across, in days.
    corr_vol_lookback_days: float = CORR_VOL_DAYS
    #: Whether a tenor without a suggestion was filled from its neighbours.
    filled: bool = False


def _ladder_value(rungs, t: float) -> tuple[float | None, str]:
    """A value off ``[(t, value, tenor), ...]`` sorted by ``t``, and how it was read.

    ``Book.dependence_at``'s rule: linear in time between two rungs, flat
    outside them.  ``(None, "")`` with no rungs.
    """
    if not rungs:
        return None, ""
    if t <= rungs[0][0]:
        return rungs[0][1], f"held flat from {rungs[0][2]}"
    if t >= rungs[-1][0]:
        return rungs[-1][1], f"held flat from {rungs[-1][2]}"
    k = next(i for i, p in enumerate(rungs) if p[0] >= t)
    (t0, v0, n0), (t1, v1, n1) = rungs[k - 1], rungs[k]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0), f"interpolated between {n0} and {n1}"


def _fill_dependence_gaps(rows: list[DependenceRow]) -> list[DependenceRow]:
    """Each suggestion a tenor could not make, filled from the tenors that made one.

    The rule ``Book.dependence_at`` reads the tab by -- each value its own
    ladder, linear in time between two rungs and flat outside them -- so a
    filled row marks what the book would have read at that tenor had the row
    been left blank, and says so instead of leaving the gap to be guessed at.
    A correlation vol filled is kept inside what the model holds at *that*
    tenor's correlation, as a measured one is.
    """
    def ladder(get):
        return sorted((r.t, get(r), r.tenor) for r in rows if get(r) is not None)

    fill = _ladder_value
    vv_rungs = ladder(lambda r: r.suggested_vol_vol)
    cv_rungs = ladder(lambda r: r.suggested_corr_vol)
    cs_rungs = ladder(lambda r: r.suggested_corr_spot)
    out: list[DependenceRow] = []
    for r in rows:
        change: dict[str, object] = {}
        warnings = list(r.warnings)
        if r.suggested_vol_vol is None:
            vv, how = fill(vv_rungs, r.t)
            if vv is not None:
                change.update(suggested_vol_vol=vv, vol_vol_filled=how)
        if r.suggested_corr_vol is None:
            cv, how = fill(cv_rungs, r.t)
            if cv is not None:
                cap = max(1.0 - abs(r.rho) - 1e-6, 0.0)
                if cv > cap:
                    warnings.append(f"a filled correlation vol of {cv:.3f} around a correlation "
                                    f"of {r.rho:+.3f} reaches past 1, so it is held at {cap:.3f}")
                    cv = cap
                change.update(suggested_corr_vol=cv, corr_vol_filled=how)
        if r.suggested_corr_spot is None:
            cs, how = fill(cs_rungs, r.t)
            if cs is not None:
                change.update(suggested_corr_spot=cs, corr_spot_filled=how)
        if change:
            change["warnings"] = tuple(warnings)
            r = replace(r, **change)
        out.append(r)
    return out


def _suggest_corr_spot(book, pair: str, tenor: str, t: float, m: MeasuredDependence,
                       source: str, lender, method, cut, *, vol_vol, corr_vol: float,
                       target_rr: float | None) -> dict:
    """The correlation-spot half of a :class:`DependenceRow`: implied, premium, suggested.

    The vol-vol correlation's rule, for the risk reversal: the lean the marked
    25-delta risk reversal asks for at the suggested vol-vol correlation and
    correlation vol, its gap over the measured one, and measured plus the
    premium the table's source names -- the cross's own, none, or a lender's.
    """
    implied, note = None, ""
    if target_rr is None:
        note = "no 25-delta risk reversal is marked, so no lean is implied"
    elif not corr_vol > 0.0:
        note = ("no correlation vol is held here, and the correlation-spot correlation "
                "leans the correlation vol, so no value of it moves the risk reversal")
    else:
        try:
            surface, curve, (leg_a, leg_b), co = _cross_legs(book, pair)
            expiry = book.clock.datetime_from_years(t)
            rho = float(np.asarray(curve.correlation(t)))
            conv = surface.slice_conv(t)
            da = moments.distribution_from_surface(book[leg_a], expiry, method=method, cut=cut)
            db = moments.distribution_from_surface(book[leg_b], expiry, method=method, cut=cut)
            implied, note = moments.implied_corr_spot(
                da, db, co, rho, conv, target_rr, vol_vol=vol_vol, corr_vol=corr_vol)
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            note = f"the correlation-spot correlation could not be implied: {exc}"
    own = None if implied is None or m.corr_spot is None else implied - m.corr_spot
    if source == PREMIUM_OWN:
        used, why = own, (note if implied is None else "")
    elif source == PREMIUM_NONE:
        used, why = 0.0, ""
    else:
        used = None if lender is None else lender.corr_spot_premium
        why = (f"{source} has no {tenor} row" if lender is None else
               f"{source} has no correlation-spot premium at {tenor}: "
               f"{lender.implied_corr_spot_note or 'its correlation-spot correlation was not measured'}")
    suggested, reason = None, ""
    if m.corr_spot is None:
        reason = next((n for n in m.notes if n.startswith("correlation-spot")),
                      "the correlation-spot correlation was not measured")
    elif used is None:
        reason = why
    else:
        suggested = max(-1.0, min(1.0, m.corr_spot + used))
    return dict(implied_corr_spot=implied, implied_corr_spot_note=note, corr_spot_premium=own,
                corr_spot_premium_used=used, suggested_corr_spot=suggested,
                corr_spot_reason=reason)


def dependence_table(book, pair: str, history, *, premium: str = PREMIUM_OWN,
                     method: str | None = None, cut: str = "NY", tenors=None,
                     basis: str = "auto", corr_vol_lookback_days: float | None = None,
                     fill_gaps: bool = False) -> DependenceTable:
    """A cross's dependence per tenor: what history shows, what its fly implies, what to mark.

    ``measured`` is :func:`measure_dependence` -- physical.  ``implied_vol_vol``
    is the vol-vol correlation the cross's marked 25-delta butterfly asks for
    with the correlation vol held at what was measured, and ``premium`` is the
    gap: what the market charges for dependence beyond what happened.

    The **suggestion** is measured plus a premium: ``premium="own"`` takes the
    cross's own (so on a liquid cross the suggestion reproduces its market
    fly, at a correlation vol history supports); ``"none"`` takes none, which
    is history alone; a **pair** takes that cross's premium at the same tenor --
    the educated guess for a cross with no market of its own, borrowed from
    the nearest cross that has one.  The correlation vol suggested is the
    measured one, kept inside what the model can hold at the cross's
    correlation.  A suggestion goes into the Config window's boxes and nowhere
    else.

    ``corr_vol_lookback_days`` is the history the correlation vol is measured
    across (``history.CORR_VOL_DAYS`` by default); a longer one reaches longer
    tenors.  ``fill_gaps`` fills a tenor that has no suggestion of its own from
    the tenors that do (:func:`_fill_dependence_gaps`) and names the fill on
    the row -- what the book would read there anyway, made visible.  A vol-vol
    correlation filled under the cross's own premium does **not** give back
    that tenor's fly: where it was blank, no vol-vol correlation could.
    """
    lookback = (CORR_VOL_DAYS if corr_vol_lookback_days is None
                else float(corr_vol_lookback_days))
    if not math.isfinite(lookback) or lookback <= 0:
        raise ValueError(f"a correlation vol lookback is a positive number of days, got "
                         f"{corr_vol_lookback_days!r}")
    surface, curve, (leg_a, leg_b), _ = _cross_legs(book, pair)
    names = list(tenors or book.data.tenors_for(pair))
    premium = str(premium or PREMIUM_OWN).strip()
    analog: dict[str, DependenceRow] = {}
    source = premium.lower() if premium.lower() in (PREMIUM_OWN, PREMIUM_NONE) else premium.upper()
    if source not in (PREMIUM_OWN, PREMIUM_NONE):
        if source == pair:
            source = PREMIUM_OWN
        else:
            spec = book.data.pairs.get(source)
            if spec is None or not spec.is_cross:
                raise ValueError(f"{source} is not a cross in this workbook, so it has no "
                                 f"premium to lend {pair}")
            if source not in book:
                raise ValueError(f"{source} is not built in this book, so it has no premium "
                                 f"to lend {pair}")
            # The lender's own suggestions, unfilled: a premium is lent only
            # where the lender's fly identified one.
            analog = {r.tenor.upper(): r for r in dependence_table(
                book, source, history, premium=PREMIUM_OWN, method=method, cut=cut,
                tenors=names, basis=basis, corr_vol_lookback_days=lookback).rows}
    unavailable = ""
    if history is None:
        unavailable = "no historical workbook is loaded, so the legs' dependence cannot be measured"
    measured = {tenor: measure_dependence(history, leg_a, leg_b, tenor,
                                          surface.tenor_years(tenor), basis=basis,
                                          corr_vol_lookback_days=lookback, pair=pair)
                for tenor in names}

    # The implied vol-vol correlation holds the correlation vol the suggestion
    # marks at the tenor, so marking the two together gives back the fly.
    # Where none was measured that is not zero: the book reads a blank
    # correlation vol off the tenors that have one (and a filled row writes
    # the same number), so the vol-vol correlation is solved at that.  Solved
    # at zero instead, a 1W beside a measured 2W overshot its own fly.
    ts = {tenor: surface.tenor_years(tenor) for tenor in names}
    rhos = {tenor: float(np.asarray(curve.correlation(ts[tenor]))) for tenor in names}

    def cap(tenor):
        return max(1.0 - abs(rhos[tenor]) - 1e-6, 0.0)

    cv_rungs = sorted((ts[k], min(measured[k].corr_vol, cap(k)), k) for k in names
                      if measured[k].corr_vol is not None)
    held_cv: dict[str, tuple[float, str]] = {}
    leans: dict[str, float] = {}
    for tenor in names:
        if measured[tenor].corr_vol is not None:
            cv, how = min(measured[tenor].corr_vol, cap(tenor)), ""
        else:
            cv, how = _ladder_value(cv_rungs, ts[tenor])
            cv = 0.0 if cv is None else min(cv, cap(tenor))
        held_cv[tenor] = (cv, how)
        # The first lean the vol-vol correlation is solved at: the measured
        # one where there is one, else what the book marks.
        marked = book.dependence_at(pair, ts[tenor])
        leans[tenor] = (measured[tenor].corr_spot if measured[tenor].corr_spot is not None
                        else 0.0 if marked is None else marked.corr_spot)

    def implied_rows(tenors_, lean):
        held = {k: (moments.Dependence(None, held_cv[k][0], lean[k]) if held_cv[k][0] > 0
                    else None) for k in tenors_}
        return {r.tenor: r for r in triangle_table(
            book, pair, method=method, cut=cut, tenors=list(tenors_), with_noise=False,
            implied_vol_vol=True, implied_corr_spot=False, dependence=held,
            dependence_source={k: (f"correlation vol {held_cv[k][1]}" if held_cv[k][1]
                                   else "measured correlation vol") for k in tenors_})}

    def vol_vol_half(tenor, r):
        m = measured[tenor]
        warnings: list[str] = []
        implied = None if r is None else r.implied_vol_vol
        implied_note = "" if r is None else (r.implied_vol_vol_note or "; ".join(
            w for w in r.warnings if "could not be built" in w))
        own = None if implied is None or m.vol_vol is None else implied - m.vol_vol
        if source == PREMIUM_OWN:
            used = own
            why_none = (implied_note if implied is None else
                        "the vol-vol correlation was not measured")
        elif source == PREMIUM_NONE:
            used, why_none = 0.0, "the vol-vol correlation was not measured"
        else:
            a = analog.get(tenor.upper())
            used = None if a is None else a.premium
            why_none = (f"{source} has no {tenor} row" if a is None else
                        f"{source} has no premium at {tenor}: "
                        f"{a.implied_note or 'its vol-vol correlation was not measured'}")
        vv = None
        reason = ""
        if m.vol_vol is None:
            reason = next((n for n in m.notes if n.startswith("vol-vol")), "") or m.notes[0] \
                if m.notes else "the vol-vol correlation was not measured"
        elif used is None:
            reason = why_none
        else:
            vv = m.vol_vol + used
            if not -1.0 <= vv <= 1.0:
                warnings.append(f"measured {m.vol_vol:+.3f} plus a premium of {used:+.3f} is "
                                f"{vv:+.3f}, held at {max(-1.0, min(1.0, vv)):+.0f}: the rest of "
                                f"the fly is correlation vol or something the legs do not carry")
                vv = max(-1.0, min(1.0, vv))
        return dict(implied_vol_vol=implied, implied_note=implied_note, premium=own,
                    premium_used=used, suggested_vol_vol=vv, reason=reason), warnings

    tri = implied_rows(names, leans)
    halves = {tenor: vol_vol_half(tenor, tri.get(tenor)) for tenor in names}
    lean_half = {}
    for tenor in names:
        r, (vv_fields, _) = tri.get(tenor), halves[tenor]
        vv = vv_fields["suggested_vol_vol"]
        lean_half[tenor] = _suggest_corr_spot(
            book, pair, tenor, ts[tenor], measured[tenor], source, analog.get(tenor.upper()),
            method, cut, corr_vol=held_cv[tenor][0],
            vol_vol=vv if vv is not None else vv_fields["implied_vol_vol"],
            target_rr=None if r is None else r.marked.get("rr25"))
    # The vol-vol correlation again, where the lean the row suggests is not
    # the one it was solved at: marked together, the two give back the fly.
    moved = [k for k in names if held_cv[k][0] > 0
             and lean_half[k]["suggested_corr_spot"] is not None
             and abs(lean_half[k]["suggested_corr_spot"] - leans[k]) > 1e-3]
    if moved:
        leans.update({k: lean_half[k]["suggested_corr_spot"] for k in moved})
        again = implied_rows(moved, leans)
        for k in moved:
            tri[k] = again.get(k, tri.get(k))
            halves[k] = vol_vol_half(k, tri[k])

    rows: list[DependenceRow] = []
    for tenor in names:
        t, rho = ts[tenor], rhos[tenor]
        m = measured[tenor]
        r = tri.get(tenor)
        now = book.dependence_at(pair, t)
        vv_fields, warnings = halves[tenor]
        cv = m.corr_vol
        if cv is not None:
            cap_ = max(1.0 - abs(rho) - 1e-6, 0.0)
            if cv > cap_:
                warnings.append(f"a measured correlation vol of {cv:.3f} around a correlation of "
                                f"{rho:+.3f} reaches past 1, so it is held at {cap_:.3f}")
                cv = cap_
        rows.append(DependenceRow(
            tenor=tenor, t=t, rho=rho, measured=m,
            marked_vol_vol=None if now is None else now.vol_vol,
            marked_corr_vol=0.0 if now is None else now.corr_vol,
            marked_corr_spot=0.0 if now is None else now.corr_spot,
            marked_fly25=None if r is None else r.marked.get("fly25"),
            marked_rr25=None if r is None else r.marked.get("rr25"),
            implied_corr_vol=held_cv[tenor][0], implied_corr_vol_from=held_cv[tenor][1],
            premium_source=source, suggested_corr_vol=cv,
            warnings=tuple(warnings), **vv_fields, **lean_half[tenor]))
    if fill_gaps:
        rows = _fill_dependence_gaps(rows)
    return DependenceTable(pair=pair, legs=(leg_a, leg_b), premium=source, rows=rows,
                           unavailable=unavailable, corr_vol_lookback_days=lookback,
                           filled=bool(fill_gaps))


def _vega_split(va: float, vb: float, rho: float, ca: int, cb: int,
                cross_vol: float) -> tuple[float, float, float]:
    """Differentiate the variance triangle: where a cross's vega really sits.

    The triangle the book is built on is

        sigma_c^2 = sigma_a^2 + sigma_b^2 + 2 * ca * cb * rho * sigma_a * sigma_b

    so a move in either leg moves the cross by

        d sigma_c / d sigma_a = (sigma_a + x * sigma_b) / sigma_c,   x = ca*cb*rho

    and symmetrically for the other leg.  Vega on the cross is therefore vega
    on the legs in those proportions: a position long 1 unit of at-the-money
    vega in the cross behaves like ``d sigma_c / d sigma_a`` units of it in
    leg A and ``d sigma_c / d sigma_b`` in leg B, which is what the two legs
    have to be traded in to hedge it.

    The two shares do **not** add up to one, and reading them as if they were
    a split of something into parts is the mistake to avoid.  What is exact is
    Euler's identity: the triangle is homogeneous of degree one in the two leg
    volatilities, so

        sigma_a * (d sigma_c / d sigma_a) + sigma_b * (d sigma_c / d sigma_b)
            == sigma_c

    -- weighted by each leg's own volatility, the two account for the whole of
    the cross's.  A test pins that against the ratios themselves.

    The correlation is homogeneous of degree zero and so appears nowhere in
    that identity, which is exactly why it is reported separately:
    ``d sigma_c / d rho = ca * cb * sigma_a * sigma_b / sigma_c``, per unit of
    correlation.  It is risk that no amount of leg vega hedges.

    Everything here is in decimals, like the rest of the module; the screen
    and the command line convert once at their edge.
    """
    if not (cross_vol > 0.0):
        # A zero cross volatility has no vega to split, and dividing by it
        # would hand the screen an infinity dressed up as a hedge ratio.
        nan = float("nan")
        return nan, nan, nan
    x = ca * cb * rho
    return ((va + x * vb) / cross_vol,
            (vb + x * va) / cross_vol,
            ca * cb * va * vb / cross_vol)


def _leg_reference(surface, expiry, method, cut, deltas) -> dict[str, float]:
    by = smile_points(surface.smile_table(expiry, deltas=tuple(deltas), method=method,
                                          cut=cut))
    out = {"atm": by["ATM"]}
    for d in deltas:
        tag = f"{int(round(d * 100))}"
        c, p = by.get(f"{tag}d call"), by.get(f"{tag}d put")
        if c is None or p is None:
            continue
        out[f"rr{tag}"] = c - p
        out[f"fly{tag}"] = 0.5 * (c + p) - by["ATM"]
    return out
