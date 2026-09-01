"""Smiles for managed and pegged currencies, where the spot lives inside a band.

USDHKD is the clear case: under the Linked Exchange Rate System the HKMA's
Convertibility Undertakings hold the rate inside 7.75-7.85, and have done for
decades.  Every model in the rest of this package is lognormal, which gets two
things structurally wrong for such a pair.

* **Support.** A lognormal puts mass everywhere on (0, inf), so it prices
  options struck outside the band as if the peg could be anywhere.  Fitted to
  plausible USDHKD quotes, the SVI surface here puts 6.5% of the three-month
  distribution outside 7.75-7.85 and pays real premium for a 8.00 strike.
* **Shape.** The realised distribution is not merely bounded, it is *U-shaped*:
  because the HKMA intervenes at the edges, the rate spends most of its life
  near 7.75 or 7.85 rather than near 7.80.  A bell-shaped density fitted to a
  U-shaped reality systematically overstates the chance of a breakout.

The model is therefore a *regime mixture*, not a bounded distribution.  The peg
can break, that probability is real and belongs in the price; what a lognormal
gets wrong is not that the probability is positive but that it is enormous and
has the wrong shape.  So:

    with probability exp(-lambda T)   the peg holds:
        x = (S_T - L) / (U - L) ~ Beta(a, b)
    otherwise it breaks, and lands in one of two regimes:
        S_T ~ lognormal(F e^{+j_w}, sigma_w)   weak-side break  (USDHKD higher)
        S_T ~ lognormal(F e^{-j_s}, sigma_s)   strong-side break

Beta carries the peg-intact regime because its support is exactly the band and
it is **U-shaped whenever a < 1 and b < 1** -- it can represent the edge-seeking
behaviour a lognormal or logit-normal cannot.  Its partial moments are closed
form, so prices need no quadrature.

The break leg is a **hazard rate**, not a per-expiry probability.  That matters:
a probability marked per tenor does not compose across the term structure,
whereas ``P(break by T) = 1 - exp(-lambda T)`` does, so one marked lambda gives
a consistent breach probability at every expiry.  Breaks are asymmetric by
default -- a defended peg usually goes the way the pressure is, and the jump
size and post-break volatility are marked separately for each side.

Out-of-band probability is an **output** of this model, and a real one.  The
risk-neutral forward constraint then does something worth noticing: if a break
is expected to devalue, the in-band distribution must sit *below* the forward
to compensate, which shifts the whole peg-intact smile.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.special import betainc, gammaln, ndtr

from . import black, paths
from .black import DeltaConvention
from .numerics import ConvergenceError, solve_scalar


@dataclass(frozen=True)
class Band:
    """A managed trading band for a currency pair."""

    pair: str
    lower: float
    upper: float
    note: str = ""

    def __post_init__(self) -> None:
        if not 0 < self.lower < self.upper:
            raise ValueError(
                f"{self.pair}: band must satisfy 0 < lower < upper, got "
                f"{self.lower!r} and {self.upper!r}"
            )

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def position(self, s: float) -> float:
        """Where ``s`` sits in the band, 0 at the strong side and 1 at the weak."""
        return (s - self.lower) / self.width

    def contains(self, s) -> bool:
        return bool(np.all((np.asarray(s) >= self.lower) & (np.asarray(s) <= self.upper)))


def load_bands(path: str | Path) -> dict[str, Band]:
    """Read ``pair,lower,upper[,note]`` rows.  Bands are policy, so they are data."""
    out: dict[str, Band] = {}
    for line in paths.read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            raise ValueError(f"bad band row {line!r}: expected 'pair,lower,upper[,note]'")
        pair = parts[0].upper()
        out[pair] = Band(pair, float(parts[1]), float(parts[2]),
                         parts[3] if len(parts) > 3 else "")
    return out


@dataclass(frozen=True)
class JumpSpec:
    """Regime-change risk for a managed pair.

    ``hazard`` is an annual intensity, so the probability the peg has broken by
    ``T`` is ``1 - exp(-hazard * T)`` and is automatically consistent across the
    term structure.  Jump sizes are logarithmic: ``weak_jump = 0.05`` means a
    weak-side break lands the pair 5% higher on average.
    """

    hazard: float = 0.02          # per annum
    weak_share: float = 0.85      # of breaks, share that go weak-side
    weak_jump: float = 0.06       # mean log jump on a weak-side break
    weak_vol: float = 0.10        # diffusion volatility after a weak-side break
    strong_jump: float = 0.04     # mean log jump on a strong-side break
    strong_vol: float = 0.08

    def __post_init__(self) -> None:
        if self.hazard < 0:
            raise ValueError(f"hazard must not be negative, got {self.hazard!r}")
        if not 0.0 <= self.weak_share <= 1.0:
            raise ValueError(f"weak_share must lie in [0, 1], got {self.weak_share!r}")
        for name in ("weak_vol", "strong_vol"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)!r}")

    def weights(self, t: float) -> tuple[float, float, float]:
        """(peg holds, weak-side break, strong-side break) probabilities at ``t``."""
        hold = math.exp(-self.hazard * t)
        brk = 1.0 - hold
        return hold, brk * self.weak_share, brk * (1.0 - self.weak_share)


@dataclass
class BetaBandSmile:
    """Beta-on-band peg-intact regime mixed with two-sided break regimes."""

    band: Band
    a: float
    b: float
    t: float
    forward: float
    jump: JumpSpec = field(default_factory=JumpSpec)

    @property
    def weights(self) -> tuple[float, float, float]:
        return self.jump.weights(self.t)

    @property
    def break_levels(self) -> tuple[float, float]:
        """Mean level of each break regime."""
        return (self.forward * math.exp(self.jump.weak_jump),
                self.forward * math.exp(-self.jump.strong_jump))

    @property
    def in_band_mean(self) -> float:
        return self.band.lower + self.band.width * self.a / (self.a + self.b)

    @property
    def mean(self) -> float:
        """Model forward across all three regimes; calibration pins it to market."""
        hold, pw, ps = self.weights
        mw, ms = self.break_levels
        return hold * self.in_band_mean + pw * mw + ps * ms

    @property
    def u_shaped(self) -> bool:
        return self.a < 1.0 and self.b < 1.0

    def required_in_band_mean(self) -> float:
        """Where the peg-intact mean must sit for the model forward to match.

        With an asymmetric break the two are not the same number, and that gap
        is a genuine effect: expected devaluation pushes the peg-intact
        distribution toward the strong side.
        """
        hold, pw, ps = self.weights
        if hold <= 1e-12:
            raise ValueError("the peg is certain to break at this horizon; the band model does not apply")
        mw, ms = self.break_levels
        return (self.forward - pw * mw - ps * ms) / hold

    def _beta_call(self, K):
        """Undiscounted call on the peg-intact regime, in closed form."""
        K = np.asarray(K, dtype=float)
        k = np.clip((K - self.band.lower) / self.band.width, 0.0, 1.0)
        m = self.a / (self.a + self.b)
        tail = m * (1.0 - betainc(self.a + 1.0, self.b, k)) - k * (1.0 - betainc(self.a, self.b, k))
        out = self.band.width * np.maximum(tail, 0.0)
        out = np.where(K >= self.band.upper, 0.0, out)
        return np.where(K <= self.band.lower, self.in_band_mean - K, out)

    def call_price(self, K):
        """Undiscounted call across the regime mixture."""
        K = np.asarray(K, dtype=float)
        hold, pw, ps = self.weights
        mw, ms = self.break_levels
        total = hold * self._beta_call(K)
        Kp = np.maximum(K, 1e-12)
        if pw > 0:
            total = total + pw * np.asarray(black.price(mw, Kp, self.jump.weak_vol, self.t, True), float)
        if ps > 0:
            total = total + ps * np.asarray(black.price(ms, Kp, self.jump.strong_vol, self.t, True), float)
        return total

    def put_price(self, K):
        K = np.asarray(K, dtype=float)
        return self.call_price(K) - (self.mean - K)

    def density(self, S):
        """Terminal density: bounded inside the band, with real tails outside."""
        S = np.asarray(S, dtype=float)
        hold, pw, ps = self.weights
        x = (S - self.band.lower) / self.band.width
        inside = (x > 0.0) & (x < 1.0)
        xs = np.where(inside, x, 0.5)
        log_beta = gammaln(self.a) + gammaln(self.b) - gammaln(self.a + self.b)
        pdf = np.exp((self.a - 1.0) * np.log(xs) + (self.b - 1.0) * np.log1p(-xs) - log_beta)
        out = np.where(inside, hold * pdf / self.band.width, 0.0)
        mw, ms = self.break_levels
        for p, level, vol in ((pw, mw, self.jump.weak_vol), (ps, ms, self.jump.strong_vol)):
            if p <= 0:
                continue
            sq = vol * math.sqrt(self.t)
            z = (np.log(np.maximum(S, 1e-12) / level) + 0.5 * sq * sq) / sq
            out = out + p * np.exp(-0.5 * z * z) / (np.maximum(S, 1e-12) * sq * math.sqrt(2 * math.pi))
        return out

    def breach_probability(self) -> dict:
        """Probability the pair ends outside the band, by cause.

        Real and positive.  Only the break regimes can put mass outside, but
        a break does not guarantee it -- a small jump can still land inside.
        """
        hold, pw, ps = self.weights
        mw, ms = self.break_levels
        out = {"broken": 1.0 - hold, "below": 0.0, "above": 0.0}
        for p, level, vol in ((pw, mw, self.jump.weak_vol), (ps, ms, self.jump.strong_vol)):
            if p <= 0:
                continue
            sq = vol * math.sqrt(self.t)
            d_lo = (math.log(self.band.lower / level) + 0.5 * sq * sq) / sq
            d_hi = (math.log(self.band.upper / level) + 0.5 * sq * sq) / sq
            out["below"] += p * float(ndtr(d_lo))
            out["above"] += p * float(1.0 - ndtr(d_hi))
        out["outside"] = out["below"] + out["above"]
        return out

    def implied_vol(self, K, *, lo: float = 1e-6, hi: float = 3.0):
        """Black volatility repricing this model at ``K``.

        Well defined everywhere now: the break regimes give every strike some
        value, so there is always a volatility to quote.
        """
        scalar = np.isscalar(K)
        Ks = np.atleast_1d(np.asarray(K, dtype=float))
        out = np.full(Ks.shape, np.nan)
        fwd = self.mean
        for i, k in enumerate(Ks):
            is_call = k >= fwd
            px = float(self.call_price(k)) if is_call else float(self.put_price(k))
            intrinsic = max(fwd - k, 0.0) if is_call else max(k - fwd, 0.0)
            if px <= intrinsic + 1e-15:
                continue
            try:
                out[i] = black.implied_vol(px, fwd, float(k), self.t, is_call, lo=lo, hi=hi)
            except (ValueError, ConvergenceError):
                continue
        return float(out[0]) if scalar else out


# ---------------------------------------------------------------------------
# Calibration.  Two stages, kept apart because they are identified by
# different quotes and fail for different reasons:
#
#   A.  the peg-intact body, from the forward and the at-the-money.  Exact,
#       one-dimensional, monotone (``_BodyFit``);
#   B.  the break regime, from the wings.  Overdetermined, least squares,
#       with the residual of every quote reported and the identifiability
#       *measured* at the answer rather than assumed (``_fit_break``).
#
# What the wings can see is less than five quotes suggests, and how much less
# is a measurement rather than an argument.  The obvious argument -- that
# from a strike inside the band a break regime shows only as *mass beyond K
# times mean distance beyond K*, so the hazard and the jump size are one
# product -- turns out to be wrong here: the forward constraint moves the
# peg-intact body with the jump size, and that is visible from inside the
# band (the hazard against the weak jump conditions at about 15 on USDHKD
# quotes with every strike inside).  What *is* degenerate is freeing the
# post-break volatilities beside the hazard and the share on a low-volatility
# tenor: the condition number runs to the thousands and one of them moves
# nothing.  So the jump sizes are given by default because where a peg would
# go is a policy view and how likely it is to go is what the market prices,
# not because the quotes could not tell them apart, and the Jacobian at the
# answer says per fit and per parameter what these quotes actually informed.
# ---------------------------------------------------------------------------

BREAK_PARAMS = ("hazard", "weak_share", "weak_jump", "strong_jump", "weak_vol", "strong_vol")
BREAK_BOUNDS = {
    "hazard": (0.0, 3.0),          # per annum; the upper end is replaced by the ATM ceiling
    "weak_share": (0.0, 1.0),
    "weak_jump": (0.001, 0.5),     # log jump
    "strong_jump": (0.001, 0.5),
    "weak_vol": (0.005, 1.0),
    "strong_vol": (0.005, 1.0),
}
DEFAULT_FREE = ("hazard", "weak_share")
SWEEP_BUDGET = 1500                # sweep nodes at most, across every free dimension
DEGENERATE_CONDITION = 1e3         # above this the Jacobian's small direction is named
INFORMED_FLOOR = 1e-4              # residual change (in vol) over a parameter's whole range


def _respec(spec: JumpSpec, **changes) -> JumpSpec:
    values = {k: getattr(spec, k) for k in BREAK_PARAMS}
    values.update(changes)
    return JumpSpec(**values)


@dataclass(frozen=True)
class WingQuote:
    """One delta's pair of wing quotes: the risk reversal and the smile butterfly.

    ``strangle`` is the smile butterfly -- the average of the two wings less
    the at-the-money -- as everywhere else in this module.  ``fit`` says which
    of the two instruments the calibration is held to; both by default.  The
    strikes are always placed off both, because a wing's strike is where the
    quoted volatility puts it whichever instrument is being fitted.
    """

    delta: float
    risk_reversal: float
    strangle: float
    fit: tuple[str, ...] = ("rr", "fly")

    def __post_init__(self) -> None:
        if not 0.0 < self.delta < 0.5:
            raise ValueError(f"the wing delta must lie in (0, 0.5), got {self.delta!r}")
        bad = sorted(set(self.fit) - {"rr", "fly"})
        if bad or not self.fit:
            raise ValueError(f"a wing is fitted to 'rr' and/or 'fly', not {self.fit!r}")


@dataclass(frozen=True)
class TenorQuotes:
    """Everything one expiry contributes to a term-structure calibration."""

    t: float
    forward: float
    atm_vol: float
    wings: tuple[WingQuote, ...]
    tenor: str = ""

    @property
    def label(self) -> str:
        return self.tenor or f"{self.t:.4f}y"


@dataclass(frozen=True)
class _Wing:
    """A wing quote placed at its strikes, with the market prices it implies."""

    quote: WingQuote
    K_c: float
    v_c: float
    K_p: float
    v_p: float
    px_c: float
    px_p: float
    vega_c: float
    vega_p: float

    def placement(self, band: Band) -> dict:
        return {
            "delta": self.quote.delta,
            "K_call": self.K_c, "K_put": self.K_p,
            "call_position": band.position(self.K_c), "put_position": band.position(self.K_p),
            "call_in_band": bool(band.contains(self.K_c)),
            "put_in_band": bool(band.contains(self.K_p)),
        }


class _BodyFit:
    """Stage A for one expiry: the peg-intact Beta from the forward and the ATM.

    Given the jump specification, the risk-neutral forward constraint fixes
    where the peg-intact mean must sit, which pins ``a / (a + b)``.  That
    leaves a single free parameter -- the Beta concentration -- set by
    repricing the at-the-money option.  A one-dimensional monotone solve, so
    the fit is exact and cannot wander, and it is what stage B profiles out
    at every point it visits.
    """

    def __init__(self, band: Band, forward: float, t: float, atm_vol: float,
                 conv: DeltaConvention | bool = False, label: str = ""):
        if not band.lower < forward < band.upper:
            raise ValueError(
                f"{band.pair}: forward {forward:.5f} lies outside the band "
                f"[{band.lower}, {band.upper}]; the model cannot be calibrated"
            )
        if atm_vol <= 0 or t <= 0:
            raise ValueError(f"need a positive ATM vol and time, got {atm_vol!r}, {t!r}")
        self.band, self.forward, self.t, self.atm_vol, self.conv = band, forward, t, atm_vol, conv
        self.label = label or f"{t:.4f}y"
        self.K_atm = black.dns_strike(forward, atm_vol, t, conv)
        self.is_call = self.K_atm >= forward
        self.atm_ref = float(black.price(forward, self.K_atm, atm_vol, t, self.is_call))
        self.atm_vega = float(black.vega(forward, self.K_atm, atm_vol, t))

    def build(self, concentration: float, spec: JumpSpec) -> BetaBandSmile:
        band, forward, t = self.band, self.forward, self.t
        probe = BetaBandSmile(band, 1.0, 1.0, t, forward, spec)
        target_mean = probe.required_in_band_mean()
        pos = (target_mean - band.lower) / band.width
        if not 0.0 < pos < 1.0:
            raise ValueError(
                f"{band.pair}: with this jump specification the peg-intact mean would have "
                f"to be {target_mean:.5f}, outside the band [{band.lower}, {band.upper}]. "
                f"The expected jump is too large to be consistent with a forward of "
                f"{forward:.5f}"
            )
        s_ = max(concentration, 1e-6)
        return BetaBandSmile(band, pos * s_, (1.0 - pos) * s_, t, forward, spec)

    def _atm_price(self, sm: BetaBandSmile) -> float:
        return float(sm.call_price(self.K_atm)) if self.is_call else float(sm.put_price(self.K_atm))

    def atm_gap(self, concentration: float, spec: JumpSpec) -> float:
        return (self._atm_price(self.build(concentration, spec)) - self.atm_ref) / max(self.atm_vega, 1e-12)

    def bound_vol(self, concentration: float, spec: JumpSpec) -> float:
        """The at-the-money volatility at one end of the concentration range.

        Both ends are bounds on what the model can produce, and which one a
        quote falls outside is a different piece of news:

        * ``1e7`` -- the Beta collapses to a point mass, the peg-intact regime
          contributes no volatility, and what is left is the **floor** the
          break regimes alone impose.  A quote below it says the marked break
          risk is too big.
        * ``1e-4`` -- the Beta is two point masses at the edges, the most
          volatile shape the band admits, giving the **ceiling**.  A quote
          above it says the band is too narrow to be the whole story.

        Reporting only the floor named one cause for both failures, which sent
        a marker to lower a hazard that was not the problem.
        """
        px = self._atm_price(self.build(concentration, spec))
        try:
            return black.implied_vol(px, self.forward, self.K_atm, self.t, self.is_call,
                                     lo=1e-8, hi=3.0)
        except (ValueError, ConvergenceError):
            return float("nan")

    def fit(self, spec: JumpSpec) -> BetaBandSmile:
        # A tighter Beta means a lower at-the-money volatility, so the gap is
        # monotone in the concentration and a bracket always works -- provided
        # the target is above the floor the jump regimes already impose.
        band, t, atm_vol = self.band, self.t, self.atm_vol
        try:
            conc = solve_scalar(lambda c: self.atm_gap(c, spec), 8.0, lo_bound=1e-6,
                                bracket=(1e-4, 1e7), what="band concentration")
        except ConvergenceError:
            floor, ceiling = self.bound_vol(1e7, spec), self.bound_vol(1e-4, spec)
            if ceiling == ceiling and atm_vol > ceiling:
                raise ConvergenceError(
                    f"{band.pair} at t={t:.4f}y: the band [{band.lower:g}, {band.upper:g}] is "
                    f"{band.width / self.forward:.2%} of the forward wide, so even a Beta sitting "
                    f"entirely on its edges reaches only {ceiling:.4%} at-the-money, below the "
                    f"quoted {atm_vol:.4%}. The band and the mark are inconsistent: at this "
                    f"maturity the quote needs the peg to break, so raise the hazard or the "
                    f"jump sizes, widen the band, or re-check the quote."
                ) from None
            raise ConvergenceError(
                f"{band.pair} at t={t:.4f}y: a hazard of {spec.hazard:.4%}/yr with "
                f"{spec.weak_jump:+.1%}/{-spec.strong_jump:+.1%} jumps already implies an "
                f"at-the-money volatility of at least {floor:.4%}, above the quoted "
                f"{atm_vol:.4%}. The break assumption and the ATM mark are inconsistent: "
                f"lower the hazard or the jump sizes, or re-check the quote."
            ) from None
        return self.build(conc, spec)

    def wing(self, q: WingQuote) -> _Wing:
        """Place a wing quote at its strikes and price it in the market's own terms."""
        v_c = self.atm_vol + q.strangle + 0.5 * q.risk_reversal
        v_p = self.atm_vol + q.strangle - 0.5 * q.risk_reversal
        if v_c <= 0 or v_p <= 0:
            raise ValueError(
                f"{self.band.pair} {self.label}: the {q.delta:.0%} wings imply a non-positive "
                f"volatility ({v_c:.4%} call, {v_p:.4%} put); re-check the risk reversal and fly")
        F, t, conv = self.forward, self.t, self.conv
        K_c = black.strike_from_delta(q.delta, F, v_c, t, True, conv)
        K_p = black.strike_from_delta(-q.delta, F, v_p, t, False, conv)
        return _Wing(q, K_c, v_c, K_p, v_p,
                     float(black.price(F, K_c, v_c, t, True)), float(black.price(F, K_p, v_p, t, False)),
                     max(float(black.vega(F, K_c, v_c, t)), 1e-12),
                     max(float(black.vega(F, K_p, v_p, t)), 1e-12))

    def residuals(self, spec: JumpSpec, wings: list[_Wing]) -> np.ndarray:
        """Model less market, per fitted instrument, in volatility.

        A price gap over vega: first order in volatility, defined everywhere
        (an implied volatility is not, at a strike the model gives no time
        value), and it puts a one-week fly and a one-year risk reversal on the
        same scale.  The risk reversal is each leg over its own vega; the fly
        is the **strangle premium** over the strangle's vega, so its zero is
        the strangle premium matched exactly -- which is what ``solve_hazard``
        always solved for, and the wrapper stays a wrapper.
        """
        sm = self.fit(spec)
        out = []
        for w in wings:
            gc = float(sm.call_price(w.K_c)) - w.px_c
            gp = float(sm.put_price(w.K_p)) - w.px_p
            if "rr" in w.quote.fit:
                out.append(gc / w.vega_c - gp / w.vega_p)
            if "fly" in w.quote.fit:
                out.append((gc + gp) / (w.vega_c + w.vega_p))
        return np.asarray(out, dtype=float)

    def labels(self, wings: list[_Wing]) -> list[tuple[str, float, str]]:
        out = []
        for w in wings:
            for inst in ("rr", "fly"):
                if inst in w.quote.fit:
                    out.append((self.label, w.quote.delta, inst))
        return out

    def report(self, smile: BetaBandSmile, wings: list[_Wing] | None = None) -> dict:
        """The read-out every caller shares: shape, breach, and the wings against the model."""
        breach = smile.breach_probability()
        spec = smile.jump
        rep = {
            "a": smile.a, "b": smile.b,
            "u_shaped": smile.u_shaped,
            "hazard": spec.hazard,
            "prob_broken": breach["broken"],
            "prob_outside_band": breach["outside"],
            "prob_above": breach["above"],
            "prob_below": breach["below"],
            "in_band_mean": smile.in_band_mean,
            "in_band_mean_shift": smile.in_band_mean - self.forward,
            "forward_error": smile.mean - self.forward,
            "atm_residual_vol": self.atm_gap(smile.a + smile.b, spec),
            "in_band_sd_pct_of_width": _beta_sd(smile.a, smile.b) * 100.0,
        }
        rep["converged"] = (abs(rep["forward_error"]) < 1e-9
                            and abs(rep["atm_residual_vol"]) < 1e-8)
        if wings:
            rep["wings"] = []
            for w in wings:
                mc, mp = smile.implied_vol(w.K_c), smile.implied_vol(w.K_p)
                row = w.placement(self.band)
                row.update({
                    "quoted_rr": w.v_c - w.v_p, "model_rr": mc - mp,
                    "quoted_fly": 0.5 * (w.v_c + w.v_p) - self.atm_vol,
                    "model_fly": 0.5 * (mc + mp) - self.atm_vol,
                })
                rep["wings"].append(row)
            first = rep["wings"][0]
            rep.update({
                "model_rr": first["model_rr"], "quoted_rr": first["quoted_rr"],
                "model_wing_avg": first["model_fly"] + self.atm_vol,
                "quoted_wing_avg": first["quoted_fly"] + self.atm_vol,
            })
        return rep


