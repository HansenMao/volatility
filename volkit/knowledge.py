"""The per-pair knowledge bank: what the desk knows that the model does not.

A volatility model produces a mid.  It does not produce a market.  Turning one
into the other is desk knowledge -- how wide EURUSD one-week at-the-money goes
in a hundred million, that this pair's wings are always quoted wider than the
model's own uncertainty would suggest, that a particular tenor is habitually
shown a touch over the curve because of a settlement quirk.  None of that
belongs in ``atm.py`` or ``sabr.py``; all of it is real, and re-deriving it
from memory every morning is how it gets lost.

So it is stored, per pair, in a JSON file beside the workbook, and applied as
an **overlay** on top of the model mid.  Three principles:

* **An entry either applies or it is advice, and the two never blur.**  A
  ``spread``, ``floor`` or ``shift`` rule changes the quote and says by how
  much.  A ``note`` is prose, is shown beside the quote, and is *never*
  applied -- a note that reads like an instruction the tool silently ignores
  is the same failure as a silent zero.
* **Whatever applied is named.**  Every quoted row reports the rule that set
  its width and the rule that moved its mid, along with the rules that matched
  but were beaten.  A width nobody can trace back to a rule is a width nobody
  can argue with.
* **Nothing is invented.**  There is no built-in default width.  A quote no
  rule matches gets no bid and no offer, and says so.  Numbers that appear on
  a screen without a source are the thing this project exists to remove;
  ``suggest_rules`` therefore proposes a starter ladder **measured from a
  pasted market**, with the evidence attached, rather than from a constant.

Widths, floors and shifts are all in volatility points -- what the desk says
out loud -- and are converted at the panel boundary like everything else.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .paths import app_dir
from .timeutil import tenor_to_years

BANK_FILENAME = "mm_knowledge.json"
BANK_VERSION = 1

RULE_KINDS = ("spread", "floor", "shift", "note")
RULE_INSTRUMENTS = ("any", "atm", "rr", "fly", "outright", "spread")
SIZE_BASES = ("any", "vega", "notional")


class KnowledgeError(ValueError):
    """Raised when a bank file or a rule cannot be read."""


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One piece of desk knowledge, with the conditions it applies under.

    Every condition left at ``None`` is simply not tested, so a rule with no
    conditions at all is the pair's catch-all.  ``value`` is in volatility
    points for ``spread``, ``floor`` and ``shift``, and is unused for ``note``.
    """

    kind: str
    value: float | None = None
    instrument: str = "any"
    tenor: str | None = None            # exact tenor, e.g. '1M'
    min_days: float | None = None       # applies at or above this many days
    max_days: float | None = None       # applies at or below
    max_size: float | None = None       # applies at or below, in millions
    size_basis: str = "any"
    delta: float | None = None          # 0.25, 0.10 ...
    text: str = ""

    def validate(self) -> list[str]:
        issues = []
        if self.kind not in RULE_KINDS:
            issues.append(f"unknown rule kind {self.kind!r}; expected one of {RULE_KINDS}")
        if self.instrument not in RULE_INSTRUMENTS:
            issues.append(f"unknown instrument {self.instrument!r}; expected one of {RULE_INSTRUMENTS}")
        if self.size_basis not in SIZE_BASES:
            issues.append(f"unknown size basis {self.size_basis!r}; expected one of {SIZE_BASES}")
        if self.kind in ("spread", "floor"):
            if self.value is None or not math.isfinite(self.value) or self.value <= 0:
                issues.append(f"a {self.kind} rule needs a positive width, got {self.value!r}")
        if self.kind == "shift" and (self.value is None or not math.isfinite(self.value)):
            issues.append(f"a shift rule needs a finite offset, got {self.value!r}")
        if self.kind == "note" and not self.text.strip():
            issues.append("a note rule with no text says nothing")
        if self.tenor is not None:
            try:
                tenor_to_years(self.tenor)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                issues.append(f"tenor {self.tenor!r}: {exc}")
        for name in ("min_days", "max_days", "max_size"):
            v = getattr(self, name)
            if v is not None and (not math.isfinite(v) or v <= 0):
                issues.append(f"{name} must be positive, got {v!r}")
        if (self.min_days is not None and self.max_days is not None
                and self.min_days > self.max_days):
            issues.append(f"min_days {self.min_days:g} is above max_days {self.max_days:g}, "
                          f"so this rule can never match")
        if self.delta is not None and not 0.0 < self.delta < 0.5:
            issues.append(f"delta must lie in (0, 0.5), got {self.delta!r}")
        return issues

    # -- matching ---------------------------------------------------------
    def matches(self, *, instrument: str, days: float, tenor: str | None,
                size: float | None, size_basis: str, delta: float | None) -> bool:
        if self.instrument != "any" and self.instrument != instrument:
            return False
        if self.tenor is not None and (tenor or "").upper() != self.tenor.upper():
            return False
        if self.min_days is not None and days < self.min_days:
            return False
        if self.max_days is not None and days > self.max_days:
            return False
        if self.delta is not None and (delta is None or abs(delta - self.delta) > 1e-9):
            return False
        if self.max_size is not None:
            if size is None or size > self.max_size:
                return False
            if self.size_basis != "any" and size_basis not in (self.size_basis, "unspecified"):
                return False
        return True

    @property
    def specificity(self) -> tuple:
        """How narrow this rule is; the narrowest matching rule wins.

        Sorted highest first, so the tuple is built to be compared directly:
        an exact tenor beats an instrument, which beats a delta, which beats a
        range, and among ranges the tighter window wins.
        """
        return (
            1 if self.tenor is not None else 0,
            1 if self.instrument != "any" else 0,
            1 if self.delta is not None else 0,
            1 if self.max_size is not None else 0,
            -(self.max_size if self.max_size is not None else float("inf")),
            -(self.max_days if self.max_days is not None else float("inf")),
            self.min_days if self.min_days is not None else 0.0,
        )

    def describe(self) -> str:
        bits = []
        if self.tenor:
            bits.append(self.tenor.upper())
        elif self.max_days is not None or self.min_days is not None:
            lo = f"{self.min_days:g}d" if self.min_days is not None else ""
            hi = f"{self.max_days:g}d" if self.max_days is not None else ""
            bits.append(f"{lo}-{hi}".strip("-") or "any tenor")
        if self.instrument != "any":
            bits.append(self.instrument.upper())
        if self.delta is not None:
            bits.append(f"{int(round(self.delta * 100))}d")
        if self.max_size is not None:
            basis = "" if self.size_basis == "any" else f" {self.size_basis}"
            bits.append(f"<={self.max_size:g}mm{basis}")
        where = " ".join(bits) or "anything"
        if self.kind == "note":
            return f"note on {where}"
        return f"{self.kind} {self.value:+.3f} on {where}" if self.kind == "shift" \
            else f"{self.kind} {self.value:.3f} on {where}"


