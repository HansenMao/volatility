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
import math
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
    "executed": ("eventtimestamp", "executiontime", "originalexecutiontimestamp"),
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
    # --- the CDE layout in circulation, which publishes an FX option as its
    # two legs rather than as a side and a strike.  ``Option Type`` is empty
    # in every row of a 2026 file; these are what carry the same facts.
    "call_amount": ("callamount",),
    "call_ccy": ("callcurrency",),
    "put_amount": ("putamount",),
    "put_ccy": ("putcurrency",),
    #: The rate the trade was struck at, and the two currencies it is written
    #: in.  On an option row it repeats the strike; on a forward or an NDF it
    #: is the traded outright, which is the only forward on the tape.
    "rate": ("exchangerate",),
    "rate_basis": ("exchangeratebasis",),
    #: The product, in words: ``NA/O Van Put HKD USD``, ``NA/Fwd NDF HKD USD``.
    #: The one field that says what the row *is* when everything else is blank.
    "fisn": ("upifisn",),
    "upi": ("uniqueproductidentifier", "upi"),
    "upi_underlier": ("upiunderliername",),
    #: TRAD, EXER, NOVA, ETRM ... an exercise is not a trade, and the action
    #: column alone does not separate them.
    "event_type": ("eventtype",),
    #: When the trade was *done*.  Kept apart from the event timestamp, which
    #: on an exercise or a correction is the moment of that event and not of
    #: the trade -- reading one as the other dates a print days late.
    "execution": ("executiontimestamp",),
    "premium_per_unit": ("optionpremiumperunit", "premiumperunit"),
    "product_name": ("productname",),
    "maturity": ("maturitydateoftheunderlier",),
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
    "postpricedswapindicator", "custombasketindicator", "indexfactor", "pricenotation",
    "strikepricenotation", "pricecurrency", "settlementlocation", "deliverytype",
    "underlieridsourceleg1", "firstexercisedate", "optionlockoutperiod",
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
    if "underlier" not in col and (col.keys() & {"product", "product_name", "strike_pair",
                                                "rate_basis", "upi_underlier", "fisn"}):
        missing.remove("underlier")     # a UPI, a FISN or a rate basis carries the pair
    if "executed" not in col and "execution" in col:
        missing.remove("executed")      # the execution timestamp is the better one anyway
    if "strike" not in col and "call_amount" in col:
        missing.remove("strike")        # an option's legs carry its strike
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
    # The underlier and the product name are empty in the layout in
    # circulation, so the pair is found wherever it *is* written -- and the
    # order it is written in is not a convention: the same file writes USDHKD
    # as "HKD/USD" and AUDHKD as "AUD/HKD".  Only the book decides which way
    # round a pair is quoted.
    pair, pair_note = _pair_of(get("underlier"), get("underlier_2"), get("product_name"),
                               get("strike_pair"), get("rate_basis"), get("upi_underlier"),
                               get("fisn"), get("ccy_1"), get("ccy_2"))
    if not pair:
        return None, f"no currency pair in {get('underlier') or get('product') or 'this row'!r}"
    pair, orient_note = _orient(pair, known or want)
    if want and pair not in want:
        return None, f"{pair} is not one of the pairs asked for"
    if known and pair not in known:
        return None, f"{pair} is not a pair this book builds"
    base, quote = pair[:3], pair[3:6]

    # The trade's own time, not the event's.  On an exercise or a correction
    # the event timestamp is the moment of *that*, and reading it as the trade
    # dates a print days after it was done.
    executed = parse_time(get("execution")) or parse_time(get("executed"))
    if executed is None:
        return None, (f"execution timestamp {get('execution') or get('executed')!r} "
                      f"cannot be read")

    option_type = get("option_type").upper().replace(" ", "")
    is_call: bool | None = None
    if option_type in _CALL:
        is_call = True
    elif option_type in _PUT:
        is_call = False
    elif option_type:
        return None, f"option type {get('option_type')!r} is neither a call nor a put"

    # The legs are the authority: ``Option Type`` is blank in every row of the
    # layout in circulation, and the legs give the side, the strike in the
    # book's convention, and the base notional in one go.
    leg_call, leg_ratio, leg_notional, leg_capped, leg_note = _side_and_strike(
        raw, col, base, quote)
    if leg_call is not None:
        is_call = leg_call
    elif is_call is None:
        is_call = _fisn_side(get("fisn"), base, quote)
        if is_call is not None:
            leg_note = (f"neither the option type nor the legs gave a side; read as a "
                        f"{'call' if is_call else 'put'} from the product name "
                        f"{get('fisn')!r}")

    strike, strike_note = _number(get("strike"))
    if strike is None and leg_ratio:
        strike, flip_note = leg_ratio, "no strike was published; the legs give it"
    else:
        strike, flip_note = _oriented(strike, leg_ratio, "strike")
        if strike is not None and not leg_ratio and orient_note:
            flip_note = ("the legs give no amounts, so the strike is the published one and "
                         "which way round it is written could not be checked")
    premium, _ = _number(get("premium"))
    notional, capped = _notional(get("notional_1"), get("cap"))
    if leg_notional:
        # The base leg, whichever column it landed in.  ``Notional amount-Leg
        # 1`` is whichever leg the reporter put first, and a premium per unit
        # of the *quote* currency is not a premium per unit of the base.
        notional, notional_ccy_leg, capped = leg_notional, base, leg_capped
    else:
        notional_ccy_leg = get("ccy_1").upper()

    notes = [n for n in (pair_note, orient_note, leg_note, flip_note, strike_note) if n]
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

    expiry = _date(get("expiry")) or _date(get("maturity"))
    at = executed.astimezone(timezone.utc).isoformat(timespec="seconds")
    event = get("event_type").upper()
    common = dict(pair=pair, at=at, tenor=expiry or "", expiry_date=expiry,
                  action=action or "NEWT", event=event,
                  external_id=get("dissemination_id"), supersedes_external=original,
                  source=source, origin=origin, via="sdr", line=line, raw=_brief(raw))

    # An option or a forward?  Two thirds of every file is outrights, NDFs and
    # swaps, and filing those as at-the-money *trades* fills the archive with
    # rows that are not options at all.  They are not noise, though: the rate
    # on one is the traded forward of its own date, and on a pair the
    # historical workbook does not cover it is the only forward there is.
    option = (strike is not None or leg_call is not None
              or any(x in (get("fisn") or "").upper() for x in _FISN_OPTION))
    if not option:
        rate, _ = _number(get("rate"))
        # The two notional legs orient the rate the same way the option's two
        # legs orient its strike, and for the same reason: the label cannot.
        amounts = {get("ccy_1").upper(): _number(get("notional_1"))[0],
                   get("ccy_2").upper(): _number(get("notional_2"))[0]}
        fwd_ratio = None
        if amounts.get(base) and amounts.get(quote):
            fwd_ratio = amounts[quote] / amounts[base]
        if rate is None and fwd_ratio:
            rate, rate_note = fwd_ratio, "no rate was published; the two legs give it"
        else:
            rate, rate_note = _oriented(rate, fwd_ratio, "rate")
            if rate is not None and not fwd_ratio and orient_note:
                return None, ("a forward whose legs do not say which way round its rate is "
                              "written, on a pair the book quotes the other way")
        if rate is None or not expiry:
            return None, ("neither an option nor a dated forward: no strike, no option legs "
                          "and no rate with a date on it")
        if rate_note:
            notes.append(rate_note)
        return Observation(kind="forward", instrument="atm", rate=rate,
                           notional=amounts.get(base) or notional, notional_ccy=base,
                           notional_capped=capped, notes=tuple(notes), **common), ""

    return Observation(
        kind="trade", instrument="outright" if strike is not None else "atm",
        strike=strike, is_call=is_call,
        premium=premium, premium_ccy=get("premium_ccy").upper(),
        notional=notional, notional_ccy=notional_ccy_leg,
        notional_capped=capped, notes=tuple(notes), **common), ""


