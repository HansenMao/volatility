"""The band as a *process*, not just a terminal distribution.

``banded.py`` prices a pegged pair from a Beta body on the band plus a
hazard-rate break leg, and the Beta's **concentration is solved from the
at-the-money** -- one free parameter per expiry, fitted to the option market,
carrying no memory of where the spot actually sits inside the band today or of
how fast it has historically moved across it.  That is enough to price a smile
and not enough to have an opinion about one.

This module supplies the opinion.  The spot's position in the band,

    x = (S - L) / (U - L)   in [0, 1],

is modelled as a **Jacobi (Wright-Fisher) diffusion**::

    dx = kappa (m - x) dt + sigma sqrt(x (1 - x)) dW

which is the natural target-zone process and not an arbitrary choice:

* Its support is **exactly the band**.  No reflection, no absorption, no
  boundary condition bolted on -- the diffusion coefficient vanishes at both
  edges on its own, which is Krugman's smooth pasting in one line: the closer
  the rate gets to a defended edge, the less it moves, because the market
  knows what is waiting there.
* Its stationary distribution is **exactly Beta(a, b)**, the family
  ``banded.py`` already prices with, at

      a = m s,   b = (1 - m) s,   with the concentration   s = 2 kappa / sigma^2.

  So this is a *prior on an existing parameter*, not a second model: what came
  out of the at-the-money can now be compared with what the spot series says,
  in the same units.
* It is **U-shaped exactly when the edges dominate** -- ``a < 1`` and ``b < 1``,
  which is ``2 kappa / sigma^2`` small: weak pull to the middle against the
  diffusion.  A lognormal or a logit-normal cannot represent edge-seeking at
  all, and a truncated normal (which is what a *reflected* OU gives) is
  bell-shaped whatever its parameters.  That is why the process is Jacobi and
  not reflected-OU, though the literature usually reaches for the latter.

Both conditional moments are closed form, which is what makes this usable
rather than a simulation study.  With ``A = 2 kappa m + sigma^2`` and
``B = 2 kappa + sigma^2``::

    E[x_T | x_0]  = m + (x_0 - m) e^{-kappa T}
    E[x_T^2|x_0]  = x_0^2 e^{-BT} + (A m / B)(1 - e^{-BT})
                    + (A (x_0 - m) / (B - kappa)) (e^{-kappa T} - e^{-BT})

and matching those two to a Beta gives the body at **any horizon**, from a
point mass at today's position as ``T -> 0`` to the stationary Beta as
``T -> infinity``.  That is the term structure of the concentration, for free
and from one estimate, where ``banded`` refits it per expiry.

**What this module does not do.**  It does not re-mark anything and it is not
wired into the price.  The band model stays calibrated to the at-the-money,
because that is the quote and the quote is what a desk is asked to reproduce.
What this adds is the comparison the model could not make before: the
at-the-money the measured band dynamics *predict*, beside the one that is
marked.  Break risk is still a marked input (§6) and none of it is inferred
here -- the process above is the peg-intact regime only, which is precisely
the regime the Beta body describes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

#: A band position is held this far inside the edges before any arithmetic
#: that divides by ``x (1 - x)``.  The diffusion never reaches an edge, but a
#: *quoted spot* can sit on one -- the Convertibility Undertaking is a price
#: somebody deals at -- and an estimate is not the place to discover that.
EDGE_EPS = 1e-6

#: Fewer steps than this and the drift is not measured, it is a difference of
#: two numbers.  A quarter of daily observations, which is also about the
#: shortest window over which a peg's regime can be called stable.
MIN_STEPS = 60

#: A fitted centre this close to an edge is reported as a caveat.  It is not
#: impossible -- a peg held against its strong side by carry estimates exactly
#: this way -- but it makes the stationary Beta strongly asymmetric on the
#: strength of a window that may never have visited the rest of the band.
EDGE_CENTRE = 0.15


@dataclass(frozen=True)
class TargetZone:
    """A Jacobi target-zone process for one pair's position inside its band.

    ``kappa`` is the pull toward ``m`` in units of 1/year, ``sigma`` the
    diffusion parameter of the *position* (not of the rate), and ``m`` the
    long-run position the band is pulled toward -- 0.5 is the middle, and a
    peg whose carry holds it against one edge estimates well away from it.
    """

    pair: str
    kappa: float
    sigma: float
    m: float = 0.5
    #: One standard error on ``kappa``, from the curvature of the profile sum
    #: of squares.  It is reported because it is **large**: the speed of mean
    #: reversion is the hardest parameter in this family to pin down, and a
    #: decade of daily data still leaves it loose.  ``sigma`` is estimated far
    #: more sharply from the same series, which is the usual asymmetry and the
    #: reason the concentration is quoted with a range rather than as a point.
    kappa_stderr: float = 0.0
    observations: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        if self.kappa < 0:
            raise ValueError(f"{self.pair}: kappa must not be negative, got {self.kappa!r}")
        if self.sigma <= 0:
            raise ValueError(f"{self.pair}: sigma must be positive, got {self.sigma!r}")
        if not 0.0 < self.m < 1.0:
            raise ValueError(f"{self.pair}: m must lie strictly inside the band, got {self.m!r}")

    # -- the stationary end -------------------------------------------------

    @property
    def concentration(self) -> float:
        """``2 kappa / sigma^2``: the Beta concentration the process settles at."""
        return 2.0 * self.kappa / (self.sigma * self.sigma)

    def stationary(self) -> tuple[float, float]:
        """The long-run ``(a, b)``.  What the band looks like given long enough."""
        s = self.concentration
        return self.m * s, (1.0 - self.m) * s

    def concentration_range(self, sds: float = 1.0) -> tuple[float, float]:
        """The concentration within ``sds`` standard errors of ``kappa``.

        The concentration is ``2 kappa / sigma^2`` and is linear in ``kappa``,
        so the interval carries straight through.  Quoting it is the point:
        the marked concentration lands inside this range far more often than a
        point estimate suggests, and a comparison that ignored the width would
        read a disagreement into ordinary sampling noise.
        """
        lo = max(self.kappa - sds * self.kappa_stderr, 0.0)
        hi = self.kappa + sds * self.kappa_stderr
        s2 = self.sigma * self.sigma
        return 2.0 * lo / s2, 2.0 * hi / s2

    @property
    def u_shaped(self) -> bool:
        """Whether the process seeks the edges rather than the middle.

        ``a < 1 and b < 1``, the same test ``banded.BetaBandSmile`` applies to
        its fitted body -- so the two can be compared without translation.
        """
        a, b = self.stationary()
        return a < 1.0 and b < 1.0

    @property
    def half_life(self) -> float:
        """Years for a displacement from ``m`` to decay by half.  ``inf`` at zero pull."""
        return math.inf if self.kappa <= 0 else math.log(2.0) / self.kappa

    # -- any horizon --------------------------------------------------------

    def moments(self, x0: float, t: float) -> tuple[float, float]:
        """``(mean, variance)`` of the band position at ``t``, from ``x0`` today.

        Closed form, from the module docstring.  At ``t = 0`` this is the point
        ``(x0, 0)`` and as ``t`` grows it approaches the stationary Beta's own
        mean and variance -- so one estimate gives every horizon and the
        concentration stops being refitted per expiry.
        """
        x0 = min(max(float(x0), EDGE_EPS), 1.0 - EDGE_EPS)
        t = float(t)
        if t <= 0:
            return x0, 0.0
        k, s2, m = self.kappa, self.sigma * self.sigma, self.m
        A, B = 2.0 * k * m + s2, 2.0 * k + s2
        ek, eB = math.exp(-k * t), math.exp(-B * t)
        mean = m + (x0 - m) * ek
        second = x0 * x0 * eB + (A * m / B) * (1.0 - eB)
        if B - k > 0:
            second += A * (x0 - m) / (B - k) * (ek - eB)
        return mean, max(second - mean * mean, 0.0)

    def beta_at(self, x0: float, t: float) -> tuple[float, float]:
        """The Beta ``(a, b)`` matching the process's mean and variance at ``t``.

        Moment matching rather than the exact transition density, which has a
        spectral expansion in Jacobi polynomials and no closed form for the
        partial moments the pricer needs.  The match is exact in both limits
        that matter -- a point mass now, the true stationary Beta later -- and
        the family is the one ``banded`` prices with either way, so the cost
        is confined to the middle of the term structure.
        """
        mean, var = self.moments(x0, t)
        room = mean * (1.0 - mean)
        if var <= 0 or var >= room:
            # No Beta has this much spread at this mean; the informative
            # answer is the closest one the family holds.
            s = 1e6 if var <= 0 else EDGE_EPS
            return mean * s, (1.0 - mean) * s
        s = room / var - 1.0
        return mean * s, (1.0 - mean) * s

    def concentration_at(self, x0: float, t: float) -> float:
        """``a + b`` at ``t``: the one number ``banded._BodyFit.build`` wants.

        The body it builds re-centres itself on the forward, so only the
        *spread* of this estimate is handed over -- the mean is the option
        market's business, through the risk-neutral constraint, and not this
        module's.
        """
        a, b = self.beta_at(x0, t)
        return a + b

    def describe(self) -> str:
        a, b = self.stationary()
        life = "no pull" if not math.isfinite(self.half_life) else f"{self.half_life:.2f}y half-life"
        lo, hi = self.concentration_range()
        span = "wide open" if not math.isfinite(hi) else f"{lo:.3f}-{hi:.3f}"
        return (f"kappa {self.kappa:.3f}+/-{self.kappa_stderr:.3f}/yr ({life}), sigma "
                f"{self.sigma:.3f}, centre {self.m:.3f}; stationary Beta({a:.3f}, {b:.3f}), "
                f"{'U-shaped' if self.u_shaped else 'bell'}, "
                f"concentration {self.concentration:.3f} ({span} at one standard error)")


def _conditional_variance(x0, h, kappa: float, m: float, sigma2: float):
    """``Var(x_{t+h} | x_t)`` from the exact moments, vectorised over steps."""
    A, B = 2.0 * kappa * m + sigma2, 2.0 * kappa + sigma2
    ek, eB = np.exp(-kappa * h), np.exp(-B * h)
    mean = m + (x0 - m) * ek
    second = x0 * x0 * eB + (A * m / B) * (1.0 - eB)
    if B - kappa > 0:
        second = second + A * (x0 - m) / (B - kappa) * (ek - eB)
    return np.maximum(second - mean * mean, 0.0)


def estimate(pair: str, positions, times, *, source: str = "") -> TargetZone:
    """Fit the process to a series of band positions.

    ``times`` is the year fraction of each observation, so irregular spacing
    -- which every daily series has across a weekend -- is carried rather than
    assumed away.

    The fit uses the **exact conditional moments**, not an Euler step, and
    that is not a refinement:

        E[x_{t+h} | x_t] = m + (x_t - m) e^{-kappa h}

    is exact for this diffusion at every spacing, so the drift is estimated
    with no discretisation bias at all.  For a fixed ``kappa`` the centre
    ``m`` is linear in that expression, so the fit is a one-dimensional search
    over ``kappa`` with ``m`` solved in closed form at each point -- cheap,
    and it cannot wander off to a second optimum.  ``sigma`` then comes from
    matching the mean squared residual to the **exact** conditional variance,
    solved for the one unknown left.

    An Euler fit was tried first and is the reason this is written out: the
    step's noise is ``sigma^2 x(1-x) dt``, which vanishes at the edges, so the
    generalised-least-squares weights ``1/(x(1-x)dt)`` explode exactly where a
    defended peg spends most of its life.  On a U-shaped series -- the case
    this module exists for -- a handful of near-edge observations took over
    the regression and ``kappa`` came back anywhere from half to twice its
    true value.  It recovered bell-shaped parameters perfectly well, which is
    what made it look convincing.
    """
    from scipy.optimize import minimize_scalar

    from .numerics import ConvergenceError, solve_scalar

    x = np.asarray(positions, dtype=float)
    ts = np.asarray(times, dtype=float)
    if x.size != ts.size:
        raise ValueError(f"{pair}: {x.size} positions against {ts.size} times")
    good = np.isfinite(x) & np.isfinite(ts)
    x, ts = x[good], ts[good]
    order = np.argsort(ts)
    x, ts = x[order], ts[order]
    h = np.diff(ts)
    keep = h > 0
    x0, x1, h = x[:-1][keep], x[1:][keep], h[keep]
    if h.size < MIN_STEPS:
        raise ValueError(
            f"{pair}: {h.size} usable step(s) is too few to estimate a target zone; "
            f"{MIN_STEPS} is the floor, below which the drift is a difference of two "
            f"numbers rather than a measurement")
    x0 = np.clip(x0, EDGE_EPS, 1.0 - EDGE_EPS)

    def fit_m(kappa: float):
        """``m`` and the residuals at this ``kappa``; linear, so closed form."""
        decay = np.exp(-kappa * h)
        c = 1.0 - decay                      # the coefficient on m
        y = x1 - x0 * decay
        denom = float(np.dot(c, c))
        m = float(np.dot(c, y) / denom) if denom > 0 else float(np.mean(x0))
        return m, y - c * m

    def ssr(kappa: float) -> float:
        return float(np.sum(fit_m(kappa)[1] ** 2))

    # The bracket is a half-life from a fortnight to a working lifetime.  A
    # peg's band position is not a fast process and a kappa outside this is a
    # statement about the sampling, not about the policy.
    best = minimize_scalar(ssr, bounds=(1e-3, 60.0), method="bounded",
                           options={"xatol": 1e-6})
    kappa = float(best.x)
    m, resid = fit_m(kappa)
    m = min(max(m, EDGE_EPS), 1.0 - EDGE_EPS)
    if not math.isfinite(kappa) or kappa <= 1e-3 + 1e-9:
        raise ValueError(
            f"{pair}: the fitted pull toward the centre came out at the bottom of its bracket "
            f"({kappa:.4g}/yr) over {h.size} steps, so this series shows no mean reversion "
            f"inside the band. There is no stationary Beta to compare a marked concentration "
            f"against")

    target = float(np.mean(resid * resid))

    def gap(sigma2: float) -> float:
        return float(np.mean(_conditional_variance(x0, h, kappa, m, sigma2))) - target

    try:
        sigma2 = solve_scalar(gap, 1.0, lo_bound=1e-9, bracket=(1e-9, 50.0),
                              what="target-zone sigma^2")
    except ConvergenceError as exc:
        raise ValueError(
            f"{pair}: no diffusion parameter reproduces the observed step variance of "
            f"{target:.4g} at kappa {kappa:.4g}/yr ({exc})") from None
    # One standard error on kappa from the curvature of the profile SSR --
    # the ordinary non-linear least squares asymptotic, ``2 s^2 / H``.  A step
    # scaled to kappa keeps the difference well conditioned at both ends of
    # the bracket.
    step = max(kappa * 1e-3, 1e-6)
    curvature = (ssr(kappa + step) - 2.0 * best.fun + ssr(kappa - step)) / (step * step)
    dof = max(h.size - 2, 1)
    stderr = (math.sqrt(2.0 * (best.fun / dof) / curvature)
              if curvature > 0 and math.isfinite(curvature) else math.inf)
    return TargetZone(pair=pair, kappa=kappa, sigma=math.sqrt(sigma2), m=m,
                      kappa_stderr=float(stderr), observations=int(h.size), source=source)


def positions_from_history(hist, band, *, days: float | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """Band positions and their year fractions off a historical sheet.

    Returns the positions, the times, and a note of what had to be done to
    them -- how many observations sat **outside** the band, which is not a
    detail: a spot outside its own policy range means the band is stale or the
    sheet is somebody else's pair, and silently clipping it would turn either
    into a confident estimate.
    """
    from .timeutil import DAYS_IN_YEAR

    spot = np.asarray(getattr(hist, "spot", np.empty(0)), dtype=float)
    dates = list(getattr(hist, "dates", ()) or ())
    note = {"observations": 0, "outside": 0, "from": "", "to": "", "days": 0.0}
    if spot.size == 0 or not dates:
        raise ValueError(f"{band.pair}: the historical sheet has no spot column, so there are "
                         f"no band positions to estimate from")
    n = min(spot.size, len(dates))
    spot, dates = spot[:n], dates[:n]
    good = np.isfinite(spot)
    spot, dates = spot[good], [d for d, g in zip(dates, good) if g]
    if days:
        cutoff = dates[-1] - timedelta(days=float(days))
        keep = [i for i, d in enumerate(dates) if d >= cutoff]
        spot, dates = spot[keep], [dates[i] for i in keep]
    if len(dates) < 2:
        raise ValueError(f"{band.pair}: fewer than two dated spot observations")

    pos = (spot - band.lower) / band.width
    note["outside"] = int(np.count_nonzero((pos < 0.0) | (pos > 1.0)))
    pos = np.clip(pos, EDGE_EPS, 1.0 - EDGE_EPS)
    origin = dates[0]
    times = np.array([(d - origin).days / DAYS_IN_YEAR for d in dates], dtype=float)
    note.update(observations=len(dates), to=dates[-1].isoformat(),
                days=float(times[-1] - times[0]))
    note["from"] = dates[0].isoformat()
    return pos, times, note


def from_history(hist, band, *, days: float | None = None) -> tuple[TargetZone, dict]:
    """Estimate the target zone for a pair straight off its historical sheet."""
    pos, times, note = positions_from_history(hist, band, days=days)
    zone = estimate(band.pair, pos, times,
                    source=f"{note['observations']} spot observations "
                           f"{note['from']} to {note['to']}")
    return zone, note


def dynamics_panel(book, pair: str, hist, tenors=None, *, cut: str = "NY",
                   days: float | None = None) -> dict:
    """The at-the-money the measured band dynamics predict, beside the marked one.

    This is what the module is for.  ``banded`` solves the Beta concentration
    from the quoted at-the-money, one per expiry; here the concentration is
    **estimated from the spot series** and the at-the-money becomes the
    prediction.  The two are then in the same units and can disagree, which
    they could not before.

    Only the *spread* of the estimate is handed to the pricer.  The mean stays
    the option market's, through ``_BodyFit.build``'s risk-neutral
    re-centring: the forward is a price and this module has no business
    overriding it.  The break specification is likewise the marked one (§6) --
    the process above describes the peg-intact regime and says nothing about
    the peg failing.

    Nothing here is marked.  It is the spot series' opinion, reported.
    """
    from .banded import _BodyFit

    if pair not in book:
        raise ValueError(f"{pair} is not built in this book")
    surface = book[pair]
    band = getattr(surface, "band", None)
    out = {"pair": pair, "cut": cut, "has_band": band is not None,
           "zone": None, "describe": "", "note": {}, "rows": [], "message": "",
           "warnings": []}
    if band is None:
        out["message"] = (f"{pair} has no managed band, so there is no band position to model; "
                          f"bands are policy and live on the PEG_BANDS tab")
        return out
    effective = surface.band_treatment.effective_band(band)
    spec = surface.band_treatment.jump
    if hist is None:
        out["message"] = (f"no historical sheet for {pair}, and the band dynamics are estimated "
                          f"from the spot series; load a history workbook "
                          f"(volkit serve --history ...)")
        return out
    try:
        zone, note = from_history(hist, effective, days=days)
    except ValueError as exc:
        out["message"] = str(exc)
        return out
    out["note"] = note
    out["describe"] = zone.describe()
    lo, hi = zone.concentration_range()
    out["zone"] = {
        "kappa": zone.kappa, "kappa_stderr": zone.kappa_stderr, "sigma": zone.sigma,
        "m": zone.m, "concentration": zone.concentration, "half_life": zone.half_life,
        "u_shaped": zone.u_shaped, "observations": zone.observations,
        "stationary_a": zone.stationary()[0], "stationary_b": zone.stationary()[1],
        "concentration_lo": lo, "concentration_hi": hi if math.isfinite(hi) else None,
    }
    if note.get("outside"):
        out["warnings"].append(
            f"{pair}: {note['outside']} of {note['observations']} historical spots sat outside "
            f"[{effective.lower:g}, {effective.upper:g}] and were held at the edge. Either the "
            f"band moved over this window or the sheet is not this policy's; an estimate over a "
            f"range that was not the one in force is a confident number about the wrong regime")
    if not math.isfinite(zone.kappa_stderr) or zone.kappa_stderr >= zone.kappa:
        out["warnings"].append(
            f"{pair}: the pull toward the centre is {zone.kappa:.3f}/yr against a standard "
            f"error of {zone.kappa_stderr:.3f}, so it is not distinguishable from no pull at "
            f"all over this window. The concentration below is the point estimate of a "
            f"parameter this series cannot resolve; read the range beside it, not the number. "
            f"Mean-reversion speed is the hardest parameter in this family to measure and a "
            f"longer window is the only fix")
    if min(zone.m, 1.0 - zone.m) < EDGE_CENTRE:
        near = "lower" if zone.m < 0.5 else "upper"
        out["warnings"].append(
            f"{pair}: the fitted centre is {zone.m:.3f}, within {EDGE_CENTRE:g} of the {near} "
            f"edge, which says the series was pinned there rather than reverting inside the "
            f"band. The stationary Beta is then strongly asymmetric on the strength of a window "
            f"that may simply not have visited the rest of the range")
    if not zone.u_shaped:
        out["warnings"].append(
            f"{pair}: the estimated process is bell-shaped (Beta({zone.stationary()[0]:.3f}, "
            f"{zone.stationary()[1]:.3f})), so over this window the spot did not seek the edges. "
            f"That is a finding about the window, not a failure: a band defended rarely enough "
            f"looks like an ordinary mean-reverting rate from inside")

    for tenor in list(tenors or getattr(surface.atm, "tenor_points", ()) or ()):
        row = {"tenor": tenor, "t": None, "position": None, "marked_atm": None,
               "marked_concentration": None, "dynamics_concentration": None,
               "dynamics_atm": None, "difference": None, "message": ""}
        try:
            t = book.tenor_years(pair, tenor)
            row["t"] = t
            expiry = surface.clock.datetime_from_years(t)
            level = book.market_level_for(pair, expiry)
            if not level["feed"] or level["forward"] is None or level["spot"] is None:
                raise ValueError("no spot / forward feed, so the band cannot be placed")
            spot, forward = float(level["spot"]), float(level["forward"])
            x0 = effective.position(spot)
            row["position"] = x0
            atm = float(surface.atm_vol(expiry, cut))
            row["marked_atm"] = atm
            fit = _BodyFit(effective, forward, t, atm, surface.conv, tenor)
            marked = fit.fit(spec)
            row["marked_concentration"] = marked.a + marked.b
            conc = zone.concentration_at(x0, t)
            row["dynamics_concentration"] = conc
            predicted = float(fit.bound_vol(conc, spec))
            if predicted != predicted:
                raise ValueError("the predicted at-the-money could not be inverted to a "
                                 "volatility at this concentration")
            row["dynamics_atm"] = predicted
            row["difference"] = atm - predicted
        except Exception as exc:  # noqa: BLE001 - one tenor, not the panel
            row["message"] = f"{type(exc).__name__}: {exc}"
        out["rows"].append(row)
    return out
