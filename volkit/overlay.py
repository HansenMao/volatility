"""The export overlay: an outside file of pillar quotes, laid over the book.

Some mornings the numbers a file has to carry are not the book's.  A client
run priced off somebody else's surface, a pair the desk has not marked yet, a
3Y row nobody has a market for -- the file still has to go out, and the desk
has the numbers on a sheet.  The overlay is that sheet, read once, hashed, and
laid over the book **for the bulk export only** (``claude/publishing-channels-
design.md``, *The export overlay*).

What it is: CSV or xlsx, one row per pair and tenor, in vol points::

    pair,   tenor, atm,  rr25,  rr10,  bf25, bf10
    USDJPY, 1M,    9.10, -1.80, -3.10, 0.32, 0.85
    AUDHKD, 3Y,    8.40,  0.40,  0.75, 0.30, 0.90

The workbook's own spellings -- ``RR 25D``, ``ST 25D``, ``RR 10D``,
``ST 10D`` -- are accepted as aliases, and so is ``fly25``.  A row may carry
``atm_bid`` and ``atm_ask`` instead of (or as well as) ``atm``, in which case
the tier is bypassed for that row and the preflight says so.  A blank cell is
**not** a zero: it falls through to the book where the book has that pair and
tenor, and is refused by name where it does not.

Three rules, each written after thinking about what the file is *for*:

* **The book does not constrain it.**  A row for a pair nobody has marked, or
  a tenor no sheet quotes, is a perfectly good row.  The overlay is a carrier
  for numbers that came from somewhere else; the whole point is to publish a
  full file on a morning when the book marks two thirds of it.  What *does*
  constrain a row is the channel: a row naming a pair or tenor the channel
  does not publish is ignored and counted, so a file built for Bloomberg
  dropped into a COS run does not quietly become a COS file.
* **Export only is the default.**  The overlay is applied to a *view* of the
  book built for the export; every other screen shows the marks as they were,
  and there is nothing to revert because nothing changed.
* **Applied to the session, it is a snapshot away from undone.**  *Apply to
  session* captures the session first (``pre-overlay-<stamp>.json`` beside
  the session file), then puts the rows the book *can* hold onto it as
  ordinary overwrites; *Revert* puts the snapshot back.  While an overlay is
  applied the workbook write path refuses by name: an overlay is by
  definition not marked, and the workbook is the book of record.

Every value the overlay supplies is tagged with where it came from, so a
file exported from one can never be mistaken, a week later, for one built
from the marks.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .kace import canonical_tenor, pillar_years
from .timeutil import tenor_to_years

#: The five quotes a row carries, in the order every channel writes them.
FIELDS = ("atm", "rr25", "rr10", "bf25", "bf10")
#: A row may carry the ATM as a two-way instead of a mid.
TWO_WAY = ("atm_bid", "atm_ask")
#: Every spelling a column may have, folded to the name the code uses.  The
#: workbook's own headings are here so a pair sheet pasted into a CSV reads
#: without renaming anything; the kACE tab's ``fly`` and the Murex file's
#: ``bfly`` are here for the same reason.
ALIASES: dict[str, str] = {
    "pair": "pair", "ccy pair": "pair", "ccypair": "pair", "cur pair": "pair",
    "currency pair": "pair", "cross": "pair",
    "tenor": "tenor", "expiry": "tenor", "maturity": "tenor", "pillar": "tenor",
    "atm": "atm", "atm mid": "atm", "mid": "atm", "vol": "atm", "atm vol": "atm",
    "atm_mid": "atm",
    "atm_bid": "atm_bid", "atm bid": "atm_bid", "bid": "atm_bid",
    "atm_ask": "atm_ask", "atm ask": "atm_ask", "ask": "atm_ask", "offer": "atm_ask",
    "atm_offer": "atm_ask", "atm offer": "atm_ask",
    "rr25": "rr25", "rr_25": "rr25", "rr 25d": "rr25", "rr25d": "rr25", "25d rr": "rr25",
    "25rr": "rr25", "rr 25": "rr25",
    "rr10": "rr10", "rr_10": "rr10", "rr 10d": "rr10", "rr10d": "rr10", "10d rr": "rr10",
    "10rr": "rr10", "rr 10": "rr10",
    "bf25": "bf25", "bf_25": "bf25", "st25": "bf25", "st_25": "bf25", "st 25d": "bf25",
    "st25d": "bf25", "fly25": "bf25", "fly_25": "bf25", "25d fly": "bf25", "25fly": "bf25",
    "bfly25": "bf25", "25d bf": "bf25", "bf 25": "bf25", "st 25": "bf25",
    "bf10": "bf10", "bf_10": "bf10", "st10": "bf10", "st_10": "bf10", "st 10d": "bf10",
    "st10d": "bf10", "fly10": "bf10", "fly_10": "bf10", "10d fly": "bf10", "10fly": "bf10",
    "bfly10": "bf10", "10d bf": "bf10", "bf 10": "bf10", "st 10": "bf10",
}


class OverlayError(ValueError):
    """The file cannot be read as an overlay, and this says why."""


def _fold(heading) -> str:
    return " ".join(str(heading or "").strip().lower().replace("-", " ").split())


def _canon_pair(text) -> str:
    """``AUD/USD``, ``audusd`` and ``AUD USD`` are one pair."""
    p = "".join(c for c in str(text or "").upper() if c.isalpha())
    return p


@dataclass(frozen=True)
class OverlayRow:
    """One pair and tenor's quotes, as the file gave them.  ``None`` is blank."""

    pair: str
    tenor: str
    values: dict[str, float]          # subset of FIELDS + TWO_WAY, blanks absent
    line: int

    @property
    def two_way(self) -> bool:
        return "atm_bid" in self.values and "atm_ask" in self.values


