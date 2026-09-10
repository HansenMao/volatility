"""Publishing the marked surface: every channel the desk's marks go out on.

Four channels carry the desk's volatilities to other systems, and until now
each was its own spreadsheet chain (``claude/publishing-channels-design.md``):

* **kACE** -- a ``RATE_FEED`` message over HTTP, built by ``kace.py``;
* **Bloomberg** -- a workbook of DCAP contribution formulas
  (``PLContribFull``) the desk opens and refreshes;
* **Murex** -- two BIFF8 ``.xls`` files, ATM in one and the wings in the
  other, picked up by a loader that is a black box;
* **COS** -- a small CSV grid of one-sided ATMs.

They are one job.  Every channel carries the same payload -- per pair, per
tenor: an ATM two-way and the 25d/10d risk reversal and butterfly -- and
differs only in the container, the pair list, the tenor labels, how wide the
two-way is and whether the mid is shaded.  So the payload is one thing here,
a list of :class:`PillarQuote`, built once from the book (``kace.read_pillars``)
or from an overlay file (``overlay.py``), and each channel is a small record
that names its pairs, its tenors, its width source and a ``write`` that turns
the list into :class:`ExportFile` s.  Nothing about kACE's XML reaches a CSV
writer.

**A destination is a channel, not a file.**  Murex takes the ATM in one
workbook and the wings in another, so its ``write`` returns two files out of
one build: one pair list, one source choice, one preflight, one date, and the
two files written together or not at all.  A destination that wants both and
has only the ATM is refused by name -- half the pair of files is the short
file this module exists to prevent -- so a channel's ``needs`` is what the
*destination* needs, across every file it writes.

**Where the numbers come from is a choice, not a modifier.**  ``source="book"``
reads the marked book; ``source="overlay"`` lays an outside file over it, and
the overlay is not confined to the pairs and tenors the book holds.  Either
way a pillar the channel wants and neither source has is **refused by name**
-- a short file written silently is the failure mode this module exists to
kill -- and every log entry records which source built it.

**Widths and shades** are configuration tables, hand-maintained, on the Vol
exporting bulk screen and in the workbook (it is the database):

* ``SPREADS`` -- tenor rows, a column per tier (``default``, ``wide``,
  ``cos``, ...).  Pair-independent: a width is a quoting *policy*.
* ``MARKET_WIDTHS`` + ``ADD_UPS`` -- an observed market two-way per pair and
  tenor, plus a policy add-up (overnight, other tenors, with per-pair and
  crosses overrides).  Pair-dependent: this is what the market is *showing*.
* ``SHADES`` -- the mid shift in vol points, by channel, with per-pair
  exceptions.  **A shade is not a width**: it moves the ATM mid, both sides,
  before the width is put around it, so the two-way stays symmetric about a
  mid the screen can show.

A channel names its width source: kACE and COS a tier, Bloomberg the market
table, Murex neither (bid equals ask, and always has).  The pair list each
channel publishes, in the file's own order, is the ``EXPORT_PAIRS`` tab --
typed by hand like the rest, because which pairs a file carries is a fact
about the file and the file is somebody else's.

Every export, sent or refused, is appended to ``publish_log.jsonl`` beside the
workbook (``kace.PostLog``) with the channel, the source and a hash of what
was produced.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import configsheets, exportseed, kace, overlay as overlay_mod
from .cross import is_cross
from .kace import KaceError, OVERNIGHT, canonical_tenor, pillar_years
from .paths import app_dir

#: Where the files a channel writes land, beside the workbook, unless the
#: server or the command line names somewhere else.
EXPORT_DIR = "exports"
#: The five instruments every channel carries, in the order they are written.
INSTRUMENTS = ("atm", "rr25", "rr10", "bf25", "bf10")
INSTRUMENT_LABELS = {"atm": "ATM", "rr25": "25d RR", "rr10": "10d RR",
                     "bf25": "25d BF", "bf10": "10d BF"}
#: The tenor ladder the Bloomberg and Murex files carry, in the files' order.
ELEVEN = ("O/N", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y")
#: The five the COS file carries.
COS_TENORS = ("1W", "2W", "1M", "3M", "6M")
#: Where the numbers may come from.
SOURCES = ("book", "overlay")
#: What a channel's width may come from.
WIDTH_SOURCES = ("tier", "market", "none")
#: The four wings, in the order the files carry them.
WING_INSTRUMENTS_ORDER = ("rr25", "rr10", "bf25", "bf10")


class PublishError(ValueError):
    """The export cannot be built or sent, and this says why."""


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------
@dataclass
class PillarQuote:
    """One pair and tenor as a channel publishes it.  Vol points throughout.

    ``atm`` is the mid *before* the shade; ``mid`` after it; ``bid``/``ask``
    after the width.  ``feed_from`` is the curve the numbers were read from
    when that is not the published pair (COS's CNY labels, an inverted
    cross), and ``flipped`` says the risk reversals changed sign on the way.
    """

    pair: str
    tenor: str
    atm: float
    rr25: float
    rr10: float
    bf25: float
    bf10: float
    origin: str                       # marks / fitted / marks at 1W / overlay / overlay two-way
    source: str                       # book / overlay
    expiry: date | None = None
    feed_from: str = ""
    flipped: bool = False
    shade: float = 0.0
    shade_from: str = ""
    width: float = 0.0
    width_from: str = ""
    given_bid: float | None = None    # an overlay row's own two-way, tier bypassed
    given_ask: float | None = None
    #: The wings' own two-way widths (WING_WIDTHS), vol points, where the
    #: channel publishes them two-way; empty where it publishes one value.
    wing_widths: dict[str, float] = field(default_factory=dict)
    wing_widths_from: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return self.atm + self.shade

    def wing_bid(self, instrument: str) -> float:
        """A wing's bid: the value less half its own width.  Never shaded."""
        return self.value(instrument) - self.wing_widths.get(instrument, 0.0) / 2.0

    def wing_ask(self, instrument: str) -> float:
        return self.value(instrument) + self.wing_widths.get(instrument, 0.0) / 2.0

    def side(self, instrument: str) -> tuple[float, float]:
        """``(bid, ask)`` for any instrument, the ATM through the shade and width."""
        if instrument == "atm":
            return self.bid, self.ask
        return self.wing_bid(instrument), self.wing_ask(instrument)

    @property
    def bid(self) -> float:
        if self.given_bid is not None:
            return self.given_bid + self.shade
        return self.mid - self.width / 2.0

    @property
    def ask(self) -> float:
        if self.given_ask is not None:
            return self.given_ask + self.shade
        return self.mid + self.width / 2.0

    def value(self, instrument: str) -> float:
        return {"atm": self.mid, "rr25": self.rr25, "rr10": self.rr10,
                "bf25": self.bf25, "bf10": self.bf10}[instrument]

    def to_dict(self) -> dict:
        return {"pair": self.pair, "tenor": self.tenor,
                "expiry": self.expiry.isoformat() if self.expiry else None,
                "atm": self.atm, "mid": self.mid, "bid": self.bid, "ask": self.ask,
                "rr25": self.rr25, "rr10": self.rr10, "bf25": self.bf25, "bf10": self.bf10,
                "origin": self.origin, "source": self.source, "feed_from": self.feed_from,
                "flipped": self.flipped, "shade": self.shade, "shade_from": self.shade_from,
                "width": (self.ask - self.bid), "width_from": self.width_from,
                "two_way_given": self.given_bid is not None,
                "wing_widths": dict(self.wing_widths), "wing_widths_from": self.wing_widths_from,
                "sides": {i: list(self.side(i)) for i in INSTRUMENTS},
                "notes": list(self.notes)}


