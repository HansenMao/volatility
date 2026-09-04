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
from dataclasses import dataclass, replace

import numpy as np
from scipy.special import log_ndtr, ndtr, ndtri

from .numerics import ConvergenceError, solve_scalar
from .timeutil import DAYS_IN_YEAR, tenor_to_years

SQRT_2PI = math.sqrt(2.0 * math.pi)


#: Where the at-the-money convention changes unless a pair says otherwise: up
#: to and including the 1Y pillar the ATM is the delta-neutral straddle,
#: beyond it the forward.
DEFAULT_ATMF_BEYOND = "1y"
_ATMF_NEVER = ("never", "none", "no", "dns", "-")
_ATMF_ALWAYS = ("always", "all", "atmf", "forward", "0")
#: A pillar's calendar expiry can sit a few days past its nominal length and
#: its cut a few hours past midnight; a day's grace keeps the boundary tenor
#: itself on the straddle side.
_ATM_BOUNDARY_GRACE = 1.0 / DAYS_IN_YEAR


def atmf_beyond_years(text) -> float:
    """Read an ``atmf beyond`` setting: a tenor, ``never``, ``always``, or blank.

    Blank is the market default.  The tenor is read nominally here -- ``1y``
    is one year -- and a surface re-reads it through its own calendar so that
    the boundary is the pillar's actual expiry (:meth:`DeltaConvention.resolved`).
    """
    word = str(text or DEFAULT_ATMF_BEYOND).strip().lower()
    if word in _ATMF_NEVER:
        return float("inf")
    if word in _ATMF_ALWAYS:
        return 0.0
    try:
        return float(tenor_to_years(word))
    except Exception:  # noqa: BLE001 -- any unreadable spelling is the one error
        raise ValueError(f"cannot read {text!r} as a tenor, 'never' or 'always'") from None


