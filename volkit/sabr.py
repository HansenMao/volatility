"""Hagan (2002) SABR and calibration from FX broker quotes.

Self-contained: the legacy code depended on ``pysabr``, which is unmaintained
and is not installed in most environments.  Owning the implementation also
means owning the numerics -- in particular the ``z / x(z)`` factor, which is
0/0 at the money and needs a series expansion rather than a naive ratio.

Calibration is restated as a properly posed problem.  The legacy
``solveSabrFromMarket`` ran an ``fsolve`` over the unknown high-strike vol, and
*inside* each objective evaluation fitted a fresh three-point SABR -- a nested
solve with no convergence checking on either level.  Here the three market
conditions (at-the-money vol, risk reversal, market strangle premium) are
matched directly by the three SABR parameters, with alpha eliminated
analytically at each step so only a well-conditioned two-dimensional problem
is handed to the optimiser.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq, least_squares

from . import black
from .black import DeltaConvention
from .numerics import ConvergenceError, solve_scalar

_SMALL_Z = 1e-6


@dataclass(frozen=True)
class SabrParams:
    """SABR parameters for a single expiry.

    ``beta`` is fixed at 1 (lognormal) for FX, matching the legacy model.
    ``log_volvol`` is the scale-free ``nu * sqrt(t)`` that the legacy code
    called ``slog`` and stored in its parameter matrix.
    """

    alpha: float
    rho: float
    volvol: float
    t: float
    beta: float = 1.0
    f: float = 1.0

    @property
    def log_volvol(self) -> float:
        return self.volvol * math.sqrt(self.t)

    def with_alpha(self, alpha: float) -> "SabrParams":
        return SabrParams(alpha, self.rho, self.volvol, self.t, self.beta, self.f)


def _z_over_x(z: np.ndarray, rho: float) -> np.ndarray:
    """``z / x(z)`` with a Taylor expansion through the removable singularity.

    ``x(z) = log((sqrt(1 - 2 rho z + z^2) + z - rho) / (1 - rho))`` vanishes
    linearly at ``z = 0``, so the ratio is 0/0 at the money.  Evaluating it
    directly loses all precision for small ``z``; the expansion
    ``1 + rho z / 2 + (2 - 3 rho^2) z^2 / 12`` is used instead.
    """
    z = np.asarray(z, dtype=float)
    small = np.abs(z) < _SMALL_Z
    safe_z = np.where(small, 1.0, z)  # keep the log argument valid where unused
    inner = np.sqrt(1.0 - 2.0 * rho * safe_z + safe_z * safe_z) + safe_z - rho
    x = np.log(np.maximum(inner / (1.0 - rho), 1e-300))
    ratio = np.where(np.abs(x) < 1e-300, 1.0, safe_z / x)
    series = 1.0 + rho * z / 2.0 + (2.0 - 3.0 * rho * rho) * z * z / 12.0
    return np.where(small, series, ratio)


def lognormal_vol(K, p: SabrParams):
    """Hagan lognormal implied volatility.  Vectorised over strike."""
    K = np.asarray(K, dtype=float)
    if np.any(K <= 0):
        raise ValueError("SABR strikes must be positive")
    if p.alpha <= 0:
        raise ValueError(f"SABR alpha must be positive, got {p.alpha!r}")
    f, beta, rho, nu, t = p.f, p.beta, p.rho, p.volvol, p.t
    one_b = 1.0 - beta
    log_fk = np.log(f / K)
    fk = (f * K) ** (one_b / 2.0)
    denom = fk * (1.0 + one_b**2 / 24.0 * log_fk**2 + one_b**4 / 1920.0 * log_fk**4)
    z = (nu / p.alpha) * fk * log_fk
    correction = 1.0 + (
        one_b**2 / 24.0 * p.alpha**2 / (f * K) ** one_b
        + 0.25 * rho * beta * nu * p.alpha / fk
        + (2.0 - 3.0 * rho * rho) / 24.0 * nu * nu
    ) * t
    return p.alpha / denom * _z_over_x(z, rho) * correction


def atm_vol(p: SabrParams) -> float:
    """Hagan volatility at ``K = F``, in closed form."""
    return float(lognormal_vol(p.f, p))


def _vol_vs_alpha(K: float, alphas, rho: float, nu: float, t: float,
                  f: float = 1.0, beta: float = 1.0):
    """Hagan volatility at one strike, vectorised over *alpha*.

    ``lognormal_vol`` vectorises over the strike; scanning for alpha needs the
    other direction, so the same expansion is written out once more here.
    """
    a = np.asarray(alphas, dtype=float)
    ob = 1.0 - beta
    log_fk = math.log(f / K)
    fk = (f * K) ** (ob / 2.0)
    denom = fk * (1.0 + ob**2 / 24.0 * log_fk**2 + ob**4 / 1920.0 * log_fk**4)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (nu / a) * fk * log_fk
        correction = 1.0 + (
            ob**2 / 24.0 * a**2 / (f * K) ** ob
            + 0.25 * rho * beta * nu * a / fk
            + (2.0 - 3.0 * rho * rho) / 24.0 * nu * nu
        ) * t
        return a / denom * _z_over_x(z, rho) * correction


def alpha_roots_at_forward(atm_vol: float, rho: float, volvol: float, t: float,
                           f: float = 1.0, beta: float = 1.0) -> list[float]:
    """Every positive alpha satisfying Hagan's at-the-forward condition, in closed form.

    Hagan's ATM expansion is a cubic in alpha (a quadratic when beta = 1):

        A a^3 + B a^2 + (1 + C) a - sigma_atm F^(1-beta) = 0

    Solving it exactly is worth doing for two reasons beyond speed.  It shows
    when *no* positive alpha exists, so the (rho, nu) pair can be rejected
    immediately rather than after a failed search; and it shows when more than
    one does -- the smallest positive root is the market-standard choice, and
    the existence of others is a concrete instance of the non-uniqueness that
    makes naive calibration unstable.
    """
    ob = 1.0 - beta
    A = ob * ob * t / (24.0 * f ** (2 * ob))
    B = rho * beta * volvol * t / (4.0 * f ** ob)
    C = (2.0 - 3.0 * rho * rho) * volvol * volvol * t / 24.0
    coeffs = [A, B, 1.0 + C, -atm_vol * f ** ob]
    while coeffs and abs(coeffs[0]) < 1e-300:
        coeffs = coeffs[1:]
    if len(coeffs) < 2:
        return []
    roots = np.roots(coeffs)
    out = [float(r.real) for r in roots
           if abs(r.imag) <= 1e-9 * max(1.0, abs(r.real)) and r.real > 0]
    return sorted(out)


def alpha_from_atm(target_atm_vol: float, K_atm: float, rho: float, volvol: float,
                   t: float, f: float = 1.0, beta: float = 1.0,
                   *, return_count: bool = False):
    """Back out alpha so the smile hits ``target_atm_vol`` at ``K_atm``.

    FX quotes the at-the-money volatility at the delta-neutral straddle strike
    rather than at the forward, so the closed form above is not the answer
    directly -- but it sets the scale, and the search is confined to a grid
    around it.  The smallest positive solution is taken, matching the market
    convention for the cubic.
    """
    seeds = alpha_roots_at_forward(target_atm_vol, rho, volvol, t, f, beta)
    if not seeds:
        raise ConvergenceError(
            f"no positive SABR alpha reproduces an ATM volatility of {target_atm_vol:.4%} "
            f"at rho={rho:.4f}, nu={volvol:.4f}, t={t:.4f}y: Hagan's at-the-forward "
            f"condition has no admissible root for this (rho, nu) pair"
        )
    a0 = seeds[0]

    def residual(a):
        return _vol_vs_alpha(K_atm, a, rho, volvol, t, f, beta) - target_atm_vol

    grid = np.geomspace(max(a0 / 40.0, 1e-9), min(a0 * 40.0, 50.0), 96)
    with np.errstate(all="ignore"):
        vals = residual(grid)
    finite = np.isfinite(vals)
    sign_change = np.where(finite[:-1] & finite[1:] & (np.sign(vals[:-1]) != np.sign(vals[1:])))[0]
    if sign_change.size == 0:
        raise ConvergenceError(
            f"could not bracket a SABR alpha for an ATM volatility of {target_atm_vol:.4%} "
            f"at rho={rho:.4f}, nu={volvol:.4f}, t={t:.4f}y"
        )
    i = int(sign_change[0])
    alpha = brentq(lambda a: float(residual(a)), float(grid[i]), float(grid[i + 1]),
                   xtol=1e-14, rtol=8.9e-16, maxiter=200)
    return (alpha, int(sign_change.size)) if return_count else alpha


@dataclass(frozen=True)
class SabrCalibration:
    """Calibrated parameters plus the diagnostics needed to trust them."""

    params: SabrParams
    residuals: tuple[float, float]
    rr_error: float
    strangle_error: float
    converged: bool
    message: str
    # Healy (2025) shows the three FX quotes need not determine a smile
    # uniquely, and that nearby quotes can jump between solutions.  Any other
    # parameter set that also reprices them is reported rather than discarded.
    alternatives: tuple[SabrParams, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def max_error(self) -> float:
        return max(abs(self.rr_error), abs(self.strangle_error))

    @property
    def unique(self) -> bool:
        return not self.alternatives


def smile_strike_and_vol(p: SabrParams, target_delta: float, t: float, is_call: bool,
                         conv: DeltaConvention | bool, *, tol: float = 1e-10,
                         max_iter: int = 60) -> tuple[float, float]:
    """The strike whose *smile* vol reproduces ``target_delta``.

    Delta and volatility are mutually dependent, so this is a fixed point.  The
    legacy code ran exactly ten iterations and returned whatever it held; this
    iterates to a tolerance, damps if it starts to oscillate, and raises if it
    cannot converge.
    """
    vol = float(lognormal_vol(black.dns_strike(p.f, atm_vol(p), t, conv), p))
    prev_step = None
    damping = 1.0
    for _ in range(max_iter):
        K = black.strike_from_delta(target_delta, p.f, vol, t, is_call, conv)
        new_vol = float(lognormal_vol(K, p))
        step = new_vol - vol
        if abs(step) <= tol * max(1.0, abs(new_vol)):
            return K, new_vol
        if prev_step is not None and step * prev_step < 0:
            damping *= 0.5  # oscillating: contract
        prev_step = step
        vol = vol + damping * step
    raise ConvergenceError(
        f"smile strike for {target_delta:+.3f} delta did not converge in {max_iter} iterations "
        f"(last vol step {abs(step):.3g}); the smile may be too steep at t={t:.4f}y",
        last=vol,
    )


def calibrate(
    atm_volatility: float,
    risk_reversal: float,
    strangle: float,
    delta: float,
    t: float,
    conv: DeltaConvention | bool = False,
    *,
    f: float = 1.0,
    beta: float = 1.0,
    tol: float = 1e-8,
    prior: SabrParams | None = None,
    prior_weight: float = 0.0,
    scan: tuple[int, int] = (13, 9),
    max_solutions: int = 1,
) -> SabrCalibration:
    """Fit SABR to an at-the-money vol, a risk reversal and a market strangle.

    Conventions, all in decimals:

    * ``atm_volatility`` is the delta-neutral-straddle volatility.
    * ``risk_reversal`` is call vol minus put vol at ``delta``.
    * ``strangle`` is the *market* strangle: the single volatility
      ``atm + strangle`` at which both wings are struck, quoted so that the
      model must reprice the resulting two-option premium exactly.

    Three conditions, three parameters.  ``alpha`` is eliminated analytically
    at every step, leaving a two-dimensional problem in ``(rho, nu)``.

    Because that problem is only two-dimensional, the whole admissible box is
    swept on a coarse grid before any local polishing.  That is what makes the
    result independent of the starting point -- an arbitrary set of seeds can
    land in a local basin and report success, which is the failure Healy's
    counterexamples describe.  Sweeping also *finds* the extra solutions when
    the quotes admit more than one; they are returned in ``alternatives``
    instead of being silently discarded.

    ``prior`` with a positive ``prior_weight`` pulls the fit toward a
    reference parameter set -- normally the neighbouring tenor.  The three
    quotes do not always pin the smile down, so this is how a term structure
    is kept from jumping between equally good solutions tenor to tenor.

    The sweep is always run, because locating the global basin is cheap: the
    node cost uses the closed-form at-the-forward alpha and needs no iteration.
    Hunting for *additional* solutions is not cheap -- each candidate basin
    costs a full polish -- so it is opt-in via ``max_solutions``.  Raise it to
    3 to have the fit report competing parameter sets; ``volkit validate``
    does exactly that.
    """
    if atm_volatility <= 0:
        raise ValueError(f"ATM volatility must be positive, got {atm_volatility!r}")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if not 0.0 < delta < 0.5:
        raise ValueError(f"delta must lie in (0, 0.5), got {delta!r}")

    K_atm = black.dns_strike(f, atm_volatility, t, conv)
    ms_vol = atm_volatility + strangle
    if ms_vol <= 0:
        raise ValueError(
            f"market strangle volatility {ms_vol:.6g} is not positive "
            f"(atm={atm_volatility:.6g}, strangle={strangle:.6g})"
        )
    K_ms_call = black.strike_from_delta(delta, f, ms_vol, t, True, conv)
    K_ms_put = black.strike_from_delta(-delta, f, ms_vol, t, False, conv)
    target_ms_premium = float(
        black.price(f, K_ms_call, ms_vol, t, True) + black.price(f, K_ms_put, ms_vol, t, False)
    )
    ms_vega = float(black.vega(f, K_ms_call, ms_vol, t) + black.vega(f, K_ms_put, ms_vol, t))
    sqt = math.sqrt(t)

    def residuals(x: np.ndarray) -> np.ndarray:
        rho, nu = float(x[0]), float(x[1])
        try:
            alpha = alpha_from_atm(atm_volatility, K_atm, rho, nu, t, f, beta)
            p = SabrParams(alpha, rho, nu, t, beta, f)
            _, call_vol = smile_strike_and_vol(p, delta, t, True, conv)
            _, put_vol = smile_strike_and_vol(p, -delta, t, False, conv)
            ms_premium = float(
                black.price(f, K_ms_call, float(lognormal_vol(K_ms_call, p)), t, True)
                + black.price(f, K_ms_put, float(lognormal_vol(K_ms_put, p)), t, False)
            )
        except (ConvergenceError, ValueError, ArithmeticError):
            # ArithmeticError covers the OverflowError raised when a sweep node
            # implies an absurd smile; such a node is simply not a solution.
            return np.array([1e3, 1e3, 0.0, 0.0])
        # The premium residual is divided by vega so both live in volatility
        # units and carry comparable weight in the least-squares norm.
        base = [
            (call_vol - put_vol) - risk_reversal,
            (ms_premium - target_ms_premium) / max(ms_vega, 1e-12),
        ]
        if prior is not None and prior_weight > 0.0:
            base += [prior_weight * (rho - prior.rho),
                     prior_weight * (nu - prior.volvol) * sqt]
        else:
            base += [0.0, 0.0]
        return np.array(base)

    def sweep_cost(rho: float, nu: float) -> float:
        """Cheap stand-in for the residual, used only to locate the basin.

        The exact risk-reversal residual needs two delta-strike fixed points,
        which dominates the cost.  For *ranking* grid nodes the wing vols at
        the (already known) market-strangle strikes carry the same information,
        and the polish step below uses the exact residual anyway.
        """
        try:
            # The closed-form at-the-forward root is pure algebra and is close
            # enough to rank nodes; the polish step re-solves alpha exactly at
            # the delta-neutral strike.
            roots = alpha_roots_at_forward(atm_volatility, rho, nu, t, f, beta)
            if not roots:
                return 1e3
            p = SabrParams(roots[0], rho, nu, t, beta, f)
            vols = np.asarray(lognormal_vol(np.array([K_ms_put, K_ms_call]), p), dtype=float)
            if not np.all(np.isfinite(vols)) or np.any(vols <= 0):
                return 1e3
            rr_proxy = float(vols[1] - vols[0])
            ms_premium = float(
                black.price(f, K_ms_call, float(vols[1]), t, True)
                + black.price(f, K_ms_put, float(vols[0]), t, False)
            )
        except (ConvergenceError, ValueError, ArithmeticError):
            return 1e3
        return max(abs(rr_proxy - risk_reversal),
                   abs(ms_premium - target_ms_premium) / max(ms_vega, 1e-12))

    # ---- coarse sweep of the admissible box -----------------------------
    n_rho, n_nu = scan
    rho_grid = np.linspace(-0.95, 0.95, n_rho)
    s_grid = np.geomspace(0.05, 3.0, n_nu)          # scale-free nu * sqrt(t)
    nodes = []
    for r in rho_grid:
        for sc in s_grid:
            nodes.append((float(r), float(sc) / sqt, sweep_cost(float(r), float(sc) / sqt)))
    nodes.sort(key=lambda n: n[2])

    # ---- polish from the best distinct basins ---------------------------
    lo = np.array([-0.999, 1e-6])
    hi = np.array([0.999, 50.0 / sqt])
    solutions: list[tuple[float, float, float]] = []   # (rho, nu, max_err)
    tried: list[tuple[float, float]] = []
    best_node_cost = nodes[0][2]
    for rho0, nu0, node_cost in nodes[:12]:
        # Only basins the sweep says could hold a solution are worth the exact
        # polish, and only if they are far from a solution already found --
        # several adjacent grid nodes drain into the same minimum, and
        # re-polishing them is pure cost with no extra information.
        if solutions and node_cost > max(1e-3, 20.0 * best_node_cost):
            break
        if any(abs(rho0 - r) < 0.25 and abs((nu0 - n) * sqt) < 0.25 for r, n, _ in solutions):
            continue
        tried.append((rho0, nu0))
        try:
            sol = least_squares(residuals, np.clip(np.array([rho0, nu0]), lo, hi),
                                bounds=(lo, hi), xtol=1e-14, ftol=1e-14, gtol=1e-14,
                                max_nfev=300)
        except Exception:  # noqa: BLE001 - a bad basin must not kill the fit
            continue
        err = float(np.max(np.abs(sol.fun[:2])))
        solutions.append((float(sol.x[0]), float(sol.x[1]), err))
        if len(solutions) >= max(1, max_solutions):
            break

    if not solutions:
        raise ConvergenceError(
            f"SABR calibration found no candidate at t={t:.4f}y "
            f"(atm={atm_volatility:.4%}, rr={risk_reversal:.4%}, strangle={strangle:.4%})"
        )

    solutions.sort(key=lambda z: (z[2], abs(z[1])))
    rho, nu, best_err = solutions[0]

    # Distinct parameter sets that reprice the same three quotes.
    good = [z for z in solutions if z[2] < max(tol, 1e-7)]
    distinct: list[tuple[float, float, float]] = []
    for z in good:
        if not any(abs(z[0] - d[0]) < 0.02 and abs((z[1] - d[1]) * sqt) < 0.02 for d in distinct):
            distinct.append(z)

    alpha, alpha_roots = alpha_from_atm(atm_volatility, K_atm, rho, nu, t, f, beta,
                                        return_count=True)
    fun = residuals(np.array([rho, nu]))
    rr_err, ms_err = float(fun[0]), float(fun[1])
    converged = max(abs(rr_err), abs(ms_err)) < 1e-6

    warnings: list[str] = []
    if len(distinct) > 1:
        others = ", ".join(f"(rho={d[0]:+.3f}, nu*sqrt(t)={d[1] * sqt:.3f})" for d in distinct[1:])
        warnings.append(
            f"{len(distinct)} distinct parameter sets reprice these quotes; "
            f"took (rho={rho:+.3f}, nu*sqrt(t)={nu * sqt:.3f}), also found {others}. "
            f"Consider calibrating with a prior from the neighbouring tenor."
        )
    if alpha_roots > 1:
        warnings.append(
            f"the at-the-money condition has {alpha_roots} positive alpha solutions; "
            f"the smallest ({alpha:.6f}) was taken, per market convention"
        )
    if not converged:
        warnings.append(
            f"no (rho, nu) reprices all three quotes: best residual is "
            f"rr={rr_err:.2e}, strangle={ms_err:.2e} in vol units. The market "
            f"strangle may be unattainable for this SABR smile"
        )

    return SabrCalibration(
        params=SabrParams(alpha, rho, nu, t, beta, f),
        residuals=(rr_err, ms_err),
        rr_error=rr_err,
        strangle_error=ms_err,
        converged=converged,
        message="converged" if converged else f"residual rr={rr_err:.2e} strangle={ms_err:.2e} (vol units)",
        alternatives=tuple(SabrParams(
            alpha_from_atm(atm_volatility, K_atm, d[0], d[1], t, f, beta), d[0], d[1], t, beta, f)
            for d in distinct[1:]),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class SmileShape:
    """The ``(rho, nu)`` a marked smile behaves like.

    Not a calibration of the book -- the book's smile is SVI or vanna-volga,
    and this is a *reading* of it: the spot/volatility correlation and
    volatility of volatility that a SABR smile would need to show the same
    risk reversal and butterfly at the same at-the-money level.  It exists so
    that a marked wing and a measured wing can be compared in the same two
    numbers, which is the only way a realized statistic and a quoted spread
    can be put beside each other at all.

    ``rho`` is the correlation, ``nu`` the volatility of volatility per year;
    ``log_volvol`` is the scale-free ``nu * sqrt(t)`` that actually controls
    the shape at this expiry.
    """

    rho: float
    nu: float
    alpha: float
    t: float
    rr_error: float               # fitted minus quoted risk reversal, decimals
    fly_error: float
    converged: bool
    message: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def log_volvol(self) -> float:
        return self.nu * math.sqrt(self.t)

    @property
    def max_error(self) -> float:
        return max(abs(self.rr_error), abs(self.fly_error))


def fit_smile_shape(
    atm_volatility: float,
    risk_reversal: float,
    butterfly: float,
    delta: float,
    t: float,
    conv: DeltaConvention | bool = False,
    *,
    f: float = 1.0,
    beta: float = 1.0,
    scan: tuple[int, int] = (17, 13),
) -> SmileShape:
    """Read ``(rho, nu)`` off an at-the-money level, a risk reversal and a *smile* butterfly.

    ``calibrate`` above matches the **market** strangle, because that is what
    a broker quotes and repricing its premium exactly is what a marking tool
    owes the market.  This one matches the **smile** butterfly
    ``(sigma_call + sigma_put)/2 - sigma_atm`` at the same delta, because that
    is the number the analysis screen has: it is read off whatever surface is
    marked, and it is the number a realized fourth moment is being compared
    against.  Using the market strangle here would compare a premium
    condition against a moment.

    Two conditions, two parameters -- ``alpha`` is eliminated by the
    at-the-money condition as it is everywhere else in this module -- so the
    whole admissible box is swept before any local polish, for the same reason
    ``calibrate`` sweeps it: a starting guess can land in a local basin and
    report success.

    Both residuals are already in volatility units, so they are weighted
    equally and no scaling is needed.
    """
    if atm_volatility <= 0:
        raise ValueError(f"ATM volatility must be positive, got {atm_volatility!r}")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if not 0.0 < delta < 0.5:
        raise ValueError(f"delta must lie in (0, 0.5), got {delta!r}")

    K_atm = black.dns_strike(f, atm_volatility, t, conv)
    sqt = math.sqrt(t)

    def wings(rho: float, nu: float) -> tuple[float, float, float]:
        alpha = alpha_from_atm(atm_volatility, K_atm, rho, nu, t, f, beta)
        p = SabrParams(alpha, rho, nu, t, beta, f)
        _, call_vol = smile_strike_and_vol(p, delta, t, True, conv)
        _, put_vol = smile_strike_and_vol(p, -delta, t, False, conv)
        return alpha, float(call_vol), float(put_vol)

    def residuals(x: np.ndarray) -> np.ndarray:
        rho, nu = float(x[0]), float(x[1])
        try:
            _, call_vol, put_vol = wings(rho, nu)
        except (ConvergenceError, ValueError, ArithmeticError):
            return np.array([1e3, 1e3])
        return np.array([
            (call_vol - put_vol) - risk_reversal,
            (0.5 * (call_vol + put_vol) - atm_volatility) - butterfly,
        ])

    def sweep_cost(rho: float, nu: float) -> float:
        """Rank a node without paying for two delta-strike fixed points.

        The wings are read at the strikes the *quoted* smile itself implies,
        which is enough to order the nodes; the polish below uses the exact
        residual.
        """
        try:
            roots = alpha_roots_at_forward(atm_volatility, rho, nu, t, f, beta)
            if not roots:
                return 1e3
            p = SabrParams(roots[0], rho, nu, t, beta, f)
            k_c = black.strike_from_delta(delta, f, atm_volatility + butterfly, t, True, conv)
            k_p = black.strike_from_delta(-delta, f, atm_volatility + butterfly, t, False, conv)
            vols = np.asarray(lognormal_vol(np.array([k_p, k_c]), p), dtype=float)
            if not np.all(np.isfinite(vols)) or np.any(vols <= 0):
                return 1e3
            return max(abs(float(vols[1] - vols[0]) - risk_reversal),
                       abs(float(0.5 * (vols[0] + vols[1])) - atm_volatility - butterfly))
        except (ConvergenceError, ValueError, ArithmeticError):
            return 1e3

    n_rho, n_nu = scan
    nodes = []
    for r in np.linspace(-0.95, 0.95, n_rho):
        for sc in np.geomspace(0.05, 3.0, n_nu):
            nu = float(sc) / sqt
            nodes.append((float(r), nu, sweep_cost(float(r), nu)))
    nodes.sort(key=lambda n: n[2])

    lo = np.array([-0.999, 1e-6])
    hi = np.array([0.999, 50.0 / sqt])
    best = None
    for rho0, nu0, _cost in nodes[:6]:
        try:
            sol = least_squares(residuals, np.clip(np.array([rho0, nu0]), lo, hi),
                                bounds=(lo, hi), xtol=1e-13, ftol=1e-13, gtol=1e-13,
                                max_nfev=200)
        except Exception:  # noqa: BLE001 - a bad basin must not kill the read
            continue
        err = float(np.max(np.abs(sol.fun)))
        if best is None or err < best[2]:
            best = (float(sol.x[0]), float(sol.x[1]), err, sol)
        if err < 1e-9:
            break
    if best is None:
        raise ConvergenceError(
            f"no SABR shape reproduces atm={atm_volatility:.4%}, rr={risk_reversal:.4%}, "
            f"fly={butterfly:.4%} at {delta:.0%} delta and t={t:.4f}y"
        )
    rho, nu, _err, sol = best
    fun = residuals(np.array([rho, nu]))
    rr_err, fly_err = float(fun[0]), float(fun[1])
    converged = max(abs(rr_err), abs(fly_err)) < 1e-7
    alpha, _, _ = wings(rho, nu)

    warn: list[str] = []
    if not converged:
        warn.append(
            f"no (rho, nu) reproduces this smile exactly: best residual is rr={rr_err:.2e}, "
            f"fly={fly_err:.2e} in volatility units. A SABR smile cannot show every "
            f"combination of a risk reversal and a butterfly, so this is the nearest one"
        )
    if abs(rho) > 0.98:
        warn.append(
            f"the correlation is pinned at the edge of its range ({rho:+.3f}); the quoted "
            f"risk reversal is steeper than a SABR smile of this butterfly can produce"
        )
    return SmileShape(rho=rho, nu=nu, alpha=alpha, t=t, rr_error=rr_err, fly_error=fly_err,
                      converged=converged,
                      message="converged" if converged else
                              f"residual rr={rr_err:.2e} fly={fly_err:.2e} (vol units)",
                      warnings=tuple(warn))