# ---------------------------------------------------------------------------
# the tables
# ---------------------------------------------------------------------------
@dataclass
class ExportTables:
    """Every export-policy table, read off the workbook (and the session's overlay).

    A table that is absent is ``None`` and a channel that needs it refuses by
    name; a table that is present and wrong is refused whole at load, with
    the reason kept in ``errors`` so the screen can show it beside the
    channel rather than failing on the click.
    """

    path: str = ""
    spreads: kace.SpreadTable | None = None
    market_widths: dict[str, dict[str, float]] | None = None    # pair -> tenor -> width
    add_ups: dict[str, dict[str, float | None]] | None = None    # key -> {overnight, other}
    shades: dict[str, dict[str, float]] | None = None            # channel -> pair|"" -> shade
    wing_widths: dict[str, dict[str, dict[str, float]]] | None = None  # key -> tenor -> inst -> w
    export_pairs: dict[str, list["PairEntry"]] | None = None     # channel -> entries
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None, *, overlay=None) -> "ExportTables":
        p = Path(path) if path else configsheets.default_workbook()
        t = cls(path=str(p))
        try:
            t.spreads = kace.SpreadTable.load(p, overlay=overlay)
        except KaceError as exc:
            t.errors["SPREADS"] = str(exc)
        for sheet, reader in (("MARKET_WIDTHS", _read_market_widths),
                              ("ADD_UPS", _read_add_ups),
                              ("SHADES", _read_shades),
                              ("WING_WIDTHS", _read_wing_widths),
                              ("EXPORT_PAIRS", _read_export_pairs)):
            try:
                value = reader(p, overlay)
            except (configsheets.ConfigSheetError, PublishError) as exc:
                t.errors[sheet] = str(exc)
                continue
            setattr(t, sheet.lower(), value)
        return t

    def summary(self) -> dict:
        return {"workbook": self.path,
                "present": {"SPREADS": self.spreads is not None,
                            "MARKET_WIDTHS": self.market_widths is not None,
                            "ADD_UPS": self.add_ups is not None,
                            "SHADES": self.shades is not None,
                            "WING_WIDTHS": self.wing_widths is not None,
                            "EXPORT_PAIRS": self.export_pairs is not None},
                "tiers": self.spreads.names if self.spreads is not None else [],
                "errors": dict(self.errors)}

    # -- widths ----------------------------------------------------------
    def tier_width(self, tier: str, tenor: str, *, multiplier=None) -> tuple[float, str]:
        if self.spreads is None:
            raise PublishError(self.errors.get("SPREADS") or configsheets.missing(
                kace.SPREADS_SHEET, self.path))
        name = self.spreads.resolve_tier(tier)
        ladder = self.spreads.for_tier(name)
        factor = kace.spread_multiplier(multiplier)
        t = canonical_tenor(tenor)
        if t in ladder:
            return ladder[t] * factor, f"{name} tier" + (f" x{factor:g}" if factor != 1 else "")
        w = kace.width_at(ladder, pillar_years(t), multiplier=factor)
        if w is None:
            raise PublishError(f"the {name} tier of {kace.SPREADS_SHEET} has no rows")
        return w, (f"{name} tier, read at {t} between its rungs"
                   + (f" x{factor:g}" if factor != 1 else ""))

    def market_width(self, pair: str, tenor: str, *, multiplier=None) -> tuple[float, str]:
        if self.market_widths is None:
            raise PublishError(self.errors.get("MARKET_WIDTHS") or configsheets.missing(
                "MARKET_WIDTHS", self.path))
        if self.add_ups is None:
            raise PublishError(self.errors.get("ADD_UPS") or configsheets.missing(
                "ADD_UPS", self.path))
        pair = pair.upper()
        t = canonical_tenor(tenor)
        column = self.market_widths.get(pair)
        if column is None:
            raise PublishError(f"MARKET_WIDTHS has no column for {pair}; a width that "
                               f"defaulted to zero would publish a one-price two-way, so "
                               f"the pair is refused until its widths are typed in")
        if t not in column:
            raise PublishError(f"MARKET_WIDTHS has no {t} width for {pair} (it has "
                               f"{', '.join(sorted(column, key=pillar_years))})")
        add, where = self.add_up(pair, t)
        factor = kace.spread_multiplier(multiplier)
        return (column[t] + add) * factor, (
            f"market {column[t]:g} + {where} {add:g}" + (f", x{factor:g}" if factor != 1 else ""))

    def add_up(self, pair: str, tenor: str) -> tuple[float, str]:
        """The policy add-up for one pair and tenor: the most specific row wins."""
        table = self.add_ups or {}
        pair = pair.upper()
        keys = [pair] + (["crosses"] if is_cross(pair) else []) + ["default"]
        column = "overnight" if canonical_tenor(tenor) == OVERNIGHT else "other"
        for key in keys:
            row = table.get(key)
            if row is not None and row.get(column) is not None:
                return float(row[column]), f"{key} {column} add-up"
        return 0.0, "no add-up row"

    def wing_width(self, pair: str, tenor: str) -> tuple[dict[str, float], str]:
        """The wings' two-way widths for one pair and tenor: most specific row wins.

        ``WING_WIDTHS`` is ``pair, tenor, rr25, rr10, bf25, bf10`` with
        ``default`` and ``crosses`` as pair keys beside the pairs themselves.
        Refused by name when the tab is absent or names nothing for the pair
        and tenor: a wing published one-price on a two-way feed is a width
        that quietly defaulted to zero.
        """
        if self.wing_widths is None:
            raise PublishError(self.errors.get("WING_WIDTHS") or configsheets.missing(
                "WING_WIDTHS", self.path))
        pair = pair.upper()
        t = canonical_tenor(tenor)
        keys = [pair] + (["crosses"] if is_cross(pair) else []) + ["default"]
        for key in keys:
            rows = self.wing_widths.get(key)
            if rows and t in rows:
                return dict(rows[t]), f"WING_WIDTHS {key} {t}"
        raise PublishError(f"WING_WIDTHS has no {t} row for {pair} (nor for "
                           f"{'crosses or ' if is_cross(pair) else ''}default)")

    def shade(self, channel: str, pair: str) -> tuple[float, str]:
        table = self.shades
        if table is None:
            return 0.0, "no SHADES tab"
        channel = channel_key(channel)
        rows = table.get(channel)
        if not rows:
            return 0.0, f"no {channel} shade"
        pair = pair.upper()
        if pair in rows:
            return rows[pair], f"{channel} {pair} shade"
        if "" in rows:
            return rows[""], f"{channel} default shade"
        return 0.0, f"no {channel} default shade"

    def pairs_for(self, channel: str, book=None) -> list["PairEntry"]:
        """The pairs a channel publishes, in order.

        From ``EXPORT_PAIRS``; a file channel with no rows is refused, because
        which pairs a file carries is a fact about the file.  kACE is the
        exception: a message is per pair and there is no file to match, so
        with no rows of its own it publishes every pair the book builds --
        the pair list the market-maker bar's bulk export used to offer.
        """
        key = channel_key(channel)
        entries = (self.export_pairs or {}).get(key) or []
        if entries:
            return list(entries)
        if key == "kace" and book is not None:
            return [PairEntry(pair=p, label=p, feed_from=p, last_tenor="") for p in book.pairs]
        if self.export_pairs is None:
            raise PublishError(self.errors.get("EXPORT_PAIRS") or (
                configsheets.missing("EXPORT_PAIRS", self.path)
                + " Which pairs a file carries, in the file's own order, is a fact about "
                  "the file, so it is typed rather than guessed"))
        raise PublishError(f"EXPORT_PAIRS lists no pairs for {channel}; add a row per "
                           f"pair the file carries, in the file's order")


@dataclass(frozen=True)
class PairEntry:
    """One row of ``EXPORT_PAIRS``: what a channel publishes, and from what."""

    pair: str                  # the published quotation, six letters
    label: str                 # as the file spells it (AUD/USD, USD/CNY)
    feed_from: str             # the book's curve; the pair itself when blank
    last_tenor: str            # the last tenor published, blank for the channel's whole list
    note: str = ""

    @property
    def inverted(self) -> bool:
        return self.feed_from == self.pair[3:6] + self.pair[:3]


def _read_market_widths(p: Path, overlay) -> dict[str, dict[str, float]] | None:
    rows = configsheets.read_rows(p, "MARKET_WIDTHS", required=("tenor",), overlay=overlay)
    if rows is None:
        return None
    columns = configsheets.columns_for("MARKET_WIDTHS", [r.cells for r in rows])
    pairs = [c for c in columns if configsheets.normalise(c) not in ("tenor", "note")]
    out: dict[str, dict[str, float]] = {pr.upper(): {} for pr in pairs}
    bad: list[str] = []
    for row in rows:
        tenor = canonical_tenor(row.text("tenor"))
        if not tenor:
            continue
        for pr in pairs:
            try:
                w = row.real(pr)
            except configsheets.ConfigSheetError as exc:
                bad.append(str(exc))
                continue
            if w is None:
                continue
            if w < 0:
                bad.append(f"MARKET_WIDTHS row {row.number}: {pr} {tenor} width {w:g} is negative")
                continue
            out[pr.upper()][tenor] = w
    if bad:
        raise PublishError("MARKET_WIDTHS could not be read:\n  " + "\n  ".join(bad))
    return out


def _read_add_ups(p: Path, overlay) -> dict[str, dict[str, float | None]] | None:
    rows = configsheets.read_rows(p, "ADD_UPS", required=("pair",), overlay=overlay)
    if rows is None:
        return None
    out: dict[str, dict[str, float | None]] = {}
    for row in rows:
        key = row.text("pair").strip().lower() or "default"
        if key not in ("default", "crosses"):
            key = key.upper()
        out[key] = {"overnight": row.real("overnight"), "other": row.real("other")}
    return out