def _hazard_ceiling(bodies: list[_BodyFit], spec: JumpSpec) -> float:
    """The largest hazard at which every expiry still fits its at-the-money.

    Above some hazard the jump regimes alone exceed the quoted ATM and
    nothing can be fitted, so any search over the hazard has to stop there.
    Bisect for that ceiling first rather than hand a solver a bracket whose
    upper end throws -- which silently left the hazard at its input value.
    Zero means no hazard at all is consistent with the quotes.
    """
    def fits(h: float) -> bool:
        try:
            for body in bodies:
                body.fit(_respec(spec, hazard=h))
            return True
        except (ConvergenceError, ValueError):
            return False

    hi_h, probe = 0.0, 1e-4
    while probe <= BREAK_BOUNDS["hazard"][1]:
        if not fits(probe):
            break
        hi_h = probe
        probe *= 2.0
    if hi_h > 0.0 and probe <= 2.0 * BREAK_BOUNDS["hazard"][1]:
        a_, b_ = hi_h, min(probe, BREAK_BOUNDS["hazard"][1])
        for _ in range(40):
            mid = 0.5 * (a_ + b_)
            if fits(mid):
                a_ = mid
            else:
                b_ = mid
        hi_h = a_
    return hi_h


def _grid(name: str, lo: float, hi: float, n: int) -> np.ndarray:
    if name in ("hazard", "weak_share"):
        return np.linspace(lo, hi, n)
    return np.geomspace(lo, hi, n)


