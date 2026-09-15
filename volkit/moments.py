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
  can produce a cross whose tails look thinner than they are: the size of one
  leg's move and the size of the other's are tied together only through the
  correlation, roughly as its square, so on a low-correlation cross the two
  legs' fat-tailed days almost never coincide, and a correlation fixed for
  the life of the option has no volatility of its own.  Both are what a cross
  butterfly is paid for, and both can be **marked** (:class:`Dependence`,
  the workbook's ``CROSS_DEPENDENCE`` tab): a correlation between the legs'
  variance regimes, whose size each leg's own smile supplies, and a
  volatility of the correlation.  Unmarked, the copula is the Gaussian one
  above, to the last digit.

The size of both is bounded from below by the diagnostic in
``reconstruction_error``: run each *leg* through the same grid on its own and
see how far its risk reversal and butterfly come back from where they started.
A cross difference smaller than that is not evidence of anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.polynomial.hermite_e import hermegauss
from scipy.special import ndtr, ndtri

from . import black
from .black import DeltaConvention
from .numerics import ConvergenceError, solve_scalar

# Grid defaults.  ``SPAN`` is in units of the at-the-money total volatility, so
# the grid widens with the expiry rather than being fixed in strike terms.
SPAN = 6.0
NODES = 1601
COPULA_SPAN = 5.5
COPULA_NODES = 161
# The marked-dependence copula (:class:`Dependence`) sums a tensor grid per
# variance regime, so it runs on a coarser score grid, and its nodes are then
# binned onto one uniform grid of log returns so every price read off it costs
# the same whatever the regimes.  At these sizes it reproduces the Gaussian
# copula's RR and fly to a thousandth of a vol point (a test pins it), and five
# regime nodes per leg converge the 10-delta fly to about the same.
DEPENDENCE_NODES = 121
REGIME_NODES = 5
DEPENDENCE_BINS = 4001


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


@dataclass(frozen=True)
class Dependence:
    """How two legs depend on each other beyond one correlation.  Both marked.

    ``vol_vol`` is the correlation between the two legs' **variance
    regimes**: each leg's volatility is lognormally uncertain, and these are
    the correlation of the two log-variance shocks.  How uncertain each leg's
    volatility is is not a second input -- it is what that leg's own smile
    already says, read off its distribution as excess kurtosis
    (:func:`regime_dispersion`) -- so the only thing marked is whether the two
    legs' calm and stressed states come together.  At ``+1`` they always do,
    which is where a risk-off cross's butterfly lives; at ``0`` they never do.
    ``None`` is no regimes at all, which is the Gaussian copula.

    **The Gaussian copula is not ``vol_vol = 0``.**  It ties the size of the
    two legs' moves together as roughly the square of the correlation, and
    independent regimes tie them less than that, so a zero here marks a
    butterfly *below* today's.  The triangle backs out the vol-vol correlation
    the marked cross implies for exactly this reason: that is the number to
    mark against, not zero.

    ``corr_vol`` is the volatility of the correlation itself, in correlation
    units: over the life of the option the correlation is ``rho - corr_vol``
    or ``rho + corr_vol`` with even odds -- the smallest law with that mean and
    that standard deviation.  Both ends must lie in ``[-1, 1]``.
    """

    vol_vol: float | None = None
    corr_vol: float = 0.0

    def __post_init__(self) -> None:
        if self.vol_vol is not None:
            v = float(self.vol_vol)
            if not math.isfinite(v) or not -1.0 <= v <= 1.0:
                raise ValueError(f"a vol-vol correlation must lie in [-1, 1], got {self.vol_vol!r}")
        c = float(self.corr_vol)
        if not math.isfinite(c) or not 0.0 <= c < 1.0:
            raise ValueError(f"a correlation vol must lie in [0, 1), got {self.corr_vol!r}")

    @property
    def active(self) -> bool:
        """Whether this is anything but the Gaussian copula."""
        return self.vol_vol is not None or self.corr_vol > 0.0


def regime_dispersion(excess_kurtosis: float) -> float:
    """The variance of a leg's log-variance regime its own smile implies.

    A normal variance mixture with a lognormal variance of log-variance ``s2``
    has excess kurtosis ``3 (exp(s2) - 1)``, so ``s2 = log(1 + kurtosis / 3)``.
    A smile with no excess kurtosis has no regimes to correlate.
    """
    k = float(excess_kurtosis)
    if not math.isfinite(k) or k <= 0.0:
        return 0.0
    return math.log1p(k / 3.0)


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
    #: The marked dependence this law was built with (:class:`Dependence`);
    #: ``None`` and ``0`` for the Gaussian copula.
    vol_vol: float | None = None
    corr_vol: float = 0.0
    #: Each leg's excess kurtosis, which sized its variance regimes; NaN where
    #: no regimes were built.
    leg_kurtosis: tuple[float, float] = (float("nan"), float("nan"))

    def atm_vol(self) -> tuple[float, float]:
        """This law's own at-the-money volatility and its strike, ``K/F``.

        One pass of the delta-neutral strike is enough: it moves the ATM by
        far less than the grid error, and iterating it would hide that.
        """
        atm_k = black.atm_strike(1.0, self.implied_vol(1.0), self.t, self.conv)
        return self.implied_vol(atm_k), atm_k

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
        atm, atm_k = self.atm_vol()
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

    def market_strangle(self, delta: float, atm: float | None = None) -> float:
        """The market strangle this smile prices, in vol over ``atm``.

        What a pair sheet's ``ST`` column holds, and not the smile strangle
        :meth:`table` reports as ``fly``: the one volatility over the ATM at
        which a strangle struck at its own ``delta`` strikes costs what this
        distribution charges for those strikes.  The same solve as
        ``VolSurface.strangle``, with the combined law's prices in place of a
        slice's -- so a number written into the sheet off this is read back by
        the fit as the smile it came from.

        ``atm`` defaults to this law's own at-the-money (:meth:`table`'s): the
        strangle is a shape measured from the smile it belongs to.
        """
        if atm is None:
            atm = self.atm_vol()[0]
        t, d = self.t, abs(delta)

        def premium_gap(s: float) -> float:
            v = atm + s
            if v <= 0:
                return 1e6
            kc = black.strike_from_delta(d, 1.0, v, t, True, self.conv)
            kp = black.strike_from_delta(-d, 1.0, v, t, False, self.conv)
            flat = float(black.price(1.0, kc, v, t, True) + black.price(1.0, kp, v, t, False))
            put = float(self.call(kp)) - (1.0 - kp)          # parity, per unit of forward
            return float(self.call(kc)) + put - flat

        return solve_scalar(premium_gap, 0.0, lo_bound=-atm * 0.9,
                            bracket=(-atm * 0.5, atm * 2.0),
                            what=f"{d:.2f} delta market strangle on the combined smile")


def combine(dist_a: Distribution, dist_b: Distribution, coefficients: tuple[int, int],
            rho: float, conv: DeltaConvention | bool = False, *,
            nodes: int = COPULA_NODES, span: float = COPULA_SPAN,
            dependence: Dependence | None = None) -> Combined:
    """Tie two leg distributions together with a Gaussian copula.

    The tensor grid is in the *normal scores* of the two legs, not in their
    returns, so each leg keeps exactly the marginal its own smile implies and
    the correlation enters only through the dependence.  The grid is uniform
    with Simpson weights rather than Gauss-Hermite: the option payoff has a
    kink, and a quadrature tuned for smooth integrands converges badly across
    it.

    A marked ``dependence`` that is active builds the regime copula of
    :func:`_combine_dependent` instead, at ``rho`` as given -- which moves the
    combined at-the-money as well as the wings.  :func:`combine_holding_atm`
    is the caller that holds it; this one does as it is told.  An absent or
    inactive one is this function exactly as it always was.
    """
    if not -1.0 <= rho <= 1.0:
        raise ValueError(f"correlation must lie in [-1, 1], got {rho!r}")
    if abs(dist_a.t - dist_b.t) > 1e-9:
        raise ValueError(
            f"the two legs are at different expiries ({dist_a.t:.6f}y and {dist_b.t:.6f}y); "
            f"a triangle only holds at a common maturity"
        )
    if dependence is not None and dependence.active:
        return _combine_dependent(dist_a, dist_b, coefficients, rho, conv, dependence,
                                  nodes=DEPENDENCE_NODES if nodes == COPULA_NODES else nodes,
                                  span=span)
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
    conv = DeltaConvention.of(conv)
    return Combined(xc=xc, weight=weight, t=dist_a.t, rho=float(rho),
                    coefficients=(ca, cb), conv=conv, clamped=clamped,
                    shift=shift, convexity=convexity, warnings=tuple(warnings))


def _score_table(dist: Distribution, scales: np.ndarray, probs: np.ndarray, reach: float,
                 points: int = 4001):
    """A leg's log return as a function of its scaled score ``sqrt(W) Z``, tabulated.

    The scaled score's own distribution function for the discrete ``W``, then
    the leg's quantile at it -- composed once per law, so a regime copula pays
    one interpolation per node rather than a sum over every regime and a
    quantile search.  Also the scores beyond which the leg's grid is exhausted,
    for the clamped-mass warning.
    """
    y = np.linspace(-reach, reach, points)
    u = (probs[None, :] * ndtr(y[:, None] / scales[None, :])).sum(axis=1)
    c = np.maximum.accumulate(dist.cdf)
    u = np.maximum.accumulate(u)
    limits = (float(np.interp(c[0], u, y)), float(np.interp(c[-1], u, y)))
    return y, dist.quantile(u), limits


def _combine_dependent(dist_a: Distribution, dist_b: Distribution,
                       coefficients: tuple[int, int], rho: float,
                       conv: DeltaConvention | bool, dependence: Dependence, *,
                       nodes: int = DEPENDENCE_NODES, span: float = COPULA_SPAN,
                       regime_nodes: int = REGIME_NODES,
                       bins: int = DEPENDENCE_BINS) -> Combined:
    """The copula with the two things a Gaussian one leaves out, both marked.

    **Variance regimes.**  Each leg's normal score is scaled by the square root
    of a variance regime ``W = exp(s e)``, normalised to mean one, where
    ``s**2`` is that leg's :func:`regime_dispersion` -- its own smile's excess
    kurtosis -- and the two shocks ``e`` are standard normals with correlation
    ``vol_vol``.  The scaled score is mapped back to a probability through the
    scaled score's *own* law (:func:`_mixture_cdf`), so each leg still takes
    exactly the marginal its smile implies and the regimes change only how the
    two move together.  The shocks are integrated by Gauss-Hermite, which is
    right here where it is wrong for the payoff: ``exp(s e)`` is smooth.

    **Correlation vol.**  The score correlation is ``rho - corr_vol`` or
    ``rho + corr_vol``, each with probability one half.

    Every (correlation, regime) pair is a tensor grid of its own, exactly as
    :func:`combine` builds one, and the whole set is binned onto one uniform
    grid of log returns by cloud-in-cell -- which keeps every node's mass and
    mean exactly -- so a price read off the result costs what one off the
    Gaussian copula does.  Deterministic: nothing is simulated.
    """
    ca, cb = coefficients
    s = float(dependence.corr_vol)
    ends = (float(rho),) if s == 0.0 else (float(rho) - s, float(rho) + s)
    for r in ends:
        if not -1.0 - 1e-12 <= r <= 1.0 + 1e-12:
            raise ValueError(
                f"a correlation vol of {s:.4g} around a correlation of {rho:.4g} reaches "
                f"{r:.4g}, outside [-1, 1]")
    ends = tuple(min(max(r, -1.0), 1.0) for r in ends)

    kurt = (dist_a.moments().excess_kurtosis, dist_b.moments().excess_kurtosis)
    lam = dependence.vol_vol
    warnings: list[str] = []
    if lam is None:
        disp = (0.0, 0.0)
    else:
        disp = (regime_dispersion(kurt[0]), regime_dispersion(kurt[1]))
        if disp == (0.0, 0.0):
            warnings.append(
                f"neither leg's smile carries any excess kurtosis at this expiry "
                f"({kurt[0]:+.3g}, {kurt[1]:+.3g}), so there are no variance regimes for the "
                f"vol-vol correlation of {lam:+.3g} to correlate and it moves nothing")

    if disp == (0.0, 0.0):
        w1 = w2 = np.ones(1)
        probs = np.ones(1)
    else:
        m = max(int(regime_nodes), 1)
        e, gw = hermegauss(m)
        gw = gw / float(np.sum(gw))
        e1 = np.repeat(e, m)
        e2 = lam * e1 + math.sqrt(max(1.0 - lam * lam, 0.0)) * np.tile(e, m)
        probs = np.repeat(gw, m) * np.tile(gw, m)
        w1 = np.exp(math.sqrt(disp[0]) * e1)
        w2 = np.exp(math.sqrt(disp[1]) * e2)
        w1 = w1 / float(np.sum(probs * w1))
        w2 = w2 / float(np.sum(probs * w2))
    root1, root2 = np.sqrt(w1), np.sqrt(w2)
    single = probs.size == 1

    n = int(nodes) | 1
    z = np.linspace(-span, span, n)
    h = float(z[1] - z[0])
    sw = _simpson_weights(n, h) * np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    grid_w = sw[:, None] * sw[None, :]
    reach = span * math.sqrt(2.0)
    if single:
        # No regimes: the scaled score is the score, and its law is exact.
        c_a, c_b = np.maximum.accumulate(dist_a.cdf), np.maximum.accumulate(dist_b.cdf)
        tab_a = tab_b = None
        lim_a = (float(ndtri(max(c_a[0], 1e-300))), float(ndtri(min(c_a[-1], 1.0 - 1e-16))))
        lim_b = (float(ndtri(max(c_b[0], 1e-300))), float(ndtri(min(c_b[-1], 1.0 - 1e-16))))
    else:
        y_a, x_a, lim_a = _score_table(dist_a, root1, probs, reach * float(root1.max()))
        y_b, x_b, lim_b = _score_table(dist_b, root2, probs, reach * float(root2.max()))
        tab_a, tab_b = (y_a, x_a), (y_b, x_b)

    xs: list[np.ndarray] = []
    ws: list[np.ndarray] = []
    total = sum_a = sum_b = sum_ab = out_a = out_b = 0.0
    leg_a_cache: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    for r in ends:
        z2 = r * z[:, None] + math.sqrt(max(1.0 - r * r, 0.0)) * z[None, :]
        for k in range(probs.size):
            key = float(root1[k])
            if key not in leg_a_cache:
                y1 = key * z
                xa = dist_a.quantile(ndtr(y1)) if single else np.interp(y1, *tab_a)
                leg_a_cache[key] = (y1, xa)
            y1, xa = leg_a_cache[key]
            y2 = float(root2[k]) * z2
            xb = dist_b.quantile(ndtr(y2)) if single else np.interp(y2, *tab_b)
            wk = grid_w * (float(probs[k]) / len(ends))
            xs.append((ca * xa[:, None] + cb * xb).ravel())
            ws.append(wk.ravel())
            row_w = wk.sum(axis=1)
            total += float(row_w.sum())
            sum_a += float(np.sum(row_w * xa))
            sum_b += float(np.sum(wk * xb))
            sum_ab += float(np.sum(wk * xa[:, None] * xb))
            out_a += float(np.sum(row_w[(y1 < lim_a[0]) | (y1 > lim_a[1])]))
            out_b += float(np.sum(wk[(y2 < lim_b[0]) | (y2 > lim_b[1])]))

    xc = np.concatenate(xs)
    weight = np.concatenate(ws)
    weight = weight / float(np.sum(weight))     # the tails outside the grid are re-spread
    lo, hi = float(xc.min()), float(xc.max())
    nb = max(int(bins), 3)
    grid = np.linspace(lo, hi, nb)
    step = (hi - lo) / (nb - 1) if hi > lo else 1.0
    pos = (xc - lo) / step
    i0 = np.minimum(pos.astype(np.int64), nb - 2)
    frac = pos - i0
    mass = np.bincount(i0, weight * (1.0 - frac), nb) + np.bincount(i0 + 1, weight * frac, nb)
    keep = mass > 0.0
    grid, mass = grid[keep], mass[keep]

    m_fwd = float(np.sum(mass * np.exp(grid)))
    if not math.isfinite(m_fwd) or m_fwd <= 0:
        raise ValueError("the combined distribution does not have a finite forward")
    shift = math.log(m_fwd)
    grid = grid - shift

    mean_a, mean_b = sum_a / total, sum_b / total
    cov = sum_ab / total - mean_a * mean_b
    convexity = (math.log(max(dist_a.mgf(ca), 1e-300))
                 + math.log(max(dist_b.mgf(cb), 1e-300))
                 + ca * cb * cov)
    clamped = max(out_a, out_b) / total
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
    return Combined(xc=grid, weight=mass, t=dist_a.t, rho=float(rho),
                    coefficients=(ca, cb), conv=DeltaConvention.of(conv), clamped=clamped,
                    shift=shift, convexity=convexity, warnings=tuple(warnings),
                    vol_vol=None if lam is None else float(lam), corr_vol=s,
                    leg_kurtosis=(float(kurt[0]), float(kurt[1])))


def combine_holding_atm(dist_a: Distribution, dist_b: Distribution,
                        coefficients: tuple[int, int], rho: float,
                        conv: DeltaConvention | bool, dependence: Dependence | None, *,
                        target_atm: float | None = None, rho_start: float | None = None,
                        **grid) -> Combined:
    """The cross at a marked dependence, with its at-the-money where it was.

    The dependence is a statement about the *shape* of the cross -- how often
    its legs' big days coincide -- and it is not allowed to move the level:
    the cross's ATM is its curve's, built from ``rho``.  So the at-the-money
    the Gaussian copula gives at ``rho`` is the target (``target_atm``, when
    the caller already has it), and the copula's own correlation is solved,
    bracketed, until the marked dependence gives the same one.  The law that
    comes back carries that correlation as ``rho``; an inactive dependence is
    the Gaussian copula at ``rho`` unchanged.  ``rho_start`` is where the
    search begins, when a neighbouring solve already knows better than ``rho``.
    """
    if dependence is None or not dependence.active:
        return combine(dist_a, dist_b, coefficients, rho, conv)
    if target_atm is None:
        target_atm = combine(dist_a, dist_b, coefficients, rho, conv).atm_vol()[0]
    s = float(dependence.corr_vol)
    lo_bound, hi_bound = -1.0 + s, 1.0 - s
    if lo_bound > hi_bound:
        raise ValueError(f"a correlation vol of {s:.4g} leaves no correlation inside [-1, 1]")
    seen: dict[float, tuple[Combined, float]] = {}

    def gap(r: float) -> float:
        if r not in seen:
            law = _combine_dependent(dist_a, dist_b, coefficients, r, conv, dependence, **grid)
            miss = law.atm_vol()[0] - target_atm
            # Within a millionth of volatility -- a ten-thousandth of a vol
            # point -- is on target, and saying so stops Brent there instead
            # of bisecting a bracket already narrower than anything shown.
            seen[r] = (law, 0.0 if abs(miss) < 1e-6 else miss)
        return seen[r][1]

    start = min(max(float(rho if rho_start is None else rho_start), lo_bound), hi_bound)
    # The bracket is aimed rather than guessed: the variance triangle says how
    # far a unit of correlation moves the cross's ATM, ``ca cb sd_a sd_b /
    # (sigma t)``, so one evaluation at the start predicts the root, and the
    # bracket runs from the start to a little past the prediction.  Brent then
    # polishes inside it; a prediction that misses is widened by the solver's
    # own bracket search, within the bounds.
    ca, cb = coefficients
    slope = (ca * cb * dist_a.moments().sd * dist_b.moments().sd
             / max(target_atm * dist_a.t, 1e-12))
    g0 = gap(start)
    step = -g0 / slope if slope != 0.0 else 0.0
    step = math.copysign(max(abs(step) * 1.25, 1e-4), step if step != 0.0 else 1.0)
    far = min(max(start + step, lo_bound), hi_bound)
    what = (f"the correlation holding the combined ATM at {target_atm:.4%} under a vol-vol "
            f"correlation of {dependence.vol_vol} and a correlation vol of {s:g}")
    try:
        root = solve_scalar(gap, start, lo_bound=lo_bound, hi_bound=hi_bound, xtol=2e-5,
                            bracket=(min(start, far), max(start, far)), what=what)
    except (ConvergenceError, ValueError) as exc:
        raise ConvergenceError(
            f"no copula correlation in [{lo_bound:+.3f}, {hi_bound:+.3f}] gives the combined "
            f"at-the-money of {target_atm:.4%} under this dependence: {exc}") from None
    gap(root)
    return seen[root][0]


#: The coarser grid :func:`implied_vol_vol` searches on.  The answer is read to
#: a hundredth of a correlation, and at this size the 25-delta fly is within a
#: thousandth of a vol point of the full grid's.
IMPLIED_GRID = {"nodes": 81, "bins": 2001}


def implied_vol_vol(dist_a: Distribution, dist_b: Distribution,
                    coefficients: tuple[int, int], rho: float,
                    conv: DeltaConvention | bool, target_fly: float, *,
                    corr_vol: float = 0.0, delta: float = 0.25,
                    target_atm: float | None = None) -> tuple[float | None, str]:
    """The vol-vol correlation at which the legs give the marked cross butterfly.

    What :class:`Dependence` is marked against, the way the implied
    correlation is what a cross's ATM is marked against.  The ATM is held
    (:func:`combine_holding_atm`) at every trial, so the answer moves the
    butterfly and nothing else; ``corr_vol`` is held at what is marked.

    ``(value, "")``, or ``(None, reason)`` when no vol-vol correlation in
    ``[-1, 1]`` reaches the marked butterfly -- the reason says how far the
    legs do reach, which is the useful half of that answer.
    """
    tag = f"fly{int(round(delta * 100))}"
    if target_atm is None:
        target_atm = combine(dist_a, dist_b, coefficients, rho, conv).atm_vol()[0]

    held: dict[float, float] = {}

    def fly(lam: float) -> float:
        # Each trial starts from the correlation the nearest one held at.
        near = min(held, key=lambda k: abs(k - lam)) if held else None
        law = combine_holding_atm(dist_a, dist_b, coefficients, rho, conv,
                                  Dependence(vol_vol=lam, corr_vol=corr_vol),
                                  target_atm=target_atm,
                                  rho_start=None if near is None else held[near], **IMPLIED_GRID)
        held[lam] = law.rho
        return float(law.table((delta,))[tag])

    try:
        at = {lam: fly(lam) - target_fly for lam in (-1.0, 0.0, 1.0)}
    except (ValueError, ArithmeticError, ConvergenceError) as exc:
        return None, f"the vol-vol correlation could not be searched: {exc}"
    if at[0.0] == 0.0:
        return 0.0, ""
    for lo, hi in ((0.0, 1.0), (-1.0, 0.0)):
        if at[lo] * at[hi] <= 0.0:
            try:
                return float(solve_scalar(lambda x: fly(x) - target_fly, 0.5 * (lo + hi),
                                          bracket=(lo, hi), lo_bound=lo, hi_bound=hi,
                                          xtol=5e-3, what="the implied vol-vol correlation")), ""
            except (ValueError, ArithmeticError, ConvergenceError) as exc:
                return None, f"the vol-vol correlation could not be solved: {exc}"
    reach = sorted(v + target_fly for v in at.values())
    why = ("so the rest is correlation vol, or something the legs do not carry"
           if target_fly > reach[-1] else
           "so the marked butterfly is thinner than any variance regimes make it -- the "
           "Gaussian copula, which has none, is the nearer description")
    return None, (
        f"no vol-vol correlation in [-1, 1] gives the marked {int(round(delta * 100))}-delta "
        f"butterfly of {target_fly:.3%}: the legs give between {reach[0]:.3%} and "
        f"{reach[-1]:.3%} over that range, {why}")


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