def _read_shades(p: Path, overlay) -> dict[str, dict[str, float]] | None:
    rows = configsheets.read_rows(p, "SHADES", required=("channel", "shade"), overlay=overlay)
    if rows is None:
        return None
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        channel = channel_key(row.text("channel"))
        if not channel:
            continue
        pair = row.text("pair").strip().upper()
        if pair.lower() in ("default", "*"):
            pair = ""
        shade = row.real("shade")
        if shade is None:
            continue
        out.setdefault(channel, {})[pair] = shade
    return out


WING_INSTRUMENTS = ("rr25", "rr10", "bf25", "bf10")


def _read_wing_widths(p: Path, overlay) -> dict[str, dict[str, dict[str, float]]] | None:
    rows = configsheets.read_rows(p, "WING_WIDTHS", required=("pair", "tenor"), overlay=overlay)
    if rows is None:
        return None
    out: dict[str, dict[str, dict[str, float]]] = {}
    bad: list[str] = []
    for row in rows:
        key = row.text("pair").strip().lower() or "default"
        if key not in ("default", "crosses"):
            key = key.upper()
        tenor = canonical_tenor(row.text("tenor"))
        if not tenor:
            continue
        widths: dict[str, float] = {}
        for inst in WING_INSTRUMENTS:
            try:
                w = row.real(inst)
            except configsheets.ConfigSheetError as exc:
                bad.append(str(exc))
                continue
            if w is None:
                continue
            if w < 0:
                bad.append(f"WING_WIDTHS row {row.number}: {key} {tenor} {inst} {w:g} is negative")
                continue
            widths[inst] = w
        missing = [i for i in WING_INSTRUMENTS if i not in widths]
        if missing:
            bad.append(f"WING_WIDTHS row {row.number}: {key} {tenor} leaves {', '.join(missing)} "
                       f"blank; a wing width is all four or none")
            continue
        out.setdefault(key, {})[tenor] = widths
    if bad:
        raise PublishError("WING_WIDTHS could not be read:\n  " + "\n  ".join(bad))
    return out


def _read_export_pairs(p: Path, overlay) -> dict[str, list[PairEntry]] | None:
    rows = configsheets.read_rows(p, "EXPORT_PAIRS", required=("channel", "pair"),
                                  overlay=overlay)
    if rows is None:
        return None
    out: dict[str, list[PairEntry]] = {}
    bad: list[str] = []
    for row in rows:
        typed = row.text("channel").strip().lower()
        channel = channel_key(typed)
        pair = "".join(c for c in row.text("pair").upper() if c.isalpha())
        if not channel or not pair:
            continue
        if len(pair) != 6:
            bad.append(f"EXPORT_PAIRS row {row.number}: {row.text('pair')!r} is not a pair")
            continue
        feed_from = "".join(c for c in row.text("feed_from").upper() if c.isalpha()) or pair
        if len(feed_from) != 6:
            bad.append(f"EXPORT_PAIRS row {row.number}: feed_from {row.text('feed_from')!r} "
                       f"is not a pair")
            continue
        last = canonical_tenor(row.text("last_tenor")) if row.text("last_tenor") else ""
        entry = PairEntry(pair=pair, label=row.text("label").strip() or pair,
                          feed_from=feed_from, last_tenor=last, note=row.text("note"))
        if any(e.pair == pair for e in out.get(channel, [])):
            if typed != channel:
                # A workbook typed before murex_vol and murex_broker became one
                # destination carries the same pair list under both old names.
                # That is not a pair listed twice, it is the merge; the first
                # row wins and the second is the same row again.
                continue
            bad.append(f"EXPORT_PAIRS row {row.number}: {pair} is listed twice for {channel}")
            continue
        out.setdefault(channel, []).append(entry)
    if bad:
        raise PublishError("EXPORT_PAIRS could not be read:\n  " + "\n  ".join(bad))
    return out


# ---------------------------------------------------------------------------
# the channels
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    kind: str                       # "post" (kACE) or "file"
    tenors: tuple[str, ...]         # canonical; kACE's are the SPREADS rows
    width: str                      # tier / market / none
    default_tier: str               # for width == "tier"
    precision: int                  # decimals written
    what: str                       # one line for the screen
    wings_two_way: bool = False     # the wings go out bid/ask (WING_WIDTHS) rather than one value
    #: The instruments the channel needs at every pillar.  A pillar short of
    #: one of these is refused by name; the others are carried when known
    #: and left out (NaN) when not -- the COS grid and the Murex ATM file
    #: carry the ATM alone, so an overlay of ATMs is a whole file for them.
    needs: tuple[str, ...] = INSTRUMENTS

    def tenor_list(self, tables: ExportTables) -> list[str]:
        if self.tenors:
            return list(self.tenors)
        if tables.spreads is None:
            raise PublishError(tables.errors.get("SPREADS") or configsheets.missing(
                kace.SPREADS_SHEET, tables.path))
        return sorted(tables.spreads.for_tier(None), key=pillar_years)

    def summary(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "tenors": list(self.tenors), "width": self.width,
                "default_tier": self.default_tier, "what": self.what,
                "wings_two_way": self.wings_two_way}


CHANNELS: dict[str, Channel] = {
    "kace": Channel(
        key="kace", label="kACE", kind="post", tenors=(), width="tier",
        default_tier=kace.DEFAULT_TIER, precision=15,
        what="RATE_FEED XML posted to the pricing platform, one message per pair; the "
             "pillars are the SPREADS rows, the widths a tier"),
    "bloomberg": Channel(
        key="bloomberg", label="Bloomberg DCAP", kind="file", tenors=ELEVEN, width="market",
        default_tier="", precision=3, wings_two_way=True,
        what="a workbook of PLContribFull formulas the desk opens and refreshes; the ATM "
             "width is MARKET_WIDTHS plus ADD_UPS about a mid shaded by SHADES, the wings "
             "go out two-way about their marks by WING_WIDTHS"),
    "murex": Channel(
        key="murex", label="Murex", kind="file", tenors=ELEVEN, width="none",
        default_tier="", precision=2,
        what="two .xls files written together: DRV_MktData_FX_Vol_<date>.xls, the ATM as "
             "ccy pair, Maturity, bid, ask; and DRV_MktData_FX_Broker_<date>.xls, the 10 "
             "and 25 delta butterfly and risk reversal. Bid equals ask in both"),
    "cos": Channel(
        key="cos", label="COS", kind="file", tenors=COS_TENORS, width="tier",
        default_tier="cos", precision=2, needs=("atm",),
        what="COS_86830_Bid.csv: the ATM bid alone, five tenors, CNY-labelled rows fed "
             "from the CNH curves; the width is the cos tier"),
}


#: Channel names that were once two and are now one.  ``murex_vol`` and
#: ``murex_broker`` were separate destinations, each writing its own file off
#: its own pair list; they are one ``murex`` destination writing both files,
#: because a Murex load is the pair of them.  The old names are read wherever
#: a channel is named -- a command line, an ``EXPORT_PAIRS`` row typed before
#: the merge, a ``SHADES`` row -- and resolve to it.
LEGACY_CHANNELS: dict[str, str] = {"murex_vol": "murex", "murex_broker": "murex"}


def channel_key(name) -> str:
    """A channel name as this module spells it, old names included."""
    k = str(name or "").strip().lower()
    return LEGACY_CHANNELS.get(k, k)


def channel(key: str) -> Channel:
    k = channel_key(key)
    if k not in CHANNELS:
        raise PublishError(f"unknown channel {key!r}; this build publishes to "
                           f"{', '.join(CHANNELS)}")
    return CHANNELS[k]


# ---------------------------------------------------------------------------
# Bloomberg tickers
# ---------------------------------------------------------------------------
#: Two-letter Bloomberg currency codes.  A currency not here cannot be
#: contributed, and is refused by name rather than guessed at.
BBG_CODES: dict[str, str] = {
    "AUD": "AD", "GBP": "BP", "CAD": "CD", "CHF": "SF", "JPY": "JY", "NZD": "ND",
    "EUR": "EU", "USD": "US", "HKD": "HD", "CNH": "CG", "XAU": "XU",
}
BBG_SUFFIX: dict[str, str] = {"atm": "V", "rr25": "RR", "rr10": "RX", "bf25": "B", "bf10": "BX"}
#: The one exception to the ticker rule, uniform across every pair: the 1M
#: 25-delta risk reversal is ``<prefix>VRR``, a legacy ticker, not
#: ``<prefix>RR1M``.  A table rather than an ``if``, because a rule that is
#: right 32 times out of 33 is the kind of thing that gets "tidied" later;
#: a test pins it.
BBG_LEGACY_TICKERS: dict[tuple[str, str], str] = {("rr25", "1M"): "VRR"}
BBG_SLOT = "Slot46"
BBG_PRECISION = 3