@dataclass
class Overlay:
    """The file, read and hashed.  Rows keyed by ``(pair, tenor)``."""

    path: str
    sha256: str
    rows: dict[tuple[str, str], OverlayRow] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.path).name if self.path else "(pasted)"

    @property
    def pairs(self) -> list[str]:
        return sorted({p for p, _ in self.rows})

    @property
    def tenors(self) -> list[str]:
        return sorted({t for _, t in self.rows}, key=pillar_years)

    def get(self, pair: str, tenor: str) -> OverlayRow | None:
        return self.rows.get((_canon_pair(pair), canonical_tenor(tenor)))

    def record(self, *, rows_outside_book: int | None = None) -> dict:
        """What a log entry and a message's notes carry about this overlay."""
        out = {"file": self.path or "(pasted)", "sha256": self.sha256, "rows": len(self.rows)}
        if rows_outside_book is not None:
            out["rows_outside_book"] = int(rows_outside_book)
        return out

    def per_pair(self) -> list[dict]:
        """One line per pair: how many rows, which tenors, whether any is a two-way."""
        out: dict[str, dict] = {}
        for (pair, tenor), row in self.rows.items():
            e = out.setdefault(pair, {"pair": pair, "rows": 0, "tenors": [], "two_way": 0,
                                      "fields": set()})
            e["rows"] += 1
            e["tenors"].append(tenor)
            e["two_way"] += int(row.two_way)
            e["fields"] |= set(row.values)
        for e in out.values():
            e["tenors"] = sorted(e["tenors"], key=pillar_years)
            e["fields"] = sorted(e["fields"])
        return [out[p] for p in sorted(out)]

    def summary(self) -> dict:
        return {"file": self.path or "(pasted)", "name": self.name, "sha256": self.sha256,
                "per_pair": self.per_pair(),
                "rows": len(self.rows), "pairs": self.pairs, "tenors": self.tenors,
                "columns": list(self.columns), "problems": list(self.problems),
                "notes": list(self.notes),
                "two_way_rows": sum(1 for r in self.rows.values() if r.two_way)}


