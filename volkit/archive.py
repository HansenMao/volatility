"""Everything the desk has seen, kept, and never quietly rewritten.

``knowledge.py`` holds what the desk *concluded*: this pair goes 0.4 wide in a
hundred million, that tenor is shown over the curve.  It is a small file of
rules a person wrote or approved.  This module holds the thing those rules are
supposed to be conclusions *from* -- the observations themselves, one row per
thing seen, with the text it was read out of still attached.

Four kinds of thing get seen, and they are deliberately four kinds rather than
one, because they are evidence of different things:

``quote``
    A market somebody showed: a broker run, a chat line, a screen.  Evidence
    of **where the market is and how wide it is shown**.
``trade``
    A trade that printed, out of an SDR dissemination file.  Evidence of
    **where business actually got done**, which is not the same as where it
    was quoted -- a trade prints at one side of somebody's market.
``shown``
    A price *we* made.  Evidence of nothing about the market at all, and the
    only record of what we did.
``outcome``
    What happened to a price we made -- traded, passed, done away at a level.
    Evidence of **whether our market was right**, and the only kind that can
    tell us we were systematically too tight on one side.

Three rules govern the file, and all three exist because the alternative has a
known failure:

**It is append-only.**  A record is never edited and never deleted.  A quote
that turns out to have been misread is *superseded* by a new record naming it,
exactly as ``quotes.ParsedRun`` keeps a superseded quote rather than dropping
it -- because the wrong reading is itself evidence (of how that broker writes)
and because a history that silently changes is a history nobody can reconcile
against a screenshot.  ``Archive.live`` is the view with supersessions applied;
``Archive.all`` is the file.

**Every record says how it was read.**  ``via`` is ``parser`` when the
deterministic grammar in ``quotes.py`` read the line, ``model:<name>`` when a
local model turned prose into a line that the grammar *then* accepted, and
``sdr`` for a dissemination row.  This is not bookkeeping.  A width statistic
computed over model-extracted lines is a different statistic from one computed
over lines a person typed in the house format, and a desk has to be able to
ask for either.  ``raw`` keeps the source text so any row can be argued with.

**An identical observation seen twice is one observation.**  The id is a hash
of the content -- not of the file it came from, not of when it was read -- so
re-reading yesterday's chat log does not double the evidence behind a width.
That is the single most likely way for this thing to lie: a folder rescanned
nightly, every width slowly gaining confidence it never earned.  The hash
deliberately excludes ``notes`` and ``via``, because the same quote read twice
by two different routes is still one quote.

Volatility lives here in **volatility points** -- 8.20, the way it is said out
loud -- because every consumer of this file is a screen, a report or a rule
written in points, and the one conversion into decimals happens where the
model is entered, as it does everywhere else in this project.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .paths import app_dir, read_text

ARCHIVE_FILENAME = "mm_archive.jsonl"
ARCHIVE_VERSION = 1

#: What a record can be.  Adding one means deciding what it is evidence *of*.
KINDS = ("quote", "trade", "shown", "outcome")

#: What happened to a price we made.  ``done_away`` carries the level it went
#: at when we know it, which is the only one of these that is itself a market
#: observation -- and it is still not a ``quote``, because we did not see the
#: two-way, only the side that traded.
RESULTS = ("traded_bid", "traded_ask", "passed", "missed", "pulled", "done_away")

#: Instruments, matching :data:`quotes.INSTRUMENTS` exactly.  A fifth name here
#: that the quote parser does not know would be a bucket nothing ever lands in.
INSTRUMENTS = ("atm", "rr", "fly", "outright", "spread")


class ArchiveError(Exception):
    """A malformed archive, or a record that cannot be written."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str:
    """A timestamp as text, always UTC, always with the offset on it.

    A naive timestamp in a file that outlives the session that wrote it is a
    timestamp in whatever zone the reader assumes, and the reader is usually
    wrong by the number of hours that matters most -- the ones between a
    London quote and a Tokyo one.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text: str) -> datetime | None:
    """Read a timestamp written by :func:`_iso`, or an obvious variant of it.

    Returns ``None`` rather than raising, because a record with an unreadable
    timestamp is still a record -- it simply cannot take part in anything that
    decays with age, and the caller is the one that has to say so.
    """
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
class Observation:
    """One thing seen, in volatility points, with its provenance attached."""

    kind: str
    pair: str
    at: str = ""                    # when the thing happened (not when it was read)

    # --- what instrument, in the vocabulary quotes.py already speaks --------
    instrument: str = "atm"
    tenor: str = ""                 # "1M", "3M", or a date for a dated expiry
    tenor_far: str | None = None    # the far leg of a calendar spread
    # A *fraction*, 0.25 for a 25 delta -- the spelling ``quotes.py`` parses
    # into and ``knowledge.Rule.delta`` matches on.  It is written in points
    # on every screen, and :meth:`describe` is the one place that converts.
    # Storing points here instead would mean a rule lookup silently missing.
    delta: float | None = None
    strike: float | None = None
    is_call: bool | None = None
    fly_kind: str | None = None     # "market" or "smile"; a fly is two instruments
    leg: str | None = None

    # --- the numbers -------------------------------------------------------
    bid: float | None = None        # volatility points
    ask: float | None = None
    size: float | None = None
    size_basis: str = "unspecified"

    # --- what our model said at the time, when we knew ---------------------
    # Kept on the record rather than recomputed later, because "was the market
    # over our curve that morning" cannot be answered by asking today's curve.
    model_mid: float | None = None
    model_note: str = ""

    # --- trades only -------------------------------------------------------
    premium: float | None = None
    premium_ccy: str = ""
    notional: float | None = None
    #: The currency the notional is in.  Without it a premium per unit of
    #: notional cannot be turned into a volatility: which side of the pair the
    #: size is on decides whether the premium is domestic or foreign, and the
    #: two differ by the forward.
    notional_ccy: str = ""
    notional_capped: bool = False   # the SDR published a cap, not the size
    expiry_date: str = ""
    action: str = ""                # NEWT / CANC / CORR, as disseminated
    #: The publisher's own identifier -- an SDR dissemination id.  Kept so a
    #: later cancel or correction naming that id can find the record it
    #: cancels.  Without it a correction is a second trade, and a print that
    #: was cancelled goes on counting as business that got done.
    external_id: str = ""
    #: An external id this record corrects, before it has been resolved to an
    #: archive id.  ``Archive.resolve`` turns it into ``supersedes``; a record
    #: reaching the file with this still set names a trade we never saw, and
    #: says so rather than silently cancelling nothing.
    supersedes_external: str = ""

    # --- prices we made, and what became of them ---------------------------
    ref: str = ""                   # the id of the ``shown`` record this answers
    result: str = ""                # one of RESULTS
    away_level: float | None = None

    # --- provenance --------------------------------------------------------
    source: str = ""                # "chat", "sdr", "desk"
    origin: str = ""                # file, channel or broker the row came from
    counterparty: str = ""
    via: str = "parser"             # "parser", "model:<name>", "sdr", "hand"
    raw: str = ""
    line: int = 0
    recorded: str = ""              # when *we* wrote it down
    supersedes: str = ""            # the id this record corrects
    notes: tuple[str, ...] = ()

    # ----------------------------------------------------------------------
    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def width(self) -> float | None:
        """The shown width, or ``None``.

        A one-sided record has no width, and a *negative* one is refused
        upstream rather than clamped: an inverted market is a misread line,
        and a misread line quietly turned into a 0.0 width is precisely the
        silent zero this project exists to remove.
        """
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def when(self) -> datetime | None:
        return parse_time(self.at) or parse_time(self.recorded)

    def content_key(self) -> str:
        """The fields that make two observations the same observation.

        ``via``, ``notes``, ``recorded`` and ``origin`` are excluded on
        purpose: the same broker line pasted into two files, or read once by
        the grammar and once by a model, is one market -- and counting it
        twice would inflate every statistic built on top of it.
        """
        parts = [self.kind, self.pair.upper(), self.at, self.instrument, self.tenor,
                 self.tenor_far or "", _num(self.delta), _num(self.strike),
                 "" if self.is_call is None else str(bool(self.is_call)),
                 self.fly_kind or "", self.leg or "",
                 _num(self.bid), _num(self.ask), _num(self.size), self.size_basis,
                 _num(self.premium), _num(self.notional), self.notional_ccy,
                 self.expiry_date,
                 self.action, self.external_id, self.ref, self.result, _num(self.away_level),
                 self.counterparty, self.raw.strip()]
        return "|".join(parts)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.content_key().encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        """One line a person can read back, in the vocabulary they typed."""
        what = self.instrument.upper()
        if self.instrument in ("rr", "fly") and self.delta is not None:
            what = f"{self.delta * 100.0:g}d {self.instrument.upper()}"
        elif self.instrument == "outright" and self.strike is not None:
            what = f"{self.strike:g} {'call' if self.is_call else 'put' if self.is_call is False else 'strike'}"
        elif self.instrument == "spread" and self.tenor_far:
            what = f"{self.tenor}/{self.tenor_far} spread"
        head = f"{self.pair} {self.tenor} {what}".replace("  ", " ").strip()
        if self.kind == "outcome":
            level = "" if self.away_level is None else f" at {self.away_level:.3f}"
            return f"{head}: {self.result or 'outcome'}{level}"
        if self.bid is not None and self.ask is not None:
            body = f"{self.bid:.3f}/{self.ask:.3f}"
        elif self.premium is not None:
            shown_premium = (f"{self.premium:,.0f}" if abs(self.premium) >= 1000
                             else f"{self.premium:g}")
            body = f"premium {shown_premium} {self.premium_ccy}".strip()
        else:
            body = "no level"
        size = "" if self.size is None else f" in {self.size:g}mm {self.size_basis}".rstrip()
        return f"{head} {body}{size}"

    def problems(self) -> list[str]:
        """Everything wrong with this record, named.

        Called before a write, never after: a bad record that reached the file
        is a bad record every later reader has to defend against.
        """
        bad: list[str] = []
        if self.kind not in KINDS:
            bad.append(f"kind {self.kind!r} is not one of {', '.join(KINDS)}")
        if not self.pair or len(self.pair) < 6:
            bad.append(f"pair {self.pair!r} does not look like a currency pair")
        if self.instrument not in INSTRUMENTS:
            bad.append(f"instrument {self.instrument!r} is not one of {', '.join(INSTRUMENTS)}")
        if self.kind == "outcome":
            if self.result not in RESULTS:
                bad.append(f"result {self.result!r} is not one of {', '.join(RESULTS)}")
            if not self.ref:
                bad.append("an outcome names no price; set ref to the id of the shown record")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            # Not clamped and not swapped.  A bid over an offer is a line that
            # was read wrong, and the two repairs disagree about which number
            # was the mistake.
            bad.append(f"bid {self.bid:g} is above offer {self.ask:g}; the line was misread")
        if self.delta is not None and not (0.0 < float(self.delta) < 1.0):
            # 25 instead of 0.25 is the mistake this catches, and it is worth
            # catching: a rule written for 0.25 would never match it, so the
            # quote would silently fall through to the panel fallback.
            bad.append(f"delta {self.delta!r} is not a fraction; a 25 delta is 0.25 here")
        for name in ("bid", "ask", "away_level"):
            v = getattr(self, name)
            if v is not None and not (-1000.0 < float(v) < 1000.0):
                bad.append(f"{name} {v!r} is not a volatility in points")
        if self.kind in ("quote", "shown") and self.bid is None and self.ask is None:
            bad.append("a quote with neither a bid nor an offer is not an observation")
        if self.at and parse_time(self.at) is None:
            bad.append(f"timestamp {self.at!r} cannot be read")
        return bad

    def to_json(self) -> dict:
        """Only the fields that carry something, so the file stays readable."""
        raw = asdict(self)
        raw["notes"] = list(self.notes)
        out = {k: v for k, v in raw.items()
               if v not in (None, "", (), []) or k in ("kind", "pair")}
        out["id"] = self.id
        return out


def _num(v) -> str:
    """A number as text for hashing: one spelling per value.

    ``8.2`` and ``8.20`` are the same observation, and ``repr`` disagrees.
    """
    if v is None:
        return ""
    return f"{float(v):.10g}"


def observation_from_dict(raw: dict) -> Observation:
    """Read one record back, refusing what it cannot place.

    An unknown key is an error rather than a shrug: a newer build wrote a
    field this one does not understand, and dropping it silently turns "your
    build is older" into "the archive lost data".
    """
    if not isinstance(raw, dict):
        raise ArchiveError(f"a record must be an object, got {type(raw).__name__}")
    known = {f for f in Observation.__dataclass_fields__}
    body = dict(raw)
    body.pop("id", None)
    unknown = set(body) - known
    if unknown:
        raise ArchiveError(
            f"record has {len(unknown)} field(s) this build does not understand "
            f"({', '.join(sorted(unknown))}); it was written by a newer volkit")
    if "notes" in body:
        body["notes"] = tuple(str(x) for x in (body["notes"] or ()))
    return Observation(**body)


@dataclass
class Archive:
    """The file, in memory, with the ids it already holds."""

    path: str = ""
    records: list[Observation] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    _ids: set = field(default_factory=set, repr=False)
    #: How many of :attr:`records` are already in the file.  Counted here and
    #: not by re-counting the file's lines, because a line the loader could
    #: not read is a line in the file and *not* a record in memory, and the
    #: two counts drifting apart would append the wrong slice.
    _written: int = field(default=0, repr=False)

    # ----------------------------------------------------------------------
    @classmethod
    def default_path(cls) -> Path:
        """Beside the workbook, like the knowledge bank: it is the desk's data."""
        return app_dir() / ARCHIVE_FILENAME

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Archive":
        """Read an archive.  A missing file is an empty archive, not an error."""
        p = Path(path) if path else cls.default_path()
        arc = cls(path=str(p))
        if not p.exists():
            arc.problems.append(f"no archive at {p}; it will be created on the first record")
            return arc
        try:
            text = read_text(p)
        except (OSError, UnicodeDecodeError) as exc:
            raise ArchiveError(f"cannot read the archive at {p}: {exc}") from None
        for n, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obs = observation_from_dict(json.loads(line))
            except (json.JSONDecodeError, ArchiveError, TypeError, ValueError) as exc:
                # One bad line does not condemn the file, but it is reported by
                # line number: an archive that quietly lost a morning is worse
                # than one that says which morning it lost.
                arc.problems.append(f"{p} line {n} could not be read and was skipped: {exc}")
                continue
            arc.records.append(obs)
            arc._ids.add(obs.id)
        arc._written = len(arc.records)
        return arc

    # ----------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.records)

    def __contains__(self, obs) -> bool:
        return (obs if isinstance(obs, str) else obs.id) in self._ids

    def add(self, obs: Observation) -> tuple[bool, str]:
        """Put one observation in memory.  Returns (written, why not).

        Writing is a separate step (:meth:`flush`), so a whole file's worth of
        records lands or none of it does -- a scan interrupted halfway through
        must not leave a half-read chat log looking fully read.
        """
        bad = obs.problems()
        if bad:
            return False, "; ".join(bad)
        if not obs.recorded:
            obs = replace(obs, recorded=_iso(_now()))
        if obs.id in self._ids:
            return False, "already in the archive"
        self.records.append(obs)
        self._ids.add(obs.id)
        return True, ""

    def extend(self, observations) -> tuple[int, list[str]]:
        """Add many.  Returns how many landed and why the others did not."""
        added, refused = 0, []
        for obs in observations:
            ok, why = self.add(obs)
            if ok:
                added += 1
            elif why != "already in the archive":
                refused.append(f"{obs.describe()}: {why}")
        return added, refused

    def flush(self) -> int:
        """Append everything not yet on disk, and return how many lines went.

        Append rather than rewrite, and the whole batch in one ``write``: the
        file is the record of what happened, and a rewrite is how a record of
        what happened becomes a record of what somebody last believed.
        """
        p = Path(self.path) if self.path else self.default_path()
        pending = self.records[self._written:]
        if not pending:
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        header = "" if p.exists() else (
            f'# volkit observation archive, format {ARCHIVE_VERSION}. '
            f'One JSON object per line, append-only. Volatility is in points.\n')
        body = "".join(json.dumps(o.to_json(), sort_keys=True) + "\n" for o in pending)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(header + body)
        self._written = len(self.records)
        return len(pending)

    # ----------------------------------------------------------------------
    def live(self) -> list[Observation]:
        """The archive with corrections applied: what is currently believed.

        A superseded record stays in the file and stays out of this list.  Two
        records superseding the same id is not an error -- the later one wins
        and the earlier correction is itself superseded evidence.
        """
        dead = {o.supersedes for o in self.records if o.supersedes}
        return [o for o in self.records if o.id not in dead]

    def query(self, *, pair: str | None = None, kinds=None, instrument: str | None = None,
              tenor: str | None = None, delta: float | None = None,
              since: datetime | None = None, until: datetime | None = None,
              via: str | None = None, include_superseded: bool = False) -> list[Observation]:
        """The records matching every condition given, newest last.

        Every filter is exact.  There is deliberately no fuzzy tenor match
        here -- deciding that a 32-day quote is evidence about the 1M is a
        modelling choice, it belongs in ``synthesis.py`` where it can be
        reported, and a lookup that quietly did it would make every statistic
        depend on a rule nobody could see.
        """
        rows = self.records if include_superseded else self.live()
        want_kinds = None if kinds is None else {kinds} if isinstance(kinds, str) else set(kinds)
        out = []
        for o in rows:
            if pair and o.pair.upper() != pair.upper():
                continue
            if want_kinds is not None and o.kind not in want_kinds:
                continue
            if instrument and o.instrument != instrument:
                continue
            if tenor and o.tenor.upper() != tenor.upper():
                continue
            if delta is not None and (o.delta is None or abs(o.delta - delta) > 1e-9):
                continue
            if via and not o.via.startswith(via):
                continue
            when = o.when
            if since is not None and (when is None or when < since):
                continue
            if until is not None and (when is None or when > until):
                continue
            out.append(o)
        out.sort(key=lambda o: (o.when or datetime.min.replace(tzinfo=timezone.utc)))
        return out

    def by_id(self, ident: str) -> Observation | None:
        for o in self.records:
            if o.id == ident:
                return o
        return None

    def by_external(self, ident: str) -> Observation | None:
        """The record carrying a publisher's identifier, newest first.

        Newest first because a dissemination id can be corrected more than
        once, and a cancel names the *trade*, not the particular correction.
        """
        if not ident:
            return None
        for o in reversed(self.records):
            if o.external_id == ident:
                return o
        return None

    def resolve(self, observations) -> tuple[list[Observation], list[str]]:
        """Turn ``supersedes_external`` into ``supersedes``, or refuse.

        A cancel for a trade this archive never held is not applied and not
        dropped: it is returned as a note.  Publishers cancel prints from
        before the desk started keeping this file, and a cancel silently
        matching nothing looks exactly like a cancel that worked.
        """
        out, notes = [], []
        pending = {o.external_id: o for o in observations if o.external_id}
        for obs in observations:
            want = obs.supersedes_external
            if not want:
                out.append(obs)
                continue
            target = self.by_external(want) or pending.get(want)
            if target is None:
                notes.append(
                    f"{obs.action or 'a correction'} names {want}, which is not in the archive; "
                    f"the record was kept and corrects nothing")
                out.append(replace(obs, supersedes_external="",
                                   notes=obs.notes + (f"corrects {want}, not held here",)))
                continue
            out.append(replace(obs, supersedes=target.id, supersedes_external=""))
        return out, notes

    def pairs(self) -> list[str]:
        return sorted({o.pair.upper() for o in self.records})

    def summary(self) -> list[dict]:
        """One row per pair: what is in here and how fresh it is."""
        out = []
        for pair in self.pairs():
            rows = self.query(pair=pair)
            times = [o.when for o in rows if o.when]
            counts = {k: sum(1 for o in rows if o.kind == k) for k in KINDS}
            out.append({
                "pair": pair, "records": len(rows), **counts,
                "first": _iso(min(times)) if times else "",
                "last": _iso(max(times)) if times else "",
                "undated": sum(1 for o in rows if o.when is None),
                "model_read": sum(1 for o in rows if o.via.startswith("model")),
            })
        return out