@dataclass(frozen=True)
class DeltaConvention:
    """The quoting conventions a pair's smile is expressed in.

    ``premium_adjusted`` says which delta the quotes are in.  The premium of
    an FX option is paid in one of the pair's two currencies, and when that is
    the **base** (foreign) currency the premium is itself a position in the
    underlying, so the delta the market quotes and hedges is net of it.  The
    market's rule is about the premium currency, not about which currency is
    written first: EURUSD, GBPUSD and AUDUSD pay premium in USD, the quote
    currency, and are unadjusted; USDJPY, USDCHF and USDCNH pay it in USD,
    now the base, and are adjusted; and the crosses -- EURJPY, EURGBP,
    AUDJPY, GBPNZD, EURCNH -- pay it in their base currency and are adjusted
    too.  The legacy ``delta_adjust`` flag, set from ``ccy[0:3] == 'USD'``,
    got every cross wrong.

    ``atmf_beyond`` says where the at-the-money strike stops being the
    delta-neutral straddle and becomes the forward: strictly beyond that
    tenor.  ``never`` is a pair that always uses the straddle, ``always`` one
    that always uses the forward.  ``atmf_beyond_years`` is the same thing as
    a number -- nominal until a surface resolves it on its calendar.

    ``spot_delta`` says which delta the quotes are in out to that same
    boundary: **spot** delta (the market's convention on the majors -- the
    hedge in spot, net of the foreign discount) or **forward** delta (the
    convention on most emerging-market and non-deliverable pairs).  Beyond
    the boundary every pair quotes forward delta.  Spot delta is forward
    delta times the foreign (base) currency's discount factor, and that
    factor is a fact about one tenor, so a pair's convention becomes a
    *slice's* convention through :meth:`at` -- which puts the discount factor
    for that tenor into ``df_foreign``, or leaves it at 1 with a note when
    the ``RATES`` tab has no rate for the currency.  ``df_foreign`` is what
    :func:`delta` and :func:`strike_from_delta` scale by, so a 25-delta quote
    lands on the strike the market meant.
    """

    premium_adjusted: bool = False
    atmf_beyond: str = DEFAULT_ATMF_BEYOND
    atmf_beyond_years: float = 1.0
    spot_delta: bool = True
    #: Set per slice by :meth:`at`: the foreign discount factor a spot delta
    #: is scaled by, 1.0 for a forward delta.
    df_foreign: float = 1.0
    #: Why the delta is what it is at this slice, for a screen.
    delta_note: str = ""

    def __bool__(self) -> bool:  # keeps legacy truthiness working
        return self.premium_adjusted

    def resolved(self, years_of) -> "DeltaConvention":
        """The same conventions with the boundary read through a calendar.

        ``years_of(tenor)`` is the surface's own ``tenor_years``: the boundary
        becomes the years to the boundary pillar's actual expiry, so the pillar
        is on the straddle side of the line on every valuation date.
        """
        word = str(self.atmf_beyond).strip().lower()
        if word in _ATMF_NEVER or word in _ATMF_ALWAYS:
            return self
        try:
            years = float(years_of(self.atmf_beyond))
        except Exception:  # noqa: BLE001 -- keep the nominal reading
            return self
        return replace(self, atmf_beyond_years=years)

    @staticmethod
    def default_premium_currency(pair: str) -> str:
        """The currency an option on ``pair`` pays its premium in, by convention.

        The dollar whenever it is in the pair, otherwise the base currency.
        """
        pair = pair.upper()
        base, quote = pair[:3], pair[3:6]
        return "USD" if "USD" in (base, quote) else base

    @classmethod
    def for_pair(cls, pair: str, premium_ccy: str | None = None,
                 atmf_beyond: str | None = None,
                 delta_type: str | None = None) -> "DeltaConvention":
        """The conventions for a pair: the market's defaults, or the desk's.

        ``delta_type`` is ``spot`` or ``forward``; blank is spot, the
        convention on the majors and their crosses.
        """
        pair = pair.upper()
        ccy = (premium_ccy or cls.default_premium_currency(pair)).upper()
        if ccy not in (pair[:3], pair[3:6]):
            raise ValueError(f"{pair}: the premium currency {ccy} is not one of the pair's")
        beyond = str(atmf_beyond or DEFAULT_ATMF_BEYOND).strip()
        kind = str(delta_type or "spot").strip().lower()
        if kind not in ("spot", "forward"):
            raise ValueError(f"{pair}: the delta type must be 'spot' or 'forward', "
                             f"not {delta_type!r}")
        return cls(premium_adjusted=ccy == pair[:3], atmf_beyond=beyond,
                   atmf_beyond_years=atmf_beyond_years(beyond), spot_delta=kind == "spot")

    def wants_spot_delta(self, t: float) -> bool:
        """Whether the quotes at ``t`` years are spot deltas by convention."""
        return self.spot_delta and not self.atm_is_forward(t)

    def at(self, t: float, df_foreign: float | None, foreign_ccy: str = "") -> "DeltaConvention":
        """This convention at one tenor, with the discount factor it needs.

        ``df_foreign`` is the base currency's discount factor to the option's
        settlement, or ``None`` when no rate is known.  Where the convention is
        forward delta -- by the pair's rule, or beyond the boundary -- the
        factor is not used and the slice reads forward delta; where it is spot
        delta and there is no rate, the slice reads forward delta *and says
        so*, because a delta quietly a point out is how a wing gets mismarked.
        """
        if not self.wants_spot_delta(t):
            return replace(self, df_foreign=1.0, delta_note="forward delta")
        if df_foreign is None or not 0.0 < df_foreign <= 1.5:
            who = f"no {foreign_ccy} rate" if foreign_ccy else "no rate"
            return replace(self, df_foreign=1.0,
                           delta_note=f"forward delta ({who} on the RATES tab)")
        return replace(self, df_foreign=float(df_foreign), delta_note="spot delta")

    @property
    def delta_is_spot(self) -> bool:
        """Whether this (slice) convention is actually reading spot delta."""
        return self.df_foreign != 1.0

    def delta_label(self) -> str:
        return self.delta_note or ("spot delta" if self.spot_delta else "forward delta")

    @classmethod
    def of(cls, conv) -> "DeltaConvention":
        """Coerce the legacy ``True``/``False`` flag; a convention passes through whole."""
        return conv if isinstance(conv, cls) else cls(premium_adjusted=bool(conv))

    def atm_is_forward(self, t: float) -> bool:
        """Whether the at-the-money strike at ``t`` years is the forward."""
        return t > self.atmf_beyond_years + _ATM_BOUNDARY_GRACE

    def atm_label(self, t: float) -> str:
        return "ATMF" if self.atm_is_forward(t) else "DNS"

    def describe(self) -> str:
        adj = "premium adjusted" if self.premium_adjusted else "unadjusted"
        kind = self.delta_label() if self.delta_note else (
            f"spot delta to {self.atmf_beyond.upper()}, forward beyond" if self.spot_delta
            and 0 < self.atmf_beyond_years < float("inf") else
            "spot delta" if self.spot_delta else "forward delta")
        if self.atmf_beyond_years == float("inf"):
            atm = "ATM = delta-neutral straddle at every tenor"
        elif self.atmf_beyond_years <= 0:
            atm = "ATM = forward at every tenor"
        else:
            atm = (f"ATM = delta-neutral straddle to {self.atmf_beyond.upper()}, "
                   f"forward beyond")
        return f"{adj} {kind}; {atm}"


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


def theta(F, K, vol, t):
    """Time decay per year of the *undiscounted* forward value.

    ``-F phi(d1) sigma / (2 sqrt(t))``, the derivative with respect to
    calendar time, so it is negative for a long option.  There is no discount
    curve anywhere in volkit (a stated limitation), so this is the decay of
    the forward premium and carries no interest term; that also makes it the
    same number for a call and a put, which the put/call parity of a forward
    value requires.
    """
    d1, _ = d1_d2(F, K, vol, t)
    return -F * np.exp(-0.5 * d1 * d1) * vol / (2.0 * math.sqrt(t) * SQRT_2PI)


