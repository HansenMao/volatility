"""Making a two-way price: check the market against the curve, and quote off it.

The other three screens answer "what is this worth".  This one answers "what
do I show", which is a different question with parts to it, and the module
keeps them apart because they fail for different reasons and a desk needs to
see which one broke.

**Nothing in this module moves a mark.**  It used to: the panel that read a
broker run also fitted the curve and the wings to it, and there were two
places on one screen where a curve could change.  Every mark that moves is the
marking agent's now (:mod:`volkit.marking`) -- one card, one journal, one
proposal a person answers -- and what is left here is the two things a desk
does with a curve it is not changing.

:class:`CheckPanel` reads a broker run and says **where the marks sit against
it**: through their bid or their offer, near an edge, or in line, with the
distance in volatility points and in units of their own width.  It puts a
price on nothing and it fits nothing.  :class:`QuotePanel` reads a list of
instruments somebody has asked for, with no prices on them, and makes a
two-way in each.  Both may be handed the marks the marking card is holding --
they meet the agent at :func:`capture_marks`, the browser carries the numbers
like every other piece of panel state (§4 -- the server holds none), and a
panel given none reads the book as it stands and says which of the two it did.

A check is answered against a run that has just arrived; a quote is answered
in seconds, over and over, against whatever is marked.  A request does not
arrive with a market on it (§17 says the same thing about the quoting agent),
which is why the two boxes are separate: tying them together meant a request
could only be priced against a market that had nothing to do with it.

**The check.**  Each line of the paste is turned into the one number the
surface says at that instrument, and compared with the two-way the line
quoted.  Outside it is an alert with the gap; inside but within
:data:`NEAR_EDGE` of a side is a warning, because that is a market move away
from being outside; anything else is in line.  A line the surface cannot be
read at is *not checked* -- a message, never a pass.  The curve that is
checked is the marks the panel was handed, or the book, and a held set of
marks that the book has moved under is dropped and named rather than laid back
over what was marked in the meantime.

**What a fit was, and where it went.**  Fitting the backbone through a target
term structure (``fit_atm_curve``) and fine tuning the four smile parameters
against the quoted wings (``tune_smile_shifts``) both still live here, because
they are model code and the agent is what calls them.  The target curve itself
is :func:`curve_targets`.  What has gone is the *panel* that ran them: there is
no button on this screen that moves a mark.

**The quote.**  A mid is not a price.  The width comes from the pair's
knowledge bank (:mod:`volkit.knowledge`), the mid is shaded by what the fair
value screen says about richness and by the vega already on the book, and both
shadings are capped as a fraction of the width so an axe can lean the price
but never walk it out of the market on its own.  Every number that moved the
quote is reported next to it with the rule or the input that moved it.  What
is quoted is the **request box** -- ``1M ATM in 100mm``, ``3M 25d RR``, read
by :func:`volkit.quotes.parse_requests`, which refuses a price on the line
rather than reading it as a strike.  A request that names something the market
paste also quoted carries that market beside our price, so "inside their
market" survives the split; a request nothing quoted is priced just the same,
which is the point of asking for it separately.

Two things the quote deliberately does *not* do.  It does not apply a fair
value or a vega axe to a risk reversal or a butterfly: a break-even against
realized volatility is a statement about the *level*, and a pasted vega profile
is a vega position, and neither says anything about where the skew should be
marked.  Those rows show the model mid with the bank's width and say why there
is no shading.  And it does not invent a width: a quote no rule matches gets
no bid and no offer, with the reason on the row.

Volatilities are decimals inside this module and volatility points at the
panel boundary, the same split :mod:`volkit.listed` uses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from . import black, kace, sabr
from .calendars import DEFAULT_CALENDARS
from .cross import CrossAtmCurve
from .knowledge import KnowledgeBank, PairKnowledge, Rule, rule_from_dict
from .numerics import ConvergenceError
from .quotes import (FLY_CONVENTIONS, MarketQuote, QuoteError, VOL_UNITS,
                     instrument_key, parse_quotes, parse_requests, parse_vega_profile)
from .sabr import SabrParams
from .smile import INTERPOLATORS
from .surface import PARAM_NAMES
from .timeutil import DAYS_IN_YEAR, UTC, tenor_to_years

TARGET_SOURCES = ("overwrites", "paste", "quotes", "current", "none")
BACKBONE_KNOBS = ("initial_vol", "long_term_vol", "mean_reversion", "short_addon", "short_decay")
CROSS_KNOBS = ("corr_initial", "corr_final", "corr_decay", "short_addon", "short_decay")
DEFAULT_BACKBONE_FREE = ("initial_vol", "long_term_vol", "mean_reversion", "short_addon")
DEFAULT_CROSS_FREE = ("corr_initial", "corr_final")

# Parameters that are volatilities and are therefore typed in points.
_PERCENT_KNOBS = ("initial_vol", "long_term_vol", "short_addon", "rate_vol")

#: The range the backbone's mean reversion is fitted in, and the one bound
#: here that is a **marking judgement rather than a property of the model**.
#: Read as a half-life -- ``ln(2) / k`` in years -- 1.5 to 6.5 is a curve that
#: closes half the gap between the front and the back end in five weeks to
#: five and a half months, which is the shape a desk marks.  Outside it the
#: fit is not wrong, it is describing a term structure nobody would mark:
#: below the floor the curve is nearly a straight line to the back end, and
#: above the ceiling the whole shape sits in the first month, where
#: ``short_addon`` already lives.  The ceiling is 6.5 and not 6 because
#: AUDUSD and NZDUSD are marked at 6.5 in ``files/vol_marks.xlsx``: a default
#: range that excludes marks the desk has actually made is a range that
#: argues with its own book on the first morning.
#:
#: It is deliberately a *fit* bound and not a bound on the mark.  A value
#: typed into the marking screen's parameter box is a mark somebody made on
#: purpose and is left exactly as typed; what this constrains is where a cold
#: fit through a target curve is allowed to land, and a fit that comes to rest
#: on it says so in its warnings rather than reporting a shape as fitted.
MEAN_REVERSION_RANGE = (1.5, 6.5)

# Bounds for every knob, in decimals.  ``short_addon`` is held non-negative:
# it is the front-end lift, and a front end *below* the backbone is what
# ``initial_vol`` under ``long_term_vol`` already expresses.  Letting both do
# it makes the pair degenerate.
_BOUNDS = {
    "initial_vol": (1e-4, 3.0), "long_term_vol": (1e-4, 3.0),
    "mean_reversion": MEAN_REVERSION_RANGE, "short_addon": (0.0, 0.5),
    "short_decay": (0.0, 500.0),
    "corr_initial": (-0.999, 0.999), "corr_final": (-0.999, 0.999), "corr_decay": (0.0, 200.0),
}
#: Sweep nodes for the mean reversion, taken from the range itself so the two
#: cannot drift apart.  A node the polish is not allowed to reach can still win
#: the sweep on cost and is then clipped into the bound, which is a different
#: curve from the one that was measured.
def reversion_nodes(rng: tuple[float, float] = MEAN_REVERSION_RANGE) -> tuple[float, ...]:
    lo, hi = rng
    return tuple(lo + i * (hi - lo) / 4.0 for i in range(5))

def check_reversion_range(value) -> tuple[float, float]:
    """Read a mean-reversion range somebody typed, or refuse it with the reason.

    One reader for the panel, the CLI and the fit, so a range that is legal on
    the screen cannot be illegal underneath it.  The floor is held above zero:
    at zero the backbone is a flat line at ``initial_vol`` and the whole term
    structure is whatever ``short_addon`` says, which is a different model
    wearing the same parameters.
    """
    try:
        lo, hi = (float(v) for v in value)
    except (TypeError, ValueError):
        raise ValueError("the mean-reversion range is two numbers, a floor and a "
                         "ceiling") from None
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError("the mean-reversion range must be two finite numbers")
    if lo <= 0.0:
        raise ValueError(f"the mean-reversion floor must be above zero, not {lo:g}; at zero "
                         f"the backbone is flat and the term structure is all short_addon")
    if hi <= lo:
        raise ValueError(f"the mean-reversion ceiling {hi:g} must be above its floor {lo:g}")
    return (lo, hi)


#: How far off its targets a fit has to be before a parameter sitting on its
#: bound is reported as limiting the shape.  A hundredth of a basis point of
#: volatility: far below anything a market quotes, so a fit that is inside it
#: reached its targets and the bound held nothing back.
_BOUND_BINDING_RMSE = 1e-5

_SHIFT_BOUNDS = {"rho25": (-1.5, 1.5), "rho10": (-1.5, 1.5),
                 "slog25": (-1.0, 1.0), "slog10": (-1.0, 1.0)}


# ===========================================================================
# knobs
# ===========================================================================


class _Knobs:
    """Read and write a curve's free parameters without caring which kind it is.

    A plain pair's level lives in its backbone; a cross's level lives in its
    legs and only its correlation is this curve's to mark.  Everything above
    this class works in names and numbers and never branches on the two.
    """

    def __init__(self, atm):
        self.atm = atm
        self.is_cross = isinstance(atm, CrossAtmCurve)
        self.available = CROSS_KNOBS if self.is_cross else BACKBONE_KNOBS
        self.default_free = DEFAULT_CROSS_FREE if self.is_cross else DEFAULT_BACKBONE_FREE

    def get(self) -> dict[str, float]:
        p = self.atm.params
        out = {"short_addon": p.short_addon, "short_decay": p.short_decay}
        if self.is_cross:
            c = self.atm.correlation
            out.update(corr_initial=c.initial, corr_final=c.final, corr_decay=c.decay)
        else:
            out.update(initial_vol=p.initial_vol, long_term_vol=p.long_term_vol,
                       mean_reversion=p.mean_reversion, rate_vol=p.rate_vol,
                       rate_corr=p.rate_corr)
        return out

    def set(self, values: dict[str, float]) -> list[str]:
        problems: list[str] = []
        if self.is_cross:
            c = self.atm.correlation
            problems += self.atm.set_correlation(
                values.get("corr_initial", c.initial), values.get("corr_final", c.final),
                values.get("corr_decay", c.decay))
        backbone = {k: v for k, v in values.items() if k in BACKBONE_KNOBS
                    and (not self.is_cross or k in ("short_addon", "short_decay"))}
        if backbone:
            problems += self.atm.set_params(**backbone)
        return problems


# ===========================================================================
# 1. the at-the-money curve
# ===========================================================================


@dataclass(frozen=True)
class CurveTarget:
    tenor: str
    t: float
    vol: float
    source: str = ""


@dataclass(frozen=True)
class CurveFit:
    """A backbone (or correlation) put through a target term structure."""

    before: dict[str, float]
    after: dict[str, float]
    free: tuple[str, ...]
    targets: tuple[CurveTarget, ...]
    achieved_before: tuple[float, ...]
    achieved_after: tuple[float, ...]
    rmse: float
    max_error: float
    max_error_tenor: str
    converged: bool
    message: str
    evaluations: int
    seconds: float
    warnings: tuple[str, ...] = ()


def _curve_vols(atm, ts: list[float]) -> list[float]:
    """Curve volatility at each of ``ts``, ignoring tenor overwrites.

    Sorted and accumulated segment by segment rather than integrated from zero
    nine times over.  Variance is additive over the day grid -- the invariant
    the whole integrator is built on -- so this is the same number to 2e-16
    and roughly three times less work, which matters inside a fit.
    """
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    out = [0.0] * len(ts)
    acc, prev = 0.0, 0.0
    for i in order:
        t = ts[i]
        if t <= prev:
            out[i] = math.sqrt(acc / t) if t > 0 and acc > 0 else 0.0
            continue
        acc += atm.integrated_variance(t, prev)
        prev = t
        out[i] = math.sqrt(acc / t) if acc > 0 else 0.0
    return out


def fit_atm_curve(atm, targets: list[CurveTarget], *, free: tuple[str, ...] | None = None,
                  weights: list[float] | None = None,
                  reversion_range: tuple[float, float] | None = None) -> CurveFit:
    """Fit the free curve parameters through a target term structure.

    The fit runs on a copy, so the curve handed in is untouched whatever
    happens; the caller applies ``CurveFit.after`` when it wants to keep it.

    There is no starting guess.  The two level parameters are read straight off
    the shortest and longest targets, and the two shape parameters -- which
    have no such reading -- are swept over the range that matters before any
    polishing.  A local minimum in mean reversion is easy to land in from a
    bad start and impossible to see afterwards.
    """
    # The mean-reversion range is a marking judgement (MEAN_REVERSION_RANGE),
    # so it is the one bound a caller may move.  Everything downstream reads
    # `bounds` and `seeds` rather than the module constants, so the sweep nodes
    # and the polish can never be taken from two different ranges.
    rev = check_reversion_range(reversion_range) if reversion_range is not None \
        else MEAN_REVERSION_RANGE
    bounds = {**_BOUNDS, "mean_reversion": rev}
    reversion_seeds = reversion_nodes(rev)

    knobs = _Knobs(atm)
    free = tuple(free) if free is not None else knobs.default_free
    unknown = [f for f in free if f not in knobs.available]
    if unknown:
        raise ValueError(
            f"{', '.join(unknown)} cannot be fitted on a "
            f"{'cross' if knobs.is_cross else 'single'} pair curve; the knobs here are "
            f"{', '.join(knobs.available)}")
    if not free:
        raise ValueError("no curve parameter was left free, so there is nothing to fit")
    targets = sorted(targets, key=lambda x: x.t)
    if len(targets) < len(free):
        raise ValueError(
            f"{len(targets)} target(s) cannot determine {len(free)} free parameter(s) "
            f"({', '.join(free)}); pin more tenors or free fewer parameters")

    work = copy.deepcopy(atm)
    work_knobs = _Knobs(work)
    before = knobs.get()
    ts = [x.t for x in targets]
    goals = np.array([x.vol for x in targets], dtype=float)
    w = np.ones(len(targets)) if weights is None else np.asarray(weights, dtype=float)
    if w.shape != goals.shape or np.any(w <= 0):
        raise ValueError("target weights must be positive and one per target")
    w = w / float(np.mean(w))

    calls = 0

    def achieved(values: dict[str, float]) -> np.ndarray | None:
        nonlocal calls
        calls += 1
        if work_knobs.set(values):
            return None
        try:
            return np.array(_curve_vols(work, ts), dtype=float)
        except (ValueError, ArithmeticError):
            return None

    def residuals(x: np.ndarray) -> np.ndarray:
        got = achieved({k: float(v) for k, v in zip(free, x)})
        if got is None or not np.all(np.isfinite(got)):
            return np.full(len(targets), 1e3)
        return w * (got - goals)

    lo = np.array([bounds[k][0] for k in free], dtype=float)
    hi = np.array([bounds[k][1] for k in free], dtype=float)

    # -- starting points: levels read off the data, shapes swept -----------
    short_vol, long_vol = float(goals[0]), float(goals[-1])
    seeds: list[dict[str, float]] = []
    def seeded(candidate: dict) -> dict:
        """A sweep node may only move a *free* parameter.

        Sweeping a frozen one and then keeping whatever the best node happened
        to hold would change a parameter the caller deliberately pinned, which
        is the silent-edit failure this project exists to remove.
        """
        return {**before, **{k: v for k, v in candidate.items() if k in free}}

    if knobs.is_cross:
        # The level of a cross is its legs'; the shape this curve owns is the
        # correlation's decay, so that is what gets swept.
        for decay in (0.25, 1.0, 4.0, 16.0, 64.0):
            for front in (10.0, 50.0, 200.0):
                seeds.append(seeded({"corr_decay": decay, "short_decay": front}))
    else:
        for reversion in reversion_seeds:
            for decay in (10.0, 50.0, 200.0):
                seeds.append(seeded({
                    "mean_reversion": reversion, "short_decay": decay,
                    "initial_vol": short_vol, "long_term_vol": long_vol,
                    "short_addon": max(before.get("short_addon", 0.0), 0.0)}))

    t0 = time.perf_counter()
    best_seed, best_cost = None, math.inf
    for seed in seeds:
        got = achieved(seed)
        if got is None or not np.all(np.isfinite(got)):
            continue
        cost = float(np.sum((w * (got - goals)) ** 2))
        if cost < best_cost:
            best_cost, best_seed = cost, seed
    if best_seed is None:
        raise ConvergenceError(
            f"no admissible curve exists anywhere on the sweep for targets "
            f"{goals.min():.4%}-{goals.max():.4%}; the parameters cannot reach them at all")

    x0 = np.clip(np.array([best_seed[k] for k in free], dtype=float), lo + 1e-12, hi - 1e-12)
    try:
        sol = least_squares(residuals, x0, bounds=(lo, hi), xtol=1e-13, ftol=1e-13,
                            gtol=1e-13, max_nfev=600)
        values = {**best_seed, **{k: float(v) for k, v in zip(free, sol.x)}}
        ok = bool(sol.success)
        why = "converged" if ok else f"least-squares stopped: {sol.message}"
    except Exception as exc:  # noqa: BLE001 - fall back to the sweep, but say so
        values, ok = best_seed, False
        why = f"polish failed ({type(exc).__name__}: {exc}); reporting the best sweep node"

    got = achieved(values)
    if got is None:
        raise ConvergenceError(f"the fitted parameters are not a valid curve: {values}")
    seconds = time.perf_counter() - t0

    err = got - goals
    j = int(np.argmax(np.abs(err)))
    rmse = float(math.sqrt(np.mean((w * err) ** 2)))

    # The "before" achieved curve, measured on the untouched original.
    achieved_before = _curve_vols(atm, ts)

    warnings: list[str] = []
    if rmse > 0.0015:
        warnings.append(
            f"the curve cannot pass through these targets: weighted RMSE {rmse * 100:.3f} vol "
            f"points, worst {err[j] * 100:+.3f} at {targets[j].tenor}. A five-parameter backbone "
            f"has one hump in it; a target curve with two does not fit, and forcing it here only "
            f"spreads the error. Pin those tenors on the marking screen instead")
    # A parameter resting on its bound is only worth saying when the bound is
    # actually holding the fit back.  Landing on it and hitting every target
    # anyway limits nothing, and the claim below would be false: EURUSD is
    # marked at exactly 6.0, the top of MEAN_REVERSION_RANGE, so an ungated
    # check warns on every refit of the curve the desk already has -- and a
    # warning that fires when nothing is wrong is one nobody reads.
    for k in free:
        v = values[k]
        span = bounds[k][1] - bounds[k][0]
        if rmse <= _BOUND_BINDING_RMSE:
            break
        if min(abs(v - bounds[k][0]), abs(v - bounds[k][1])) < 1e-6 * max(span, 1.0):
            why_bound = (
                f" That bound is a marking judgement, not a property of the model: "
                f"the backbone is fitted inside {rev[0]:g}-{rev[1]:g} because a curve "
                f"outside it is one nobody marks. Widen the range on the fit panel to let "
                f"the fit go there, or type the value on the marking screen, which is not "
                f"bounded."
                if k == "mean_reversion" else "")
            warnings.append(
                f"{k} came to rest on its bound at {v:.6g}; the targets want more than the "
                f"parameter can give, so the shape is being limited rather than fitted."
                + why_bound)
    if len(targets) == len(free):
        warnings.append(
            f"{len(free)} free parameters against {len(targets)} targets is an exact solve, not "
            f"a fit: the residuals will be zero whatever the targets say and are no evidence the "
            f"shape is right")
    return CurveFit(
        before=before, after={k: values[k] for k in knobs.available},
        free=free, targets=tuple(targets),
        achieved_before=tuple(achieved_before), achieved_after=tuple(float(v) for v in got),
        rmse=rmse, max_error=float(err[j]), max_error_tenor=targets[j].tenor,
        converged=ok, message=why, evaluations=calls, seconds=seconds,
        warnings=tuple(warnings),
    )


# ===========================================================================
# 2. evaluating a quote on the surface
# ===========================================================================


# How close the interpolation has to sit to its own anchors before the fit is
# allowed to read the anchors instead of building it.  A ten-millionth of a
# volatility point: far below anything a market quotes, far above the delta
# solve's own fixed-point tolerance.
ANCHOR_TOLERANCE = 1e-9


def anchor_wing(method: str, delta: float | None) -> int | None:
    """Which SABR wing *should* reproduce the interpolated smile at ``delta``.

    The interpolators are built through five anchor points taken off the two
    SABR wings, and each is meant to reproduce the anchors it was built
    through: SVI has five parameters for five points, vanna-volga reprices its
    own three by construction, and the SABR methods simply *are* one wing.
    Where that holds, the wing and the interpolation are the same number and
    the fit can skip a 19ms SVI solve per expiry per evaluation without
    approximating anything.

    It does not always hold.  SVI here is **arbitrage constrained**, so five
    parameters through five points is not a free interpolation: when the marked
    anchors imply a butterfly arbitrage the constrained fit cannot pass through
    them and lands up to a tenth of a volatility point away.  On this
    workbook that is nine slices in fifty-two -- USDCNY, a managed pair whose
    marked wings are the least well behaved, misses by 0.15 points at a week.

    So this function says only where the shortcut is *plausible*.  Whether it
    is actually exact is measured per expiry by :func:`anchor_gap` and checked
    before the fit uses it; ``None`` means there is no shortcut at all.
    """
    if method in ("SABR25", "SABR10"):
        return 25 if method == "SABR25" else 10
    if delta is None:
        return None
    if abs(delta - 0.25) < 1e-12 and method in ("SVI", "VV25"):
        return 25
    if abs(delta - 0.10) < 1e-12 and method in ("SVI", "VV10"):
        return 10
    return None


class Evaluator:
    """Reads quote values off a surface, caching each expiry within one pass.

    Built fresh for every objective evaluation: the parameters move underneath
    it, so a cache that outlived one pass would be a stale-number bug of
    exactly the kind this project exists to remove.

    By default it is **exact**: every delta goes through the interpolated
    smile, which is the number the pricing screen shows.  ``fast_at`` names
    the expiries where :func:`anchor_gap` has *measured* the wings and the
    interpolation to agree, and only those are allowed the shortcut.  An
    unverified fast path is how a fit ends up marking to a smile the rest of
    the tool does not use.
    """

    def __init__(self, surface, method: str, cut: str, fast_at: frozenset | None = None):
        self.s = surface
        self.method = method or surface.method
        self.cut = cut
        self.fast_at = fast_at or frozenset()
        self._atm: dict[float, float] = {}
        self._wing: dict[tuple[float, int], SabrParams] = {}
        self.slices_built = 0

    def atm(self, dt, t: float) -> float:
        hit = self._atm.get(t)
        if hit is None:
            hit = float(self.s.atm.cut_vol(dt, self.cut))
            if hit <= 0:
                raise ValueError(
                    f"{self.s.pair}: the at-the-money volatility at {dt:%Y-%m-%d} is zero. "
                    f"An expiry inside today's volatility day has no volatility days in it")
            self._atm[t] = hit
        return hit

    def wing(self, dt, t: float, which: int) -> SabrParams:
        key = (t, which)
        hit = self._wing.get(key)
        if hit is None:
            atm_vol = self.atm(dt, t)
            p = self.s.params_at(t)
            rho, slog = p[f"rho{which}"], p[f"slog{which}"]
            nu = slog / math.sqrt(t)
            alpha = sabr.alpha_from_atm(
                atm_vol, black.atm_strike(1.0, atm_vol, t, self.s.conv), rho, nu, t, 1.0)
            hit = SabrParams(alpha=alpha, rho=rho, volvol=nu, t=t, f=1.0)
            self._wing[key] = hit
        return hit

    def delta_vol(self, dt, t: float, delta: float, is_call: bool) -> float:
        """Volatility at a delta, off the wing when that was verified, else the slice."""
        which = anchor_wing(self.method, delta)
        signed = abs(delta) if is_call else -abs(delta)
        if which is not None and round(t, 10) in self.fast_at:
            _, vol = sabr.smile_strike_and_vol(self.wing(dt, t, which), signed, t, is_call,
                                               self.s.conv)
            return float(vol)
        self.slices_built += 1
        return float(self.s.slice_at(dt, self.method, self.cut).strike_from_delta(signed, is_call)[1])

    def strike_vol(self, dt, t: float, ratio: float) -> float:
        self.slices_built += 1
        return float(self.s.vol(ratio, dt, self.method, self.cut))

    def strangle(self, dt, t: float, delta: float) -> float:
        self.slices_built += 1
        return float(self.s.strangle(dt, delta, self.method, self.cut))

    # -- the instruments ---------------------------------------------------
    def leg_value(self, kind: str, q: MarketQuote, dt, t: float,
                  forward: float | None) -> float:
        if kind == "atm":
            return self.atm(dt, t)
        if kind == "rr":
            return (self.delta_vol(dt, t, q.delta, True)
                    - self.delta_vol(dt, t, q.delta, False))
        if kind == "fly":
            if q.fly_kind == "market":
                return self.strangle(dt, t, q.delta)
            return 0.5 * (self.delta_vol(dt, t, q.delta, True)
                          + self.delta_vol(dt, t, q.delta, False)) - self.atm(dt, t)
        if kind == "outright":
            if q.strike is not None:
                if forward is None:
                    raise ValueError(
                        f"a strike of {q.strike:g} needs an outright forward to become a "
                        f"moneyness, and there is no forward feed for {self.s.pair}. Load a feed, "
                        f"or quote the option by its delta")
                return self.strike_vol(dt, t, q.strike / forward)
            return self.delta_vol(dt, t, q.delta, bool(q.is_call))
        raise ValueError(f"cannot value a {kind!r} quote")

    def value(self, q: MarketQuote, expiries: dict, forwards: dict) -> float:
        """The model's mid for one quote, in decimals.

        A structure is the signed sum of its legs, each leg valued exactly as
        the plain instrument it is; a premium quote is valued as the
        volatility at its strike, because the market side of it has already
        been turned into a volatility by :func:`premiums_as_vols` and the fit
        compares like with like.
        """
        if q.instrument == "structure":
            total = 0.0
            for leg in q.legs:
                dt, t = expiries[_key(leg.expiry)]
                total += leg.weight * self.leg_value(leg.kind, leg, dt, t,
                                                     forwards.get(_key(leg.expiry)))
            return total
        if q.instrument == "spread":
            near_dt, near_t = expiries[_key(q.expiry)]
            far_dt, far_t = expiries[_key(q.expiry_far)]
            kind = q.leg or "atm"
            return (self.leg_value(kind, q, far_dt, far_t, forwards.get(_key(q.expiry_far)))
                    - self.leg_value(kind, q, near_dt, near_t, forwards.get(_key(q.expiry))))
        dt, t = expiries[_key(q.expiry)]
        return self.leg_value(q.instrument, q, dt, t, forwards.get(_key(q.expiry)))


def anchor_gap(surface, dt, t: float, method: str, cut: str) -> float:
    """How far the interpolated smile sits from the wings at their own anchors.

    Zero to rounding when the interpolation passes through its anchors, which
    is the ordinary case; up to a tenth of a volatility point when an
    arbitrage-constrained fit could not.  Building the slice costs one SVI
    solve, which is why this is measured once per expiry rather than assumed.
    """
    sl = surface.slice_at(dt, method, cut)
    ev = Evaluator(surface, method, cut, fast_at=frozenset({round(t, 10)}))
    worst = 0.0
    for delta, is_call in ((0.25, True), (0.25, False), (0.10, True), (0.10, False)):
        if anchor_wing(method, delta) is None:
            continue
        fast = ev.delta_vol(dt, t, delta, is_call)
        slow = float(sl.strike_from_delta(delta if is_call else -delta, is_call)[1])
        worst = max(worst, abs(fast - slow))
    return worst


def verified_fast_expiries(surface, quotes, expiries, method: str,
                           cut: str) -> tuple[frozenset, list[str]]:
    """Which expiries the fit may read off the wings, measured rather than assumed."""
    wanted = set()
    for q in quotes:
        for value, delta in _leg_deltas(q):
            if anchor_wing(method, delta) is not None:
                wanted.add(_key(value))
    ok, notes = set(), []
    for key in sorted(wanted):
        dt, t = expiries[key]
        try:
            gap = anchor_gap(surface, dt, t, method, cut)
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            notes.append(f"{key}: could not be checked against its own anchors ({exc}); "
                         f"the full interpolation is being used")
            continue
        if gap <= ANCHOR_TOLERANCE:
            ok.add(round(t, 10))
        else:
            notes.append(
                f"{key}: the {method} smile does not pass through its own anchor points -- it "
                f"misses by {gap * 100:.4f} volatility points. The arbitrage constraint is "
                f"binding, so the wings and the smile you price on are not the same curve "
                f"there. The fit is using the full interpolation for this expiry, which is "
                f"slower and correct")
    return frozenset(ok), notes


def _leg_deltas(q) -> list[tuple]:
    """``(expiry, delta)`` for every leg of a quote, structures included."""
    if q.instrument == "structure":
        return [(leg.expiry, leg.delta) for leg in q.legs]
    return [(value, q.delta) for value in (q.expiry, q.expiry_far) if value is not None]


def _row_expiry(q):
    """The expiry a row is filed under: the far leg of a spread or structure."""
    return q.expiry_far if q.instrument in ("spread", "structure") and q.expiry_far is not None \
        else q.expiry


def informative_params(quotes, method: str) -> tuple[set, list[str]]:
    """Which smile parameters the pasted quotes can actually determine.

    A 25-delta quote reads off the 25-delta anchor, and that anchor is built
    from ``rho25`` and ``slog25`` alone -- the ten-delta parameters do not
    enter it.  Leaving them free anyway does not make the fit better informed;
    it makes the objective flat in two directions, and the optimiser then
    spends its whole budget wandering along that plateau chasing the
    tie-breakers, each step a fresh interpolation solve per expiry.

    Quotes that do not sit on an anchor -- an absolute strike, an odd delta, a
    market strangle read through the interpolation -- depend on the shape of
    the whole slice and so inform all four.  This is the same rule the curve
    fit applies to its targets: a fit may not have more free parameters than
    the market gave it.
    """
    informed: set = set()
    reasons: list[str] = []
    for q in _flatten_legs(quotes):
        kind = q.leg if q.instrument == "spread" else q.instrument
        if kind == "atm":
            continue
        wing = anchor_wing(method, q.delta) or (
            25 if q.delta is not None and abs(q.delta - 0.25) < 1e-9 else
            10 if q.delta is not None and abs(q.delta - 0.10) < 1e-9 else None)
        if wing is None:
            # No delta to hang it on -- an absolute strike or an odd delta reads
            # the interpolation wherever it lands, so it informs everything.
            informed |= set(PARAM_NAMES)
            reasons.append(f"{q.describe()} has no anchor delta, so it depends on the "
                           f"whole slice")
            continue
        informed |= {f"rho{wing}", f"slog{wing}"}
        if kind == "fly" and q.fly_kind == "market":
            reasons.append(
                f"{q.describe()} is a market strangle, so it is read through the interpolation "
                f"and depends weakly on the far wing as well as on its own; it is being counted "
                f"against the {wing}-delta parameters it actually moves")
    return informed, reasons


def _key(expiry) -> str:
    return str(expiry)


def _flatten_legs(quotes) -> list:
    """Every plain instrument the quotes contain: a structure's legs, each as
    a quote of its own, so a rule written for quotes reads them unchanged."""
    out = []
    for q in quotes:
        if q.instrument != "structure":
            out.append(q)
            continue
        for leg in q.legs:
            out.append(MarketQuote(instrument=leg.kind, expiry=leg.expiry, bid=0.0, ask=0.0,
                                   delta=leg.delta, strike=leg.strike, is_call=leg.is_call,
                                   fly_kind=leg.fly_kind, line=q.line, raw=q.raw))
    return out


def _levels_for(book, pair: str, expiries: dict) -> dict:
    """``Book.market_level_for`` at every expiry: spot, forward and pip for the
    premium conversions.  One lookup, the same one the forwards came from --
    and read on each expiry's own settlement date, as a forward is."""
    return {key: book.market_level_for(pair, dt.date())
            for key, (dt, _) in expiries.items()}


