"""The volatility surface: an ATM curve plus a smile, and everything priced off it.

Replaces the legacy ``Vol``.  The structural change is caching.  The legacy
``get_vol`` rebuilt two SABR calibrations, solved four delta strikes by
fixed-point, and ran a twelve-parameter SVI optimisation *per strike query*.
Here a ``SmileSlice`` is built once per expiry and memoised, so a surface plot
or a delta ladder costs one fit rather than thousands.

The smile parameters themselves are fitted per quoted tenor and then given a
term structure of their own (initial / final / decay), exactly as before, but
with bounded least squares instead of an unconstrained ``minimize`` on a
non-differentiable ``sqrt`` of squared error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from . import black, sabr
from .atm import AtmCurve
from .black import DeltaConvention
from .numerics import ConvergenceError, fixed_point, solve_scalar
from .sabr import SabrCalibration, SabrParams
from .smile import INTERPOLATORS, SmileSlice
from .timeutil import Clock, tenor_to_years

# The four smile parameters carried across expiries.
PARAM_NAMES = ("slog10", "slog25", "rho25", "rho10")


@dataclass
class SmileMark:
    """Broker quotes for one expiry, in decimals."""

    tenor: str
    st_10: float   # 10-delta market strangle
    st_25: float   # 25-delta market strangle
    rr_25: float   # 25-delta risk reversal, call vol minus put vol
    rr_10: float   # 10-delta risk reversal


@dataclass
class TenorFit:
    """The calibration outcome for one quoted tenor."""

    tenor: str
    t: float
    atm_vol: float
    slog10: float
    slog25: float
    rho25: float
    rho10: float
    cal_25: SabrCalibration
    cal_10: SabrCalibration
    ok: bool
    message: str = ""


@dataclass
class ParamTermStructure:
    """``final - (final - initial) * exp(-decay * t)`` for one smile parameter."""

    initial: float
    final: float
    decay: float

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return self.final - (self.final - self.initial) * np.exp(-self.decay * t)


def fit_param_term_structure(ts, values, *, name: str = "parameter") -> ParamTermStructure:
    """Bounded least squares for the initial/final/decay shape.

    The legacy ``min_diff`` minimised ``sqrt(sum of squares)``, which is not
    differentiable at its minimum, using an unconstrained quasi-Newton method
    with no bounds -- so a negative decay (an exploding term structure) was a
    perfectly acceptable answer.  Decay is now constrained non-negative and the
    residuals are fed to a least-squares solver in their natural form.
    """
    ts = np.asarray(ts, dtype=float)
    values = np.asarray(values, dtype=float)
    if ts.size == 0:
        raise ValueError(f"no points to fit the {name} term structure")
    if ts.size == 1:
        return ParamTermStructure(float(values[0]), float(values[0]), 1.0)

    def residuals(x):
        return ParamTermStructure(x[0], x[1], x[2])(ts) - values

    lo, hi = float(values[0]), float(values[-1])
    span = max(abs(hi - lo), 1e-6)
    best = None
    for decay0 in (0.5, 2.0, 8.0, 30.0):
        sol = least_squares(
            residuals, np.array([lo, hi, decay0]),
            bounds=(np.array([lo - 10 * span, hi - 10 * span, 0.0]),
                    np.array([lo + 10 * span, hi + 10 * span, 500.0])),
            xtol=1e-14, ftol=1e-14, gtol=1e-14, max_nfev=800,
        )
        if best is None or sol.cost < best.cost:
            best = sol
    return ParamTermStructure(float(best.x[0]), float(best.x[1]), float(best.x[2]))


@dataclass
class VolSurface:
    """ATM curve plus smile, for one currency pair."""

    pair: str
    atm: AtmCurve
    conv: DeltaConvention = field(default_factory=DeltaConvention)
    method: str = "SVI"
    marks: list[SmileMark] = field(default_factory=list)
    fits: list[TenorFit] = field(default_factory=list)
    term: dict[str, ParamTermStructure] = field(default_factory=dict)
    anchor_tenors: bool = False
    # Set for managed / pegged pairs.  The lognormal smile below is not a valid
    # model outside a hard band, so queries there are flagged.
    band: object | None = None
    param_overwrites: dict[str, dict[str, float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.conv, DeltaConvention):
            self.conv = DeltaConvention(bool(self.conv))
        self._slices: dict[tuple, SmileSlice] = {}

    @property
    def clock(self) -> Clock:
        return self.atm.clock

    # -- calibration ------------------------------------------------------
    def fit_smiles(self, marks: list[SmileMark] | None = None,
                   only: list[str] | None = None, *,
                   max_solutions: int = 1, prior_weight: float = 0.0) -> list[TenorFit]:
        """Calibrate SABR at each quoted tenor for both the 25d and 10d wings.

        Tenors are fitted short to long.  With ``prior_weight`` above zero each
        tenor is pulled toward the one before it, which stabilises the
        parameter term structure when the quotes admit more than one fit.
        ``max_solutions`` above 1 makes each fit report competing solutions.
        """
        if marks is not None:
            self.marks = list(marks)
        wanted = {t.upper() for t in only} if only else None
        keep = [f for f in self.fits if wanted and f.tenor.upper() not in wanted]
        results: list[TenorFit] = []
        prev25 = prev10 = None
        for mark in sorted(self.marks, key=lambda m: tenor_to_years(m.tenor)):
            if wanted and mark.tenor.upper() not in wanted:
                continue
            t = tenor_to_years(mark.tenor)
            atm_vol = self.atm.cut_vol(self.clock.datetime_from_years(t), "NY")
            if atm_vol <= 0:
                atm_vol = self.atm.term_vol(t)
            msgs = []
            try:
                c25 = sabr.calibrate(atm_vol, mark.rr_25, mark.st_25, 0.25, t, self.conv,
                                     prior=prev25, prior_weight=prior_weight,
                                     max_solutions=max_solutions)
                c10 = sabr.calibrate(atm_vol, mark.rr_10, mark.st_10, 0.10, t, self.conv,
                                     prior=prev10, prior_weight=prior_weight,
                                     max_solutions=max_solutions)
                prev25, prev10 = c25.params, c10.params
                for cal, wing in ((c25, "25d"), (c10, "10d")):
                    msgs.extend(f"{wing}: {w}" for w in cal.warnings if "no (rho" not in w)
                ok = c25.converged and c10.converged
                if not c25.converged:
                    msgs.append(f"25d: {c25.message}")
                if not c10.converged:
                    msgs.append(f"10d: {c10.message}")
                results.append(TenorFit(
                    tenor=mark.tenor, t=t, atm_vol=atm_vol,
                    slog10=c10.params.log_volvol, slog25=c25.params.log_volvol,
                    rho25=c25.params.rho, rho10=c10.params.rho,
                    cal_25=c25, cal_10=c10, ok=ok, message="; ".join(msgs),
                ))
            except (ConvergenceError, ValueError) as exc:
                self.warnings.append(f"{self.pair} {mark.tenor}: smile calibration failed ({exc})")
        self.fits = sorted(keep + results, key=lambda f: f.t)
        self._slices.clear()
        return self.fits

    def interpolate_params(self) -> dict[str, ParamTermStructure]:
        """Give each smile parameter a term structure across expiries."""
        if not self.fits:
            raise ValueError(f"{self.pair}: fit_smiles must run before interpolate_params")
        ts = [f.t for f in self.fits]
        self.term = {
            name: fit_param_term_structure(ts, [getattr(f, name) for f in self.fits], name=name)
            for name in PARAM_NAMES
        }
        self._slices.clear()
        return self.term

    def calibrate(self, marks: list[SmileMark] | None = None) -> "VolSurface":
        """Fit every tenor then build the parameter term structures."""
        self.fit_smiles(marks)
        if self.fits:
            self.interpolate_params()
        return self

    # -- parameters at an arbitrary expiry --------------------------------
    def params_at(self, t: float) -> dict[str, float]:
        """The four smile parameters at ``t``.

        With ``anchor_tenors`` on, the curve is pinned to the fitted values at
        the quoted tenors and the shape between them comes from the term
        structure -- the legacy ``use_overwrite`` behaviour, but without the
        division by ``v2_c - v1_c`` that blew up whenever the term structure
        happened to be flat between two tenors.
        """
        if not self.term:
            raise ValueError(f"{self.pair}: no smile term structure; run calibrate() first")
        out = {name: float(self.term[name](t)) for name in PARAM_NAMES}
        for name, ow in self.param_overwrites.items():
            if name in out and "curve" in ow:
                out[name] = float(ow["curve"])
        if not self.anchor_tenors or not self.fits:
            return out

        ts = [f.t for f in self.fits]
        if t <= ts[0] or t >= ts[-1]:
            idx = 0 if t <= ts[0] else len(ts) - 1
            for name in PARAM_NAMES:
                out[name] = self._anchor_value(name, idx)
            return out
        j = int(np.searchsorted(np.array(ts), t, side="left"))
        i = j - 1
        t1, t2 = ts[i], ts[j]
        for name in PARAM_NAMES:
            v1, v2 = self._anchor_value(name, i), self._anchor_value(name, j)
            c1, c2 = float(self.term[name](t1)), float(self.term[name](t2))
            ct = float(self.term[name](t))
            denom = c2 - c1
            # Fall back to linear in time when the model curve is flat across
            # the interval, instead of dividing by (almost) zero.
            ratio = (ct - c1) / denom if abs(denom) > 1e-12 else (t - t1) / (t2 - t1)
            out[name] = v1 + ratio * (v2 - v1)
        return out

    def _anchor_value(self, name: str, index: int) -> float:
        fit = self.fits[index]
        ow = self.param_overwrites.get(name, {})
        return float(ow.get(fit.tenor.upper(), getattr(fit, name)))

    def overwrite_param(self, name: str, tenor: str, value: float) -> None:
        """Pin one smile parameter, at a tenor or on the whole curve."""
        if name not in PARAM_NAMES:
            raise ValueError(f"unknown smile parameter {name!r}; expected one of {PARAM_NAMES}")
        self.param_overwrites.setdefault(name, {})[tenor.upper()] = float(value)
        self._slices.clear()

    def clear_param_overwrites(self) -> None:
        self.param_overwrites.clear()
        self._slices.clear()

    # -- slices -----------------------------------------------------------
    def slice_at(self, expiry, method: str | None = None, cut: str = "TK",
                 forward: float = 1.0) -> SmileSlice:
        """Build (or fetch) the cached smile for an expiry."""
        method = method or self.method
        if method not in INTERPOLATORS:
            raise ValueError(f"unknown interpolation method {method!r}; expected one of {INTERPOLATORS}")
        dt = self.clock.coerce_datetime(expiry)
        t = self.clock.years_to(dt)
        if t <= 0:
            raise ValueError(f"expiry {dt:%Y-%m-%d %H:%M} is not in the future")
        key = (round(t, 10), method, cut.upper(), round(forward, 10))
        hit = self._slices.get(key)
        if hit is not None:
            return hit

        atm_vol = self.atm.cut_vol(dt, cut)
        if atm_vol <= 0:
            raise ValueError(f"{self.pair}: ATM volatility is zero at {dt:%Y-%m-%d}")
        p = self.params_at(t)
        sqt = math.sqrt(t)
        s25 = SabrParams(
            alpha=sabr.alpha_from_atm(atm_vol, black.dns_strike(forward, atm_vol, t, self.conv),
                                      p["rho25"], p["slog25"] / sqt, t, forward),
            rho=p["rho25"], volvol=p["slog25"] / sqt, t=t, f=forward)
        s10 = SabrParams(
            alpha=sabr.alpha_from_atm(atm_vol, black.dns_strike(forward, atm_vol, t, self.conv),
                                      p["rho10"], p["slog10"] / sqt, t, forward),
            rho=p["rho10"], volvol=p["slog10"] / sqt, t=t, f=forward)
        sl = SmileSlice.build(t, atm_vol, s25, s10, self.conv, forward=forward, method=method)
        self._slices[key] = sl
        return sl

    # -- the pricing surface ----------------------------------------------
    def vol(self, strike_ratio, expiry, method: str | None = None, cut: str = "TK"):
        """Implied volatility for a strike/forward ratio.  Vectorised over strikes."""
        return self.slice_at(expiry, method, cut).vol(strike_ratio)

    def band_check(self, strike_abs, forward_abs: float) -> list[str]:
        """Warn about strikes a lognormal smile has no business pricing.

        For a pegged pair the terminal distribution has compact support, so an
        option struck outside the band is worth only whatever the peg breaking
        is worth -- not what a lognormal wing says it is.
        """
        if self.band is None:
            return []
        ks = np.atleast_1d(np.asarray(strike_abs, dtype=float))
        bad = ks[(ks < self.band.lower) | (ks > self.band.upper)]
        if bad.size == 0:
            return []
        return [
            f"{self.pair} strike {float(bad[0]):.5f} lies outside the managed band "
            f"[{self.band.lower}, {self.band.upper}]; the lognormal smile prices it as if "
            f"the peg did not exist. Use the band model for a defensible value."
        ]

    def atm_vol(self, expiry, cut: str = "TK") -> float:
        return self.atm.cut_vol(expiry, cut)

    def daily_vol(self, when) -> float:
        return self.atm.daily_vol(when)

    def delta_strike(self, expiry, delta: float, is_call: bool,
                     method: str | None = None, cut: str = "TK") -> tuple[float, float]:
        """Strike and volatility for a delta, solved on the interpolated smile."""
        signed = abs(delta) if is_call else -abs(delta)
        return self.slice_at(expiry, method, cut).strike_from_delta(signed, is_call)

    def risk_reversal(self, expiry, delta: float, method: str | None = None,
                      cut: str = "TK") -> float:
        """Smile risk reversal: call vol minus put vol at ``delta``."""
        _, cv = self.delta_strike(expiry, delta, True, method, cut)
        _, pv = self.delta_strike(expiry, delta, False, method, cut)
        return cv - pv

    def strangle(self, expiry, delta: float, method: str | None = None,
                 cut: str = "TK") -> float:
        """Market strangle implied by the surface, in vol over ATM.

        Solved by bracketing on the strangle premium, replacing the legacy
        ``fsolve(func, 0)`` which reported no diagnostics.
        """
        sl = self.slice_at(expiry, method, cut)
        t, f, atm = sl.t, sl.forward, sl.atm_vol

        def premium_gap(s: float) -> float:
            v = atm + s
            if v <= 0:
                return 1e6
            kc = black.strike_from_delta(abs(delta), f, v, t, True, self.conv)
            kp = black.strike_from_delta(-abs(delta), f, v, t, False, self.conv)
            market = float(black.price(f, kc, v, t, True) + black.price(f, kp, v, t, False))
            model = float(black.price(f, kc, float(sl.vol(kc)), t, True)
                          + black.price(f, kp, float(sl.vol(kp)), t, False))
            return model - market

        return solve_scalar(premium_gap, 0.0, lo_bound=-atm * 0.9,
                            bracket=(-atm * 0.5, atm * 2.0), what="market strangle")

    # -- greeks -----------------------------------------------------------
    def smile_delta(self, spot: float, strike: float, expiry, is_call: bool = True,
                    method: str | None = None, cut: str = "TK", bump: float = 1e-3) -> float:
        """Delta including the smile's reaction to spot, by central difference."""
        sl = self.slice_at(expiry, method, cut)
        up, dn = spot * (1.0 + bump), spot * (1.0 - bump)
        pv_up = float(black.price(up, strike, float(sl.vol(strike / up)), sl.t, is_call,
                                  foreign_premium=bool(self.conv)))
        pv_dn = float(black.price(dn, strike, float(sl.vol(strike / dn)), sl.t, is_call,
                                  foreign_premium=bool(self.conv)))
        d_spot = (up - dn) if not bool(self.conv) else (up - dn) / spot
        return (pv_up - pv_dn) / d_spot

    def density(self, strike_ratio: float, expiry, method: str | None = None,
                cut: str = "TK", bump: float = 1e-3) -> float:
        """Risk-neutral density, as the second strike derivative of a call.

        The legacy ``getDensity`` raised ``NameError`` on an undefined ``S``
        whenever ``delta_adjust`` was false, and divided by ``step**2`` rather
        than ``(K * step)**2``, so the scale was wrong by a factor of ``K^2``
        even on the branch that ran.
        """
        sl = self.slice_at(expiry, method, cut)
        K = float(strike_ratio)
        h = K * bump
        ks = np.array([K - h, K, K + h])
        vols = np.asarray(sl.vol(ks), dtype=float)
        pv = np.array([float(black.price(sl.forward, k, v, sl.t, True)) for k, v in zip(ks, vols)])
        return float((pv[0] - 2.0 * pv[1] + pv[2]) / (h * h))

    def digital(self, spot: float, strike: float, expiry, ramp: float = 0.5,
                is_call: bool = True, method: str | None = None, cut: str = "TK") -> float:
        """Call/put spread replication of a digital, priced on the smile."""
        sl = self.slice_at(expiry, method, cut)
        k2 = strike * (1.0 + ramp / 100.0) if is_call else strike * (1.0 - ramp / 100.0)
        width = abs(k2 - strike)
        if width <= 0:
            raise ValueError(f"digital ramp must be non-zero, got {ramp!r}")
        v1 = float(sl.vol(strike / spot))
        v2 = float(sl.vol(k2 / spot))
        p1 = float(black.price(spot, strike, v1, sl.t, is_call, foreign_premium=True))
        p2 = float(black.price(spot, k2, v2, sl.t, is_call, foreign_premium=True))
        return (p1 * (k2 / width)) - (p2 * (strike / width))

    def smile_table(self, expiry, deltas=(0.10, 0.25), method: str | None = None,
                    cut: str = "TK") -> list[dict]:
        """The quoted smile points for display."""
        sl = self.slice_at(expiry, method, cut)
        rows = []
        for d in sorted(deltas, reverse=True):
            kp, vp = sl.strike_from_delta(-d, False)
            rows.append({"label": f"{int(d * 100)}d put", "delta": -d, "strike": kp, "vol": vp})
        rows.append({"label": "ATM", "delta": 0.5, "strike": sl.strikes[2], "vol": sl.atm_vol})
        for d in sorted(deltas):
            kc, vc = sl.strike_from_delta(d, True)
            rows.append({"label": f"{int(d * 100)}d call", "delta": d, "strike": kc, "vol": vc})
        return rows

    def invalidate(self) -> None:
        self._slices.clear()
        self.atm.invalidate()