def bbg_ticker(pair: str, instrument: str, tenor: str) -> str:
    pair = pair.upper()
    base, term = pair[:3], pair[3:6]
    for ccy in (base, term):
        if ccy not in BBG_CODES:
            raise PublishError(f"{pair}: no Bloomberg code is known for {ccy}; add it to "
                               f"publish.BBG_CODES before the pair can be contributed")
    prefix = BBG_CODES[base] + BBG_CODES[term]
    t = canonical_tenor(tenor)
    t = "1D" if t == OVERNIGHT else t
    legacy = BBG_LEGACY_TICKERS.get((instrument, t))
    if legacy:
        return prefix + legacy
    return prefix + BBG_SUFFIX[instrument] + t


def bbg_formula(cell: str, side: str, ticker_cell: str) -> str:
    """The DCAP cell, exactly as the desk's sheet has it.

    ``ticker_cell`` is the cell the ticker sits in (``O3``), which is how the
    desk's sheet writes it -- the ticker is a cell, not a literal -- so the
    strings compare byte for byte with the sheet's.
    """
    return (f'=_xll.PLContribFull({cell}*100,"{side}",{ticker_cell},"{BBG_SLOT}","TICKER",'
            f'{BBG_PRECISION},,"Valid")')


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExportFile:
    """One file a channel writes: its name, its bytes and what it carries.

    A channel writes one of these, or -- Murex -- two.  They are written
    together and logged one row each, because the log's row has always been
    "a file was written" and a Murex run writes two.
    """

    name: str
    body: bytes
    what: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def summary(self) -> dict:
        return {"name": self.name, "bytes": len(self.body), "sha256": self.sha256,
                "what": self.what}


@dataclass
class Build:
    """One export, built and checked, before it is sent or written."""

    channel: Channel
    source: str
    quotes: list[PillarQuote]
    preflight: dict
    refused: list[str]
    files: list[ExportFile] = field(default_factory=list)
    feeds: dict[str, "kace.Feed"] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    overlay: overlay_mod.Overlay | None = None
    file_date: date | None = None
    tier: str = ""
    multiplier: float = 1.0

    @property
    def ok(self) -> bool:
        return not self.refused

    def file(self, name: str | None = None) -> ExportFile:
        """One of the files this build wrote, by name.

        With no name, the channel must write exactly one: a caller that says
        "the file" of a two-file destination is asking an ambiguous question,
        and the answer names both rather than picking the first.
        """
        if name:
            for f in self.files:
                if f.name == name:
                    return f
            raise PublishError(f"{self.channel.label} does not write {name!r}; it writes "
                               + (", ".join(f.name for f in self.files) or "no file"))
        if len(self.files) != 1:
            raise PublishError(f"{self.channel.label} writes "
                               + (f"{len(self.files)} files ("
                                  + ", ".join(f.name for f in self.files)
                                  + "); name the one wanted" if self.files else "no file"))
        return self.files[0]

    @property
    def file_name(self) -> str:
        return self.file().name if self.files else ""

    @property
    def file_bytes(self) -> bytes:
        return self.file().body if self.files else b""

    @property
    def sha256(self) -> str:
        return self.file().sha256 if self.files else ""

    def summary(self) -> dict:
        out = {"channel": self.channel.key, "label": self.channel.label,
               "kind": self.channel.kind, "source": self.source,
               "ok": self.ok, "refused": list(self.refused),
               "quotes": [q.to_dict() for q in self.quotes],
               "pairs": sorted({q.pair for q in self.quotes}, key=lambda p: p),
               "rows": len(self.quotes), "preflight": self.preflight,
               "files": [f.summary() for f in self.files],
               "bytes": sum(len(f.body) for f in self.files), "notes": list(self.notes),
               "tier": self.tier, "multiplier": self.multiplier,
               "file_date": self.file_date.isoformat() if self.file_date else None,
               "overlay": self.overlay.record() if self.overlay else None}
        if self.feeds:
            out["feeds"] = {p: f.summary() for p, f in self.feeds.items()}
        return out


def _tenors_for(entry: PairEntry, tenors: list[str]) -> list[str]:
    if not entry.last_tenor:
        return list(tenors)
    cap = pillar_years(entry.last_tenor)
    return [t for t in tenors if pillar_years(t) <= cap + 1e-12]


def _book_read(book, pair: str, tenors: list[str], *, cut: str, source: str,
               method: str | None, where: str) -> tuple[kace.PillarRead | None, list[str]]:
    """Read what the book *can* mark of these tenors; the rest are named.

    Reads only the tenors the sheet quotes (O/N always, it is a curve point),
    so one unquoted tenor does not refuse the whole pair: coverage is reported
    per tenor and the overlay may fill the gap.
    """
    if pair not in book:
        return None, list(tenors)
    surface = book[pair]
    quoted = {canonical_tenor(m.tenor) for m in surface.quoted_marks()}
    if not quoted:
        return None, list(tenors)
    last = book.calendars.expiry_date(pair, max(quoted, key=pillar_years), book.clock.now.date())
    can: list[str] = []
    cannot: list[str] = []
    for t in tenors:
        if t == OVERNIGHT:
            can.append(t)
            continue
        if source == "marks" and t not in quoted:
            cannot.append(t)
            continue
        if book.calendars.expiry_date(pair, t, book.clock.now.date()) > last:
            cannot.append(t)
            continue
        can.append(t)
    if not [t for t in can if t != OVERNIGHT]:
        return None, list(tenors)
    read = kace.read_pillars(book, pair, can, cut=cut, source=source,
                             method=method or surface.method, where=where)
    return read, cannot


def pair_sources(entries, source: str, overlay: overlay_mod.Overlay | None,
                 sources: dict | None) -> dict[str, str]:
    """Which source each pair is read from: the choice per pair, else the default.

    ``sources`` is ``{pair: "book" | "overlay"}`` -- the screen's per-pair
    toggles -- and ``source`` is the default for a pair it does not name.
    A pair told to read the overlay that the overlay has no rows for is not
    an error: every tenor falls through to the book and the coverage says so.
    """
    chosen: dict[str, str] = {}
    for e in entries:
        want = str((sources or {}).get(e.pair) or source or "book").strip().lower()
        if want == "marks":
            want = "book"
        if want not in SOURCES:
            raise PublishError(f"{e.pair}: unknown source {want!r}; expected one of {SOURCES}")
        if want == "overlay" and overlay is None:
            raise PublishError(f"{e.pair} is to be read from the overlay, and no overlay "
                               f"file is loaded")
        chosen[e.pair] = want
    return chosen