def side_from_moneyness(strike, forward) -> bool:
    """Which side a strike names when the line did not say.

    Only ever asked of a **live** option -- one dealt without its delta hedge,
    which is what makes it a low-delta option -- so it is the out-of-the-money
    one: above the forward it is the call, below it the put.  At the forward
    either answer is the same option to within the smile, and the call is
    taken, as the pricing screen's own strike box does.
    """
    return float(strike) >= float(forward)


def _side_note(q, is_call: bool, forward: float) -> str:
    return (f"no side on the line, so it was read from the moneyness: the strike "
            f"{q.strike:g} is {'above' if is_call else 'below'} the forward {forward:g}, "
            f"so this is the {'call' if is_call else 'put'} -- a live option is the "
            f"out-of-the-money one")


def _side_warning(q, is_call: bool, forward: float, vol: float, t: float) -> str:
    """Said when a side read off the moneyness was read off a near-the-money
    strike: the rule is only as good as the option being far out of it, and a
    45-delta option is one the run should have named a side for."""
    try:
        d = abs(float(black.delta(forward, q.strike, vol, t, is_call)))
    except (ValueError, ArithmeticError):
        return ""
    if d <= 0.40:
        return ""
    return (f"the side was read from the moneyness, but at {d * 100:.0f} delta this option is "
            f"not the low-delta one a live price usually is; write 'call' or 'put' if the "
            f"other side was meant")


