"""Smile interpolation: arbitrage-constrained SVI, vanna-volga, and SABR.

The legacy ``solve_svi3`` summed *three* SVI slices and fitted **twelve free
parameters to five data points** with an unconstrained ``scipy.optimize.
minimize``, checking neither convergence nor no-arbitrage.  Worse,
``Vol.get_vol`` re-ran that twelve-parameter optimisation on every single
strike query, so a surface plot re-solved it thousands of times.

Two things change:

* A single raw SVI slice -- five parameters for five points -- is fitted with
  Zeliade's quasi-explicit method: for a fixed ``(m, sigma)`` the problem is
  *linear* in the remaining three parameters, so the inner solve is a small
  convex least-squares over the no-arbitrage region and only a two-dimensional
  search is left outside.  Gatheral's constraints are imposed rather than
  hoped for, and Durrleman's butterfly condition is reported.
* The fit happens once per expiry inside a cached ``SmileSlice``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from . import black, sabr
from .black import DeltaConvention
from .numerics import ConvergenceError
from .sabr import SabrParams


# --------------------------------------------------------------------------
# Raw SVI
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SVIParams:
    """Raw SVI: ``w(k) = a + b (rho (k - m) + sqrt((k - m)^2 + sigma^2))``.

    ``w`` is total implied variance and ``k = log(K / F)``.
    """

    a: float
    b: float
    rho: float
    m: float
    sigma: float
    t: float

    def total_variance(self, k):
        k = np.asarray(k, dtype=float)
        km = k - self.m
        return self.a + self.b * (self.rho * km + np.sqrt(km * km + self.sigma * self.sigma))

    def vol(self, k):
        w = np.maximum(self.total_variance(k), 1e-12)
        return np.sqrt(w / self.t)

    def min_variance(self) -> float:
        """The global minimum of ``w``, attained analytically."""
        return self.a + self.b * self.sigma * math.sqrt(max(1.0 - self.rho * self.rho, 0.0))

    def violates(self) -> list[str]:
        """Gatheral's static no-arbitrage conditions that this slice breaks."""
        bad = []
        if self.b < -1e-12:
            bad.append(f"b={self.b:.4g} < 0")
        if abs(self.rho) >= 1.0:
            bad.append(f"|rho|={abs(self.rho):.4g} >= 1")
        if self.sigma <= 0:
            bad.append(f"sigma={self.sigma:.4g} <= 0")
        if self.min_variance() < -1e-10:
            bad.append(f"minimum total variance {self.min_variance():.4g} < 0")
        if self.b * (1.0 + abs(self.rho)) > 4.0 / self.t + 1e-9:
            bad.append(f"wing slope b(1+|rho|)={self.b * (1 + abs(self.rho)):.4g} > 4/t={4 / self.t:.4g}")
        return bad

    def durrleman(self, k) -> np.ndarray:
        """Durrleman's function ``g(k)``; negative values are butterfly arbitrage."""
        k = np.asarray(k, dtype=float)
        km = k - self.m
        root = np.sqrt(km * km + self.sigma * self.sigma)
        w = self.a + self.b * (self.rho * km + root)
        wp = self.b * (self.rho + km / root)
        wpp = self.b * self.sigma * self.sigma / (root**3)
        return (1.0 - 0.5 * k * wp / w) ** 2 - 0.25 * wp**2 * (1.0 / w + 0.25) + 0.5 * wpp


@dataclass(frozen=True)
class SVIFit:
    params: SVIParams
    rmse: float
    max_abs_vol_error: float
    arbitrage_free: bool
    warnings: tuple[str, ...] = ()


