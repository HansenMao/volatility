"""Making a two-way price: fit the curve and the wings, and quote off them.

The other three screens answer "what is this worth".  This one answers "what
do I show", which is a different question with three parts to it, and the
module keeps them apart because they fail for different reasons and a desk
needs to see which one broke.

Those three parts are **two panels**, and the split is the shape of the
screen.  :class:`Panel` reads a broker run and moves the marks -- parts 1 and
2 below -- and puts a price on nothing.  :class:`QuotePanel` reads a list of
instruments somebody has asked for, with no prices on them, and makes a
two-way in each -- part 3 -- and fits nothing.  A fit is a morning's decision
taken against a run that has just arrived; a quote is answered in seconds,
over and over, against whatever was fitted.  Tying the two together meant a
request could only be priced by re-running a fit against a market that had
nothing to do with it, and a market could not be fitted without also
producing prices in instruments nobody had asked for.

They meet at :func:`capture_marks`: the fit hands back the parameters it
arrived at, the browser holds them like every other piece of panel state
(§4 -- the server holds none), and posts them with the quote.  A quote given
no marks prices the surface as it stands, and says which of the two it did.

**1. The curve, fitted to a target at-the-money.**  ``fit_atm_curve`` puts the
backbone parameters through a target term structure -- the tenor overwrites
the marking screen has pinned, a pasted curve, or the at-the-money quotes
themselves.  It is a *cold* fit: the level parameters are read off the targets
and the two shape parameters are swept before anything is polished, so it does
not depend on a starting guess, exactly as ``sabr.calibrate`` and
``listed.fit_sabr`` do not.  For a cross the level is not the backbone's to
set -- it comes from the legs -- so what gets fitted there is the correlation
term structure instead, and the panel says so rather than fitting a parameter
that cannot move the answer.

**2. The wings, fine tuned against the quoted market.**  This one is
deliberately *not* a cold fit.  It starts from the marked surface, because
that is the thing being adjusted, and it moves the four smile parameters by an
additive shift across the whole curve (``VolSurface.param_shifts``).  A shift
rather than an overwrite because a broker run should move the level of a wing,
not flatten its term structure; curve-wide rather than per tenor because a
handful of quotes does not determine a shape, and a curve-wide shift that
cannot reach a tenor says so in its residual instead of quietly bending the
surface to a single quote.

The objective is a **hinge**: zero penalty anywhere inside the quoted bid and
offer, and the distance to the nearer side outside it.  That is literally the
brief -- our mid has to fall inside the market, not on top of somebody's mid --
and it is what lets a dozen quotes be satisfied at once when a least-squares
through their mids could satisfy none of them.  A hinge alone has a flat
bottom, so any point inside the market would do and the answer would be
arbitrary; a small pull toward the quoted mids picks one, and being small it
never overrides an actual violation.

**3. The quote.**  A mid is not a price.  The width comes from the pair's
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

Two things this deliberately does *not* do.  It does not apply a fair value or
a vega axe to a risk reversal or a butterfly: a break-even against realized
volatility is a statement about the *level*, and a pasted vega profile is a
vega position, and neither says anything about where the skew should be
marked.  Those rows show the model mid with the bank's width and say why there
is no shading.  And it does not invent a width: a quote no rule matches gets
no bid and no offer, with the reason on the row.

Volatilities are decimals inside this module and volatility points at the
panel boundary, the same split :mod:`volkit.listed` uses.
"""

from __future__ import annotations

import copy
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

from . import black, sabr
from .cross import CrossAtmCurve
from .knowledge import KnowledgeBank, PairKnowledge, Rule, rule_from_dict, suggest_rules
from .numerics import ConvergenceError
from .quotes import (FLY_CONVENTIONS, MarketQuote, QuoteError, VOL_UNITS,
                     instrument_key, parse_quotes, parse_requests, parse_vega_profile)
from .sabr import SabrParams
from .smile import INTERPOLATORS
from .surface import PARAM_NAMES
from .timeutil import DAYS_IN_YEAR, tenor_to_years

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
                atm_vol, black.dns_strike(1.0, atm_vol, t, self.s.conv), rho, nu, t, 1.0)
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
    """``Book.market_level`` at every expiry: spot, forward and pip for the
    premium conversions.  One lookup, the same one the forwards came from."""
    return {key: book.market_level(pair, t) for key, (_, t) in expiries.items()}


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
        out.append(MarketQuote(**{**vars(q), "bid": lo, "ask": hi, "quote_kind": "vol",
                                  "premium_unit": None, "notes": q.notes + (
            f"premium {q.bid:g}/{q.ask:g} {unit} ({how}) inverted against the forward "
            f"{fwd:g}: {lo * 100:.3f}/{hi * 100:.3f} vol. Undiscounted, so a touch low on a "
            f"long-dated option",)}))
        errors.append("")
    return out, errors