def premiums_as_vols(quotes, expiries: dict, levels: dict, pair: str) -> tuple[list, list[str]]:
    """Every premium quote in the run, as the volatility two-way it implies.

    A premium is turned into a volatility **once, here**, so the fit, the
    residuals and the market table all read one unit.  The price is brought
    to the term currency per unit of base -- pips through the pip size, a per
    cent of the base notional through the spot it is paid at -- and Black-76
    is inverted against the feed's forward at the quote's own expiry.  No
    discount curve anywhere in this package, so the volatility reads a touch
    low on a long-dated option, and the row says so.  A quote that cannot be
    converted keeps its place with the reason and is not used by the fit.

    Returns the quotes with the premiums replaced, and a parallel list of
    reasons, empty where the quote is usable.
    """
    out, errors = [], []
    for q in quotes:
        if q.quote_kind != "premium":
            out.append(q)
            errors.append("")
            continue
        if q.instrument == "structure":
            out.append(q)
            errors.append("a premium on a multi-leg structure cannot be turned into one "
                          "volatility; quote the legs in volatility, or the structure as a "
                          "volatility spread")
            continue
        level = levels.get(_key(q.expiry)) or {}
        fwd, spot, pip = level.get("forward"), level.get("spot"), level.get("pip")
        if not level.get("feed") or fwd is None:
            out.append(q)
            errors.append(f"a premium needs the forward to become a volatility, and there is "
                          f"no forward feed for {pair}")
            continue
        _, t = expiries[_key(q.expiry)]
        # A live line may carry no side: the parse leaves it open because the
        # forward it is read against lives here and not there.
        side_note = ""
        if q.is_call is None and q.strike is not None:
            q = MarketQuote(**{**vars(q), "is_call": side_from_moneyness(q.strike, fwd)})
            side_note = _side_note(q, bool(q.is_call), fwd)
        try:
            if q.premium_unit == "pips":
                if not pip:
                    raise ValueError("the feed gives no pip size for this pair")
                # The feed's pip is a divisor: 10000 pips to the unit on EURUSD.
                factor, how = 1.0 / pip, f"/ {pip:g} pips per unit"
            elif q.premium_unit == "pct":
                base_level = spot if spot else fwd
                factor, how = base_level / 100.0, (f"% of base x {'spot' if spot else 'forward'} "
                                                   f"{base_level:g}")
            else:
                factor, how = 1.0, "term currency per unit of base"
            vols = [black.implied_vol(px * factor, fwd, q.strike, t, bool(q.is_call))
                    for px in (q.bid, q.ask)]
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            out.append(q)
            errors.append(f"the premium could not be inverted to a volatility: {exc}")
            continue
        lo, hi = sorted(vols)
        unit = {"pips": "pips", "pct": "%", "price": ""}[q.premium_unit or "price"]
        extra = ()
        if side_note:
            warn = _side_warning(q, bool(q.is_call), fwd, 0.5 * (lo + hi), t)
            extra = (side_note,) + ((warn,) if warn else ())
        out.append(MarketQuote(**{**vars(q), "bid": lo, "ask": hi, "quote_kind": "vol",
                                  "premium_unit": None, "notes": q.notes + extra + (
            f"premium {q.bid:g}/{q.ask:g} {unit} ({how}) inverted against the forward "
            f"{fwd:g}: {lo * 100:.3f}/{hi * 100:.3f} vol. Undiscounted, so a touch low on a "
            f"long-dated option",)}))
        errors.append("")
    return out, errors


def resolve_expiries(clock, quotes, pair: str = "", calendars=None) -> dict[str, tuple]:
    """Map every expiry mentioned in a run to a (datetime, years) pair.

    A tenor is resolved **on the pair's calendar** -- through the spot date and
    the settlement date, as the market does -- so a run that quotes ``1M``
    lands on the same expiry the marking screen and the pricing screen put it
    on.  Resolving it out of a nominal year fraction instead put the quote a
    day or so away from the pillar it was quoting, and the fit then residualed
    against a slice nobody had marked.  A written date is taken as it stands.

    Without a pair and a calendar -- a paste read for its widths alone, with
    no book behind it -- the nominal length is all there is, and it is used.
    """
    out: dict[str, tuple] = {}
    for q in quotes:
        for value in q.expiries():
            if value is None or _key(value) in out:
                continue
            if isinstance(value, str):
                if pair and calendars is not None:
                    when = calendars.expiry_date(pair, value, clock.now.date())
                    dt = datetime.combine(when, datetime.min.time()).replace(tzinfo=UTC)
                    out[_key(value)] = (dt, clock.years_to(dt))
                else:
                    t = tenor_to_years(value)
                    out[_key(value)] = (clock.datetime_from_years(t), t)
            else:
                dt = clock.coerce_datetime(value)
                out[_key(value)] = (dt, clock.years_to(dt))
    return out


# ===========================================================================
# 3. the fine tune
# ===========================================================================


@dataclass(frozen=True)
class TuneResult:
    before: dict[str, float]
    after: dict[str, float]
    free: tuple[str, ...]
    inside_before: int
    inside_after: int
    worst_before: float
    worst_after: float
    converged: bool
    message: str
    evaluations: int
    slices: int
    seconds: float
    warnings: tuple[str, ...] = ()


def _hinge(value: float, bid: float, ask: float) -> float:
    """Signed distance outside the quoted market; zero anywhere inside it."""
    if value < bid:
        return value - bid
    if value > ask:
        return value - ask
    return 0.0


def tune_smile_shifts(surface, quotes, expiries, forwards, *, method: str, cut: str,
                      free: tuple[str, ...] = PARAM_NAMES, mid_pull: float = 0.05,
                      prior_pull: float = 0.02, max_nfev: int = 300) -> TuneResult:
    """Move the four smile parameters until the quoted wings are satisfied.

    Mutates ``surface.param_shifts``; the caller restores them when it is only
    reporting.  The at-the-money level is *not* free here -- it is set by the
    curve fit above, which is the order a desk marks in (level first, then
    wings) and which also keeps a level quote and a wing quote from fighting
    over the same vol point.  Quotes that depend on both still constrain the
    wings; they simply cannot move the level.

    The objective is a hinge: zero anywhere inside the quoted bid and offer,
    and the distance to the nearer side outside it.

    ``mid_pull`` and ``prior_pull`` are small on purpose.  The hinge has a flat
    bottom, so without them any shift that lands inside every market would do
    and the answer would depend on where the optimiser happened to stop; with
    them the answer is the smallest adjustment that satisfies the market and
    then sits nearest the quoted mids.

    Both are also *scaled to the market they are competing with*, which is the
    part that is easy to get wrong.  The hinge and the mid pull are already in
    volatility, but a parameter shift is not: a shift of 0.1 against a hinge of
    0.001 means a raw prior weight of 0.02 is not a tie-breaker at all, it is
    twenty times the violation it is supposed to defer to -- and the fit stops
    short of a market it could reach while reporting that it converged.  The
    prior is therefore multiplied by the market's own half width.

    The search may read an expiry off the SABR wings instead of solving the
    interpolation, but only where the two have been *measured* to agree, and
    the answer is always re-read through the interpolation afterwards.  If the
    fitted shifts have moved the smile somewhere the interpolation can no
    longer follow the wings, the whole fit is run again on the slow, exact
    path rather than the drift being reported and left in.
    """
    free = tuple(f for f in free if f in PARAM_NAMES)
    if not free:
        raise ValueError(f"no smile parameter left free; expected some of {PARAM_NAMES}")
    if not quotes:
        raise ValueError("no quote constrains the wings, so there is nothing to fine tune")

    informed, why_all = informative_params(quotes, method)
    dropped = [f for f in free if f not in informed]
    free = tuple(f for f in free if f in informed)
    pinned_note = ""
    if dropped:
        pinned_note = (
            f"{', '.join(dropped)} were left where they are: nothing in the paste reads off the "
            f"{'/'.join(sorted({d[-2:] for d in dropped}))}-delta anchor, so freeing them would "
            f"only make the objective flat in those directions")
    if not free:
        raise ValueError(
            f"none of {', '.join(PARAM_NAMES)} is both free and informed by the paste; the "
            f"quotes read off the {', '.join(sorted(informed)) or 'no'} anchor(s)")
    if len(free) > len(quotes):
        raise ValueError(
            f"{len(quotes)} wing quote(s) cannot determine {len(free)} free smile parameter(s) "
            f"({', '.join(free)}"
            + (f"; {'; '.join(why_all[:2])}" if why_all else "")
            + "). Quote more of the smile, or pin parameters on the panel")

    before = {k: float(surface.param_shifts.get(k, 0.0)) for k in PARAM_NAMES}
    bids = np.array([q.bid for q in quotes], dtype=float)
    asks = np.array([q.ask for q in quotes], dtype=float)
    mids = 0.5 * (bids + asks)
    widths = asks - bids
    live = widths[widths > 0]
    # The scale the prior is expressed in.  A run of choice prices has no width
    # to borrow, so a thousandth of the typical quote stands in for one.
    prior_scale = float(np.median(live) / 2.0) if live.size else max(
        float(np.median(np.abs(mids))) * 1e-3, 1e-6)

    lo = np.array([_SHIFT_BOUNDS[k][0] for k in free], dtype=float)
    hi = np.array([_SHIFT_BOUNDS[k][1] for k in free], dtype=float)
    x0 = np.clip(np.array([before[k] for k in free], dtype=float), lo + 1e-12, hi - 1e-12)

    def inside_of(got) -> int:
        return int(sum(1 for v, b, a in zip(got, bids, asks) if b <= v <= a))

    def worst_of(got) -> float:
        return float(np.max(np.abs([_hinge(v, b, a) for v, b, a in zip(got, bids, asks)])))

    counters = {"calls": 0, "slices": 0}

    def solve(fast_at: frozenset, x_start=None):
        counters["calls"] = counters["slices"] = 0
        x_start = x0 if x_start is None else x_start

        def values_at(shifts: dict[str, float]):
            counters["calls"] += 1
            if surface.set_param_shifts({**before, **shifts}):
                return None
            ev = Evaluator(surface, method, cut, fast_at=fast_at)
            try:
                got = np.array([ev.value(q, expiries, forwards) for q in quotes], dtype=float)
            except (ValueError, ArithmeticError, ConvergenceError):
                return None
            counters["slices"] += ev.slices_built
            return got

        def residuals(x: np.ndarray) -> np.ndarray:
            got = values_at({k: float(v) for k, v in zip(free, x)})
            if got is None or not np.all(np.isfinite(got)):
                return np.full(2 * len(quotes) + len(free), 1e3)
            hinge = np.array([_hinge(v, b, a) for v, b, a in zip(got, bids, asks)])
            return np.concatenate([hinge, mid_pull * (got - mids),
                                   prior_pull * prior_scale * x])

        start = values_at({k: before[k] for k in free})
        if start is None:
            raise ConvergenceError(
                "the surface cannot be evaluated at the marks it already carries, so there is "
                "nothing to fine tune from; fix the marks first")
        try:
            # Tolerances are set against what a volatility quote can resolve,
            # not as tight as the solver will go.  On the flat bottom of the
            # hinge the only gradient left is the tie-breakers', so a 1e-12
            # step tolerance grinds through hundreds of evaluations chasing
            # movement a ten-millionth of a vol point wide -- each one a fresh
            # SVI solve per expiry.  1e-9 in a shift is 1e-8 of a vol point.
            sol = least_squares(residuals, x_start, bounds=(lo, hi), xtol=1e-9, ftol=1e-11,
                                gtol=1e-11, max_nfev=max_nfev,
                                diff_step=np.full(len(free), 1e-4))
            after = {**before, **{k: float(v) for k, v in zip(free, sol.x)}}
            ok = bool(sol.success)
            why = "converged" if ok else f"least-squares stopped: {sol.message}"
        except Exception as exc:  # noqa: BLE001
            after, ok = dict(before), False
            why = f"the fine tune failed ({type(exc).__name__}: {exc}); the marks were left alone"
        got = values_at(after)
        if got is None:
            surface.set_param_shifts(before)
            raise ConvergenceError(f"the tuned shifts do not produce a valid surface: {after}")
        return start, after, got, ok, why

    notes: list[str] = []
    fast_at, anchor_notes = verified_fast_expiries(surface, quotes, expiries, method, cut)
    notes.extend(anchor_notes)

    t0 = time.perf_counter()
    start, after, got, ok, why = solve(fast_at)
    calls, slices = counters["calls"], counters["slices"]

    if fast_at:
        # What the desk will price on is the interpolation.  Check the shortcut
        # at the answer rather than trusting it there.
        exact = Evaluator(surface, method, cut)
        try:
            settled = np.array([exact.value(q, expiries, forwards) for q in quotes], dtype=float)
            drift = float(np.max(np.abs(settled - got)))
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            drift = float("inf")
            notes.append(f"the tuned surface could not be re-read through the full "
                         f"interpolation: {exc}")
        if not math.isfinite(drift) or drift > ANCHOR_TOLERANCE:
            notes.append(
                f"the shifts moved the smile into a shape the arbitrage-constrained {method} fit "
                f"can no longer follow the wings through -- they had drifted "
                f"{drift * 100:.4f} volatility points apart at the answer. The fit was run again "
                f"on the full interpolation, which is what the quote sheet prices on")
            # Started from the shortcut's answer rather than from the marks:
            # it is a good point, and the tune is a local refinement by
            # construction, so re-sweeping from scratch buys nothing.
            start, after, got, ok, why = solve(
                frozenset(),
                np.clip(np.array([after[k] for k in free], dtype=float), lo + 1e-12, hi - 1e-12))
            calls += counters["calls"]
            slices += counters["slices"]
        else:
            notes.append(
                f"{len(fast_at)} expiry(ies) were read off the SABR wings rather than through a "
                f"{method} solve, after checking that the two agree there to "
                f"{ANCHOR_TOLERANCE * 100:.0e} volatility points, at the start and at the answer")
    seconds = time.perf_counter() - t0

    inside_before, worst_before = inside_of(start), worst_of(start)
    inside_after, worst_after = inside_of(got), worst_of(got)

    warnings: list[str] = notes + list(surface.shift_warnings())
    if pinned_note:
        warnings.append(pinned_note)
    if inside_after < len(quotes):
        missed = [q.describe() for q, v, b, a in zip(quotes, got, bids, asks) if not b <= v <= a]
        warnings.append(
            f"{len(missed)} of {len(quotes)} wing quote(s) are still outside their market after "
            f"the fine tune ({', '.join(missed[:6])}"
            f"{', ...' if len(missed) > 6 else ''}). A shift moves the whole curve, so quotes that "
            f"disagree across tenors cannot all be met; re-mark those tenors individually on the "
            f"marking screen, or accept that the market is telling you the term structure is wrong")
    if inside_after < inside_before:
        warnings.append(
            f"the fine tune has fewer quotes inside their market than it started with "
            f"({inside_after} against {inside_before}). The mid pull is trading a small miss "
            f"everywhere against a large one somewhere; lower it, or free fewer parameters")
    if calls >= max_nfev:
        warnings.append(
            f"the fine tune used its whole budget of {max_nfev} evaluations and stopped there; "
            f"the answer is where it had got to, not where it was going")
    if worst_after > 0 and calls < max_nfev and inside_after < len(quotes):
        warnings.append(
            f"the pulls toward the quoted mids and the marked shifts are worth "
            f"{mid_pull:g} and {prior_pull:g} of the market's own half width; if the fit is "
            f"stopping short of a quote it could reach, they are what is holding it back")
    return TuneResult(before=before, after=after, free=free,
                      inside_before=inside_before, inside_after=inside_after,
                      worst_before=worst_before, worst_after=worst_after,
                      converged=ok, message=why, evaluations=calls, slices=slices,
                      seconds=seconds, warnings=tuple(warnings))


