"""Black-76 pricing, greeks and the FX delta conventions.

Two changes matter here relative to the legacy ``common_functions``:

1.  Strike-from-delta is *closed form* for the unadjusted convention, and the
    delta-neutral-straddle strike is closed form for both conventions.  The
    legacy code solved all four numerically with ``fsolve`` from a guess of
    1.0.
2.  The premium-adjusted call delta is not monotone in strike -- it rises,
    peaks, then falls back to zero.  The legacy solver could therefore land on
    the wrong branch, silently returning a strike below the forward for an
    out-of-the-money call.  The peak is located explicitly and the solve is
    confined to the decreasing branch, which is the market convention.

Everything is expressed on the forward: pass F and K in the same units, or
pass F = 1 and K as a strike/forward ratio.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass

import numpy as np
from scipy.special import log_ndtr, ndtr, ndtri

from .numerics import ConvergenceError, solve_scalar

SQRT_2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class DeltaConvention:
    """Which delta the quotes are expressed in.

    ``premium_adjusted`` is the convention used when the option premium is
    paid in the foreign currency, i.e. for most USD-base pairs such as
    USDJPY and USDCNH.  It matches the legacy ``delta_adjust`` flag, which
    ``Vols`` set from ``ccy[0:3] == 'USD'``.
    """

    premium_adjusted: bool = False

    def __bool__(self) -> bool:  # keeps legacy truthiness working
        return self.premium_adjusted

    @classmethod
    def for_pair(cls, pair: str) -> "DeltaConvention":
        return cls(premium_adjusted=pair[:3].upper() == "USD")


# Beyond this total volatility the lognormal formulae stop being meaningful and
# exp() overflows.  A calibration search can wander here; it should get a clear
# error rather than an OverflowError from deep inside math.exp.
MAX_TOTAL_VOL = 20.0


def _check(vol: float, t: float) -> float:
    """Validate and return the total volatility ``sigma * sqrt(t)``."""
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got t={t!r}")
    if vol <= 0 or not math.isfinite(vol):
        raise ValueError(f"volatility must be positive and finite, got vol={vol!r}")
    sqt = vol * math.sqrt(t)
    if sqt > MAX_TOTAL_VOL:
        raise ValueError(
            f"total volatility sigma*sqrt(t) = {sqt:.4g} exceeds {MAX_TOTAL_VOL}; "
            f"vol={vol:.4g} at t={t:.4g}y is outside the range the lognormal "
            f"formulae can represent"
        )
    return sqt


def d1_d2(F, K, vol, t):
    """The Black d1/d2 pair.  Vectorised over K."""
    sqt = vol * np.sqrt(t)
    d1 = (np.log(np.asarray(F, dtype=float) / np.asarray(K, dtype=float)) + 0.5 * vol * vol * t) / sqt
    return d1, d1 - sqt


def price(F, K, vol, t, is_call: bool, *, foreign_premium: bool = False):
    """Undiscounted Black-76 price.  ``foreign_premium`` divides by F."""
    d1, d2 = d1_d2(F, K, vol, t)
    if is_call:
        pv = ndtr(d1) * F - ndtr(d2) * K
    else:
        pv = ndtr(-d2) * K - ndtr(-d1) * F
    return pv / F if foreign_premium else pv


def digital_price(F, K, vol, t, is_call: bool, *, foreign_premium: bool = False):
    """Undiscounted cash-or-nothing digital paying 1 unit of domestic."""
    d1, d2 = d1_d2(F, K, vol, t)
    d = d1 if foreign_premium else d2
    return ndtr(d) if is_call else 1.0 - ndtr(d)


def vega(F, K, vol, t):
    """Vega per unit of volatility (not per vol point)."""
    d1, _ = d1_d2(F, K, vol, t)
    return F * np.exp(-0.5 * d1 * d1) * math.sqrt(t) / SQRT_2PI


def gamma(F, K, vol, t):
    d1, _ = d1_d2(F, K, vol, t)
    return np.exp(-0.5 * d1 * d1) / (F * vol * math.sqrt(t) * SQRT_2PI)


def delta(F, K, vol, t, is_call: bool, conv: DeltaConvention | bool = False):
    """Forward delta under the requested convention.

    Unadjusted: ``N(d1)`` for a call, ``N(d1) - 1`` for a put.
    Premium adjusted: the same scaled by ``K / F`` and evaluated at ``d2``.
    """
    pa = bool(conv)
    d1, d2 = d1_d2(F, K, vol, t)
    if pa:
        scale = np.asarray(K, dtype=float) / F
        nd = ndtr(d2)
    else:
        scale = 1.0
        nd = ndtr(d1)
    return scale * (nd if is_call else nd - 1.0)


def dns_strike(F: float, vol: float, t: float, conv: DeltaConvention | bool = False) -> float:
    """Delta-neutral straddle strike, in closed form.

    Setting call delta plus put delta to zero gives ``d1 = 0`` unadjusted and
    ``d2 = 0`` premium adjusted, hence ``F exp(+/- sigma^2 t / 2)``.  The
    legacy ``getDNStrike`` solved this with ``fsolve``.
    """
    v2t = _check(vol, t) ** 2
    return F * math.exp(-0.5 * v2t if bool(conv) else 0.5 * v2t)


@functools.lru_cache(maxsize=8192)
def _pa_peak_d2(sqt: float) -> float:
    """``d2`` at the premium-adjusted call delta peak, as a function of sigma*sqrt(t) alone.

    Cached because a calibration inverts the delta thousands of times at
    nearly the same total volatility, and this root find was the single
    hottest call in the profile.
    """
    log_sqt = math.log(sqt)

    def stationarity(d2: float) -> float:
        # Solved as log(n/N) - log(sigma sqrt(t)).  In levels both n(d2) and
        # N(d2) underflow to zero for d2 around -40, which manufactures a
        # spurious root; the log form of the inverse Mills ratio stays
        # accurate deep into the tail.
        return float(-0.5 * d2 * d2 - math.log(SQRT_2PI) - log_ndtr(d2)) - log_sqt

    return solve_scalar(stationarity, 0.0, bracket=(-40.0, 40.0),
                        what="premium-adjusted delta peak")


def _pa_call_delta_peak(F: float, vol: float, t: float) -> tuple[float, float]:
    """Strike at which the premium-adjusted call delta peaks, and that delta.

    The stationarity condition ``n(d2) = sigma sqrt(t) N(d2)`` involves only
    the total volatility, so the peak location is a one-dimensional function
    of ``sigma sqrt(t)`` and can be cached.
    """
    sqt = _check(vol, t)
    d2_star = _pa_peak_d2(round(sqt, 12))
    k_star = F * math.exp(-d2_star * sqt - 0.5 * sqt * sqt)
    return k_star, float(delta(F, k_star, vol, t, True, True))


def strike_from_delta(
    target_delta: float,
    F: float,
    vol: float,
    t: float,
    is_call: bool,
    conv: DeltaConvention | bool = False,
) -> float:
    """Invert the delta for a strike.

    ``target_delta`` is signed: positive for calls, negative for puts.
    """
    sqt = _check(vol, t)
    if is_call and not 0.0 < target_delta < 1.0:
        raise ValueError(f"call delta must lie in (0, 1), got {target_delta!r}")
    if not is_call and not -1.0 < target_delta < 0.0:
        raise ValueError(f"put delta must lie in (-1, 0), got {target_delta!r}")

    if not bool(conv):
        # Closed form: invert N(d1) directly.
        d1 = ndtri(target_delta if is_call else 1.0 + target_delta)
        return F * math.exp(0.5 * sqt * sqt - d1 * sqt)

    if not is_call:
        # Premium-adjusted put delta is strictly decreasing in K over (0, inf),
        # so a plain expanding bracket is safe.
        f = lambda k: float(delta(F, k, vol, t, False, True)) - target_delta
        return solve_scalar(
            f, F, lo_bound=1e-12, hi_bound=F * 1e6,
            bracket=(F * 1e-6, F * 10.0), what="premium-adjusted put strike",
        )

    # Premium-adjusted call delta peaks then decays; the convention takes the
    # root on the decreasing branch, above the peak strike.
    k_star, max_delta = _pa_call_delta_peak(F, vol, t)
    if target_delta >= max_delta:
        raise ConvergenceError(
            f"a {target_delta:.4f} premium-adjusted call delta does not exist at "
            f"vol={vol:.4%}, t={t:.4f}y: the delta peaks at {max_delta:.4f} "
            f"(strike/forward {k_star / F:.4f}). Quote a lower delta or check the vol."
        )
    f = lambda k: float(delta(F, k, vol, t, True, True)) - target_delta
    return solve_scalar(
        f, k_star * 1.5, lo_bound=k_star, hi_bound=F * 1e6,
        bracket=(k_star, F * 20.0), what="premium-adjusted call strike",
    )


def implied_vol(target_price: float, F: float, K: float, t: float, is_call: bool,
                *, lo: float = 1e-6, hi: float = 5.0) -> float:
    """Invert Black-76 for volatility, bracketed on [lo, hi]."""
    intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
    if target_price < intrinsic - 1e-12:
        raise ValueError(
            f"price {target_price:.6g} is below intrinsic {intrinsic:.6g}; no implied vol exists"
        )
    f = lambda v: float(price(F, K, v, t, is_call)) - target_price
    return solve_scalar(f, 0.1, bracket=(lo, hi), what="implied volatility")
