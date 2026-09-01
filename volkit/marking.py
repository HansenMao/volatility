"""The marking agent: how to run the fit, and where this desk lands after it.

The market-maker screen's two fitters are good and stay exactly as they are.
``fit_atm_curve`` is a cold fit against a target term structure; the wings move
by a curve-wide additive shift under a hinge that wants our mid *inside* the
market rather than on top of somebody's.  Those were decided for reasons (§11)
and none of them is the thing a marker actually agonises over.

What a marker agonises over is the judgement *around* the fit: which knobs to
let move this morning and which to leave alone, whether four targets are
enough to free four parameters, whether to touch the wings at all when the
only thing quoted was the at-the-money -- and then, after the fit has run,
whether to take what it produced or nudge it.  That is what this module does,
and the nudging is the part it learns.

**It learns tendencies, not a policy.**  A desk re-marks a curve a few times a
day.  Over a month that is a few dozen instances, which is enough to learn a
handful of scalar tendencies with error bars on them and nowhere near enough
to learn a function.  So what comes out of ``learn`` is a small set of
statements a person can read and argue with -- *this desk has not moved
``short_decay`` in eleven instances*, *the last six times we proposed a long
end this desk landed 0.12 below it, give or take 0.03* -- each carrying the
number of instances behind it, and each refusing to say anything at all below
a floor.  Anything shaped like a model over forty examples is overfitting with
better manners.

**A correction has to be a tendency and not a scatter.**  The median of six
corrections is a number whatever those six were; it is only *evidence* when
they agree with each other.  So a correction is applied only when its spread
is small beside its size, and when it is not, the rows say "they land on both
sides of it" and nothing moves.  That single test is what stops the agent
learning the desk's noise and quoting it back with confidence.

**The proposal is a proposal.**  Nothing here writes to the book beyond the
moment it takes to measure what it would look like, and the restore is
verified rather than assumed.  A person accepts, edits or rejects, and *that
answer is the next training instance* -- which is the whole reason this is
worth building rather than a fit with better defaults.  An edited proposal is
the most valuable row in the journal: it is the only place the tool's opinion
and the desk's sit side by side on the same morning.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import remarks as rem
from . import rules as rot
from . import session
from .numerics import ConvergenceError
from .marketmaker import (BACKBONE_KNOBS, CROSS_KNOBS, DEFAULT_BACKBONE_FREE,
                          DEFAULT_CROSS_FREE, CurveTarget, TuneResult, _Knobs,
                          fit_atm_curve, informative_params, tune_smile_shifts)
from .surface import PARAM_NAMES

DAYS_IN_YEAR = 365.2425

#: Instances of a knob being *available* to move before any tendency about it
#: is stated.  Five: below that "they have never moved it" and "they have not
#: happened to move it yet" are the same sentence.
MIN_INSTANCES = 5

#: Corrections behind a systematic bias.  Fewer, because an answered proposal
#: is a much stronger observation than a diff -- it is the desk's number and
#: the tool's on the same morning -- but not so few that three coincidences
#: become a rule.
MIN_CORRECTIONS = 4

#: A correction is a tendency rather than a scatter when its size beats its
#: spread by this much.  At 1.0: land 0.12 either side of the proposal and
#: nothing is applied; land 0.12 below it every time and it is.
BIAS_SIGNAL = 1.0

#: A learned correction may never move a knob further than this share of what
#: the fit itself moved it.  A tendency is a nudge on a fitted number, and a
#: nudge that can exceed the fit is not a nudge -- it is a second, unexamined
#: fit with a smaller sample behind it.
CORRECTION_CAP = 0.5

#: Real corrections -- the desk's own, not a rule of thumb's pseudo-instances
#: -- that must lie on the median's side of zero before a correction is
#: applied.  A prior shapes the size of a nudge and never authorises one: with
#: no real correction behind it a rule is printed and nothing moves, however
#: confident the rule.
MIN_REAL_CORRECTIONS = 3


class MarkingError(Exception):
    """A proposal that cannot be made."""


# ==========================================================================
# What has been learned
# ==========================================================================
def _median(values: list[float]) -> float | None:
    if not values:
        return None
    rows = sorted(values)
    n = len(rows)
    return rows[n // 2] if n % 2 else 0.5 * (rows[n // 2 - 1] + rows[n // 2])


def _spread(values: list[float]) -> float | None:
    """Half the interquartile range: a dispersion that one outlier cannot set.

    A standard deviation over six observations is mostly a statement about
    whichever of them was furthest out, and the whole point of this number is
    to decide whether six observations agree.
    """
    if len(values) < 2:
        return None
    rows = sorted(values)
    n = len(rows)
    lo = rows[int(0.25 * (n - 1))]
    hi = rows[int(math.ceil(0.75 * (n - 1)))]
    return 0.5 * (hi - lo)


@dataclass(frozen=True)
class Tendency:
    """What this desk does with one knob, with the count behind it."""

    key: str
    section: str
    knob: str
    seen: int                       # instances where this knob was on the book
    moved: int
    median_move: float | None = None
    move_spread: float | None = None
    answered: int = 0               # instances where something was proposed for it
    accepted: int = 0
    correction_n: int = 0
    correction: float | None = None
    correction_spread: float | None = None
    enough: bool = False
    why_not: str = ""
    # the desk's own corrections, apart from a rule of thumb's pseudo-instances
    real_corrections: tuple = ()
    prior: rot.NudgeRule | None = None

    @property
    def correction_real_n(self) -> int:
        return len(self.real_corrections)

    @property
    def correction_real(self) -> float | None:
        return _median(list(self.real_corrections))

    def decompose(self) -> str:
        """``+0.15 = +0.10 rule of thumb, +0.05 desk (n=7)``, or nothing.

        Two medians and a subtraction: the seeded list's and the real-only
        list's.  No new arithmetic, so nothing here can drift from ``bias``.
        """
        if self.prior is None or self.correction is None:
            return ""
        desk = self.correction_real
        if desk is None:
            return (f"{self.correction:+.3f} = {self.correction:+.3f} {rot.LABEL}, "
                    f"no desk correction yet")
        return (f"{self.correction:+.3f} = {self.correction - desk:+.3f} {rot.LABEL}, "
                f"{desk:+.3f} desk (n={self.correction_real_n})")

    @property
    def moved_fraction(self) -> float:
        return 0.0 if not self.seen else self.moved / self.seen

    @property
    def reluctant(self) -> bool:
        """Has this desk been given the chance to move it, and declined every time?"""
        return self.enough and self.moved == 0

    def bias(self) -> tuple[float | None, str]:
        """The correction worth applying, and why it is or is not one."""
        if self.correction_n < MIN_CORRECTIONS:
            pseudo = self.correction_n - self.correction_real_n
            return None, (f"only {self.correction_n} correction(s) here"
                          + (f" ({pseudo} of them {rot.LABEL})" if pseudo else "")
                          + f"; {MIN_CORRECTIONS} is the floor before a correction is a tendency")
        if self.correction is None:
            return None, "no correction could be measured"
        spread = self.correction_spread
        if spread is None:
            return None, "a correction needs more than one observation to have a spread"
        if abs(self.correction) < max(1e-9, BIAS_SIGNAL * spread):
            return None, (f"the corrections are {self.correction:+.3f} on average but spread "
                          f"±{spread:.3f}; this desk lands on both sides of the fit here, "
                          f"which is not a bias")
        # The third test, and the one a prior cannot pass on its own: real
        # corrections on the median's side of zero.  A rule of thumb with
        # weight 4 clears MIN_CORRECTIONS by itself; it must not clear this.
        sign = 1.0 if self.correction > 0 else -1.0
        agreeing = sum(1 for c in self.real_corrections if c * sign > 0)
        if agreeing < MIN_REAL_CORRECTIONS:
            what = (f"a {rot.LABEL} says {self.correction:+.3f} here" if self.prior is not None
                    else f"the corrections say {self.correction:+.3f} here")
            return None, (f"{what}, but only {agreeing} real correction(s) lie on that "
                          f"side of the fit; {MIN_REAL_CORRECTIONS} is the floor before a "
                          f"correction is applied" + ("" if self.prior is None else
                          ", and a rule of thumb may shape a nudge but never authorise one"))
        n = self.correction_real_n
        why = (f"over {n} answered proposal(s) this desk landed "
               f"{self.correction:+.3f} from the fit, spread ±{spread:.3f}")
        if self.prior is not None:
            why = (f"over {n} answered proposal(s) plus a {rot.LABEL} of weight "
                   f"{self.prior.weight}, {self.decompose()}, spread ±{spread:.3f}")
        return self.correction, why

    def describe(self) -> str:
        if not self.enough:
            body = f"{self.key}: {self.why_not}"
            if self.prior is not None:
                value, why = self.bias()
                body += (f"; {rot.LABEL} {self.prior.value:+.3f} ±{self.prior.spread:.3f} "
                         f"(weight {self.prior.weight}): "
                         + (why if value is None else f"correction {value:+.3f} ({why})"))
            return body
        if self.reluctant:
            return (f"{self.key}: not moved once in {self.seen} instance(s) -- this desk "
                    f"leaves it alone")
        body = (f"{self.key}: moved in {self.moved} of {self.seen} instance(s)"
                + (f", typically {self.median_move:+.3f}" if self.median_move is not None else ""))
        if self.answered:
            body += f"; {self.accepted} of {self.answered} proposal(s) taken as they stood"
        value, why = self.bias()
        body += f"; {why}" if value is None else f"; correction {value:+.3f} ({why})"
        return body


@dataclass
class Tendencies:
    """Everything learned about one pair, at one moment."""

    pair: str
    asof: datetime
    instances: int = 0
    answered: int = 0
    by_key: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # the rules of thumb seeded in, and what the real corrections make of them
    rules: rot.RuleBook | None = None
    prior_n: int = 0                # pseudo-instances seeded, across every rule
    rule_reports: list = field(default_factory=list)

    def get(self, section: str, knob: str) -> Tendency | None:
        return self.by_key.get(f"{section}.{knob}")

    @property
    def contested(self) -> list[rot.RuleReport]:
        return [r for r in self.rule_reports if r.contested]

    def learned_from(self) -> str:
        """One sentence, never one blended count."""
        body = (f"{self.instances} re-marking instance(s), {self.answered} of them "
                f"answering a proposal")
        if self.prior_n:
            body += (f", plus {self.prior_n} {rot.LABEL} pseudo-instance(s) from "
                     f"{len(self.rule_reports)} rule(s)")
        return body

    def reluctant_knobs(self) -> list[str]:
        return sorted(t.knob for t in self.by_key.values()
                      if t.section == "curve" and t.reluctant)

    def lines(self) -> list[str]:
        out = [f"{self.pair}: {self.learned_from()}"]
        out += ["  " + t.describe() for t in
                sorted(self.by_key.values(), key=lambda t: (t.section, t.knob))]
        out += [f"  {rot.LABEL}: " + r.line() for r in self.rule_reports]
        out += ["  note: " + n for n in self.notes]
        return out


def learn(journal: rem.Journal, pair: str, *, asof: datetime | None = None,
          lookback_days: float = 365.0, min_instances: int = MIN_INSTANCES,
          rules: rot.RuleBook | None = None) -> Tendencies:
    """Work the journal into tendencies for one pair.

    Age is *not* a weight here, unlike the quote archive.  A width is a fact
    about a market that moves; how a desk marks is a fact about the desk, and
    a habit from three months ago is still that desk's habit.  What ages out
    is the window, and it is a year by default.

    ``rules`` seeds each nudge rule's pseudo-corrections into the sample
    before the tendencies are built (:mod:`rules` says why the sample and
    not the statistic).  Everything after that point is untouched, and the
    real corrections are kept apart on the tendency so that ``bias`` can
    refuse a nudge no real correction supports.
    """
    now = asof or datetime.now(timezone.utc)
    rows = journal.query(pair=pair, since=now - timedelta(days=lookback_days), until=now)
    out = Tendencies(pair=pair.upper(), asof=now, instances=len(rows),
                     answered=sum(1 for e in rows if e.answered), rules=rules)
    if not rows:
        out.notes.append(f"the journal holds no re-marking instance for {pair.upper()}; "
                         f"the agent will run the fit the way the screen's defaults would")

    seen: dict[str, int] = {}
    moves: dict[str, list[float]] = {}
    answered: dict[str, int] = {}
    accepted: dict[str, int] = {}
    corrections: dict[str, list[float]] = {}
    where: dict[str, tuple[str, str]] = {}

    for entry in rows:
        # Every knob the *snapshot* holds was available to move, whether or not
        # it did.  Counting only the ones that moved would make every knob look
        # like one this desk always moves.
        for section in ("curve", "param_shifts"):
            for knob in (entry.before.get(section) or {}):
                key = f"{section}.{knob}"
                seen[key] = seen.get(key, 0) + 1
                where[key] = (section, knob)
        for change in entry.changes():
            key = change.key
            where.setdefault(key, (change.section, change.knob))
            seen.setdefault(key, seen.get(key, 0))
            if change.move is not None and abs(change.move) > 1e-12:
                moves.setdefault(key, []).append(change.move)
            if change.proposed is not None:
                answered[key] = answered.get(key, 0) + 1
                if entry.verdict == "accepted":
                    accepted[key] = accepted.get(key, 0) + 1
                if change.correction is not None:
                    corrections.setdefault(key, []).append(change.correction)

    # -- the rules of thumb, seeded into the sample --------------------------
    # Kept apart first, because the real corrections are what the third test
    # in ``bias`` and the contradiction register both read.
    real = {k: list(v) for k, v in corrections.items()}
    priors: dict[str, rot.NudgeRule] = {}
    for rule in (rules.nudges_for(pair) if rules is not None else []):
        key = rule.key
        if key in priors:
            out.notes.append(f"a second {rot.LABEL} for {key} was passed over; one rule "
                             f"per knob per pair")
            continue
        priors[key] = rule
        where.setdefault(key, (rule.section, rule.knob))
        seen.setdefault(key, 0)
        corrections.setdefault(key, []).extend(rule.pseudo())
        out.prior_n += rule.weight
        out.rule_reports.append(rot.report(rule, real.get(key, []), _median))
    for r in out.contested:
        out.notes.append(f"{rot.LABEL} on {r.rule.key} is contested: {r.real_n} real "
                         f"correction(s) with median {r.real_median:+.3f} against a rule of "
                         f"{r.rule.value:+.3f}; nothing is retired here, edit the file")

    for key in sorted(set(seen) | set(moves) | set(answered)):
        section, knob = where.get(key, ("curve", key))
        n = max(seen.get(key, 0), len(moves.get(key, [])))
        got = moves.get(key, [])
        corr = corrections.get(key, [])
        enough = n >= min_instances
        out.by_key[key] = Tendency(
            key=key, section=section, knob=knob, seen=n, moved=len(got),
            median_move=_median(got), move_spread=_spread([abs(x) for x in got]),
            answered=answered.get(key, 0), accepted=accepted.get(key, 0),
            correction_n=len(corr), correction=_median(corr),
            correction_spread=_spread(corr), enough=enough,
            why_not="" if enough else (
                f"{n} instance(s) is under the floor of {min_instances}; nothing is claimed"),
            real_corrections=tuple(real.get(key, [])), prior=priors.get(key))
    return out


# ==========================================================================
# How to run the fit
# ==========================================================================
@dataclass(frozen=True)
class Choice:
    """One decision about how to run the fit, and what made it."""

    what: str
    value: str
    source: str             # "rule" | "learned" | "caller" | rules.LABEL
    why: str = ""

    def line(self) -> str:
        return f"{self.what}: {self.value}  [{self.source}] {self.why}".rstrip()


@dataclass
class Plan:
    """The setup a marker would otherwise choose by hand."""

    free: tuple[str, ...] = ()
    pinned: tuple[str, ...] = ()
    smile_free: tuple[str, ...] = PARAM_NAMES
    tune_wings: bool = True
    mid_pull: float = 0.05
    choices: list[Choice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [c.line() for c in self.choices]


def plan_fit(book, pair: str, *, tendencies: Tendencies | None = None,
             targets: list[CurveTarget] | None = None, wing_quotes=None,
             free: tuple[str, ...] | None = None,
             smile_free: tuple[str, ...] | None = None,
             method: str | None = None) -> Plan:
    """Decide how to run the fit, saying what decided each thing.

    Two kinds of reason appear in the trace and they are labelled apart.  A
    **rule** is something true of the model -- four targets cannot determine
    five parameters, a run with no wing in it does not constrain the smile.  A
    **learned** reason is something true of this desk, and it always carries
    the number of instances behind it.  A person disagreeing with the second
    should be able to see immediately that it is the second.
    """
    surface = book[pair]
    knobs = _Knobs(surface.atm)
    plan = Plan()
    method = method or surface.method
    available = tuple(knobs.available)
    is_cross = knobs.is_cross
    default = DEFAULT_CROSS_FREE if is_cross else DEFAULT_BACKBONE_FREE
    plan.choices.append(Choice(
        "curve kind", "cross correlation" if is_cross else "backbone", "rule",
        "for a cross the level belongs to the legs, so the correlation is what is fitted"))

    if free is not None:
        wanted = tuple(k for k in free if k in available)
        plan.choices.append(Choice("free knobs", ", ".join(wanted) or "none", "caller",
                                   "the caller named them"))
        plan.free = wanted
        plan.pinned = tuple(k for k in available if k not in wanted)
        return _plan_wings(plan, wing_quotes, tendencies, smile_free, method)

    default = _free_order(default, available, tendencies, plan)
    wanted = [k for k in default if k in available]
    pinned_why: dict[str, str] = {}
    for knob in available:
        if knob not in wanted:
            pinned_why[knob] = ("pinned by default: nine tenors barely see it"
                                if knob == "short_decay" else "not in the default free set")

    # -- what the desk has told us, by never doing it ----------------------
    if tendencies is not None:
        for knob in list(wanted):
            t = tendencies.get("curve", knob)
            if t is not None and t.reluctant:
                wanted.remove(knob)
                pinned_why[knob] = (f"learned: not moved once in {t.seen} instance(s)")
                plan.choices.append(Choice(
                    "pin " + knob, "pinned", "learned",
                    f"this desk has not moved it in {t.seen} re-marking instance(s)"))

    # -- what the targets can actually determine ---------------------------
    n_targets = len(targets or [])
    if n_targets and len(wanted) > n_targets:
        # Not a preference.  Freeing more parameters than there are targets
        # leaves a family of fits that all hit them, and the one that comes
        # back is whichever the optimiser wandered into.
        keep = [k for k in default if k in wanted][:max(1, n_targets)]
        dropped = [k for k in wanted if k not in keep]
        for knob in dropped:
            pinned_why[knob] = f"rule: {n_targets} target(s) cannot determine {len(wanted)} knobs"
        wanted = keep
        plan.choices.append(Choice(
            "free knobs", ", ".join(wanted), "rule",
            f"{n_targets} target(s), so at most {n_targets} parameter(s) are freed; "
            f"{', '.join(dropped)} pinned"))
    else:
        plan.choices.append(Choice("free knobs", ", ".join(wanted) or "none", "rule",
                                   "the screen's default set for this curve"))
    plan.free = tuple(wanted)
    plan.pinned = tuple(k for k in available if k not in wanted)
    for knob in plan.pinned:
        plan.notes.append(f"{knob} pinned -- {pinned_why.get(knob, 'not freed')}")
    return _plan_wings(plan, wing_quotes, tendencies, smile_free, method)


def _free_order(default, available, tendencies: Tendencies | None, plan: Plan):
    """The order knobs are freed in, and so which are kept when targets are few.

    A plan rule of thumb may replace the screen's default order; the journal
    then reorders whichever order is in force by how often this desk has
    actually moved each knob -- a discrete choice reordered by observed
    frequency, not a number blended with a prior.  What neither may do is
    free more knobs than the targets determine; that rule is applied after
    this and is not expressible in a rules file.
    """
    order = list(default)
    rules = tendencies.rules if tendencies is not None else None
    if rules is not None:
        rule, named = rules.free_order(tendencies.pair)
        if rule is not None:
            known = [k for k in named if k in available]
            unknown = [k for k in named if k not in available]
            if unknown:
                plan.notes.append(f"{rot.LABEL} names {', '.join(unknown)}, which this "
                                  f"curve does not have; passed over")
            if known:
                order = known + [k for k in order if k not in known]
                plan.choices.append(Choice(
                    "free order", ", ".join(known), rot.LABEL,
                    rule.why or "the order the rules file frees curve knobs in"))
    if tendencies is not None:
        moved = {}
        for knob in order:
            t = tendencies.get("curve", knob)
            if t is not None and t.enough and t.moved:
                moved[knob] = t.moved
        if moved:
            ranked = sorted(order, key=lambda k: -moved.get(k, 0))
            if ranked != order:
                plan.choices.append(Choice(
                    "free order", ", ".join(ranked), "learned",
                    "reordered by how often this desk has moved each knob: "
                    + ", ".join(f"{k} {moved[k]}x" for k in ranked if k in moved)))
                order = ranked
    return tuple(order)


def _plan_wings(plan: Plan, wing_quotes, tendencies: Tendencies | None,
                smile_free: tuple[str, ...] | None = None, method: str | None = None) -> Plan:
    """Whether to touch the smile, and which of its parameters."""
    n = len(wing_quotes or [])
    if not n:
        plan.tune_wings = False
        plan.choices.append(Choice(
            "wings", "left alone", "rule",
            "nothing quoted constrains the smile; the at-the-money is the curve's job"))
        return plan
    if smile_free is not None:
        # The caller's, like a caller's free set above: named on the panel,
        # so neither the default nor anything learned is consulted.
        wanted = [p for p in smile_free if p in PARAM_NAMES]
        plan.smile_free = tuple(wanted)
        plan.tune_wings = bool(wanted)
        plan.choices.append(Choice("wings", ", ".join(wanted) or "left alone", "caller",
                                   "the caller named the smile parameters"))
        return plan
    wanted = list(PARAM_NAMES)
    # Only what the quotes reach.  A 25-delta quote reads off the 25-delta
    # anchor and says nothing about the ten-delta parameters; freeing those
    # anyway is a flat objective, and the tune refuses it.  Same rule the
    # curve applies to its targets, and the same function the fit panel uses
    # to say so.
    informed, _ = informative_params(list(wing_quotes), method or "SVI")
    uninformed = [p for p in wanted if p not in informed]
    if uninformed and informed:
        wanted = [p for p in wanted if p in informed]
        plan.choices.append(Choice(
            "pin " + ", ".join(uninformed), "pinned", "rule",
            "no quote in the paste reads off these; a parameter nothing informs makes the "
            "objective flat in that direction"))
    if tendencies is not None:
        for name in list(wanted):
            t = tendencies.get("param_shifts", name)
            if t is not None and t.reluctant:
                wanted.remove(name)
                plan.choices.append(Choice(
                    "pin " + name, "pinned", "learned",
                    f"this desk has not shifted it in {t.seen} instance(s)"))
    if n < len(wanted):
        # One quote cannot determine two parameters any more than three
        # targets can determine four knobs; the slope of the wing (slog) is
        # what a single quote on it moves, so that is what is kept.
        keep = sorted(wanted, key=lambda p: (not p.startswith("slog"), p))[:max(1, n)]
        plan.notes.append(f"{n} wing quote(s) cannot determine {len(wanted)} smile "
                          f"parameter(s); {', '.join(p for p in wanted if p not in keep)} pinned")
        wanted = [p for p in wanted if p in keep]
    plan.smile_free = tuple(wanted)
    plan.tune_wings = bool(wanted)
    plan.choices.append(Choice(
        "wings", ", ".join(wanted) or "left alone", "rule",
        f"{n} quote(s) constrain the smile"))
    return plan


# ==========================================================================
# Putting a proposal on the book for as long as it takes to look at it
# ==========================================================================
@contextmanager
def marked(book, pair: str, snapshot: dict):
    """Put a snapshot on the book, yield, and put back exactly what was there.

    The restore is *verified* rather than assumed, because §11's rule for the
    market-maker screen -- report and then restore the book exactly -- is the
    kind of thing that stays true right up until it does not, and a surface
    left half-marked by a proposal nobody accepted is the worst possible
    outcome of a tool whose whole job is marking.
    """
    surface = book[pair]
    before = session.capture_pair(book, pair)
    try:
        problems = session.apply_block(surface, snapshot)
        surface.invalidate()
        yield problems
    finally:
        session.apply_block(surface, before)
        surface.invalidate()
        back = session.capture_pair(book, pair)
        if back != before:
            raise MarkingError(
                f"the book was not restored after a {pair} proposal; this is a bug and the "
                f"surface should be reloaded before anything is priced off it")


@dataclass
class Correction:
    """A learned nudge applied to what the fit produced."""

    knob: str
    fitted: float
    applied: float
    wanted: float
    capped: bool
    why: str
    prior: str = ""            # the decomposition, when a rule of thumb is in it

    @property
    def source(self) -> str:
        return f"learned + {rot.LABEL}" if self.prior else "learned"

    def line(self) -> str:
        body = (f"{self.knob}: fit said {self.fitted:.4g}, this desk lands "
                f"{self.wanted:+.4g} from it -> {self.applied:.4g}")
        return body + ("  (capped)" if self.capped else "") + f"  [{self.source}] {self.why}"


@dataclass
class Proposal:
    """A re-mark the agent would make, and everything behind it."""

    pair: str
    plan: Plan
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    fit: object = None
    tune: TuneResult | None = None
    corrections: list[Correction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    critique: object = None

    @property
    def changes(self) -> list[rem.Change]:
        return rem.diff_snapshots(self.before, self.after)

    @property
    def moved(self) -> bool:
        return bool(self.changes)

    def lines(self) -> list[str]:
        out = [f"{self.pair}: {'no change proposed' if not self.moved else str(len(self.changes)) + ' knob(s)'}"]
        out += ["  plan   " + c.line() for c in self.plan.choices]
        if self.fit is not None:
            out.append(f"  fit    {len(self.fit.targets)} target(s), rmse "
                       f"{self.fit.rmse * 100:.4f} vol points, worst "
                       f"{self.fit.max_error * 100:+.4f} at {self.fit.max_error_tenor}"
                       + ("" if self.fit.converged else "  (did not converge)"))
        if self.tune is not None:
            out.append(f"  wings  {self.tune.inside_before} -> {self.tune.inside_after} "
                       f"quote(s) with our mid inside, worst "
                       f"{self.tune.worst_after * 100:+.4f}")
        out += ["  learn  " + c.line() for c in self.corrections]
        out += ["  move   " + c.describe() for c in self.changes]
        out += ["  note   " + n for n in self.plan.notes + self.notes]
        out += ["  !      " + w for w in self.warnings]
        return out

    def to_json(self) -> dict:
        return {
            "pair": self.pair, "moved": self.moved,
            "plan": [{"what": c.what, "value": c.value, "source": c.source, "why": c.why}
                     for c in self.plan.choices],
            "free": list(self.plan.free), "pinned": list(self.plan.pinned),
            "smile_free": list(self.plan.smile_free), "tune_wings": self.plan.tune_wings,
            "before": self.before, "after": self.after,
            "changes": [{"key": c.key, "before": c.before, "after": c.after,
                         "move": c.move, "describe": c.describe()} for c in self.changes],
            "corrections": [{"knob": c.knob, "fitted": c.fitted, "applied": c.applied,
                             "wanted": c.wanted, "capped": c.capped, "why": c.why,
                             "prior": c.prior, "source": c.source,
                             "line": c.line()} for c in self.corrections],
            "fit": None if self.fit is None else {
                "rmse": self.fit.rmse * 100.0, "max_error": self.fit.max_error * 100.0,
                "max_error_tenor": self.fit.max_error_tenor,
                "converged": self.fit.converged, "message": self.fit.message,
                "rows": [{"tenor": t.tenor, "target": t.vol * 100.0, "source": t.source,
                          "before": b * 100.0, "after": a * 100.0}
                         for t, b, a in zip(self.fit.targets, self.fit.achieved_before,
                                            self.fit.achieved_after)]},
            "wings": None if self.tune is None else {
                "inside_before": self.tune.inside_before,
                "inside_after": self.tune.inside_after,
                "worst_after": self.tune.worst_after * 100.0,
                "converged": self.tune.converged},
            "notes": list(self.plan.notes) + list(self.notes),
            "warnings": list(self.warnings),
        }


def propose(book, pair: str, *, targets: list[CurveTarget] | None = None,
            wing_quotes=None, expiries=None, forwards=None,
            tendencies: Tendencies | None = None, method: str | None = None,
            cut: str = "NY", free: tuple[str, ...] | None = None,
            smile_free: tuple[str, ...] | None = None,
            mid_pull: float = 0.05, max_nfev: int = 300) -> Proposal:
    """Run the fit the plan says to run, nudge it, and hand back a proposal.

    Nothing stays on the book.  The fit itself runs on a deepcopy inside
    ``fit_atm_curve``; the wing tune does not, so it is done inside
    :func:`marked` and undone before this returns.
    """
    if book is None or pair not in book:
        raise MarkingError(f"{pair} is not built in this book")
    surface = book[pair]
    plan = plan_fit(book, pair, tendencies=tendencies, targets=targets,
                    wing_quotes=wing_quotes, free=free, smile_free=smile_free,
                    method=method)
    plan.mid_pull = mid_pull
    out = Proposal(pair=pair.upper(), plan=plan,
                   before=session.capture_pair(book, pair))
    # Said on every proposal, including when the answer is none.  A proposal
    # that quietly had nothing to learn from and one built on a year of
    # instances must not read the same.
    if tendencies is None:
        out.notes.append("no journal was given, so nothing here is learned; this is the fit "
                         "the screen's defaults would run")
    else:
        out.notes.append(
            f"learned from {tendencies.learned_from()}"
            + ("" if tendencies.instances else
               " -- which is no instance, so nothing here is learned from this desk yet"))
        for r in tendencies.contested:
            out.warnings.append(f"{rot.LABEL} on {r.rule.key} is contested by "
                                f"{r.real_n} real correction(s)")
    after = {k: (dict(v) if isinstance(v, dict) else v) for k, v in out.before.items()}

    # -- the curve ---------------------------------------------------------
    if targets:
        try:
            fit = fit_atm_curve(surface.atm, list(targets), free=plan.free or None)
            out.fit = fit
            fitted = {k: v for k, v in fit.after.items()}
            after["curve"] = dict(session.curve_params(surface.atm))
            knobs = _Knobs(surface.atm)
            for name in knobs.available:
                if name in fitted:
                    after["curve"][name] = _to_screen(name, fitted[name])
            _correct(out, after, tendencies)
            if not fit.converged:
                out.warnings.append(f"the curve fit did not converge: {fit.message}")
        except Exception as exc:                 # noqa: BLE001 - one section, one failure
            out.warnings.append(f"the curve was not fitted: {type(exc).__name__}: {exc}")
    else:
        out.notes.append("nothing was given to fit the at-the-money curve to")

    # -- the wings, measured with the fitted curve on the book -------------
    if plan.tune_wings and wing_quotes and expiries is not None:
        try:
            with marked(book, pair, after):
                tune = tune_smile_shifts(
                    surface, list(wing_quotes), expiries, forwards or {},
                    method=method or surface.method, cut=cut,
                    free=tuple(plan.smile_free), mid_pull=mid_pull, max_nfev=max_nfev)
                out.tune = tune
                shifts = {k: float(v) for k, v in tune.after.items() if abs(float(v)) > 1e-12}
            after["param_shifts"] = shifts
            if not tune.converged:
                out.warnings.append(f"the wing tune did not converge: {tune.message}")
        except MarkingError:
            raise
        except Exception as exc:                 # noqa: BLE001
            out.warnings.append(f"the wings were not tuned: {type(exc).__name__}: {exc}")

    out.after = after
    if not out.moved:
        out.notes.append("the fit landed where the surface already is; there is nothing to do")
    return out


def _to_screen(name: str, value: float) -> float:
    """A fitted knob in the units the snapshot holds it in."""
    from .marketmaker import _PERCENT_KNOBS
    return value * 100.0 if name in _PERCENT_KNOBS else value


def _correct(out: Proposal, after: dict, tendencies: Tendencies | None) -> None:
    """Apply what this desk has done to proposals like this one.

    Capped at a share of what the fit itself moved: a tendency is a nudge on a
    fitted number, and a nudge that can exceed the fit is a second fit with a
    smaller sample behind it.
    """
    if tendencies is None:
        return
    fitted_before = out.before.get("curve") or {}
    for knob, value in list((after.get("curve") or {}).items()):
        t = tendencies.get("curve", knob)
        if t is None:
            continue
        wanted, why = t.bias()
        if wanted is None:
            continue
        moved = abs(value - float(fitted_before.get(knob, value)))
        cap = CORRECTION_CAP * moved if moved > 0 else 0.0
        applied = max(-cap, min(cap, wanted)) if cap > 0 else 0.0
        capped = abs(applied - wanted) > 1e-12
        if abs(applied) <= 1e-12:
            out.notes.append(
                f"{knob}: a learned correction of {wanted:+.3f} was not applied because the "
                f"fit barely moved it, and a nudge may not exceed half of what the fit did")
            continue
        after["curve"][knob] = value + applied
        out.corrections.append(Correction(knob=knob, fitted=value, applied=value + applied,
                                          wanted=wanted, capped=capped, why=why,
                                          prior=t.decompose()))


# ==========================================================================
def record(journal: rem.Journal, proposal: Proposal, book, *, verdict: str,
           note: str = "", context: dict | None = None,
           at: datetime | None = None) -> rem.Remark:
    """Write what happened to a proposal into the journal.

    The ``after`` is the book **as it is now**, not the proposal: the point of
    an ``edited`` verdict is that the two differ, and taking the proposal as
    the outcome would record the agent agreeing with itself.
    """
    if verdict not in rem.VERDICTS:
        raise MarkingError(f"{verdict!r} is not one of {', '.join(rem.VERDICTS)}")
    entry = rem.instance(
        proposal.pair, proposal.before, session.capture_pair(book, proposal.pair),
        proposed=proposal.after if verdict != "unprompted" else None,
        verdict=verdict, note=note, source="agent", at=at,
        context={**(context or {}),
                 "free": list(proposal.plan.free),
                 "pinned": list(proposal.plan.pinned),
                 "targets": 0 if proposal.fit is None else len(proposal.fit.targets),
                 "wings_tuned": proposal.tune is not None})
    ok, why = journal.add(entry)
    if not ok:
        raise MarkingError(f"the instance was not recorded: {why}")
    return entry


# ==========================================================================
# The card on the market-maker tab: the agent run off the fit panel's inputs
# ==========================================================================
#: What may be said back about a proposal from a screen.  ``unprompted`` is a
#: diff with no proposal behind it and is the marking screen's business, not
#: this card's.
SCREEN_VERDICTS = ("accepted", "edited", "rejected")


@dataclass
class MarkPanel:
    """The marking agent, aimed at exactly what the fit panel is aimed at.

    The card sits beside the **Fit** button and reads the same boxes: the
    market paste, the target curve and its source, the butterfly and unit
    conventions.  It does not have a market of its own, because the question
    it answers is *how would you run this fit, and would you take what came
    out* -- and that is a question about the fit on the screen, not about
    some other fit.  What it adds is what a marker adds: which knobs to free
    (``choose_knobs``), what the journal says this desk does afterwards, and
    what the quote archive makes of the result (``use_archive``).

    Posted whole by the browser like every other panel here, and read by
    :func:`panel_from_request`, so the card and ``volkit mark propose --file``
    produce the same proposal.
    """

    pair: str
    cut: str = "NY"
    method: str | None = None

    # the fit panel's own inputs, unchanged
    text: str = ""
    vol_unit: str = "auto"
    fly_convention: str = "market"
    target_source: str = "overwrites"
    target_text: str = ""
    free: tuple[str, ...] | None = None
    smile_free: tuple[str, ...] = PARAM_NAMES
    mid_pull: float = 0.05
    max_nfev: int = 300

    # the agent's own
    choose_knobs: bool = True
    use_rules: bool = True
    lookback_days: float = 365.0
    min_instances: int = MIN_INSTANCES
    use_archive: bool = True
    half_life: float = 5.0
    min_effective: float = 2.0
    evidence_lookback: float = 90.0

    def fit_payload(self) -> dict:
        """The fit panel this card is aimed at, as the fit reader takes it."""
        return {
            "pair": self.pair, "cut": self.cut, "method": self.method,
            "text": self.text, "vol_unit": self.vol_unit,
            "fly_convention": self.fly_convention,
            "target_source": self.target_source, "target_text": self.target_text,
            "free": list(self.free or ()), "smile_free": list(self.smile_free),
            "mid_pull": self.mid_pull, "max_nfev": self.max_nfev, "apply": False,
        }

    def run(self, book, journal: rem.Journal, archive=None, rules=None) -> dict:
        from . import marketmaker as mm
        from .quotes import parse_quotes

        if book is None or self.pair not in book:
            raise MarkingError(f"{self.pair} is not built in this book")
        fit = mm.panel_from_request(self.fit_payload())
        surface, method, clock = mm._prepare(book, self.pair, self.method)
        out: dict = {
            "pair": self.pair, "cut": self.cut, "method": method,
            "valuation": clock.now.isoformat(),
            "choose_knobs": bool(self.choose_knobs), "use_archive": bool(self.use_archive),
            "notes": [], "warnings": [], "parse": {"notes": [], "skipped": []},
            "targets": {"n": 0, "evidence": ""}, "proposal": None, "lines": [],
            "plan": None, "marks": None, "tendencies": None, "critique": None,
            "use_rules": bool(self.use_rules), "rules": None,
        }

        # -- the market, read exactly as the fit reads it -------------------
        quotes: list = []
        if str(self.text or "").strip():
            run_ = parse_quotes(self.text, pair=self.pair, vol_unit=self.vol_unit,
                                fly_convention=self.fly_convention,
                                today=clock.now.date())
            quotes = list(run_.quotes)
            out["parse"] = {"notes": list(run_.notes),
                            "skipped": [{"line": n, "text": t, "why": w}
                                        for n, t, w in run_.skipped]}
        expiries = mm.resolve_expiries(clock, quotes)
        stale = [k for k, (_, t) in expiries.items() if t <= 0]
        if stale:
            raise MarkingError(
                f"{', '.join(stale)} is not in the future at the valuation time "
                f"{clock.now:%Y-%m-%d %H:%M}Z")
        forwards, forward_notes = mm._forwards_for(book, self.pair, expiries)
        out["notes"].extend(forward_notes)

        targets: list[CurveTarget] = []
        try:
            targets, evidence = fit._targets(surface, quotes, expiries)
        except (ValueError, ConvergenceError) as exc:
            evidence = f"{type(exc).__name__}: {exc}"
            out["warnings"].append(f"no target curve: {exc}")
        out["targets"] = {"n": len(targets), "evidence": evidence}
        wing_quotes = [q for q in quotes if q.instrument in ("rr", "fly", "outright")
                       or (q.instrument == "spread" and q.leg in ("rr", "fly"))]

        # -- the desk, then the proposal --------------------------------------
        rules = rules if self.use_rules else None
        tendencies = learn(journal, self.pair, asof=clock.now,
                           lookback_days=self.lookback_days,
                           min_instances=self.min_instances, rules=rules)
        out["rules"] = None if rules is None else {
            "path": rules.path, "problems": list(rules.problems),
            "n": len(rules), "prior_n": tendencies.prior_n,
            "rows": [{"key": r.rule.key, "value": r.rule.value, "spread": r.rule.spread,
                      "weight": r.rule.weight, "why": r.rule.why,
                      "real_n": r.real_n, "real_median": r.real_median,
                      "far_side": r.far_side, "contested": r.contested,
                      "line": r.line()} for r in tendencies.rule_reports],
            "contested": [r.rule.key for r in tendencies.contested],
        }
        out["tendencies"] = {
            "path": journal.path, "problems": list(journal.problems),
            "instances": tendencies.instances, "answered": tendencies.answered,
            "prior_n": tendencies.prior_n, "learned_from": tendencies.learned_from(),
            "lookback_days": self.lookback_days, "min_instances": self.min_instances,
            "lines": tendencies.lines(),
            "rows": [{"section": t.section, "knob": t.knob, "seen": t.seen,
                      "moved": t.moved, "reluctant": t.reluctant,
                      "describe": t.describe()}
                     for t in sorted(tendencies.by_key.values(),
                                     key=lambda t: (t.section, t.knob))],
        }
        caller_free = None if self.choose_knobs else tuple(self.free or ())
        caller_smile = None if self.choose_knobs else tuple(self.smile_free or ())
        if caller_free is not None and not caller_free:
            targets = []
            out["notes"].append("the fit panel frees no curve parameter, so the curve was "
                                "left as marked; tick 'agent chooses the knobs' to let the "
                                "agent pick")
        proposal = propose(book, self.pair, targets=targets or None,
                           wing_quotes=wing_quotes or None,
                           expiries=expiries if wing_quotes else None,
                           forwards=forwards, tendencies=tendencies, method=method,
                           cut=self.cut, free=caller_free, smile_free=caller_smile,
                           mid_pull=self.mid_pull, max_nfev=self.max_nfev)
        out["proposal"] = proposal.to_json()
        out["lines"] = proposal.lines()
        out["plan"] = {"free": list(proposal.plan.free),
                       "smile_free": list(proposal.plan.smile_free),
                       "tune_wings": bool(proposal.plan.tune_wings),
                       "fit_curve": bool(targets)}
        out["marks"] = marks_from_snapshot(surface, proposal.after, pair=self.pair,
                                           cut=self.cut, method=method, clock=clock,
                                           fitted=proposal.moved)

        # -- what the quoting agent makes of it -------------------------------
        if self.use_archive:
            out["critique"] = self._critique(book, archive, proposal, method, forwards,
                                             clock, out["notes"])
        out["warnings"].extend(surface.warnings[-6:])
        return out

    def _critique(self, book, archive, proposal, method, forwards, clock, notes):
        """The quoting agent's score of the proposal, or the reason there is none."""
        from . import consult
        from . import synthesis as syn
        if archive is None:
            notes.append("no observation archive was given, so the proposal was not scored "
                         "against what the market has shown")
            return None
        synthesis = syn.synthesize(archive, self.pair, asof=clock.now,
                                   half_life=self.half_life,
                                   min_effective=self.min_effective,
                                   lookback_days=self.evidence_lookback)
        findings, f_notes = consult.findings_from(book, self.pair, synthesis, method=method,
                                                 cut=self.cut, forwards=forwards)
        notes.extend(f_notes)
        if not findings:
            return {"available": False, "verdict": "nothing to judge", "findings": [],
                    "rows": [], "notes": list(f_notes), "inside_before": 0,
                    "inside_after": 0, "archive": archive.path}
        judged = consult.critique(book, self.pair, findings, proposal.after, method=method,
                                  cut=self.cut, forwards=forwards)
        return {
            "available": True, "archive": archive.path, "verdict": judged.verdict,
            "inside_before": judged.inside_before, "inside_after": judged.inside_after,
            "broke": [r.key for r in judged.broke], "fixed": [r.key for r in judged.fixed],
            "findings": [{"key": consult._key(f), "describe": f.describe(),
                          "inside": f.inside, "gap": f.gap,
                          "observations": f.observations, "low": f.low, "high": f.high,
                          "model_mid": f.model_mid, "typical": f.typical}
                         for f in findings],
            "rows": [{"key": r.key, "describe": r.describe, "before": r.before,
                      "after": r.after, "inside_before": r.inside_before,
                      "inside_after": r.inside_after, "gap_before": r.gap_before,
                      "gap_after": r.gap_after, "improved": r.improved,
                      "worsened": r.worsened, "line": r.line()} for r in judged.rows],
            "notes": list(judged.notes),
        }