# ===========================================================================
# 4. skewing the mid
# ===========================================================================

# Instruments a level statement can legitimately shade.  A risk reversal and a
# butterfly are excluded on purpose: a break-even against realized volatility
# and a vega position are both statements about the *level*, and neither says
# anything about where the skew belongs.
_LEVEL_INSTRUMENTS = ("atm", "outright")


@dataclass(frozen=True)
class Skew:
    fair: float
    axe: float
    bank: float
    #: What the tape has been doing, as a lean.  Positive is the market having
    #: paid for volatility, which is a reason to mark *up*: the street is
    #: getting shorter and the next caller is more likely another buyer.
    flow: float
    #: What this caller has done with our prices before, as a lean.  Positive
    #: is a client who lifts, and a buyer coming is a reason to mark *up* --
    #: the same sign as the tape and the opposite of the axe.
    client: float = 0.0
    total: float = 0.0
    capped: bool = False
    cap: float | None = None
    reason: str = ""


def _bucket_days(label: str) -> float | None:
    """A representative number of days for a tenor bucket.

    The geometric middle of the bucket rather than its edge: a bucket that
    reaches from a month to three is answered at about seven weeks, which is
    where its evidence actually sits, and the open-ended one is answered at
    twice its floor rather than at infinity.
    """
    from .synthesis import BUCKETS
    prev = 1.0
    for edge, name in BUCKETS:
        if name == label:
            return 2.0 * prev if edge == float("inf") else math.sqrt(max(prev, 1.0) * edge)
        prev = edge
    return None


def _interp(ts: list[float], values: list[float], t: float) -> float | None:
    if not ts:
        return None
    return float(np.interp(t, ts, values))


def skew_for(q: MarketQuote, t: float, *, half_width: float | None, richness, axe,
             fair_weight: float, axe_weight: float, cap_ratio: float,
             bank_shift: float, flow=None, flow_weight: float = 0.0,
             client=None, client_weight: float = 0.0) -> Skew:
    """How far to lean the mid, and why.

    The first two leans point the same way: a rich market and a long position
    are both reasons to *want to sell*, and you attract a seller's trade by
    shading the price down, not up.

    The third points the other way, and deliberately.  ``flow`` is what the
    printed tape has been doing -- positive when the market has been *paying*
    for volatility -- and that is a reason to mark **up**: the street is
    getting shorter as it sells, and the next caller is more likely to be
    another buyer.  A desk that reads it the other way, as a crowd to fade,
    sets a negative weight and the same arithmetic runs backwards.

    The fourth is the caller's own record, ``client``: ``+1`` for a client who
    has only ever lifted our offer on this instrument, ``-1`` for one who has
    only ever hit our bid, age-weighted between.  It points the way the tape
    does and for the same reason -- a buyer is on the phone -- and unlike the
    other three it is about *this* instrument whatever the instrument is: a
    client who buys the risk reversal is a buyer of the risk reversal, so it is
    the one lean a wing row carries besides the bank's shift.

    All of them are capped together as a fraction of the width, so no lean and
    no combination of leans can walk the price out of the market on its own --
    which would stop being a quote and start being a bet.
    """
    level = q.instrument in _LEVEL_INSTRUMENTS or (
        q.instrument == "spread" and (q.leg or "atm") in _LEVEL_INSTRUMENTS)
    reason = ""
    fair = axe_part = flow_part = client_part = 0.0
    if not level:
        reason = (f"a {q.instrument} is not a level, so neither the fair-value richness, a "
                  f"vega position nor the printed tape says where it should be marked; only "
                  f"the bank's own shift and the client's own record apply")
    else:
        if richness is not None:
            fair = -fair_weight * richness
        if axe is not None and half_width is not None:
            axe_part = -axe_weight * max(-1.0, min(1.0, axe)) * half_width
        elif axe is not None:
            reason = "there is no width for this quote, so the axe has nothing to lean against"
        if flow is not None and half_width is not None:
            flow_part = flow_weight * max(-1.0, min(1.0, flow)) * half_width
        elif flow is not None and not reason:
            reason = "there is no width for this quote, so the tape has nothing to lean against"
    if client is not None and half_width is not None:
        client_part = client_weight * max(-1.0, min(1.0, client)) * half_width
    elif client is not None and not reason:
        reason = ("there is no width for this quote, so the client's record has nothing to "
                  "lean against")
    total = fair + axe_part + flow_part + client_part + bank_shift
    cap = None if half_width is None else cap_ratio * half_width
    capped = False
    if cap is not None and abs(total) > cap:
        total = math.copysign(cap, total)
        capped = True
    return Skew(fair=fair, axe=axe_part, bank=bank_shift, flow=flow_part, client=client_part,
                total=total, capped=capped, cap=cap, reason=reason)


# ===========================================================================
# 5. the two panels: the fit, and the quote
# ===========================================================================

# Fitting and quoting are two jobs and they are two panels, because they fail
# for different reasons, they are asked at different moments, and they read
# different things.
#
# **The fit** reads the market -- a broker run with two-way prices on it --
# and moves the marks: the backbone through a target term structure, then the
# four smile parameters by a curve-wide shift until the quoted wings are
# satisfied.  It reports where the surface sits against every quote it was
# shown and it produces no price at all.
#
# **The quote** reads a list of instruments somebody has asked for, with no
# prices on them, and makes a two-way in each: the model's mid, the bank's
# width round it, and the two leans.  It fits nothing.
#
# They meet at :func:`capture_marks` -- a dictionary of the parameters the fit
# arrived at, which travels back through the browser and is put on the surface
# for the length of one quote run.  That is what keeps the server free of
# screen state (§4) while letting the price stand on the morning's fit: the
# browser owns the fit's answer exactly as it owns the panel, and posts it
# whole.  A quote run given no marks prices the surface as it stands, and says
# which of the two it did.


def _knob_points(name: str, value: float) -> float:
    """A knob on its way out: volatility parameters in points, the rest raw."""
    return value * 100.0 if name in _PERCENT_KNOBS else value


def _knob_decimal(name: str, value: float) -> float:
    """A knob on its way back in, the way :func:`_knob_points` sent it out.

    It is the inverse in arithmetic and **not in binary**: ``x * 100 / 100``
    differs from ``x`` in the last place for about an eighth of the values it
    is given.  That is why keeping a fit's marks puts them on through this
    same pair of conversions rather than leaving the raw fitted numbers on the
    surface -- see :meth:`Panel.run`.
    """
    return value / 100.0 if name in _PERCENT_KNOBS else value


def capture_marks(surface) -> dict:
    """Every parameter the two fits can move, as the panel boundary spells it.

    Volatility points at the edge and decimals inside, like everything else
    that crosses this line (§4).  A person reading the payload sees the same
    numbers the curve card shows them.
    """
    knobs = _Knobs(surface.atm)
    values = knobs.get()
    return {
        "knobs": {k: _knob_points(k, values[k]) for k in knobs.available if k in values},
        "shifts": {k: float(surface.param_shifts.get(k, 0.0)) for k in PARAM_NAMES},
    }


def apply_marks(surface, marks: dict) -> list[str]:
    """Put a captured set of marks on a surface.  Returns what would not take.

    A name this curve does not have is a **refusal**, not a silent skip: these
    marks are posted by a browser and can be typed by hand, and a knob that
    quietly did nothing is the failure this project exists to remove.
    """
    knobs = _Knobs(surface.atm)
    legal = set(knobs.available)
    values, bad = {}, []
    for name, value in (marks.get("knobs") or {}).items():
        if name not in legal:
            bad.append(name)
            continue
        values[name] = _knob_decimal(name, float(value))
    if bad:
        raise ValueError(
            f"the marks name {', '.join(sorted(bad))}, which this curve does not have; it holds "
            f"{', '.join(knobs.available)}")
    shifts = marks.get("shifts")
    if shifts is not None:
        stray = [k for k in shifts if k not in PARAM_NAMES]
        if stray:
            raise ValueError(
                f"the marks name smile parameter(s) {', '.join(sorted(stray))}; the four are "
                f"{', '.join(PARAM_NAMES)}")
    problems = knobs.set(values) if values else []
    if shifts is not None:
        surface.set_param_shifts({k: float(v) for k, v in shifts.items()})
    surface.invalidate()
    return problems


#: What each part of a pair's marked state is called where a person reads it.
#: The keys are ``session.capture_pair``'s, plus the two things a session does
#: not capture because they belong to the workbook rather than to a marker.
FINGERPRINT_LABELS = {
    "curve": "the curve parameters",
    "events": "the event table",
    "atm_overwrites": "the pinned at-the-money tenors",
    "quote_overwrites": "the re-quoted wings",
    "wing_ratios": "the wing ratios",
    "smile_overwrites": "the smile parameter overwrites",
    "smile_term": "a marked smile term structure",
    "param_shifts": "the smile shifts",
    "anchor_tenors": "the smile anchor",
    "band": "the band treatment",
    "sheet_quotes": "the pair sheet's own quotes",
    "sheet_ratios": "the WING_RATIOS tab",
}


def mark_fingerprint(book, pair: str) -> dict[str, str]:
    """One short hash per part of a pair's marked state, as it stands now.

    A fit's answer is a set of numbers the browser holds and posts back to the
    quote, and the book underneath it can be re-marked in between -- that is
    the whole of the marking screen.  Stamped with this, a held fit is stale
    exactly when the pair has been re-marked, and the quote can say **which
    part** moved rather than pricing off a curve nobody is marked on any more.

    It is a photograph rather than a counter on purpose.  ``capture_pair`` is
    already the snapshot a re-marking instance is diffed from
    (``remarks.diff_snapshots``), so every route that marks anything is
    covered the day it is written and there is no bump for a future one to
    forget -- which is the failure this replaces: ``applied_marks`` put the
    fit's backbone knobs and smile shifts back over whatever the marking
    screen had done to them, silently, while an at-the-money pin or a
    re-quoted wing went through untouched.

    The sheet's own quotes and wing ratios are hashed beside the session's
    marks because a workbook reloaded from an edited file moves those and
    nothing a session captured.
    """
    from . import session

    surface = book[pair]
    block = dict(session.capture_pair(book, pair))
    block["sheet_quotes"] = [[m.tenor, m.rr_25, m.rr_10, m.st_25, m.st_10]
                             for m in surface.marks]
    block["sheet_ratios"] = {t: [r.st, r.rr] for t, r in sorted(surface.wing_ratios.items())}
    return {
        key: hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        for key, value in sorted(block.items())
    }


def fingerprint_moved(stamped: dict | None, now: dict) -> list[str]:
    """Which parts of the marks have moved since a fit stamped them.

    An empty list for a fit that carries no stamp: a payload from a client
    that predates this is quoted off as it always was rather than refused,
    because refusing on a *missing* field would break every saved panel the
    day it shipped.
    """
    if not stamped:
        return []
    return sorted(FINGERPRINT_LABELS.get(k, k) for k in set(stamped) | set(now)
                  if stamped.get(k) != now.get(k))


@contextmanager
def applied_marks(surface, marks: dict | None, warnings: list[str]):
    """Quote off a set of marks, then put back exactly what was there.

    The restore is *verified* rather than assumed, for the reason
    :func:`volkit.marking.marked` gives: a surface left half-marked by a quote
    nobody kept, priced off all morning, is the worst outcome available to a
    tool whose whole job is marking.  It reports rather than raises, because
    the quote sheet the caller is holding is still correct -- what is no
    longer safe is the book, and saying so is what the reader needs.
    """
    before = capture_marks(surface)
    try:
        if marks:
            for problem in apply_marks(surface, marks):
                warnings.append(f"the fit's marks did not go on cleanly: {problem}")
        yield
    finally:
        try:
            apply_marks(surface, before)
            back = capture_marks(surface)
        except (ValueError, ArithmeticError) as exc:  # pragma: no cover - a broken restore
            back, exc_text = None, str(exc)
            warnings.append(
                f"the marks could not be put back after the quote ({exc_text}). Reload the "
                f"workbook before anything is priced off this book")
        if back is not None and back != before:
            moved = sorted(set(
                [k for k, v in before["knobs"].items() if back["knobs"].get(k) != v]
                + [k for k, v in before["shifts"].items() if back["shifts"].get(k) != v]))
            warnings.append(
                f"the marks were not put back exactly after the quote: "
                f"{', '.join(moved)} did not return. Reload the workbook before anything is "
                f"priced off this book")


def _forwards_for(book, pair: str, expiries: dict) -> tuple[dict, list[str]]:
    """The outright forward at every expiry a panel mentions, and what it cost.

    Both panels ask, and they must ask the same way: a strike is turned into a
    moneyness here and nowhere else, and a pair the feed reaches only through
    its legs is composed by ``Book.market_level`` rather than refused (§4).
    """
    from .analytics import _forward_at
    forwards, notes, said = {}, [], set()
    for key, (dt, t) in expiries.items():
        fwd, real, note = _forward_at(book, pair, t, dt.date())
        forwards[key] = fwd if real else None
        # Said once, not once a tenor: which pair the feed quotes is a
        # property of the feed, and eight tenors repeating one sentence is
        # a note nobody reads.  A tenor's own trouble carries its own
        # ``t`` and so is never the same text twice.
        for part in (x.strip() for x in note.split(";")) if real and note else ():
            if part and part not in said:
                said.add(part)
                notes.append(f"{key}: {part}")
    if expiries and not any(v is not None for v in forwards.values()):
        notes.append(
            f"there is no forward feed for {pair}, so an instrument written against an "
            f"absolute strike cannot be turned into a moneyness and is reported as "
            f"unavailable rather than priced at a forward of 1")
    return forwards, notes


# ===========================================================================
# the target at-the-money curve
# ===========================================================================


def curve_targets(surface, quotes, expiries, *, source: str,
                  text: str = "") -> tuple[list[CurveTarget], str]:
    """Where the target at-the-money curve comes from, and what it is.

    A module-level function and not a panel's method, because the thing that
    *moves* a curve and the thing that *checks* one are no longer the same
    panel (§11).  Every mark that moves is moved by the marking agent now, and
    the agent is what asks this; hanging it off the check panel would leave the
    check carrying a fit's machinery around for somebody else to borrow.
    """
    atm = surface.atm
    if source == "none":
        return [], "no target curve; the level was left as marked"
    if source == "overwrites":
        pinned = dict(atm.tenor_overwrites)
        if not pinned:
            raise ValueError(
                "no tenor is pinned on the marking screen, so there is no target curve to "
                "fit to. Pin the at-the-money levels you want, paste a curve, or fit to the "
                "at-the-money quotes instead")
        targets = [CurveTarget(tenor.upper(), atm.tenor_years(tenor), vol, "pinned tenor")
                   for tenor, vol in pinned.items()]
        return sorted(targets, key=lambda x: x.t), (
            f"{len(targets)} tenor(s) pinned on the marking screen")
    if source == "quotes":
        atms = [q for q in quotes if q.instrument == "atm"]
        if not atms:
            raise ValueError("the paste has no at-the-money quote to fit the curve to")
        targets = [CurveTarget(str(q.expiry), expiries[_key(q.expiry)][1], q.mid,
                               f"mid of {q.bid * 100:.3f}/{q.ask * 100:.3f}") for q in atms]
        return sorted(targets, key=lambda x: x.t), (
            f"the mid of {len(targets)} at-the-money quote(s) in the paste")
    if source == "current":
        targets = [CurveTarget(tp.upper(), atm.tenor_years(tp),
                               atm.curve_vol(atm.tenor_years(tp)),
                               "the curve as it stands")
                   for tp in atm.tenor_points]
        return targets, "the curve as it stands, as a no-op check on the fit itself"
    if source == "paste":
        targets, bad = [], []
        for n, line in enumerate(text.splitlines(), start=1):
            body = line.split("#")[0].replace(",", " ").replace(":", " ").strip()
            if not body:
                continue
            bits = body.split()
            if len(bits) < 2:
                bad.append(f"line {n}: expected a tenor and a volatility")
                continue
            try:
                # On the pair's calendar, like every other tenor here: a
                # pasted target names this pair's own expiries.
                t = atm.tenor_years(bits[0])
                vol = float(bits[1])
            except Exception as exc:  # noqa: BLE001
                bad.append(f"line {n}: {exc}")
                continue
            targets.append(CurveTarget(bits[0].upper(), t, vol, f"pasted line {n}"))
        if bad:
            raise ValueError("the pasted target curve has bad lines: " + "; ".join(bad))
        if not targets:
            raise ValueError("the pasted target curve is empty")
        # In volatility points, as written.  The level does not decide the
        # unit (§4) -- a managed pair's target curve sits below 1.0 and was
        # being read as decimals, a hundred times its mark.
        targets = [CurveTarget(x.tenor, x.t, x.vol / 100.0, x.source) for x in targets]
        return sorted(targets, key=lambda x: x.t), (
            f"{len(targets)} pasted line(s), read as volatility points")
    raise ValueError(f"unknown target source {source!r}; "
                     f"expected one of {TARGET_SOURCES}")