def _orient(pair: str, known) -> tuple[str, str]:
    """The book's own spelling of a pair the file may have written either way.

    The file writes USDHKD as ``HKD/USD`` -- and AUDHKD as ``AUD/HKD`` -- so
    the order in the file is not a convention anything may be read from.  The
    currencies are, and the book is the authority on which way round the pair
    is quoted: matched either way, the book's spelling wins and the row says
    so.  Without a book there is nothing to orient against and the file's own
    order stands, which is the one case a strike may come out inverted.
    """
    if not pair or not known:
        return pair, ""
    if pair in known:
        return pair, ""
    flipped = pair[3:6] + pair[:3]
    if flipped in known:
        return flipped, (f"the file wrote the pair as {pair[:3]}/{pair[3:6]}; the book quotes "
                         f"{flipped}, and the side and the strike are read in that convention")
    return pair, ""


def _side_and_strike(raw, col, base: str, quote: str) -> tuple:
    """The side, the strike and the base notional, from the option's two legs.

    An FX option is a call on one currency and a put on the other, and that is
    how this file publishes it: ``Call currency USD`` with ``Put currency HKD``
    is a USD call, which on USDHKD is a call.  Two facts come out of the pair
    of legs and neither needs a naming convention:

    * **the side** -- the call currency is the base, or it is not;
    * **the strike** -- the quote-currency amount over the base-currency
      amount, which is the strike in the book's own convention whatever order
      the file wrote the pair in.

    This matters because ``Option Type`` is **empty in every row** of the
    layout in circulation: read only from that column, no print in the file
    has a side, and a trade with no side cannot be inverted to a volatility.

    Returns ``(is_call, ratio, base_notional, capped, note)``, any of which may be
    ``None`` when the legs do not carry it.  The ratio is a *check* on the
    published strike and not a replacement for it: leg amounts are rounded,
    and a strike of 7.75 published against legs that round to 7.80 is a 7.75
    strike.  What the ratio is for is deciding **which way round** the
    published number is written, which nothing else in the row can settle.
    """
    get = lambda key: str(raw.get(col.get(key, ""), "") or "").strip()
    call_ccy, put_ccy = get("call_ccy").upper(), get("put_ccy").upper()
    if not (call_ccy and put_ccy) or {call_ccy, put_ccy} != {base, quote}:
        return None, None, None, False, ""
    is_call = call_ccy == base
    call_amt, call_capped = _notional(get("call_amount"), "")
    put_amt, put_capped = _notional(get("put_amount"), "")
    base_amt = call_amt if is_call else put_amt
    quote_amt = put_amt if is_call else call_amt
    # The cap is a property of the *amount used*, not of the row.  The file
    # caps one leg and not the other often enough that reading the row's flag
    # instead threw away trades whose base leg was published in full -- and
    # the base leg is the only one a premium per unit is divided by.
    base_capped = call_capped if is_call else put_capped
    side = "call" if is_call else "put"
    note = f"read as a {side} from the legs ({call_ccy} call against {put_ccy} put)"
    if base_capped:
        note += f"; the {base} leg is the dissemination cap and not the size"
    if base_amt and quote_amt:
        return is_call, quote_amt / base_amt, base_amt, base_capped, note
    return is_call, None, base_amt or None, base_capped, note


