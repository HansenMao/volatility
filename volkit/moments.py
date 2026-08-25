"""Risk-neutral distributions read off a smile, and how two of them make a third.

Two jobs, both needed by the analysis screen.

**Reading a smile as a distribution.** Breeden-Litzenberger says the second
strike derivative of a call price is the risk-neutral density, and the first
derivative is (minus) the survival function.  That turns any of volkit's
interpolators into a distribution of ``x = log(S_T / F)`` on a grid, from which
the variance, skew and excess kurtosis the market is pricing fall out by
integration.  The same numbers computed from a history of spot are what the
market actually got, so the two sit side by side.

**Combining two of them.**  A cross is the product of two legs, so its log
return is the signed sum of theirs.  The variance triangle for that is exact
and is what ``cross.py`` already uses.  There is no equally exact triangle for
the risk reversal or the butterfly, because two marginals and one correlation
do not determine a joint distribution.  What is done here is to *choose* the
joint distribution explicitly -- each leg keeps its own marked marginal, and
they are tied together by a Gaussian copula at the marked correlation -- and
then integrate the cross's whole smile out of it on a deterministic tensor
grid.  Nothing is fitted and nothing is simulated, so the same inputs give the
same numbers to the last digit.

Two approximations are made and neither is hidden:

* **The measure.**  A leg's risk-neutral density is quoted under its own
  domestic measure; the cross's is under a third.  Combining them ignores that
  change of measure.  The level of it is absorbed by renormalising the
  combined distribution back onto its own forward, which is done here; the
  effect on the shape is not, and is left in the answer.
* **The copula.**  A Gaussian copula is an assumption about tail dependence
  that the market does not quote.  It is the reason two legs with fat tails
  can produce a cross whose tails look thinner than they are.

The size of both is bounded from below by the diagnostic in
``reconstruction_error``: run each *leg* through the same grid on its own and
see how far its risk reversal and butterfly come back from where they started.
A cross difference smaller than that is not evidence of anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtr

from . import black
from .black import DeltaConvention
from .numerics import ConvergenceError, solve_scalar

# Grid defaults.  ``SPAN`` is in units of the at-the-money total volatility, so
# the grid widens with the expiry rather than being fixed in strike terms.
SPAN = 6.0
NODES = 1601
COPULA_SPAN = 5.5
COPULA_NODES = 161


@dataclass(frozen=True)
class Moments:
    """Cumulants of ``log(S_T / F)``, and the standardised shape numbers."""

    mean: float
    variance: float
    kappa3: float
    kappa4: float

    @property
    def sd(self) -> float:
        return math.sqrt(max(self.variance, 0.0))

    @property
    def skew(self) -> float:
        s = self.sd
        return self.kappa3 / s ** 3 if s > 0 else 0.0

    @property
    def excess_kurtosis(self) -> float:
        v = self.variance
        return self.kappa4 / (v * v) if v > 0 else 0.0

    def annualised_vol(self, t: float) -> float:
        return self.sd / math.sqrt(t) if t > 0 else 0.0


def _simpson_weights(n: int, h: float) -> np.ndarray:
    """Composite Simpson weights, falling back to the trapezoid on even n."""
    if n < 3:
        return np.full(n, h)
    if n % 2 == 0:                       # Simpson needs an odd node count
        w = np.full(n, h)
        w[0] = w[-1] = 0.5 * h
        return w
    w = np.ones(n)
    w[1:-1:2] = 4.0
    w[2:-1:2] = 2.0
    return w * (h / 3.0)


@dataclass
class Distribution:
    """The risk-neutral law of ``x = log(S_T / F)``, tabulated on a grid.

    Everything is per unit of forward, so a distribution built from a yen
    surface and one built from a euro surface are directly comparable and can
    be combined without carrying either contract's units around.
    """

    x: np.ndarray
    pdf: np.ndarray
    cdf: np.ndarray
    t: float
    label: str = ""
    captured: float = 1.0
    forward_error: float = 0.0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._w = _simpson_weights(self.x.size, float(self.x[1] - self.x[0]))
        # The moments are taken against the mass actually on the grid; a grid
        # that has lost 2% of the distribution would otherwise quietly report
        # a variance 2% too small.
        self._mass = float(np.sum(self._w * self.pdf))

    # -- shape ------------------------------------------------------------
    def moments(self) -> Moments:
        p = self._w * self.pdf / max(self._mass, 1e-300)
        m1 = float(np.sum(p * self.x))
        d = self.x - m1
        m2 = float(np.sum(p * d ** 2))
        m3 = float(np.sum(p * d ** 3))
        m4 = float(np.sum(p * d ** 4))
        return Moments(mean=m1, variance=m2, kappa3=m3, kappa4=m4 - 3.0 * m2 * m2)

    def mgf(self, c: float) -> float:
        """``E[exp(c x)]`` -- 1 at ``c = 1`` for a martingale, and the
        convexity of the inverted quote at ``c = -1``.

        A leg that enters a cross the other way up contributes the second of
        these, and it is a real number, not a rounding error: for a lognormal
        it is ``exp(sigma^2 T)``.  Knowing it exactly is what lets the
        combined distribution tell a genuine triangle convexity apart from a
        grid problem.
        """
        p = self._w * self.pdf / max(self._mass, 1e-300)
        return float(np.sum(p * np.exp(float(c) * self.x)))

    # -- inversion --------------------------------------------------------
    def quantile(self, u):
        """``x`` at probability ``u``, clamped to the range the grid holds."""
        c = np.maximum.accumulate(self.cdf)          # kill any numerical dips
        lo, hi = float(c[0]), float(c[-1])
        uu = np.clip(np.asarray(u, dtype=float), lo, hi)
        return np.interp(uu, c, self.x)

    def clamped_mass(self, u, weight) -> float:
        """The *probability* the grid could not reach, not the share of nodes.

        Counting nodes would report several percent for any quadrature with
        tails, because a uniform grid spends many of its nodes out there
        carrying almost no probability.
        """
        c = np.maximum.accumulate(self.cdf)
        uu = np.asarray(u, dtype=float)
        w = np.asarray(weight, dtype=float)
        outside = (uu < c[0]) | (uu > c[-1])
        return float(np.sum(w[outside]) / max(np.sum(w), 1e-300))


def distribution_from_surface(surface, expiry, *, method: str | None = None, cut: str = "TK",
                              nodes: int = NODES, span: float = SPAN,
                              label: str = "") -> Distribution:
    """Turn a marked smile into a risk-neutral distribution of the log return.

    The call curve is differentiated twice, which is why the grid is uniform in
    log-moneyness and generous: a one-sided or unevenly spaced difference here
    is what turns a perfectly good smile into a density with a kink in it.
    """
    t = surface.clock.years_to(surface.clock.coerce_datetime(expiry))
    if t <= 0:
        raise ValueError(f"expiry must be in the future to have a distribution, got t={t:.6g}y")
    atm = float(surface.atm_vol(expiry, cut))
    if atm <= 0:
        raise ValueError(f"{surface.pair}: ATM volatility is not positive at this expiry")
    s = atm * math.sqrt(t)
    n = int(nodes) | 1                                   # odd, so Simpson applies
    x = np.linspace(-span * s, span * s, n)
    k = np.exp(x)
    vols = np.asarray(surface.vol(k, expiry, method, cut), dtype=float)
    warnings: list[str] = []
    if not np.all(np.isfinite(vols)) or np.any(vols <= 0):
        bad = int(np.sum(~np.isfinite(vols) | (vols <= 0)))
        raise ValueError(
            f"{surface.pair}: the smile is not usable at {bad} of {n} grid points spanning "
            f"{span:g} at-the-money standard deviations; the interpolation breaks down "
            f"before the distribution can be read off it"
        )

    # Call prices per unit of forward.  cdf(K) = 1 + dC/dK exactly, so the
    # distribution comes from one differentiation of a smooth curve and the
    # density from a second, rather than two of a kinked payoff.
    c = np.asarray(black.price(1.0, k, vols, t, True), dtype=float)
    dc_dx = np.gradient(c, x, edge_order=2)
    cdf = 1.0 + dc_dx / k
    pdf = np.gradient(cdf, x, edge_order=2)

    neg = pdf < -1e-9
    if np.any(neg):
        lo, hi = float(np.min(x[neg])), float(np.max(x[neg]))
        warnings.append(
            f"the marked smile implies a negative density between K/F "
            f"{math.exp(lo):.4f} and {math.exp(hi):.4f} ({int(neg.sum())} of {n} points); "
            f"the moments below are taken over it as it stands rather than clipped"
        )
    w = _simpson_weights(n, float(x[1] - x[0]))
    captured = float(np.sum(w * pdf))
    if captured < 0.98:
        warnings.append(
            f"only {captured:.2%} of the distribution lies inside {span:g} standard "
            f"deviations; widen the grid before trusting the tail-sensitive numbers"
        )
    fwd_err = float(np.sum(w * pdf * k)) / max(captured, 1e-300) - 1.0
    if abs(fwd_err) > 5e-3:
        warnings.append(
            f"the density reprices the forward {fwd_err:+.3%} away from 1; this is grid "
            f"truncation, and it biases the mean of the log return by about that much"
        )
    return Distribution(x=x, pdf=pdf, cdf=cdf, t=t, label=label or surface.pair,
                        captured=captured, forward_error=fwd_err, warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# combining two legs
# ---------------------------------------------------------------------------


def triangle_coefficients(pair: str, leg_a: str, leg_b: str) -> tuple[int, int]:
    """How the two legs' log returns add up to the cross's.

    ``AUDJPY = AUDUSD x USDJPY`` gives ``(+1, +1)``; ``EURGBP`` from ``EURUSD``
    and ``GBPUSD`` gives ``(+1, -1)``.  This is the same fact that
    ``cross.infer_leg_signs`` encodes for the variance triangle, stated in the
    form the higher cumulants need -- there the two signs only ever appear as
    a product, and an odd moment needs them one at a time.  Getting the
    product right and the individual signs wrong would leave the variance
    correct and flip the risk reversal, which is the failure the cross triangle
    already had once.
    """
    pair, leg_a, leg_b = pair.upper(), leg_a.upper(), leg_b.upper()
    base, term = pair[:3], pair[3:6]
    common = ({leg_a[:3], leg_a[3:6]} & {leg_b[:3], leg_b[3:6]}) - {base, term}
    if not common:
        raise ValueError(
            f"legs {leg_a} and {leg_b} share no third currency, so they cannot build {pair}"
        )
    c = common.pop()

    def coeff(leg: str) -> int:
        lb, lt = leg[:3], leg[3:6]
        if lb == base and lt == c:
            return 1                     # base -> common
        if lb == c and lt == base:
            return -1
        if lb == c and lt == term:
            return 1                     # common -> term
        if lb == term and lt == c:
            return -1
        raise ValueError(f"leg {leg} does not connect {pair} to {c}")

    return coeff(leg_a), coeff(leg_b)


@dataclass
class Combined:
    """A cross distribution built from two leg distributions and a correlation."""

    xc: np.ndarray                 # combined log returns at the quadrature nodes
    weight: np.ndarray             # the matching probability weights
    t: float
    rho: float
    coefficients: tuple[int, int]
    conv: DeltaConvention
    clamped: float = 0.0
    shift: float = 0.0             # log shift applied to reprice the forward
    convexity: float = 0.0         # rho * sd_a * sd_b, the part of it that is expected
    warnings: tuple[str, ...] = ()

    def moments(self) -> Moments:
        w = self.weight
        m1 = float(np.sum(w * self.xc))
        d = self.xc - m1
        m2 = float(np.sum(w * d ** 2))
        m3 = float(np.sum(w * d ** 3))
        m4 = float(np.sum(w * d ** 4))
        return Moments(m1, m2, m3, m4 - 3.0 * m2 * m2)

    def call(self, k):
        """Undiscounted call price per unit of forward, struck at ``k = K/F``."""
        kk = np.atleast_1d(np.asarray(k, dtype=float))
        pay = np.maximum(np.exp(self.xc)[None, :] - kk[:, None], 0.0)
        out = pay @ self.weight
        return out if np.ndim(k) else float(out[0])

    def implied_vol(self, k) -> float:
        """Black volatility reproducing this distribution's price at ``k``."""
        k = float(k)
        return black.implied_vol(float(self.call(k)), 1.0, k, self.t, True)

    def smile(self, ks) -> np.ndarray:
        return np.array([self.implied_vol(k) for k in np.asarray(ks, dtype=float)])

    def delta_strike(self, delta: float, is_call: bool) -> tuple[float, float]:
        """Strike and volatility at a delta, solved on this combined smile."""
        target = abs(delta) if is_call else -abs(delta)
        atm = self.implied_vol(1.0)
        s = max(atm * math.sqrt(self.t), 1e-6)

        def gap(z: float) -> float:
            k = math.exp(z)
            v = self.implied_vol(k)
            return float(black.delta(1.0, k, v, self.t, is_call, self.conv)) - target

        lo, hi = (0.02 * s, 4.0 * s) if is_call else (-4.0 * s, -0.02 * s)
        try:
            z = solve_scalar(gap, 0.5 * (lo + hi), bracket=(lo, hi),
                             what=f"{target:+.2f} delta strike on the combined smile")
        except ConvergenceError as exc:
            raise ConvergenceError(
                f"the combined cross smile has no {target:+.2f} delta strike between "
                f"K/F {math.exp(lo):.4f} and {math.exp(hi):.4f}: {exc}"
            ) from None
        k = math.exp(z)
        return k, self.implied_vol(k)

    def table(self, deltas=(0.10, 0.25)) -> dict:
        """ATM, risk reversals and butterflies, in the book's own convention."""
        atm_k = black.dns_strike(1.0, self.implied_vol(1.0), self.t, self.conv)
        # One pass of the delta-neutral strike is enough: it moves the ATM by
        # far less than the grid error, and iterating it would hide that.
        atm = self.implied_vol(atm_k)
        out = {"atm": atm, "atm_strike": atm_k}
        for d in deltas:
            kc, vc = self.delta_strike(d, True)
            kp, vp = self.delta_strike(d, False)
            tag = f"{int(round(d * 100))}"
            out[f"rr{tag}"] = vc - vp
            out[f"fly{tag}"] = 0.5 * (vc + vp) - atm
            out[f"call{tag}"] = vc
            out[f"put{tag}"] = vp
        return out