def resolve_expiries(clock, quotes) -> dict[str, tuple]:
    """Map every expiry mentioned in a run to a (datetime, years) pair.

    A tenor goes through ``tenor_to_years`` and back out of the clock, which is
    how the rest of the package turns a quoted tenor into a date; a written
    date is taken as it stands.
    """
    out: dict[str, tuple] = {}
    for q in quotes:
        for value in q.expiries():
            if value is None or _key(value) in out:
                continue
            if isinstance(value, str):
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
    total: float
    capped: bool
    cap: float | None
    reason: str = ""


def _interp(ts: list[float], values: list[float], t: float) -> float | None:
    if not ts:
        return None
    return float(np.interp(t, ts, values))


def skew_for(q: MarketQuote, t: float, *, half_width: float | None, richness, axe,
             fair_weight: float, axe_weight: float, cap_ratio: float,
             bank_shift: float) -> Skew:
    """How far to lean the mid, and why.

    Both leans point the same way: a rich market and a long position are both
    reasons to *want to sell*, and you attract a seller's trade by shading the
    price down, not up.  Both are capped as a fraction of the width, so an axe
    can lean the price inside the market but cannot on its own walk it out of
    the market -- which would stop being a quote and start being a bet.
    """
    level = q.instrument in _LEVEL_INSTRUMENTS or (
        q.instrument == "spread" and (q.leg or "atm") in _LEVEL_INSTRUMENTS)
    reason = ""
    fair = axe_part = 0.0
    if not level:
        reason = (f"a {q.instrument} is not a level, so neither the fair-value richness nor a "
                  f"vega position says where it should be marked; only the bank's own shift "
                  f"applies")
    else:
        if richness is not None:
            fair = -fair_weight * richness
        if axe is not None and half_width is not None:
            axe_part = -axe_weight * max(-1.0, min(1.0, axe)) * half_width
        elif axe is not None:
            reason = "there is no width for this quote, so the axe has nothing to lean against"
    total = fair + axe_part + bank_shift
    cap = None if half_width is None else cap_ratio * half_width
    capped = False
    if cap is not None and abs(total) > cap:
        total = math.copysign(cap, total)
        capped = True
    return Skew(fair=fair, axe=axe_part, bank=bank_shift, total=total,
                capped=capped, cap=cap, reason=reason)


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
    for key, (_, t) in expiries.items():
        fwd, real, note = _forward_at(book, pair, t)
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