def build(channel_name: str, book, tables: ExportTables, *, source: str = "book",
          overlay: overlay_mod.Overlay | None = None, pairs=None, tier: str | None = None,
          multiplier=None, wings: str = "marks", cut: str = "NY", methods=None,
          tolerance=None, pillars_only: bool = False, interpolate: bool = False,
          file_date: date | None = None, sources: dict | None = None) -> Build:
    """Build one channel's export and its preflight.  Nothing leaves here.

    ``pairs`` narrows the channel's list (the screen's picker); ``sources``
    says per pair whether it is read from the book or the overlay, ``source``
    being the default for the rest -- so a run is not all from one or all
    from the other, and the coverage names which pair came from where.
    ``tolerance`` is the largest move, in vol points, an overlay may make
    against a book value before the run is refused rather than warned about.
    ``file_date`` is the date a Murex or COS file name carries when it is not
    the book's valuation date -- the person's decision, after the preflight
    said the two disagree.
    """
    ch = channel(channel_name)
    if source == "marks":
        source = "book"
    if source not in SOURCES:
        raise PublishError(f"unknown source {source!r}; expected one of {SOURCES}")
    if source == "overlay" and overlay is None:
        raise PublishError("source is overlay, and no overlay file is loaded")
    if wings not in kace.SOURCES:
        raise PublishError(f"unknown wing source {wings!r}; expected one of {kace.SOURCES}")
    methods = {str(k).upper(): str(v) for k, v in (methods or {}).items() if str(v or "").strip()}
    today = book.clock.now.date()
    tenors = ch.tenor_list(tables)
    entries = tables.pairs_for(ch.key, book)
    chosen_tier = ""
    if ch.width == "tier":
        if tables.spreads is None:
            raise PublishError(tables.errors.get("SPREADS") or configsheets.missing(
                kace.SPREADS_SHEET, tables.path))
        chosen_tier = tables.spreads.resolve_tier(tier or ch.default_tier)
    factor = kace.spread_multiplier(multiplier)
    if pairs:
        wanted = {str(p).strip().upper() for p in pairs if str(p or "").strip()}
        unknown = sorted(wanted - {e.pair for e in entries})
        if unknown:
            raise PublishError(f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} "
                               f"not on the {ch.label} pair list (EXPORT_PAIRS); it publishes "
                               f"{', '.join(e.pair for e in entries)}")
        entries = [e for e in entries if e.pair in wanted]
    chosen = pair_sources(entries, source, overlay, sources)
    # The overlay stays in scope for the bookkeeping -- its rows for a pair
    # that reads the book are counted, not silently dropped -- and is the
    # build's source only where a pair actually reads it.
    any_overlay = overlay is not None and any(v == "overlay" for v in chosen.values())

    quotes: list[PillarQuote] = []
    refused: list[str] = []
    notes: list[str] = []
    coverage: list[dict] = []
    diffs: list[dict] = []
    from_book = from_overlay = fell_through = 0
    used_keys: set[tuple[str, str]] = set()
    feeds: dict[str, kace.Feed] = {}
    where = f"the {ch.label} tenor list"

    for entry in entries:
        want = _tenors_for(entry, tenors)
        read: kace.PillarRead | None = None
        cannot: list[str] = list(want)
        try:
            read, cannot = _book_read(book, entry.feed_from, want, cut=cut, source=wings,
                                      method=methods.get(entry.feed_from), where=where)
        except (KaceError, ValueError, KeyError) as exc:
            notes.append(f"{entry.pair}: the book could not be read ({exc}); every tenor "
                         f"falls to the overlay")
            read, cannot = None, list(want)
        pair_missing: list[str] = []
        row_src: dict[str, str] = {}
        use_overlay = overlay is not None and chosen[entry.pair] == "overlay"
        for t in want:
            # Looked up by the *published* pair: an overlay row is in the
            # channel's quotation, whatever curve the book reads it from --
            # and only for a pair that is read from the overlay.
            ov = overlay.get(entry.pair, t) if use_overlay else None
            have_book = read is not None and t in read.atm
            base: dict[str, float] = {}
            origin = ""
            if have_book:
                base = {"atm": read.atm[t], "rr25": read.wings[t][0], "rr10": read.wings[t][1],
                        "bf25": read.wings[t][2], "bf10": read.wings[t][3]}
                origin = read.origin[t]
            vals = dict(base)
            src = "book"
            given = (None, None)
            if ov is not None:
                used_keys.add((ov.pair, ov.tenor))
                fields_from_file = [f for f in overlay_mod.FIELDS if f in ov.values]
                if ov.two_way:
                    given = (ov.values["atm_bid"], ov.values["atm_ask"])
                for f in fields_from_file:
                    vals[f] = ov.values[f]
                if fields_from_file:
                    src = "overlay"
                    origin = ("overlay two-way" if ov.two_way else "overlay")
                    if len(fields_from_file) < len(overlay_mod.FIELDS) and have_book:
                        fell = [f for f in overlay_mod.FIELDS if f not in fields_from_file]
                        origin += f" ({', '.join(fell)} from the book: {read.origin[t]})"
                        fell_through += 1
                    if have_book:
                        for f in fields_from_file:
                            d = ov.values[f] - base[f]
                            if abs(d) > 1e-12:
                                diffs.append({"pair": entry.pair, "tenor": t, "field": f,
                                              "book": base[f], "overlay": ov.values[f],
                                              "move": d})
                elif have_book:
                    fell_through += 1
                    origin = f"{read.origin[t]} (overlay row blank)"
            missing = [f for f in ch.needs if f not in vals]
            if missing:
                pair_missing.append(t)
                row_src[t] = "missing"
                continue
            # What the channel does not need is carried when known and left
            # out when not; never made up.
            for f in overlay_mod.FIELDS:
                vals.setdefault(f, float("nan"))
            if src == "book":
                from_book += 1
            else:
                from_overlay += 1
            row_src[t] = src
            q = PillarQuote(
                pair=entry.pair, tenor=t, atm=vals["atm"], rr25=vals["rr25"], rr10=vals["rr10"],
                bf25=vals["bf25"], bf10=vals["bf10"], origin=origin, source=src,
                expiry=(read.expiries.get(t) if read is not None else None)
                or _expiry(book, entry.pair, t, today),
                feed_from=entry.feed_from if entry.feed_from != entry.pair else "",
                given_bid=given[0], given_ask=given[1])
            if entry.inverted and src == "book":
                q.rr25, q.rr10, q.flipped = -q.rr25, -q.rr10, True
                q.notes.append(f"read off {entry.feed_from} and inverted to {entry.pair}: "
                               f"the risk reversals change sign, the ATM and flies do not")
            elif entry.feed_from != entry.pair and src == "book":
                q.notes.append(f"fed from the {entry.feed_from} curve")
            # The shade, then the width around the shaded mid.
            q.shade, q.shade_from = tables.shade(ch.key, entry.pair)
            if q.given_bid is not None:
                q.width_from = "the overlay's own two-way"
            elif ch.width == "tier":
                q.width, q.width_from = tables.tier_width(chosen_tier, t, multiplier=factor)
            elif ch.width == "market":
                try:
                    q.width, q.width_from = tables.market_width(entry.pair, t, multiplier=factor)
                except PublishError as exc:
                    refused.append(f"{entry.pair} {t}: {exc}")
                    row_src[t] = "no width"
                    continue
            else:
                q.width, q.width_from = 0.0, "bid equals ask"
            if ch.wings_two_way:
                try:
                    q.wing_widths, q.wing_widths_from = tables.wing_width(entry.pair, t)
                except PublishError as exc:
                    refused.append(f"{entry.pair} {t}: {exc}")
                    row_src[t] = "no width"
                    continue
            if q.bid <= 0:
                refused.append(f"{entry.pair} {t}: the ATM bid is {q.bid:.4f} -- the width "
                               f"is wider than twice the volatility")
                continue
            quotes.append(q)
        if pair_missing:
            refused.append(f"{entry.pair}: no {', '.join(pair_missing)} "
                           + ("in the book or the overlay" if overlay is not None
                              else "in the book")
                           + (f" (the book has no {entry.feed_from})"
                              if entry.feed_from not in book else ""))
        coverage.append({"pair": entry.pair, "label": entry.label,
                         "feed_from": entry.feed_from if entry.feed_from != entry.pair else "",
                         # Which source the pair was told to read, and which
                         # its rows actually came from -- a pair sent to the
                         # overlay with no rows there is a book pair in fact.
                         "source": chosen[entry.pair],
                         "from": sorted({v for v in row_src.values()
                                         if v in ("book", "overlay")}),
                         "tenors": {t: row_src.get(t, "missing") for t in want},
                         "missing": pair_missing,
                         "in_book": entry.feed_from in book,
                         "in_overlay": (overlay is not None
                                        and any(k[0] == entry.pair for k in overlay.rows))})

    # Rows the overlay carries that this channel does not publish, and rows
    # for a pair the run reads from the book instead.
    ignored: list[str] = []
    left_on_book: list[str] = []
    if overlay is not None:
        published = {(e.pair, t) for e in entries for t in _tenors_for(e, tenors)}
        book_pairs = {e.pair for e in entries if chosen[e.pair] == "book"}
        for key in overlay.rows:
            if key in used_keys:
                continue
            if key[0] in book_pairs and key in published:
                left_on_book.append(f"{key[0]} {key[1]}")
            elif key not in published:
                ignored.append(f"{key[0]} {key[1]}")
    diffs.sort(key=lambda d: -abs(d["move"]))
    tol = None
    if tolerance not in (None, ""):
        try:
            tol = float(tolerance)
        except (TypeError, ValueError):
            raise PublishError(f"the tolerance {tolerance!r} is not a number") from None
        if tol < 0:
            raise PublishError("the tolerance cannot be negative")
        over = [d for d in diffs if abs(d["move"]) > tol]
        if over:
            refused.append(f"the overlay moves {len(over)} value(s) by more than the "
                           f"{tol:g} vol point tolerance, the largest {over[0]['pair']} "
                           f"{over[0]['tenor']} {over[0]['field']} by {over[0]['move']:+.4f}")

    # The file date: the book's, unless told otherwise, and never silently
    # different from the machine's.
    stamp = file_date or today
    machine = datetime.now().date()
    date_note = {"file": stamp.isoformat(), "book": today.isoformat(),
                 "machine": machine.isoformat(), "agree": stamp == machine,
                 "overridden": file_date is not None and file_date != today}
    if ch.kind == "file" and stamp != machine:
        notes.append(f"the file is dated {stamp:%Y%m%d} (the book's valuation date) and "
                     f"this machine says {machine:%Y-%m-%d}; the run asks before writing")

    outside = 0
    if overlay is not None and any_overlay:
        outside = overlay_mod.overflow_report(overlay, book)["outside"]

    b = Build(channel=ch, source="overlay" if any_overlay else "book", quotes=quotes,
              refused=refused,
              preflight={"coverage": coverage,
                         "wanted": {"pairs": len(entries), "tenors": tenors,
                                    "rows": sum(len(_tenors_for(e, tenors)) for e in entries)},
                         "from_book": from_book, "from_overlay": from_overlay,
                         "fell_through": fell_through,
                         "ignored": ignored, "left_on_book": left_on_book,
                         "sources": {"book": sorted(p for p, v in chosen.items() if v == "book"),
                                     "overlay": sorted(p for p, v in chosen.items()
                                                       if v == "overlay")},
                         "diffs": diffs[:200], "tolerance": tol,
                         "outside": outside, "date": date_note,
                         "widths": {"source": ch.width, "tier": chosen_tier,
                                    "multiplier": factor}},
              notes=notes, overlay=overlay, file_date=stamp, tier=chosen_tier,
              multiplier=factor)
    b.overlay = overlay if any_overlay else None
    if refused:
        return b
    try:
        if ch.kind == "file":
            b.files = WRITERS[ch.key](b)
        else:
            feeds_notes: list[str] = []
            b.feeds = _kace_feeds(b, book, tables, cut=cut, wings=wings, methods=methods,
                                  pillars_only=pillars_only, interpolate=interpolate,
                                  notes=feeds_notes)
            b.notes.extend(feeds_notes)
    except (KaceError, PublishError) as exc:
        b.refused.append(str(exc))
    return b


