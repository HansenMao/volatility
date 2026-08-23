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

import numpy as np
from scipy.optimize import least_squares
from scipy.special import betainc, gammaln, ndtr

from . import black
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
    for line in Path(path).read_text().splitlines():
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


def calibrate_band_smile(
    band: Band, forward: float, t: float, atm_vol: float,
    *, risk_reversal: float | None = None, strangle: float | None = None,
    delta: float = 0.25, conv: DeltaConvention | bool = False,
    jump: JumpSpec | None = None, solve_hazard: bool = False,
) -> tuple[BetaBandSmile, dict]:
    """Fit the peg-intact regime to the forward and the at-the-money volatility.

    Given the jump specification, the risk-neutral forward constraint fixes
    where the peg-intact mean must sit, which pins ``a / (a + b)``.  That leaves
    a single free parameter -- the Beta concentration -- set by repricing the
    at-the-money option.  A one-dimensional monotone solve, so the fit is exact
    and cannot wander.

    ``solve_hazard=True`` additionally backs the break intensity out of the
    quoted wings.  Because it is a hazard rate rather than a per-tenor
    probability, the answer is directly comparable across expiries; it still
    depends on the assumed jump sizes and post-break volatilities, and the
    report says so.
    """
    jump = jump or JumpSpec()
    if not band.lower < forward < band.upper:
        raise ValueError(
            f"{band.pair}: forward {forward:.5f} lies outside the band "
            f"[{band.lower}, {band.upper}]; the model cannot be calibrated"
        )
    if atm_vol <= 0 or t <= 0:
        raise ValueError(f"need a positive ATM vol and time, got {atm_vol!r}, {t!r}")

    K_atm = black.dns_strike(forward, atm_vol, t, conv)
    is_call = K_atm >= forward
    atm_ref = float(black.price(forward, K_atm, atm_vol, t, is_call))
    atm_vega = float(black.vega(forward, K_atm, atm_vol, t))

    def build(concentration: float, spec: JumpSpec) -> BetaBandSmile:
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

    def atm_gap(concentration: float, spec: JumpSpec) -> float:
        sm = build(concentration, spec)
        px = float(sm.call_price(K_atm)) if is_call else float(sm.put_price(K_atm))
        return (px - atm_ref) / max(atm_vega, 1e-12)

    def jump_floor_vol(spec: JumpSpec) -> float:
        """ATM volatility from the break regimes alone.

        As the Beta tightens to a point mass the peg-intact regime contributes
        no volatility, so this is the lowest ATM the model can produce for a
        given jump specification.  A quote below it is not fittable, and that
        is a statement about the marks, not a solver failure.
        """
        sm = build(1e7, spec)
        px = float(sm.call_price(K_atm)) if is_call else float(sm.put_price(K_atm))
        try:
            return black.implied_vol(px, forward, K_atm, t, is_call, lo=1e-8, hi=3.0)
        except (ValueError, ConvergenceError):
            return float("nan")

    def fit_concentration(spec: JumpSpec) -> BetaBandSmile:
        # A tighter Beta means a lower at-the-money volatility, so the gap is
        # monotone in the concentration and a bracket always works -- provided
        # the target is above the floor the jump regimes already impose.
        try:
            conc = solve_scalar(lambda c: atm_gap(c, spec), 8.0, lo_bound=1e-6,
                                bracket=(1e-4, 1e7), what="band concentration")
        except ConvergenceError:
            floor = jump_floor_vol(spec)
            raise ConvergenceError(
                f"{band.pair} at t={t:.4f}y: a hazard of {spec.hazard:.4%}/yr with "
                f"{spec.weak_jump:+.1%}/{-spec.strong_jump:+.1%} jumps already implies an "
                f"at-the-money volatility of at least {floor:.4%}, above the quoted "
                f"{atm_vol:.4%}. The break assumption and the ATM mark are inconsistent: "
                f"lower the hazard or the jump sizes, or re-check the quote."
            ) from None
        return build(conc, spec)

    wings = None
    if risk_reversal is not None and strangle is not None:
        v_c = atm_vol + strangle + 0.5 * risk_reversal
        v_p = atm_vol + strangle - 0.5 * risk_reversal
        K_c = black.strike_from_delta(delta, forward, v_c, t, True, conv)
        K_p = black.strike_from_delta(-delta, forward, v_p, t, False, conv)
        wings = (K_c, v_c, K_p, v_p)

    if solve_hazard and wings is not None:
        K_c, v_c, K_p, v_p = wings
        market = (float(black.price(forward, K_c, v_c, t, True))
                  + float(black.price(forward, K_p, v_p, t, False)))

        def respec(h: float) -> JumpSpec:
            return JumpSpec(h, jump.weak_share, jump.weak_jump, jump.weak_vol,
                            jump.strong_jump, jump.strong_vol)

        def wing_gap(h: float) -> float:
            sm = fit_concentration(respec(h))
            return float(sm.call_price(K_c)) + float(sm.put_price(K_p)) - market

        # Above some hazard the jump regimes alone exceed the quoted ATM and
        # nothing can be fitted, so the search has to stop there.  Bisect for
        # that ceiling first rather than hand the solver a bracket whose upper
        # end throws -- which silently left the hazard at its input value.
        lo_h, hi_h = 0.0, 0.0
        probe = 1e-4
        while probe <= 3.0:
            try:
                wing_gap(probe)
                hi_h = probe
                probe *= 2.0
            except (ConvergenceError, ValueError):
                break
        if hi_h > 0.0 and probe <= 6.0:
            a_, b_ = hi_h, min(probe, 3.0)
            for _ in range(40):
                mid = 0.5 * (a_ + b_)
                try:
                    wing_gap(mid)
                    a_ = mid
                except (ConvergenceError, ValueError):
                    b_ = mid
            hi_h = a_
        hazard_note = ""
        if hi_h <= 0.0:
            hazard_note = "no hazard is consistent with the quoted ATM volatility"
        else:
            g_lo, g_hi = wing_gap(lo_h), wing_gap(hi_h)
            if g_lo * g_hi > 0:
                side = "more" if g_lo > 0 else "less"
                hazard_note = (
                    f"the quoted wings imply {side} break value than this jump "
                    f"specification can produce for any hazard up to {hi_h:.3%}/yr; "
                    f"adjust the jump sizes or post-break volatilities"
                )
                jump = respec(hi_h if g_lo < 0 else lo_h)
            else:
                jump = respec(solve_scalar(wing_gap, 0.5 * (lo_h + hi_h), lo_bound=lo_h,
                                           hi_bound=hi_h, bracket=(lo_h, hi_h),
                                           what="implied break hazard"))

    smile = fit_concentration(jump)
    breach = smile.breach_probability()
    hazard_note = locals().get("hazard_note", "")
    report = {
        "a": smile.a, "b": smile.b,
        "u_shaped": smile.u_shaped,
        "hazard": jump.hazard,
        "prob_broken": breach["broken"],
        "prob_outside_band": breach["outside"],
        "prob_above": breach["above"],
        "prob_below": breach["below"],
        "in_band_mean": smile.in_band_mean,
        "in_band_mean_shift": smile.in_band_mean - forward,
        "forward_error": smile.mean - forward,
        "atm_residual_vol": atm_gap(smile.a + smile.b, jump),
        "in_band_sd_pct_of_width": _beta_sd(smile.a, smile.b) * 100.0,
        "hazard_note": hazard_note,
    }
    report["converged"] = (abs(report["forward_error"]) < 1e-9
                           and abs(report["atm_residual_vol"]) < 1e-8)
    if wings is not None:
        K_c, v_c, K_p, v_p = wings
        mc, mp = smile.implied_vol(K_c), smile.implied_vol(K_p)
        report.update({
            "model_rr": mc - mp, "quoted_rr": v_c - v_p,
            "model_wing_avg": 0.5 * (mc + mp), "quoted_wing_avg": 0.5 * (v_c + v_p),
        })
    return smile, report


def _beta_sd(a: float, b: float) -> float:
    return math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))