def combine(dist_a: Distribution, dist_b: Distribution, coefficients: tuple[int, int],
            rho: float, conv: DeltaConvention | bool = False, *,
            nodes: int = COPULA_NODES, span: float = COPULA_SPAN) -> Combined:
    """Tie two leg distributions together with a Gaussian copula.

    The tensor grid is in the *normal scores* of the two legs, not in their
    returns, so each leg keeps exactly the marginal its own smile implies and
    the correlation enters only through the dependence.  The grid is uniform
    with Simpson weights rather than Gauss-Hermite: the option payoff has a
    kink, and a quadrature tuned for smooth integrands converges badly across
    it.
    """
    if not -1.0 <= rho <= 1.0:
        raise ValueError(f"correlation must lie in [-1, 1], got {rho!r}")
    if abs(dist_a.t - dist_b.t) > 1e-9:
        raise ValueError(
            f"the two legs are at different expiries ({dist_a.t:.6f}y and {dist_b.t:.6f}y); "
            f"a triangle only holds at a common maturity"
        )
    ca, cb = coefficients
    n = int(nodes) | 1
    z = np.linspace(-span, span, n)
    h = float(z[1] - z[0])
    w1 = _simpson_weights(n, h) * np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)

    z1 = z[:, None]
    z2 = (rho * z[:, None] + math.sqrt(max(1.0 - rho * rho, 0.0)) * z[None, :])
    u1 = ndtr(np.broadcast_to(z1, (n, n)))
    u2 = ndtr(z2)
    xa = dist_a.quantile(u1)
    xb = dist_b.quantile(u2)
    weight = (w1[:, None] * w1[None, :]).ravel()
    weight = weight / float(np.sum(weight))     # the tails outside the grid are re-spread
    xc = (ca * xa + cb * xb).ravel()
    clamped = max(dist_a.clamped_mass(u1.ravel(), weight),
                  dist_b.clamped_mass(u2.ravel(), weight))

    # The combined law must price its own forward at 1 or every strike is
    # measured from the wrong place.  The shift absorbs both the grid
    # truncation and the level part of the measure change that is being
    # ignored; what it cannot absorb is the effect on the shape.
    m = float(np.sum(weight * np.exp(xc)))
    if not math.isfinite(m) or m <= 0:
        raise ValueError("the combined distribution does not have a finite forward")
    shift = math.log(m)
    xc = xc - shift

    # The product of two martingales is not a martingale unless they are
    # independent, and a leg entering the cross upside down is not a
    # martingale at all.  Both effects are known in closed form from the legs
    # themselves -- the moment generating functions at the triangle
    # coefficients, plus the dependence term rho*sd_a*sd_b -- so a shift of
    # that size is the triangle's own convexity and is expected.  The cross's
    # forward is observable and is the right anchor, so the shift is simply
    # applied; only the part left unexplained is evidence of a grid problem,
    # and that is what gets reported.
    sd_a = dist_a.moments().sd
    sd_b = dist_b.moments().sd
    convexity = (math.log(max(dist_a.mgf(ca), 1e-300))
                 + math.log(max(dist_b.mgf(cb), 1e-300))
                 + ca * cb * rho * sd_a * sd_b)
    warnings: list[str] = []
    if clamped > 0.02:
        warnings.append(
            f"{clamped:.1%} of the copula grid falls outside the range the legs' own grids "
            f"cover and was held at their extreme quantiles; the wings of the triangle are "
            f"less reliable than the body"
        )
    if abs(shift - convexity) > 5e-3:
        warnings.append(
            f"the combined law needed a {shift:+.3%} shift to reprice its forward, where the "
            f"triangle's own convexity accounts for {convexity:+.3%}. The {shift - convexity:+.3%} "
            f"left over is grid truncation or the change of measure between the legs' domestic "
            f"currencies and the cross's, neither of which this method corrects for"
        )
    if not isinstance(conv, DeltaConvention):
        conv = DeltaConvention(bool(conv))
    return Combined(xc=xc, weight=weight, t=dist_a.t, rho=float(rho),
                    coefficients=(ca, cb), conv=conv, clamped=clamped,
                    shift=shift, convexity=convexity, warnings=tuple(warnings))


def reconstruction_error(dist: Distribution, conv: DeltaConvention | bool,
                         reference: dict, deltas=(0.10, 0.25), *,
                         nodes: int = COPULA_NODES, span: float = COPULA_SPAN) -> dict:
    """Push one leg through the grid alone and see what comes back.

    Combining a distribution with a point mass reproduces it, so any
    difference between what goes in and what comes out is the machinery's own
    error -- grid truncation, the two differentiations, the quadrature.  It is
    the floor below which a cross discrepancy means nothing, and it is
    reported next to the triangle for exactly that reason.
    """
    flat = Distribution(x=np.array([-1e-9, 0.0, 1e-9]), pdf=np.array([0.0, 1e9, 0.0]),
                        cdf=np.array([0.0, 0.5, 1.0]), t=dist.t, label="degenerate")
    solo = combine(dist, flat, (1, 1), 0.0, conv, nodes=nodes, span=span)
    got = solo.table(deltas)
    out = {}
    for key, value in reference.items():
        if key in got and isinstance(value, (int, float)):
            out[key] = float(got[key]) - float(value)
    return out
