"""Keeping up: the folders the desk drops things in, read once each.

The archive is only worth anything if it is current, and it is only current if
something reads the day's chats without being asked twice.  That is this
module.  It watches folders, reads what is new, and writes observations.

**A file is read once, by content.**  Not by name, not by modification time --
by the SHA-1 of its bytes.  Copying yesterday's log to a new name does not
re-import it; touching a file does not re-import it; *editing* a file does
re-import it, because the bytes changed and the new bytes may hold a quote the
old ones did not.  The archive's own content ids then catch anything the
edited file repeated, so the two mechanisms overlap on purpose: a folder
rescanned every thirty seconds all day must not slowly inflate every width
statistic it touches, and one guard is not enough for something that runs
unattended.

**The grammar reads first and the model reads what is left.**  Every chat file
goes to ``quotes.parse_quotes`` whole.  The lines it could not read -- and only
those -- go to the local model, which rewrites them into the same grammar,
which then has to accept them.  This order matters both ways: the house format
never costs a model call, and prose never reaches the archive without the
parser having agreed with it.  Records read the second way are marked
``via="model:<name>"`` so a statistic can be computed with them or without.

**A file that cannot be placed is not read.**  A chat naming no pair is
skipped by name, with the reason, and the state file remembers the failure so
the next scan does not silently retry into the same wall.  A pair is found in
one of three places, in this order: what the caller said, a line in the file
that is nothing but a pair name, and the file's own name.  A chat covering
several pairs is *split* at the lines that name them -- which is how a desk
actually writes one -- and each block is parsed against its own pair, because
a risk reversal's direction word cannot be resolved without knowing which pair
it belongs to.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import archive as arch
from . import quotes as qmod
from . import sdr as sdrmod
from .paths import app_dir, read_text, write_text

STATE_FILENAME = "mm_ingest.json"
STATE_VERSION = 1

#: What a folder holds.  A folder's role is declared, never sniffed: a CSV of
#: quotes and a CSV of dissemination rows look alike enough that guessing is
#: how a broker run ends up filed as trades that printed.
ROLES = ("chat", "sdr")

CHAT_SUFFIXES = (".txt", ".log", ".md", ".csv", ".chat")
SDR_SUFFIXES = (".csv", ".txt", ".zip")

#: A line that is nothing but a currency pair: the header a desk puts above a
#: block of quotes.  Anchored, so "EURUSD 1M ATM 8.2/8.6" is a quote and not a
#: heading -- getting that backwards silently drops the first line of a block.
_PAIR_LINE = re.compile(r"^\s*([A-Za-z]{6})\s*[:\-]?\s*$")
_PAIR_IN_NAME = re.compile(r"(?<![A-Za-z])([A-Za-z]{6})(?![A-Za-z])")


class IngestError(Exception):
    """A watch folder or state file that cannot be used at all."""


def _digest(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_time(path: Path) -> datetime:
    """The file's own timestamp, used for lines that carry no clock.

    A property of the source and not of the scan: see
    :func:`archive.from_quotes` for why the difference matters to the ids.
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


# --------------------------------------------------------------------------
@dataclass
class FileResult:
    """One file, and everything reading it produced."""

    path: str
    role: str
    digest: str = ""
    added: int = 0
    duplicates: int = 0
    refused: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pairs: list[str] = field(default_factory=list)
    model_lines: int = 0
    error: str = ""

    def line(self) -> str:
        if self.error:
            return f"{Path(self.path).name}: not read -- {self.error}"
        bits = [f"{self.added} new"]
        if self.duplicates:
            bits.append(f"{self.duplicates} already held")
        if self.model_lines:
            bits.append(f"{self.model_lines} read by the model")
        if self.skipped:
            bits.append(f"{len(self.skipped)} line(s) not understood")
        where = "/".join(self.pairs) if self.pairs else "?"
        return f"{Path(self.path).name} [{where}]: " + ", ".join(bits)