def _expiry(book, pair: str, tenor: str, today: date) -> date | None:
    try:
        return book.calendars.expiry_date(pair, kace.calendar_tenor(tenor), today)
    except Exception:  # noqa: BLE001 - a pair the calendars cannot place has no expiry
        return None


def _kace_feeds(b: Build, book, tables: ExportTables, *, cut: str, wings: str, methods: dict,
                pillars_only: bool, interpolate: bool, notes: list[str]) -> dict[str, kace.Feed]:
    """One ``Feed`` per pair for the kACE channel.

    A pair read whole off the book goes through ``kace.build`` so the daily
    series is what the feed tab would post; a pair the overlay touched, or
    one the book does not hold, is pillars only -- an overlay row is a pillar
    quote, not a curve, and a daily series interpolated out of eleven numbers
    would be fiction -- and the notes say so.
    """
    feeds: dict[str, kace.Feed] = {}
    by_pair: dict[str, list[PillarQuote]] = {}
    for q in b.quotes:
        by_pair.setdefault(q.pair, []).append(q)
    today = book.clock.now.date()
    for pair, qs in by_pair.items():
        whole = all(q.source == "book" for q in qs) and pair in book and not any(
            q.feed_from for q in qs)
        shade = qs[0].shade
        if whole:
            feed = kace.build(book, pair, tables.spreads, tier=b.tier, cut=cut, source=wings,
                              method=methods.get(pair, book[pair].method),
                              multiplier=b.multiplier, interpolate=interpolate,
                              pillars_only=pillars_only)
            if shade:
                feed.daily = {d: v + shade for d, v in feed.daily.items()}
                feed.pillars = [kace.Pillar(tenor=p.tenor, expiry=p.expiry, spread=p.spread,
                                            atm=p.atm + shade, rr25=p.rr25, rr10=p.rr10,
                                            fly25=p.fly25, fly10=p.fly10, wings=p.wings)
                                for p in feed.pillars]
                feed.notes.append(f"the ATM mid is shaded by {shade:+g} vol points "
                                  f"({qs[0].shade_from})")
        else:
            pillars = []
            for q in sorted(qs, key=lambda q: pillar_years(q.tenor)):
                expiry = q.expiry or _expiry(book, pair, q.tenor, today)
                if expiry is None:
                    raise PublishError(f"{pair} {q.tenor}: no expiry date can be built for "
                                       f"the pair, so no kACE node can carry it")
                pillars.append(kace.Pillar(tenor=q.tenor, expiry=expiry, spread=q.ask - q.bid,
                                           atm=q.mid, rr25=q.rr25, rr10=q.rr10,
                                           fly25=q.bf25, fly10=q.bf10, wings=q.origin))
            feed = kace.Feed(pair=pair, hor_date=today, cut=cut.upper(), source=wings,
                             daily={}, pillars=pillars, tier=b.tier, multiplier=b.multiplier,
                             interpolate=False, pillars_only=True,
                             notes=[f"{pair}: built from the overlay, so the pillars alone "
                                    f"are posted -- an overlay row is a pillar quote, not a "
                                    f"curve, and no calendar-day series is made out of it"])
            if not pillars_only:
                notes.append(f"{pair} went as key tenors only: it is "
                             + ("not in the book" if pair not in book else "overlaid")
                             + ", and a daily series cannot be built from pillar quotes")
        feeds[pair] = feed
    return feeds


# ---------------------------------------------------------------------------
# writers: bytes for the file channels
# ---------------------------------------------------------------------------
def _sorted(b: Build) -> list[PillarQuote]:
    order = {c["pair"]: i for i, c in enumerate(b.preflight["coverage"])}
    return sorted(b.quotes, key=lambda q: (order.get(q.pair, 999), pillar_years(q.tenor)))


def _labels(b: Build) -> dict[str, str]:
    return {c["pair"]: c["label"] for c in b.preflight["coverage"]}


def murex_tenor(tenor: str) -> str:
    return "O/N" if canonical_tenor(tenor) == OVERNIGHT else canonical_tenor(tenor)


def _provenance(b: Build) -> str:
    src = "book marks" if b.overlay is None else (
        f"overlay {b.overlay.name} sha256 {b.overlay.sha256[:16]} ({len(b.overlay.rows)} rows)")
    return f"volkit {b.channel.label} export · {b.file_date:%Y-%m-%d} · source: {src}"


