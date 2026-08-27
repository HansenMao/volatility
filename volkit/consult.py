"""What the two agents say to each other.

The quoting agent (``agent.py``) knows where the market has been: an archive
of two-way prices, aged and counted. The marking agent (``marking.py``) knows
how to move the surface and what this desk does after a fit. Neither can act
on what the other knows, and the gap between them is a real one -- the quoting
agent's most interesting output is a flag it is forbidden to apply (*the mark
is 0.45 below where this has been quoted*), and the marking agent's hardest
input is the thing that flag contains.

So they confer, and the whole exchange is numbers:

1. **A finding** goes from the quote side to the mark side: this instrument,
   at this tenor, is marked here and has been quoted there, over this many
   observations from this many brokers, this recently. It is evidence, not an
   instruction, and it carries its own weight.
2. The mark side turns findings into what the existing fitters already take --
   a ``CurveTarget`` for the at-the-money, a two-way ``MarketQuote`` for a
   wing -- and proposes.
3. **A critique** comes back the other way: with that proposal on the book,
   how many of the observed markets does the surface now sit inside, which
   ones improved, and *which ones it broke*. A re-mark that fixes the one
   tenor somebody complained about and pushes three others out is a re-mark
   the quote side should refuse, and this is where that gets said.
4. The mark side weights what it broke and tries again, a bounded number of
   times, and the best round is put in front of a person.

**No language model is anywhere near this.** Both sides produce numbers; a
model between them could only paraphrase, and the numeric guard that makes
``llm.py`` safe cannot check a negotiation. What the model may do, at the very
end, is describe the round that won.

**The critique is the honest half.** It is easy to build a loop where the
marking agent proposes and the quoting agent applauds, because both are
reading the same archive -- fit to the archive, then score against the
archive, and of course it improved. The two things that stop this being
circular: the score counts *inside the observed two-way*, not distance to its
mid, so a fit that lands anywhere sensible scores the same and only a fit that
leaves the market scores worse; and **every** finding is scored, including the
ones no target was built from, so the tenors the fit was not aimed at are
exactly where a re-mark gets caught doing damage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import marking
from .marketmaker import CurveTarget, Evaluator, resolve_expiries
from .quotes import MarketQuote
from .timeutil import TenorError, tenor_to_years

DAYS_IN_YEAR = 365.2425

#: A finding is only raised when the mark is outside the observed two-way by
#: more than this, in volatility points.  Inside the market is not a
#: disagreement, and a surface nudged toward the middle of every market it has
#: ever seen is a surface being led.
MIN_GAP = 0.02

#: Rounds of propose-and-critique before the best one is put to a person.
MAX_ROUNDS = 3

#: What a broken finding's target is multiplied by on the next round.
REWEIGHT = 3.0


class ConsultError(Exception):
    """A conference that cannot be held."""


@dataclass(frozen=True)
class Finding:
    """The quote side's evidence that the surface is somewhere the market is not."""

    pair: str
    instrument: str
    tenor: str
    delta: float | None
    days: float
    model_mid: float           # volatility points
    typical: float
    low: float
    high: float
    observations: int
    sources: int
    newest_days: float
    quote: MarketQuote = None

    @property
    def inside(self) -> bool:
        return self.low - 1e-9 <= self.model_mid <= self.high + 1e-9

    @property
    def gap(self) -> float:
        """How far outside, signed.  Zero when inside."""
        if self.inside:
            return 0.0
        return (self.model_mid - self.high if self.model_mid > self.high
                else self.model_mid - self.low)

    @property
    def weight(self) -> float:
        """How much this finding deserves to be believed.

        More observations and more brokers count for more; an old one counts
        for less. Deliberately crude -- it orders findings against each other
        and nothing else depends on its scale.
        """
        depth = min(3.0, (self.observations ** 0.5) * (1.0 + 0.5 * max(0, self.sources - 1)))
        fresh = 1.0 if self.newest_days < 1 else max(0.25, 1.0 / (1.0 + self.newest_days / 5.0))
        return depth * fresh

    def describe(self) -> str:
        what = f"{self.tenor} {self.instrument.upper()}"
        if self.delta is not None:
            what = f"{self.tenor} {self.delta * 100:g}d {self.instrument.upper()}"
        where = "inside" if self.inside else f"{self.gap:+.3f} outside"
        return (f"{what}: marked {self.model_mid:.3f}, quoted {self.low:.3f}/{self.high:.3f} "
                f"({self.observations} obs, {self.sources} broker(s), "
                f"{'today' if self.newest_days < 1 else f'{self.newest_days:.0f}d old'}) -- "
                f"{where}")