def load(path: str | Path) -> Overlay:
    """Read an overlay from a file: CSV, or the first sheet of an xlsx."""
    p = Path(path)
    if not p.exists():
        raise OverlayError(f"no overlay file at {p}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        grid = _read_xlsx(p)
        blob = p.read_bytes()
    else:
        text = paths.read_text(p)
        blob = text.encode("utf-8")
        grid = _read_csv(text)
    return from_grid(grid, path=str(p), sha256=hashlib.sha256(blob).hexdigest())


def parse(text: str, *, path: str = "") -> Overlay:
    """An overlay pasted as text: the same CSV, no file."""
    if not str(text or "").strip():
        raise OverlayError("the overlay is empty: paste rows of pair, tenor, atm, rr25, rr10, "
                           "bf25, bf10 (blank cells fall through to the book)")
    return from_grid(_read_csv(text), path=path,
                     sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def _read_csv(text: str) -> list[list]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def _read_xlsx(p: Path) -> list[list]:
    from . import configsheets
    wb = configsheets.open_workbook(p)
    try:
        ws = wb[wb.sheetnames[0]]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def from_grid(grid: list[list], *, path: str = "", sha256: str = "") -> Overlay:
    """Rows of cells into an :class:`Overlay`.  The header is found, not assumed."""
    ov = Overlay(path=path, sha256=sha256)
    header: list[str] | None = None
    start = 0
    for n, raw in enumerate(grid):
        cells = ["" if c is None else str(c).strip() for c in raw]
        if not any(cells):
            continue
        if cells[0].startswith("#"):
            continue
        names = [ALIASES.get(_fold(c), "") for c in cells]
        if "pair" in names and "tenor" in names:
            header = names
            start = n + 1
            ov.columns = [c for c in names if c]
            break
    if header is None:
        raise OverlayError("the overlay has no header row naming pair and tenor (and atm, "
                           "rr25, rr10, bf25, bf10, or the workbook's RR 25D / ST 25D "
                           "spellings); a header is the first row that names both")
    unknown = [str(c).strip() for c, name in zip(grid[start - 1], header)
               if str(c or "").strip() and not name]
    if unknown:
        ov.notes.append(f"column(s) {', '.join(unknown)} are not quotes and were ignored")
    if not any(f in header for f in FIELDS + TWO_WAY):
        raise OverlayError(f"the overlay's header names no quote column; expected some of "
                           f"{', '.join(FIELDS + TWO_WAY)}")
    for n, raw in enumerate(grid[start:], start=start + 1):
        cells = list(raw)
        if not any(c is not None and str(c).strip() for c in cells):
            continue
        first = "" if cells[0] is None else str(cells[0]).strip()
        if first.startswith("#"):
            continue
        row: dict[str, object] = {}
        for name, value in zip(header, cells):
            if name:
                row[name] = value
        pair = _canon_pair(row.get("pair"))
        tenor_text = str(row.get("tenor") or "").strip()
        if len(pair) != 6:
            ov.problems.append(f"line {n}: {row.get('pair')!r} is not a six-letter pair")
            continue
        if not tenor_text:
            ov.problems.append(f"line {n}: {pair} has no tenor")
            continue
        tenor = canonical_tenor(tenor_text)
        if tenor != "O/N":
            try:
                tenor_to_years(tenor)
            except ValueError as exc:
                ov.problems.append(f"line {n}: {pair} {tenor_text!r}: {exc}")
                continue
        values: dict[str, float] = {}
        bad = False
        for name in FIELDS + TWO_WAY:
            if name not in row:
                continue
            v = row[name]
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            try:
                x = float(str(v).replace(",", "")) if isinstance(v, str) else float(v)
            except (TypeError, ValueError):
                ov.problems.append(f"line {n}: {pair} {tenor} {name} is {v!r}, not a number")
                bad = True
                break
            if not math.isfinite(x):
                ov.problems.append(f"line {n}: {pair} {tenor} {name} is not finite")
                bad = True
                break
            values[name] = x
        if bad:
            continue
        if ("atm_bid" in values) != ("atm_ask" in values):
            ov.problems.append(f"line {n}: {pair} {tenor} names one side of the ATM two-way "
                               f"and not the other")
            continue
        if "atm_bid" in values and values["atm_bid"] > values["atm_ask"]:
            ov.problems.append(f"line {n}: {pair} {tenor} atm_bid {values['atm_bid']:g} is "
                               f"above atm_ask {values['atm_ask']:g}")
            continue
        if "atm_bid" in values and "atm" not in values:
            values["atm"] = 0.5 * (values["atm_bid"] + values["atm_ask"])
        if not values:
            # A row that names a pair and tenor and nothing else is a row that
            # falls through to the book entirely; kept, so the count is honest.
            pass
        key = (pair, tenor)
        if key in ov.rows:
            ov.problems.append(f"line {n}: {pair} {tenor} is listed twice; the first row "
                               f"is kept")
            continue
        ov.rows[key] = OverlayRow(pair=pair, tenor=tenor, values=values, line=n)
    if ov.problems:
        raise OverlayError(f"the overlay {ov.name} could not be read:\n  "
                           + "\n  ".join(ov.problems))
    if not ov.rows:
        raise OverlayError(f"the overlay {ov.name} has a header and no rows")
    return ov


# ---------------------------------------------------------------------------
# applying to the live book
# ---------------------------------------------------------------------------
#: The overlay's wing names as the surface spells them.
WING_FIELDS = {"rr25": "rr_25", "rr10": "rr_10", "bf25": "st_25", "bf10": "st_10"}


def split_by_book(overlay: Overlay, book) -> tuple[list[OverlayRow], list[OverlayRow]]:
    """The rows the book can hold, and the rows it cannot (pair or tenor)."""
    inside: list[OverlayRow] = []
    outside: list[OverlayRow] = []
    for row in overlay.rows.values():
        if row.pair in book and _book_lists(book, row.pair, row.tenor):
            inside.append(row)
        else:
            outside.append(row)
    return inside, outside


def _book_lists(book, pair: str, tenor: str) -> bool:
    if tenor == "O/N":
        return False                     # no sheet quotes an O/N row; it is a curve point
    surface = book[pair]
    try:
        return surface.lists_tenor(tenor)
    except Exception:  # noqa: BLE001 - a tenor the surface cannot place is outside
        return False


def overflow_report(overlay: Overlay, book) -> dict:
    """What a session save would drop: the rows outside the book, by name."""
    inside, outside = split_by_book(overlay, book)
    pairs_out = sorted({r.pair for r in outside if r.pair not in book})
    tenors_out = sorted({r.tenor for r in outside if r.pair in book}, key=pillar_years)
    return {"inside": len(inside), "outside": len(outside), "rows": len(overlay.rows),
            "pairs": pairs_out, "tenors": tenors_out,
            "message": (f"{len(inside)} of {len(overlay.rows)} overlay rows are inside the "
                        f"book"
                        + (f" · {len(outside)} outside it, ignored by a session save: "
                           + "; ".join(x for x in (
                               f"pairs {', '.join(pairs_out)}" if pairs_out else "",
                               f"tenor{'s' if len(tenors_out) > 1 else ''} "
                               f"{', '.join(tenors_out)}" if tenors_out else "") if x)
                           if outside else ""))}


def apply_to_book(overlay: Overlay, book, pairs=None) -> dict:
    """Put the overlay's inside rows onto the live book as ordinary overwrites.

    The ATM becomes a tenor overwrite on the curve, the wings become typed
    quotes -- exactly what the marking screen does when somebody types them --
    so every screen, the session file and the kACE feed tab see them, and
    ``Revert`` (a captured session put back) takes them off again.  A row
    carrying only a two-way is applied at its mid.  ``pairs`` narrows it to
    the pairs ticked on the screen; the rest of the file stays export-only.
    Returns what was applied and what was refused, by name.
    """
    inside, outside = split_by_book(overlay, book)
    if pairs is not None:
        wanted = {str(p).strip().upper() for p in pairs}
        skipped = [r for r in inside if r.pair not in wanted]
        inside = [r for r in inside if r.pair in wanted]
    else:
        skipped = []
    applied: list[str] = []
    problems: list[str] = []
    touched: set[str] = set()
    for row in sorted(inside, key=lambda r: (r.pair, pillar_years(r.tenor))):
        surface = book[row.pair]
        try:
            if "atm" in row.values:
                surface.atm.overwrite_tenor(row.tenor, row.values["atm"] / 100.0)
            for name, fld in WING_FIELDS.items():
                if name in row.values:
                    surface.overwrite_quote(row.tenor, fld, row.values[name] / 100.0)
            touched.add(row.pair)
            applied.append(f"{row.pair} {row.tenor}")
        except (TypeError, ValueError) as exc:
            problems.append(f"{row.pair} {row.tenor}: {exc}")
    for pair in sorted(touched):
        surface = book[pair]
        if surface.quote_overwrites and surface.fits:
            surface.calibrate()
            problems.extend(f"{pair}: {w}" for w in surface.warnings)
            surface.warnings.clear()
        surface.invalidate()
    report = overflow_report(overlay, book)
    return {"applied": applied, "problems": problems, "pairs": sorted(touched),
            "not_applied": sorted({r.pair for r in skipped}),
            "outside": len(outside), "overflow": report, "message": report["message"]}