def rule_from_dict(raw: dict) -> Rule:
    """Build a Rule from JSON or a request body, coercing blanks to None."""
    def opt(key):
        v = raw.get(key)
        if v in (None, "", "-"):
            return None
        return float(v)

    tenor = raw.get("tenor")
    tenor = None if tenor in (None, "", "-") else str(tenor).strip().upper()
    return Rule(
        kind=str(raw.get("kind") or "spread").strip().lower(),
        value=opt("value"),
        instrument=str(raw.get("instrument") or "any").strip().lower(),
        tenor=tenor,
        min_days=opt("min_days"), max_days=opt("max_days"), max_size=opt("max_size"),
        size_basis=str(raw.get("size_basis") or "any").strip().lower(),
        delta=opt("delta"),
        text=str(raw.get("text") or "").strip(),
    )


# ---------------------------------------------------------------------------
# what a lookup produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overlay:
    """The bank's verdict for one quote."""

    spread: float | None
    spread_rule: str | None
    floor: float | None
    floor_rule: str | None
    shift: float
    shift_rule: str | None
    notes: tuple[str, ...] = ()          # advisory prose, never applied
    beaten: tuple[str, ...] = ()         # rules that matched but lost
    reason: str = ""                     # why there is no width, when there is none


# ---------------------------------------------------------------------------
# the bank
# ---------------------------------------------------------------------------