def _xls(sheet_name: str, header: list[str], rows: list[list], precision: int) -> bytes:
    """A BIFF8 workbook with one sheet, through xlwt (pure Python)."""
    try:
        import xlwt
    except ImportError:
        raise PublishError("writing a Murex .xls file needs the xlwt package (pip install "
                           "xlwt); it is pure Python and in requirements.txt") from None
    wb = xlwt.Workbook()
    ws = wb.add_sheet(sheet_name)
    for c, h in enumerate(header):
        ws.write(0, c, h)
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row):
            ws.write(r, c, round(v, precision) if isinstance(v, float) else v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_murex(b: Build) -> list[ExportFile]:
    """Both Murex files off one build: the ATM in one, the wings in the other.

    They were two channels and are one destination.  The pair list, the
    source choice, the preflight and the date are shared -- a Murex load is
    the pair of files, and a desk that wrote one of them yesterday and the
    other today has a surface that disagrees with itself -- while the files
    themselves are byte for byte what the loader has always been handed.
    """
    labels = _labels(b)
    quotes = _sorted(b)
    vol = [[labels[q.pair], murex_tenor(q.tenor), float(q.bid), float(q.ask)] for q in quotes]
    broker = []
    for q in quotes:
        for ordinate, bf, rr in ((10, q.bf10, q.rr10), (25, q.bf25, q.rr25)):
            broker.append([labels[q.pair], murex_tenor(q.tenor), ordinate,
                           float(bf), float(bf), float(rr), float(rr)])
    return [
        ExportFile(f"DRV_MktData_FX_Vol_{b.file_date:%Y%m%d}.xls",
                   _xls("Sheet1&TODAY", ["ccy pair", "Maturity", "bid", "ask"], vol,
                        b.channel.precision),
                   what="the ATM, bid equal to ask"),
        ExportFile(f"DRV_MktData_FX_Broker_{b.file_date:%Y%m%d}.xls",
                   _xls("Sheet1&TODAY",
                        ["ccy pair", "Maturity", "Ordinate", "fxbflyBid", "fxbflyAsk",
                         "fxbrrBid", "fxbrrAsk"], broker, b.channel.precision),
                   what="the 10 and 25 delta butterfly and risk reversal"),
    ]


COS_FILENAME = "COS_86830_Bid.csv"


def write_cos(b: Build) -> list[ExportFile]:
    labels = _labels(b)
    by_pair: dict[str, dict[str, PillarQuote]] = {}
    for q in _sorted(b):
        by_pair.setdefault(q.pair, {})[q.tenor] = q
    lines = ["CUR PAIR," + ",".join(COS_TENORS)]
    for c in b.preflight["coverage"]:
        qs = by_pair.get(c["pair"])
        if not qs:
            continue
        cells = [f"{qs[t].bid:.{b.channel.precision}f}" for t in COS_TENORS if t in qs]
        lines.append(labels[c["pair"]] + "," + ",".join(cells))
    return [ExportFile(COS_FILENAME, ("\n".join(lines) + "\n").encode("utf-8"),
                       what="the ATM bid alone, five tenors")]


#: The output sheet's shape, exactly as the desk's ``BCFO Vols bbg output.xlsx``
#: has it (its ``G7 Output`` / ``G7 Cross Output`` / ``EM PM Output`` tabs, here
#: one tab): a block per pair -- the pair on its own row, a header row, then a
#: row per tenor -- two blank rows between blocks.  In a tenor row: the bid
#: values in B..F as decimals, the ask values in I..M, and for each instrument
#: a ticker cell followed by the BID and ASK ``PLContribFull`` cells that read
#: the value cells (``B3*100``).  The DCAP add-in reads the formulas, so the
#: layout is what the desk is used to opening rather than a rule of the
#: add-in's; it is kept because a sheet that looks like the old one is a sheet
#: the desk can check by eye against the old one.
BBG_SHEET = "Output"
BBG_TENOR_LABEL = {OVERNIGHT: "ON"}
#: (ticker column, bid column, ask column) per instrument, 1-based.
BBG_COLUMNS = {"atm": (15, 16, 17), "rr25": (18, 19, 20), "rr10": (21, 22, 23),
               "bf25": (24, 25, 26), "bf10": (27, 28, 29)}
BBG_HEADER = {"atm": "ATM", "rr25": "RR25", "rr10": "RR10", "bf25": "Fly25", "bf10": "Fly10"}
#: Where each instrument's bid and ask *values* sit, as the formulas read them.
BBG_VALUE_COLUMNS = {"atm": ("B", "I"), "rr25": ("C", "J"), "rr10": ("D", "K"),
                     "bf25": ("E", "L"), "bf10": ("F", "M")}


def bbg_rows(b: Build) -> list[dict]:
    """The sheet as rows: what ``write_bloomberg`` writes and a test reads.

    One entry per cell row of the output tab, in order -- ``{"kind":
    "pair"|"header"|"tenor"|"blank", ...}`` -- with a tenor row carrying the
    published pair, the tenor label, the five ``(bid, ask)`` pairs in vol
    points and the five tickers.
    """
    rows: list[dict] = []
    by_pair: dict[str, list[PillarQuote]] = {}
    for q in _sorted(b):
        by_pair.setdefault(q.pair, []).append(q)
    for c in b.preflight["coverage"]:
        qs = by_pair.get(c["pair"])
        if not qs:
            continue
        rows.append({"kind": "pair", "pair": c["pair"]})
        rows.append({"kind": "header"})
        for q in qs:
            rows.append({"kind": "tenor", "pair": q.pair,
                         "tenor": BBG_TENOR_LABEL.get(q.tenor, q.tenor),
                         "sides": {i: q.side(i) for i in INSTRUMENTS},
                         "tickers": {i: bbg_ticker(q.pair, i, q.tenor) for i in INSTRUMENTS}})
        rows.append({"kind": "blank"})
        rows.append({"kind": "blank"})
    while rows and rows[-1]["kind"] == "blank":
        rows.pop()
    return rows


def write_bloomberg(b: Build) -> list[ExportFile]:
    """The DCAP workbook, in the desk's own layout (``bbg_rows``).

    The values are decimals (0.0584 for 5.84 vol), because the formula the
    desk's sheet carries is ``PLContribFull(<cell>*100, ...)`` and that is
    kept exactly; the provenance goes into the workbook's properties and a
    second tab, so the output tab is nothing but the blocks.
    """
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = BBG_SHEET
    r = 1
    for row in bbg_rows(b):
        if row["kind"] == "pair":
            ws.cell(row=r, column=1, value=row["pair"])
            for inst, (tc, bc, ac) in BBG_COLUMNS.items():
                ws.cell(row=r, column=tc, value="Bloomberg")
                ws.cell(row=r, column=bc, value="DCAP Formula")
        elif row["kind"] == "header":
            for col, label in zip("ABCDEF", ("Bid", "ATM Vol", "RR25", "RR10", "Fly25", "Fly10")):
                ws[f"{col}{r}"] = label
            for col, label in zip("HIJKLM", ("Ask", "ATM Vol", "RR25", "RR10", "Fly25", "Fly10")):
                ws[f"{col}{r}"] = label
            for inst, (tc, bc, ac) in BBG_COLUMNS.items():
                ws.cell(row=r, column=tc, value=BBG_HEADER[inst])
                ws.cell(row=r, column=bc, value="Bid")
                ws.cell(row=r, column=ac, value="Ask")
        elif row["kind"] == "tenor":
            ws[f"A{r}"] = row["tenor"]
            ws[f"H{r}"] = row["tenor"]
            for inst in INSTRUMENTS:
                bid, ask = row["sides"][inst]
                bcol, acol = BBG_VALUE_COLUMNS[inst]
                ws[f"{bcol}{r}"] = round(bid / 100.0, 10)
                ws[f"{acol}{r}"] = round(ask / 100.0, 10)
                tc, fb, fa = BBG_COLUMNS[inst]
                tcell = ws.cell(row=r, column=tc, value=row["tickers"][inst])
                ws.cell(row=r, column=fb, value=bbg_formula(f"{bcol}{r}", "BID", tcell.coordinate))
                ws.cell(row=r, column=fa, value=bbg_formula(f"{acol}{r}", "ASK", tcell.coordinate))
        r += 1
    about = wb.create_sheet("volkit")
    about["A1"] = _provenance(b)
    about["A2"] = ("Values are decimals; every DCAP cell reads its value cell times 100. "
                   "The ATM two-way is MARKET_WIDTHS + ADD_UPS about the SHADES-shifted mid; "
                   "the wings are two-way by WING_WIDTHS about the mark.")
    buf = io.BytesIO()
    wb.save(buf)
    return [ExportFile(f"BCFO_Vols_bbg_{b.file_date:%Y%m%d}.xlsx", buf.getvalue(),
                       what="a DCAP contribution block per pair")]


def bloomberg_cells(b: Build) -> list[dict]:
    """The formula cells the sheet would carry, for a test or a preview."""
    out = []
    for q in _sorted(b):
        for inst in INSTRUMENTS:
            ticker = bbg_ticker(q.pair, inst, q.tenor)
            bid, ask = q.side(inst)
            out.append({"pair": q.pair, "tenor": q.tenor, "instrument": inst, "ticker": ticker,
                        "bid": bid, "ask": ask})
    return out


WRITERS = {"bloomberg": write_bloomberg, "murex": write_murex, "cos": write_cos}


# ---------------------------------------------------------------------------
# book against overlay
# ---------------------------------------------------------------------------
def compare(channel_name: str, book, tables: ExportTables, overlay: overlay_mod.Overlay, *,
            pairs=None, tier: str | None = None, multiplier=None, wings: str = "marks",
            cut: str = "NY", methods=None) -> dict:
    """The book against the overlay, mids and both sides, pair by pair and tenor by tenor.

    Two builds of the same channel -- every pair from the book, every pair
    from the overlay -- joined on pair and tenor, so the two-ways compared
    are the two-ways the channel would actually publish: the same tier or
    market width, the same shade, the same wing widths on both.  Only the
    pairs the overlay carries are compared; a pair or tenor one side cannot
    supply is listed, not silently dropped.
    """
    ch = channel(channel_name)
    entries = tables.pairs_for(ch.key, book)
    in_overlay = sorted({k[0] for k in overlay.rows})
    wanted = {e.pair for e in entries if e.pair in in_overlay}
    if pairs:
        wanted &= {str(p).strip().upper() for p in pairs}
    if not wanted:
        raise PublishError(f"the overlay carries none of the {ch.label} pairs"
                           + (f" (it has {', '.join(in_overlay)})" if in_overlay else ""))
    common = dict(tier=tier, multiplier=multiplier, wings=wings, cut=cut, methods=methods,
                  pairs=sorted(wanted))
    a = build(ch.key, book, tables, source="book", **common)
    b = build(ch.key, book, tables, source="overlay", overlay=overlay, **common)
    qa = {(q.pair, q.tenor): q for q in a.quotes}
    qb = {(q.pair, q.tenor): q for q in b.quotes}
    rows: list[dict] = []
    for key in sorted(set(qa) | set(qb), key=lambda k: (k[0], pillar_years(k[1]))):
        x, y = qa.get(key), qb.get(key)
        row = {"pair": key[0], "tenor": key[1],
               "book": x.to_dict() if x else None, "overlay": y.to_dict() if y else None,
               "diff": {}}
        if x and y:
            def d(a, b):
                return (b - a) if (a == a and b == b) else None     # NaN: not carried
            row["diff"] = {"mid": y.mid - x.mid, "bid": y.bid - x.bid, "ask": y.ask - x.ask,
                           **{i: d(x.value(i), y.value(i)) for i in WING_INSTRUMENTS},
                           **{f"{i}_bid": d(x.side(i)[0], y.side(i)[0])
                              for i in WING_INSTRUMENTS},
                           **{f"{i}_ask": d(x.side(i)[1], y.side(i)[1])
                              for i in WING_INSTRUMENTS}}
            row["origin"] = y.origin
            row["same"] = all(v is None or abs(v) < 1e-9 for v in row["diff"].values())
        rows.append(row)
    by_pair: dict[str, dict] = {}
    for row in rows:
        entry = by_pair.setdefault(row["pair"], {"pair": row["pair"], "tenors": 0,
                                                 "compared": 0, "max_mid": 0.0, "max_side": 0.0,
                                                 "book_only": [], "overlay_only": []})
        entry["tenors"] += 1
        if row["diff"]:
            entry["compared"] += 1
            entry["max_mid"] = max(entry["max_mid"], abs(row["diff"]["mid"]))
            entry["max_side"] = max(entry["max_side"], abs(row["diff"]["bid"]),
                                    abs(row["diff"]["ask"]))
        elif row["book"]:
            entry["book_only"].append(row["tenor"])
        elif row["overlay"]:
            entry["overlay_only"].append(row["tenor"])
    return {"channel": ch.key, "label": ch.label, "pairs": sorted(wanted),
            "rows": rows, "by_pair": list(by_pair.values()),
            "book_refused": a.refused, "overlay_refused": b.refused,
            "overlay": overlay.record(),
            "widths": {"source": ch.width, "tier": a.tier, "multiplier": a.multiplier,
                       "wings_two_way": ch.wings_two_way}}


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
def export_dir(path: str | Path | None = None) -> Path:
    return Path(path) if path else app_dir() / EXPORT_DIR


def write_file(b: Build, directory: str | Path | None, *, log: kace.PostLog | None,
               when: datetime, dry_run: bool = False) -> list[dict]:
    """Write a file channel's files into the export folder and record them.

    One log entry per file, because the log's row has always been "a file was
    written"; a Murex run writes two and appears twice, each with its own
    name, size and hash, the channel and the source shared.  The folder is
    made once and every file written before anything is logged, so a run
    that cannot write its second file does not leave a logged first one
    claiming the destination is up to date.
    """
    if b.channel.kind != "file":
        raise PublishError(f"{b.channel.label} is not a file channel")
    if not b.ok:
        raise PublishError("the build was refused: " + "; ".join(b.refused))
    if not b.files:
        raise PublishError(f"{b.channel.label} produced no file")
    folder = export_dir(directory)
    shared = {"at": when.isoformat(timespec="seconds"), "channel": b.channel.key,
              "source": "marks" if b.overlay is None else b.overlay.record(
                  rows_outside_book=b.preflight.get("outside")),
              "pairs": sorted({q.pair for q in b.quotes}), "rows": len(b.quotes),
              "sources": b.preflight.get("sources"),
              "file_date": b.file_date.isoformat() if b.file_date else None,
              "tier": b.tier, "multiplier": b.multiplier,
              "shades": sorted({f"{q.pair} {q.shade:+g}" for q in b.quotes if q.shade}),
              "dry_run": bool(dry_run)}
    entries = []
    for f in b.files:
        target = folder / f.name
        entry = dict(shared, file=str(target), bytes=len(f.body), hash=f.sha256[:16],
                     sha256=f.sha256, what=f.what)
        if dry_run:
            entry.update({"ok": None, "message": f"dry run: {len(f.body)} bytes would be "
                                                 f"written to {target}"})
            entries.append(entry)
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.body)
            entry.update({"ok": True, "message": f"wrote {len(f.body)} bytes to {target}"})
        except OSError as exc:
            entry.update({"ok": False, "message": f"could not write {target}: {exc}"})
        entries.append(entry)
    if not dry_run and log is not None:
        for entry in entries:
            try:
                log.record(entry)
                entry["logged"] = log.path
            except OSError as exc:
                entry["logged"] = None
                entry["message"] += f" (and the log at {log.path} could not be written: {exc})"
    return entries