def _inner_linear_fit(y: np.ndarray, w: np.ndarray, sigma: float, w_max: float):
    """Constrained linear least squares in ``(a, d, c)`` for fixed ``(m, sigma)``.

    With ``y = (k - m) / sigma``, ``d = rho b sigma`` and ``c = b sigma``, SVI
    is linear: ``w = a + d y + c sqrt(y^2 + 1)``.  Zeliade's admissible region
    (``0 <= c <= 4 sigma``, ``|d| <= c``, ``|d| <= 4 sigma - c``, ``a >= 0``)
    is convex, so this small problem is solved reliably.

    The unconstrained normal-equation solution is tried first.  For a
    well-behaved smile it already lands inside the admissible region, and
    taking it directly avoids running a constrained optimiser -- which
    otherwise dominates the whole calibration, since this routine sits inside
    the outer search's objective.
    """
    basis = np.column_stack([np.ones_like(y), y, np.sqrt(y * y + 1.0)])
    four_sigma = 4.0 * sigma

    def sse(p):
        r = basis @ p - w
        return float(r @ r)

    def feasible(p, tol=1e-12):
        a, d, c = p
        return (-tol <= a <= w_max + tol and -tol <= c <= four_sigma + tol
                and abs(d) <= c + tol and abs(d) <= four_sigma - c + tol)

    try:
        p_free = np.linalg.lstsq(basis, w, rcond=None)[0]
        if feasible(p_free):
            return p_free, sse(p_free)
    except np.linalg.LinAlgError:  # pragma: no cover
        pass

    # Constraints bind: fall back to SLSQP, with analytic jacobians so the
    # optimiser does not finite-difference them.
    def jac(p):
        return 2.0 * basis.T @ (basis @ p - w)

    constraints = (
        {"type": "ineq",
         "fun": lambda p: p[2] - abs(p[1]),
         "jac": lambda p: np.array([0.0, -np.sign(p[1]), 1.0])},
        {"type": "ineq",
         "fun": lambda p: four_sigma - p[2] - abs(p[1]),
         "jac": lambda p: np.array([0.0, -np.sign(p[1]), -1.0])},
    )
    bounds = [(0.0, max(w_max, 1e-12)), (-four_sigma, four_sigma), (0.0, four_sigma)]
    start = np.clip(p_free if "p_free" in dir() else np.array([float(np.min(w)), 0.0, sigma]),
                    [b[0] for b in bounds], [b[1] for b in bounds])
    res = minimize(sse, start, jac=jac, bounds=bounds, constraints=constraints,
                   method="SLSQP", options={"maxiter": 100, "ftol": 1e-16})
    return res.x, float(res.fun)


