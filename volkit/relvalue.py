"""Relative value across the expiry / strike surface.

The other sections of the Analysis screen each answer one question about one
number: what did the volatility realize, what does it cost to hold, what do
the legs imply for the cross.  A desk asks a different question in the
morning -- *which point on this surface is the one to sell* -- and answering
it means putting those together for every expiry and every strike at once.

That is what this module builds: a grid of tenors by delta points, and for
each cell a **score**, positive when the mark is rich.  Nothing here is a new
model.  Every signal is one of the comparisons the screen already makes, read
at a strike instead of at the at-the-money, and each is reported on its own
line beside the score so the number can be taken apart.

Five signals, in two groups.

The first three are the break-even identity of ``analytics.fair_value_table``
extended from the at-the-money to a strike, and they **add**:

* ``level``  -- the marked at-the-money less the realized volatility.  The
  same number for every cell of a tenor, because it is a statement about the
  level and not about the shape.
* ``shape``  -- the marked smile's own shape at this strike less the shape a
  SABR smile built from the *measured* ``(rho, nu)`` would show.  The realized
  volatility fixes the level of that comparison smile and the measured
  dynamics fix its wings, so what is left is the marked wing against the wing
  history actually delivered.  Those dynamics are measured over the
  **history** window and not over the realized lookback -- see the note at the
  ``vol_dynamics`` call in :func:`build`.  Zero at the at-the-money by
  definition -- the at-the-money *is* the level -- rather than by a
  near-cancellation, and *shown but not scored* there for that reason: a zero
  that is a statement rather than a measurement drags the cell toward the
  middle exactly as a counted absence would.
* ``carry``  -- minus the roll and the forward carry, valued as the
  volatility they are worth: ``-(roll_value + carry_value)``.  Minus, because
  an option that rolls *down* has to be cheaper to break even, so a mark that
  did not fall is rich by that much.  The forward's half is read **delta
  hedged**: a break-even volatility belongs to the strike, and the whole
  difference between writing that strike as a call and as a put is the
  first-order ``delta * (F2 - F1)``, which is a direction and not a
  volatility.  Left in, it pushed the put columns and the call columns of one
  row in opposite directions and the score changed sign across the strike
  axis for a reason that was not a mark.

``level + shape + carry`` is exactly ``implied(K) - fair(K)``, and at the
at-the-money column it is exactly ``fair_value_table``'s ``richness``.  A test
pins that, because two ways of computing one number is how they drift apart.

It also inherits that break-even's assumptions, and stretches one of them.
The gamma against theta is taken as ``(h/T) * vega * (sigma_R - sigma_I)``,
which is a first-order reading at the at-the-money and a rougher one out in a
wing, where the option's gamma over the horizon is not that share of its
whole life.  It is stated rather than corrected for, in the same spirit as
the section it comes from: this is a break-even, not a valuation.

The other two are comparisons of a different kind, so they are kept apart
rather than added in:

* ``history``  -- where this cell's own volatility sits in its own recent
  history, as a z-score.  The series is reconstructed from the sheet's
  at-the-money, risk reversal and butterfly at the same delta, which is the
  same arithmetic the marked wing is read with.
* ``triangle`` -- for a cross only: the cell's marked volatility against the
  one its two legs imply, out of ``analytics.triangle_table``.  A difference
  inside the triangle's own noise floor is not a difference and is reported
  but **not scored**, which is the rule that section already follows.

All five are already in **volatility points**, and that is the unit the score
is in: the composite is the weighted mean of whichever signals a cell has,
renormalised over the ones it has, and it reads as the number of volatility
points this mark is rich by.  Every cell reports which signals it used and
why the others are missing: a score that quietly averaged three things on one
row and five on the next would be a different statistic in each column.

The composite was a **z-score** until 2026-08-31 -- each signal divided by the
cell's own historical standard deviation -- and the desk asked for the points
back, because the two are not the same question.  *How unusual is this* is a
statistic about a series; *how much am I being paid* is the number a mark is
moved by and a price is made in, and a headline figure that cannot be added to
a bid has to be translated before it can be traded on.  Two things follow from
the change, and both are gains rather than costs:

* **A cell with no history now scores.**  The scale was the only thing the
  level, the shape and the carry needed a historical sheet for, so a pair the
  sheet does not quote scored nothing at all while three of its five signals
  were measured perfectly well.  Only ``history`` itself needs the series now.
* **The whole card is in one unit.**  The score, the richness under it and
  every signal inside it are the same number in the same units, and
  ``level + shape + carry`` is still exactly the richness.

What the z-score was there for is real and is **kept beside the value rather
than removed**: half a volatility point is a great deal on a one-year
at-the-money and nothing on a one-week 10 delta wing, and only the history
knows which.  So every signal still carries its ``z`` wherever a scale can be
measured, the cell still carries the ``scale`` and where it came from, and the
detail card shows both columns.  It is context now instead of the composite.
No history means no ``z`` and no ``history`` signal; it no longer means no
score, and the volatility points were always measured either way.

Three things are said out loud rather than left to be inferred, because each
of them is a way this grid could be over-read:

* **What the realized number is made of** travels with the row -- the spot
  leg, the forward leg, the swap-point volatility and its correlation with
  spot.  The one number that answers "does the carry support this volatility"
  is ``forward_vol_ratio``, and it is never the level of the swap points.
* **Which regime a tenor is in** (:func:`_regime`).  Past
  :data:`CARRY_DOMINANT_Z` the forward has already travelled further than the
  distribution is wide and the position is mostly a carry trade in an option's
  clothes; the row, the carry signal and the command line all say so, and
  nothing is silently reweighted on the strength of it.
* **Which signals are a property of the tenor rather than the strike**
  (:data:`SHARED`).  ``level`` is one number in all five cells of a row, and
  five copies of one observation is not five observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import black, sabr
from .analytics import MIN_ROLLED_DAYS, carry_table, triangle_table
from .history import DYNAMICS_DAYS, realized as measure_realized, vol_dynamics
from .numerics import ConvergenceError
from .timeutil import tenor_to_years

#: One column of the grid: a point on the strike axis.  ``target`` is the
#: ``analytics.TARGETS`` name, so the carry is read from the same table the
#: carry section shows rather than from a second implementation of the roll.
@dataclass(frozen=True)
class GridColumn:
    name: str
    label: str
    target: str
    delta: float
    is_call: bool
    #: The signed share of the risk reversal in this column, used both to
    #: reconstruct the historical series and to map the triangle's quoted
    #: differences onto a strike: a call is ``atm + fly + rr/2``.
    rr_share: float
    fly_share: float


COLUMNS: tuple[GridColumn, ...] = (
    GridColumn("10dp", "10d put", "10dp", 0.10, False, -0.5, 1.0),
    GridColumn("25dp", "25d put", "25dp", 0.25, False, -0.5, 1.0),
    GridColumn("atm", "ATM", "atm", 0.0, True, 0.0, 0.0),
    GridColumn("25dc", "25d call", "25dc", 0.25, True, +0.5, 1.0),
    GridColumn("10dc", "10d call", "10dc", 0.10, True, +0.5, 1.0),
)

#: What each signal is worth in the composite, before renormalisation over
#: the ones a cell actually has.  Declared once and overridable per request:
#: this is a marking judgement, not a result, and a desk that trusts its
#: history more than its realized measurement must be able to say so.
WEIGHTS: dict[str, float] = {
    "level": 0.30,
    "shape": 0.20,
    "carry": 0.20,
    "history": 0.20,
    "triangle": 0.10,
}

SIGNALS: tuple[tuple[str, str], ...] = (
    ("level", "implied less realized, at the money"),
    ("shape", "marked smile less the smile the measured dynamics imply"),
    ("carry", "minus the roll and the forward carry, as volatility"),
    ("history", "where this volatility sits in its own recent history"),
    ("triangle", "the cross's mark against what its legs imply"),
)

#: The three that are volatility points of richness on one footing and add up
#: to the fair-value answer.  The other two answer different questions and are
#: deliberately not summed with them.
ADDITIVE: tuple[str, ...] = ("level", "shape", "carry")

#: Signals that are a property of the **tenor** and not of the strike, so
#: every cell of a row carries the identical number.  ``level`` is one by
#: construction -- it is a statement about the level and the level is one
#: number per expiry -- and that has to be visible.  Shown five times across a
#: row with nothing to tie the cells together, one at-the-money mispricing
#: reads on a heat map as five independent confirmations that a whole tenor is
#: rich, which is precisely the over-reading a heat map invites.  Declared
#: here so the screen and the command line can mark it without either of them
#: knowing which signal it happens to be.
SHARED: tuple[str, ...] = ("level",)

#: How far back "recent history" reaches, in calendar days, for the history
#: signal and for the scale each z is read against.  Deliberately **not** the
#: realized lookback, which is matched to each tenor because a one-month
#: implied volatility forecasts one month.  How much a volatility usually
#: moves is a different measurement and a slower one: a month of a one-month
#: at-the-money is a handful of observations of a smooth series, and dividing
#: by its standard deviation turned an ordinary half-point of richness into
#: thirty standard deviations.  A year is the desk's own window for it.
HISTORY_DAYS = 250.0

#: A standard deviation measured on fewer observations than this is not a
#: scale, it is a rounding error with a denominator.  The cell says so and
#: falls back to the at-the-money series, or to nothing.
MIN_SCALE_OBS = 20

#: Above this the score stops being a reading and starts being an outlier;
#: it is reported, not clipped, but the summary says how many there are.  In
#: decimals, like every volatility inside the model: half a volatility point.
#: It was two standard deviations while the score was a z; half a point of
#: composite richness is the same kind of statement in the unit the desk
#: asked for.  It travels on the response as ``RelativeValue.extreme_score``
#: so the page's tint saturates exactly where the summary starts counting,
#: rather than holding a second copy of the number.
EXTREME_SCORE = 0.005

#: Where the carry stops being a detail of the option and starts being the
#: trade.  ``z = |ln(F/S)| / (sigma * sqrt(T))`` is the forward's own drift
#: measured in standard deviations of the diffusion the option has to travel
#: through: below one, the option is a bet on variance and the carry is a
#: correction; above it, the forward has already moved further than the
#: distribution is wide and the position is mostly a carry trade wearing an
#: option's clothes.  A one-week G10 cell and a two-year emerging-market cell
#: sit on opposite sides of this and were being weighted identically.
CARRY_DOMINANT_Z = 0.8

#: The same statement as a horizon: ``z(T) = c * sqrt(T) / sigma`` reaches
#: :data:`CARRY_DOMINANT_Z` at ``T = 0.64 * sigma**2 / c**2``.  The factor is
#: the threshold squared and is derived from it here rather than written down
#: twice, so the horizon and the z can never disagree about where the line is.
CARRY_HORIZON_FACTOR = CARRY_DOMINANT_Z ** 2

#: The three signals that read a volatility as the width of a lognormal
#: distribution.  On a managed float the carry is compensating jump and
#: devaluation risk rather than diffusion, so all three are doing more work
#: than usual and the grid says which share of the weight that is.
LOGNORMAL_SIGNALS: tuple[str, ...] = ("level", "shape", "history")

#: The suppressed-diffusion test, which takes **two** conditions and not one.
#:
#: The obvious reading of "realized volatility far below what the carry
#: implies" is the ratio ``|c| / sigma`` alone, and that is wrong: USDJPY on a
#: five point rate differential and ten volatility points scores 0.53, right
#: beside USDCNH's 0.50, and USDJPY is not managed in any sense.  What
#: separates them is the second condition -- a managed float has a *low*
#: realized volatility in absolute terms (USDCNH runs 4-5, USDJPY 9-10), which
#: is the suppressed diffusion itself rather than its consequence.  Both are
#: measured, both are reported on the row, and both have to hold.
#:
#: This is a heuristic on measured quantities and it is not the authority on
#: anything.  A hard, defended band is a **policy fact** and is marked in
#: ``files/bands.csv`` (§6); this only raises a hand on a pair whose numbers
#: have the shape.  A high-carry, high-volatility pair -- USDTRY at 35 and 25
#: -- is deliberately outside it: its diffusion is not suppressed, it is
#: merely expensive.
MANAGED_CARRY_RATIO = 0.35
MANAGED_VOL_CEILING = 0.08


class RelativeValueError(ValueError):
    """The grid could not be built at all -- not one cell of it."""


@dataclass(frozen=True)
class Signal:
    """One comparison, in volatility points, and how unusual that is.

    ``value`` is the comparison itself and is what the score is built from.
    ``z`` is the same number divided by how much this cell's volatility
    usually moves -- reported beside it and scored on by nothing, since the
    score is in volatility points.  It is present wherever the history can
    measure a scale and absent where it cannot, whether or not the signal was
    used: a signal that is counted and a signal that can be standardised are
    two different questions now.

    ``used`` is whether the score counted it.  A signal can have a value and
    still not be used -- a triangle difference inside its own noise floor is
    the case this was written for -- and the difference has to be visible,
    because a number on the screen that is silently ignored by the number
    beside it is the same failure as a box that is filled in and read by
    nobody.

    ``scorable`` is the other half of that: a value that is zero *by
    construction* rather than by measurement.  The at-the-money's shape is the
    one of those -- the at-the-money is the level, so it has no shape to be
    rich or cheap in -- and averaging it into the score is the very thing the
    module refuses to do with a missing signal, since a structural zero drags
    the cell toward the middle just as hard as a counted absence would.  It is
    still reported, with its value and its reason, because "zero" and "not
    measured" are different answers.
    """

    name: str
    label: str
    value: float | None = None
    z: float | None = None
    weight: float = 0.0
    used: bool = False
    message: str = ""
    #: True where this signal is one number for the whole tenor rather than
    #: one per strike -- see :data:`SHARED`.  The value is identical across
    #: the row; the z beside it is not, because each cell is standardised on
    #: its own scale.
    shared: bool = False
    #: False where the value is zero by construction; see above.
    scorable: bool = True


@dataclass(frozen=True)
class Cell:
    """One expiry, one strike, and what every comparison says about it."""

    column: str
    label: str
    delta: float
    is_call: bool
    strike: float | None            # absolute where there is a feed, else K/F
    strike_ratio: float
    implied: float
    signals: tuple[Signal, ...] = ()
    richness: float | None = None   # level + shape + carry, in volatility points
    #: The weighted mean of the signals this cell used, in **volatility
    #: points** and positive when rich -- the same unit as the values it is
    #: built from and as the richness above it.  It is not the richness: that
    #: is the three additive signals alone, and this is every signal the cell
    #: has, at the weights the desk declared.
    score: float | None = None
    used: tuple[str, ...] = ()
    #: The share of the declared weight the score actually rests on.  A cell
    #: scored on one signal and a cell scored on four both print a number, and
    #: only this says which is which.  It cannot reach one on a pair that has
    #: no triangle, and it is not meant to: a signal this pair cannot have is
    #: still a signal this score is missing.
    confidence: float = 0.0
    #: How much this cell's volatility usually moves, and which series that
    #: was measured on.  Context beside the signals rather than the score's
    #: denominator: see the module docstring.
    scale: float | None = None
    scale_source: str = ""
    history_mean: float | None = None
    history_sd: float | None = None
    observations: int = 0
    percentile: float | None = None
    message: str = ""

    @property
    def signal(self) -> dict[str, Signal]:
        return {s.name: s for s in self.signals}


@dataclass(frozen=True)
class TenorRow:
    """One expiry: the inputs every cell of it shares, and the cells."""

    tenor: str
    t: float
    expiry: str
    forward: float | None
    atm: float
    realized: float | None
    realized_basis: str
    window_days: float | None
    observations: int
    realized_rho: float | None
    realized_nu: float | None
    dynamics_source: str | None
    #: The window the dynamics were measured over.  Not ``window_days``: see
    #: the note beside the ``vol_dynamics`` call in :func:`build`.
    dynamics_days: float | None = None
    #: What the realized number is made of.  ``history.realized`` measures all
    #: of this and the grid used to keep only ``vol``, which left a cell scored
    #: rich on ``level`` with no way to say whether the richness is genuine
    #: forward variance or a level comparison against a thin estimate.  The
    #: one defensible "the carry supports the volatility" number is the
    #: **ratio** of the two realized volatilities -- what the forward actually
    #: did against what spot did -- and not the level of the swap points, so
    #: the ratio is computed here rather than left to be eyeballed.
    realized_spot: float | None = None
    realized_forward: float | None = None
    points_vol: float | None = None
    points_correlation: float | None = None
    realized_carry_rate: float | None = None
    forward_vol_ratio: float | None = None
    #: Which regime this tenor is in: the forward's own drift measured in
    #: standard deviations of the option's diffusion.  See :func:`_regime`.
    #: ``carry_horizon_days`` is the same statement as a maturity -- where the
    #: two cross for this pair's carry and volatility.
    spot: float | None = None
    carry_drift: float | None = None
    regime_z: float | None = None
    regime_z_realized: float | None = None
    carry_to_vol: float | None = None
    carry_horizon_days: float | None = None
    carry_dominant: bool = False
    cells: tuple[Cell, ...] = ()
    message: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelativeValue:
    """The whole grid, its inputs, and what it adds up to."""

    pair: str
    is_cross: bool
    legs: tuple[str, ...]
    has_feed: bool
    cut: str
    method: str | None
    horizon_days: float
    lookback_days: float | None
    history_days: float
    weights: dict[str, float]
    columns: tuple[dict, ...]
    signals: tuple[dict, ...]
    rows: tuple[TenorRow, ...] = ()
    #: The pair-level suppressed-diffusion reading: the median carry against
    #: realized volatility, the median realized volatility, and whether both
    #: conditions of :data:`MANAGED_CARRY_RATIO` hold.
    managed: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    #: :data:`EXTREME_SCORE`, carried so the screen tints on the server's own
    #: threshold instead of on a copy of it -- the same arrangement as the
    #: signal weights in ``/api/state``.
    extreme_score: float = EXTREME_SCORE


def resolve_weights(given=None) -> dict[str, float]:
    """The signal weights, defaulted and validated.

    A weight that cannot be read is an error rather than a silent fallback to
    the default: a screen that showed 0.3 and scored on 0.2 would be lying
    quietly, which is the one thing this project will not do.
    """
    out = dict(WEIGHTS)
    for key, value in (given or {}).items():
        name = str(key).strip().lower()
        if name not in WEIGHTS:
            raise RelativeValueError(
                f"unknown signal {key!r}; the weights are {sorted(WEIGHTS)}")
        if value in (None, ""):
            continue
        try:
            w = float(value)
        except (TypeError, ValueError):
            raise RelativeValueError(
                f"the weight for {name!r} must be a number, got {value!r}") from None
        if not math.isfinite(w) or w < 0:
            raise RelativeValueError(
                f"the weight for {name!r} must be zero or more, got {value!r}")
        out[name] = w
    if sum(out.values()) <= 0:
        raise RelativeValueError(
            "every signal weight is zero, so there is nothing to score with")
    return out


def _spot_and_forward(book, pair: str, t: float):
    """Spot and the outright forward to ``t``, or ``(None, None)``.

    Straight off the level lookup rather than out of the carry table: that
    table reports ``1.0`` and a warning when there is no feed, which is right
    for a strike held in moneyness and useless for a ratio of two prices.
    ``Book.market_level`` is that lookup, so a cross the feed builds from its
    legs is read here too.
    """
    try:
        level = book.market_level(pair, t)
    except Exception:  # noqa: BLE001 - the row says so and carries on
        return None, None
    if not level["feed"]:
        return None, None
    spot, forward = float(level["spot"]), float(level["forward"])
    if not (math.isfinite(spot) and math.isfinite(forward)) or spot <= 0 or forward <= 0:
        return None, None
    return spot, forward


def _regime(spot, forward, t: float, atm: float, realized) -> dict:
    """Is this tenor a bet on variance, or a carry trade in an option?

    ``z = |ln(F/S)| / (sigma * sqrt(T))`` puts the forward's drift and the
    option's diffusion in the same units, and :data:`CARRY_DOMINANT_Z` is the
    line between them.  Two of them are computed, and the difference between
    the two is the point:

    * against the **marked** volatility, ``regime_z`` says whether the carry
      dominates the distribution the market is pricing.  Past the line the
      ``carry`` signal is carrying most of what the cell is saying, and the
      row says so before that signal is read.
    * against the **realized** volatility, ``regime_z_realized`` says whether
      it dominated the distribution that actually happened.

    ``carry_to_vol`` is the second of those at a one-year reference,
    ``|c| / sigma_realized``, and it is a fixed reference on purpose: whether
    a pair is managed is a property of the *pair*, not of a tenor, and a
    per-tenor test would clear every one-week cell and flag every three-year
    one for the same pair.  It is evidence and not a verdict -- see
    :data:`MANAGED_CARRY_RATIO` for why one number is not enough.

    The weight is deliberately **not** tapered on the strength of any of this.
    The weights are a marking judgement and belong to the desk (§11's rule for
    the knowledge bank, and this grid's own), and a score that quietly
    reweighted itself would be a different statistic on every row with nothing
    on the screen to say so.  What changes is that the row, the carry signal
    and the command line all now say which regime the tenor is in, which is
    what was missing.
    """
    out: dict = {"spot": None, "carry_drift": None, "regime_z": None,
                 "regime_z_realized": None, "carry_to_vol": None,
                 "carry_horizon_days": None, "carry_dominant": False}
    if spot is None or forward is None or t <= 0:
        return out
    drift = math.log(forward / spot) / t
    out["spot"] = spot
    out["carry_drift"] = drift
    if atm > 0 and math.isfinite(atm):
        out["regime_z"] = abs(drift) * math.sqrt(t) / atm
        out["carry_dominant"] = out["regime_z"] >= CARRY_DOMINANT_Z
        if drift != 0.0:
            out["carry_horizon_days"] = (
                CARRY_HORIZON_FACTOR * atm * atm / (drift * drift) * 365.2425)
    if realized is not None and realized > 0 and math.isfinite(realized):
        out["regime_z_realized"] = abs(drift) * math.sqrt(t) / realized
        out["carry_to_vol"] = abs(drift) / realized
    return out


def suppressed_diffusion(rows) -> dict:
    """Does this pair's own history have the managed-float shape?

    A pair-level reading taken from the per-tenor evidence: the median carry
    against realized volatility, and the median realized volatility itself.
    Both conditions of :data:`MANAGED_CARRY_RATIO` have to hold, and the
    numbers behind them are returned whether they do or not -- a desk that
    disagrees with the thresholds can read the measurements.
    """
    ratios = [r.carry_to_vol for r in rows if r.carry_to_vol is not None]
    vols = [r.realized for r in rows if r.realized is not None]
    out = {"carry_to_vol": None, "realized": None, "managed": False}
    if not ratios or not vols:
        return out
    out["carry_to_vol"] = float(np.median(ratios))
    out["realized"] = float(np.median(vols))
    out["managed"] = bool(out["carry_to_vol"] >= MANAGED_CARRY_RATIO
                          and out["realized"] <= MANAGED_VOL_CEILING)
    return out


def _clean(value) -> float | None:
    """A float, or ``None`` where the measurement has none.

    ``history.realized`` uses ``nan`` for "not measured on this basis" in some
    fields and ``None`` in others; a grid that passed either straight out
    would put a ``NaN`` in a JSON response, which ``JSON.parse`` refuses.
    """
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _realized_parts(stats) -> dict[str, float | None]:
    """What the realized volatility is made of, kept rather than discarded.

    An implied volatility is a volatility of the **forward**, so the honest
    comparison for ``level`` is realized forward variance.  Whether the swap
    points are carrying that variance is answered by
    ``forward_vol_ratio = vol_forward / vol_spot`` -- what the forward
    actually did against what spot did.  It is deliberately *not* the level of
    the points: a large carry says nothing on its own about whether the
    forward is more volatile than spot, and reading it as though it did is the
    mistake this ratio exists to prevent.

    A ratio near one means the points moved with spot and the level comparison
    rests on the same variance either way; well above one means the swap
    points are contributing variance of their own, which is what a
    carry-heavy pair looks like and is exactly when ``level`` deserves a
    second look.
    """
    spot = _clean(stats.vol_spot)
    forward = _clean(stats.vol_forward)
    ratio = None
    if spot is not None and forward is not None and spot > 0:
        ratio = forward / spot
    return {
        "realized_spot": spot,
        "realized_forward": forward,
        "points_vol": _clean(stats.points_vol),
        "points_correlation": _clean(stats.points_correlation),
        "realized_carry_rate": _clean(stats.carry_rate),
        "forward_vol_ratio": ratio,
    }


def _column_series(hist, tenor: str, col: GridColumn):
    """This column's own historical volatility, and the reason it has none.

    Reconstructed from the sheet's own quotes the same way the marked wing is
    read off the marked smile: a call is ``atm + fly + rr/2``.  The sheet's
    butterfly convention is the sheet's, so where it quotes a *market*
    strangle rather than a smile butterfly this differs from the marked wing
    by the strangle-to-smile adjustment; that is small next to the standard
    deviation being measured, and it is stated on the panel rather than
    corrected for silently.
    """
    atm = hist.series("atm", tenor)
    if atm is None:
        return None, f"the sheet quotes no at-the-money volatility at {tenor}"
    if col.name == "atm":
        return np.asarray(atm, dtype=float), ""
    d = int(round(col.delta * 100))
    rr = hist.series("rr", tenor, d)
    bf = hist.series("bf", tenor, d)
    missing = [n for n, s in (("risk reversal", rr), ("butterfly", bf)) if s is None]
    if missing:
        return None, (f"the sheet quotes no {' or '.join(missing)} at {d} delta, {tenor}, "
                      f"so this wing has no history of its own")
    series = (np.asarray(atm, dtype=float)
              + col.fly_share * np.asarray(bf, dtype=float)
              + col.rr_share * np.asarray(rr, dtype=float))
    return series, ""


def _window_stats(series, hist, window_days: float, current: float):
    """Mean, standard deviation, count and percentile over the lookback."""
    i, j = hist.window(window_days)
    v = np.asarray(series[i:j], dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    sd = float(np.std(v, ddof=1)) if v.size > 1 else 0.0
    return {
        "n": int(v.size),
        "mean": float(np.mean(v)),
        "sd": sd,
        "percentile": float(np.mean(v <= current) * 100.0),
    }


def _measured_smile_shape(realized_vol: float, rho: float, nu: float, t: float,
                          strike_ratio: float, conv) -> float:
    """``vol(K) - vol(ATM)`` on a SABR smile built from the measured dynamics.

    The level comes from the realized volatility and the wings from the
    measured ``(rho, nu)``, so the shape returned is what the history says the
    smile should look like -- and it is read at the *marked* strike, because a
    comparison at two different strikes is not a comparison.
    """
    k_atm = float(black.dns_strike(1.0, realized_vol, t, conv))
    alpha = sabr.alpha_from_atm(realized_vol, k_atm, rho, nu, t, 1.0, 1.0)
    p = sabr.SabrParams(alpha, rho, nu, t, 1.0, 1.0)
    return float(sabr.lognormal_vol(strike_ratio, p)) - realized_vol


def _triangle_difference(row, col: GridColumn):
    """The triangle's quoted differences read at one strike.

    ``triangle_table`` compares the at-the-money, the risk reversal and the
    butterfly.  A call at that delta is ``atm + fly + rr/2``, so the same
    combination of the differences is the difference at the call's strike.
    The noise floor is combined the same way and without cancellation --
    errors do not have signs to net off.
    """
    tag = f"{int(round(col.delta * 100))}"
    parts = [("atm", 1.0)]
    if col.name != "atm":
        parts += [(f"fly{tag}", col.fly_share), (f"rr{tag}", col.rr_share)]
    value = 0.0
    noise = 0.0
    for key, share in parts:
        if key not in row.difference:
            return None, None, f"the triangle has no {key} at {row.tenor}"
        value += share * float(row.difference[key])
        noise += abs(share) * abs(float(row.noise.get(key, 0.0)))
    return value, noise, ""


def relative_value(book, pair: str, hist=None, *, horizon_days: float = 30.0,
                   lookback_days: float | None = None, history_days: float = HISTORY_DAYS,
                   method: str | None = None,
                   cut: str = "NY", annualisation: str = "weighted",
                   realized_basis: str = "auto", weights=None,
                   tenors=None, with_triangle: bool = True) -> RelativeValue:
    """Score every expiry and strike of one pair's surface for relative value.

    Returns a grid of :class:`Cell`, one per tenor and column, each carrying
    its own signals, the volatility-point richness three of them add to, and
    the composite score -- the weighted mean of the signals, in volatility
    points like the signals themselves.  Positive is **rich**: the mark is
    above what the comparison says it should be, and it is the side to sell.

    Sections that cannot be built are reported per cell and per row rather
    than dropped.  A missing forward feed costs the carry signal its
    forward-curve half and says so; a missing historical sheet costs the
    level, the shape, the history signal and every z, and leaves the carry to
    be scored on its own points.  A pair that is not a cross simply has no
    triangle signal, and the weight is renormalised away rather than counted
    as a zero.
    """
    if pair not in book:
        raise RelativeValueError(f"{pair} is not built in this book")
    surface = book[pair]
    clock = book.clock
    w = resolve_weights(weights)
    h = float(horizon_days) / 365.2425
    if h <= 0:
        raise RelativeValueError(f"the horizon must be positive, got {horizon_days!r} days")
    history_days = float(history_days)
    if history_days <= 0:
        raise RelativeValueError(
            f"the history window must be positive, got {history_days!r} days")
    names = list(tenors or book.data.tenor_points)
    info = book.data.pairs[pair]
    is_cross = bool(info.is_cross)
    unavailable: dict[str, str] = {}
    warnings: list[str] = []

    # The roll, once per column, out of the same table the carry section
    # shows.  Five passes over the tenors rather than a second implementation
    # of the rolldown: the number under this score and the number on the
    # carry table have to be the same number.
    carry: dict[str, dict[str, object]] = {}
    for col in COLUMNS:
        try:
            carry[col.name] = {r.tenor: r for r in carry_table(
                book, pair, horizon_days=horizon_days, target=col.target,
                method=method, cut=cut, tenors=names)}
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            carry[col.name] = {}
            unavailable[f"carry.{col.name}"] = str(exc)

    tri: dict[str, object] = {}
    tri_note = ""
    if is_cross and with_triangle:
        try:
            tri = {r.tenor: r for r in triangle_table(
                book, pair, method=method, cut=cut, tenors=names)}
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            tri_note = str(exc)
    elif is_cross:
        tri_note = "the triangle was switched off for this run"
    else:
        tri_note = f"{pair} is not a cross, so its legs imply nothing about it"
    if tri_note:
        unavailable["triangle"] = tri_note
    if hist is None:
        unavailable["history"] = (
            "no historical sheet for this pair, so there is no realized volatility to "
            "compare against, no history signal and no scale to say how unusual a "
            "difference is. The score is in volatility points and does not need one, so "
            "the carry still measures and still scores")

    has_feed = bool(book.market_level(pair, 1.0)["feed"])
    if not has_feed:
        warnings.append(
            f"no forward feed for {pair}: the strike is held in moneyness rather than in "
            f"price, so the roll carries the term structure alone and the forward curve's "
            f"own carry is missing from every carry signal")
    if hist is not None:
        warnings.append(
            f"the shape signal's (rho, nu) are measured over their own {DYNAMICS_DAYS:.0f}-day "
            f"window rather than over each tenor's realized lookback, for the same "
            f"reason the scale beside it is: how a volatility moves with spot and how much it moves "
            f"are properties of the process and need more paired observations than a "
            f"realized volatility needs returns. On the lookback they were blank at every "
            f"short tenor, and at every tenor at once on a lookback under about a month")
        warnings.append(
            "the shape signal compares your smile against a SABR smile built from the "
            "measured (rho, nu). SABR has no mean reversion and real volatility has, so "
            "the measured nu falls away at long tenors and the comparison smile flattens "
            "with it; a wing can be reported rich there for that reason alone. It is the "
            "same caveat the rho / nu card carries")
        warnings.append(
            "a wing's history is reconstructed as atm + fly + rr/2 from the sheet's own "
            "columns. Where the sheet quotes a market strangle rather than a smile "
            "butterfly the reconstruction differs from the marked wing by that adjustment, "
            "which is small next to the standard deviation it is being measured with")

    rows: list[TenorRow] = []
    for tenor in names:
        t = tenor_to_years(tenor)
        expiry = clock.datetime_from_years(t)
        warn: list[str] = []
        try:
            atm = float(surface.atm_vol(expiry, cut))
        except (ValueError, ArithmeticError, ConvergenceError) as exc:
            rows.append(TenorRow(tenor=tenor, t=t, expiry="", forward=None, atm=float("nan"),
                                 realized=None, realized_basis="", window_days=None,
                                 observations=0, realized_rho=None, realized_nu=None,
                                 dynamics_source=None,
                                 message=f"{tenor} has no marked at-the-money: {exc}"))
            continue

        window = float(lookback_days) if lookback_days else t * 365.2425
        rv = None
        basis = ""
        obs = 0
        parts: dict[str, float | None] = {}
        if hist is not None:
            try:
                stats = measure_realized(hist, window, annualisation=annualisation,
                                         basis=realized_basis, basis_tenor=tenor)
                rv, basis, obs = stats.vol, stats.basis, stats.observations
                parts = _realized_parts(stats)
                warn.extend(stats.warnings)
            except Exception as exc:  # noqa: BLE001 - one row, reported in place
                warn.append(f"{tenor}: no realized volatility over {window:.0f} days ({exc})")

        # The shape signal's comparison smile is built from measured dynamics,
        # and those are read over their own window rather than over the
        # realized lookback -- the same distinction, and for the same reason,
        # as the scale above.  A spot/volatility correlation and a vol of vol
        # are properties of the process, not forecasts over a horizon, and
        # they need *more* paired observations than a realized volatility
        # needs returns.  Measured on the lookback they were therefore blank
        # at every short tenor, and on a lookback under about thirty days they
        # were blank at every tenor at once: the at-the-money reported a shape
        # of zero (it has none by statement) and all four wings reported no
        # shape at all, which reads as a signal that does not work rather than
        # as a window that was too short.  Never *shorter* than the lookback,
        # so asking for two years of realized data does not quietly measure
        # the dynamics on one.
        #
        # Its own constant and not ``history_days``, close as the two
        # arguments are: ``history_days`` measures the scale every z is read
        # against, and a knob that also moved the shape signal's own
        # measurement would change the volatility-point column -- which is
        # the score -- as a side effect of rescaling those z's.  A test pins
        # that column against it.
        dyn_window = max(window, float(DYNAMICS_DAYS))
        rho = nu = None
        source = None
        if hist is not None:
            try:
                dyn = vol_dynamics(hist, dyn_window, tenor)
                rho, nu, source = dyn.rho, dyn.nu, dyn.source
                warn.extend(dyn.warnings)
            except Exception as exc:  # noqa: BLE001 - one signal, reported in place
                warn.append(f"{tenor}: no measured volatility dynamics over "
                            f"{dyn_window:.0f} days ({exc})")

        # The feed first, the carry table second.  Reading it only off the
        # carry table left a tenor that could not be rolled -- every short one
        # under a month-long horizon -- with no forward at all and therefore
        # no absolute strikes, on a pair whose forward the feed was quoting
        # perfectly well.  The two are the same number where both exist:
        # ``analytics._forward_at`` asks this same feed.
        spot, forward = _spot_and_forward(book, pair, t)
        if forward is None:
            for col in COLUMNS:
                row = carry[col.name].get(tenor)
                if row is not None and row.expiry and math.isfinite(row.forward):
                    forward = float(row.forward)
                    break

        regime = _regime(spot, forward, t, atm, rv)
        if regime["carry_dominant"]:
            warn.append(
                f"the forward to {tenor} has drifted {regime['regime_z']:.2f} standard "
                f"deviations of this option's own volatility "
                f"({regime['carry_drift'] * 100:+.2f}%/yr of carry against a "
                f"{atm * 100:.2f}% mark). Past {CARRY_DOMINANT_Z:g} the position is mostly a "
                f"carry trade in an option's clothes and the carry signal is carrying most "
                f"of what this row says; the weight is still yours to set")

        cells = [_cell(surface, hist, tri.get(tenor), tri_note, carry, col, tenor, t, expiry,
                       atm, rv, rho, nu, window, history_days, h, method, cut, w, forward,
                       regime)
                 for col in COLUMNS]
        rows.append(TenorRow(
            tenor=tenor, t=t, expiry=expiry.isoformat(), forward=forward, atm=atm,
            realized=rv, realized_basis=basis, window_days=window if hist is not None else None,
            observations=obs, realized_rho=rho, realized_nu=nu, dynamics_source=source,
            dynamics_days=dyn_window if hist is not None else None,
            cells=tuple(cells), warnings=tuple(dict.fromkeys(warn)), **parts, **regime,
        ))

    # Managed-float evidence is a statement about the pair, so it is read off
    # the whole grid and said once rather than repeated on nine rows.
    managed = suppressed_diffusion(rows)
    if managed["managed"]:
        share = sum(w[n] for n in LOGNORMAL_SIGNALS) / sum(w.values())
        warnings.append(
            f"{pair} realized {managed['realized'] * 100:.2f}% against a carry worth "
            f"{managed['carry_to_vol']:.2f} of it, which is the shape of a managed float: "
            f"the carry is compensating jump and devaluation risk rather than diffusion. "
            f"The level, shape and history signals all read a volatility as the width of a "
            f"lognormal distribution and are {share * 100:.0f}% of the declared weight here. "
            f"This is a heuristic on the numbers; a hard defended band is a policy fact and "
            f"is marked in bands.csv")

    return RelativeValue(
        pair=pair, is_cross=is_cross, legs=tuple(info.legs), has_feed=has_feed,
        cut=cut, method=method,
        horizon_days=float(horizon_days), lookback_days=lookback_days,
        history_days=history_days, weights=w,
        columns=tuple({"name": c.name, "label": c.label, "delta": c.delta,
                       "is_call": c.is_call, "target": c.target} for c in COLUMNS),
        signals=tuple({"name": n, "label": l, "weight": w[n], "shared": n in SHARED}
                      for n, l in SIGNALS),
        rows=tuple(rows), summary=summarise(rows), unavailable=unavailable,
        managed=managed, warnings=tuple(dict.fromkeys(warnings)),
        extreme_score=EXTREME_SCORE,
    )


def _cell(surface, hist, tri_row, tri_note, carry, col: GridColumn, tenor: str, t: float, expiry,
          atm: float, rv, rho, nu, window: float, history_days: float, h: float,
          method, cut: str, weights: dict[str, float], forward, regime=None) -> Cell:
    """One cell: its strike, its five signals, and what they come to."""
    # -- where on the smile this cell is -------------------------------------
    try:
        if col.name == "atm":
            k = float(black.dns_strike(1.0, atm, t, surface.conv))
            implied = atm
        else:
            k, implied = surface.delta_strike(expiry, col.delta, col.is_call, method, cut)
            k, implied = float(k), float(implied)
    except (ValueError, ArithmeticError, ConvergenceError) as exc:
        return Cell(column=col.name, label=col.label, delta=col.delta, is_call=col.is_call,
                    strike=None, strike_ratio=float("nan"), implied=float("nan"),
                    message=f"{col.label} at {tenor} has no strike on this smile: {exc}")

    signals: list[Signal] = []

    def add(name: str, value=None, message: str = "", *, scorable: bool = True) -> None:
        label = dict(SIGNALS)[name]
        signals.append(Signal(name=name, label=label, value=value,
                              weight=weights[name], message=message,
                              shared=name in SHARED, scorable=scorable))

    # -- implied against realized: the level, and then the shape -------------
    if rv is None:
        add("level", message="no realized volatility over this window")
        add("shape", message="no realized volatility to build a comparison smile on")
    else:
        add("level", atm - rv)
        if col.name == "atm":
            # The at-the-money *is* the level.  Saying its shape is zero is a
            # statement, not a cancellation that happens to come out small --
            # and a statement is not a measurement, so it is shown and not
            # scored.  Averaged in, this zero pulled every at-the-money score
            # a fifth of the way to the middle for no reason anybody could
            # point at, which is exactly what the module refuses to do with a
            # signal that is missing.  The value stays 0.0 so that
            # ``level + shape + carry`` is still the richness exactly.
            add("shape", 0.0, "the at-the-money is the level, so it carries no shape "
                              "to be rich or cheap in, and none is scored here",
                scorable=False)
        elif rho is None or nu is None:
            add("shape", message="no measured volatility dynamics to imply a smile shape")
        else:
            try:
                measured = _measured_smile_shape(rv, rho, nu, t, k, surface.conv)
                add("shape", (implied - atm) - measured)
            except (ValueError, ArithmeticError, ConvergenceError) as exc:
                add("shape", message=f"no SABR smile at the measured (rho, nu): {exc}")

    # -- the roll and the forward carry, as the volatility they are worth ----
    row = carry[col.name].get(tenor)
    if row is None:
        add("carry", message=f"{col.label} was not rolled at {tenor}")
    elif not row.expiry:
        add("carry", message=(row.warnings[0] if row.warnings else
                              f"{tenor} could not be rolled over this horizon"))
    else:
        value, note = _carry_signal(row, t, h)
        # A carry-dominated tenor is not a broken measurement -- the number is
        # right -- but it is a different kind of number, and the cell says so
        # where the signal is read rather than only in the row's warnings.
        if value is not None and (regime or {}).get("carry_dominant"):
            note = (note + "; " if note else "") + (
                f"the forward's drift is {regime['regime_z']:.2f} standard deviations of "
                f"this option's volatility, so this tenor is carry dominated and the signal "
                f"is most of what the cell says")
        add("carry", value, note)

    # -- where this volatility sits in its own history -----------------------
    scale = None
    scale_source = ""
    mean = sd = None
    n_obs = 0
    percentile = None
    hist_message = ""
    if hist is None:
        hist_message = "no historical sheet for this pair"
    else:
        series, why = _column_series(hist, tenor, col)
        if series is None:
            hist_message = why
        else:
            stats = _window_stats(series, hist, history_days, implied)
            if stats is None:
                hist_message = f"no readable rows in the last {history_days:.0f} days"
            elif stats["n"] < MIN_SCALE_OBS:
                hist_message = (f"{stats['n']} observation(s) in the last {history_days:.0f} days "
                                f"is too few to measure how much this volatility moves")
            elif stats["sd"] <= 0:
                hist_message = "this series never moved over the window, so it has no scale"
            else:
                mean, sd = stats["mean"], stats["sd"]
                n_obs, percentile = stats["n"], stats["percentile"]
                scale, scale_source = sd, col.name
    if scale is None and hist is not None and col.name != "atm":
        # A wing the sheet does not quote still moves, and the at-the-money it
        # sits on is the best measured statement of how much.  Substituted
        # openly: ``scale_source`` says which series the score was divided by,
        # because a z-score is only as meaningful as its denominator.
        atm_series, _ = _column_series(hist, tenor, COLUMNS[2])
        if atm_series is not None:
            atm_stats = _window_stats(atm_series, hist, history_days, atm)
            if atm_stats and atm_stats["n"] >= MIN_SCALE_OBS and atm_stats["sd"] > 0:
                scale, scale_source = atm_stats["sd"], "atm"

    if mean is None or sd in (None, 0.0):
        add("history", message=hist_message or "no history for this cell")
    else:
        add("history", implied - mean)

    # -- the cross against its own legs --------------------------------------
    if tri_row is None:
        add("triangle", message=tri_note or f"no triangle at {tenor}")
    else:
        value, noise, why = _triangle_difference(tri_row, col)
        if value is None:
            add("triangle", message=why)
        elif noise is not None and abs(value) <= noise:
            add("triangle", value, (
                f"inside the triangle's own noise floor of {noise * 100:.4f} volatility "
                f"points, so it is not a difference and is not scored"))
        else:
            add("triangle", value)

    # -- the score ------------------------------------------------------------
    by_name = {s.name: s for s in signals}
    additive = [by_name[n].value for n in ADDITIVE]
    richness = None if any(v is None for v in additive) else float(sum(additive))

    def _counts(s: Signal) -> bool:
        """Whether the score averages this signal in.

        The score is in volatility points, so a **scale is not part of this
        question** -- it used to be, and a cell whose history could not
        measure one then scored nothing at all, in every column, even where
        the level, the shape and the carry had all been measured perfectly
        well.  What is left is the three reasons a value is shown and not
        counted, each of them a statement about the value itself.
        """
        if s.value is None or s.weight <= 0:
            return False
        if not s.scorable:
            return False              # zero by construction: shown, not counted
        if s.name == "triangle" and s.message:
            return False              # inside the noise floor: shown, not counted
        return True

    # The z follows the **scale**, not the score: it is the reading of how
    # unusual this difference is against how much this cell's volatility
    # usually moves, so it is attached wherever the history can measure one
    # and is absent where it cannot, whether or not the value was counted.
    scored = [Signal(s.name, s.label, s.value,
                     None if (scale is None or s.value is None) else s.value / scale,
                     s.weight, _counts(s), s.message, s.shared, s.scorable)
              for s in signals]
    used = [s for s in scored if s.used]
    total = sum(s.weight for s in used)
    declared = sum(weights.values())
    score = (sum(s.weight * s.value for s in used) / total) if total > 0 else None

    return Cell(
        column=col.name, label=col.label, delta=col.delta, is_call=col.is_call,
        strike=(None if forward is None else k * forward), strike_ratio=k, implied=implied,
        signals=tuple(scored), richness=richness, score=score,
        used=tuple(s.name for s in used),
        confidence=(total / declared if declared > 0 else 0.0),
        scale=scale, scale_source=scale_source,
        history_mean=mean, history_sd=sd, observations=n_obs, percentile=percentile,
    )


def _carry_signal(row, t: float, h: float):
    """``-(roll_value + carry_value)`` for one rolled row, and any caveat.

    The break-even multiplier is the ratio of the two vegas rather than the
    ``sqrt(T)`` proxy, exactly as in ``analytics.fair_value_table``: once the
    forward curve has slope in it the fixed strike is not the same distance
    from the two forwards.  The sign is the one that makes this a *richness*:
    an option that rolls down has to be cheaper to break even, so a mark that
    did not fall by the roll is rich by the difference.

    The forward's own carry is read **delta hedged** (``carry_hedged``, not
    ``carry_pnl``), and that is the whole of what this grid asks of it.  A
    break-even volatility is a property of the strike: the same strike written
    as a call and as a put is one mark, and put-call parity puts the entire
    difference between the two revaluations in the first-order term
    ``delta * (F2 - F1)``, which a hedge removes and which has nothing to say
    about volatility.  Read unhedged, this signal carried that term with the
    option's own direction on it -- around a quarter of the forward move at a
    25 delta strike, worth a volatility point a year on a carried pair -- so
    the put columns and the call columns of one row were pushed in *opposite*
    directions and the composite score changed sign across the strike axis for
    a reason that was not a mark.  The at-the-money column barely showed it,
    because a delta-neutral straddle has almost no first-order term, which is
    what made the flip look like it belonged to the wings.  What is left is
    the gamma over the move, which is what ``fair_value_table`` always meant
    by this term and is now computed the same way there.
    """
    if not math.isfinite(row.roll) or row.vega is None or row.vega <= 0:
        return None, f"{row.tenor} has no vega to value its roll against"
    t2 = row.t_rolled
    if not math.isfinite(t2) or t2 * 365.2425 < MIN_ROLLED_DAYS:
        return None, f"{row.tenor} rolls inside the window this model cannot quote"
    vega_then = float(black.vega(row.forward_rolled, row.strike, row.level_rolled, t2))
    if not math.isfinite(vega_then) or vega_then <= 0:
        return None, f"{row.tenor} has no vega after the roll"
    multiplier = (t / h) * (vega_then / row.vega)
    roll_value = row.roll * multiplier
    carry_value = 0.0
    note = ""
    if row.carry_hedged is None:
        note = ("without a forward feed only the term structure rolls, so this is the "
                "roll alone and the forward curve's own carry is missing from it")
    else:
        carry_value = row.carry_hedged * (t / h) / row.vega
    if multiplier > 20.0:
        note = (note + "; " if note else "") + (
            f"the roll is multiplied by {multiplier:.0f} to reach this tenor's break-even, "
            f"which multiplies any interpolation error in it by the same factor")
    return -(roll_value + carry_value), note


def summarise(rows) -> dict:
    """What the grid comes to: the extremes, and how much of it was scored.

    In volatility points, like the cells it reads.  A mean over a grid that
    scored half its cells is a different statistic from one that scored all
    of them, so the count travels with it.
    """
    scored = [(r, c) for r in rows for c in r.cells if c.score is not None]
    total = sum(len(r.cells) for r in rows)
    out: dict = {
        "cells": total,
        "scored": len(scored),
        "unscored": total - len(scored),
        "mean_score": None,
        "richest": None,
        "cheapest": None,
        "extreme": 0,
        "mean_confidence": None,
        "headline": "",
    }
    if not scored:
        out["headline"] = ("nothing could be scored: not one cell of this grid has a signal "
                           "with a value in it")
        return out
    out["mean_score"] = float(np.mean([c.score for _, c in scored]))
    out["mean_confidence"] = float(np.mean([c.confidence for _, c in scored]))
    out["extreme"] = int(sum(1 for _, c in scored if abs(c.score) >= EXTREME_SCORE))
    rich = max(scored, key=lambda rc: rc[1].score)
    cheap = min(scored, key=lambda rc: rc[1].score)
    out["richest"] = _point(*rich)
    out["cheapest"] = _point(*cheap)
    out["headline"] = (
        f"richest {rich[0].tenor} {rich[1].label} at {rich[1].score * 100:+.3f} vol points, "
        f"cheapest {cheap[0].tenor} {cheap[1].label} at {cheap[1].score * 100:+.3f} vol "
        f"points, across {len(scored)} of {total} cells")
    return out


def _point(row: TenorRow, cell: Cell) -> dict:
    return {"tenor": row.tenor, "column": cell.column, "label": cell.label,
            "score": cell.score, "richness": cell.richness, "implied": cell.implied,
            "confidence": cell.confidence, "used": list(cell.used)}


@dataclass(frozen=True)
class Panel:
    """One relative-value grid, as the screen asked for it.

    The browser owns this panel and posts it whole, like the listed, market
    maker, comparison and monitor panels, so ``volkit analysis
    --relative-value`` reproduces the screen exactly and the server keeps none
    of it.
    """

    pair: str
    cut: str = "NY"
    method: str | None = None
    horizon_days: float = 30.0
    lookback_days: float | None = None
    history_days: float = HISTORY_DAYS
    annualisation: str = "weighted"
    realized_basis: str = "auto"
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    with_triangle: bool = True

    def run(self, book, history=None) -> RelativeValue:
        hist = None
        if history is not None and self.pair in history:
            hist = history[self.pair]
        return relative_value(
            book, self.pair, hist, horizon_days=self.horizon_days,
            lookback_days=self.lookback_days, history_days=self.history_days,
            method=self.method, cut=self.cut, annualisation=self.annualisation,
            realized_basis=self.realized_basis, weights=self.weights,
            with_triangle=self.with_triangle)


def _number(payload: dict, key: str, default: float) -> float:
    raw = payload.get(key)
    if raw in (None, ""):
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise RelativeValueError(f"{key} must be a number, got {raw!r}") from None


def _flag(payload: dict, key: str, default: bool = True) -> bool:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def panel_from_request(payload: dict | None) -> Panel:
    """One panel out of a browser request, or the reason it is not one.

    Every field the screen sends is read here and nowhere else.  A field the
    page posts and the server quietly ignores is a setting that appears to do
    something and does not, which is the failure this project exists to
    remove; a test walks the panel's own field list against this function.
    """
    p = dict(payload or {})
    pair = str(p.get("pair") or "").strip()
    if not pair:
        raise RelativeValueError("no pair was given to score")
    lookback_raw = p.get("lookback_days")
    lookback = (None if lookback_raw in (None, "", "match")
                else _number(p, "lookback_days", 0.0))
    if lookback is not None and lookback <= 0:
        raise RelativeValueError(
            f"the realized lookback must be positive, or 'match' to use each tenor's "
            f"own length; got {lookback_raw!r}")
    weights = p.get("weights") or {}
    if not isinstance(weights, dict):
        raise RelativeValueError(
            f"the signal weights must be an object of name to number, got {weights!r}")
    return Panel(
        pair=pair,
        cut=str(p.get("cut") or "NY"),
        method=(str(p["method"]) if p.get("method") else None),
        horizon_days=_number(p, "horizon_days", 30.0),
        lookback_days=lookback,
        history_days=_number(p, "history_days", HISTORY_DAYS),
        annualisation=str(p.get("annualisation") or "weighted"),
        realized_basis=str(p.get("realized_basis") or "auto"),
        weights=resolve_weights(weights),
        with_triangle=_flag(p, "triangle", True),
    )