def _oriented(value: float | None, ratio: float | None, what: str) -> tuple[float | None, str]:
    """A published rate, in the direction the two legs say it is written.

    The file's own labels do not settle it: the same file writes USDHKD as
    ``HKD/USD`` and AUDHKD as ``AUD/HKD``, and ``Exchange rate basis`` reads
    "second per first" in nine rows out of ten and the other way in the tenth.
    The **amounts** settle it, always: one leg over the other is the rate, to
    whatever rounding the amounts carry, and the published number is whichever
    of it and its reciprocal that ratio is near.
    """
    if value is None or not value or not ratio or ratio <= 0:
        return value, ""
    near = abs(math.log(value / ratio))
    flipped = abs(math.log((1.0 / value) / ratio))
    if near <= flipped:
        return value, ""
    return 1.0 / value, (f"the {what} {value:g} was published in the other direction; the legs "
                         f"put it at {ratio:g}, so it is read as {1.0 / value:g}")


#: What the FISN calls a product: ``NA/O Van Put HKD USD``, ``NA/Fwd NDF HKD
#: USD``.  It is the only field that says what the row *is* when ``Product
#: name`` and ``Underlier ID`` are blank, which in a 2026 file they always are.
_FISN_OPTION = ("/O ", " VAN ", " NDO ", " OPT ")
_FISN_FORWARD = ("/FWD", " NDF ", " FWD ")


def _fisn_side(fisn: str, base: str, quote: str) -> bool | None:
    """The side out of ``NA/O Van Put HKD USD``: a put on the first currency
    named after it.  Used only when the legs did not say, and never against
    the order the pair is written in -- the currency it names is looked up."""
    text = str(fisn or "").upper()
    m = re.search(r"(CALL|PUT)\s+([A-Z]{3})", text)
    if not m:
        return None
    on, ccy = m.group(1), m.group(2)
    if ccy not in (base, quote):
        return None
    # A put on the quote currency is a call on the base, and the other way.
    return (on == "CALL") if ccy == base else (on == "PUT")


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