# ---------------------------------------------------------------------------
# seeding the tables
# ---------------------------------------------------------------------------
def seed_tables(present: dict[str, bool]) -> dict[str, list[dict]]:
    """Rows for the export tabs a workbook does not have yet.

    Seeded from the desk's own files as captured on 2026-09-10
    (``exportseed.py``): each channel's pair list in the file's order, the
    Bloomberg ATM and wing widths cell for cell, the −0.2 shade every
    Bloomberg ATM carried (CHFJPY included -- the design note's zero was not
    what the file did), and the add-ups that reproduce the sheet: 0 overnight
    and 0.2 otherwise for the dollar pairs and the HKD legs the G7 tab
    carried, 0 for the crosses.  The ``cos`` tier is not seeded: the COS
    file's widths are not a ladder anything here can derive, so that column
    of ``SPREADS`` stays hand-typed.
    """
    out: dict[str, list[dict]] = {}
    if not present.get("SHADES"):
        out["SHADES"] = [
            {"channel": "bloomberg", "pair": "", "shade": -0.2,
             "note": "every ATM two-way 0.2 under the marked mid, both sides (the 2026-09-10 "
                     "sheet: CHFJPY too)"},
            {"channel": "kace", "pair": "", "shade": 0.0, "note": ""},
            {"channel": "cos", "pair": "", "shade": 0.0, "note": ""},
        ]
    if not present.get("ADD_UPS"):
        out["ADD_UPS"] = [
            {"pair": "default", "overnight": 0.0, "other": 0.2,
             "note": "the G7 tab's corner table: 0.2 outside overnight"},
            {"pair": "crosses", "overnight": 0.0, "other": 0.0,
             "note": "the G7 Cross tab: the raw observed width"},
        ] + [{"pair": p, "overnight": 0.0, "other": 0.2,
              "note": "an HKD leg the G7 tab carried, so it takes the G7 add-up"}
             for p in exportseed.G7_TAB_CROSSES]
    if not present.get("MARKET_WIDTHS"):
        pairs = list(exportseed.BLOOMBERG_PAIRS)
        out["MARKET_WIDTHS"] = [
            {"tenor": t, **{p: exportseed.MARKET_WIDTHS[p][t]
                            for p in pairs if t in exportseed.MARKET_WIDTHS[p]}, "note": ""}
            for t in ELEVEN]
    if not present.get("WING_WIDTHS"):
        rows = []
        for p in exportseed.BLOOMBERG_PAIRS:
            for t in ELEVEN:
                w = exportseed.WING_WIDTHS[p].get(t)
                if w is None:
                    continue
                rows.append({"pair": p, "tenor": t, "rr25": w[0], "rr10": w[1],
                             "bf25": w[2], "bf10": w[3], "note": ""})
        out["WING_WIDTHS"] = rows
    if not present.get("EXPORT_PAIRS"):
        rows = []
        for p in exportseed.BLOOMBERG_PAIRS:
            rows.append({"channel": "bloomberg", "pair": p, "label": p, "feed_from": "",
                         "last_tenor": "1Y" if p == "XAUUSD" else "", "note": ""})
        for label in exportseed.MUREX_PAIRS:
            # One list for the destination: both files carry the same pairs in
            # the same order, which is why they are one channel.
            rows.append({"channel": "murex", "pair": label.replace("/", ""), "label": label,
                         "feed_from": "", "last_tenor": "", "note": ""})
        for label in exportseed.COS_PAIRS:
            pair = label.replace("/", "").replace("CNY", "CNH")
            rows.append({"channel": "cos", "pair": pair, "label": label, "feed_from": "",
                         "last_tenor": "",
                         "note": "CNY label, fed the CNH curve" if "CNY" in label else ""})
        out["EXPORT_PAIRS"] = rows
    return out