# --------------------------------------------------------------------------
# Turning what the quote parser read into what the archive keeps
# --------------------------------------------------------------------------
def from_quotes(run, *, pair: str, source: str = "chat", origin: str = "",
                counterparty: str = "", via: str = "parser",
                default_time: datetime | None = None) -> list[Observation]:
    """Every quote in a parsed run, superseded ones included.

    Superseded ones are kept for the reason ``quotes.py`` keeps them: one
    tenor quoted twice in a run is one live price and *two* observations of
    how wide that tenor is shown.  Dropping the earlier one here would throw
    away half the width evidence in every conversation the desk has.

    A run with no date anywhere is ordered on a nominal day by the parser and
    must never be given that day back, so those quotes take ``default_time``
    and say so on the record.

    ``default_time`` must be a property of the *source* -- the chat file's own
    modification time is what ``ingest.py`` passes -- and never the clock at
    the moment of reading.  The id is a hash of the content, so a fallback of
    "now" would give the same broker line a new id on every scan, and the one
    protection against a rescanned folder inflating every width statistic it
    touches would be gone.  It is a required argument in everything but name.
    """
    if default_time is None:
        raise ArchiveError(
            "from_quotes needs a default_time for the lines that carry no clock: "
            "pass the source file's own timestamp, not the current one")
    stamp = _iso(default_time)
    out: list[Observation] = []
    for q in run.all_quotes:
        notes = list(q.notes)
        at, dated = stamp, bool(q.timestamp_text)
        if dated:
            parsed = parse_time(q.timestamp_text)
            if parsed is not None:
                at = _iso(parsed)
            else:
                dated = False
        if not dated:
            notes.append("the line carried no usable time; stamped when the file was read")
        if q.replaced_by:
            notes.append(f"superseded in its own run by line {q.replaced_by}; "
                         f"kept as width evidence, not as a live level")
        out.append(Observation(
            kind="quote", pair=pair.upper(), at=at,
            instrument=q.instrument,
            tenor=_tenor_text(q.expiry),
            tenor_far=(_tenor_text(q.expiry_far) if q.expiry_far is not None else None),
            delta=q.delta, strike=q.strike, is_call=q.is_call, fly_kind=q.fly_kind,
            leg=q.leg,
            # The parser works in decimals; the archive is in points, and this
            # is the one place the conversion happens on the way in.  Rounded,
            # because 8.2 * 100 is not 8.2 in binary and the id is a hash of
            # the text of the number: unrounded, the same quote read by two
            # routes would hash two ways and be counted twice.
            bid=None if q.bid is None else round(q.bid * 100.0, 6),
            ask=None if q.ask is None else round(q.ask * 100.0, 6),
            size=q.size, size_basis=q.size_basis,
            source=source, origin=origin, counterparty=counterparty, via=via,
            raw=q.raw, line=q.line, notes=tuple(notes)))
    return out