def _fit_break(bodies: list[tuple[_BodyFit, list[_Wing]]], spec: JumpSpec,
               free: tuple[str, ...], *, ceiling: float | None = None) -> tuple[JumpSpec, dict]:
    """Stage B: the break regime from the wings, holding everything not in ``free``.

    Sweep the admissible box, then polish -- the same discipline as the
    listed-option fit, so the answer does not depend on a starting guess.
    Each point visited profiles the body out exactly (``_BodyFit.fit``), so the
    forward and every at-the-money are matched at every node and only the
    wings are traded off.  One free parameter against one instrument is the
    exact bracketed case, which is what ``solve_hazard`` always was.
    """
    free = tuple(free)
    if not free:
        raise ValueError("nothing is free: name at least one of " + ", ".join(BREAK_PARAMS))
    unknown = [f for f in free if f not in BREAK_PARAMS]
    if unknown:
        raise ValueError(f"unknown break parameter(s) {unknown}; expected some of "
                         + ", ".join(BREAK_PARAMS))
    if len(set(free)) != len(free):
        raise ValueError(f"a parameter is freed twice in {free}")
    labels = [lab for body, wings in bodies for lab in body.labels(wings)]
    if not labels:
        raise ValueError("no wing quote to fit the break regime to")

    def spec_at(x) -> JumpSpec:
        return _respec(spec, **dict(zip(free, (float(v) for v in x))))

    def residuals(x) -> np.ndarray | None:
        try:
            return np.concatenate([body.residuals(spec_at(x), wings) for body, wings in bodies])
        except (ConvergenceError, ValueError):
            return None

    if ceiling is None:
        ceiling = _hazard_ceiling([b for b, _ in bodies], spec)
    if ceiling <= 0.0:
        raise ConvergenceError("no hazard is consistent with the quoted ATM volatility")
    lo = np.array([BREAK_BOUNDS[f][0] for f in free])
    hi = np.array([ceiling if f == "hazard" else BREAK_BOUNDS[f][1] for f in free])
    held = {f: getattr(spec, f) for f in BREAK_PARAMS if f not in free}
    notes: list[str] = []

    # -- one parameter, one instrument: the exact case -------------------
    if len(free) == 1 and len(labels) == 1:
        def gap(v: float) -> float:
            r = residuals([v])
            if r is None:
                raise ConvergenceError(f"{free[0]} = {v:.6g} cannot be fitted")
            return float(r[0])
        g_lo, g_hi = gap(lo[0]), gap(hi[0])
        if g_lo * g_hi > 0:
            side = "more" if g_lo > 0 else "less"
            unit = "/yr" if free[0] == "hazard" else ""
            notes.append(
                f"the quoted wings imply {side} break value than this jump specification "
                f"can produce for any {free[0]} up to {hi[0]:.3%}{unit}; adjust the jump "
                f"sizes or post-break volatilities")
            x = np.array([hi[0] if g_lo < 0 else lo[0]])
            exact = False
        else:
            x = np.array([solve_scalar(gap, 0.5 * (lo[0] + hi[0]), lo_bound=lo[0], hi_bound=hi[0],
                                       bracket=(float(lo[0]), float(hi[0])),
                                       what=f"implied {free[0]}")])
            exact = True
        answer = spec_at(x)
        res = residuals(x)
        return answer, _fit_report(answer, free, held, labels, res, x, lo, hi, residuals,
                                   ceiling, notes, converged=exact, method="bracketed solve")

    # -- sweep the admissible box ----------------------------------------
    per_dim = max(3, min(13, int(round(SWEEP_BUDGET ** (1.0 / len(free))))))
    axes = [_grid(f, lo[i], hi[i], per_dim) for i, f in enumerate(free)]
    best_x, best_sse, visited, feasible = None, math.inf, 0, 0
    for node in np.array(np.meshgrid(*axes, indexing="ij")).reshape(len(free), -1).T:
        visited += 1
        r = residuals(node)
        if r is None:
            continue
        feasible += 1
        sse = float(r @ r)
        if sse < best_sse:
            best_x, best_sse = node.copy(), sse
    if best_x is None:
        raise ConvergenceError(
            f"none of the {visited} sweep nodes over {', '.join(free)} can be fitted to the "
            f"at-the-money marks; the held jump specification and the ATM are inconsistent")
    notes.append(f"sweep: {feasible} of {visited} nodes feasible over {', '.join(free)}")

    # -- polish -----------------------------------------------------------
    x = best_x
    converged = False

    def r_pen(v):
        # An infeasible point (the held jumps and this hazard exceed an ATM)
        # is a wall, not a value; a flat penalty sends the polish back inside.
        r = residuals(v)
        return r if r is not None else np.full(len(labels), 1e3)

    try:
        sol = least_squares(r_pen, best_x, bounds=(lo, hi), method="trf",
                            x_scale=np.maximum(hi - lo, 1e-9), xtol=1e-12, ftol=1e-14,
                            gtol=1e-12, max_nfev=400)
        r_sol = residuals(sol.x)
        if r_sol is not None and float(r_sol @ r_sol) <= best_sse * (1 + 1e-12):
            x, converged = sol.x, True
        else:
            notes.append("polish left the feasible box or did not improve on the sweep; "
                         "reporting the sweep node")
    except Exception as exc:  # noqa: BLE001 - fall back to the sweep node, but say so
        notes.append(f"polish failed ({type(exc).__name__}: {exc}); reporting the sweep node")
    answer = spec_at(x)
    res = residuals(x)
    return answer, _fit_report(answer, free, held, labels, res, x, lo, hi, residuals, ceiling,
                               notes, converged=converged,
                               method=f"least squares, {len(labels)} quotes / {len(free)} free")