def findings_from(book, pair: str, synthesis, *, method: str | None = None,
                  cut: str = "NY", min_gap: float = MIN_GAP,
                  forwards: dict | None = None) -> tuple[list[Finding], list[str]]:
    """Every point where the archive has an opinion, priced on the surface.

    Points the surface already agrees with are kept, not filtered: they are
    what the critique measures damage against, and a loop that only ever saw
    its complaints would have no way to notice it had broken something.
    """
    surface = book[pair]
    notes: list[str] = []
    rows: list[tuple[Finding, MarketQuote]] = []
    wanted = [lv for lv in synthesis.levels if lv.enough]
    if not wanted:
        notes.append(f"the archive holds no level for {pair.upper()} with enough behind it, "
                     f"so there is nothing to tell the marking agent")
        return [], notes

    quotes: list[MarketQuote] = []
    keep: list = []
    for lv in wanted:
        try:
            days = tenor_to_years(lv.tenor) * DAYS_IN_YEAR
        except (TenorError, ValueError):
            notes.append(f"{lv.tenor} is not a tenor this build can price at; that level was "
                         f"not passed on")
            continue
        # The observed range as a two-way. The hinge in ``tune_smile_shifts``
        # wants a market to sit inside, and the range the archive actually saw
        # is a better statement of that than the typical mid with a made-up
        # width around it.
        low, high = min(lv.low, lv.high), max(lv.low, lv.high)
        if high - low < min_gap:
            pad = 0.5 * (min_gap - (high - low))
            low, high = low - pad, high + pad
        q = MarketQuote(instrument=lv.instrument, expiry=lv.tenor.upper(),
                        bid=low / 100.0, ask=high / 100.0, delta=lv.delta,
                        fly_kind="market" if lv.instrument == "fly" else None,
                        label=f"archive: {lv.observations} obs", line=len(quotes) + 1)
        quotes.append(q)
        keep.append((lv, days, q))

    if not quotes:
        return [], notes
    try:
        expiries = resolve_expiries(book.clock, quotes)
    except Exception as exc:                     # noqa: BLE001
        notes.append(f"the archived tenors could not be resolved to expiries: {exc}")
        return [], notes
    evaluator = Evaluator(surface, method or surface.method, cut)
    out: list[Finding] = []
    for lv, days, q in keep:
        try:
            mid = evaluator.value(q, expiries, forwards or {}) * 100.0
        except Exception as exc:                 # noqa: BLE001 - one point, one failure
            notes.append(f"{lv.tenor} {lv.instrument}: the surface could not be read there "
                         f"({type(exc).__name__}: {exc})")
            continue
        out.append(Finding(
            pair=pair.upper(), instrument=lv.instrument, tenor=lv.tenor.upper(),
            delta=lv.delta, days=days, model_mid=mid, typical=lv.typical,
            low=q.bid * 100.0, high=q.ask * 100.0, observations=lv.observations,
            sources=1, newest_days=lv.newest_days, quote=q))
    out.sort(key=lambda f: (f.days, f.instrument))
    return out, notes


def targets_from(findings: list[Finding], *, extra_weight: dict | None = None
                 ) -> tuple[list[CurveTarget], list[float]]:
    """The at-the-money findings as a target term structure, with weights.

    Only the at-the-money: a risk reversal is a statement about the *shape*
    and belongs to the wing tune, and feeding one to a curve fit that can only
    move the level would ask a level to explain a skew.
    """
    targets, weights = [], []
    for f in findings:
        if f.instrument != "atm":
            continue
        targets.append(CurveTarget(
            tenor=f.tenor, t=f.days / DAYS_IN_YEAR, vol=f.typical / 100.0,
            source=(f"the archive: {f.observations} observation(s), "
                    f"newest {'today' if f.newest_days < 1 else f'{f.newest_days:.0f}d ago'}")))
        weights.append(f.weight * float((extra_weight or {}).get(_key(f), 1.0)))
    return targets, weights


def wing_quotes_from(findings: list[Finding]) -> list[MarketQuote]:
    """The findings that constrain the smile rather than the level."""
    return [f.quote for f in findings
            if f.instrument in ("rr", "fly", "outright") and f.quote is not None]