# ===========================================================================
# checking a market against the curve
# ===========================================================================

#: How far inside a quoted two-way still counts as **near its edge**, as a
#: fraction of the width measured in from the nearer side.  A quarter, which
#: is the shading the Now column on the screen has always used.  A mark in the
#: outer quarter of somebody's market is not wrong; it is one move away from
#: being wrong, and a desk would rather see that before the phone rings than
#: after.  Zero switches the warning off and leaves only what is actually
#: through, which is the honest setting for a desk that does not want amber.
NEAR_EDGE = 0.25

#: What a checked line can come back as, worst first.  ``through`` is the mark
#: outside the quoted two-way; ``edge`` inside it but within ``near_edge`` of a
#: side; ``in line`` comfortably inside; ``not checked`` a line the surface
#: could not be read at, which is a message and never a pass.
SEVERITIES = ("through", "edge", "in line", "not checked")

#: The quoting agent's verdict on a width, on every quote row.  ``agrees`` is
#: the quiet case; ``tight`` and ``wide`` are the bank's rule (or the fallback
#: tier) against what the archive has seen; ``no rule`` is a width taken
#: off the archive because the bank had none; ``thin`` an archive that does not
#: know; ``not read`` a row nothing could be said about.
AGENT_VERDICTS = ("agrees", "tight", "wide", "no rule", "thin", "not read")

#: How far apart the bank and the archive have to be before it is worth
#: saying so, as a fraction of the archived width.  Below this the two agree:
#: a ladder written at 0.40 against a market that has been 0.41 is a ladder
#: that is right, and a screen that says otherwise has an opinion about every
#: row, which is a screen nobody reads.
AGENT_TOLERANCE = 0.10

#: The narrowest gap worth reporting whatever the fraction says.  Without it
#: a 0.08 butterfly width would be "disagreeing" over four thousandths.
AGENT_MIN_GAP = 0.02

#: The most a client's record may add to a width, as a fraction of the width
#: before it.  One: a client the market follows may cost a whole width, and a
#: price twice as wide as the bank's is already a price that says "go away";
#: beyond that the answer is a pass, which is a person's call.
CLIENT_WIDEN_CAP = 1.0


@dataclass
class CheckPanel:
    """One pair's market, read against the curve as it stands.  It moves nothing.

    This used to be the fit, and moving marks was half of what it did.  It is
    now the narrow question a desk actually asks of an arriving run: **is any
    of this through where we are marked**.  Every mark that moves is the
    marking agent's (:mod:`volkit.marking`) -- one card, one journal, one place
    a curve can change -- so there is nothing on this panel to keep, nothing to
    put back, and no knob on it at all.

    It reads the same paste, in the same grammar, and it may be handed the
    marks the marking card is holding: a check of the book while a proposal is
    on the screen unanswered would be a check of a curve nobody is quoting off.
    Which of the two it read is on the answer, because a market checked against
    this morning's proposal and one checked against last night's marks must
    never read the same.
    """

    pair: str
    cut: str = "NY"
    method: str | None = None
    label: str = ""

    # the market
    text: str = ""
    vol_unit: str = "auto"
    fly_convention: str = "market"

    #: What counts as near the edge of a quoted two-way; see :data:`NEAR_EDGE`.
    near_edge: float = NEAR_EDGE

    #: The marks to check against: what the marking card is holding, or nothing
    #: for the book as it stands.  Read exactly as :class:`QuotePanel` reads
    #: them, stale stamp and all, so the two screens can never disagree about
    #: which curve they are looking at.
    marks: dict | None = None

    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- the run ----------------------------------------------------------
    def run(self, book) -> dict:
        surface, method, clock = _prepare(book, self.pair, self.method)

        out: dict = {
            "pair": self.pair, "cut": self.cut, "method": method, "label": self.label,
            "valuation": clock.now.isoformat(),
            "near_edge": float(self.near_edge),
            "notes": list(self.notes), "warnings": [], "unavailable": {},
            "market": None, "marks": None,
        }

        # -- the paste -----------------------------------------------------
        run_ = parse_quotes(self.text, pair=self.pair, vol_unit=self.vol_unit,
                            fly_convention=self.fly_convention, today=clock.now.date())
        quotes = list(run_.quotes)
        expiries = resolve_expiries(clock, quotes, self.pair, book.calendars)
        stale = [k for k, (_, t) in expiries.items() if t <= 0]
        if stale:
            raise ValueError(
                f"{', '.join(stale)} is not in the future at the valuation time "
                f"{clock.now:%Y-%m-%d %H:%M}Z")
        forwards, forward_notes = _forwards_for(book, self.pair, expiries)
        # A premium becomes a volatility here, once, against the same forward
        # the strike quotes are placed with; a line that cannot be converted
        # keeps its place and its reason, like any other row that will not
        # price.
        quotes, premium_errors = premiums_as_vols(
            quotes, expiries, _levels_for(book, self.pair, expiries), self.pair)

        # -- which curve is being checked ----------------------------------
        # The same reading QuotePanel gives a held set of marks, for the same
        # reason: marks made against a curve that has since been re-marked are
        # dropped rather than laid back over what was marked in the meantime.
        marks = self.marks
        moved = fingerprint_moved((marks or {}).get("book"),
                                  mark_fingerprint(book, self.pair))
        if moved:
            out["warnings"].append(
                f"{self.pair} has been re-marked since these marks were made "
                f"({', '.join(moved)} moved), so the market below is checked against the "
                f"marks as they are now rather than against them. Propose again to check "
                f"against a proposal")
            marks = None

        # -- where the surface sits, and what is off -----------------------
        model: list[float | None] = []
        row_errors: list[str] = []
        with applied_marks(surface, marks, out["warnings"]):
            ev = Evaluator(surface, method, self.cut)
            for q, unusable in zip(quotes, premium_errors):
                if unusable:
                    model.append(None)
                    row_errors.append(unusable)
                    continue
                try:
                    model.append(ev.value(q, expiries, forwards))
                    row_errors.append("")
                except (ValueError, ArithmeticError, ConvergenceError) as exc:
                    model.append(None)
                    row_errors.append(f"{type(exc).__name__}: {exc}")

        stood = dict(marks or self.marks or {})
        out["marks"] = {
            "on_the_marks": bool(marks),
            "what": stood.get("what") or "",
            "stamp": stood.get("stamp") or "",
            "stale": moved,
            "note": (f"checked against the marks this panel was handed: "
                     f"{stood.get('what') or 'unnamed'}" if marks else
                     (f"the marks this panel was handed are out of date -- "
                      f"{', '.join(moved)} moved since they were made -- so the market is "
                      f"checked against the marks as they stand on the book" if moved else
                      "checked against the marks as they stand on the book")),
        }
        out["market"] = self._market(quotes, expiries, model, row_errors, run_, forward_notes)
        out["warnings"].extend(surface.warnings[-6:])
        return out

    # -- pieces of the run --------------------------------------------------
    def _market(self, quotes, expiries, model, errors, run_, forward_notes) -> dict:
        """Every quote the paste held, against the curve, with what is off named.

        No width and no price: what we would show is the quote panel's, asked
        of the request box.  This answers the one question the button asks --
        is the mark inside the market that just arrived, and if not by how far.
        """
        rows = []
        for q, mv, err in zip(quotes, model, errors):
            _, t = expiries[_key(_row_expiry(q))]
            unit_scale = 100.0 if q.quote_kind == "vol" else 1.0
            row = {
                "line": q.line, "raw": q.raw, "label": q.label, "describe": q.describe(),
                "instrument": q.instrument, "leg": q.leg, "delta": q.delta,
                "strike": q.strike, "is_call": q.is_call, "fly_kind": q.fly_kind,
                "legs": [leg.describe() for leg in q.legs],
                "quote_kind": q.quote_kind, "premium_unit": q.premium_unit,
                "tenor": _key(q.expiry), "tenor_far": (None if q.expiry_far is None
                                                       else _key(q.expiry_far)),
                # What was written, not the resolved instant: a run with no
                # date in it is ordered on a nominal day, and showing that day
                # back would be a date the paste never contained.
                "timestamp": q.timestamp_text,
                "days": t * DAYS_IN_YEAR, "size": q.size, "size_basis": q.size_basis,
                # A premium that could not be turned into a volatility is shown
                # as it was written, in its own unit, and the row says so.
                "market_bid": q.bid * unit_scale, "market_ask": q.ask * unit_scale,
                "market_mid": q.mid * unit_scale, "market_width": q.spread * unit_scale,
                "model": None if mv is None else mv * 100.0,
                "position": None, "gap": None, "widths": None, "depth": None,
                "severity": "not checked", "verdict": "",
                "notes": list(q.notes), "warnings": [],
            }
            if err:
                row["verdict"] = "not checked"
                row["warnings"].append(err)
            else:
                row.update(self._verdict(q, mv))
            rows.append(row)

        counted = [r for r in rows if r["severity"] != "not checked"]
        return {
            "rows": rows,
            "vol_unit": run_.vol_unit,
            "unit_evidence": run_.unit_evidence,
            "notes": list(run_.notes) + list(forward_notes),
            "skipped": [{"line": n, "text": t, "why": w} for n, t, w in run_.skipped],
            # Lines that quote another pair: not wrong, just somebody else's.
            "ignored": [{"line": n, "text": t, "why": w} for n, t, w in run_.ignored],
            # Read, understood, and then replaced by a later quote of the same
            # thing.  Reported rather than dropped: a line that disappeared
            # between the paste and the screen is a silent zero in disguise.
            "superseded": [{"line": q.line, "text": q.raw, "describe": q.describe(),
                            "timestamp": q.timestamp_text, "replaced_by": q.replaced_by,
                            "bid": q.bid * 100.0, "ask": q.ask * 100.0}
                           for q in run_.superseded],
            "n_quotes": len(rows),
            "checked": len(counted),
            "inside": sum(1 for r in counted if r["position"] == "inside"),
            "through": sum(1 for r in counted if r["severity"] == "through"),
            "edge": sum(1 for r in counted if r["severity"] == "edge"),
            # The whole point of the button, as one line the screen and the
            # shell can both print without recomputing it.
            "alerts": [{"line": r["line"], "describe": r["describe"],
                        "severity": r["severity"], "verdict": r["verdict"],
                        "gap": r["gap"], "widths": r["widths"]}
                       for r in counted if r["severity"] in ("through", "edge")],
            "fly_convention": self.fly_convention,
            "near_edge": float(self.near_edge),
        }

    def _verdict(self, q, mv: float) -> dict:
        """One line's answer: where the mark sits, how far, and how bad.

        ``gap`` is signed the way a desk reads it -- positive when the mark is
        above their offer, negative when it is below their bid, zero inside --
        and ``widths`` is that distance in units of their own width, which is
        the number that says whether being outside matters.  ``depth`` is how
        far in from the nearer side a mark inside the market is, as a fraction
        of the width, so a choice price and a wide two-way are comparable.
        """
        width = q.ask - q.bid
        if mv > q.ask:
            gap = mv - q.ask
            out = {"position": "above", "gap": gap * 100.0, "severity": "through",
                   "verdict": "through their offer"}
        elif mv < q.bid:
            gap = q.bid - mv
            out = {"position": "below", "gap": -gap * 100.0, "severity": "through",
                   "verdict": "through their bid"}
        else:
            gap = 0.0
            # A choice price has no inside: the mark is on it or through it,
            # and calling that "near the edge" would be a warning about a
            # market that quoted no width to be near the edge of.
            depth = (min(mv - q.bid, q.ask - mv) / width) if width > 0 else None
            near = depth is not None and depth < float(self.near_edge)
            out = {"position": "inside", "gap": 0.0, "depth": depth,
                   "severity": "edge" if near else "in line",
                   "verdict": ("near their " + ("bid" if mv - q.bid < q.ask - mv else "offer"))
                              if near else "in line"}
        out["widths"] = (out["gap"] / (width * 100.0)) if width > 0 else None
        return out