def _fit_report(answer: JumpSpec, free, held, labels, res, x, lo, hi, residuals, ceiling,
                notes, *, converged: bool, method: str) -> dict:
    """What the fit found, and how sure the quotes let it be.

    The Jacobian is measured at the answer by finite differences, and two
    things are read off it.  Per parameter, how much the residual vector
    moves over the parameter's *whole admissible range*: below
    ``INFORMED_FLOOR`` these quotes do not see the parameter at all and the
    answer for it is the starting point in disguise.  Across parameters, the
    condition number and the direction of the smallest singular value: above
    ``DEGENERATE_CONDITION`` the two parameters that direction mixes are
    named, because the fit has found *a* pair and not *the* pair.
    """
    x = np.asarray(x, dtype=float)
    n = len(free)
    J = np.zeros((len(labels), n))
    width = hi - lo
    for i in range(n):
        h = max(1e-4 * width[i], 1e-9)
        up, dn = min(x[i] + h, hi[i]), max(x[i] - h, lo[i])
        xu, xd = x.copy(), x.copy()
        xu[i], xd[i] = up, dn
        ru, rd = residuals(xu), residuals(xd)
        if ru is None or rd is None or up == dn:
            J[:, i] = np.nan
            continue
        J[:, i] = (ru - rd) / (up - dn)
    sens = {}
    for i, f in enumerate(free):
        col = J[:, i]
        per_unit = float(np.sqrt(np.nansum(col * col))) if np.isfinite(col).any() else float("nan")
        over_range = per_unit * float(width[i])
        sens[f] = {"value": float(x[i]), "lower": float(lo[i]), "upper": float(hi[i]),
                   "per_unit": per_unit, "over_range": over_range,
                   "informed": bool(over_range == over_range and over_range >= INFORMED_FLOOR)}
    condition, singular, degenerate = float("nan"), [], None
    if np.isfinite(J).all() and n >= 1:
        Js = J * width[None, :]                     # dimensionless: over each range
        try:
            u, s, vt = np.linalg.svd(Js, full_matrices=False)
            singular = [float(v) for v in s]
            if len(s) >= 2 and s[-1] > 0:
                condition = float(s[0] / s[-1])
            elif len(s) >= 2:
                condition = float("inf")
            if n >= 2 and (condition != condition or condition > DEGENERATE_CONDITION):
                v = np.abs(vt[-1])
                pair = [free[i] for i in np.argsort(v)[::-1][:2]]
                degenerate = (f"{pair[0]} and {pair[1]} are nearly degenerate in these quotes "
                              f"(condition {condition:.3g}); hold one of them")
        except np.linalg.LinAlgError:
            pass
    if len(labels) < n:
        notes.append(f"{len(labels)} quotes cannot determine {n} parameters; the fit is "
                     f"underdetermined and the answer is one of many")
    uninformed = [f for f in free if not sens[f]["informed"]]
    if uninformed:
        notes.append("not informed by these quotes: " + ", ".join(uninformed)
                     + " (the answer there is the starting point in disguise; hold it)")
    if degenerate:
        notes.append(degenerate)
    res = np.asarray(res, dtype=float)
    rows = [{"tenor": lab[0], "delta": lab[1], "instrument": lab[2], "residual": float(r)}
            for lab, r in zip(labels, res)]
    return {
        "method": method,
        "free": list(free),
        "held": {k: float(v) for k, v in held.items()},
        "fitted": {f: float(getattr(answer, f)) for f in free},
        "hazard_ceiling": ceiling,
        "converged": bool(converged),
        "n_quotes": len(labels),
        "residuals": rows,
        "rmse": float(np.sqrt(np.mean(res * res))) if len(res) else float("nan"),
        "max_abs_residual": float(np.max(np.abs(res))) if len(res) else float("nan"),
        "sensitivity": sens,
        "singular_values": singular,
        "condition": condition,
        "degenerate": degenerate,
        "notes": notes,
    }


