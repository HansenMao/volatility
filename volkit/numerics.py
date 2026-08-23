"""Guarded root finding and small numerical helpers.

The legacy code reached for ``scipy.optimize.fsolve`` with a hard-coded
initial guess of 1.0 and never inspected the result, and used fixed-count
``while i < 10`` fixed-point loops with no convergence test.  Both fail
silently: fsolve returns its last iterate whether or not it converged, and a
fixed-point loop that oscillates simply returns the oscillation.

Everything here either converges to a stated tolerance or raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.optimize import brentq


class ConvergenceError(RuntimeError):
    """A solver failed to reach its tolerance.  Never raised silently."""

    def __init__(self, message: str, *, last: float | None = None, residual: float | None = None):
        super().__init__(message)
        self.last = last
        self.residual = residual


@dataclass(frozen=True)
class Bracket:
    lo: float
    hi: float
    f_lo: float
    f_hi: float


def find_bracket(
    f: Callable[[float], float],
    x0: float,
    *,
    lo_bound: float = -np.inf,
    hi_bound: float = np.inf,
    growth: float = 1.6,
    max_iter: int = 80,
    initial_step: float | None = None,
) -> Bracket:
    """Expand outwards from ``x0`` until ``f`` changes sign.

    Returns the bracket rather than a root so the caller can choose the
    polishing method.  Raises if no sign change exists within the bounds,
    which is genuine information -- it usually means the target is
    unattainable (an inverted smile, an unreachable delta) rather than a
    solver tuning problem.
    """
    f0 = f(x0)
    if not math.isfinite(f0):
        raise ConvergenceError(f"objective is not finite at the initial point x0={x0!r} (f={f0!r})")
    if f0 == 0.0:
        return Bracket(x0, x0, 0.0, 0.0)

    step = initial_step if initial_step is not None else max(abs(x0) * 0.1, 1e-3)
    lo = hi = x0
    f_lo = f_hi = f0
    for _ in range(max_iter):
        moved = False
        new_hi = min(hi + step, hi_bound)
        if new_hi > hi:
            f_new = f(new_hi)
            if math.isfinite(f_new):
                if f_new * f_hi <= 0:
                    return Bracket(hi, new_hi, f_hi, f_new)
                hi, f_hi = new_hi, f_new
                moved = True
        new_lo = max(lo - step, lo_bound)
        if new_lo < lo:
            f_new = f(new_lo)
            if math.isfinite(f_new):
                if f_new * f_lo <= 0:
                    return Bracket(new_lo, lo, f_new, f_lo)
                lo, f_lo = new_lo, f_new
                moved = True
        if not moved:
            break
        step *= growth
    raise ConvergenceError(
        f"no sign change for the objective on [{lo:.6g}, {hi:.6g}] "
        f"(f={f_lo:.6g} .. {f_hi:.6g}); the target is likely unattainable",
        last=x0,
        residual=f0,
    )


def solve_scalar(
    f: Callable[[float], float],
    x0: float,
    *,
    lo_bound: float = -np.inf,
    hi_bound: float = np.inf,
    xtol: float = 1e-12,
    rtol: float = 8.9e-16,
    max_iter: int = 200,
    bracket: tuple[float, float] | None = None,
    what: str = "value",
) -> float:
    """Solve ``f(x) == 0`` by bracketing then Brent.

    Brent is used rather than a Newton/secant scheme because it cannot leave
    the bracket, so it cannot wander into a region where the objective is
    undefined -- the usual failure mode when inverting a delta or a strangle
    premium.
    """
    if bracket is not None:
        lo, hi = bracket
        f_lo, f_hi = f(lo), f(hi)
        if not (math.isfinite(f_lo) and math.isfinite(f_hi)):
            raise ConvergenceError(
                f"objective is not finite at the supplied bracket ends for {what}: "
                f"f({lo:.6g})={f_lo!r}, f({hi:.6g})={f_hi!r}"
            )
        if f_lo * f_hi > 0:
            br = find_bracket(f, x0, lo_bound=lo_bound, hi_bound=hi_bound)
            lo, hi = br.lo, br.hi
    else:
        br = find_bracket(f, x0, lo_bound=lo_bound, hi_bound=hi_bound)
        lo, hi = br.lo, br.hi
    if lo == hi:
        return lo
    return brentq(f, lo, hi, xtol=xtol, rtol=rtol, maxiter=max_iter)


def fixed_point(
    g: Callable[[float], float],
    x0: float,
    *,
    tol: float = 1e-10,
    max_iter: int = 100,
    damping: float = 1.0,
    what: str = "fixed point",
) -> float:
    """Damped fixed-point iteration that actually checks convergence.

    Replaces the legacy ``while i < 10: v = f(v)`` loops, which ran a fixed
    number of steps and returned whatever they happened to hold -- converged,
    oscillating, or diverged.
    """
    x = x0
    for _ in range(max_iter):
        gx = g(x)
        if not math.isfinite(gx):
            raise ConvergenceError(f"{what} iteration produced a non-finite value from x={x!r}")
        x_new = x + damping * (gx - x)
        if abs(x_new - x) <= tol * max(1.0, abs(x_new)):
            return x_new
        x = x_new
    raise ConvergenceError(
        f"{what} did not converge in {max_iter} iterations (last step {abs(g(x) - x):.3g})",
        last=x,
    )


def safe_sqrt(x: float, *, what: str = "variance", tol: float = 1e-10) -> float:
    """Square root that tolerates round-off below zero but not real negatives.

    The legacy backbone could pass a genuinely negative argument to ``sqrt``
    for a negative rate correlation and blow up with a bare ``math domain
    error`` a dozen frames from the cause.
    """
    if x < -tol:
        raise ValueError(f"negative {what}: {x:.6g}")
    return math.sqrt(max(x, 0.0))


def gauss_legendre(n: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Nodes and weights on [0, 1] for fixed-order Gauss-Legendre."""
    x, w = np.polynomial.legendre.leggauss(n)
    return 0.5 * (x + 1.0), 0.5 * w


def integrate_piecewise(
    f: Callable[[np.ndarray], np.ndarray],
    breakpoints: np.ndarray,
    order: int = 5,
) -> float:
    """Integrate a vectorised ``f`` over a set of smooth panels.

    The instantaneous variance is piecewise smooth: it jumps at every hour
    boundary (the intraday weight profile) and kinks at every event time.
    ``scipy.integrate.quad`` is adaptive and general, so on a discontinuous
    integrand it burns subdivisions hunting the jumps -- which is why the
    legacy code needed ``limit=500`` and was still slow.

    Splitting at the known breakpoints makes each panel smooth, so a fixed
    low-order rule is both faster and more accurate.  The whole integral is
    evaluated in one vectorised call.
    """
    breakpoints = np.asarray(breakpoints, dtype=float)
    if breakpoints.size < 2:
        return 0.0
    lo = breakpoints[:-1]
    hi = breakpoints[1:]
    keep = hi > lo
    if not np.any(keep):
        return 0.0
    lo, hi = lo[keep], hi[keep]
    nodes, weights = gauss_legendre(order)
    # points[i, j] = lo[i] + nodes[j] * (hi[i] - lo[i])
    span = (hi - lo)[:, None]
    points = lo[:, None] + nodes[None, :] * span
    values = f(points.ravel()).reshape(points.shape)
    return float(np.sum(values * weights[None, :] * span))