def _key(f: Finding) -> str:
    return f"{f.instrument}.{f.tenor}" + ("" if f.delta is None else f"@{f.delta:g}")


@dataclass
class Row:
    """One finding, before and after a proposal."""

    key: str
    describe: str
    before: float
    after: float
    inside_before: bool
    inside_after: bool
    gap_before: float
    gap_after: float

    @property
    def improved(self) -> bool:
        return (not self.inside_before and self.inside_after) or (
            not self.inside_after and abs(self.gap_after) < abs(self.gap_before) - 1e-9)

    @property
    def worsened(self) -> bool:
        return (self.inside_before and not self.inside_after) or (
            not self.inside_before and abs(self.gap_after) > abs(self.gap_before) + 1e-9)

    def line(self) -> str:
        arrow = f"{self.before:.3f} -> {self.after:.3f}"
        state = ("inside" if self.inside_after else f"{self.gap_after:+.3f} outside")
        mark = " broke" if self.worsened else (" fixed" if self.improved else "")
        return f"{self.key}: {arrow}, {state}{mark}"


@dataclass
class Critique:
    """What the quote side makes of a proposal."""

    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def inside_before(self) -> int:
        return sum(1 for r in self.rows if r.inside_before)

    @property
    def inside_after(self) -> int:
        return sum(1 for r in self.rows if r.inside_after)

    @property
    def broke(self) -> list[Row]:
        return [r for r in self.rows if r.worsened]

    @property
    def fixed(self) -> list[Row]:
        return [r for r in self.rows if r.improved]

    @property
    def worst(self) -> float:
        return max((abs(r.gap_after) for r in self.rows), default=0.0)

    @property
    def score(self) -> tuple:
        """Better is larger.  Inside count first, then the worst dislocation.

        Counting *inside* rather than distance-to-mid is what stops this being
        a fit scored against its own objective: anywhere inside the observed
        two-way scores the same, so the loop cannot improve its score by
        walking the surface onto the middle of every market it has seen.
        """
        return (self.inside_after, -self.worst)

    @property
    def verdict(self) -> str:
        if not self.rows:
            return "nothing to judge"
        if self.broke and not self.fixed:
            return f"refused: it breaks {len(self.broke)} point(s) and fixes none"
        if self.broke:
            return (f"mixed: fixes {len(self.fixed)}, breaks {len(self.broke)}; "
                    f"{self.inside_before} -> {self.inside_after} inside")
        if self.fixed:
            return (f"better: fixes {len(self.fixed)} and breaks nothing; "
                    f"{self.inside_before} -> {self.inside_after} inside")
        return "no material change at any archived point"

    def lines(self) -> list[str]:
        out = [f"critique: {self.verdict}"]
        out += ["  " + r.line() for r in self.rows if r.worsened or r.improved]
        out += ["  note " + n for n in self.notes]
        return out


def critique(book, pair: str, findings: list[Finding], snapshot: dict, *,
             method: str | None = None, cut: str = "NY",
             forwards: dict | None = None) -> Critique:
    """Score a proposal at every archived point, not only the ones it aimed at."""
    out = Critique()
    live = [f for f in findings if f.quote is not None]
    if not live:
        out.notes.append("there are no archived points to score this against")
        return out
    surface = book[pair]
    quotes = [f.quote for f in live]
    expiries = resolve_expiries(book.clock, quotes)
    after: dict[str, float] = {}
    with marking.marked(book, pair, snapshot) as problems:
        evaluator = Evaluator(surface, method or surface.method, cut)
        for f, q in zip(live, quotes):
            try:
                after[_key(f)] = evaluator.value(q, expiries, forwards or {}) * 100.0
            except Exception as exc:             # noqa: BLE001
                out.notes.append(f"{_key(f)}: could not be read after the proposal ({exc})")
        out.notes.extend(problems or [])
    for f in live:
        key = _key(f)
        if key not in after:
            continue
        now = after[key]
        inside_after = f.low - 1e-9 <= now <= f.high + 1e-9
        gap_after = 0.0 if inside_after else (now - f.high if now > f.high else now - f.low)
        out.rows.append(Row(key=key, describe=f.describe(), before=f.model_mid, after=now,
                            inside_before=f.inside, inside_after=inside_after,
                            gap_before=f.gap, gap_after=gap_after))
    return out


