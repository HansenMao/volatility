"""Every time somebody moved a mark, and what they moved it from.

A *remark* here is a **re-mark**: one instance of a person changing what the
surface says.  ``session.py`` saves where the marks ended up; this saves that
they moved, from what, to what, and -- when the tool had suggested something
-- what the person did to the suggestion.  The first is a file you load; the
second is the only record of the desk's judgement, and it is the thing a
marking agent can learn anything at all from.

**The instance is a diff of two snapshots, not an instrumented control.**
``session.capture_pair`` already photographs every knob, so a re-marking
instance is a before, an after and a subtraction.  Nothing in the marking
screen has to report anything, nothing can be forgotten when a new control is
added, and a session file from last month can be turned into instances
retroactively.  The cost is that a diff cannot tell *why* something moved,
which is exactly what the verdict field is for.

**A verdict is worth more than a diff.**  ``unprompted`` is somebody marking;
``accepted``, ``edited`` and ``rejected`` are somebody answering a proposal,
and those carry the one signal a diff cannot: what the tool would have done,
beside what the desk actually did.  A month of the second is worth a year of
the first, which is why the marking agent asks.

**Nothing here decides anything.**  This is a file and a subtraction.  What a
tendency *means* -- whether a knob this desk has never let move should be
pinned -- is ``marking.py``'s question, and it is answered with the number of
instances attached so it can be argued with.

The file is append-only and content-addressed, like ``archive.py``, and for
the same reason: a morning re-marked twice must not become two instances of a
desk that likes moving that knob.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .paths import app_dir, read_text

JOURNAL_FILENAME = "mm_remarks.jsonl"
JOURNAL_VERSION = 1

#: What a person did.  ``unprompted`` is a re-mark the tool did not suggest;
#: the other three are answers to a proposal, and are worth much more.
VERDICTS = ("accepted", "edited", "rejected", "unprompted")

#: The kinds of thing a snapshot holds, and what a change to each is called.
#: These are the keys ``session.capture_pair`` writes.
SECTIONS = ("curve", "param_shifts", "atm_overwrites", "smile_overwrites",
            "smile_term", "events", "anchor_tenors", "band", "quote_overwrites")

#: Sections where a key that is simply absent means a real zero, so a knob
#: appearing from nothing is a *move* of that size.  A missing smile shift is
#: a shift of zero; a missing tenor overwrite is not an overwrite of zero, it
#: is the absence of one, and the two must not be counted the same way -- one
#: is "they moved it", the other is "they left the curve to speak".
ZERO_IF_ABSENT = ("param_shifts",)

#: Knobs whose value is a volatility in points on the screen.  Everything in
#: this module is in the units the screen shows, like the knowledge bank: a
#: tendency a person cannot read in the units they type is a tendency they
#: cannot check.
POINT_KNOBS = ("initial_vol", "long_term_vol", "short_addon", "rate_vol")


class RemarkError(Exception):
    """A journal that cannot be read, or an instance that cannot be written."""


def _iso(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    raw = str(text).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    for attempt in (raw, raw[:19], raw[:10]):
        try:
            dt = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


@dataclass(frozen=True)
class Change:
    """One knob that moved, in the units the screen shows it in."""

    section: str            # one of SECTIONS
    knob: str               # "long_term_vol", "rho25", "1M", ...
    where: str = ""         # the tenor, for a per-tenor overwrite
    before: float | None = None
    after: float | None = None
    proposed: float | None = None

    @property
    def key(self) -> str:
        return f"{self.section}.{self.knob}" + (f"@{self.where}" if self.where else "")

    @property
    def move(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before

    @property
    def correction(self) -> float | None:
        """What the person did to the suggestion.  The high-value number.

        Positive means they landed above what was proposed.  ``None`` when
        nothing was proposed for this knob, which is most of the time.
        """
        if self.proposed is None or self.after is None:
            return None
        return self.after - self.proposed

    @property
    def unit(self) -> str:
        return "vol points" if self.knob in POINT_KNOBS or self.section in (
            "atm_overwrites", "quote_overwrites") else ""

    def describe(self) -> str:
        def fmt(v):
            return "—" if v is None else f"{v:,.4g}"
        body = f"{self.key}: {fmt(self.before)} -> {fmt(self.after)}"
        if self.proposed is not None:
            body += f" (proposed {fmt(self.proposed)})"
        return body


def _numbers(section: str, body) -> dict[tuple[str, str], float]:
    """One section of a snapshot flattened to ``(knob, where) -> value``.

    Everything that is not a number is skipped rather than compared: events
    and the band treatment are structures, and "the event schedule changed" is
    a fact about a schedule, not a knob that moved by an amount.
    """
    out: dict[tuple[str, str], float] = {}
    if body is None:
        return out
    if isinstance(body, bool):
        out[(section, "")] = float(body)
        return out
    if isinstance(body, (int, float)):
        out[(section, "")] = float(body)
        return out
    if isinstance(body, dict):
        for name, value in body.items():
            if isinstance(value, bool):
                out[(str(name), "")] = float(value)
            elif isinstance(value, (int, float)):
                out[(str(name), "")] = float(value)
            elif isinstance(value, dict):
                for where, inner in value.items():
                    if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                        out[(str(name), str(where))] = float(inner)
    return out


def diff_snapshots(before: dict, after: dict, proposed: dict | None = None) -> list[Change]:
    """What moved between two snapshots, with what was proposed beside it.

    A knob that appears in one snapshot and not the other counts as a change
    with a missing end: an overwrite that was *cleared* is a re-marking
    instance, and treating a missing key as "unchanged" would lose exactly the
    decisions a marker thinks hardest about.
    """
    out: list[Change] = []
    for section in SECTIONS:
        was = _numbers(section, (before or {}).get(section))
        now = _numbers(section, (after or {}).get(section))
        want = _numbers(section, (proposed or {}).get(section)) if proposed else {}
        for key in sorted(set(was) | set(now) | set(want)):
            a, b = was.get(key), now.get(key)
            p = want.get(key)
            if section in ZERO_IF_ABSENT:
                a = 0.0 if a is None else a
                b = 0.0 if b is None else b
                p = None if p is None else p
            if a is None and b is None:
                continue
            if a is not None and b is not None and abs(a - b) <= 1e-12 and p is None:
                continue
            if a == b and p is not None and abs((p or 0.0) - (b or 0.0)) <= 1e-12:
                continue        # proposed exactly what was already there
            out.append(Change(section=section, knob=key[0], where=key[1],
                              before=a, after=b, proposed=p))
    return out


@dataclass(frozen=True)
class Remark:
    """One re-marking instance."""

    pair: str
    at: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    proposed: dict = field(default_factory=dict)
    verdict: str = "unprompted"
    #: What was true when it happened -- whether a market was pasted, whether
    #: an event fell in the window, how fresh the archive was.  Kept as the
    #: agent recorded it rather than recomputed, because the point of a
    #: context is what was known *then*.
    context: dict = field(default_factory=dict)
    note: str = ""
    source: str = "screen"
    recorded: str = ""

    def content_key(self) -> str:
        parts = [self.pair.upper(), self.at, self.verdict,
                 json.dumps(self.before, sort_keys=True),
                 json.dumps(self.after, sort_keys=True),
                 json.dumps(self.proposed, sort_keys=True)]
        return "|".join(parts)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.content_key().encode("utf-8")).hexdigest()[:16]

    @property
    def when(self) -> datetime | None:
        return parse_time(self.at) or parse_time(self.recorded)

    def changes(self) -> list[Change]:
        return diff_snapshots(self.before, self.after, self.proposed or None)

    @property
    def answered(self) -> bool:
        """Did a person answer a proposal, rather than just mark?"""
        return self.verdict in ("accepted", "edited", "rejected")

    def describe(self) -> str:
        moved = self.changes()
        head = f"{self.pair} {self.at[:16]} {self.verdict}"
        if not moved:
            return f"{head}: nothing moved"
        return f"{head}: {len(moved)} knob(s) -- " + "; ".join(c.describe() for c in moved[:4])

    def problems(self) -> list[str]:
        bad = []
        if not self.pair or len(self.pair) < 6:
            bad.append(f"pair {self.pair!r} does not look like a currency pair")
        if self.verdict not in VERDICTS:
            bad.append(f"verdict {self.verdict!r} is not one of {', '.join(VERDICTS)}")
        if self.at and parse_time(self.at) is None:
            bad.append(f"timestamp {self.at!r} cannot be read")
        if self.verdict in ("accepted", "edited") and not self.proposed:
            bad.append(f"a verdict of {self.verdict!r} answers a proposal, but none is recorded")
        if not self.before and not self.after:
            bad.append("an instance with no snapshots on either side records nothing")
        return bad

    def to_json(self) -> dict:
        out = {"pair": self.pair, "at": self.at, "verdict": self.verdict,
               "before": self.before, "after": self.after, "source": self.source,
               "recorded": self.recorded, "id": self.id}
        if self.proposed:
            out["proposed"] = self.proposed
        if self.context:
            out["context"] = self.context
        if self.note:
            out["note"] = self.note
        return out


def remark_from_dict(raw: dict) -> Remark:
    if not isinstance(raw, dict):
        raise RemarkError(f"an instance must be an object, got {type(raw).__name__}")
    known = set(Remark.__dataclass_fields__)
    body = {k: v for k, v in raw.items() if k != "id"}
    unknown = set(body) - known
    if unknown:
        raise RemarkError(
            f"instance has {len(unknown)} field(s) this build does not understand "
            f"({', '.join(sorted(unknown))}); it was written by a newer volkit")
    return Remark(**body)


@dataclass
class Journal:
    """The file of re-marking instances, in memory."""

    path: str = ""
    entries: list[Remark] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    _ids: set = field(default_factory=set, repr=False)
    _written: int = field(default=0, repr=False)

    @classmethod
    def default_path(cls) -> Path:
        return app_dir() / JOURNAL_FILENAME

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Journal":
        p = Path(path) if path else cls.default_path()
        out = cls(path=str(p))
        if not p.exists():
            out.problems.append(f"no journal at {p}; it will be created on the first instance")
            return out
        try:
            text = read_text(p)
        except (OSError, UnicodeDecodeError) as exc:
            raise RemarkError(f"cannot read the journal at {p}: {exc}") from None
        for n, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = remark_from_dict(json.loads(line))
            except (json.JSONDecodeError, RemarkError, TypeError, ValueError) as exc:
                out.problems.append(f"{p} line {n} could not be read and was skipped: {exc}")
                continue
            out.entries.append(entry)
            out._ids.add(entry.id)
        out._written = len(out.entries)
        return out

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, entry: Remark) -> tuple[bool, str]:
        bad = entry.problems()
        if bad:
            return False, "; ".join(bad)
        if not entry.recorded:
            entry = replace(entry, recorded=_iso(datetime.now(timezone.utc)))
        if entry.id in self._ids:
            return False, "already in the journal"
        self.entries.append(entry)
        self._ids.add(entry.id)
        return True, ""

    def flush(self) -> int:
        p = Path(self.path) if self.path else self.default_path()
        pending = self.entries[self._written:]
        if not pending:
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        header = "" if p.exists() else (
            f"# volkit re-marking journal, format {JOURNAL_VERSION}. One JSON object per "
            f"line, append-only. Knob values are in the units the screen shows.\n")
        body = "".join(json.dumps(e.to_json(), sort_keys=True) + "\n" for e in pending)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(header + body)
        self._written = len(self.entries)
        return len(pending)

    def query(self, *, pair: str | None = None, since: datetime | None = None,
              until: datetime | None = None, answered_only: bool = False) -> list[Remark]:
        out = []
        for e in self.entries:
            if pair and e.pair.upper() != pair.upper():
                continue
            if answered_only and not e.answered:
                continue
            when = e.when
            if since is not None and (when is None or when < since):
                continue
            if until is not None and (when is None or when > until):
                continue
            out.append(e)
        out.sort(key=lambda e: (e.when or datetime.min.replace(tzinfo=timezone.utc)))
        return out

    def pairs(self) -> list[str]:
        return sorted({e.pair.upper() for e in self.entries})

    def summary(self) -> list[dict]:
        out = []
        for pair in self.pairs():
            rows = self.query(pair=pair)
            times = [e.when for e in rows if e.when]
            counts = {v: sum(1 for e in rows if e.verdict == v) for v in VERDICTS}
            out.append({
                "pair": pair, "instances": len(rows), **counts,
                "answered": sum(1 for e in rows if e.answered),
                "first": _iso(min(times)) if times else "",
                "last": _iso(max(times)) if times else "",
            })
        return out


def instance(pair: str, before: dict, after: dict, *, proposed: dict | None = None,
             verdict: str = "unprompted", context: dict | None = None,
             note: str = "", source: str = "screen", at: datetime | None = None) -> Remark:
    """One instance, ready for the journal."""
    return Remark(pair=pair.upper(), at=_iso(at or datetime.now(timezone.utc)),
                  before=dict(before or {}), after=dict(after or {}),
                  proposed=dict(proposed or {}), verdict=verdict,
                  context=dict(context or {}), note=note, source=source)