@dataclass
class PairKnowledge:
    rules: list[Rule] = field(default_factory=list)
    updated: str = ""
    source_note: str = ""

    def problems(self) -> list[str]:
        out = []
        for i, r in enumerate(self.rules, start=1):
            out.extend(f"rule {i} ({r.kind}): {p}" for p in r.validate())
        return out

    def overlay(self, *, instrument: str, days: float, tenor: str | None = None,
                size: float | None = None, size_basis: str = "unspecified",
                delta: float | None = None, fallback: float | None = None) -> Overlay:
        """Resolve every rule that applies to one quote.

        Widths and shifts take the **narrowest** matching rule, and later rules
        break ties, so appending a rule refines rather than fights.  Floors are
        the one exception: every matching floor applies and the widest wins,
        because that is what a floor means.
        """
        hits = [r for r in self.rules
                if r.matches(instrument=instrument, days=days, tenor=tenor,
                             size=size, size_basis=size_basis, delta=delta)]

        def best(kind: str) -> Rule | None:
            of_kind = [r for r in hits if r.kind == kind]
            if not of_kind:
                return None
            return max(enumerate(of_kind), key=lambda p: (p[1].specificity, p[0]))[1]

        spread_rule = best("spread")
        shift_rule = best("shift")
        floors = [r for r in hits if r.kind == "floor" and r.value is not None]
        floor_rule = max(floors, key=lambda r: r.value) if floors else None

        spread = spread_rule.value if spread_rule is not None else fallback
        reason = ""
        if spread_rule is None:
            reason = ("no width rule in the bank matches this quote"
                      + ("; the panel fallback was used instead" if fallback is not None
                         else ", and no panel fallback is set, so it has no bid or offer"))
        if spread is not None and floor_rule is not None and floor_rule.value > spread:
            spread = floor_rule.value

        beaten = [r.describe() for r in hits
                  if r.kind in ("spread", "shift") and r is not spread_rule and r is not shift_rule]
        return Overlay(
            spread=spread,
            spread_rule=spread_rule.describe() if spread_rule is not None else None,
            floor=floor_rule.value if floor_rule is not None else None,
            floor_rule=floor_rule.describe() if floor_rule is not None else None,
            shift=float(shift_rule.value) if shift_rule is not None else 0.0,
            shift_rule=shift_rule.describe() if shift_rule is not None else None,
            notes=tuple(r.text for r in hits if r.kind == "note" and r.text),
            beaten=tuple(beaten),
            reason=reason,
        )