@dataclass
class QuotePanel:
    """What we would show, on the instruments somebody has asked for.

    **The one pricing engine.**  The Quote button, ``volkit mm --request`` and
    ``volkit agent quote`` all arrive here (§17): a price made in a browser and
    a price made in a shell are the same price because there is one function
    that makes one.

    It fits nothing.  Its inputs are the request box, the marks it is told to
    stand on, the knowledge bank, the archive -- widths, the level check, and
    the caller's own record -- the position, the fair value and the printed
    tape; and the market paste, for one purpose only: a request that names the
    same instrument as a quoted line carries that market beside our price, so
    "inside their market" is still a thing this screen can say.

    Every row carries its ``trace``: the ordered list of ingredients, each
    with its value, unit and source, that sum to the bid and the offer.  The
    prose -- the CLI's explanation and the local model's paragraph -- is
    generated from that list and never the other way round.
    """

    pair: str
    cut: str = "NY"
    method: str | None = None
    label: str = ""

    # what is being asked for
    request_text: str = ""
    fly_convention: str = "market"

    # the market, for comparison only.  Never fitted to here.
    text: str = ""
    vol_unit: str = "auto"

    # the marks to quote off: what a fit handed back, or nothing
    marks: dict | None = None

    # skewing the mid
    vega_text: str = ""
    vega_scale: float = 0.0
    fair_weight: float = 0.25
    axe_weight: float = 0.5
    #: The printed tape's lean.  **Off unless a weight is set**: what the
    #: dissemination file says about direction is inferred and not published,
    #: and a desk may not want an inference moving its price at all.  Positive
    #: leans the mid *up* when the tape has been paying (see `skew_for`).
    flow_weight: float = 0.0
    #: The net vega, in the base currency per volatility point, that counts as
    #: a full lean.  The flow is divided by it and clamped to one.
    flow_scale: float = 5_000_000.0
    #: How far from our mark a print has to be to be read as paid or given,
    #: as a fraction of the mark, where the archive knows no width for the
    #: bucket.  The tape's age weight and window are the archive's own
    #: (``archive_half_life``, ``archive_lookback_days``): one evidence clock
    #: for everything the archive says, so the tape and the widths on one
    #: sheet never disagree about what "recent" means.
    flow_tolerance: float = 0.03
    skew_cap: float = 1.0
    horizon_days: float = 30.0
    lookback_days: float | None = None

    # widths
    # The bottom rung of the width ladder: a **spreading tier** off the
    # workbook's KACE_SPREADS tab, read at the row's own maturity.  It used to
    # be one typed number for every tenor on the screen, which is not a width
    # any desk shows -- a one-week two-way and a one-year two-way are not the
    # same width, and a single box made the fallback either far too wide at
    # the front or far too tight at the back.  A tier is the ladder the desk
    # already maintains, so the fallback is now the same object the feed
    # posts: named here, scaled by ``fallback_multiplier``, and read between
    # its rungs by the same rule the message uses (``kace.width_at``).
    #
    # Empty is no fallback at all, which is the honest default: nothing gets a
    # width the bank and the archive cannot account for unless somebody names
    # the ladder it should come off.
    fallback_tier: str = ""
    fallback_multiplier: float = 1.0
    #: Stepped (``False``, the tab's own rule) or read across between the two
    #: tenors a maturity falls between.
    fallback_interpolate: bool = False
    # The archive's rung on the width ladder: bank, then what the archive has
    # seen this shown at, then the fallback tier, then no price.  Always on
    # the ladder (§17): thin evidence produces no number, so an archive that
    # knows nothing costs nothing, and a row always names the rung it stood on.
    archive_half_life: float = 5.0
    archive_min_effective: float = 2.0
    archive_lookback_days: float = 90.0
    include_model_read: bool = True
    #: How far apart the bank's width and the archive's may be before the row
    #: says so, as a fraction of the archived width (the agent's verdict).
    tolerance: float = 0.10

    # who is asking
    #: The client the price is for.  Their record on this instrument -- which
    #: way they trade, and whether the market follows them -- leans the mid and
    #: widens the price (§17).  Empty is a price for nobody in particular, and
    #: nobody's record applies.
    client: str = ""
    #: The lean per unit of ``side``, as a fraction of the half width: 0.5 and
    #: a client who only ever lifts moves the mid up by a quarter of the width.
    #: The same number scales the widening: the width grows by this fraction
    #: of the mean move against us after their trades.  Zero switches both off.
    client_weight: float = 0.5
    #: Answered prices a client needs on an instrument before their record
    #: counts.  Below it the record is shown and nothing moves.
    client_min: int = 4

    notes: tuple[str, ...] = field(default_factory=tuple)

    def run(self, book, *, bank: KnowledgeBank | None = None, hist=None,
            archive=None, spreads=None) -> dict:
        surface, method, clock = _prepare(book, self.pair, self.method)
        self._book = book
        # The fallback ladder is resolved once, before any row: a tier that is
        # not there is one message on the sheet rather than the same sentence
        # repeated on every row that wanted a width.
        self._ladder = self._fallback_ladder(spreads)

        out: dict = {
            "pair": self.pair, "cut": self.cut, "method": method, "label": self.label,
            "valuation": clock.now.isoformat(),
            "notes": list(self.notes), "warnings": [], "unavailable": {},
            "sheet": None, "bank": None, "axe": None, "fair": None, "flow": None,
            "marks": None,
        }

        asked = parse_requests(self.request_text, pair=self.pair,
                               fly_convention=self.fly_convention,
                               today=clock.now.date())
        requests = list(asked.requests)

        # The market, if there is one, for the comparison columns only.  A
        # paste that cannot be read does not stop the quote: the price is a
        # property of the marks and the bank, and never of what a broker
        # happened to show.
        market: dict = {}
        market_notes: list[str] = []
        if self.text.strip():
            try:
                run_ = parse_quotes(self.text, pair=self.pair, vol_unit=self.vol_unit,
                                    fly_convention=self.fly_convention,
                                    today=clock.now.date())
                market = {instrument_key(q): q for q in run_.quotes
                          if q.quote_kind == "vol"}
                if any(q.quote_kind == "premium" for q in run_.quotes):
                    market_notes.append(
                        "premium lines in the market paste are not set beside the prices "
                        "here; the fit turns them into volatilities and reports them")
                # Its own notes are not repeated here.  The paste is read by
                # the fit, which reports what it inferred from it; saying the
                # same three sentences again beside a price is how a panel
                # trains somebody to stop reading its notes.
            except (QuoteError, ValueError) as exc:
                market_notes = [f"the market paste could not be read for comparison ({exc}); "
                                f"the prices below are unaffected"]

        expiries = resolve_expiries(clock, requests, self.pair, book.calendars)
        stale = [k for k, (_, t) in expiries.items() if t <= 0]
        if stale:
            raise ValueError(
                f"{', '.join(stale)} is not in the future at the valuation time "
                f"{clock.now:%Y-%m-%d %H:%M}Z")
        forwards, forward_notes = _forwards_for(book, self.pair, expiries)

        axe_at, axe_block = self._axe(clock)
        out["axe"] = axe_block

        # The desk agent's evidence, worked once for the sheet.  A width that
        # came off the archive names itself on the row, and the archive's
        # level check rides on the row as a flag and is applied to nothing:
        # a mid that follows the last thing it was shown is being led by the
        # party it is about to trade with.
        synthesis, archive_block = self._archive(archive, clock)
        out["archive"] = archive_block
        out["client"] = self._client_block(synthesis)

        bank = bank if bank is not None else KnowledgeBank()
        pk = bank.for_pair(self.pair)
        out["bank"] = {
            "pair": self.pair.upper(),
            "path": bank.path,
            "rules": [_rule_json(r) for r in pk.rules],
            "updated": pk.updated,
            "source_note": pk.source_note,
            "problems": list(bank.problems),
        }

        # A held fit is only good for the book it was fitted on.  The marks
        # come from the browser, which keeps them across a trip to the marking
        # screen, and ``applied_marks`` would put the fit's backbone knobs and
        # smile shifts back over whatever was marked there -- silently, and
        # only over *those two*, so a pinned tenor or a re-quoted wing went
        # through while a re-marked curve did not.  A price that is half this
        # morning's marks and half a fit of the curve they replaced is a wrong
        # answer that reads perfectly well, so the stale marks are dropped and
        # the quote stands on the book, saying which part moved.
        marks = self.marks
        moved = fingerprint_moved((marks or {}).get("book"),
                                  mark_fingerprint(book, self.pair))
        if moved:
            out["warnings"].append(
                f"{self.pair} has been re-marked since these marks were made "
                f"({', '.join(moved)} moved), so these prices stand on the marks as they are "
                f"now rather than on them. Propose or fit again on the marking card to price "
                f"on a curve of this book")
            marks = None

        # Everything that reads the surface happens inside the marks, and the
        # fair value with it: richness is the mark against realized, and the
        # mark being shaded is the one being quoted.  Measured outside, a fit
        # that moved the at-the-money half a point would be shaded by the
        # richness of the level it had just left.
        with applied_marks(surface, marks, out["warnings"]):
            rich_at, fair_block = self._fair(book, hist, method)
            out["fair"] = fair_block
            ev = Evaluator(surface, method, self.cut)
            flow_at, flow_block = self._flow(archive, ev, clock, expiries, forwards, hist)
            out["flow"] = flow_block
            rows = [self._row(q, ev, expiries, forwards, pk, rich_at, axe_at, flow_at,
                              market, synthesis)
                    for q in requests]

        stood = dict(marks or self.marks or {})
        out["marks"] = {
            "on_the_marks": bool(marks),
            "fitted": bool(stood.get("fitted")),
            "what": stood.get("what") or "",
            "stamp": stood.get("stamp") or "",
            # Which parts of the pair's marks moved after the fit was made.
            # Empty for a fit that is still good, and for a panel that was
            # handed no marks at all: the two are told apart by
            # ``on_the_marks``, which is spelled the same on the check.
            "stale": moved,
            "note": (f"quoted off the marks this panel was handed: "
                     f"{stood.get('what') or 'unnamed'}" if marks else
                     (f"the marks this panel was handed are out of date -- "
                      f"{', '.join(moved)} moved since they were made -- so this is quoted off "
                      f"the marks as they stand on the book" if moved else
                      "quoted off the marks as they stand on the book; propose or fit on the "
                      "marking card and hand its answer over to price on that instead")),
        }
        out["sheet"] = {
            "rows": rows,
            "notes": list(asked.notes) + market_notes + forward_notes,
            "skipped": [{"line": n, "text": t, "why": w} for n, t, w in asked.skipped],
            "ignored": [{"line": n, "text": t, "why": w} for n, t, w in asked.ignored],
            "n_quotes": len(rows),
            "priced": sum(1 for r in rows if r["our_bid"] is not None),
            "matched": sum(1 for r in rows if r["market_mid"] is not None),
            # The agent's verdicts, counted so the header can say "2 rows are
            # shown at a width the archive does not support" without the
            # page re-deriving what counts as disagreeing.
            "disagreeing": sum(1 for r in rows if r["agent_verdict"] in ("tight", "wide")),
            "leaned_by_client": sum(1 for r in rows if r["skew_client"]),
            "widened_by_client": sum(1 for r in rows if r["client_widen"]),
            "fly_convention": self.fly_convention,
            # What the bottom rung was, so the screen can say it once rather
            # than the reader inferring it from the rows that used it.
            "fallback": dict(self._ladder),
            "tolerance": self.tolerance,
        }
        out["warnings"].extend(surface.warnings[-6:])
        return out

    # -- the bottom rung ----------------------------------------------------
    def _fallback_ladder(self, spreads) -> dict:
        """The spreading tier this panel falls back on, or why it has none.

        Always a dict, never a refusal: a quote run that cannot reach its
        fallback still prices every row the bank and the archive can answer,
        and the rows that wanted the fallback say what was missing.  A width
        the desk did not get is already a first-class outcome here -- there is
        no built-in default width and never has been -- so a missing tier is
        that same outcome with a better sentence.
        """
        out = {"tier": "", "multiplier": 1.0, "interpolate": bool(self.fallback_interpolate),
               "widths": {}, "table": "", "error": ""}
        wanted = str(self.fallback_tier or "").strip()
        if not wanted:
            return out
        try:
            out["multiplier"] = kace.spread_multiplier(self.fallback_multiplier)
        except kace.KaceError as exc:
            out["error"] = str(exc)
            return out
        if spreads is None:
            out["error"] = (f"the fallback tier {wanted!r} was named, but no {kace.SPREADS_SHEET} "
                            f"table was loaded, so the bottom rung of the width ladder is empty")
            return out
        out["table"] = spreads.path
        try:
            out["tier"] = spreads.resolve_tier(wanted)
        except kace.KaceError as exc:
            out["error"] = str(exc)
            return out
        out["widths"] = dict(spreads.for_tier(out["tier"]))
        if not out["widths"]:
            out["error"] = f"the {out['tier']} tier of {spreads.path} holds no tenors"
        return out

    def _fallback_width(self, days: float, tenor: str = "") -> float | None:
        """The tier's width for one row, in volatility points, or ``None``.

        **A tenor the tab names is read off its own rung**, exactly.  Only a
        maturity the ladder does not name is read along it by year fraction.
        The difference matters: the calendar's 1M is 30 or 31 days and the
        ladder's ``1M`` rung sits at 365.2425/12 = 30.44, so a quote asked for
        as *1M* would otherwise fall one rung short of the row the desk wrote
        for it and be shown at the 1W width.  A rung is a tenor, and a request
        that says the tenor means that rung.
        """
        L = self._ladder
        widths = L.get("widths") or {}
        if not widths:
            return None
        named = kace.canonical_tenor(tenor) if tenor else ""
        if named and named in widths:
            return widths[named] * L["multiplier"]
        return kace.width_at(widths, days / DAYS_IN_YEAR,
                             multiplier=L["multiplier"], interpolate=L["interpolate"])

    def _fallback_describe(self) -> str:
        """How the bottom rung is named wherever a row says it stood on one."""
        L = self._ladder
        out = f"the {L['tier']} tier of {L['table'] or kace.SPREADS_SHEET}"
        if L["multiplier"] != 1.0:
            out += f" \u00d7{L['multiplier']:g}"
        out += ", interpolated" if L["interpolate"] else ", stepped"
        return out

    # -- one row ------------------------------------------------------------
    def _archive(self, archive, clock) -> tuple[object, dict]:
        """The archive worked into evidence, or the reason it was not."""
        if archive is None:
            return None, {"available": False, "used": True, "counted": 0, "widths": 0,
                          "reason": "no observation archive is loaded, so the archive rung "
                                    "of the width ladder is empty and no client has a record"}
        from . import synthesis as syn
        made = syn.synthesize(archive, self.pair, asof=clock.now,
                              half_life=self.archive_half_life,
                              min_effective=self.archive_min_effective,
                              lookback_days=self.archive_lookback_days,
                              include_model_read=self.include_model_read)
        block = {"available": True, "used": True, "path": archive.path,
                 "counted": made.counted, "half_life": self.archive_half_life,
                 "min_effective": self.archive_min_effective,
                 "lookback_days": self.archive_lookback_days,
                 "include_model_read": bool(self.include_model_read),
                 "widths": sum(1 for w in made.widths if w.enough),
                 "notes": list(made.notes),
                 # What the file holds for this pair, for the archive card:
                 # the counts by kind, how fresh it is, and every width it
                 # has enough behind.  Nothing here reaches a price.
                 "records": 0, "quote": 0, "trade": 0, "shown": 0, "outcome": 0,
                 "model_read": 0, "last": "", "age_days": None,
                 "held": [{"instrument": w.instrument, "bucket": w.bucket, "delta": w.delta,
                           "observations": w.observations, "sources": w.sources,
                           "median": w.median if w.enough else None,
                           "low": w.low, "high": w.high, "newest_days": w.newest_days,
                           "enough": w.enough, "why_not": w.why_not}
                          for w in made.widths]}
        for row in archive.summary():
            if row.get("pair") == self.pair.upper():
                block.update({k: row.get(k) for k in ("records", "quote", "trade", "shown",
                                                      "outcome", "model_read", "last")
                              if k in row})
                from .archive import parse_time
                newest = parse_time(row.get("last") or "")
                if newest is not None:
                    block["age_days"] = max(0.0, (clock.now - newest).total_seconds() / 86400.0)
        return made, block

    def _client_block(self, synthesis) -> dict:
        """Who the price is for, and what the record holds on them."""
        name = " ".join(str(self.client or "").split())
        block = {"name": name, "weight": self.client_weight, "minimum": self.client_min,
                 "known": [], "record": [], "applied": False, "reason": ""}
        if synthesis is not None:
            block["known"] = synthesis.client_names()
        if not name:
            block["reason"] = ("no client is named, so this is a price for nobody in "
                               "particular and nobody's record leans it")
            return block
        if synthesis is None:
            block["reason"] = "no archive is loaded, so there is no record of this client"
            return block
        from .synthesis import _client_key
        mine = [c for c in synthesis.clients if _client_key(c.client) == _client_key(name)]
        if not mine:
            block["reason"] = (f"the archive holds no price shown to {name} on {self.pair}; "
                               f"record what is quoted here and their record starts")
            return block
        block["record"] = [c.describe() for c in mine if c.bucket is None]
        block["applied"] = bool(self.client_weight)
        block["reason"] = ("" if self.client_weight else
                           "the client weight is zero, so the record is shown and applied to "
                           "nothing")
        return block

    def _premium_row(self, row: dict, q, t: float, bid, ask, forward,
                     expiry=None) -> None:
        if bid is None:
            row["notes"].append("asked as a premium, but there is no width, so there is no "
                                "two-way to turn into one")
            return
        if forward is None:
            row["warnings"].append(f"asked as a premium, and there is no forward feed for "
                                   f"{self.pair} to price it against; the volatility two-way "
                                   f"stands")
            return
        level = self._level_at(t, expiry)
        is_call = q.is_call
        if is_call is None and q.strike is not None:
            is_call = side_from_moneyness(q.strike, forward)
            row["notes"].append(_side_note(q, bool(is_call), forward))
            warn = _side_warning(q, bool(is_call), forward, 0.5 * (bid + ask), t)
            if warn:
                row["warnings"].append(warn)
        try:
            prices = [float(black.price(forward, q.strike, v, t, bool(is_call)))
                      for v in (bid, ask)]
        except (ValueError, ArithmeticError) as exc:
            row["warnings"].append(f"the premium could not be priced: {exc}")
            return
        if q.premium_unit == "pips":
            pip = level.get("pip")
            if not pip:
                row["warnings"].append("asked in pips, and the feed gives no pip size")
                return
            factor, label = pip, "pips"          # the feed's pip is a divisor
        elif q.premium_unit == "pct":
            base_level = level.get("spot") or forward
            factor, label = 100.0 / base_level, "% of base"
        else:
            factor, label = 1.0, f"{self.pair[3:6].upper()} per {self.pair[:3].upper()}"
        row["premium_bid"], row["premium_ask"] = prices[0] * factor, prices[1] * factor
        row["premium_label"] = label
        row["notes"].append(f"premium {prices[0] * factor:.4g}/{prices[1] * factor:.4g} {label} "
                            f"off the volatility two-way against the forward {forward:g}; "
                            f"undiscounted")

    def _level_at(self, t: float, expiry=None) -> dict:
        book = getattr(self, "_book", None)
        if book is None:
            return {}
        try:
            if expiry is not None:
                return book.market_level_for(self.pair, expiry)
            return book.market_level(self.pair, t)
        except (ValueError, KeyError):
            return {}

    def _row(self, q, ev, expiries, forwards, pk: PairKnowledge, rich_at, axe_at, flow_at,
             market: dict, synthesis=None) -> dict:
        dt_key = _key(_row_expiry(q))
        t = expiries[dt_key][1]
        days = t * DAYS_IN_YEAR
        row = {
            "line": q.line, "raw": q.raw, "label": q.label, "describe": q.describe(),
            "instrument": q.instrument, "leg": q.leg, "delta": q.delta,
            "strike": q.strike, "is_call": q.is_call, "fly_kind": q.fly_kind,
            "legs": [leg.describe() for leg in q.legs],
            "quote_kind": q.quote_kind, "premium_unit": q.premium_unit,
            "premium_bid": None, "premium_ask": None, "premium_label": "",
            "tenor": _key(q.expiry), "tenor_far": (None if q.expiry_far is None
                                                   else _key(q.expiry_far)),
            "days": days, "size": q.size, "size_basis": q.size_basis,
            "sign": q.sign, "direction": q.direction,
            "model": None,
            "skew_fair": None, "skew_axe": None, "skew_flow": None, "skew_client": None,
            "skew_bank": None,
            "skew_total": None, "skew_cap": None, "skew_capped": False, "skew_reason": "",
            "our_mid": None, "our_bid": None, "our_ask": None,
            "width": None, "width_source": None, "width_rung": "none", "floor": None,
            # What the fallback tier reads at this maturity, whether or not it
            # was the rung used: a bank rule beside the ladder it beat is the
            # comparison somebody makes before editing the rule.
            "fallback_width": None,
            "market_bid": None, "market_ask": None, "market_mid": None, "market_width": None,
            "position": None, "edge": None, "crossing": "",
            "richness": None, "axe": None, "flow": None, "verdict": "",
            # the archive, three ways: the width it has seen, the level it has
            # seen, and the agent's verdict on the width we are about to show
            "archive_width": None, "archive_observations": None, "archive_sources": None,
            "archive_low": None, "archive_high": None, "archive_newest_days": None,
            "archive_level": None, "archive_gap": None,
            "bank_width": None, "agent_verdict": "not read", "agent_gap": None,
            "agent_note": "",
            # the client's record on this instrument, and what it did
            "client": "", "client_scope": "", "client_side": None, "client_after": None,
            "client_widen": None, "client_record": "", "client_enough": False,
            "flags": [],
            # The bank's prose kept apart from the reader's own notes: a
            # note exists to be read, and burying it among parser chatter
            # is most of the way to not applying it at all.
            "advice": [], "notes": list(q.notes), "warnings": [],
            # Every ingredient that reached the price, in the order it was
            # read, with its unit and source.  The bid and the offer are the
            # sum of this list, and every sentence about the row comes off it.
            "trace": [],
        }
        trace = row["trace"]

        def ingredient(name, value, *, unit="vol points", source="", detail="",
                       applied=True):
            trace.append({"name": name, "value": value, "unit": unit, "source": source,
                          "detail": detail, "applied": bool(applied)})

        try:
            # The book's convention throughout, then the sign once, here: a
            # request asked as 'JPY call over' is answered in that convention
            # and every number on the row turns with it.  §5 item 1 is what a
            # sign applied in two places costs.
            model = ev.value(q, expiries, forwards) * q.sign
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            row["verdict"] = "not priced"
            row["warnings"].append(f"{type(exc).__name__}: {exc}")
            ingredient("model mid", None, source="the marked surface",
                       detail=f"{type(exc).__name__}: {exc}")
            return row
        row["model"] = model * 100.0
        ingredient("model mid", row["model"],
                   source=f"the marked surface ({ev.method}, {self.cut} cut)",
                   detail=f"{row['tenor']} is {days:.1f} days"
                          + (f"; asked as {q.direction}, so the sign is turned"
                             if q.sign < 0 else ""))

        # -- the width: the bank, then the archive, then the fallback tier ----
        # The fallback is read at *this row's* maturity, so a one-week and a
        # one-year quote no longer fall back on one number: the tier is a
        # ladder and the row asks it where it sits.
        fallback = self._fallback_width(days, row["tenor"])
        row["fallback_width"] = fallback
        overlay = pk.overlay(instrument=q.instrument, days=days, tenor=_key(q.expiry),
                             size=q.size, size_basis=q.size_basis, delta=q.delta,
                             fallback=fallback)
        width = None if overlay.spread is None else overlay.spread / 100.0
        row["width"] = overlay.spread
        row["bank_width"] = overlay.spread if overlay.spread_rule else None
        row["width_source"] = overlay.spread_rule or (
            self._fallback_describe() if width is not None else None)
        if self._ladder.get("error") and overlay.spread_rule is None:
            row["notes"].append(self._ladder["error"])
        row["width_rung"] = "bank" if overlay.spread_rule else (
            "fallback" if width is not None else "none")
        row["floor"] = overlay.floor
        row["advice"] = list(overlay.notes)
        if overlay.reason:
            row["warnings"].append(overlay.reason)
        row["notes"].extend(f"beaten: {b}" for b in overlay.beaten)
        # -- the archive rung, between the bank and the fallback -------------
        # One ladder, in one order: a rule the desk wrote beats what the
        # market showed, and what the market showed beats a number typed on a
        # panel this morning.  A spread has no width evidence of its own --
        # the archive keeps outrights.
        evidence = None
        if synthesis is not None and q.instrument != "spread":
            evidence = synthesis.width_for(instrument=q.instrument, days=days, delta=q.delta)
            if evidence is not None:
                row["archive_observations"] = evidence.observations
                row["archive_sources"] = evidence.sources
                row["archive_low"] = evidence.low if evidence.enough else None
                row["archive_high"] = evidence.high if evidence.enough else None
                row["archive_newest_days"] = evidence.newest_days
            if evidence is not None and evidence.enough:
                row["archive_width"] = evidence.median
                # A bank rule beats the archive; the archive beats the typed
                # fallback, which the overlay has already folded in when no
                # rule matched (``spread`` set, ``spread_rule`` not).
                if overlay.spread_rule is None:
                    if overlay.spread is not None:
                        row["notes"].append("the fallback tier was not needed; the archive "
                                            "holds a width for this")
                    width = evidence.median / 100.0
                    row["width"] = evidence.median
                    row["width_rung"] = "archive"
                    row["width_source"] = (f"the archive: {evidence.observations} "
                                           f"observation(s) from {evidence.sources} "
                                           f"broker(s), newest "
                                           + ("today" if evidence.newest_days < 1 else
                                              f"{evidence.newest_days:.0f}d ago"))
                    row["advice"].append("this width came from the archive and not from "
                                         "the bank; 'Learn widths' writes it in as a rule")
                    if overlay.reason and overlay.reason in row["warnings"]:
                        # The bank's refusal stands as the reason the archive
                        # was asked; it is not a warning any more.
                        row["warnings"].remove(overlay.reason)
                        row["notes"].append(overlay.reason)
            elif evidence is not None and overlay.spread_rule is None:
                row["notes"].append(f"archive: {evidence.why_not}")
            level = synthesis.level_for(instrument=q.instrument, tenor=_key(q.expiry),
                                        delta=q.delta)
            if level is not None and level.enough:
                # The archive holds the book's convention; the row is in the
                # convention it was asked in.  Compared in the archive's.
                gap, what = level.gap_to(row["model"] * q.sign)
                row["archive_level"] = level.typical
                row["archive_gap"] = gap
                if "worth knowing" in what:
                    row["flags"].append(what + "; applied to nothing")
            ingredient("market level", level.typical if level is not None and level.enough
                       else None, source="the archive", applied=False,
                       detail=(level.describe() if level is not None else
                               "nothing in the archive quotes this instrument at this tenor"))
        self._agent_verdict(row, overlay, evidence)

        if width is None:
            ingredient("width", None, source="nothing",
                       detail=(overlay.reason or "no bank rule matched")
                              + (f", and {evidence.why_not}" if evidence is not None
                                 and not evidence.enough else
                                 ", and the archive holds no width for this instrument at "
                                 "this tenor" if synthesis is not None
                                 and q.instrument != "spread" and evidence is None else ""))
        else:
            ingredient("width", row["width"],
                       source={"bank": f"the bank: {overlay.spread_rule}",
                               "archive": row["width_source"],
                               "fallback": self._fallback_describe()}[row["width_rung"]],
                       detail=(evidence.describe() if row["width_rung"] == "archive" else
                               f"no bank rule matched and the archive is too thin; the tier "
                               f"reads {fallback:.3f} at {days:.1f} days"
                               if row["width_rung"] == "fallback" and fallback is not None
                               else ""))
        if overlay.floor is not None:
            held = width is not None and width * 100.0 < overlay.floor
            ingredient("floor", overlay.floor, source=f"the bank: {overlay.floor_rule}",
                       applied=held,
                       detail=(f"the width was {row['width']:.3f} and is held at the floor"
                               if held else "the width is already at or above it"))
            if held:
                width = overlay.floor / 100.0
                row["width"] = overlay.floor

        # -- the client's record: their side leans the mid, their cost widens --
        client_side = client_after = None
        record = None
        if synthesis is not None and self.client.strip():
            record = synthesis.client_for(self.client, instrument=q.instrument, days=days,
                                          minimum=int(self.client_min))
            row["client"] = " ".join(self.client.split())
            if record is None:
                row["client_record"] = (f"no price has been shown to {row['client']} on the "
                                        f"{q.instrument.upper()} yet")
            else:
                row["client_scope"] = record.scope
                row["client_record"] = record.reading()
                row["client_enough"] = record.enough
                if record.enough:
                    row["client_side"] = record.side
                    row["client_after"] = record.after_move
                    if self.client_weight:
                        client_side = record.side
                        client_after = record.after_move
                else:
                    row["notes"].append(f"{row['client']}: {record.why_not}, so their "
                                        f"record moves nothing here")
            ingredient("client record", None, source="the archive", applied=False,
                       detail=row["client_record"])
        if (client_after is not None and client_after > 0 and width is not None
                and self.client_weight > 0):
            # The market has followed this client after they dealt: that is
            # what dealing with them costs, and the width absorbs it -- a
            # fraction of the mean move, and never more than the width itself.
            extra = min(self.client_weight * client_after, CLIENT_WIDEN_CAP * width * 100.0)
            row["client_widen"] = extra
            width += extra / 100.0
            row["width"] = width * 100.0
            ingredient("widening, client", extra, source="the client's record",
                       detail=f"the market moved {client_after:.3f} their way on average "
                              f"after {record.after_count} trade(s); {self.client_weight:g} "
                              f"of that, capped at {CLIENT_WIDEN_CAP:g} of the width")

        # A calendar spread's level statement is the *difference* of the two
        # legs' statements.  Taking the far leg's richness alone would shade a
        # 1M/3M spread by the whole of the 3M richness, which is not what
        # owning the spread exposes anybody to.
        if q.instrument == "spread":
            t_near = expiries[_key(q.expiry)][1]
            richness = (None if rich_at is None else rich_at(t) - rich_at(t_near))
            axe = (None if axe_at is None else axe_at(t) - axe_at(t_near))
            flow = (None if flow_at is None else flow_at(t) - flow_at(t_near))
            row["notes"].append(
                f"the width and the shading are taken across the spread: the bank rule is "
                f"matched on the {_key(q.expiry_far)} leg, and the richness and the axe are "
                f"the {_key(q.expiry_far)} figure less the {_key(q.expiry)} one")
        else:
            richness = None if rich_at is None else rich_at(t)
            axe = None if axe_at is None else axe_at(t)
            flow = None if flow_at is None else flow_at(t)
        row["richness"] = None if richness is None else richness * 100.0
        row["axe"] = axe
        row["flow"] = flow
        skew = skew_for(q, t, half_width=None if width is None else width / 2.0,
                        richness=richness, axe=axe, fair_weight=self.fair_weight,
                        axe_weight=self.axe_weight, cap_ratio=self.skew_cap,
                        bank_shift=q.sign * overlay.shift / 100.0,
                        flow=flow, flow_weight=self.flow_weight,
                        client=None if client_side is None else client_side * q.sign,
                        client_weight=self.client_weight)
        row["skew_fair"] = skew.fair * 100.0
        row["skew_axe"] = skew.axe * 100.0
        row["skew_flow"] = skew.flow * 100.0
        row["skew_client"] = skew.client * 100.0
        row["skew_bank"] = skew.bank * 100.0
        row["skew_total"] = skew.total * 100.0
        row["skew_cap"] = None if skew.cap is None else skew.cap * 100.0
        row["skew_capped"] = skew.capped
        row["skew_reason"] = skew.reason
        if overlay.shift_rule:
            row["notes"].append(f"bank shift: {overlay.shift_rule}")
        ingredient("shading, fair value", row["skew_fair"], source="implied against realized",
                   detail=("nothing shades this row" if richness is None else
                           f"the mark is {richness * 100.0:+.3f} rich to fair value at this "
                           f"tenor"))
        ingredient("shading, position", row["skew_axe"], source="the vega profile",
                   detail=("no position was given" if axe is None else
                           f"the position at this tenor is {axe:+.2f} of a full axe"))
        if flow is not None:
            ingredient("shading, tape", row["skew_flow"], source="the printed tape",
                       detail=f"the tape at this tenor is {flow:+.2f} of a full lean")
        if client_side is not None:
            ingredient("shading, client", row["skew_client"], source="the client's record",
                       detail=f"{row['client']} is {client_side:+.2f} on the side scale: "
                              f"+1 only ever lifts our offer, -1 only ever hits our bid; "
                              f"{self.client_weight:g} of that times the half width")
        ingredient("shift, bank", row["skew_bank"],
                   source=f"the bank: {overlay.shift_rule}" if overlay.shift_rule else "the bank",
                   detail="no shift rule matched" if not overlay.shift_rule else "")
        if skew.capped:
            ingredient("shading, capped", row["skew_total"], source="the cap on this panel",
                       detail=f"the total lean was held to {self.skew_cap:g} of a half width")

        our_mid = model + skew.total
        row["our_mid"] = our_mid * 100.0
        bid = ask = None
        ingredient("mid", row["our_mid"], source="the mark plus the shading",
                   detail=f"{row['model']:.3f} {row['skew_total']:+.3f}")
        if width is not None:
            bid, ask = our_mid - width / 2.0, our_mid + width / 2.0
            row["our_bid"], row["our_ask"] = bid * 100.0, ask * 100.0
            ingredient("bid / offer", None, source=f"the mid, {row['width']:.3f} wide",
                       detail=f"{row['our_bid']:.3f} / {row['our_ask']:.3f}")
        else:
            row["warnings"].append("no width: there is no built-in default, so there is no "
                                   "bid and no offer")
        if q.quote_kind == "premium":
            # Asked for live, so answered as a premium: our volatility two-way
            # at the strike, put through Black-76 against the feed's forward.
            # The volatilities stay on the row beside it.
            self._premium_row(row, q, t, bid, ask, forwards.get(_key(q.expiry)),
                              expiries[_key(_row_expiry(q))][0].date())

        # The market, when this exact instrument was quoted in the paste.  In
        # the row's own convention, like everything else on it.
        theirs = market.get(instrument_key(q))
        if theirs is not None:
            t_bid, t_ask = theirs.bid, theirs.ask
            if q.sign < 0:
                t_bid, t_ask = -t_ask, -t_bid
            row["market_bid"], row["market_ask"] = t_bid * 100.0, t_ask * 100.0
            row["market_mid"] = 0.5 * (t_bid + t_ask) * 100.0
            row["market_width"] = (t_ask - t_bid) * 100.0
            row["position"] = ("inside" if t_bid <= our_mid <= t_ask
                               else ("below" if our_mid < t_bid else "above"))
            row["edge"] = _hinge(our_mid, t_bid, t_ask) * 100.0
            row["notes"].append(f"line {theirs.line} of the market paste quotes this")
            if bid is not None:
                if bid > t_ask:
                    row["crossing"] = "our bid is through their offer"
                elif ask < t_bid:
                    row["crossing"] = "our offer is through their bid"
                elif bid > t_bid and ask < t_ask:
                    row["crossing"] = "inside their market on both sides"

        if evidence is not None and evidence.enough and evidence.model_read \
                and row["width_rung"] == "archive":
            row["flags"].append(f"{evidence.model_read} of {evidence.observations} "
                                f"observation(s) behind this width were transcribed by a "
                                f"language model and checked by the quote parser")

        if width is None:
            row["verdict"] = "no width"
        elif row["market_mid"] is None:
            row["verdict"] = "quoted"
        else:
            row["verdict"] = row["crossing"] or (
                "in line" if row["position"] == "inside" else
                f"our mid is {row['position']} their market")
        return row

    def _agent_verdict(self, row: dict, overlay, evidence) -> None:
        """The quoting agent's one opinion: is this width the one it trades at.

        The width we are about to show -- the bank's rule, or the fallback
        tier -- against the width the archive has seen this shown at.
        ``agrees``, ``tight``, ``wide``, ``no rule`` (the archive has a width
        and the bank has none, so the archive's is the one being shown),
        ``thin`` (not enough behind an archived width) or ``not read``.  A
        gap has to clear both a fraction of the archived width and an
        absolute floor before it is worth saying: without the floor a 0.08
        butterfly disagrees over four thousandths and every wing row carries
        a flag forever, which is a screen nobody reads.
        """
        if row["instrument"] == "spread":
            row["agent_note"] = "the archive keeps outrights, so a spread has no width of its own"
            return
        if evidence is None:
            row["agent_verdict"] = "thin"
            row["agent_note"] = ("the archive holds no width for this instrument at this "
                                 "tenor; nothing to compare the bank against")
            return
        if not evidence.enough:
            row["agent_verdict"] = "thin"
            row["agent_note"] = f"not enough behind a width here: {evidence.why_not}"
            return
        archived = evidence.median
        if overlay.spread is None:
            row["agent_verdict"] = "no rule"
            row["agent_note"] = (f"no bank rule matches this, and the archive has it "
                                 f"{archived:.3f} wide over {evidence.observations} "
                                 f"observation(s) from {evidence.sources} source(s)")
            return
        gap = overlay.spread - archived
        row["agent_gap"] = gap
        threshold = max(AGENT_MIN_GAP, abs(archived) * max(0.0, self.tolerance))
        shown_as = "bank" if overlay.spread_rule else "fallback tier"
        if abs(gap) <= threshold:
            row["agent_verdict"] = "agrees"
            row["agent_note"] = (f"the {shown_as} width and the archive agree to within "
                                 f"{threshold:.3f}")
            return
        row["agent_verdict"] = "tight" if gap < 0 else "wide"
        side = "tighter" if gap < 0 else "wider"
        age = "today" if evidence.newest_days < 1 else f"{evidence.newest_days:.0f} days ago"
        row["agent_note"] = (
            f"the {shown_as} would show {overlay.spread:.3f}, which is {abs(gap):.3f} {side} "
            f"than the {archived:.3f} this has been shown over {evidence.observations} "
            f"observation(s) from {evidence.sources} source(s), newest {age}")

    # -- the two leans ------------------------------------------------------
    def _axe(self, clock) -> tuple[object, dict]:
        if not self.vega_text.strip():
            return None, {"available": False,
                          "reason": "no vega profile was given, so no position is leaning the mid",
                          "profile": {}, "scale": self.vega_scale, "notes": [], "skipped": []}
        profile, notes, skipped = parse_vega_profile(self.vega_text)
        block = {"available": bool(profile), "reason": "",
                 "profile": {k: v for k, v in sorted(profile.items(), key=lambda kv: tenor_to_years(kv[0]))},
                 "scale": self.vega_scale, "notes": list(notes),
                 "skipped": [{"line": n, "text": t, "why": w} for n, t, w in skipped]}
        if not profile:
            block["reason"] = "every line of the vega profile was rejected"
            return None, block
        if not self.vega_scale or self.vega_scale <= 0:
            block["available"] = False
            block["reason"] = (
                "a vega profile was given but the axe scale is not set, so there is nothing to "
                "measure the position against. The scale is the position that counts as a full "
                "axe, in whatever unit the profile is written in")
            return None, block
        ts = sorted(tenor_to_years(k) for k in profile)
        vals = [profile[k] / self.vega_scale
                for k in sorted(profile, key=tenor_to_years)]
        block["reason"] = (f"{len(profile)} tenor(s), against an axe scale of "
                           f"{self.vega_scale:g}; held flat outside the pasted range")
        return (lambda t: _interp(ts, vals, t)), block

    def _flow(self, archive, ev, clock, expiries, forwards, hist) -> tuple[object, dict]:
        """What the printed tape has been doing, as a lean per tenor.

        The one inference in this panel.  The dissemination file publishes no
        buyer and no seller, so a print's side is decided by where it sat
        against **our own mark** -- which means the answer moves when the
        marks move, and the card says so rather than presenting it as
        something the file stated.

        Off unless ``flow_weight`` is set: a desk that has not looked at the
        tape should not have it moving a price, and an inference that quietly
        leans a quote is the failure this whole package is written against.
        """
        block = {"available": False, "reason": "", "weight": self.flow_weight,
                 "scale": self.flow_scale, "half_life": self.archive_half_life,
                 "lookback_days": self.archive_lookback_days,
                 "tolerance": self.flow_tolerance,
                 "buckets": [], "prints": [], "notes": [], "warnings": []}
        if archive is None:
            block["reason"] = ("no observation archive is loaded, so there is no printed tape "
                               "to read")
            return None, block
        from . import flow as flow_mod

        def mark_vol(days, strike, forward_unused=None, forward=None):
            """Our own volatility at that strike, on the marks being quoted."""
            fwd = forward if forward is not None else forward_unused
            try:
                t = max(float(days) / DAYS_IN_YEAR, 1e-9)
                dt = clock.datetime_from_years(t)
                return float(ev.strike_vol(dt, t, float(strike) / float(fwd))) * 100.0
            except Exception:            # noqa: BLE001 - a strike off the surface takes no side
                return None

        try:
            read = flow_mod.read_flow(
                archive, self.pair, asof=clock.now,
                mark_vol=lambda days, strike, is_call, fwd: mark_vol(days, strike, forward=fwd),
                hist_pair=(hist if hist is not None else None),
                half_life=self.archive_half_life, min_effective=self.archive_min_effective,
                lookback_days=self.archive_lookback_days, tolerance=self.flow_tolerance)
        except Exception as exc:         # noqa: BLE001 - a section that fails empties only itself
            block["reason"] = f"the printed tape could not be read: {exc}"
            return None, block

        block["notes"] = list(read.notes)
        block["buckets"] = [{
            "bucket": b.bucket, "prints": b.prints, "paid": b.paid, "given": b.given,
            "unclear": b.unclear, "calls": b.calls, "puts": b.puts,
            "paid_vega": b.paid_vega, "given_vega": b.given_vega, "net_vega": b.net_vega,
            "gross_vega": b.gross_vega, "effective": b.effective,
            "newest_days": b.newest_days, "enough": b.enough, "why_not": b.why_not,
            "net": b.net(self.flow_scale), "line": b.describe(),
        } for b in read.buckets]
        block["prints"] = [{
            "at": p.at, "days": p.days, "bucket": p.bucket, "strike": p.strike,
            "is_call": p.is_call, "vol": p.vol, "mark": p.mark, "notional": p.notional,
            "vega": p.vega, "side": p.side, "forward": p.forward, "why": p.why,
            "line": p.describe(),
        } for p in read.prints[-200:]]
        if not read.buckets:
            block["reason"] = ("the archive holds no printed trade for this pair that could be "
                               "turned into a volatility; 'Fetch from DTCC' and 'Scan folders' "
                               "fill it")
            return None, block
        block["available"] = True
        if not self.flow_weight:
            block["reason"] = ("the tape is read and shown, and it is leaning nothing: set a "
                               "flow weight above zero to let it shade the mid")
            return None, block
        ts, vals = [], []
        for b in read.buckets:
            net = b.net(self.flow_scale)
            if net is None:
                continue
            days = _bucket_days(b.bucket)
            if days is None:
                continue
            ts.append(days / DAYS_IN_YEAR)
            vals.append(net)
        if not ts:
            block["reason"] = ("no bucket has enough behind it to lean on; what printed is "
                               "shown and applied to nothing")
            return None, block
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        ts = [ts[i] for i in order]
        vals = [vals[i] for i in order]
        block["reason"] = (f"{len(ts)} bucket(s) with enough behind them, against a scale of "
                           f"{self.flow_scale:,.0f}; held flat outside them")
        return (lambda t: _interp(ts, vals, t)), block

    def _fair(self, book, hist, method) -> tuple[object, dict]:
        from .analytics import fair_value_table
        if hist is None:
            return None, {"available": False, "rows": [],
                          "reason": ("no historical workbook is loaded, so there is no realized "
                                     "volatility and no fair value to shade the mid with")}
        try:
            rows = fair_value_table(book, self.pair, hist, horizon_days=self.horizon_days,
                                    lookback_days=self.lookback_days, method=method,
                                    cut=self.cut)
        except Exception as exc:  # noqa: BLE001 - a section that fails empties only itself
            return None, {"available": False, "rows": [],
                          "reason": f"the fair value table could not be built: {exc}"}
        live = [r for r in rows if r.richness is not None]
        block = {
            "available": bool(live),
            "reason": "" if live else ("the fair value table has no richness in it; the pair may "
                                       "have no sheet in the historical workbook"),
            "horizon_days": self.horizon_days, "lookback_days": self.lookback_days,
            "rows": [{"tenor": r.tenor, "t": r.t, "implied": r.implied * 100.0,
                      "realized": None if r.realized is None else r.realized * 100.0,
                      "fair": None if r.fair is None else r.fair * 100.0,
                      "richness": None if r.richness is None else r.richness * 100.0}
                     for r in rows],
        }
        if not live:
            return None, block
        ts = [r.t for r in live]
        vals = [r.richness for r in live]
        return (lambda t: _interp(ts, vals, t)), block