def calibrate_band_smile(
    band: Band, forward: float, t: float, atm_vol: float,
    *, risk_reversal: float | None = None, strangle: float | None = None,
    delta: float = 0.25, conv: DeltaConvention | bool = False,
    jump: JumpSpec | None = None, solve_hazard: bool = False,
) -> tuple[BetaBandSmile, dict]:
    """Fit the peg-intact regime to the forward and the at-the-money volatility.

    Stage A on its own (``_BodyFit``): the forward pins ``a / (a + b)`` and
    the at-the-money sets the concentration, exactly.  The wings, if given,
    are *reported* against the result.

    ``solve_hazard=True`` is the one-parameter, one-instrument case of
    ``_fit_break``: the hazard alone, against the strangle premium at
    ``delta``, holding the jump sizes and post-break volatilities -- a
    bracketed solve, as it always was, and the report says what it depended
    on.  For the hazard *and* the share of breaks from both wings at both
    deltas see ``calibrate_band_wings``; across the term structure,
    ``calibrate_band_term_structure``.
    """
    jump = jump or JumpSpec()
    body = _BodyFit(band, forward, t, atm_vol, conv)
    wings = None
    if risk_reversal is not None and strangle is not None:
        wings = [body.wing(WingQuote(delta, risk_reversal, strangle, fit=("fly",)))]

    hazard_note = ""
    fit = None
    if solve_hazard and wings is not None:
        ceiling = _hazard_ceiling([body], jump)
        if ceiling <= 0.0:
            hazard_note = "no hazard is consistent with the quoted ATM volatility"
        else:
            jump, fit = _fit_break([(body, wings)], jump, ("hazard",), ceiling=ceiling)
            hazard_note = "; ".join(fit["notes"])

    smile = body.fit(jump)
    report = body.report(smile, wings)
    report["hazard_note"] = hazard_note
    if fit is not None:
        report["fit"] = fit
    return smile, report