@dataclass
class Panel:
    """One pair's fit: the market in, the marks out.

    The unit the browser owns and posts whole.  It reads a broker run, moves
    the curve and the wings, and reports where the surface sits against every
    quote it was shown.  It puts no price on anything -- that is
    :class:`QuotePanel`, and the two are separate because a fit is a morning's
    decision and a quote is answered in seconds, over and over, against it.
    """

    pair: str
    cut: str = "NY"
    method: str | None = None
    label: str = ""

    # the market
    text: str = ""
    vol_unit: str = "auto"
    fly_convention: str = "market"

    # the target at-the-money curve
    target_source: str = "overwrites"
    target_text: str = ""
    fit_curve: bool = True
    free: tuple[str, ...] | None = None
    #: The range the backbone's mean reversion is fitted in.  ``None`` is the
    #: house judgement, ``MEAN_REVERSION_RANGE``; a panel that names one is
    #: overriding a marking judgement for this fit and the run says so, because
    #: a fit made inside the house range and one made outside it must not read
    #: the same.
    reversion_range: tuple[float, float] | None = None

    # the wings
    tune_wings: bool = True
    smile_free: tuple[str, ...] = PARAM_NAMES
    mid_pull: float = 0.05
    max_nfev: int = 300

    apply: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- helpers ----------------------------------------------------------
    def _targets(self, surface, quotes, expiries) -> tuple[list[CurveTarget], str]:
        """Where the target at-the-money curve comes from, and what it is."""
        atm = surface.atm
        if self.target_source == "none":
            return [], "no target curve; the level was left as marked"
        if self.target_source == "overwrites":
            pinned = dict(atm.tenor_overwrites)
            if not pinned:
                raise ValueError(
                    "no tenor is pinned on the marking screen, so there is no target curve to "
                    "fit to. Pin the at-the-money levels you want, paste a curve, or fit to the "
                    "at-the-money quotes instead")
            targets = [CurveTarget(tenor.upper(), tenor_to_years(tenor), vol, "pinned tenor")
                       for tenor, vol in pinned.items()]
            return sorted(targets, key=lambda x: x.t), (
                f"{len(targets)} tenor(s) pinned on the marking screen")
        if self.target_source == "quotes":
            atms = [q for q in quotes if q.instrument == "atm"]
            if not atms:
                raise ValueError("the paste has no at-the-money quote to fit the curve to")
            targets = [CurveTarget(str(q.expiry), expiries[_key(q.expiry)][1], q.mid,
                                   f"mid of {q.bid * 100:.3f}/{q.ask * 100:.3f}") for q in atms]
            return sorted(targets, key=lambda x: x.t), (
                f"the mid of {len(targets)} at-the-money quote(s) in the paste")
        if self.target_source == "current":
            targets = [CurveTarget(tp.upper(), tenor_to_years(tp), atm.curve_vol(tenor_to_years(tp)),
                                   "the curve as it stands")
                       for tp in atm.tenor_points]
            return targets, "the curve as it stands, as a no-op check on the fit itself"
        if self.target_source == "paste":
            targets, bad = [], []
            for n, line in enumerate(self.target_text.splitlines(), start=1):
                body = line.split("#")[0].replace(",", " ").replace(":", " ").strip()
                if not body:
                    continue
                bits = body.split()
                if len(bits) < 2:
                    bad.append(f"line {n}: expected a tenor and a volatility")
                    continue
                try:
                    t = tenor_to_years(bits[0])
                    vol = float(bits[1])
                except Exception as exc:  # noqa: BLE001
                    bad.append(f"line {n}: {exc}")
                    continue
                targets.append(CurveTarget(bits[0].upper(), t, vol, f"pasted line {n}"))
            if bad:
                raise ValueError("the pasted target curve has bad lines: " + "; ".join(bad))
            if not targets:
                raise ValueError("the pasted target curve is empty")
            levels = [x.vol for x in targets]
            if max(levels) >= 1.0:
                targets = [CurveTarget(x.tenor, x.t, x.vol / 100.0, x.source) for x in targets]
                unit = "read as volatility points"
            else:
                unit = "read as decimals"
            return sorted(targets, key=lambda x: x.t), (
                f"{len(targets)} pasted line(s), {unit}")
        raise ValueError(f"unknown target source {self.target_source!r}; "
                         f"expected one of {TARGET_SOURCES}")

    # -- the run ----------------------------------------------------------
    def run(self, book) -> dict:
        surface, method, clock = _prepare(book, self.pair, self.method)
        knobs = _Knobs(surface.atm)

        out: dict = {
            "pair": self.pair, "cut": self.cut, "method": method, "label": self.label,
            "valuation": clock.now.isoformat(),
            "is_cross": knobs.is_cross,
            "knobs": list(knobs.available),
            "applied": bool(self.apply),
            "notes": list(self.notes), "warnings": [], "unavailable": {},
            "curve": None, "wings": None, "market": None, "marks": None,
        }

        # -- the paste -----------------------------------------------------
        run_ = parse_quotes(self.text, pair=self.pair, vol_unit=self.vol_unit,
                            fly_convention=self.fly_convention)
        quotes = list(run_.quotes)
        expiries = resolve_expiries(clock, quotes)
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

        # -- what the surface says before anything moves --------------------
        before = capture_marks(surface)
        before_knobs = knobs.get()
        before_shifts = {k: float(surface.param_shifts.get(k, 0.0)) for k in PARAM_NAMES}
        ev0 = Evaluator(surface, method, self.cut)
        model_before: list[float | None] = []
        row_errors: list[str] = []
        for q, unusable in zip(quotes, premium_errors):
            if unusable:
                model_before.append(None)
                row_errors.append(unusable)
                continue
            try:
                model_before.append(ev0.value(q, expiries, forwards))
                row_errors.append("")
            except (ValueError, ArithmeticError, ConvergenceError) as exc:
                model_before.append(None)
                row_errors.append(f"{type(exc).__name__}: {exc}")

        # -- 1. the curve ---------------------------------------------------
        curve_fit = None
        if self.fit_curve:
            try:
                targets, evidence = self._targets(surface, quotes, expiries)
                if targets:
                    curve_fit = fit_atm_curve(surface.atm, targets, free=self.free,
                                              reversion_range=self.reversion_range)
                    problems = knobs.set(curve_fit.after)
                    if problems:
                        raise ValueError("; ".join(problems))
                    surface.invalidate()
                    out["curve"] = self._curve_block(curve_fit, evidence, knobs)
                else:
                    out["unavailable"]["curve"] = evidence
            except (ValueError, ConvergenceError) as exc:
                out["unavailable"]["curve"] = f"{type(exc).__name__}: {exc}"
        else:
            out["unavailable"]["curve"] = "the curve fit is switched off on this panel"

        # -- 2. the wings ---------------------------------------------------
        tune = None
        # Anything whose value depends on the shape of the smile constrains the
        # wings.  A pure at-the-money quote does not, and is the curve's job.
        wing_quotes = [q for q, e in zip(quotes, row_errors) if not e and (
            q.instrument in ("rr", "fly", "outright")
            or (q.instrument == "spread" and q.leg in ("rr", "fly"))
            or (q.instrument == "structure" and any(l.kind != "atm" for l in q.legs)))]
        if self.tune_wings:
            try:
                if not wing_quotes:
                    raise ValueError(
                        "the paste has no risk reversal, butterfly or outright in it, so nothing "
                        "constrains the wings; the at-the-money quotes are the curve's job")
                tune = tune_smile_shifts(
                    surface, wing_quotes, expiries, forwards, method=method, cut=self.cut,
                    free=tuple(self.smile_free), mid_pull=self.mid_pull, max_nfev=self.max_nfev)
                out["wings"] = {
                    "before": {k: v for k, v in tune.before.items()},
                    "after": {k: v for k, v in tune.after.items()},
                    "free": list(tune.free),
                    "inside_before": tune.inside_before, "inside_after": tune.inside_after,
                    "quotes": len(wing_quotes),
                    "worst_before": tune.worst_before * 100.0,
                    "worst_after": tune.worst_after * 100.0,
                    "converged": tune.converged, "message": tune.message,
                    "evaluations": tune.evaluations, "slices": tune.slices,
                    "seconds": tune.seconds, "mid_pull": self.mid_pull,
                    "warnings": list(tune.warnings),
                }
                out["warnings"].extend(tune.warnings)
            except (ValueError, ConvergenceError) as exc:
                out["unavailable"]["wings"] = f"{type(exc).__name__}: {exc}"
        else:
            out["unavailable"]["wings"] = "the wing fine tune is switched off on this panel"

        # -- what the surface says now ---------------------------------------
        ev1 = Evaluator(surface, method, self.cut)
        model_after: list[float | None] = []
        for q, err in zip(quotes, row_errors):
            if err:
                model_after.append(None)
                continue
            try:
                model_after.append(ev1.value(q, expiries, forwards))
            except (ValueError, ArithmeticError, ConvergenceError):
                model_after.append(None)

        # -- the marks the quote panel will stand on --------------------------
        # Captured *before* the restore below, because that is the whole point
        # of the split: the fit's answer leaves here as numbers, and nothing of
        # it is left on the book unless somebody asked for that separately.
        out["marks"] = {
            **capture_marks(surface),
            "pair": self.pair, "cut": self.cut, "method": method,
            "fitted": bool(curve_fit is not None or tune is not None),
            "stamp": clock.now.isoformat(),
            "what": ", ".join(
                x for x in (
                    ("the at-the-money curve" if curve_fit is not None else ""),
                    ("the wings" if tune is not None else "")) if x) or "nothing",
        }
        out["market"] = self._market(quotes, expiries, model_before, model_after, row_errors,
                                     run_, forward_notes)

        # -- restore unless asked to keep -------------------------------------
        if not self.apply:
            problems = knobs.set(before_knobs)
            surface.set_param_shifts(before_shifts)
            surface.invalidate()
            if problems:
                out["warnings"].append(
                    "the marks could not be put back exactly after the fit: "
                    + "; ".join(problems) + ". Reload the workbook before trusting this book")
            elif capture_marks(surface) != before:
                out["warnings"].append(
                    "the marks were not put back exactly after the fit. Reload the workbook "
                    "before trusting this book")
        else:
            # What goes on the book is the marks that were handed back, not the
            # raw numbers the optimiser stopped at.  They differ: a knob leaves
            # here in volatility points and comes back divided by a hundred,
            # and that round trip moves about an eighth of all values by one
            # place in the last bit.  Left alone, a price then depended on
            # whether "keep the marks" had been ticked -- quoting off a book
            # the fit was applied to and quoting off the marks it handed back
            # gave prices a nanovol apart, which is nothing to a market and
            # everything to a screen that has to reproduce itself.  One number,
            # one spelling: the book holds exactly what the panel shows.
            for problem in apply_marks(surface, out["marks"]):
                out["warnings"].append(f"the fitted marks did not go on cleanly: {problem}")
            out["warnings"].append(
                f"the fitted marks were written into the loaded book for {self.pair}. They are "
                f"in memory only -- the workbook on disk is unchanged, and a reload discards them")
        out["warnings"].extend(surface.warnings[-6:])
        return out

    # -- pieces of the run --------------------------------------------------
    def _curve_block(self, fit: CurveFit, evidence: str, knobs: _Knobs) -> dict:
        return {
            "evidence": evidence,
            "source": self.target_source,
            "free": list(fit.free),
            "before": {k: _knob_points(k, v) for k, v in fit.before.items()},
            "after": {k: _knob_points(k, v) for k, v in fit.after.items()},
            "rows": [
                {"tenor": tg.tenor, "days": tg.t * DAYS_IN_YEAR, "source": tg.source,
                 "target": tg.vol * 100.0, "before": b * 100.0, "after": a * 100.0,
                 "diff": (a - tg.vol) * 100.0, "moved": (a - b) * 100.0}
                for tg, b, a in zip(fit.targets, fit.achieved_before, fit.achieved_after)
            ],
            "rmse": fit.rmse * 100.0, "max_error": fit.max_error * 100.0,
            "max_error_tenor": fit.max_error_tenor,
            "converged": fit.converged, "message": fit.message,
            "evaluations": fit.evaluations, "seconds": fit.seconds,
            "warnings": list(fit.warnings),
            # The range this fit was actually run in, and whether it was the
            # house one.  A fit made inside the marking judgement and one made
            # outside it must not read the same on the screen.
            "reversion_range": list(self.reversion_range or MEAN_REVERSION_RANGE),
            "reversion_house": self.reversion_range is None,
        }

    def _market(self, quotes, expiries, before, after, errors, run_, forward_notes) -> dict:
        """Where the surface sits against every quote the paste contained.

        No width and no price: this table answers "did the fit reach the
        market", which is the fit's own question.  What we would show is the
        quote panel's, and it is asked of the instruments in the request box
        rather than of the market that moved the marks.
        """
        rows = []
        for q, mb, ma, err in zip(quotes, before, after, errors):
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
                # A premium the fit could not turn into a volatility is shown as
                # it was written, in its own unit, and the row says so.
                "market_bid": q.bid * unit_scale, "market_ask": q.ask * unit_scale,
                "market_mid": q.mid * unit_scale, "market_width": q.spread * unit_scale,
                "model_before": None if mb is None else mb * 100.0,
                "model_after": None if ma is None else ma * 100.0,
                "model_move": None if (mb is None or ma is None) else (ma - mb) * 100.0,
                "position": None, "edge": None, "verdict": "",
                "notes": list(q.notes), "warnings": [],
            }
            if err:
                row["verdict"] = "not priced"
                row["warnings"].append(err)
            else:
                row["position"] = ("inside" if q.bid <= ma <= q.ask
                                   else ("below" if ma < q.bid else "above"))
                row["edge"] = _hinge(ma, q.bid, q.ask) * 100.0
                row["verdict"] = ("in line" if row["position"] == "inside"
                                  else f"the model is {row['position']} their market")
            rows.append(row)

        inside = sum(1 for r in rows if r["position"] == "inside")
        was_inside = sum(1 for r, mb in zip(rows, before)
                         if mb is not None and r["market_bid"] / 100.0 <= mb
                         <= r["market_ask"] / 100.0)
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
            "n_quotes": len(rows), "inside": inside, "inside_before": was_inside,
            "fly_convention": self.fly_convention,
        }