def marks_from_snapshot(surface, snapshot: dict, *, pair: str, cut: str, method: str,
                        clock, fitted: bool) -> dict:
    """A session snapshot as the marks the quote panel stands on.

    The same object ``marketmaker.Panel.run`` hands back, built from a
    proposal instead of a fit, so the quote panel takes either without
    knowing which it was given -- and ``what`` says which, because a price
    made on the agent's proposal and one made on the morning's fit must not
    read the same.
    """
    from .marketmaker import _Knobs
    knobs = _Knobs(surface.atm)
    curve = snapshot.get("curve") or {}
    shifts = snapshot.get("param_shifts") or {}
    return {
        "knobs": {k: float(curve[k]) for k in knobs.available if k in curve},
        "shifts": {k: float(shifts.get(k, 0.0)) for k in PARAM_NAMES},
        "pair": pair, "cut": cut, "method": method,
        "fitted": bool(fitted), "stamp": clock.now.isoformat(),
        "what": "the marking agent's proposal" if fitted else "nothing",
    }


def snapshot_from_marks(before: dict, marks: dict) -> dict:
    """The browser's held marks, as the snapshot an instance records.

    The reverse of :func:`marks_from_snapshot`, on top of what the proposal
    started from: a fit moves knobs and shifts and nothing else, so the
    overwrites and events are the ones the morning began with.
    """
    after = {k: (dict(v) if isinstance(v, dict) else v) for k, v in before.items()}
    after["curve"] = dict(after.get("curve") or {})
    for k, v in (marks.get("knobs") or {}).items():
        after["curve"][k] = float(v)
    if marks.get("shifts") is not None:
        after["param_shifts"] = {k: float(v) for k, v in marks["shifts"].items()
                                 if abs(float(v)) > 1e-12}
    return after