def calibrate_band_wings(
    band: Band, forward: float, t: float, atm_vol: float, wings: Sequence[WingQuote],
    *, conv: DeltaConvention | bool = False, jump: JumpSpec | None = None,
    free: Sequence[str] = DEFAULT_FREE,
) -> tuple[BetaBandSmile, dict]:
    """One expiry: the body from the ATM, the break regime from every wing given.

    ``jump`` holds the starting point and everything not in ``free``; by
    default the jump sizes and post-break volatilities are given and the
    hazard and the weak-side share are fitted to the four wing quotes, which
    is the most the wings of one expiry can honestly determine (see the note
    at the top of this section).  Any subset of ``BREAK_PARAMS`` may be freed;
    the report measures whether the quotes informed each one.
    """
    jump = jump or JumpSpec()
    body = _BodyFit(band, forward, t, atm_vol, conv)
    placed = [body.wing(q) for q in wings]
    jump, fit = _fit_break([(body, placed)], jump, tuple(free))
    smile = body.fit(jump)
    report = body.report(smile, placed)
    report["fit"] = fit
    report["hazard_note"] = "; ".join(n for n in fit["notes"] if not n.startswith("sweep:"))
    return smile, report


def calibrate_band_term_structure(
    band: Band, tenors: Sequence[TenorQuotes],
    *, conv: DeltaConvention | bool = False, jump: JumpSpec | None = None,
    free: Sequence[str] = DEFAULT_FREE,
) -> dict:
    """Every expiry at once: one break regime, one body per tenor.

    The hazard, the share and the jump parameters are properties of the
    regime, not of a tenor -- the model already says so, ``JumpSpec`` is
    annual -- and the body is bounded: a Beta on the band cannot carry more
    variance than two point masses on its edges, and that bound does not grow
    with ``T``.  So the term structure separates the two regimes in a way no
    single expiry can: short tenors are all body, long tenors cap the body
    and any variance left over has to be break.  Every quote of every tenor
    goes into one least squares over the shared parameters, with each
    tenor's concentration profiled out exactly at every point visited.

    Beside the shared answer, each tenor's own implied hazard is solved with
    the shared answer's other parameters held: a flat row says the regime
    model is consistent with the market across the curve, a sloping one says
    the jump sizes or the band are wrong -- the diagnostic a sloping SABR
    ``nu`` gives for mean reversion.  A tenor that cannot be placed keeps its
    row and carries the reason, and is left out of the shared fit.
    """
    jump = jump or JumpSpec()
    free = tuple(free)
    bodies: list[tuple[_BodyFit, list[_Wing]]] = []
    rows: list[dict] = []
    for tq in tenors:
        row = {"tenor": tq.label, "t": tq.t, "forward": tq.forward, "atm": tq.atm_vol,
               "message": "", "used": False}
        try:
            body = _BodyFit(band, tq.forward, tq.t, tq.atm_vol, conv, label=tq.label)
            placed = [body.wing(q) for q in tq.wings]
            if not placed:
                raise ValueError(f"{tq.label}: no wing quotes")
            bodies.append((body, placed))
            row["used"] = True
        except (ValueError, ConvergenceError) as exc:
            row["message"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    # A tenor no hazard at all can fit -- a band too narrow for its ATM at
    # that maturity -- would make the shared ceiling zero and take every
    # other tenor down with it.  It keeps its row, says so, and sits out.
    kept, ceiling = [], math.inf
    used_rows = iter(r for r in rows if r["used"])
    for body, placed in bodies:
        row = next(used_rows)
        own = _hazard_ceiling([body], jump)
        if own <= 0.0:
            row["used"] = False
            try:
                body.fit(_respec(jump, hazard=0.0))
                row["message"] = f"{row['tenor']}: no hazard is consistent with its ATM"
            except (ConvergenceError, ValueError) as exc:
                row["message"] = f"{type(exc).__name__}: {exc}"
            continue
        kept.append((body, placed))
        ceiling = min(ceiling, own)
    bodies = kept
    if not bodies:
        raise ValueError("no tenor could be placed on the band: "
                         + "; ".join(r["message"] for r in rows if r["message"]))

    shared, fit = _fit_break(bodies, jump, free, ceiling=ceiling)

    used = iter(bodies)
    hazard_by_tenor = []
    for row in rows:
        if not row["used"]:
            continue
        body, placed = next(used)
        try:
            smile = body.fit(shared)
            row.update(body.report(smile, placed))
            row["residuals"] = [{"delta": lab[1], "instrument": lab[2], "residual": float(r)}
                                for lab, r in zip(body.labels(placed), body.residuals(shared, placed))]
        except (ValueError, ConvergenceError) as exc:
            row["message"] = f"{type(exc).__name__}: {exc}"
        # The tenor's own hazard, with the shared share and jumps held.
        own = {"tenor": row["tenor"], "t": row["t"], "hazard": None, "note": ""}
        try:
            alone, own_fit = _fit_break([(body, placed)], shared, ("hazard",))
            own["hazard"] = alone.hazard
            own["rmse"] = own_fit["rmse"]
            own["note"] = "; ".join(n for n in own_fit["notes"] if not n.startswith("sweep:"))
        except (ValueError, ConvergenceError) as exc:
            own["note"] = f"{type(exc).__name__}: {exc}"
        hazard_by_tenor.append(own)

    own_values = [h["hazard"] for h in hazard_by_tenor if h["hazard"] is not None]
    slope_note = ""
    if len(own_values) >= 2:
        spread = max(own_values) - min(own_values)
        rel = spread / max(shared.hazard, 1e-9)
        if rel > 0.5:
            slope_note = (f"the per-tenor hazards run from {min(own_values):.3%} to "
                          f"{max(own_values):.3%} against a shared {shared.hazard:.3%}: the "
                          f"regime is not flat across the curve, which points at the jump "
                          f"sizes, the post-break volatilities or the band rather than at "
                          f"the hazard")
    return {
        "jump": shared,
        "fit": fit,
        "rows": rows,
        "hazard_by_tenor": hazard_by_tenor,
        "hazard_slope_note": slope_note,
        "n_tenors": len(bodies),
    }


def _beta_sd(a: float, b: float) -> float:
    return math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))


