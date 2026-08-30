"""Rules of thumb: what a desk believes about marking before the journal knows
anything, written down so the first month can falsify them.

Everything ``marking.learn`` knows comes out of the journal, so the agent has
nothing to say for the first month, and its floors are judgement calls no real
journal has argued with yet.  But a marker already knows things -- the back end
lags broker moves, risk reversals are moved less often than the at-the-money,
a desk is readier to raise a vol than to cut it into a bid.  A rule of thumb
is one of those beliefs with a number on it, and it is **a third kind of
reason**: not a rule (true of the model) and not a learned reason (true of
this desk, with its count), but true of markers generally and not yet shown
to be true of this desk.  Every place the first two are labelled apart, this
one is labelled apart from both.

**A nudge rule is seeded into the sample, not blended beside it.**  At
``learn`` time each rule becomes ``weight`` pseudo-corrections placed
symmetrically about its ``value`` so that ``marking._median`` returns exactly
``value`` and ``marking._spread`` returns exactly ``spread``.  Medians and
interquartile ranges do not compose analytically the way means and variances
do, so seeding the sample is the one clean way to put a prior under statistics
of that shape, and it leaves every downstream test -- ``BIAS_SIGNAL``,
``CORRECTION_CAP``, the cap on what the fit moved -- exactly as it was.

The failure this must not have is a prior that never gets falsified: the
agent reciting its author's hunch back with the desk's confidence attached,
looking like evidence.  So a weight is clamped, a prior can shape the size of
a nudge but never authorise one (``marking.MIN_REAL_CORRECTIONS``), every
rule-shaped number decomposes into the rule's share and the desk's, and a
rule the desk edits away every time is printed **contested** -- printed, and
nothing more.  Retiring it is a person's job.

**A plan rule** is the other object in the file: a default order in which the
curve's knobs are freed as the targets allow.  It is discrete, so it is not
seeded; the journal reorders it by how often each knob has actually been
moved.  The hard constraints are not expressible here: four targets still
cannot determine five parameters, and ``informative_params`` still governs
the wings.  A rules file may seed a habit and may not weaken a rule that is
true of the model.

The file is TOML, read with the standard library's ``tomllib``, so a trader
can edit it by hand and no write path is needed.  ``mm_rules.toml`` beside the
workbook, like the journal and the bank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .paths import app_dir, read_text

try:                                    # 3.11+; the desk build is 3.12
    import tomllib
except ImportError:                     # pragma: no cover - 3.10
    tomllib = None                      # type: ignore[assignment]

RULES_FILENAME = "mm_rules.toml"

#: Pseudo-corrections one rule may put into the sample.  Five at most, so that
#: a fortnight of real corrections can outvote it; two at least, because below
#: two there is no spread and ``bias()`` refuses anyway.  The arithmetic of
#: the outvote, since it is the whole point of the cap: ``_spread`` reads its
#: upper quartile at ``ceil(0.75 (n - 1))``, so with weight 5 and ``n_real``
#: real corrections the pseudo-corrections leave the interquartile range once
#: ``n_real >= 16``.  Fifteen consistent real corrections outvote a rule
#: inside the desk's own range; a rule half a point off the desk holds the
#: spread open until the sixteenth.  A test pins that boundary.
MAX_PRIOR_WEIGHT = 5
MIN_PRIOR_WEIGHT = 2

#: Real corrections on the far side of a rule before the rule is contested.
#: The highest-information row in the file is a rule the desk edits away every
#: single time, and this is where it is said.
CONTESTED_N = 8

#: The sections a nudge rule may name.  A rule is a correction to a fitted
#: number, and only the curve and the smile shifts are fitted.
NUDGE_SECTIONS = ("curve", "param_shifts")

#: What a rule of thumb is called wherever a rule or a learned reason is
#: labelled.  One spelling, so the page's tag map and the CLI agree.
LABEL = "rule of thumb"


class RulesError(Exception):
    """A rules file that must not load."""


def seed(value: float, spread: float, weight: int) -> list[float]:
    """The pseudo-corrections that make ``_median`` say ``value`` and
    ``_spread`` say ``spread`` exactly.

    ``_spread`` reads its quartiles at ``rows[int(0.25 (n-1))]`` and
    ``rows[ceil(0.75 (n-1))]``, which are different rows at each ``n``, so
    the placement is written out per weight rather than derived.  A test
    pins each one against the two functions themselves.
    """
    v, s = float(value), float(spread)
    if weight == 2:
        return [v - s, v + s]
    if weight == 3:
        return [v - s, v, v + s]
    if weight == 4:
        return [v - s, v - s / 2, v + s / 2, v + s]
    if weight == 5:
        return [v - 1.5 * s, v - s, v, v + s, v + 1.5 * s]
    raise RulesError(f"weight must be between {MIN_PRIOR_WEIGHT} and {MAX_PRIOR_WEIGHT}, "
                     f"not {weight}")


@dataclass(frozen=True)
class NudgeRule:
    """A belief about where this desk lands relative to the fit on one knob."""

    section: str
    knob: str
    value: float
    spread: float
    weight: int
    why: str = ""
    added: str = ""
    scope: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.section}.{self.knob}"

    def applies(self, pair: str) -> bool:
        want = self.scope.get("pair")
        return want is None or str(want).upper() == pair.upper()

    def pseudo(self) -> list[float]:
        return seed(self.value, self.spread, self.weight)

    def describe(self) -> str:
        where = f" on {self.scope['pair']}" if self.scope.get("pair") else ""
        return (f"{self.key}{where}: {self.value:+.3f} ±{self.spread:.3f}, weight {self.weight}"
                + (f" -- {self.why}" if self.why else ""))


@dataclass(frozen=True)
class PlanRule:
    """The order curve knobs are freed in, as the targets allow."""

    free_order: tuple[str, ...]
    why: str = ""
    scope: dict = field(default_factory=dict)

    def applies(self, pair: str) -> bool:
        want = self.scope.get("pair")
        return want is None or str(want).upper() == pair.upper()


@dataclass
class RuleBook:
    """Every rule of thumb the file holds."""

    path: str = ""
    nudges: list[NudgeRule] = field(default_factory=list)
    plans: list[PlanRule] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @classmethod
    def default_path(cls) -> Path:
        return app_dir() / RULES_FILENAME

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RuleBook":
        """Read a rules file, or an empty book when there is none.

        A file that is *there* and wrong is an error, not an empty book: a
        rule with a weight of twenty silently trimmed to five is the agent
        taking an order it was never given, and a file that half loads is
        a set of beliefs nobody can point at.
        """
        p = Path(path) if path else cls.default_path()
        out = cls(path=str(p))
        if not p.exists():
            out.problems.append(f"no rules of thumb at {p}; the agent learns from the "
                                f"journal alone")
            return out
        if tomllib is None:
            raise RulesError(f"reading {p} needs Python 3.11 or later (tomllib)")
        try:
            data = tomllib.loads(read_text(p))
        except (OSError, UnicodeDecodeError) as exc:
            raise RulesError(f"cannot read the rules at {p}: {exc}") from None
        except tomllib.TOMLDecodeError as exc:
            raise RulesError(f"{p} is not valid TOML: {exc}") from None
        return cls.from_dict(data, path=str(p))

    @classmethod
    def from_dict(cls, data: dict, *, path: str = "") -> "RuleBook":
        out = cls(path=path)
        if not isinstance(data, dict):
            raise RulesError(f"{path or 'the rules'}: expected tables, got {type(data).__name__}")
        known = {"nudge_rule", "plan_rule"}
        stray = sorted(set(data) - known)
        if stray:
            raise RulesError(f"{path or 'the rules'}: unknown table(s) {', '.join(stray)}; "
                             f"the file holds [[nudge_rule]] and [[plan_rule]]")
        for n, row in enumerate(_rows(data, "nudge_rule", path), start=1):
            out.nudges.append(_nudge(row, n, path))
        for n, row in enumerate(_rows(data, "plan_rule", path), start=1):
            out.plans.append(_plan(row, n, path))
        return out

    def __len__(self) -> int:
        return len(self.nudges) + len(self.plans)

    def nudges_for(self, pair: str) -> list[NudgeRule]:
        return [r for r in self.nudges if r.applies(pair)]

    def free_order(self, pair: str) -> tuple[PlanRule | None, tuple[str, ...]]:
        """The first plan rule that names the pair, or the first that names none."""
        named = [r for r in self.plans if r.scope.get("pair") and r.applies(pair)]
        general = [r for r in self.plans if not r.scope.get("pair")]
        rule = (named or general or [None])[0]
        return rule, (rule.free_order if rule else ())

    def lines(self) -> list[str]:
        out = [f"{self.path}: {len(self.nudges)} nudge rule(s), {len(self.plans)} plan rule(s)"]
        out += ["  " + r.describe() for r in self.nudges]
        for r in self.plans:
            where = f" on {r.scope['pair']}" if r.scope.get("pair") else ""
            out.append(f"  free in order{where}: {', '.join(r.free_order)}"
                       + (f" -- {r.why}" if r.why else ""))
        out += ["  note: " + p for p in self.problems]
        return out


def _rows(data: dict, table: str, path: str) -> list[dict]:
    rows = data.get(table, [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RulesError(f"{path or 'the rules'}: [[{table}]] must be an array of tables")
    return rows


def _number(row: dict, name: str, where: str) -> float:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesError(f"{where}: '{name}' must be a number, got {value!r}")
    return float(value)


def _scope(row: dict, where: str) -> dict:
    scope = row.get("scope", {})
    if not isinstance(scope, dict):
        raise RulesError(f"{where}: 'scope' must be a table such as {{ pair = \"EURUSD\" }}")
    stray = sorted(set(scope) - {"pair"})
    if stray:
        raise RulesError(f"{where}: scope may name a pair and nothing else, not {', '.join(stray)}")
    if "pair" in scope and not isinstance(scope["pair"], str):
        raise RulesError(f"{where}: scope.pair must be a string")
    return {k: (v.upper() if isinstance(v, str) else v) for k, v in scope.items()}


def _nudge(row: dict, n: int, path: str) -> NudgeRule:
    where = f"{path or 'the rules'} nudge_rule #{n}"
    stray = sorted(set(row) - {"section", "knob", "scope", "value", "spread", "weight",
                               "why", "added"})
    if stray:
        raise RulesError(f"{where}: unknown key(s) {', '.join(stray)}")
    section = row.get("section")
    knob = row.get("knob")
    if section not in NUDGE_SECTIONS:
        raise RulesError(f"{where}: section must be one of {', '.join(NUDGE_SECTIONS)}, "
                         f"not {section!r}")
    if not isinstance(knob, str) or not knob:
        raise RulesError(f"{where}: 'knob' must name a parameter")
    value = _number(row, "value", where)
    spread = _number(row, "spread", where)
    if spread <= 0:
        raise RulesError(f"{where}: spread must be positive; a rule with no spread is a "
                         f"certainty, and the journal could never outvote it")
    weight = row.get("weight", MIN_PRIOR_WEIGHT)
    if isinstance(weight, bool) or not isinstance(weight, int):
        raise RulesError(f"{where}: weight must be a whole number")
    if not MIN_PRIOR_WEIGHT <= weight <= MAX_PRIOR_WEIGHT:
        # A load error and not a trim.  Ten to fifteen real corrections must
        # always be able to outvote a rule, and a file asking for more than
        # that has to be told no rather than quietly given less.
        raise RulesError(f"{where}: weight {weight} is outside {MIN_PRIOR_WEIGHT}.."
                         f"{MAX_PRIOR_WEIGHT}; a rule may not outweigh a fortnight of "
                         f"real corrections")
    return NudgeRule(section=section, knob=knob, value=value, spread=spread, weight=weight,
                     why=str(row.get("why", "")), added=str(row.get("added", "")),
                     scope=_scope(row, where))


def _plan(row: dict, n: int, path: str) -> PlanRule:
    where = f"{path or 'the rules'} plan_rule #{n}"
    stray = sorted(set(row) - {"free_order", "scope", "why"})
    if stray:
        raise RulesError(f"{where}: unknown key(s) {', '.join(stray)}")
    order = row.get("free_order")
    if (not isinstance(order, list) or not order
            or not all(isinstance(k, str) and k for k in order)):
        raise RulesError(f"{where}: free_order must be a non-empty list of knob names")
    if len(set(order)) != len(order):
        raise RulesError(f"{where}: free_order names a knob twice")
    return PlanRule(free_order=tuple(order), why=str(row.get("why", "")),
                    scope=_scope(row, where))


@dataclass(frozen=True)
class RuleReport:
    """One rule against the real corrections, at `learn` time."""

    rule: NudgeRule
    real_n: int
    real_median: float | None
    far_side: int                  # real corrections on the far side of zero from value
    contested: bool

    def line(self) -> str:
        head = self.rule.describe()
        if not self.real_n:
            return head + "  [no real correction yet; the rule stands untested]"
        body = (f"  [{self.real_n} real correction(s), median "
                f"{self.real_median:+.3f}, {self.far_side} on the far side]")
        return head + body + ("  CONTESTED" if self.contested else "")


def report(rule: NudgeRule, real: list[float], median) -> RuleReport:
    """Whether the desk agrees with a rule, from the real corrections alone.

    Flagged and nothing else.  An auto-halved weight or a silent retirement
    would be a second unexamined mechanism with a smaller sample behind it,
    which is the mistake this whole module refuses to make.
    """
    med = median(real)
    sign = 1.0 if rule.value >= 0 else -1.0
    far = sum(1 for c in real if c * sign < 0)
    contested = (med is not None and len(real) >= CONTESTED_N
                 and med * sign < 0)
    return RuleReport(rule=rule, real_n=len(real), real_median=med, far_side=far,
                      contested=contested)