def answer(journal: rem.Journal, pair: str, *, before: dict, proposed: dict, after: dict,
           verdict: str, note: str = "", source: str = "screen",
           at: datetime | None = None, context: dict | None = None,
           repeat_ok: bool = False) -> rem.Remark:
    """Record what became of a proposal, from the numbers alone.

    Takes dicts rather than a :class:`Proposal` because the proposal has been
    round-tripped through a browser or a file by the time it is answered; the
    journal is the same either way.  ``after`` is what the desk did, which is
    the proposal on ``accepted``, the start on ``rejected``, and the desk's
    own marks on ``edited`` -- and the caller decides that, because only the
    caller knows where the desk's marks are.
    """
    if verdict not in rem.VERDICTS:
        raise MarkingError(f"{verdict!r} is not one of {', '.join(rem.VERDICTS)}")
    entry = rem.instance(pair, before, after,
                         proposed=proposed if verdict != "unprompted" else None,
                         verdict=verdict, note=note, source=source, at=at,
                         context=context)
    ok, why = journal.add(entry)
    if not ok:
        held = next((e for e in journal.entries if e.id == entry.id), None)
        if held is not None and repeat_ok:
            # The journal is content-addressed on purpose (§18): the same
            # morning answered twice is one instance.  A screen pressing the
            # button again is told that rather than shown an error.
            return held
        raise MarkingError(f"the instance was not recorded: {why}")
    journal.flush()
    return entry