def _prepare(book, pair: str, method: str | None):
    """The three things both panels need, checked the one way.

    A pair the book does not build and an interpolation nobody implements are
    the two ways either panel is asked for something that cannot exist, and
    they must read the same on both.
    """
    if book is None:
        raise ValueError("the market-maker screen needs a loaded book")
    if pair not in book:
        raise ValueError(f"{pair} is not built in this book; it holds {', '.join(book.pairs)}")
    surface = book[pair]
    resolved = method or surface.method
    if resolved not in INTERPOLATORS:
        raise ValueError(f"unknown interpolation method {resolved!r}; "
                         f"expected one of {INTERPOLATORS}")
    return surface, resolved, book.clock


def _rule_json(r: Rule) -> dict:
    from dataclasses import asdict
    out = asdict(r)
    out["describe"] = r.describe()
    return out


# -- reading a panel off a request ------------------------------------------

def _opt_float(payload, key, default=None):
    v = payload.get(key)
    if v in (None, "", "-"):
        return default
    return float(v)


def _opt_bool(payload, key, default):
    v = payload.get(key)
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _opt_tuple(payload, key, default):
    v = payload.get(key)
    if v in (None, "", []):
        return default
    if isinstance(v, str):
        v = [x for x in v.replace(",", " ").split() if x]
    return tuple(str(x) for x in v)