def _tenor_text(expiry) -> str:
    """A tenor the way it was written: "1M", or an ISO date for a dated expiry."""
    if expiry is None:
        return ""
    if isinstance(expiry, str):
        return expiry.upper()
    if hasattr(expiry, "isoformat"):
        return expiry.isoformat()[:10]
    return str(expiry)


def shown(pair: str, *, instrument: str, tenor: str, bid: float | None, ask: float | None,
          delta: float | None = None, strike: float | None = None,
          is_call: bool | None = None, fly_kind: str | None = None,
          tenor_far: str | None = None, size: float | None = None,
          size_basis: str = "unspecified", counterparty: str = "",
          model_mid: float | None = None, model_note: str = "",
          at: datetime | None = None, notes=()) -> Observation:
    """A price we made, ready to be answered later by :func:`outcome`.

    ``model_mid`` is taken now and kept, not looked up later: the question an
    outcome eventually answers is "was our market right *that morning*", and
    a curve re-marked since cannot answer it.
    """
    return Observation(
        kind="shown", pair=pair.upper(), at=_iso(at or _now()), instrument=instrument,
        tenor=tenor.upper(), tenor_far=tenor_far, delta=delta, strike=strike,
        is_call=is_call, fly_kind=fly_kind, bid=bid, ask=ask, size=size,
        size_basis=size_basis, counterparty=counterparty, model_mid=model_mid,
        model_note=model_note, source="desk", via="hand", notes=tuple(notes))


def outcome(ref: Observation | str, result: str, *, away_level: float | None = None,
            at: datetime | None = None, counterparty: str = "", notes=()) -> Observation:
    """What became of a price we made.

    The instrument fields are copied off the price rather than re-typed, so an
    outcome can never end up attached to a different tenor than the quote it
    answers -- which is the one way this record could quietly poison every
    hit-rate statistic built on it.
    """
    if isinstance(ref, str):
        return Observation(kind="outcome", pair="", at=_iso(at or _now()), ref=ref,
                           result=result, away_level=away_level, source="desk",
                           via="hand", counterparty=counterparty, notes=tuple(notes))
    return Observation(
        kind="outcome", pair=ref.pair, at=_iso(at or _now()), instrument=ref.instrument,
        tenor=ref.tenor, tenor_far=ref.tenor_far, delta=ref.delta, strike=ref.strike,
        is_call=ref.is_call, fly_kind=ref.fly_kind, size=ref.size,
        size_basis=ref.size_basis, ref=ref.id, result=result, away_level=away_level,
        counterparty=counterparty or ref.counterparty, source="desk", via="hand",
        notes=tuple(notes))