def answer_from_request(journal: rem.Journal, book, payload: dict, *, clock) -> dict:
    """The card's verdict buttons: record, and optionally keep the marks.

    ``edited`` needs the marks the desk ended on, which the browser holds
    (the fit's ``marks``); without them there is nothing to call the edit and
    it is refused rather than recorded as an acceptance in disguise.
    ``apply`` puts the recorded ``after`` on the loaded book -- the same
    decision as the fit panel's *keep the marks*, and in memory only.
    """
    saved = payload.get("proposal")
    if not isinstance(saved, dict) or "before" not in saved or "after" not in saved:
        raise MarkingError("the verdict needs the proposal the card was shown; run it first")
    pair = str(saved.get("pair") or payload.get("pair") or "").strip().upper()
    if not pair:
        raise MarkingError("the proposal names no pair")
    if book is None or pair not in book:
        raise MarkingError(f"{pair} is not built in this book")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in SCREEN_VERDICTS:
        raise MarkingError(f"{verdict!r} is not one of {', '.join(SCREEN_VERDICTS)}")
    before, proposed = dict(saved["before"]), dict(saved["after"])
    marks = payload.get("marks")
    if verdict == "accepted":
        after = proposed
    elif verdict == "rejected":
        after = before
    else:
        if not isinstance(marks, dict) or not marks.get("knobs"):
            raise MarkingError(
                "'edited' records the marks you ended on, and the card was handed none; "
                "run the fit your way first, then record the edit")
        named = str(marks.get("pair") or "").strip().upper()
        if named and named != pair:
            raise MarkingError(f"the marks held are {named}'s and the proposal is {pair}'s")
        after = snapshot_from_marks(before, marks)
    held_before = len(journal)
    entry = answer(journal, pair, before=before, proposed=proposed, after=after,
                   verdict=verdict, note=str(payload.get("note") or ""), source="screen",
                   at=clock.now, context={"free": list(saved.get("free") or []),
                                          "pinned": list(saved.get("pinned") or []),
                                          "targets": len((saved.get("fit") or {}).get("rows") or []),
                                          "wings_tuned": saved.get("wings") is not None},
                   repeat_ok=True)
    out = {"id": entry.id, "describe": entry.describe(), "verdict": verdict,
           "path": journal.path, "applied": False, "warnings": [], "notes": [],
           "repeated": len(journal) == held_before}
    if out["repeated"]:
        out["notes"].append("this answer was already in the journal and was not written twice")
    changes = entry.changes()
    if verdict == "edited" and not any(c.correction for c in changes):
        out["notes"].append("nothing differs from the proposal; 'accepted' is the verdict "
                            "for that, and this was recorded as typed")
    if verdict == "rejected" and not changes:
        out["notes"].append("recorded: the desk left the surface where it was")
    if payload.get("apply") and verdict in ("accepted", "edited"):
        surface = book[pair]
        problems = session.apply_block(surface, after)
        surface.invalidate()
        out["applied"] = True
        out["warnings"].extend(problems)
        out["notes"].append(f"the {verdict} marks were written into the loaded book for "
                            f"{pair}. They are in memory only -- the workbook on disk is "
                            f"unchanged, and a reload discards them")
    return out