# ==========================================================================
@dataclass
class Round:
    """One pass of propose-and-critique."""

    n: int
    proposal: object
    critique: Critique
    reweighted: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        head = [f"round {self.n}"
                + (f" (weighted up: {', '.join(self.reweighted)})" if self.reweighted else "")]
        return head + ["  " + x for x in self.proposal.lines()] + \
            ["  " + x for x in self.critique.lines()]


@dataclass
class Conference:
    """The whole exchange, and the round that won."""

    pair: str
    findings: list[Finding] = field(default_factory=list)
    rounds: list[Round] = field(default_factory=list)
    best: Round | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def proposal(self):
        return None if self.best is None else self.best.proposal

    def lines(self) -> list[str]:
        out = [f"{self.pair}: {len(self.findings)} archived point(s), "
               f"{sum(1 for f in self.findings if not f.inside)} the surface disagrees with"]
        out += ["  " + f.describe() for f in self.findings]
        for r in self.rounds:
            out.append("")
            out += r.lines()
        if self.best is not None:
            out += ["", f"chosen: round {self.best.n} -- {self.best.critique.verdict}"]
        out += ["  note " + n for n in self.notes]
        return out

    def to_json(self) -> dict:
        return {
            "pair": self.pair,
            "findings": [{"key": _key(f), "describe": f.describe(), "inside": f.inside,
                          "gap": f.gap, "weight": f.weight,
                          "observations": f.observations} for f in self.findings],
            "rounds": [{"n": r.n, "verdict": r.critique.verdict,
                        "inside_before": r.critique.inside_before,
                        "inside_after": r.critique.inside_after,
                        "broke": [x.key for x in r.critique.broke],
                        "fixed": [x.key for x in r.critique.fixed],
                        "reweighted": list(r.reweighted),
                        "proposal": r.proposal.to_json()} for r in self.rounds],
            "chosen": None if self.best is None else self.best.n,
            "notes": list(self.notes),
        }


def confer(book, pair: str, synthesis, *, tendencies=None, method: str | None = None,
           cut: str = "NY", forwards: dict | None = None, rounds: int = MAX_ROUNDS,
           mid_pull: float = 0.05) -> Conference:
    """Let the two agents settle on a re-mark, and put the best of it to a person.

    Bounded on purpose. This is a search with a person at the end of it, not
    an optimiser: three rounds is enough to back off something that broke a
    tenor, and past that it is fitting the archive's noise with extra steps.
    """
    out = Conference(pair=pair.upper())
    out.findings, notes = findings_from(book, pair, synthesis, method=method, cut=cut,
                                        forwards=forwards)
    out.notes.extend(notes)
    if not out.findings:
        return out
    disagreements = [f for f in out.findings if not f.inside]
    if not disagreements:
        out.notes.append("the surface sits inside every market the archive has seen; "
                         "there is nothing for the marking agent to do")
        return out

    weights: dict[str, float] = {}
    for n in range(1, max(1, rounds) + 1):
        targets, target_weights = targets_from(out.findings, extra_weight=weights)
        wings = wing_quotes_from(out.findings)
        if not targets and not wings:
            out.notes.append("the archive's findings are all at points no fit can reach")
            break
        proposal = marking.propose(
            book, pair, targets=targets or None, wing_quotes=wings or None,
            expiries=(resolve_expiries(book.clock, [f.quote for f in out.findings])
                      if wings else None),
            forwards=forwards, tendencies=tendencies, method=method, cut=cut,
            mid_pull=mid_pull)
        if target_weights and proposal.fit is not None:
            proposal.notes.append(
                "the targets are weighted by how much evidence stands behind each: "
                + ", ".join(f"{t.tenor} x{w:.1f}" for t, w in zip(targets, target_weights)))
        judged = critique(book, pair, out.findings, proposal.after, method=method, cut=cut,
                          forwards=forwards)
        this = Round(n=n, proposal=proposal, critique=judged,
                     reweighted=sorted(weights))
        out.rounds.append(this)
        if out.best is None or judged.score > out.best.critique.score:
            out.best = this
        broke = judged.broke
        if not broke:
            if n > 1:
                out.notes.append(f"round {n} broke nothing, so the exchange stopped there")
            break
        for row in broke:
            weights[row.key] = weights.get(row.key, 1.0) * REWEIGHT
        if n == rounds:
            out.notes.append(
                f"{rounds} rounds were spent and round {out.best.n} was the best of them; "
                f"the rest is for a person")
    return out