# --------------------------------------------------------------------------
# How a band is allowed to affect the surface
# --------------------------------------------------------------------------
#: What a marked band does to the lognormal surface.
#:
#: ``warn``     the surface prices as it always did, and a strike outside the
#:              band is flagged.  The default: a band is a fact about the
#:              policy regime long before anyone decides to price off it.
#: ``off``      not even the flag.  A deliberate statement that this range is
#:              not a hard barrier -- an ERM II style central rate the central
#:              bank does not actually defend at the edge, say.
#: ``mixture``  the regime mixture above prices the smile: Beta on the band
#:              for the peg-intact regime, plus the two break legs.  This is
#:              what the ``BAND`` interpolation method selects.
BAND_MODES = ("warn", "off", "mixture")


@dataclass(frozen=True)
class BandTreatment:
    """The adjustable part of the barrier: everything a marker may move.

    The band itself is policy and lives in ``bands.csv``.  What is *marked* is
    how much the peg is worth paying attention to, and none of it can be
    inferred from the quotes: a wider Beta body and a higher hazard both raise
    the at-the-money, so a joint fit is degenerate (§6).  Every number here is
    therefore an input, and ``solve_hazard`` inverts one of them deliberately
    and reports what it depended on.

    ``lower`` and ``upper`` override the edges.  A desk that thinks the HKMA
    will let 7.85 go by a figure before it intervenes can say so here rather
    than editing the policy file, and the override is reported everywhere the
    band is.

    ``blend`` is a **marking convenience, not a model**: at anything strictly
    between 0 and 1 the result is a weighted average of two implied
    volatilities and carries neither model's arbitrage guarantee.  It exists
    because a peg regime is not switched on overnight, and it warns.
    """

    mode: str = "warn"
    jump: JumpSpec = field(default_factory=JumpSpec)
    lower: float | None = None
    upper: float | None = None
    blend: float = 1.0
    delta: float = 0.25
    solve_hazard: bool = False

    def __post_init__(self) -> None:
        if self.mode not in BAND_MODES:
            raise ValueError(
                f"unknown band mode {self.mode!r}; expected one of {', '.join(BAND_MODES)}")
        if not 0.0 <= self.blend <= 1.0:
            raise ValueError(f"blend must lie in [0, 1], got {self.blend!r}")
        if not 0.0 < self.delta < 0.5:
            raise ValueError(f"the wing delta must lie in (0, 0.5), got {self.delta!r}")
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ValueError(
                f"the band override needs lower < upper, got {self.lower!r} and {self.upper!r}")

    @property
    def active(self) -> bool:
        """Whether this treatment puts the band into the price."""
        return self.mode == "mixture"

    def effective_band(self, band: Band) -> Band:
        """The band after any override, with the override written into the note."""
        lo = band.lower if self.lower is None else float(self.lower)
        hi = band.upper if self.upper is None else float(self.upper)
        if lo == band.lower and hi == band.upper:
            return band
        return Band(band.pair, lo, hi,
                    (band.note + " | " if band.note else "")
                    + f"edges overridden from [{band.lower}, {band.upper}] on the marking screen")

    def scaled(self, band: Band, factor: float) -> Band:
        """The effective band moved into a space where the forward is *factor*.

        The surface works in strike/forward ratio and a band is absolute, which
        is the one piece of plumbing this model was missing.  The whole
        regime mixture is scale invariant -- jump sizes are logarithmic and the
        post-break volatilities are relative -- so dividing the edges and the
        forward by the same number moves the model into moneyness exactly.
        """
        eff = self.effective_band(band)
        if factor == 1.0:
            return eff
        return Band(eff.pair, eff.lower * factor, eff.upper * factor, eff.note)

    def warnings(self) -> list[str]:
        out = []
        if self.active and 0.0 < self.blend < 1.0:
            out.append(
                f"the smile is {self.blend:.0%} band model and {1 - self.blend:.0%} lognormal. "
                "A blend of two implied volatilities is a marking convenience and is not "
                "arbitrage free in either model's sense")
        if self.active and self.blend == 0.0:
            out.append("blend is zero, so the band model is calibrated and reported but "
                       "prices nothing; the lognormal smile is what you are quoting")
        return out

    def describe(self) -> str:
        if self.mode == "off":
            return "band ignored: the surface prices as if the pair were free floating"
        if self.mode == "warn":
            return "band flagged only: strikes outside it are warned about, prices are lognormal"
        j = self.jump
        return (f"regime mixture at {self.blend:.0%}: hazard {j.hazard:.3%}/yr, "
                f"{j.weak_share:.0%} weak-side, jumps {j.weak_jump:+.2%}/{-j.strong_jump:+.2%}, "
                f"post-break vols {j.weak_vol:.2%}/{j.strong_vol:.2%}"
                + (", hazard solved from the wings" if self.solve_hazard else ""))

    # -- the edges ---------------------------------------------------------
    # Everything a human types or reads is in points and percentages;
    # everything inside the model is a decimal.  The conversion happens here
    # and nowhere else (§4).
    def to_request(self) -> dict:
        j = self.jump
        return {
            "mode": self.mode,
            "hazard": j.hazard * 100.0,
            "weak_share": j.weak_share * 100.0,
            "weak_jump": j.weak_jump * 100.0,
            "weak_vol": j.weak_vol * 100.0,
            "strong_jump": j.strong_jump * 100.0,
            "strong_vol": j.strong_vol * 100.0,
            "lower": self.lower,
            "upper": self.upper,
            "blend": self.blend * 100.0,
            "delta": self.delta * 100.0,
            "solve_hazard": self.solve_hazard,
        }

    @classmethod
    def from_request(cls, payload: dict | None) -> "BandTreatment":
        """Read the treatment the browser or the CLI supplied.

        Percentages in, decimals out.  A blank or missing field keeps the
        default rather than becoming zero -- a hazard silently set to zero is
        a peg that cannot break, which is the very thing §6 forbids.
        """
        payload = payload or {}
        base = JumpSpec()

        def pct(name: str, default: float) -> float:
            raw = payload.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return default
            try:
                return float(raw) / 100.0
            except (TypeError, ValueError):
                raise ValueError(f"band {name}: {raw!r} is not a number") from None

        def edge(name: str):
            raw = payload.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"band {name}: {raw!r} is not a number") from None

        jump = JumpSpec(
            hazard=pct("hazard", base.hazard),
            weak_share=pct("weak_share", base.weak_share),
            weak_jump=pct("weak_jump", base.weak_jump),
            weak_vol=pct("weak_vol", base.weak_vol),
            strong_jump=pct("strong_jump", base.strong_jump),
            strong_vol=pct("strong_vol", base.strong_vol),
        )
        return cls(
            mode=str(payload.get("mode") or "warn").strip().lower(),
            jump=jump,
            lower=edge("lower"), upper=edge("upper"),
            blend=pct("blend", 1.0),
            delta=pct("delta", 0.25),
            solve_hazard=bool(payload.get("solve_hazard")),
        )