@dataclass
class KnowledgeBank:
    """Every pair's knowledge, and the file it came from."""

    pairs: dict[str, PairKnowledge] = field(default_factory=dict)
    path: str | None = None
    problems: list[str] = field(default_factory=list)

    # -- io ---------------------------------------------------------------
    @classmethod
    def default_path(cls) -> Path:
        """Beside the user's workbook -- desk knowledge is the user's data."""
        return app_dir() / BANK_FILENAME

    @classmethod
    def load(cls, path: str | Path | None = None) -> "KnowledgeBank":
        """Read a bank.  A missing file is an empty bank, not an error."""
        p = Path(path) if path else cls.default_path()
        bank = cls(path=str(p))
        if not p.exists():
            bank.problems.append(f"no knowledge bank at {p}; it will be created on the first save")
            return bank
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(f"cannot read the knowledge bank at {p}: {exc}") from None
        if not isinstance(raw, dict) or "pairs" not in raw:
            raise KnowledgeError(
                f"{p} is not a knowledge bank; expected an object with a 'pairs' key, got "
                f"{type(raw).__name__}")
        version = int(raw.get("version") or 1)
        if version > BANK_VERSION:
            bank.problems.append(
                f"{p} was written by a newer version of volkit (format {version}, this build "
                f"reads {BANK_VERSION}); anything it does not understand is left untouched")
        for name, body in (raw.get("pairs") or {}).items():
            rules = []
            for i, r in enumerate(body.get("rules") or [], start=1):
                try:
                    rules.append(rule_from_dict(r))
                except (TypeError, ValueError) as exc:
                    bank.problems.append(f"{name} rule {i} could not be read and was dropped: {exc}")
            pk = PairKnowledge(rules=rules, updated=str(body.get("updated") or ""),
                               source_note=str(body.get("source_note") or ""))
            bank.problems.extend(f"{name}: {x}" for x in pk.problems())
            bank.pairs[name.upper()] = pk
        return bank

    def save(self, path: str | Path | None = None) -> str:
        """Write the bank atomically, so an interrupted save cannot lose it."""
        p = Path(path) if path else (Path(self.path) if self.path else self.default_path())
        payload = {
            "version": BANK_VERSION,
            "pairs": {
                name: {
                    "rules": [asdict(r) for r in pk.rules],
                    "updated": pk.updated,
                    "source_note": pk.source_note,
                }
                for name, pk in sorted(self.pairs.items())
            },
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".mm_knowledge", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.path = str(p)
        return str(p)

    # -- access -----------------------------------------------------------
    def for_pair(self, pair: str) -> PairKnowledge:
        return self.pairs.get(pair.upper(), PairKnowledge())

    def set_pair(self, pair: str, rules: list[Rule], when: datetime,
                 source_note: str = "") -> list[str]:
        """Replace a pair's rules.  Returns validation problems; a bad set is
        rejected whole rather than saved half-applied."""
        pk = PairKnowledge(rules=list(rules),
                           updated=when.replace(microsecond=0).isoformat(),
                           source_note=source_note)
        problems = pk.problems()
        if problems:
            return problems
        self.pairs[pair.upper()] = pk
        return []


# ---------------------------------------------------------------------------
# learning a starter ladder from a pasted market
# ---------------------------------------------------------------------------

# The buckets a desk actually thinks in.  They only decide how the *observed*
# widths are grouped; no width comes from this table.
_BUCKETS = ((7.0, "out to a week"), (31.0, "out to a month"),
            (93.0, "out to three months"), (366.0, "out to a year"),
            (float("inf"), "beyond a year"))


def suggest_rules(quotes, *, days_of, min_observations: int = 1) -> tuple[list[Rule], list[str]]:
    """Propose spread rules from the widths a pasted market actually showed.

    This is the only way a width gets into the bank without somebody typing
    it, and it is still not invented: every proposed rule is the median width
    of quotes that were really on the screen, and its text says how many and
    over what range.  Quotes written as a single mid have no width and are
    excluded -- averaging a zero width in would quietly tighten the ladder.

    ``days_of`` maps a quote to its calendar days to expiry, so this function
    needs no clock of its own.
    """
    from statistics import median

    buckets: dict[tuple[str, float, float | None], list[float]] = {}
    for q in quotes:
        if q.is_choice:
            continue
        days = days_of(q)
        if days is None or not math.isfinite(days) or days <= 0:
            continue
        edge = next(e for e, _ in _BUCKETS if days <= e)
        buckets.setdefault((q.instrument, edge, q.delta), []).append(q.spread)

    rules: list[Rule] = []
    notes: list[str] = []
    for (instrument, edge, delta), widths in sorted(
            buckets.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2] or 0)):
        if len(widths) < min_observations:
            notes.append(f"{instrument} {'' if delta is None else f'{delta:.0%} '}"
                         f"had only {len(widths)} two-way quote(s); no rule proposed")
            continue
        label = next(lbl for e, lbl in _BUCKETS if e == edge)
        width = float(median(widths))
        rules.append(Rule(
            kind="spread", value=width, instrument=instrument,
            max_days=(None if not math.isfinite(edge) else edge),
            min_days=None, delta=delta,
            text=(f"median of {len(widths)} quoted width(s) {label} in this paste "
                  f"({min(widths):.3f} to {max(widths):.3f})"),
        ))
    if not rules:
        notes.append("no two-way quote in the paste had a width, so there is nothing to learn "
                     "from it; add rules by hand instead")
    return rules, notes


def merge_rules(existing: list[Rule], proposed: list[Rule]) -> tuple[list[Rule], list[str]]:
    """Add proposed rules, replacing any existing rule with the same conditions.

    Replacing rather than appending keeps re-learning from a fresh paste from
    silently stacking a dozen near-identical ladders that only differ in which
    one happens to sort last.
    """
    def key(r: Rule):
        return (r.kind, r.instrument, r.tenor, r.min_days, r.max_days, r.max_size,
                r.size_basis, r.delta)

    out = list(existing)
    notes = []
    index = {key(r): i for i, r in enumerate(out)}
    for r in proposed:
        k = key(r)
        if k in index:
            old = out[index[k]]
            out[index[k]] = replace(r, text=r.text)
            notes.append(f"replaced {old.describe()} with {r.describe()}")
        else:
            out.append(r)
            notes.append(f"added {r.describe()}")
    return out, notes