@dataclass
class IngestResult:
    """A whole scan."""

    files: list[FileResult] = field(default_factory=list)
    unchanged: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    def summary(self) -> str:
        read = len([f for f in self.files if not f.error])
        bad = len([f for f in self.files if f.error])
        parts = [f"{read} file(s) read", f"{self.added} observation(s) added"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if bad:
            parts.append(f"{bad} could not be read")
        return ", ".join(parts)


# --------------------------------------------------------------------------
@dataclass
class State:
    """What has been read, so it is not read again."""

    path: str = ""
    files: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @classmethod
    def default_path(cls) -> Path:
        return app_dir() / STATE_FILENAME

    @classmethod
    def load(cls, path=None) -> "State":
        p = Path(path) if path else cls.default_path()
        st = cls(path=str(p))
        if not p.exists():
            return st
        try:
            raw = json.loads(read_text(p))
        except (OSError, ValueError) as exc:
            # Not fatal, and not silent.  A lost state file means everything
            # gets re-read, and the archive's ids are what stop that from
            # doubling anything -- but the desk should know it happened.
            st.problems.append(f"the ingest state at {p} could not be read ({exc}); "
                               f"every watched file will be read again")
            return st
        for name, body in (raw.get("files") or {}).items():
            if isinstance(body, dict):
                st.files[name] = body
        return st

    def seen(self, path: Path, digest: str) -> tuple[bool, str]:
        """Have these exact bytes been through?  And with what result?

        A file that failed counts as seen while its bytes are unchanged, and
        the reason comes back with it.  Retrying it every thirty seconds
        would put the same refusal in the log all day and teach the desk to
        stop reading the log; fixing the file changes the bytes, which is
        what makes it new again.
        """
        body = self.files.get(str(path))
        if not body or body.get("digest") != digest:
            return False, ""
        return True, str(body.get("error") or "")

    def record(self, result: FileResult) -> None:
        self.files[result.path] = {
            "digest": result.digest, "added": result.added,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": result.error, "pairs": result.pairs,
        }

    def save(self) -> str:
        p = Path(self.path) if self.path else self.default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        write_text(tmp, json.dumps({"version": STATE_VERSION, "files": self.files},
                                   indent=1, sort_keys=True))
        tmp.replace(p)
        return str(p)

    def forget(self, pattern: str = "") -> int:
        """Drop what has been seen, so it is read again.  Returns how many."""
        if not pattern:
            n = len(self.files)
            self.files.clear()
            return n
        gone = [k for k in self.files if pattern in k]
        for k in gone:
            del self.files[k]
        return len(gone)


# --------------------------------------------------------------------------
def split_by_pair(text: str, *, default_pair: str = "",
                  known_pairs=None) -> list[tuple[str, str, int]]:
    """Blocks of ``(pair, text, first_line)``, split at lines naming a pair.

    A chat is written pair by pair with the name on its own line above the
    quotes.  Parsing the whole thing against one pair would resolve every risk
    reversal's direction against the wrong currency, which is a sign error on
    a number the desk reads as a direction -- the worst kind of wrong, because
    it looks like a market.

    Text before the first heading belongs to ``default_pair``.  If there is no
    default and no heading, the caller gets one block with an empty pair and
    decides what to do about it -- this function never invents one.
    """
    known = {p.upper() for p in known_pairs} if known_pairs else None
    blocks: list[tuple[str, list[str], int]] = []
    current, lines, start = default_pair.upper(), [], 1
    for n, line in enumerate(str(text or "").splitlines(), start=1):
        m = _PAIR_LINE.match(line)
        name = m.group(1).upper() if m else ""
        if name and (known is None or name in known):
            if lines:
                blocks.append((current, lines, start))
            current, lines, start = name, [], n + 1
            continue
        lines.append(line)
    if lines:
        blocks.append((current, lines, start))
    return [(pair, "\n".join(body), first) for pair, body, first in blocks
            if any(x.strip() for x in body)]


def pair_from_name(path, known_pairs=None) -> str:
    """A pair out of a file name, or empty.  Never a partial match."""
    known = {p.upper() for p in known_pairs} if known_pairs else None
    for m in _PAIR_IN_NAME.finditer(Path(path).stem):
        name = m.group(1).upper()
        if known is None or name in known:
            return name
    return ""


# --------------------------------------------------------------------------
def read_chat(path, *, archive: arch.Archive, model=None, pair: str = "",
              known_pairs=None, counterparty: str = "",
              fly_convention: str = "market") -> FileResult:
    """One chat file: the grammar first, the model on what it could not read."""
    p = Path(path)
    res = FileResult(path=str(p), role="chat")
    try:
        text = read_text(p)
        res.digest = _digest(p)
        when = _file_time(p)
    except (OSError, UnicodeDecodeError) as exc:
        res.error = str(exc)
        return res

    named = pair or pair_from_name(p, known_pairs)
    blocks = split_by_pair(text, default_pair=named, known_pairs=known_pairs)
    if not blocks:
        res.notes.append("the file held nothing to read")
        return res
    if not any(b[0] for b in blocks):
        res.error = ("no currency pair: the file name does not contain one, no line in it is "
                     "just a pair name, and none was given. A risk reversal cannot be read "
                     "without knowing the pair, so nothing was taken from it")
        return res

    for block_pair, body, first_line in blocks:
        if not block_pair:
            res.skipped.append(
                f"lines {first_line}+ come before any pair is named and were not read")
            continue
        res.pairs.append(block_pair) if block_pair not in res.pairs else None
        try:
            # A year-less expiry in a chat file is read forward from the
            # file's own timestamp, which is when somebody wrote it.
            run = qmod.parse_quotes(body, pair=block_pair, fly_convention=fly_convention,
                                    today=when.date())
        except qmod.QuoteError as exc:
            res.skipped.append(f"{block_pair}: the block could not be read ({exc})")
            continue
        _absorb(res, archive, run, pair=block_pair, origin=str(p),
                counterparty=counterparty, via="parser", when=when)
        for note in run.notes:
            res.notes.append(f"{block_pair}: {note}")

        # What the grammar refused, handed to the model as one block: a line's
        # neighbours are context ("that in 100 vega" only means something
        # under the line above it), and one call is also one call.
        leftovers = [txt for _, _, txt in run.skipped if str(txt).strip()]
        if not leftovers or model is None:
            for line_no, why, txt in run.skipped:
                res.skipped.append(f"{block_pair} line {line_no} ({why}): {str(txt)[:70]}")
            continue
        extraction = _from_model(model, "\n".join(leftovers), block_pair)
        res.notes.extend(f"{block_pair}: {n}" for n in extraction.notes)
        for bad_line, why in extraction.refused:
            res.skipped.append(f"{block_pair}: the model wrote {bad_line[:60]!r} and it was "
                               f"refused -- {why}")
        if not extraction.lines:
            for line_no, why, txt in run.skipped:
                res.skipped.append(f"{block_pair} line {line_no} ({why}): {str(txt)[:70]}")
            continue
        try:
            second = qmod.parse_quotes(extraction.text, pair=block_pair,
                                       fly_convention=fly_convention, today=when.date())
        except qmod.QuoteError as exc:
            res.skipped.append(f"{block_pair}: what the model wrote was not a market ({exc})")
            continue
        for line_no, why, txt in second.skipped:
            res.skipped.append(f"{block_pair}: the model's line {str(txt)[:60]!r} was refused "
                               f"by the parser ({why})")
        tag = f"model:{getattr(getattr(model, 'config', None), 'model', 'local')}"
        before = res.added
        _absorb(res, archive, second, pair=block_pair, origin=str(p),
                counterparty=counterparty, via=tag, when=when)
        res.model_lines += res.added - before
    return res


def _absorb(res: FileResult, archive: arch.Archive, run, *, pair: str, origin: str,
            counterparty: str, via: str, when: datetime) -> None:
    obs = arch.from_quotes(run, pair=pair, source="chat", origin=origin,
                           counterparty=counterparty, via=via, default_time=when)
    added, refused = archive.extend(obs)
    res.added += added
    res.duplicates += len(obs) - added - len(refused)
    res.refused.extend(refused)


def _from_model(model, text: str, pair: str):
    from . import llm
    return llm.extract_quotes(model, text, pair=pair)


def read_sdr_file(path, *, archive: arch.Archive, known_pairs=None,
                  pairs=None) -> FileResult:
    """One dissemination file, with cancels and corrections tied to their trades."""
    p = Path(path)
    res = FileResult(path=str(p), role="sdr")
    try:
        res.digest = _digest(p)
        read = sdrmod.read_sdr(p, pairs=pairs, known_pairs=known_pairs)
    except (sdrmod.SdrError, OSError, UnicodeDecodeError) as exc:
        res.error = str(exc)
        return res
    resolved, notes = archive.resolve(read.records)
    added, refused = archive.extend(resolved)
    res.added = added
    res.duplicates = len(resolved) - added - len(refused)
    res.refused.extend(refused)
    res.notes.extend(read.notes)
    res.notes.extend(notes)
    res.pairs = sorted({o.pair for o in resolved})
    res.skipped.extend(f"row {n} ({why}): {txt[:70]}" for n, why, txt in read.skipped[:50])
    if len(read.skipped) > 50:
        res.skipped.append(f"... and {len(read.skipped) - 50} more row(s) not kept")
    return res


# --------------------------------------------------------------------------
def scan(folders, *, archive: arch.Archive, state: State, model=None,
         known_pairs=None, pair: str = "", counterparty: str = "",
         force: bool = False, limit: int = 0) -> IngestResult:
    """Read every new file in every watched folder.

    ``folders`` is a sequence of ``(path, role)``.  Nothing is written to the
    archive file here -- the caller flushes -- so a scan that raises halfway
    leaves neither the archive nor the state claiming work it did not do.
    """
    started = time.time()
    out = IngestResult()
    for raw_folder, role in folders:
        folder = Path(raw_folder)
        if role not in ROLES:
            raise IngestError(f"{folder} is declared as {role!r}, which is not one of "
                              f"{', '.join(ROLES)}")
        if not folder.exists():
            out.notes.append(f"{folder} does not exist; nothing was read from it")
            continue
        if not folder.is_dir():
            candidates = [folder]
        else:
            suffixes = CHAT_SUFFIXES if role == "chat" else SDR_SUFFIXES
            candidates = sorted(x for x in folder.iterdir()
                                if x.is_file() and x.suffix.lower() in suffixes
                                and not x.name.startswith("."))
        if not candidates:
            out.notes.append(f"{folder} holds no {role} files this build reads")
            continue
        for path in candidates:
            try:
                digest = _digest(path)
            except OSError as exc:
                out.files.append(FileResult(path=str(path), role=role, error=str(exc)))
                continue
            already, failed_because = state.seen(path, digest)
            if already and not force:
                out.unchanged += 1
                if failed_because:
                    out.notes.append(
                        f"{path.name} is still unread and unchanged since it failed: "
                        f"{failed_because}")
                continue
            if role == "chat":
                res = read_chat(path, archive=archive, model=model, pair=pair,
                                known_pairs=known_pairs, counterparty=counterparty)
            else:
                res = read_sdr_file(path, archive=archive, known_pairs=known_pairs)
            out.files.append(res)
            state.record(res)
            if limit and out.added >= limit:
                out.notes.append(
                    f"the scan stopped after {out.added} observation(s) because a limit of "
                    f"{limit} was set; the rest of the folder is unread and will be picked "
                    f"up next time")
                out.seconds = time.time() - started
                return out
    out.seconds = time.time() - started
    return out


def watch(folders, *, archive: arch.Archive, state: State, every: float = 30.0,
          rounds: int = 0, on_result=None, **kw) -> list[IngestResult]:
    """Scan, flush, sleep, repeat.

    ``rounds`` bounds it -- zero means until interrupted.  Bounded is what the
    tests use and what a cron-style single pass wants; the desk uses zero.
    Each round flushes before sleeping, so a session killed between rounds has
    already written everything it read.
    """
    out = []
    n = 0
    while True:
        result = scan(folders, archive=archive, state=state, **kw)
        archive.flush()
        state.save()
        out.append(result)
        if on_result is not None:
            on_result(result)
        n += 1
        if rounds and n >= rounds:
            return out
        try:
            time.sleep(max(1.0, float(every)))
        except KeyboardInterrupt:
            return out
