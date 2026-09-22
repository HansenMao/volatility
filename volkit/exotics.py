"""Digitals, one-touches and their overhedge buffers.

Everything is undiscounted, in line with the rest of the package: this model
carries no rate curve, so a "price" is a forward value and a touch payout is
valued as its probability under the drift implied by the forward.

Two payout families:

* **European digitals** pay at expiry if the spot is beyond the strike.  They
  are priced by the call-spread that actually hedges them, over a *ramp*: a
  digital replicated with a narrow ramp is closer to the theoretical value but
  needs a bigger gamma position at the strike, so the ramp is the overhedge
  knob and it changes both price and risk.
* **One-touch / no-touch** pay on the barrier ever being hit.  The flat-barrier
  case has a closed form.  The overhedge shifts the barrier, either in
  parallel ("extend") or with a time-dependent taper ("bend"); a taper is no
  longer a flat barrier and has no closed form, so it is priced by Monte Carlo
  with a Brownian-bridge touch correction, which is unbiased for continuous
  monitoring and reports its own standard error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtr

from . import black

TOUCH_MODES = ("none", "extend", "bend_front", "bend_back")


def implied_drift(spot: float, forward: float, t: float) -> float:
    """Log drift ``mu`` such that ``E[S_T] = F``."""
    if spot <= 0 or forward <= 0:
        raise ValueError(f"spot and forward must be positive, got {spot!r}, {forward!r}")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    return math.log(forward / spot) / t


def touch_probability(spot: float, barrier: float, vol: float, t: float,
                      drift: float, *, is_up: bool | None = None) -> float:
    """Probability of touching a flat barrier before ``t`` (continuous monitoring).

    Uses the reflection result for Brownian motion with drift: for
    ``X_s = lambda s + W_s`` and a level ``a > 0``,
    ``P(max X >= a) = N((lambda T - a)/sqrt(T)) + exp(2 lambda a) N((-a - lambda T)/sqrt(T))``.
    """
    if vol <= 0:
        raise ValueError(f"volatility must be positive, got {vol!r}")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if spot <= 0 or barrier <= 0:
        raise ValueError(f"spot and barrier must be positive, got {spot!r}, {barrier!r}")
    if is_up is None:
        is_up = barrier > spot
    # Already touched.
    if (is_up and barrier <= spot) or (not is_up and barrier >= spot):
        return 1.0

    nu = drift - 0.5 * vol * vol          # drift of log S
    lam = nu / vol
    if is_up:
        a = math.log(barrier / spot) / vol
    else:
        a = math.log(spot / barrier) / vol
        lam = -lam
    sq = math.sqrt(t)
    term1 = ndtr((lam * t - a) / sq)
    # exp(2 lam a) can overflow for a strongly drifting, far barrier; the
    # second term is a probability, so cap the exponent instead of inf * 0.
    expo = 2.0 * lam * a
    if expo > 700.0:
        term2 = math.exp(700.0) * float(ndtr((-a - lam * t) / sq))
    else:
        term2 = math.exp(expo) * float(ndtr((-a - lam * t) / sq))
    return float(min(max(term1 + term2, 0.0), 1.0))


def _barrier_profile(barrier: float, spot: float, mode: str, buffer_pct: float,
                     conservative: bool, taus: np.ndarray, t: float) -> np.ndarray:
    """The (possibly time-dependent) barrier level at each time in ``taus``.

    ``conservative`` shifts the barrier *toward* spot, which makes a touch more
    likely -- the direction a seller of a one-touch wants to be wrong in.
    """
    is_up = barrier > spot
    sign = -1.0 if (is_up == conservative) else 1.0   # toward spot when conservative
    frac = buffer_pct / 100.0
    if mode == "none" or frac == 0.0:
        shape = np.zeros_like(taus)
    elif mode == "extend":
        shape = np.ones_like(taus)
    elif mode == "bend_front":
        # Full shift at inception, tapering to nothing at expiry.
        shape = 1.0 - taus / t
    elif mode == "bend_back":
        # Nothing at inception, full shift by expiry.
        shape = taus / t
    else:
        raise ValueError(f"unknown overhedge mode {mode!r}; expected one of {TOUCH_MODES}")
    return barrier * (1.0 + sign * frac * shape)


@dataclass
class TouchResult:
    price: float
    probability: float
    barrier_used: float
    method: str
    std_error: float = 0.0
    unhedged_price: float = 0.0
    overhedge_cost: float = 0.0


def one_touch(spot: float, barrier: float, vol: float, t: float, forward: float,
              *, is_no_touch: bool = False, mode: str = "none",
              buffer_pct: float = 0.0, conservative: bool = True,
              paths: int = 60_000, steps: int = 128, seed: int = 12345) -> TouchResult:
    """Price a one-touch (or no-touch) with an optional overhedge buffer.

    ``mode='none'`` and ``mode='extend'`` keep the barrier flat, so the closed
    form applies.  The bends make the barrier time-dependent, which has no
    closed form; those are simulated.
    """
    if mode not in TOUCH_MODES:
        raise ValueError(f"unknown overhedge mode {mode!r}; expected one of {TOUCH_MODES}")
    drift = implied_drift(spot, forward, t)
    is_up = barrier > spot

    base_p = touch_probability(spot, barrier, vol, t, drift, is_up=is_up)
    base_price = (1.0 - base_p) if is_no_touch else base_p

    if mode in ("none", "extend") or buffer_pct == 0.0:
        shifted = float(_barrier_profile(barrier, spot, mode, buffer_pct,
                                         conservative, np.array([0.0]), t)[0])
        p = touch_probability(spot, shifted, vol, t, drift, is_up=is_up)
        price = (1.0 - p) if is_no_touch else p
        return TouchResult(price=price, probability=p, barrier_used=shifted,
                           method="analytic", unhedged_price=base_price,
                           overhedge_cost=price - base_price)

    p, se = _touch_mc(spot, barrier, vol, t, drift, is_up, mode, buffer_pct,
                      conservative, paths, steps, seed)
    price = (1.0 - p) if is_no_touch else p
    profile = _barrier_profile(barrier, spot, mode, buffer_pct, conservative,
                               np.linspace(0.0, t, 3), t)
    return TouchResult(price=price, probability=p, barrier_used=float(np.mean(profile)),
                       method=f"monte carlo ({paths:,} paths x {steps} steps, Brownian bridge)",
                       std_error=se, unhedged_price=base_price,
                       overhedge_cost=price - base_price)


def _touch_mc(spot, barrier, vol, t, drift, is_up, mode, buffer_pct, conservative,
              paths, steps, seed) -> tuple[float, float]:
    """Touch probability against a time-dependent barrier.

    Simulating the log-spot on a grid and checking only the grid points would
    systematically miss touches that happen between them.  The Brownian-bridge
    correction adds, for each step, the exact probability that the path crossed
    the barrier within the step given its endpoints, which removes that bias.
    """
    rng = np.random.default_rng(seed)
    dt = t / steps
    nu = drift - 0.5 * vol * vol
    taus = np.linspace(0.0, t, steps + 1)
    levels = _barrier_profile(barrier, spot, mode, buffer_pct, conservative, taus, t)
    log_b = np.log(levels)

    x = np.full(paths, math.log(spot))
    alive = np.ones(paths, dtype=bool)
    survive_prob = np.ones(paths)
    sqdt = math.sqrt(dt)
    for i in range(steps):
        z = rng.standard_normal(paths)
        x_next = x + nu * dt + vol * sqdt * z
        b0, b1 = log_b[i], log_b[i + 1]
        # Discrete hit at either endpoint.
        if is_up:
            hit = (x_next >= b1) | (x >= b0)
        else:
            hit = (x_next <= b1) | (x <= b0)
        # Brownian-bridge probability of crossing inside the step, using the
        # mid-step barrier level for the (slightly) sloped barrier.
        bm = 0.5 * (b0 + b1)
        with np.errstate(over="ignore", invalid="ignore"):
            expo = -2.0 * (bm - x) * (bm - x_next) / (vol * vol * dt)
            p_cross = np.exp(np.minimum(expo, 0.0))
        if is_up:
            valid = (x < bm) & (x_next < bm)
        else:
            valid = (x > bm) & (x_next > bm)
        p_cross = np.where(valid, p_cross, 0.0)
        survive_prob = np.where(alive, survive_prob * (1.0 - p_cross), survive_prob)
        survive_prob = np.where(hit & alive, 0.0, survive_prob)
        alive &= ~hit
        x = x_next
    touched = 1.0 - survive_prob
    return float(np.mean(touched)), float(np.std(touched, ddof=1) / math.sqrt(paths))


@dataclass
class DigitalResult:
    price: float
    fair_value: float          # smile-consistent digital, the ramp -> 0 limit
    flat_vol_price: float      # N(d2) at the strike vol, ignoring skew
    skew_adjustment: float     # fair_value - flat_vol_price
    ramp: float
    strikes: tuple[float, float]
    vols: tuple[float, float]
    notional_ratio: float
    overhedge_cost: float


def _call_spread(forward, k_lo, k_hi, t, vol_fn):
    """Value of a unit call spread paying 1 above ``k_hi``, ramped from ``k_lo``."""
    width = k_hi - k_lo
    p_lo = float(black.price(forward, k_lo, vol_fn(k_lo), t, True))
    p_hi = float(black.price(forward, k_hi, vol_fn(k_hi), t, True))
    return (p_lo - p_hi) / width


def european_digital(spot: float, strike: float, t: float, forward: float,
                     vol_fn, *, is_call: bool = True, ramp_pct: float = 0.0,
                     conservative: bool = True) -> DigitalResult:
    """A digital priced as the call spread that actually replicates it.

    ``vol_fn(K)`` returns the smile volatility for an absolute strike, so each
    leg is priced on its own vol.  That matters: the fair value of a digital is
    ``-dC/dK`` *through the smile*, which is ``N(d2) - vega * dsigma/dK``, not
    ``N(d2)`` at a single vol.  Reporting the flat-vol number as the benchmark
    would make the skew term look like an overhedge cost -- and with an upward
    skew it would even make a narrow ramp look cheaper than fair.

    ``ramp_pct`` is the spread width as a percentage of the strike.  Zero is the
    unhedgeable limit; anything wider is an instrument you can actually run, and
    is priced against the seller when ``conservative`` is set.
    """
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike!r}")
    if ramp_pct < 0:
        raise ValueError(f"ramp must not be negative, got {ramp_pct!r}")

    strike_vol = vol_fn(strike)
    flat = float(black.digital_price(forward, strike, strike_vol, t, is_call))
    # The ramp -> 0 limit, taken as a tight centred spread: this is the smile
    # derivative, evaluated with the same machinery that prices the real ramp.
    eps = strike * 1e-4
    tight = _call_spread(forward, strike - eps, strike + eps, t, vol_fn)
    fair = tight if is_call else 1.0 - tight

    if ramp_pct == 0.0:
        return DigitalResult(fair, fair, flat, fair - flat, 0.0, (strike, strike),
                             (strike_vol, strike_vol), 0.0, 0.0)

    width = strike * ramp_pct / 100.0
    # The ramp is placed on the side that makes the seller pay: the payout
    # starts earlier, so the spread costs more than the digital it replaces.
    if is_call:
        k_lo = strike - width if conservative else strike
        k_hi = k_lo + width
    else:
        k_hi = strike + width if conservative else strike
        k_lo = k_hi - width
    spread = _call_spread(forward, k_lo, k_hi, t, vol_fn)
    price = spread if is_call else 1.0 - spread
    return DigitalResult(price=price, fair_value=fair, flat_vol_price=flat,
                         skew_adjustment=fair - flat, ramp=ramp_pct,
                         strikes=(k_lo, k_hi), vols=(vol_fn(k_lo), vol_fn(k_hi)),
                         notional_ratio=1.0 / width, overhedge_cost=price - fair)


@dataclass
class BandTouchResult(TouchResult):
    """A touch under the regime mixture, split by the regime that caused it."""

    #: Probability the barrier was hit with the peg still intact.
    intact: float = 0.0
    #: Probability it was hit only because the peg broke -- the whole of the
    #: answer for a barrier outside the band, and the part a lognormal price
    #: gets wrong for one inside it.
    broken: float = 0.0
    #: Probability the peg broke at all over the life, ``1 - exp(-hazard t)``.
    break_probability: float = 0.0
    lognormal_probability: float = 0.0
    reachable_in_band: bool = True


def band_touch(spot: float, barrier: float, band, t: float, forward: float,
               zone, jump, *, is_no_touch: bool = False, paths: int = 40_000,
               steps: int = 128, seed: int = 12345) -> BandTouchResult:
    """Touch probability for a pair whose spot lives inside a defended band.

    Everything else in this module prices a touch off a lognormal, which on a
    pegged pair is wrong in both directions at once.  Inside the band it
    **overstates** the chance of reaching a level, because a lognormal has no
    idea the edges are defended and its diffusion does not die as it
    approaches one.  Outside the band it prices a touch that can only happen
    if the peg breaks *as though it were ordinary diffusion*, which is the
    same error the smile module exists to correct (§6) -- and a double
    no-touch struck on the Convertibility Undertakings is exactly that trade.

    So the path is the regime mixture's, not a lognormal's:

    * while the peg holds, the band position follows the Jacobi target-zone
      diffusion (``targetzone.py``), whose volatility vanishes at both edges;
    * the peg breaks at a Poisson time with the **marked** hazard, weak or
      strong side by the marked share, and from there the pair is an ordinary
      lognormal at the marked post-break volatility, started at the jump
      level.

    Monte Carlo, because a first passage under a state-dependent diffusion has
    no closed form.  The Brownian-bridge correction that removes discrete-
    monitoring bias is applied with the diffusion coefficient **frozen at each
    step's start**: exact for the lognormal leg as it is elsewhere in this
    module, and an approximation for the Jacobi leg, where the coefficient
    moves within the step.  It errs toward *under*-counting touches near an
    edge, where the true coefficient is falling -- the conservative direction
    for a no-touch seller and the reported one either way.
    """
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if spot <= 0 or barrier <= 0:
        raise ValueError(f"spot and barrier must be positive, got {spot!r}, {barrier!r}")
    width = band.upper - band.lower
    x0 = (spot - band.lower) / width
    xb = (barrier - band.lower) / width
    is_up = barrier > spot
    hazard = float(getattr(jump, "hazard", 0.0))
    p_break = 1.0 - math.exp(-hazard * t)

    rng = np.random.default_rng(seed)
    dt = t / steps
    sqdt = math.sqrt(dt)
    kappa, sigma, m = float(zone.kappa), float(zone.sigma), float(zone.m)

    # When the peg breaks, and on which side.
    tau = (rng.exponential(1.0 / hazard, paths) if hazard > 0
           else np.full(paths, math.inf))
    weak = rng.random(paths) < float(getattr(jump, "weak_share", 1.0))
    broke = tau <= t

    x = np.full(paths, min(max(x0, 1e-9), 1.0 - 1e-9))
    survive = np.ones(paths)          # probability of NOT having touched
    hit_intact = np.zeros(paths, dtype=bool)
    reachable = 0.0 < xb < 1.0

    for i in range(steps):
        t0, t1 = i * dt, (i + 1) * dt
        live = (tau > t0)             # the peg is still intact over this step
        if not live.any():
            break
        vol_x = sigma * np.sqrt(np.clip(x * (1.0 - x), 0.0, None))
        x_next = x + kappa * (m - x) * dt + vol_x * sqdt * rng.standard_normal(paths)
        x_next = np.clip(x_next, 1e-12, 1.0 - 1e-12)
        if reachable:
            hit = (x_next >= xb) | (x >= xb) if is_up else (x_next <= xb) | (x <= xb)
            # Brownian bridge with the diffusion frozen at the step's start.
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                expo = -2.0 * (xb - x) * (xb - x_next) / np.maximum(vol_x ** 2 * dt, 1e-300)
                p_cross = np.exp(np.minimum(expo, 0.0))
            valid = (x < xb) & (x_next < xb) if is_up else (x > xb) & (x_next > xb)
            p_cross = np.where(valid & live, p_cross, 0.0)
            survive = np.where(live, survive * (1.0 - p_cross), survive)
            survive = np.where(hit & live, 0.0, survive)
            hit_intact |= hit & live
        x = np.where(live, x_next, x)

    # The peg-intact leg's touch probability, path by path.
    touched_intact = np.where(hit_intact, 1.0, 1.0 - survive)
    # A path whose peg broke can only have touched *before* the break through
    # the leg above; after it, the lognormal leg answers.
    intact_only = np.where(broke, 0.0, touched_intact)

    # The break leg: a lognormal from the jump level over the time that is
    # left, priced with the closed form rather than simulated -- it is an
    # ordinary flat-barrier touch once the jump has happened.
    weak_jump = float(getattr(jump, "weak_jump", 0.0))
    strong_jump = float(getattr(jump, "strong_jump", 0.0))
    weak_vol = float(getattr(jump, "weak_vol", 0.10))
    strong_vol = float(getattr(jump, "strong_vol", 0.10))
    after = np.zeros(paths)
    idx = np.flatnonzero(broke)
    for j in idx:
        level = forward * (math.exp(weak_jump) if weak[j] else math.exp(-strong_jump))
        left = t - float(tau[j])
        if left <= 0:
            after[j] = 1.0 if ((level >= barrier) if is_up else (level <= barrier)) else 0.0
            continue
        vol = weak_vol if weak[j] else strong_vol
        if (level >= barrier) if is_up else (level <= barrier):
            after[j] = 1.0          # the jump itself carried it through
            continue
        after[j] = touch_probability(level, barrier, vol, left, 0.0, is_up=is_up)
    # Before the break the path was still in the band, so it may have touched
    # there too; the two are combined as "either".
    before_break = np.where(broke, touched_intact, 0.0)
    combined = np.where(broke, 1.0 - (1.0 - before_break) * (1.0 - after), intact_only)

    p = float(np.mean(combined))
    se = float(np.std(combined, ddof=1) / math.sqrt(paths))
    price = (1.0 - p) if is_no_touch else p
    lognormal = touch_probability(spot, barrier, weak_vol, t,
                                  implied_drift(spot, forward, t), is_up=is_up)
    return BandTouchResult(
        price=price, probability=p, barrier_used=barrier,
        method=f"regime mixture, monte carlo ({paths:,} paths x {steps} steps, "
               f"Jacobi band + Poisson break)",
        std_error=se,
        unhedged_price=(1.0 - lognormal) if is_no_touch else lognormal,
        overhedge_cost=0.0,
        intact=float(np.mean(intact_only)),
        broken=float(np.mean(np.where(broke, combined, 0.0))),
        break_probability=float(np.mean(broke)),
        lognormal_probability=float(lognormal),
        reachable_in_band=reachable,
    )
