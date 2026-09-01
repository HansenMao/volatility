"""Trades that printed: the public dissemination file, read without guessing.

A broker run says where a market was *shown*.  A dissemination file says where
business actually got **done** -- and those are different numbers, because a
trade prints at one side of somebody's market and the side it printed on is
the interesting part.  That is what this module is for.

The file is a CSV whose layout has changed at least once and will change
again, so nothing here reads a column by position.  Headers are matched by
meaning against a synonym table, the way ``history.py`` matches a workbook's
headers, and **a column that cannot be placed is reported with the header that
confused it** rather than skipped.  Both the layout in circulation before the
2022 reporting rewrite (``ROUNDED_NOTIONAL_AMOUNT_1``, ``OPTION_STRIKE_PRICE``)
and the CDE layout after it (``Notional amount-Leg 1``, ``Strike Price``) are
recognised, because a desk's archive spans the change.

Four things this file gets wrong if read naively, and what is done instead:

**A capped notional is not a notional.**  Sizes above the cap are published as
the cap, sometimes with a ``+`` on the end.  Read as a number, a 750 million
trade becomes a 250 million trade, and every size-conditioned width statistic
downstream is computed against a size that never traded.  A capped row is kept
with ``notional_capped`` set and its size is a *lower bound*; nothing in this
package may use it as an equality.

**A cancel is not a trade and a correction is not two.**  ``CANC``/``EROR``
withdraws a print and ``CORR``/``MODI``/``REVI`` replaces one, both naming the
original dissemination id.  Read as ordinary rows they are business that
happened twice, or business that was withdrawn and still counts.  Each is
emitted carrying the id it corrects, and :meth:`archive.Archive.resolve` ties
it to the record it supersedes -- or says it could not.

**A premium is not a volatility.**  Turning one into the other needs a
forward, a discount factor and a model, none of which belong in a file reader,
and all of which depend on marks that can be re-marked afterwards.  So the
economics are stored as published and the inversion happens in
``synthesis.py``, where the result can be labelled as derived and the inputs
that produced it can be named.  A vol computed here would arrive at every
later screen with no way to ask what forward it used.

**The pair is not always written the same way.**  ``EUR-USD``, ``EURUSD``,
``USD/JPY`` and a UPI's own spelling all appear.  They are normalised to the
six-letter form, and a row whose underlying cannot be read to a pair is
reported rather than filed under a guess -- filing an unknown pair under the
nearest match is how one pair's history acquires another pair's trades.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .archive import Observation, parse_time
from .paths import READ_ENCODING, open_text

#: What the publisher calls an action, and what it means here.  ``NEWT`` is a
#: new trade; the rest either withdraw or replace one already disseminated.
NEW_ACTIONS = ("NEWT", "NEW", "TRAD")
CANCEL_ACTIONS = ("CANC", "EROR", "ERROR", "TERM")
CORRECT_ACTIONS = ("CORR", "MODI", "REVI", "MODIFY")

#: Header synonyms, by meaning.  Matching is on the *squashed* header -- case,
#: spaces, underscores and hyphens removed -- so "Notional amount-Leg 1",
#: "NOTIONAL_AMOUNT_1" and "notional amount leg1" are one entry, not three.
_FIELDS: dict[str, tuple[str, ...]] = {
    "dissemination_id": ("disseminationid", "disseminationidentifier", "dissemid"),
    "original_id": ("originaldisseminationid", "originaldisseminationidentifier",
                    "origdisseminationid", "priordisseminationid"),
    "action": ("action", "actiontype", "eventtype", "actiontypeeventtype"),
    "executed": ("executiontimestamp", "eventtimestamp", "executiontime",
                 "originalexecutiontimestamp"),
    "expiry": ("optionexpirationdate", "expirationdate", "expirydate", "enddate",
               "optionexpiry", "maturitydate"),
    "effective": ("effectivedate", "startdate"),
    "strike": ("optionstrikeprice", "strikeprice", "strike", "strikepriceleg1"),
    "strike_pair": ("strikepricecurrencypair", "optioncurrency", "strikecurrency"),
    "option_type": ("optiontype", "putcallindicator", "optiontypeputcall"),
    "option_style": ("optionfamily", "optionstyle", "exercisestyle"),
    "premium": ("optionpremium", "optionpremiumamount", "premiumamount", "premium"),
    "premium_ccy": ("optionpremiumcurrency", "premiumcurrency"),
    "notional_1": ("roundednotionalamount1", "notionalamount", "notionalamountleg1",
                   "notionalamount1", "notionalquantityleg1"),
    "notional_2": ("roundednotionalamount2", "notionalamountleg2", "notionalamount2",
                   "notionalquantityleg2"),
    "ccy_1": ("notionalcurrency1", "notionalcurrencyleg1", "notionalcurrency"),
    "ccy_2": ("notionalcurrency2", "notionalcurrencyleg2"),
    "underlier": ("underlyingasset1", "underlierid", "underlieridleg1",
                  "underlyingassetname", "underlyingassetid", "underlier"),
    "underlier_2": ("underlyingasset2", "underlieridleg2"),
    "product": ("taxonomy", "uniqueproductidentifier", "upifisn", "upiunderliername",
                "productid", "assetclass"),
    "cap": ("notionalamountcapindicator", "notionalcapindicator", "capindicator",
            "roundednotionalamountcap"),
    "platform": ("platformidentifier", "executionvenue", "venue"),
    "cleared": ("cleared", "clearedindicator"),
}
_BY_SYNONYM = {syn: name for name, syns in _FIELDS.items() for syn in syns}

#: Headers that carry nothing this module reads and are *known* to carry
#: nothing, so they are passed over silently.  Anything not here and not in
#: the synonym table is reported: an unrecognised header is either a layout
#: this build has not met or a column somebody needs.
_IGNORED = frozenset((
    "collateralisation", "collateralization", "enduserexception", "blocktradeelection",
    "indicationofcollateralization", "indicationofenduserexception", "settlementcurrency",
    "daycountconvention", "priceformingcontinuationdata", "packageindicator",
    "packagetransactionprice", "packagetransactionspread", "mandatorilyclearableindicator",
    "embeddedoption", "postpricedindicator", "priceunitofmeasure", "quantityunitofmeasure",
    "amendmentindicator", "reportingjurisdiction", "counterparty1", "counterparty2",
))

# Both legs must stand alone.  Without the boundary look-arounds this finds
# "COM"+"MOD" inside COMMODITY and files a crude oil trade under the pair
# COMMOD -- which is not a hypothetical, it is what the first version did.
_PAIR = re.compile(r"(?<![A-Z])([A-Z]{3})\s*[-/_ ]?\s*([A-Z]{3})(?![A-Z])")
_CALL = ("CALL", "C", "CALLOPTION", "CALL OPTION")
_PUT = ("PUT", "PUTO", "P", "PUTOPTION", "PUT OPTION")


class SdrError(Exception):
    """A dissemination file that cannot be read at all."""


def _squash(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


@dataclass
class SdrRead:
    """What one file yielded, and everything it could not."""

    records: list[Observation] = field(default_factory=list)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)   # row, why, text
    notes: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)   # meaning -> the header used
    unplaced: list[str] = field(default_factory=list)
    rows_read: int = 0

    def summary(self) -> str:
        parts = [f"{self.rows_read} row(s) read", f"{len(self.records)} kept"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.unplaced:
            parts.append(f"{len(self.unplaced)} column(s) not understood")
        return ", ".join(parts)


def read_sdr(path, *, pairs=None, known_pairs=None, source: str = "sdr",
             origin: str = "") -> SdrRead:
    """Read a dissemination CSV into trade observations.

    ``known_pairs`` -- normally ``book.pairs()`` -- is what a row's underlying
    is checked against.  Given it, a row naming a pair the book does not build
    is skipped *by name*, which is what turns "the archive has 40,000 trades"
    into "the archive has 40,000 trades in pairs you actually mark".  Without
    it every readable pair is kept.

    ``pairs`` narrows further to an explicit list.
    """
    out = SdrRead()
    want = {p.upper() for p in pairs} if pairs else None
    known = {p.upper() for p in known_pairs} if known_pairs else None
    origin = origin or str(path)

    # A file straight off DTCC is a zip holding one CSV, and the same file
    # unpacked by hand is that CSV.  Both are read here rather than making the
    # caller know which it has: a desk that unzipped one file and not another
    # would otherwise have half a folder silently unread.
    if zipfile.is_zipfile(path):
        return _read_zip(path, out, want=want, known=known, source=source, origin=origin)
    try:
        handle = open_text(path, newline="")
    except (OSError, UnicodeDecodeError) as exc:
        # ``open_text`` reads and decodes before it hands back a handle, so a
        # file in no readable encoding fails here rather than mid-parse.
        raise SdrError(f"cannot open the dissemination file {path}: {exc}") from None
    with handle as fh:
        _read_handle(fh, out, want=want, known=known, source=source, origin=origin,
                     label=str(path))

    if not out.records and out.rows_read:
        out.notes.append(
            f"{out.rows_read} row(s) were read and none were kept; the commonest reason is a "
            f"file of a different asset class, or pairs this book does not build")
    return out


def _read_handle(fh, out: SdrRead, *, want, known, source: str, origin: str,
                 label: str) -> None:
    """One CSV, from a file on disk or from a member of a zip.

    ``label`` is what an error calls the thing, so a complaint about a zip
    names the member inside it rather than the archive -- "no header row" is
    a very different problem depending on which of the two it is about.
    """
    sample = fh.read(8192)
    fh.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel     # a one-column file sniffs as nothing; comma is right
    reader = csv.DictReader(fh, dialect=dialect)
    if not reader.fieldnames:
        raise SdrError(f"{label} has no header row; a dissemination file starts with one")

    col: dict[str, str] = {}
    for header in reader.fieldnames:
        key = _BY_SYNONYM.get(_squash(header))
        if key is None:
            if _squash(header) not in _IGNORED and _squash(header):
                if header not in out.unplaced:
                    out.unplaced.append(header)
            continue
        # First header wins.  Two headers meaning the same thing is a file
        # that has been joined to itself, and taking the last one silently
        # would prefer whichever copy happened to be on the right.
        col.setdefault(key, header)
    out.columns = dict(col)

    missing = [k for k in ("executed", "underlier", "strike") if k not in col]
    if "underlier" not in col and "product" in col:
        missing.remove("underlier")     # a UPI/taxonomy string carries the pair
    if missing:
        raise SdrError(
            f"{label} is missing the column(s) this reader needs: {', '.join(missing)}. "
            f"Headers found: {', '.join(reader.fieldnames[:12])}"
            + (" ..." if len(reader.fieldnames) > 12 else ""))
    if out.unplaced:
        note = (f"{len(out.unplaced)} column(s) were not understood and were not read: "
                f"{', '.join(out.unplaced[:8])}"
                + (" ..." if len(out.unplaced) > 8 else ""))
        if note not in out.notes:
            out.notes.append(note)

    for n, raw in enumerate(reader, start=2):
        out.rows_read += 1
        try:
            obs, why = _row(raw, col, want=want, known=known, source=source,
                            origin=origin, line=n)
        except (ValueError, TypeError) as exc:
            out.skipped.append((n, f"could not be read: {exc}", _brief(raw)))
            continue
        if obs is None:
            out.skipped.append((n, why, _brief(raw)))
            continue
        out.records.append(obs)


def _read_zip(path, out: SdrRead, *, want, known, source: str, origin: str) -> SdrRead:
    """Every CSV inside a zip, read into one result.

    Members are read in the order the archive lists them and each row keeps
    the member it came from in its provenance, so a file that turns out to
    hold two days can still be taken apart afterwards.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SdrError(f"cannot open the dissemination file {path}: {exc}") from None
    with archive:
        members = [n for n in archive.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not members:
            raise SdrError(
                f"{path} is a zip with no CSV in it; it holds "
                f"{', '.join(archive.namelist()[:4]) or 'nothing'}")
        skipped_others = [n for n in archive.namelist() if n not in members]
        if skipped_others:
            out.notes.append(f"{len(skipped_others)} member(s) of the zip were not CSV and "
                             f"were not read: {', '.join(skipped_others[:4])}")
        for member in members:
            # Read whole and wrapped in a StringIO rather than streamed: the
            # header sniff needs to rewind, and a zip member is not reliably
            # seekable once it has been decoded.
            text = archive.read(member).decode(READ_ENCODING, "replace")
            _read_handle(io.StringIO(text), out, want=want, known=known, source=source,
                         origin=f"{origin}!{member}", label=f"{path}!{member}")
    if not out.records and out.rows_read:
        out.notes.append(
            f"{out.rows_read} row(s) were read and none were kept; the commonest reason is a "
            f"file of a different asset class, or pairs this book does not build")
    return out


def _brief(raw: dict) -> str:
    return ", ".join(f"{v}" for v in list(raw.values())[:6] if v)[:120]


def _row(raw: dict, col: dict, *, want, known, source: str, origin: str,
         line: int) -> tuple[Observation | None, str]:
    """One row, or ``None`` and the reason it was not kept."""
    get = lambda key: str(raw.get(col.get(key, ""), "") or "").strip()

    action = get("action").upper()
    pair, pair_note = _pair_of(get("underlier"), get("underlier_2"), get("product"),
                               get("strike_pair"), get("ccy_1"), get("ccy_2"))
    if not pair:
        return None, f"no currency pair in {get('underlier') or get('product') or 'this row'!r}"
    if want and pair not in want:
        return None, f"{pair} is not one of the pairs asked for"
    if known and pair not in known:
        return None, f"{pair} is not a pair this book builds"

    executed = parse_time(get("executed"))
    if executed is None:
        return None, f"execution timestamp {get('executed')!r} cannot be read"

    option_type = get("option_type").upper().replace(" ", "")
    is_call: bool | None = None
    if option_type in _CALL:
        is_call = True
    elif option_type in _PUT:
        is_call = False
    elif option_type:
        return None, f"option type {get('option_type')!r} is neither a call nor a put"

    strike, strike_note = _number(get("strike"))
    premium, _ = _number(get("premium"))
    notional, capped = _notional(get("notional_1"), get("cap"))

    notes = [n for n in (pair_note, strike_note) if n]
    if capped:
        notes.append(
            "the published notional is the dissemination cap, so the size is a lower bound "
            "and not the trade's size")
    if strike is None:
        notes.append("no strike was published; this trade fixes no point on the smile")
    if premium is None:
        notes.append("no premium was published; the level it traded at cannot be recovered")

    kind_of_action = ("new" if action in NEW_ACTIONS or not action else
                      "cancel" if action in CANCEL_ACTIONS else
                      "correct" if action in CORRECT_ACTIONS else "")
    if not kind_of_action:
        return None, f"action {action!r} is not one this reader knows"

    original = get("original_id")
    if kind_of_action in ("cancel", "correct") and not original:
        return None, (f"a {action} names no original dissemination id, so there is nothing "
                      f"to apply it to")
    if kind_of_action == "new" and original:
        notes.append(f"published as {action} but names an original id ({original}); "
                     f"read as a new trade, not as a correction")
        original = ""

    expiry = _date(get("expiry"))
    return Observation(
        kind="trade", pair=pair, at=executed.astimezone(timezone.utc).isoformat(timespec="seconds"),
        instrument="outright" if strike is not None else "atm",
        tenor=expiry or "", strike=strike, is_call=is_call,
        premium=premium, premium_ccy=get("premium_ccy").upper(),
        notional=notional, notional_ccy=get("ccy_1").upper(),
        notional_capped=capped, expiry_date=expiry,
        action=action or "NEWT", external_id=get("dissemination_id"),
        supersedes_external=original,
        source=source, origin=origin, via="sdr", line=line,
        raw=_brief(raw), notes=tuple(notes)), ""


def _pair_of(*candidates) -> tuple[str, str]:
    """The six-letter pair, and a note when it took work to find.

    The order the two currencies are written in is the order they are read in.
    Re-ordering them into the market's conventional direction is a *marking*
    decision -- USDJPY is quoted one way round and the file may write the other
    -- and it is made once, against the book's own pair list, by the caller.
    """
    for i, text in enumerate(candidates[:-2]):
        if not text:
            continue
        m = _PAIR.search(str(text).upper())
        if m and m.group(1) != m.group(2):
            note = "" if i == 0 else f"the pair was read from {str(text)[:40]!r}"
            return m.group(1) + m.group(2), note
    # Last resort: the two notional currencies.  Their order in the file is
    # the order the legs were reported in, which need not be the order the
    # pair is quoted in -- so this one always carries a note.
    one, two = (str(x or "").strip().upper() for x in candidates[-2:])
    if len(one) == 3 and len(two) == 3 and one.isalpha() and two.isalpha() and one != two:
        return one + two, (f"no underlying was published; the pair was built from the two "
                           f"notional currencies ({one}, {two}) in the order the file "
                           f"reported the legs")
    return "", ""


def _number(text: str) -> tuple[float | None, str]:
    if not text:
        return None, ""
    cleaned = str(text).replace(",", "").strip()
    try:
        return float(cleaned), ""
    except ValueError:
        return None, f"{text!r} is not a number and was not read"


def _notional(text: str, cap_flag: str) -> tuple[float | None, bool]:
    """A notional, and whether it is the cap rather than the size.

    Two spellings of the same fact: a trailing ``+`` on the amount, and a
    separate indicator column.  Either one makes it a lower bound, and the
    amount is still kept -- a capped size is real information about a trade
    being *large*, which is exactly the information a width statistic wants.
    """
    flag = str(cap_flag or "").strip().upper() in ("Y", "YES", "TRUE", "1", "CAPPED")
    if not text:
        return None, flag
    body = str(text).replace(",", "").strip()
    if body.endswith("+"):
        value, _ = _number(body[:-1])
        return value, True
    value, _ = _number(body)
    return value, flag


def _date(text: str) -> str:
    """An expiry as ``YYYY-MM-DD``, or empty.

    Ambiguous two-digit-first formats are refused rather than assumed: an
    American ``03/04/2026`` and a European one are four weeks apart, and the
    tenor a trade lands on is the whole point of keeping it.
    """
    if not text:
        return ""
    body = str(text).strip()
    head = body.split("T")[0].split(" ")[0] if body[:4].isdigit() else body
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d-%b-%Y", "%d-%b-%y",
                "%d %b %Y", "%b %d %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    parsed = parse_time(body)
    return parsed.date().isoformat() if parsed else ""