def panel_from_request(payload: dict) -> MarkPanel:
    """The card as the browser posts it.

    Same rule as ``marketmaker.panel_from_request``: a field the browser
    sends that this does not read is a setting that silently does nothing,
    and a test pins the page's list against this function.
    """
    from .marketmaker import _common, _opt_bool, _opt_float, _opt_tuple, TARGET_SOURCES
    pair, cut, method, fly, vol_unit = _common(payload)
    source = str(payload.get("target_source") or "overwrites").strip().lower()
    if source not in TARGET_SOURCES:
        raise ValueError(f"unknown target source {source!r}; expected one of {TARGET_SOURCES}")
    return MarkPanel(
        pair=pair, cut=cut, method=method,
        text=str(payload.get("text") or ""), vol_unit=vol_unit, fly_convention=fly,
        target_source=source, target_text=str(payload.get("target_text") or ""),
        free=_opt_tuple(payload, "free", None),
        smile_free=_opt_tuple(payload, "smile_free", PARAM_NAMES),
        mid_pull=_opt_float(payload, "mid_pull", 0.05),
        max_nfev=int(_opt_float(payload, "max_nfev", 300)),
        choose_knobs=_opt_bool(payload, "choose_knobs", True),
        use_rules=_opt_bool(payload, "use_rules", True),
        lookback_days=_opt_float(payload, "lookback_days", 365.0),
        min_instances=int(_opt_float(payload, "min_instances", MIN_INSTANCES)),
        use_archive=_opt_bool(payload, "use_archive", True),
        half_life=_opt_float(payload, "half_life", 5.0),
        min_effective=_opt_float(payload, "min_effective", 2.0),
        evidence_lookback=_opt_float(payload, "evidence_lookback", 90.0),
    )