def _common(payload: dict) -> tuple[str, str, str | None, str, str]:
    """Pair, cut, method, butterfly convention and volatility unit.

    Both panels take these and both validate them the same way, because a
    butterfly that meant one thing to the fit and another to the quote would
    be two conventions on one screen.
    """
    pair = str(payload.get("pair") or "").strip().upper()
    if not pair:
        raise ValueError("a currency pair is required")
    vol_unit = str(payload.get("vol_unit") or "auto").strip().lower()
    if vol_unit not in VOL_UNITS:
        raise ValueError(f"unknown volatility unit {vol_unit!r}; expected one of {VOL_UNITS}")
    fly = str(payload.get("fly_convention") or "market").strip().lower()
    if fly not in FLY_CONVENTIONS:
        raise ValueError(f"unknown butterfly convention {fly!r}; "
                         f"expected one of {FLY_CONVENTIONS}")
    cut = str(payload.get("cut") or "NY").strip().upper()
    method = str(payload["method"]).strip() if payload.get("method") else None
    return pair, cut, method, fly, vol_unit


def _reversion_from_request(lo, hi) -> tuple[float, float] | None:
    """The mean-reversion range a panel typed, or ``None`` for the house one.

    Two empty boxes are not a range of nothing, they are "leave it to the
    house judgement" -- the same reading as an empty market box on the pricing
    screen handing the field back to the feed.  One box filled and the other
    empty is refused rather than half-read: a ceiling with no floor under it
    is a range somebody meant to type and did not finish.
    """
    blank = [v is None or (isinstance(v, str) and not v.strip()) for v in (lo, hi)]
    if all(blank):
        return None
    if any(blank):
        raise ValueError("the mean-reversion range needs both a floor and a ceiling, or "
                         "neither; leave both empty for the house range "
                         f"{MEAN_REVERSION_RANGE[0]:g}-{MEAN_REVERSION_RANGE[1]:g}")
    return check_reversion_range((lo, hi))


def check_panel_from_request(payload: dict) -> CheckPanel:
    """Build the check panel from a JSON body or a CLI namespace-like mapping.

    Deliberately short, and that is the change: the fields a fit needed --
    which knobs are free, what the target curve is, the mean-reversion range,
    whether to keep the marks -- are the marking agent's now, and are read by
    :func:`volkit.marking.fit_panel_from_request`.  A field this reader does
    not take is a setting that would silently do nothing, and a test pins the
    page's list against this function.
    """
    pair, cut, method, fly, vol_unit = _common(payload)
    marks = payload.get("marks") or None
    if marks is not None and not isinstance(marks, dict):
        raise ValueError("the marks to check against must be the object the marking card "
                         "handed back")
    near = _opt_float(payload, "near_edge", NEAR_EDGE)
    if not (0.0 <= near < 0.5):
        raise ValueError(
            f"the near-the-edge tolerance is a fraction of the quoted width, from zero "
            f"(warn about nothing that is inside) up to but not including a half (the mid "
            f"itself); {near:g} is not one")
    return CheckPanel(
        pair=pair, cut=cut, method=method,
        label=str(payload.get("label") or ""),
        text=str(payload.get("text") or ""),
        vol_unit=vol_unit,
        fly_convention=fly,
        near_edge=near,
        marks=marks,
    )


def quote_panel_from_request(payload: dict) -> QuotePanel:
    """Build the quote panel from a JSON body or a CLI namespace-like mapping."""
    pair, cut, method, fly, vol_unit = _common(payload)
    marks = payload.get("marks") or None
    if marks is not None:
        if not isinstance(marks, dict):
            raise ValueError("the marks to quote off must be the object a fit returned")
        named = str(marks.get("pair") or "").strip().upper()
        if named and named != pair:
            # The browser holds the fit and the pair selector separately, and
            # the two can be moved apart.  Quoting EURUSD off a USDJPY fit is
            # a wrong answer that reads perfectly well, so it is refused.
            raise ValueError(
                f"these marks were fitted on {named} and this panel is quoting {pair}; "
                f"fit {pair} before quoting it, or quote off the marks as they stand")
    return QuotePanel(
        pair=pair, cut=cut, method=method,
        label=str(payload.get("label") or ""),
        request_text=str(payload.get("request_text") or ""),
        fly_convention=fly,
        text=str(payload.get("text") or ""),
        vol_unit=vol_unit,
        marks=marks,
        vega_text=str(payload.get("vega_text") or ""),
        vega_scale=_opt_float(payload, "vega_scale", 0.0) or 0.0,
        fair_weight=_opt_float(payload, "fair_weight", 0.25),
        axe_weight=_opt_float(payload, "axe_weight", 0.5),
        skew_cap=_opt_float(payload, "skew_cap", 1.0),
        horizon_days=_opt_float(payload, "horizon_days", 30.0),
        lookback_days=_opt_float(payload, "lookback_days", None),
        fallback_tier=str(payload.get("fallback_tier") or "").strip(),
        fallback_multiplier=_opt_float(payload, "fallback_multiplier", 1.0),
        fallback_interpolate=_opt_bool(payload, "fallback_interpolate", False),
        archive_half_life=_opt_float(payload, "archive_half_life", 5.0),
        archive_min_effective=_opt_float(payload, "archive_min_effective", 2.0),
        archive_lookback_days=_opt_float(payload, "archive_lookback_days", 90.0),
        include_model_read=_opt_bool(payload, "include_model_read", True),
        tolerance=_opt_float(payload, "tolerance", AGENT_TOLERANCE),
        client=str(payload.get("client") or "").strip(),
        client_weight=_opt_float(payload, "client_weight", 0.5) or 0.0,
        client_min=int(_opt_float(payload, "client_min", 4) or 0),
        flow_weight=_opt_float(payload, "flow_weight", 0.0) or 0.0,
        flow_scale=_opt_float(payload, "flow_scale", 5_000_000.0) or 5_000_000.0,
        flow_tolerance=_opt_float(payload, "flow_tolerance", 0.03),
    )


# ===========================================================================
# 6. the bank's rules, as the browser posts them.  Learning a width is the
#    archive's job (`agent.learn_widths`): one pipeline for a width into the
#    bank, and this module proposes none.
# ===========================================================================


def rules_from_request(payload: dict) -> list[Rule]:
    return [rule_from_dict(r) for r in (payload.get("rules") or [])]
