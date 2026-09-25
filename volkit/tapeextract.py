"""The DTCC dissemination zips -> one tidy row per FX option print, for the bid-offer study.

``bidoffer.run_study`` reads its tape as ``fx_options_<year>.csv.gz`` files, the layout the quant
repo's extract writes.  On the desk there is no quant repo: what there is is the ``sdr`` folder of
``volkit.cfg``, where the DTCC download (the agent card's *Fetch*, ``volkit agent fetch``) lands
the raw ``CFTC_CUMULATIVE_FOREX_<date>.zip`` files.  This module turns that folder into the same
extract, so the study can be built wherever volkit runs.

It is a port of the quant repo's ``qcore/data/external/dtcc/extract.py``, which was itself ported
from ``volkit/sdr.py``; the reading below is that module's, unchanged, so a study built here and
one built off the quant repo read the same tape the same way.  What differs is only where the zips
come from (the folders named, rather than a manifest) and where the extract is kept (``cache``,
beside the workbook), plus :func:`build`.

``sdr.py`` stays the reader for the agent's archive.  The two differ on purpose on ``TERM``: the
archive treats it as a withdrawal, which is right for "where did this pair trade", and this
module keeps the trade live and records the close, which is what a flow measure wants -- see
``LIFECYCLE_ACTIONS``.

What one output row is
----------------------
One **print**, not one trade and not one position.  There is no counterparty
side in this data: a row says a call on the base currency changed hands at a
strike, and says nothing about who was buying it.

Four columns carry the traps, and they are all in the output rather than
resolved away:

`capped`
    The size is the dissemination cap and therefore a **lower bound**.

`status`
    `live`, `cancelled`, `superseded` or `lifecycle`.  `CANC`/`EROR` withdraw a print and
    `CORR`/`MODI`/`REVI` replace one, both naming an original dissemination id
    that is frequently in an **earlier day's file** -- which is why this is
    resolved across the whole extract and re-resolved on every run, and why
    nothing is deleted.  The study reads `status == "live"`.

`side` and `strike`
    Both come off the option's two legs (a USD call against a JPY put is a
    USDJPY call), because `Option Type` is empty in every row of the layout in
    circulation, and because the file's own pair order is not a convention.
    The pair is put into market convention by `CCY_ORDER` below and the strike
    oriented to match.

`file_date` is the date the desk could have **seen** the print; `exec_date` is the
trade's own UTC date off `Execution Timestamp`, never the event timestamp.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .paths import READ_ENCODING


NEW_ACTIONS = ("NEWT", "NEW", "TRAD")
#: A withdrawal: the print was a mistake and the business never happened.
CANCEL_ACTIONS = ("CANC", "EROR", "ERROR")
#: A replacement: the business happened and this row is its corrected form.
CORRECT_ACTIONS = ("CORR", "MODI", "REVI", "MODIFY")
#: A lifecycle event on a trade that **did** happen -- it was exercised, unwound
#: early or novated away.  Kept apart from a cancel deliberately, and this is the
#: one place this reader departs from `volkit/sdr.py`, which treats `TERM` as a
#: cancel.  That is right for a vol desk reading where the market traded and
#: wrong here.  On 2026-09-18 every one of 556 `TERM` rows carried an `ETRM`,
#: `EXER` or `NOVA` event and not one carried `TRAD`: a termination is not an
#: erroneous print.  **A flow measure must count the original trade; a position
#: measure must remove it.**  Collapsing the two makes one of those two readings
#: impossible, so the withdrawal goes in `status` and the lifecycle event goes in
#: `closed_by` / `closed_on`, and the store serves both.
LIFECYCLE_ACTIONS = ("TERM",)

#: Header synonyms, by meaning, matched on the squashed header.
FIELDS: dict[str, tuple[str, ...]] = {
    "diss_id": ("disseminationidentifier", "disseminationid"),
    "orig_id": ("originaldisseminationidentifier", "originaldisseminationid",
                "priordisseminationid"),
    "action": ("actiontype", "action"),
    "event": ("eventtype",),
    "event_ts": ("eventtimestamp",),
    "exec_ts": ("executiontimestamp", "executiontime"),
    "expiry": ("expirationdate", "optionexpirationdate", "expirydate"),
    "maturity": ("maturitydateoftheunderlier", "maturitydate"),
    "effective": ("effectivedate", "startdate"),
    "strike": ("strikeprice", "optionstrikeprice", "strike"),
    "strike_pair": ("strikepricecurrencycurrencypair", "strikepricecurrencypair",
                    "optioncurrency"),
    "option_type": ("optiontype", "putcallindicator"),
    "premium": ("optionpremiumamount", "optionpremium", "premiumamount"),
    "premium_ccy": ("optionpremiumcurrency", "premiumcurrency"),
    "notional_1": ("notionalamountleg1", "roundednotionalamount1", "notionalamount1"),
    "notional_2": ("notionalamountleg2", "roundednotionalamount2", "notionalamount2"),
    "ccy_1": ("notionalcurrencyleg1", "notionalcurrency1", "notionalcurrency"),
    "ccy_2": ("notionalcurrencyleg2", "notionalcurrency2"),
    "call_amount": ("callamount",),
    "call_ccy": ("callcurrency",),
    "put_amount": ("putamount",),
    "put_ccy": ("putcurrency",),
    "rate": ("exchangerate",),
    "rate_basis": ("exchangeratebasis",),
    #: The one field that says what the row *is* when everything else is blank:
    #: `NA/O Van Put JPY USD`, `NA/Fwd NDF KRW USD`.
    "fisn": ("upifisn",),
    "upi": ("uniqueproductidentifier", "upi"),
    "upi_underlier": ("upiunderliername",),
    "underlier": ("underlyingassetid", "underlyingasset1", "underlierid", "underlieridleg1",
                  "underlyingassetname"),
    #: Equal to the expiry on a European, earlier on an American or a window
    #: option.  The only style discriminator the file actually populates:
    #: `Option Style` and `Embedded Option type` are empty in every row, and
    #: this one is filled in all 4,619 on 2026-09-18.
    "first_exercise": ("firstexercisedate",),
    "product_name": ("productname",),
    "cap_1": ("notionalamountcapindicatorleg1", "notionalamountcapindicator",
              "capindicator"),
    "platform": ("platformidentifier", "executionvenue"),
    "cleared": ("cleared", "clearedindicator"),
    "prime_brokerage": ("primebrokeragetransactionindicator",),
    "block": ("blocktradeelectionindicator",),
    "large_notional": ("largenotionalofffacilityswapelectionindicator",),
}
BY_SYNONYM = {syn: name for name, syns in FIELDS.items() for syn in syns}

#: Headers that carry nothing an FX option store wants and are **known** to carry
#: nothing: the rate-swap machinery every CFTC layout drags along, the package and
#: collateral fields, and the three option columns (`Option Style`, `Embedded
#: Option type`, `Underlying Asset Name`) that are empty in every row of a 2026
#: file.  They are passed over silently so that a header which is *not* here and
#: not in the synonym table stands out -- reporting all 77 every night would hide
#: the 78th, which is how a layout change gets missed.
IGNORED_HEADERS = (
    "Amendment indicator", "Asset Class", "Mandatory clearing indicator",
    "Non-standardized term indicator", "Notional quantity-Leg 1", "Notional quantity-Leg 2",
    "Total notional quantity-Leg 1", "Total notional quantity-Leg 2",
    "Quantity frequency multiplier-Leg 1", "Quantity frequency multiplier-Leg 2",
    "Quantity unit of measure-Leg 1", "Quantity unit of measure-Leg 2",
    "Quantity frequency-Leg 1", "Quantity frequency-Leg 2",
    "Notional amount in effect on associated effective date-Leg 1",
    "Notional amount in effect on associated effective date-Leg 2",
    "Effective date of the notional amount-Leg 1",
    "Effective date of the notional amount-Leg 2",
    "End date of the notional amount-Leg 1", "End date of the notional amount-Leg 2",
    "Fixed rate-Leg 1", "Fixed rate-Leg 2", "Price", "Price unit of measure",
    "Spread-Leg 1", "Spread-Leg 2", "Spread currency-Leg 1", "Spread currency-Leg 2",
    "Post-priced swap indicator", "Price currency", "Price notation",
    "Spread notation-Leg 1", "Spread notation-Leg 2", "Strike price notation",
    "Fixed rate day count convention-leg 1", "Fixed rate day count convention-leg 2",
    "Floating rate day count convention-leg 1", "Floating rate day count convention-leg 2",
    "Floating rate reset frequency period-leg 1", "Floating rate reset frequency period-leg 2",
    "Floating rate reset frequency period multiplier-leg 1",
    "Floating rate reset frequency period multiplier-leg 2",
    "Other payment amount", "Other payment type", "Other payment currency",
    "Fixed rate payment frequency period-Leg 1", "Fixed rate payment frequency period-Leg 2",
    "Floating rate payment frequency period-Leg 1", "Floating rate payment frequency period-Leg 2",
    "Fixed rate payment frequency period multiplier-Leg 1",
    "Fixed rate payment frequency period multiplier-Leg 2",
    "Floating rate payment frequency period multiplier-Leg 1",
    "Floating rate payment frequency period multiplier-Leg 2",
    "Settlement currency-Leg 1", "Settlement currency-Leg 2", "Settlement location",
    "Collateralisation category", "Custom basket indicator", "Index factor",
    "Underlier ID-Leg 2", "Underlier ID source-Leg 1",
    "Underlying asset subtype or underlying contract subtype-Leg 1",
    "Underlying asset subtype or underlying contract subtype-Leg 2",
    "Embedded Option type", "Option Style",
    "Package indicator", "Package transaction price", "Package transaction price currency",
    "Package transaction price notation", "Package transaction spread",
    "Package transaction spread currency", "Package transaction spread notation",
    "Physical delivery location-Leg 1", "Delivery Type",
)

#: Market convention: of a pair's two currencies, the one earlier in this list
#: is the base.  EURUSD and GBPUSD, but USDJPY and USDMXN; metals against USD.
#: Anything unlisted sits after USD and is ordered alphabetically among its
#: kind, which puts every EM cross the conventional way round (USDKRW, USDTWD).
CCY_ORDER = ("XAU", "XAG", "XPT", "XPD", "EUR", "GBP", "AUD", "NZD", "USD")

#: What the FISN calls a product, and what this package calls it.
_FISN_OPTION = ("/O ", " VAN ", " NDO ", " OPT ", " BAR ", " DIG ", " TARG ", " DIGBAR ")

_PAIR = re.compile(r"(?<![A-Z])([A-Z]{3})\s*[-/_ ]?\s*([A-Z]{3})(?![A-Z])")
_SIDE = re.compile(r"\b(CALL|PUT)\b\s+([A-Z]{3})\b")
_CALL_WORDS = ("CALL", "C", "CALLOPTION")
_PUT_WORDS = ("PUT", "PUTO", "P", "PUTOPTION")

OUT_COLUMNS = [
    "file_date", "exec_date", "exec_ts", "event_date", "pair", "base", "quote", "side",
    "product",
    "strike", "oriented_by", "expiry", "first_exercise", "tenor_days",
    "notional_base", "capped", "implausible",
    "notional_quote",
    "premium", "premium_ccy", "action", "event", "status", "closed_by", "closed_on",
    "diss_id", "orig_id",
    "cleared", "platform", "prime_brokerage", "block",
]


class ExtractError(Exception):
    """A dissemination file that cannot be read at all."""


def _squash(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


#: The same headers, squashed, so a lookup is one dict hit.
IGNORED = frozenset(_squash(h) for h in IGNORED_HEADERS)


def _text(value) -> str:
    """A cell as a stripped string, with pandas' missing values as empty.

    Not `str(value or "")`: a float NaN is **truthy**, so that spelling turns an
    empty cell into the literal string "nan" -- which then rides into the
    extract as an event type, a platform code and an original dissemination id.
    """
    if value is None or value != value:
        return ""
    return str(value).strip()


def _number(text) -> float | None:
    if text is None or text != text or text == "":
        return None
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def _notional(text) -> tuple[float | None, bool]:
    """A notional, and whether it is the cap rather than the size.

    A trailing `+` makes it a lower bound.  The amount is still kept: a capped
    size is real information about a trade being *large*, which is exactly the
    information a flow statistic wants -- it just may not be used as an equality.
    """
    if text is None or text != text or text == "":
        return None, False
    body = str(text).replace(",", "").strip()
    if body.endswith("+"):
        return _number(body[:-1]), True
    return _number(body), False


def _flag(text) -> bool | None:
    s = _text(text).upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return True
    if s in ("N", "NO", "FALSE", "0"):
        return False
    return None


def _conventional(one: str, two: str) -> tuple[str, str]:
    """The pair in market convention: base first."""
    def rank(c):
        return CCY_ORDER.index(c) if c in CCY_ORDER else len(CCY_ORDER)
    a, b = rank(one), rank(two)
    if a < b or (a == b and one <= two):
        return one, two
    return two, one


def _pair_of(*candidates) -> tuple[str, str]:
    """The two currencies, in the order they were written.  Orientation comes later."""
    for text in candidates[:-2]:
        if not text:
            continue
        m = _PAIR.search(str(text).upper())
        if m and m.group(1) != m.group(2):
            return m.group(1), m.group(2)
    one, two = (str(x or "").strip().upper() for x in candidates[-2:])
    if len(one) == 3 and len(two) == 3 and one.isalpha() and two.isalpha() and one != two:
        return one, two
    return "", ""


def _product(fisn: str) -> str:
    """`NA/O Van Put JPY USD` -> `Van`; `NA/FX O Nstd USD XXX` -> `Nstd`.

    The word after the option marker, whatever the marker's spelling.  `Other`
    when the FISN says only that it is an option, which is what a bespoke
    structure the taxonomy has no word for looks like.
    """
    text = _text(fisn).upper()
    m = re.search(r"(?:/O|\bO)\s+(\S+)", text)
    if not m:
        return "Other" if text else ""
    word = m.group(1).title()
    return "Other" if word in ("Ot", "Oth", "Other") else word


def _is_option(fisn: str, strike, call_ccy: str, put_ccy: str) -> bool:
    """Two thirds of a FOREX day is NDFs and outrights.  Filing those as
    at-the-money trades fills the store with rows that are not options."""
    text = str(fisn or "").upper()
    if any(x in text for x in _FISN_OPTION) or text.startswith("NA/O"):
        return True
    if "NDF" in text or "/FWD" in text:
        return False
    return bool(call_ccy and put_ccy and _text(strike))


def _parse_ts(text) -> datetime | None:
    body = str(text or "").strip()
    if not body:
        return None
    body = body.replace("Z", "+00:00")
    try:
        got = datetime.fromisoformat(body)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                got = datetime.strptime(body[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def _parse_date(text) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    got = _parse_ts(body)
    return got.astimezone(timezone.utc).date().isoformat() if got else ""


#: How near a leg ratio must be to a published strike before it is allowed to
#: say which way round that strike is written.  Leg amounts are rounded, so a
#: correct pair of legs lands within a fraction of a percent; a pair that is
#: 14% away is not rounding, it is a reporter who published one leg wrong.
ORIENT_TOL = 0.02


def _oriented(value: float | None, ratio: float | None) -> tuple[float | None, str]:
    """A published strike, in the direction the two leg amounts say it is written.

    The labels do not settle it -- `Exchange rate basis` reads "second per
    first" in nine rows out of ten and the other way in the tenth.  The amounts
    usually do: one leg over the other is the rate, and the published number is
    whichever of it and its reciprocal that ratio is near.

    **The ratio only gets a vote when it is actually near one of them.**  Without
    that condition this inverts good strikes on the rows where the reporter
    published a leg wrong: a 2026-09-18 EURUSD print carries a 1.1475 strike
    against legs of 3,000,000 EUR and 3,000,000 USD, whose ratio is 1.0 -- equally
    far from 1.1475 and from 0.8715, so the comparison is a coin flip and it came
    down on 0.8715.  A strike that is 14% from the legs means the legs are wrong,
    not the strike.
    """
    if not value or not ratio or ratio <= 0 or value <= 0:
        return value, "published"
    near = abs(math.log(value / ratio))
    flipped = abs(math.log((1.0 / value) / ratio))
    if min(near, flipped) > ORIENT_TOL:
        return value, "published"         # the legs know nothing; leave it alone
    return (value, "legs") if near <= flipped else (1.0 / value, "legs")


# --------------------------------------------------------------------- parse ---
def columns_of(header: list[str]) -> tuple[dict[str, str], list[str]]:
    """meaning -> the header that carries it, and the headers that were not placed."""
    col, unplaced = {}, []
    for raw in header:
        squashed = _squash(raw)
        name = BY_SYNONYM.get(squashed)
        if name and name not in col:
            col[name] = raw
        elif not name and squashed not in IGNORED:
            unplaced.append(raw)
    return col, unplaced


def parse_day(path: Path, *, file_date: date | None = None) -> tuple[pd.DataFrame, dict]:
    """One day's zip -> the FX option prints in it, and a report of what it cost.

    Only the columns that mean something are read, off a header mapped by
    meaning -- which is also why this is fast: the file is 110 columns wide and
    32 MB, and about 25 of those columns are wanted.
    """
    if not zipfile.is_zipfile(path):
        raise ExtractError(f"{path} is not a zip")
    # Read as bytes and decoded as the one encoding every text file here is read in
    # (``paths.READ_ENCODING``), never the machine's locale -- cp1252 on the desk.
    with zipfile.ZipFile(path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not members:
            raise ExtractError(f"{path} holds no CSV")
        body = zf.read(members[0])
    header = pd.read_csv(io.BytesIO(body), nrows=0, encoding=READ_ENCODING).columns.tolist()
    col, unplaced = columns_of(header)
    if "fisn" not in col and "call_ccy" not in col:
        raise ExtractError(
            f"{path.name}: neither a UPI FISN nor call/put legs in {len(header)} columns; "
            f"the layout has changed -- first few headers {header[:6]}")
    df = pd.read_csv(io.BytesIO(body), usecols=list(col.values()), dtype=str, low_memory=False,
                     encoding=READ_ENCODING)
    del body

    stamp = file_date or _stamp_of(path)
    get = lambda key: (df[col[key]] if key in col else pd.Series([None] * len(df), index=df.index))

    fisn = get("fisn").fillna("")
    call_ccy = get("call_ccy").fillna("").str.upper().str.strip()
    put_ccy = get("put_ccy").fillna("").str.upper().str.strip()
    strike_raw = get("strike")
    keep = [_is_option(f, s, c, p) for f, s, c, p
            in zip(fisn, strike_raw, call_ccy, put_ccy)]
    opt = df[pd.Series(keep, index=df.index)]
    report = {"file": path.name, "rows": len(df), "options": len(opt),
              "unplaced": unplaced, "skipped": []}
    if not len(opt):
        return pd.DataFrame(columns=OUT_COLUMNS), report

    rows = []
    for i, raw in enumerate(opt.to_dict("records")):
        got = lambda key: raw.get(col.get(key, ""), None)
        text = lambda key: _text(got(key))
        out, why = _row(got, text, stamp)
        if out is None:
            report["skipped"].append((i, why))
        else:
            rows.append(out)
    frame = pd.DataFrame(rows, columns=OUT_COLUMNS)
    report["kept"] = len(frame)
    return frame, report


def _row(got, text, stamp: date) -> tuple[dict | None, str]:
    """One print, or None and the reason it was not kept."""
    fisn = text("fisn")
    one, two = _pair_of(text("underlier"), text("product_name"), text("strike_pair"),
                        text("upi_underlier"), fisn, text("ccy_1"), text("ccy_2"))
    if not one:
        return None, f"no currency pair in {fisn or 'this row'!r}"
    base, quote = _conventional(one, two)

    # Two different dates, and conflating them is the commonest way to misdate
    # this tape.  `Execution Timestamp` is when the **trade** was done -- on a
    # termination row that is the *original* trade, often years back.  `Event
    # timestamp` is when **this report** happened, which on a termination is the
    # unwind and on a new trade is the trade.
    executed = _parse_ts(text("exec_ts")) or _parse_ts(text("event_ts"))
    if executed is None:
        return None, f"execution timestamp {text('exec_ts') or text('event_ts')!r} cannot be read"
    reported = _parse_ts(text("event_ts")) or executed

    # --- the side and the strike, off the legs.  `Option Type` is empty in
    # every row of the layout in circulation, so the legs are the authority:
    # a call on one currency is a put on the other, and the ratio of the two
    # amounts is the strike in whatever convention the pair is written in.
    call_ccy, put_ccy = text("call_ccy").upper(), text("put_ccy").upper()
    is_call = None
    ratio = base_amt = quote_amt = None
    capped = False
    if call_ccy and put_ccy and {call_ccy, put_ccy} == {base, quote}:
        is_call = call_ccy == base
        call_amt, call_capped = _notional(got("call_amount"))
        put_amt, put_capped = _notional(got("put_amount"))
        base_amt = call_amt if is_call else put_amt
        quote_amt = put_amt if is_call else call_amt
        # The cap is a property of the *amount used*, not of the row: the file
        # caps one leg and not the other often enough that reading a row flag
        # instead throws away trades whose base leg was published in full.
        capped = bool(call_capped if is_call else put_capped)
        if base_amt and quote_amt:
            ratio = quote_amt / base_amt
    if is_call is None:
        word = text("option_type").upper().replace(" ", "")
        if word in _CALL_WORDS:
            is_call = True
        elif word in _PUT_WORDS:
            is_call = False
        else:
            m = _SIDE.search(fisn.upper())
            if m and m.group(2) in (base, quote):
                # A put on the quote currency is a call on the base.
                is_call = (m.group(1) == "CALL") if m.group(2) == base else (m.group(1) == "PUT")
    if is_call is None:
        return None, f"neither the legs, the option type nor {fisn!r} give a side"

    strike, oriented_by = _oriented(_number(got("strike")), ratio)
    if strike is None and ratio:
        strike, oriented_by = ratio, "legs"
    if base_amt is None:
        base_amt, capped = _notional(got("notional_1"))
        if text("ccy_1").upper() != base:
            base_amt, capped = (None, capped) if text("ccy_2").upper() != base else \
                _notional(got("notional_2"))

    action = text("action").upper() or "NEWT"
    kind = ("new" if action in NEW_ACTIONS else
            "cancel" if action in CANCEL_ACTIONS else
            "correct" if action in CORRECT_ACTIONS else
            "lifecycle" if action in LIFECYCLE_ACTIONS else "")
    if not kind:
        return None, f"action {action!r} is not one this reader knows"
    orig = text("orig_id")
    if kind in ("cancel", "correct", "lifecycle") and not orig:
        return None, f"a {action} names no original dissemination id"
    if kind == "new":
        orig = ""

    expiry = _parse_date(got("expiry")) or _parse_date(got("maturity"))
    exec_date = executed.astimezone(timezone.utc).date()
    tenor = ((date.fromisoformat(expiry) - exec_date).days
             if expiry else None)
    return {
        "file_date": stamp.isoformat(), "exec_date": exec_date.isoformat(),
        "exec_ts": executed.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "event_date": reported.astimezone(timezone.utc).date().isoformat(),
        "pair": base + quote, "base": base, "quote": quote,
        "side": "call" if is_call else "put", "product": _product(fisn),
        "strike": strike, "oriented_by": oriented_by,
        "expiry": expiry or "", "first_exercise": _parse_date(got("first_exercise")),
        "tenor_days": tenor,
        "notional_base": base_amt, "capped": bool(capped),
        "implausible": bool(base_amt is not None and base_amt >= SENTINEL_NOTIONAL),
        "notional_quote": quote_amt,
        "premium": _number(got("premium")), "premium_ccy": text("premium_ccy").upper(),
        "action": action, "event": text("event").upper(), "status": "",
        "closed_by": "", "closed_on": "",
        "diss_id": text("diss_id"), "orig_id": orig,
        "cleared": text("cleared").upper(), "platform": text("platform").upper(),
        "prime_brokerage": _flag(got("prime_brokerage")), "block": _flag(got("block")),
    }, ""


def _stamp_of(path: Path) -> date:
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})", path.name)
    if not m:
        raise ExtractError(f"no date in the file name {path.name}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


#: How far a strike must sit from its pair's own level before its reciprocal is
#: preferred.  A factor of 1.5 in log space: wide enough that no strike on a real
#: smile is caught, narrow enough that an inverted one always is.
REORIENT_MARGIN = math.log(1.5)

#: A notional at or above this is not a size.  440 prints across 12 days publish
#: **exactly** 1e20 with a premium of exactly zero -- a placeholder for a size the
#: reporter did not disclose, not a 100 quintillion dollar option.  The gap is not
#: a judgement call: live notionals reach 3.7e9 at the 99.9th percentile and about
#: 1e11 at the very top (a 14 billion *yen* trade, which is a real $90 million),
#: and then nothing at all for nine orders of magnitude.  1e15 sits in the middle
#: of that gap and is above any real FX notional in any currency -- 1e15 JPY is
#: six trillion dollars.  The row is kept and flagged; a single one of these in a
#: notional-weighted sum swamps a whole year of real trades.
SENTINEL_NOTIONAL = 1e15

#: Prints in a pair before its median is allowed to overrule a published strike.
#: Below this the "level" is one or two trades and could itself be the inverted one.
MIN_ANCHOR = 20


def reorient(frame: pd.DataFrame) -> pd.DataFrame:
    """Catch the strikes that sit inverted against their own pair's level.

    `_oriented` settles a strike whenever the option's two leg amounts agree
    with one direction of it.  That is not enough on its own, because the legs
    themselves can be reversed: a 2026-09-18 USDCNY print publishes a call
    amount of 165,027,699 tagged USD against a put amount of 25,000,000 tagged
    CNY, whose ratio is 0.15149, and the strike is published as 0.15149 -- so
    the legs "confirm" a number that is 1/6.60 and not a USDCNY strike at all.
    The amounts and the currency tags were paired the wrong way round, and
    nothing inside the row can tell.

    What settles it is the pair's *own* level, taken as the **median** log
    strike of every print in that pair.  A median is what makes this safe: a
    day's strikes in one pair straddle its spot, and a handful of inverted rows
    cannot move the middle of several hundred.  A pair whose prints are too few
    to locate (`MIN_ANCHOR`) is left alone.

    **A row marked `oriented_by == "pair"` had its strike repaired and nothing
    else.**  Whatever reversed the strike reversed the legs, so that row's
    `side`, `notional_base` and `notional_quote` are not to be trusted either --
    which of the several ways they could be wrong it is cannot be recovered from
    the row, so nothing is guessed.  There are very few of them; drop them from
    anything notional- or side-weighted.
    """
    if frame.empty or "strike" not in frame:
        return frame
    out = frame.copy()
    s = pd.to_numeric(out["strike"], errors="coerce")
    usable = s.notna() & (s > 0)
    if not usable.any():
        return out
    ls = pd.Series(float("nan"), index=out.index)
    ls[usable] = np.log(s[usable])
    by_pair = ls.groupby(out["pair"])
    anchor, count = by_pair.transform("median"), by_pair.transform("count")
    flip = (usable & anchor.notna() & (count >= MIN_ANCHOR)
            & (((-ls) - anchor).abs() < (ls - anchor).abs() - REORIENT_MARGIN))
    out.loc[flip, "strike"] = 1.0 / s[flip]
    out.loc[flip, "oriented_by"] = "pair"
    return out


# ------------------------------------------------------------------- resolve ---
def resolve(frame: pd.DataFrame) -> pd.DataFrame:
    """Mark each print, and carry each lifecycle event back to the trade it ends.

    Nothing is ever dropped.  Four values of `status`:

    `live`
        New business, as last corrected.  **This is the flow.**
    `cancelled`
        Withdrawn -- the print was a mistake and the trade never happened, so it
        is in neither the flow nor the position.
    `superseded`
        Replaced by a later correction, which is the `live` row.  Counting both
        double-counts one trade.
    `lifecycle`
        A `TERM` row: not new business, but a report that an existing trade was
        exercised, terminated early or novated.  It is **not** a cancel, and the
        trade it names stays `live` -- it really traded.  What it leaves behind
        is `closed_by` and `closed_on` on that trade.

    So a flow measure reads `status == "live"`, and a position measure reads
    `status == "live"` and drops what `closed_on` has already closed.  Both are
    possible only because the two facts are in two columns.

    All of this runs over the **whole** extract and is recomputed on every
    refresh, because the id a cancel, a correction or a termination names is
    regularly in an earlier day's file -- often months earlier, for an option
    exercised at its expiry.
    """
    if frame.empty:
        return frame
    out = frame.copy()
    out["status"] = "live"
    for col in ("closed_by", "closed_on"):
        out[col] = out.get(col, "").fillna("") if col in out else ""
    ids = out["diss_id"].astype(str)
    action = out["action"].astype(str).str.upper()
    orig = out["orig_id"].astype(str).replace("nan", "").fillna("")

    is_corr = action.isin(CORRECT_ACTIONS)
    is_canc = action.isin(CANCEL_ACTIONS)
    is_life = action.isin(LIFECYCLE_ACTIONS)

    # A correction replaces the id it names, and can itself be corrected again.
    # Walking the chain is what lets a termination reach the row that is
    # actually live rather than the first version of it.
    replaced_by = dict(zip(orig[is_corr], ids[is_corr]))

    def current(start: str, limit: int = 16) -> str:
        seen, node = set(), start
        while node in replaced_by and node not in seen and len(seen) < limit:
            seen.add(node)
            node = replaced_by[node]
        return node

    cancelled = {current(o) for o in orig[is_canc] if o} | set(orig[is_canc]) - {""}
    superseded = set(replaced_by) - {""}
    out.loc[is_canc, "status"] = "cancelled"
    out.loc[is_life, "status"] = "lifecycle"
    ordinary = ~(is_canc | is_life)
    out.loc[ordinary & ids.isin(superseded), "status"] = "superseded"
    out.loc[ordinary & ids.isin(cancelled), "status"] = "cancelled"

    # The lifecycle events, carried back onto the trade each one ends.
    if is_life.any():
        ended = pd.DataFrame({
            "target": [current(o) for o in orig[is_life]],
            "by": out.loc[is_life, "event"].astype(str).str.upper().replace("", "ETRM").values,
            "on": out.loc[is_life, "event_date"].astype(str).values,
        })
        # The first close is the one that counts; a later report against the
        # same id is a duplicate of it, not a second unwind.
        ended = ended.sort_values("on").drop_duplicates("target", keep="first")
        # Only a trade can be closed.  A cancelled print never happened, and a
        # lifecycle row is a report *about* a close rather than a thing that
        # gets one -- without this a novation chain marks its own links closed.
        trade = out["status"].isin(("live", "superseded"))
        hit = ids.map(ended.set_index("target")["by"]).where(trade)
        when = ids.map(ended.set_index("target")["on"]).where(trade)
        out.loc[hit.notna(), "closed_by"] = hit[hit.notna()]
        out.loc[when.notna(), "closed_on"] = when[when.notna()]
    return out


# ------------------------------------------------------------------- extract ---
#: Where :func:`build` keeps the extract when it is not told: beside the workbook.
CACHE_DIRNAME = "tape_extract"


def held_days(folders) -> tuple[list[tuple[date, Path]], list[str]]:
    """Every dated dissemination zip under ``folders``, oldest first, and what was passed over.

    A day held twice (the same file in two folders) is read once, from the first folder naming
    it.  A zip whose name carries no date cannot be placed on the tape and is reported, not read.
    """
    seen: dict[date, Path] = {}
    notes: list[str] = []
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            notes.append(f"{folder} is not a folder")
            continue
        for p in sorted(folder.glob("*.zip")):
            try:
                day = _stamp_of(p)
            except ExtractError:
                notes.append(f"{p.name}: no date in the name, not read")
                continue
            seen.setdefault(day, p)
    return sorted(seen.items()), notes


def extract_path(year: int, cache) -> Path:
    return Path(cache) / f"fx_options_{year}.csv.gz"


def load_extract(cache, years=None) -> pd.DataFrame:
    frames = []
    for p in sorted(Path(cache).glob("fx_options_*.csv.gz")):
        year = int(p.stem.split("_")[-1].split(".")[0])
        if years and year not in years:
            continue
        frames.append(pd.read_csv(p, dtype={"diss_id": str, "orig_id": str}))
    if not frames:
        return pd.DataFrame(columns=OUT_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def build(folders, cache, *, rebuild: bool = False, log=print) -> dict:
    """Parse whatever zips under ``folders`` are not yet in the extract at ``cache``, then
    re-resolve it all.

    Incremental: a day already in a year file is not re-parsed, because a day takes a second or
    two and a year of the tape is 260 of them.  Supersession *is* recomputed over everything every
    run -- it is cheap, and it is the only way a cancel published today retires a print written
    months ago.  Returns what was read, for the screen to say.
    """
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    days, notes = held_days(folders)
    if not days:
        where = ", ".join(str(f) for f in folders) or "no folder"
        raise ExtractError(f"no dissemination zips (CFTC_CUMULATIVE_FOREX_<date>.zip) under "
                           f"{where}; fetch some on the Market maker screen's agent card or "
                           f"with `volkit agent fetch`")
    have = pd.DataFrame(columns=OUT_COLUMNS) if rebuild else load_extract(cache)
    done = set(have["file_date"].astype(str)) if len(have) else set()
    fresh, parsed, failed, unplaced = [], 0, [], set()
    todo = [(d, p) for d, p in days if d.isoformat() not in done]
    for n, (day, path) in enumerate(todo, 1):
        try:
            frame, report = parse_day(path, file_date=day)
        except (ExtractError, zipfile.BadZipFile, ValueError, KeyError) as exc:
            failed.append(f"{path.name}: {exc}")
            log(f"  {path.name}: {exc}")
            continue
        parsed += 1
        unplaced |= set(report["unplaced"])
        if len(frame):
            fresh.append(frame)
        if n == 1 or n == len(todo) or n % 20 == 0:
            log(f"  read {n} of {len(todo)} new day(s): {day} held {report.get('kept', 0):,} "
                f"option prints")

    blocks = [f for f in ([have] if len(have) else []) + fresh if len(f)]
    whole = pd.concat(blocks, ignore_index=True) if len(blocks) > 1 else (
        blocks[0] if blocks else have)
    if not len(whole):
        raise ExtractError(f"{len(days)} zip(s) read and not one FX option print in them")
    whole = whole.drop_duplicates(["file_date", "diss_id", "action", "exec_ts"], keep="last")
    whole = whole.drop(columns=["closed_by", "closed_on"], errors="ignore")
    whole = resolve(reorient(whole)).sort_values(["file_date", "exec_ts", "diss_id"])

    written = []
    if fresh or rebuild:
        for year, block in whole.groupby(whole["file_date"].astype(str).str[:4]):
            dest = extract_path(int(year), cache)
            part = dest.with_name(dest.name + ".part")
            block[OUT_COLUMNS].to_csv(part, index=False, compression="gzip")
            part.replace(dest)
            written.append(dest.name)
    live = int((whole["status"] == "live").sum())
    log(f"tape: {len(days)} day(s) held, {parsed} newly read, {len(whole):,} prints "
        f"({live:,} live), {whole['file_date'].min()} .. {whole['file_date'].max()}")
    return {"days": len(days), "parsed": parsed, "rows": len(whole), "live": live,
            "first": str(whole["file_date"].min()), "last": str(whole["file_date"].max()),
            "written": written, "failed": failed, "notes": notes,
            "unplaced": sorted(unplaced), "cache": str(cache)}