def fit_svi(strikes, vols, t: float, forward: float = 1.0, *,
            vol_tolerance: float = 5e-4) -> SVIFit:
    """Fit a single arbitrage-constrained raw SVI slice to market points."""
    strikes = np.asarray(strikes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    if strikes.shape != vols.shape or strikes.size < 3:
        raise ValueError(f"need at least 3 matching strike/vol points, got {strikes.size}")
    if np.any(strikes <= 0) or np.any(vols <= 0):
        raise ValueError("strikes and vols must be positive")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")

    k = np.log(strikes / forward)
    w = vols * vols * t
    w_max = float(np.max(w))

    def outer(params):
        m, sigma = float(params[0]), float(params[1])
        if sigma <= 1e-8:
            return 1e12
        y = (k - m) / sigma
        _, sse = _inner_linear_fit(y, w, sigma, w_max)
        return sse

    span = float(np.max(k) - np.min(k)) or 0.1
    best_x, best_val = None, np.inf
    for m0 in (0.0, float(np.mean(k))):
        for s0 in (0.15 * span, 0.5 * span, 1.5 * span):
            res = minimize(outer, np.array([m0, max(s0, 1e-6)]), method="Nelder-Mead",
                           options={"xatol": 1e-11, "fatol": 1e-18, "maxiter": 600})
            if res.fun < best_val:
                best_val, best_x = float(res.fun), res.x

    if best_x is None:  # pragma: no cover
        raise ConvergenceError("SVI outer search produced no candidate")

    m, sigma = float(best_x[0]), float(max(best_x[1], 1e-8))
    y = (k - m) / sigma
    (a, d, c), _ = _inner_linear_fit(y, w, sigma, w_max)
    b = c / sigma
    rho = float(np.clip(d / c, -0.999999, 0.999999)) if c > 1e-14 else 0.0
    params = SVIParams(a=float(a), b=float(b), rho=rho, m=m, sigma=sigma, t=t)

    model_vols = np.asarray(params.vol(k), dtype=float)
    err = model_vols - vols
    rmse = float(np.sqrt(np.mean(err * err)))
    max_err = float(np.max(np.abs(err)))

    warnings = list(params.violates())
    g = params.durrleman(np.linspace(float(np.min(k)) - 1.5, float(np.max(k)) + 1.5, 400))
    if np.any(g < -1e-8):
        warnings.append(f"butterfly arbitrage: Durrleman g dips to {float(np.min(g)):.3g}")
    if max_err > vol_tolerance:
        warnings.append(f"fit misses a quote by {max_err * 100:.3f} vol points")

    return SVIFit(params=params, rmse=rmse, max_abs_vol_error=max_err,
                  arbitrage_free=not any("arbitrage" in x or "<" in x or ">" in x for x in warnings),
                  warnings=tuple(warnings))


# --------------------------------------------------------------------------
# Vanna-volga
# --------------------------------------------------------------------------
def vanna_volga_vol(K, t, K1, K2, K3, s1, s2, s3):
    """Second-order vanna-volga interpolation through three pivots.

    Guarded where the legacy ``getVV`` was not: the square root argument can
    go negative for a steep smile, and ``d1 d2`` vanishes at two strikes,
    which made the legacy version raise a bare domain error or divide by zero.
    """
    K = np.asarray(K, dtype=float)
    sq = s2 * math.sqrt(t)
    # d1/d2 measured against the middle pivot, matching the standard formulation
    dd1 = np.log(K2 / K) / sq + sq / 2.0
    dd2 = dd1 - sq

    l1 = np.log(K2 / K) * np.log(K3 / K) / (math.log(K2 / K1) * math.log(K3 / K1))
    l2 = np.log(K / K1) * np.log(K3 / K) / (math.log(K2 / K1) * math.log(K3 / K2))
    l3 = np.log(K / K1) * np.log(K / K2) / (math.log(K3 / K1) * math.log(K3 / K2))
    first = l1 * s1 + l2 * s2 + l3 * s3

    d1k1 = math.log(K2 / K1) / sq + sq / 2.0
    d1k3 = math.log(K2 / K3) / sq + sq / 2.0
    D1 = first - s2
    D2 = (l1 * d1k1 * (d1k1 - sq) * (s1 - s2) ** 2
          + l3 * d1k3 * (d1k3 - sq) * (s3 - s2) ** 2)

    prod = dd1 * dd2
    disc = s2 * s2 + prod * (2.0 * s2 * D1 + D2)
    safe = np.where(np.abs(prod) < 1e-10, 1.0, prod)
    second = s2 + (-s2 + np.sqrt(np.maximum(disc, 0.0))) / safe
    # Where d1*d2 vanishes the expansion degenerates to the first-order term.
    return np.where(np.abs(prod) < 1e-10, first, second)


# --------------------------------------------------------------------------
# The cached slice
# --------------------------------------------------------------------------
INTERPOLATORS = ("SVI", "VV25", "VV10", "SABR25", "SABR10")


@dataclass
class SmileSlice:
    """One expiry's smile: five anchor points plus a fitted interpolator.

    Built once and cached by the surface.  ``vol(K)`` is then a cheap
    evaluation instead of a twelve-parameter optimisation.
    """

    t: float
    forward: float
    atm_vol: float
    strikes: np.ndarray
    vols: np.ndarray
    deltas: tuple[float, ...]
    sabr_25: SabrParams
    sabr_10: SabrParams
    conv: DeltaConvention
    method: str = "SVI"
    svi: SVIFit | None = None
    warnings: tuple[str, ...] = ()

    @classmethod
    def build(cls, t: float, atm_vol: float, sabr_25: SabrParams, sabr_10: SabrParams,
              conv: DeltaConvention | bool, *, forward: float = 1.0,
              method: str = "SVI") -> "SmileSlice":
        """Construct the 10d/25d/ATM/25d/10d anchor set, then fit."""
        if method not in INTERPOLATORS:
            raise ValueError(f"unknown interpolation method {method!r}; expected one of {INTERPOLATORS}")
        conv = conv if isinstance(conv, DeltaConvention) else DeltaConvention(bool(conv))
        deltas = (-0.10, -0.25, 0.50, 0.25, 0.10)
        strikes = np.empty(5)
        vols = np.empty(5)
        warn: list[str] = []
        strikes[0], vols[0] = sabr.smile_strike_and_vol(sabr_10, -0.10, t, False, conv)
        strikes[1], vols[1] = sabr.smile_strike_and_vol(sabr_25, -0.25, t, False, conv)
        strikes[2], vols[2] = black.dns_strike(forward, atm_vol, t, conv), atm_vol
        strikes[3], vols[3] = sabr.smile_strike_and_vol(sabr_25, 0.25, t, True, conv)
        strikes[4], vols[4] = sabr.smile_strike_and_vol(sabr_10, 0.10, t, True, conv)

        order = np.argsort(strikes)
        if not np.all(order == np.arange(5)):
            warn.append("anchor strikes are not monotone in delta; the smile may be malformed")
            strikes, vols = strikes[order], vols[order]

        svi_fit = None
        if method == "SVI":
            svi_fit = fit_svi(strikes, vols, t, forward)
            warn.extend(svi_fit.warnings)
        return cls(t=t, forward=forward, atm_vol=atm_vol, strikes=strikes, vols=vols,
                   deltas=deltas, sabr_25=sabr_25, sabr_10=sabr_10, conv=conv,
                   method=method, svi=svi_fit, warnings=tuple(warn))

    def vol(self, K):
        """Implied volatility at strike ``K`` (or an array of strikes)."""
        K = np.asarray(K, dtype=float)
        if np.any(K <= 0):
            raise ValueError("strike must be positive")
        if self.method == "SVI":
            out = self.svi.params.vol(np.log(K / self.forward))
        elif self.method == "SABR25":
            out = sabr.lognormal_vol(K, self.sabr_25)
        elif self.method == "SABR10":
            out = sabr.lognormal_vol(K, self.sabr_10)
        elif self.method == "VV25":
            out = vanna_volga_vol(K, self.t, self.strikes[1], self.strikes[2], self.strikes[3],
                                  self.vols[1], self.vols[2], self.vols[3])
        elif self.method == "VV10":
            out = vanna_volga_vol(K, self.t, self.strikes[0], self.strikes[2], self.strikes[4],
                                  self.vols[0], self.vols[2], self.vols[4])
        else:  # pragma: no cover
            raise ValueError(f"unknown interpolation method {self.method!r}")
        return float(out) if np.isscalar(K) or out.ndim == 0 else np.asarray(out)

    def strike_from_delta(self, target_delta: float, is_call: bool, **kw) -> tuple[float, float]:
        """Delta strike on the *interpolated* smile, not on a single SABR."""
        from .numerics import fixed_point
        vol = self.atm_vol

        def step(v: float) -> float:
            K = black.strike_from_delta(target_delta, self.forward, v, self.t, is_call, self.conv)
            return float(self.vol(K))

        vol = fixed_point(step, vol, tol=kw.get("tol", 1e-11), max_iter=kw.get("max_iter", 80),
                          what=f"{target_delta:+.2f}-delta strike")
        K = black.strike_from_delta(target_delta, self.forward, vol, self.t, is_call, self.conv)
        return K, vol