def band_panel(surface, tenors=None, *, cut: str = "NY") -> dict:
    """What the regime mixture says about one pair, tenor by tenor.

    This is the read-out behind the marking screen's band card and behind
    ``volkit band``: the same function, so a figure quoted off the screen can
    be reproduced in a batch job.

    It is run whatever the treatment mode is, because the question it answers
    -- *how much of this smile is peg-break premium* -- is the one a marker
    asks before deciding whether to price off the band at all.  A tenor that
    cannot be calibrated keeps its row and carries the reason.
    """

    treatment = surface.band_treatment
    band = surface.band
    out = {
        "pair": surface.pair,
        "cut": cut,
        "has_band": band is not None,
        "band": None,
        "treatment": treatment.to_request(),
        "describe": treatment.describe(),
        "warnings": list(treatment.warnings()),
        "rows": [],
    }
    if band is None:
        out["message"] = (f"{surface.pair} has no managed band. Bands are policy and live in "
                          f"bands.csv; only put a pair there if the range is genuinely defended")
        return out
    effective = treatment.effective_band(band)
    out["band"] = {
        "pair": band.pair, "lower": band.lower, "upper": band.upper, "note": band.note,
        "effective_lower": effective.lower, "effective_upper": effective.upper,
        "overridden": (effective.lower, effective.upper) != (band.lower, band.upper),
    }

    tenors = list(tenors or getattr(surface.atm, "tenor_points", ()) or ())
    for tenor in tenors:
        t = surface.tenor_years(tenor)
        row = {"tenor": tenor, "t": t, "forward": None, "message": ""}
        try:
            expiry = surface.clock.datetime_from_years(t)
            lookup = surface.forward_lookup
            row["forward"] = float(lookup(t)) if lookup is not None and lookup(t) else None
            sl = surface.slice_at(expiry, "BAND", cut)
            row.update({k: v for k, v in (sl.band_report or {}).items()})
            row["atm"] = sl.atm_vol
            row["lognormal_rr"] = float(sl.vols[3] - sl.vols[1])
            row["band_lower"] = sl.band.lower
            row["band_upper"] = sl.band.upper
        except Exception as exc:  # noqa: BLE001 - one tenor, not the panel
            row["message"] = f"{type(exc).__name__}: {exc}"
        out["rows"].append(row)
    return out


def fit_band_treatment(surface, tenors=None, *, free: Sequence[str] = DEFAULT_FREE,
                       treatment: "BandTreatment | None" = None, cut: str = "NY") -> dict:
    """Propose a break regime for one pair from its marked wings, every tenor at once.

    The read-out behind the band card's **Fit from the wings** and behind
    ``volkit band --fit``: the marked 10 and 25 delta risk reversals and
    butterflies at every quoted tenor go into ``calibrate_band_term_structure``
    with the treatment's own jump specification as the given part, and what
    comes back is a *proposal* in the card's own units.  Nothing is marked:
    break risk stays a marked input (§6), and the desk applies the proposal
    with the same Apply it uses for a number it typed.

    The wings are read off the lognormal surface -- the marks -- rather than
    off the band slice, which is the thing being fitted.  A tenor with no
    forward, or one the band cannot hold, keeps its row and its reason.
    """
    from .smile import LOGNORMAL_INTERPOLATORS

    treatment = treatment or surface.band_treatment
    band = surface.band
    out = {
        "pair": surface.pair, "cut": cut, "free": list(free),
        "has_band": band is not None, "proposal": None, "rows": [], "warnings": [],
    }
    if band is None:
        out["message"] = (f"{surface.pair} has no managed band. Bands are policy and live in "
                          f"bands.csv; only put a pair there if the range is genuinely defended")
        return out
    effective = treatment.effective_band(band)
    method = surface.method if surface.method in LOGNORMAL_INTERPOLATORS else LOGNORMAL_INTERPOLATORS[0]
    tenors = list(tenors or getattr(surface.atm, "tenor_points", ()) or ())
    quotes: list[TenorQuotes] = []
    skipped: list[dict] = []
    for tenor in tenors:
        t = surface.tenor_years(tenor)
        try:
            lookup = surface.forward_lookup
            fwd = float(lookup(t)) if lookup is not None and lookup(t) else None
            if not fwd:
                raise ValueError(f"no forward for {surface.pair} at {tenor}; a band is absolute "
                                 f"and placing it needs a feed")
            expiry = surface.clock.datetime_from_years(t)
            sl = surface.slice_at(expiry, method, cut, forward=fwd)
            v = [float(x) for x in sl.vols]
            atm = float(sl.atm_vol)
            wings = (WingQuote(0.25, v[3] - v[1], 0.5 * (v[3] + v[1]) - atm),
                     WingQuote(0.10, v[4] - v[0], 0.5 * (v[4] + v[0]) - atm))
            quotes.append(TenorQuotes(t, fwd, atm, wings, tenor))
        except Exception as exc:  # noqa: BLE001 - one tenor, not the panel
            skipped.append({"tenor": tenor, "t": t, "forward": None, "atm": None,
                            "message": f"{type(exc).__name__}: {exc}", "used": False})
    if not quotes:
        out["rows"] = skipped
        out["message"] = "no tenor could be read: " + "; ".join(r["message"] for r in skipped)
        return out

    result = calibrate_band_term_structure(effective, quotes, conv=surface.conv,
                                           jump=treatment.jump, free=free)
    proposed = BandTreatment(mode=treatment.mode, jump=result["jump"], lower=treatment.lower,
                             upper=treatment.upper, blend=treatment.blend,
                             delta=treatment.delta, solve_hazard=False)
    out.update({
        "proposal": proposed.to_request(),
        "starting": treatment.to_request(),
        "describe": proposed.describe(),
        "fit": result["fit"],
        "rows": result["rows"] + skipped,
        "hazard_by_tenor": result["hazard_by_tenor"],
        "hazard_slope_note": result["hazard_slope_note"],
        "n_tenors": result["n_tenors"],
    })
    if result["hazard_slope_note"]:
        out["warnings"].append(result["hazard_slope_note"])
    out["warnings"].extend(n for n in result["fit"]["notes"] if not n.startswith("sweep:"))
    if not result["fit"]["converged"]:
        out["warnings"].append("the polish did not converge; the proposal is the best sweep node")
    return out