def vanna(F, K, vol, t):
    """``d(delta)/d(vol)``, equivalently ``d(vega)/dF``: ``-phi(d1) d2 / sigma``.

    Unadjusted delta only -- the premium-adjusted conventions change delta by
    a factor of ``K / F`` that has its own volatility sensitivity, and no
    caller needs that.  Same for a call and a put.
    """
    d1, d2 = d1_d2(F, K, vol, t)
    return -np.exp(-0.5 * d1 * d1) * d2 / (vol * SQRT_2PI)


def volga(F, K, vol, t):
    """``d(vega)/d(vol)``: ``vega * d1 * d2 / sigma``.  Per unit of volatility."""
    d1, d2 = d1_d2(F, K, vol, t)
    return vega(F, K, vol, t) * d1 * d2 / vol


def delta(F, K, vol, t, is_call: bool, conv: DeltaConvention | bool = False):
    """Delta under the requested convention.

    Unadjusted: ``N(d1)`` for a call, ``N(d1) - 1`` for a put.
    Premium adjusted: the same scaled by ``K / F`` and evaluated at ``d2``.
    Both are forward deltas; a slice convention carrying a foreign discount
    factor (``conv.df_foreign``) turns them into spot deltas.
    """
    conv = DeltaConvention.of(conv)
    pa = conv.premium_adjusted
    d1, d2 = d1_d2(F, K, vol, t)
    if pa:
        scale = np.asarray(K, dtype=float) / F
        nd = ndtr(d2)
    else:
        scale = 1.0
        nd = ndtr(d1)
    return conv.df_foreign * scale * (nd if is_call else nd - 1.0)


def dns_strike(F: float, vol: float, t: float, conv: DeltaConvention | bool = False) -> float:
    """Delta-neutral straddle strike, in closed form.

    Setting call delta plus put delta to zero gives ``d1 = 0`` unadjusted and
    ``d2 = 0`` premium adjusted, hence ``F exp(+/- sigma^2 t / 2)``.  The
    legacy ``getDNStrike`` solved this with ``fsolve``.
    """
    v2t = _check(vol, t) ** 2
    return F * math.exp(-0.5 * v2t if bool(conv) else 0.5 * v2t)


def atm_strike(F: float, vol: float, t: float, conv: DeltaConvention | bool = False) -> float:
    """The strike the quoted at-the-money volatility belongs to.

    The delta-neutral straddle up to the pair's boundary and the forward
    beyond it (``DeltaConvention.atmf_beyond_years``).  This, not
    :func:`dns_strike`, is what every smile anchor and calibration reads,
    because an ATM vol read at the wrong strike moves the whole smile.
    """
    if DeltaConvention.of(conv).atm_is_forward(t):
        _check(vol, t)
        return float(F)
    return dns_strike(F, vol, t, conv)


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

    ``target_delta`` is signed: positive for calls, negative for puts, and in
    the convention's own delta: a spot-delta slice (``conv.df_foreign`` below
    one) is inverted as the forward delta it corresponds to.
    """
    sqt = _check(vol, t)
    if is_call and not 0.0 < target_delta < 1.0:
        raise ValueError(f"call delta must lie in (0, 1), got {target_delta!r}")
    if not is_call and not -1.0 < target_delta < 0.0:
        raise ValueError(f"put delta must lie in (-1, 0), got {target_delta!r}")
    conv = DeltaConvention.of(conv)
    target_delta = target_delta / conv.df_foreign
    if is_call and target_delta >= 1.0:
        raise ValueError(f"a {target_delta * conv.df_foreign:.4f} spot call delta is above the "
                         f"foreign discount factor {conv.df_foreign:.6f}: no such strike")
    if not is_call and target_delta <= -1.0:
        raise ValueError(f"a {target_delta * conv.df_foreign:.4f} spot put delta is beyond the "
                         f"foreign discount factor {conv.df_foreign:.6f}: no such strike")
    forward_conv = replace(conv, df_foreign=1.0)

    if not conv.premium_adjusted:
        # Closed form: invert N(d1) directly.
        d1 = ndtri(target_delta if is_call else 1.0 + target_delta)
        return F * math.exp(0.5 * sqt * sqt - d1 * sqt)

    if not is_call:
        # Premium-adjusted put delta is strictly decreasing in K over (0, inf),
        # so a plain expanding bracket is safe.
        f = lambda k: float(delta(F, k, vol, t, False, forward_conv)) - target_delta
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
    f = lambda k: float(delta(F, k, vol, t, True, forward_conv)) - target_delta
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