@dataclass
class QuotePanel:
    """What we would show, on the instruments somebody has asked for.

    It fits nothing.  Its inputs are the request box, the marks it is told to
    stand on, the knowledge bank, the position and the fair value -- and the
    market paste, optionally and for one purpose only: a request that names
    the same instrument as a quoted line carries that market beside our price,
    so "inside their market" is still a thing this screen can say.
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
    skew_cap: float = 1.0
    horizon_days: float = 30.0
    lookback_days: float | None = None

    # widths
    fallback_spread: float | None = None       # volatility points
    # The desk agent's rung on the width ladder: bank, then what the archive
    # has seen this shown at, then the typed fallback, then no price.  Off,
    # the ladder is the bank and the fallback and nothing else -- the archive
    # is evidence about the market and a desk may not trust it yet.
    use_archive_width: bool = False
    archive_half_life: float = 5.0
    archive_min_effective: float = 2.0
    archive_lookback_days: float = 90.0

    notes: tuple[str, ...] = field(default_factory=tuple)

    def run(self, book, *, bank: KnowledgeBank | None = None, hist=None,
            archive=None) -> dict:
        surface, method, clock = _prepare(book, self.pair, self.method)
        self._book = book

        out: dict = {
            "pair": self.pair, "cut": self.cut, "method": method, "label": self.label,
            "valuation": clock.now.isoformat(),
            "notes": list(self.notes), "warnings": [], "unavailable": {},
            "sheet": None, "bank": None, "axe": None, "fair": None, "marks": None,
        }

        asked = parse_requests(self.request_text, pair=self.pair,
                               fly_convention=self.fly_convention)
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
                                    fly_convention=self.fly_convention)
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

        expiries = resolve_expiries(clock, requests)
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

        # Everything that reads the surface happens inside the marks, and the
        # fair value with it: richness is the mark against realized, and the
        # mark being shaded is the one being quoted.  Measured outside, a fit
        # that moved the at-the-money half a point would be shaded by the
        # richness of the level it had just left.
        with applied_marks(surface, self.marks, out["warnings"]):
            rich_at, fair_block = self._fair(book, hist, method)
            out["fair"] = fair_block
            ev = Evaluator(surface, method, self.cut)
            rows = [self._row(q, ev, expiries, forwards, pk, rich_at, axe_at, market,
                              synthesis)
                    for q in requests]

        stood = dict(self.marks or {})
        out["marks"] = {
            "on_the_fit": bool(self.marks),
            "fitted": bool(stood.get("fitted")),
            "what": stood.get("what") or "",
            "stamp": stood.get("stamp") or "",
            "note": (f"quoted off the marks this panel was handed: "
                     f"{stood.get('what') or 'unnamed'}" if self.marks else
                     "quoted off the marks as they stand on the book; run the fit and hand its "
                     "answer over to price on that instead"),
        }
        out["sheet"] = {
            "rows": rows,
            "notes": list(asked.notes) + market_notes + forward_notes,
            "skipped": [{"line": n, "text": t, "why": w} for n, t, w in asked.skipped],
            "ignored": [{"line": n, "text": t, "why": w} for n, t, w in asked.ignored],
            "n_quotes": len(rows),
            "priced": sum(1 for r in rows if r["our_bid"] is not None),
            "matched": sum(1 for r in rows if r["market_mid"] is not None),
            "fly_convention": self.fly_convention,
            "fallback_spread": self.fallback_spread,
        }
        out["warnings"].extend(surface.warnings[-6:])
        return out

    # -- one row ------------------------------------------------------------
    def _archive(self, archive, clock) -> tuple[object, dict]:
        """The archive worked into evidence, or the reason it was not."""
        if not self.use_archive_width:
            return None, {"available": False, "used": False,
                          "reason": "the archive is not on the width ladder for this quote; "
                                    "tick 'widths from the archive' to put it there"}
        if archive is None:
            return None, {"available": False, "used": True,
                          "reason": "no observation archive is loaded, so the archive rung "
                                    "of the width ladder is empty"}
        from . import synthesis as syn
        made = syn.synthesize(archive, self.pair, asof=clock.now,
                              half_life=self.archive_half_life,
                              min_effective=self.archive_min_effective,
                              lookback_days=self.archive_lookback_days)
        return made, {"available": True, "used": True, "path": archive.path,
                      "counted": made.counted, "half_life": self.archive_half_life,
                      "min_effective": self.archive_min_effective,
                      "lookback_days": self.archive_lookback_days,
                      "widths": sum(1 for w in made.widths if w.enough),
                      "notes": list(made.notes)}

    def _premium_row(self, row: dict, q, t: float, bid, ask, forward) -> None:
        if bid is None:
            row["notes"].append("asked as a premium, but there is no width, so there is no "
                                "two-way to turn into one")
            return
        if forward is None:
            row["warnings"].append(f"asked as a premium, and there is no forward feed for "
                                   f"{self.pair} to price it against; the volatility two-way "
                                   f"stands")
            return
        level = self._level_at(t)
        try:
            prices = [float(black.price(forward, q.strike, v, t, bool(q.is_call)))
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

    def _level_at(self, t: float) -> dict:
        book = getattr(self, "_book", None)
        if book is None:
            return {}
        try:
            return book.market_level(self.pair, t)
        except (ValueError, KeyError):
            return {}

    def _row(self, q, ev, expiries, forwards, pk: PairKnowledge, rich_at, axe_at,
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
            "skew_fair": None, "skew_axe": None, "skew_bank": None,
            "skew_total": None, "skew_cap": None, "skew_capped": False, "skew_reason": "",
            "our_mid": None, "our_bid": None, "our_ask": None,
            "width": None, "width_source": None, "floor": None,
            "market_bid": None, "market_ask": None, "market_mid": None, "market_width": None,
            "position": None, "edge": None, "crossing": "",
            "richness": None, "axe": None, "verdict": "",
            "archive_width": None, "archive_observations": None, "archive_level": None,
            "archive_gap": None, "flags": [],
            # The bank's prose kept apart from the reader's own notes: a
            # note exists to be read, and burying it among parser chatter
            # is most of the way to not applying it at all.
            "advice": [], "notes": list(q.notes), "warnings": [],
        }
        try:
            # The book's convention throughout, then the sign once, here: a
            # request asked as 'JPY call over' is answered in that convention
            # and every number on the row turns with it.  §5 item 1 is what a
            # sign applied in two places costs.
            model = ev.value(q, expiries, forwards) * q.sign
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            row["verdict"] = "not priced"
            row["warnings"].append(f"{type(exc).__name__}: {exc}")
            return row
        row["model"] = model * 100.0

        overlay = pk.overlay(instrument=q.instrument, days=days, tenor=_key(q.expiry),
                             size=q.size, size_basis=q.size_basis, delta=q.delta,
                             fallback=None if self.fallback_spread in (None, "")
                             else float(self.fallback_spread))
        width = None if overlay.spread is None else overlay.spread / 100.0
        row["width"] = overlay.spread
        row["width_source"] = overlay.spread_rule or (
            "panel fallback" if width is not None else None)
        row["floor"] = overlay.floor
        row["advice"] = list(overlay.notes)
        if overlay.reason:
            row["warnings"].append(overlay.reason)
        row["notes"].extend(f"beaten: {b}" for b in overlay.beaten)
        # -- the archive rung, between the bank and the fallback -------------
        # Same ladder as ``agent.run`` and in the same order: a rule the desk
        # wrote beats what the market showed, and what the market showed
        # beats a number typed on a panel this morning.  A spread has no
        # width evidence of its own -- the archive keeps outrights.
        if synthesis is not None and q.instrument != "spread":
            evidence = synthesis.width_for(instrument=q.instrument, days=days, delta=q.delta)
            if evidence is not None and evidence.enough:
                row["archive_width"] = evidence.median
                row["archive_observations"] = evidence.observations
                # A bank rule beats the archive; the archive beats the typed
                # fallback, which the overlay has already folded in when no
                # rule matched (``spread`` set, ``spread_rule`` not).
                if overlay.spread_rule is None:
                    if overlay.spread is not None:
                        row["notes"].append("the panel fallback was not needed; the archive "
                                            "holds a width for this")
                    width = evidence.median / 100.0
                    row["width"] = evidence.median
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

        # A calendar spread's level statement is the *difference* of the two
        # legs' statements.  Taking the far leg's richness alone would shade a
        # 1M/3M spread by the whole of the 3M richness, which is not what
        # owning the spread exposes anybody to.
        if q.instrument == "spread":
            t_near = expiries[_key(q.expiry)][1]
            richness = (None if rich_at is None else rich_at(t) - rich_at(t_near))
            axe = (None if axe_at is None else axe_at(t) - axe_at(t_near))
            row["notes"].append(
                f"the width and the shading are taken across the spread: the bank rule is "
                f"matched on the {_key(q.expiry_far)} leg, and the richness and the axe are "
                f"the {_key(q.expiry_far)} figure less the {_key(q.expiry)} one")
        else:
            richness = None if rich_at is None else rich_at(t)
            axe = None if axe_at is None else axe_at(t)
        row["richness"] = None if richness is None else richness * 100.0
        row["axe"] = axe
        skew = skew_for(q, t, half_width=None if width is None else width / 2.0,
                        richness=richness, axe=axe, fair_weight=self.fair_weight,
                        axe_weight=self.axe_weight, cap_ratio=self.skew_cap,
                        bank_shift=q.sign * overlay.shift / 100.0)
        row["skew_fair"] = skew.fair * 100.0
        row["skew_axe"] = skew.axe * 100.0
        row["skew_bank"] = skew.bank * 100.0
        row["skew_total"] = skew.total * 100.0
        row["skew_cap"] = None if skew.cap is None else skew.cap * 100.0
        row["skew_capped"] = skew.capped
        row["skew_reason"] = skew.reason
        if overlay.shift_rule:
            row["notes"].append(f"bank shift: {overlay.shift_rule}")

        our_mid = model + skew.total
        row["our_mid"] = our_mid * 100.0
        bid = ask = None
        if width is not None:
            bid, ask = our_mid - width / 2.0, our_mid + width / 2.0
            row["our_bid"], row["our_ask"] = bid * 100.0, ask * 100.0
        if q.quote_kind == "premium":
            # Asked for live, so answered as a premium: our volatility two-way
            # at the strike, put through Black-76 against the feed's forward.
            # The volatilities stay on the row beside it.
            self._premium_row(row, q, t, bid, ask, forwards.get(_key(q.expiry)))

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

        if width is None:
            row["verdict"] = "no width"
        elif row["market_mid"] is None:
            row["verdict"] = "quoted"
        else:
            row["verdict"] = row["crossing"] or (
                "in line" if row["position"] == "inside" else
                f"our mid is {row['position']} their market")
        return row

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


def panel_from_request(payload: dict) -> Panel:
    """Build the fit panel from a JSON body or a CLI namespace-like mapping."""
    pair, cut, method, fly, vol_unit = _common(payload)
    source = str(payload.get("target_source") or "overwrites").strip().lower()
    if source not in TARGET_SOURCES:
        raise ValueError(f"unknown target source {source!r}; expected one of {TARGET_SOURCES}")
    return Panel(
        pair=pair, cut=cut, method=method,
        label=str(payload.get("label") or ""),
        text=str(payload.get("text") or ""),
        vol_unit=vol_unit,
        fly_convention=fly,
        target_source=source,
        target_text=str(payload.get("target_text") or ""),
        fit_curve=_opt_bool(payload, "fit_curve", True),
        free=_opt_tuple(payload, "free", None),
        # Named here rather than inside the helper so the guard that pins the
        # panel's field list against this reader can see them.
        reversion_range=_reversion_from_request(payload.get("reversion_lo"),
                                                payload.get("reversion_hi")),
        tune_wings=_opt_bool(payload, "tune_wings", True),
        smile_free=_opt_tuple(payload, "smile_free", PARAM_NAMES),
        mid_pull=_opt_float(payload, "mid_pull", 0.05),
        max_nfev=int(_opt_float(payload, "max_nfev", 300)),
        apply=_opt_bool(payload, "apply", False),
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
        fallback_spread=_opt_float(payload, "fallback_spread", None),
        use_archive_width=_opt_bool(payload, "use_archive_width", False),
        archive_half_life=_opt_float(payload, "archive_half_life", 5.0),
        archive_min_effective=_opt_float(payload, "archive_min_effective", 2.0),
        archive_lookback_days=_opt_float(payload, "archive_lookback_days", 90.0),
    )


# ===========================================================================
# 6. learning from a paste
# ===========================================================================


def learn_from_panel(payload: dict, clock) -> tuple[list[Rule], list[str], dict]:
    """Propose bank rules from the widths a pasted market actually showed.

    Returns the proposed rules, the notes explaining them, and the parse so the
    caller can report what the paste contained.  Nothing is saved here: the
    browser is shown the proposal and saves it, which is what keeps a stray
    paste from silently rewriting the desk's ladder.
    """
    panel = panel_from_request(payload)
    run_ = parse_quotes(panel.text, pair=panel.pair, vol_unit=panel.vol_unit,
                        fly_convention=panel.fly_convention)
    # Every quote, so a superseded one can still be measured: the expiry it
    # names has to resolve before its width can be attributed to a tenor.
    expiries = resolve_expiries(clock, run_.all_quotes)

    def days_of(q):
        got = expiries.get(_key(_row_expiry(q)))
        return None if got is None else got[1] * DAYS_IN_YEAR

    # Width evidence, not fit input: a tenor quoted twice is one live price
    # and two observations of how wide it is shown.  See ParsedRun.all_quotes.
    evidence = run_.all_quotes
    rules, notes = suggest_rules(evidence, days_of=days_of)
    if run_.superseded:
        notes.append(
            f"{len(run_.superseded)} quote(s) were superseded by a later quote of the same "
            f"thing. They do not go into a fit, but they are still evidence of how wide this "
            f"market is shown, so they are measured here")
    # Widths are in decimals inside the parser and volatility points in the bank.
    rules = [Rule(**{**vars(r), "value": r.value * 100.0}) for r in rules]
    return rules, notes, {
        "n_quotes": len(evidence), "vol_unit": run_.vol_unit,
        "skipped": [{"line": n, "text": t, "why": w} for n, t, w in run_.skipped],
        "notes": list(run_.notes),
    }


def rules_from_request(payload: dict) -> list[Rule]:
    return [rule_from_dict(r) for r in (payload.get("rules") or [])]
