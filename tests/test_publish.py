"""The bulk export: every channel, the tables, the overlay, the screen's routes.

`claude/publishing-channels-design.md` is the specification.  The three
file channels are byte-comparable, so the tests here pin the *shape* of each
file against what that note recorded of the desk's own files -- header row,
sheet name, pair order, labels, tenor spellings, two decimals, bid equal to
ask -- and the Bloomberg tickers cell by cell, the 33 `VRR` cells included.
The overlay tests pin the rules that keep the book of record clean: a partial
file changes exactly the cells it names, a value neither source has is
refused by name, a row the channel does not publish is ignored and counted,
an applied overlay blocks the workbook write, and Revert restores the
captured session.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from volkit import configsheets, kace, overlay, publish, session
from volkit.book import Book
from volkit.timeutil import UTC, Clock

WORKBOOK = Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx"
REFERENCE = Path(__file__).resolve().parents[1] / "files" / "reference"
ASOF = Clock(datetime(2024, 2, 28, 12, 0, tzinfo=UTC))

#: The pairs the tests publish.  A small list, deliberately -- the real
#: EXPORT_PAIRS tab is the desk's -- but with one of every kind: a dollar
#: pair, a cross, a pair capped at 1Y, a pair fed from another curve, an
#: inverted one, and one the book does not hold.
PAIRS = [
    {"channel": "bloomberg", "pair": "USDJPY", "label": "USDJPY", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "bloomberg", "pair": "AUDJPY", "label": "AUDJPY", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "bloomberg", "pair": "CHFJPY", "label": "CHFJPY", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "bloomberg", "pair": "HKDJPY", "label": "HKDJPY", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "bloomberg", "pair": "XAUUSD", "label": "XAUUSD", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    # One list for the destination: both Murex files carry the same pairs.
    {"channel": "murex", "pair": "AUDUSD", "label": "AUD/USD", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "murex", "pair": "USDJPY", "label": "USD/JPY", "feed_from": "",
     "last_tenor": "1Y", "note": ""},
    {"channel": "cos", "pair": "USDCNH", "label": "USD/CNY", "feed_from": "",
     "last_tenor": "", "note": ""},
    {"channel": "cos", "pair": "EURCNH", "label": "EUR/CNY", "feed_from": "",
     "last_tenor": "", "note": ""},
    {"channel": "cos", "pair": "HKDCNH", "label": "HKD/CNY", "feed_from": "CNHHKD",
     "last_tenor": "", "note": ""},
    {"channel": "kace", "pair": "USDCNH", "label": "USDCNH", "feed_from": "",
     "last_tenor": "", "note": ""},
    {"channel": "kace", "pair": "AUDHKD", "label": "AUDHKD", "feed_from": "",
     "last_tenor": "", "note": ""},
]

OVERLAY = """pair,tenor,atm,RR 25D,RR 10D,ST 25D,ST 10D
# a whole pair the book does not hold
AUDHKD,O/N,8.0,0.10,0.20,0.30,0.90
AUDHKD,1W,8.1,0.10,0.20,0.30,0.90
AUDHKD,2W,8.2,0.10,0.20,0.30,0.90
AUDHKD,1M,8.3,0.10,0.20,0.30,0.90
AUDHKD,2M,8.3,0.10,0.20,0.30,0.90
AUDHKD,3M,8.3,0.10,0.20,0.30,0.90
AUDHKD,6M,8.3,0.10,0.20,0.30,0.90
AUDHKD,9M,8.3,0.10,0.20,0.30,0.90
AUDHKD,1Y,8.3,0.10,0.20,0.30,0.90
# one cell of a pair the book holds
USDJPY,1M,,-2.5,,,
# a pair the book holds the other way up
HKDCNH,1W,5.0,0.10,0.20,0.30,0.40
HKDCNH,2W,5.0,0.10,0.20,0.30,0.40
HKDCNH,1M,5.0,0.10,0.20,0.30,0.40
HKDCNH,3M,5.0,0.10,0.20,0.30,0.40
HKDCNH,6M,5.0,0.10,0.20,0.30,0.40
# a tenor no sheet quotes
EURUSD,3Y,7.0,0.10,0.20,0.30,0.40
"""


#: The USDJPY-shaped wings HKDJPY and CNHHKD are marked with in the fixture:
#: a negative risk reversal, as the dollar leg's is.
_WINGS = {t: {"rr_25": -1.5, "rr_10": -2.8, "st_25": 0.32, "st_10": 0.85}
          for t in ("1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y")}


def _workbook(tmp: Path, *, cos_widths: bool = True) -> Path:
    """A copy of the shipped workbook with the export tables written in.

    Five pairs are added the way the Config window adds them: HKDJPY, so the
    sign convention can be pinned against the HKDJPY surface itself, and
    CNHHKD, which the COS channel publishes the other way up as HKD/CNY.
    Both are crosses, so the level typed is a correlation.  Then USDCHF,
    XAUUSD and the CHFJPY cross USDCHF carries, which the Bloomberg block
    publishes: the shipped workbook holds none of the three, and pairs this
    module needs are added here rather than assumed, so the export tests do
    not depend on which pairs the desk's workbook happens to carry that day.
    """
    wb = tmp / "vol_marks.xlsx"
    shutil.copy(WORKBOOK, wb)
    session.add_pair(wb, "HKDJPY", atm=0.95, quotes=_WINGS)
    # CNHHKD has an empty sheet in the shipped workbook, so add_pair puts it
    # back into CONFIG and leaves the quotes to the screen; here they are
    # typed straight into the sheet, in the workbook's own column order.
    session.add_pair(wb, "CNHHKD", atm=0.2)
    # The Bloomberg block publishes these three and the shipped workbook has
    # none of them; USDCHF comes first because it is the dollar leg CHFJPY is
    # built from.  Volatility points for the two dollar pairs, a correlation
    # for the cross.
    session.add_pair(wb, "USDCHF", atm=7.0, quotes=_WINGS)
    session.add_pair(wb, "XAUUSD", atm=12.0, quotes=_WINGS)
    session.add_pair(wb, "CHFJPY", atm=0.55, quotes=_WINGS)
    import io
    import openpyxl
    blob = wb.read_bytes()
    book = openpyxl.load_workbook(io.BytesIO(blob))
    # Saved the way the session module saves: openpyxl drops every formula's
    # cached value, and the other sheets' 10-delta wings are formulas.
    cached = session._formula_cache(book, openpyxl.load_workbook(io.BytesIO(blob),
                                                                 data_only=True))
    ws = book["CNHHKD"]
    # Write from row 1 rather than appending.  The shipped CNHHKD sheet looks
    # empty but carries ten blank formatted rows, so ``append`` put the header
    # at row 11; a pair sheet's header is row 1, so the reader saw no quotes
    # and every COS test failed with "HKDCNH: no 1W, 2W, 1M, 3M, 6M in the
    # book".  Clearing first makes the fixture independent of how much blank
    # formatting the sheet happens to carry.
    if ws.max_row:
        ws.delete_rows(1, ws.max_row)
    rows = [["expiry", "ST 10D", "ST 25D", "RR 25D", "RR 10D"]]
    rows += [[tenor, q["st_10"], q["st_25"], q["rr_25"], q["rr_10"]]
             for tenor, q in _WINGS.items()]
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)
    session._save_workbook(book, wb, cached)
    tabs = publish.seed_tables({})
    tabs["EXPORT_PAIRS"] = [dict(r) for r in PAIRS]
    tabs["MARKET_WIDTHS"] = [{"tenor": t, "USDJPY": 0.5, "AUDJPY": 0.7, "CHFJPY": 0.8,
                              "HKDJPY": 0.6, "XAUUSD": 1.0} for t in publish.ELEVEN]
    # One wing ladder for every pair, so a width is a known number in the tests.
    tabs["WING_WIDTHS"] = [{"pair": "default", "tenor": t, "rr25": 0.4, "rr10": 0.8,
                            "bf25": 0.3, "bf10": 0.6, "note": ""} for t in publish.ELEVEN]
    tabs["SHADES"] = [{"channel": "bloomberg", "pair": "", "shade": -0.2, "note": ""},
                      {"channel": "bloomberg", "pair": "CHFJPY", "shade": 0.0, "note": ""}]
    # Rewrite SPREADS as it stands, so the fixture exercises the rename of
    # the tab from its old KACE_SPREADS name the way a first write does.
    tabs["SPREADS"] = [dict(r.cells) for r in
                       configsheets.read_rows(wb, "SPREADS", required=("tenor", "default"))]
    # One COS ladder for every pair, so the one-sided width is a known number
    # in the tests rather than whichever rung the desk's own seed carries.
    if cos_widths:
        tabs["COS_WIDTHS"] = [{"pair": "default", "tenor": t, "width": 0.8, "note": ""}
                              for t in publish.COS_TENORS]
    else:
        tabs.pop("COS_WIDTHS", None)
    session.write_config_tabs(wb, tabs)
    return wb


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.wb = _workbook(cls.tmp)
        cls.book = Book.from_excel(cls.wb, ASOF).load_all()
        cls.tables = publish.ExportTables.load(cls.wb)
        cls.overlay = overlay.parse(OVERLAY, path="client-run.csv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestTables(_Fixture):
    def test_the_spread_table_is_read_under_its_old_name_and_renamed_on_write(self):
        """KACE_SPREADS became SPREADS.  The shipped workbook still says
        KACE_SPREADS, so the reader finds it under the old name and the
        first write of the tab renames it, in place, prose and all."""
        names = configsheets.sheet_names(WORKBOOK)
        self.assertIn("KACE_SPREADS", names)
        self.assertNotIn("SPREADS", names)
        self.assertEqual(configsheets.match_sheet(names, "SPREADS"), "KACE_SPREADS")
        self.assertTrue(any("called SPREADS now" in x for x in configsheets.renamed_tabs(WORKBOOK)))
        table = kace.SpreadTable.load(WORKBOOK)
        self.assertEqual(table.for_tier()["1M"], 0.4)
        # After the fixture's write, which wrote the tab: renamed, same place.
        after = configsheets.sheet_names(self.wb)
        self.assertIn("SPREADS", after)
        self.assertNotIn("KACE_SPREADS", after)
        self.assertEqual(after.index("SPREADS"), names.index("KACE_SPREADS"))
        self.assertEqual(configsheets.renamed_tabs(self.wb), [])
        self.assertEqual(kace.SPREADS_SHEET, "SPREADS")

    def test_every_export_table_is_editable_and_belongs_to_the_export_screen(self):
        for sheet in configsheets.EXPORT_TABS:
            self.assertIn(sheet, configsheets.EDITABLE)
            self.assertIn(sheet, configsheets.SHEETS)
        self.assertEqual(configsheets.OPEN_COLUMNS["MARKET_WIDTHS"], "pair")
        self.assertEqual(configsheets.OPEN_COLUMNS["SPREADS"], "tier")
        summary = self.tables.summary()
        self.assertTrue(all(summary["present"].values()), summary)
        self.assertEqual(summary["errors"], {})

    def test_widths_and_shades_resolve_as_the_note_describes(self):
        """The Bloomberg width is an observed market two-way plus a policy
        add-up -- 0.2 outside overnight, 0 for a cross -- and the shade is a
        mid shift with CHFJPY's zero as a row of the table."""
        t = self.tables
        w, where = t.market_width("USDJPY", "1M")
        self.assertAlmostEqual(w, 0.5 + 0.2)
        self.assertIn("default other add-up", where)
        w, where = t.market_width("USDJPY", "O/N")
        self.assertAlmostEqual(w, 0.5 + 0.0)
        self.assertIn("overnight", where)
        w, where = t.market_width("AUDJPY", "1M")
        self.assertAlmostEqual(w, 0.7 + 0.0)
        self.assertIn("crosses", where)
        # A pair the table does not carry is refused by name, never zero.
        with self.assertRaises(publish.PublishError) as ctx:
            t.market_width("GBPUSD", "1M")
        self.assertIn("GBPUSD", str(ctx.exception))
        self.assertEqual(t.shade("bloomberg", "USDJPY"), (-0.2, "bloomberg default shade"))
        self.assertEqual(t.shade("bloomberg", "CHFJPY"), (0.0, "bloomberg CHFJPY shade"))
        self.assertEqual(t.shade("murex", "USDJPY")[0], 0.0)
        # A tier read at a tenor between its rungs, and a multiplier.
        self.assertAlmostEqual(t.tier_width("default", "1M", multiplier=2)[0], 0.8)
        # The COS ladder is pair-dependent and one-sided, so it is its own
        # table rather than a column of SPREADS.
        self.assertEqual(t.cos_width("USDCNH", "1M"), (0.8, "COS_WIDTHS default 1M"))
        with self.assertRaises(publish.PublishError) as ctx:
            t.cos_width("USDCNH", "2Y")
        self.assertIn("COS_WIDTHS", str(ctx.exception))

    def test_a_channel_with_no_pair_list_is_refused_by_name(self):
        bare = publish.ExportTables.load(WORKBOOK)
        self.assertIsNone(bare.export_pairs)
        with self.assertRaises(publish.PublishError) as ctx:
            publish.build("murex", self.book, bare)
        self.assertIn("EXPORT_PAIRS", str(ctx.exception))
        with self.assertRaises(publish.PublishError):
            publish.channel("murex_wings")

    def test_a_workbook_typed_before_the_murex_merge_still_reads(self):
        """`murex_vol` and `murex_broker` were two channels writing one file
        each; they are one `murex` destination writing both.  A workbook
        typed before that carries the same pair list under both old names,
        which is the merge and not a pair listed twice."""
        with tempfile.TemporaryDirectory() as tmp:
            wb = Path(tmp) / "legacy.xlsx"
            shutil.copy(self.wb, wb)
            legacy = []
            for old_key in ("murex_vol", "murex_broker"):
                for row in PAIRS:
                    if row["channel"] == "murex":
                        legacy.append(dict(row, channel=old_key))
                    elif old_key == "murex_vol":
                        legacy.append(dict(row))
            session.write_config_tabs(wb, {"EXPORT_PAIRS": legacy})
            tables = publish.ExportTables.load(wb)
            self.assertNotIn("murex_vol", tables.export_pairs)
            self.assertEqual([e.pair for e in tables.pairs_for("murex")],
                             ["AUDUSD", "USDJPY"])
            self.assertEqual([e.pair for e in tables.pairs_for("murex_broker")],
                             ["AUDUSD", "USDJPY"])

    def test_seeding_writes_only_what_is_missing_and_carries_the_desks_files(self):
        """The seed is the desk's own files (`exportseed.py`): the pair lists
        in file order, the Bloomberg widths cell for cell, the add-ups that
        reproduce the sheet, the COS ladder off the Guideline sheet and the
        CNH crosses' correlation off the cross workbook."""
        from volkit import exportseed
        rows = publish.seed_tables({"SHADES": True, "ADD_UPS": True})
        self.assertEqual(sorted(rows), ["COS_WIDTHS", "CROSS_CORR", "EXPORT_PAIRS",
                                        "MARKET_WIDTHS", "WING_WIDTHS"])
        self.assertEqual(publish.seed_tables(self.tables.summary()["present"]), {})
        every = publish.seed_tables({})
        bbg = [r["pair"] for r in every["EXPORT_PAIRS"] if r["channel"] == "bloomberg"]
        self.assertEqual(len(bbg), 33)
        self.assertEqual(bbg[:4], ["AUDUSD", "AUDHKD", "USDCAD", "CADHKD"])
        self.assertEqual([r["last_tenor"] for r in every["EXPORT_PAIRS"]
                          if r["pair"] == "XAUUSD" and r["channel"] == "bloomberg"], ["1Y"])
        murex = [r["label"] for r in every["EXPORT_PAIRS"] if r["channel"] == "murex"]
        self.assertEqual(len(murex), 32)
        self.assertEqual(murex[0], "AUD/USD")
        self.assertEqual(murex[-1], "AUD/CHF")
        cos = [(r["pair"], r["label"]) for r in every["EXPORT_PAIRS"] if r["channel"] == "cos"]
        self.assertEqual(len(cos), 25)
        self.assertIn(("USDCNH", "USD/CNY"), cos)
        self.assertIn(("CNHJPY", "CNY/JPY"), cos)
        # Every G7 and G7 Cross ATM two-way was 0.2 under the mark, CHFJPY
        # included -- and the four EM/PM pairs, whose block is a separate
        # sheet on the desk's side, are shaded by nothing at all.
        bbg_shades = [r for r in every["SHADES"] if r["channel"] == "bloomberg"]
        self.assertEqual(bbg_shades[0]["pair"], "")
        self.assertEqual(bbg_shades[0]["shade"], -0.2)
        self.assertEqual({r["pair"]: r["shade"] for r in bbg_shades[1:]},
                         {"USDCNH": 0.0, "USDHKD": 0.0, "HKDCNH": 0.0, "XAUUSD": 0.0})
        # The COS ladder, one-sided, off the desk's Guideline sheet: a common
        # shape and the handful of pairs that widen at the front, which is
        # why it cannot be one tier of SPREADS.
        cos_w = {(r["pair"], r["tenor"]): r["width"] for r in every["COS_WIDTHS"]}
        self.assertEqual(cos_w[("default", "1W")], 0.8)
        self.assertEqual(cos_w[("default", "6M")], 0.5)
        self.assertEqual(cos_w[("USDJPY", "1W")], 1.0)
        self.assertEqual(cos_w[("NZDJPY", "2W")], 0.7)
        self.assertEqual(cos_w[("USDCNH", "1W")], 0.5)
        self.assertEqual(sorted({p for p, _ in cos_w}), sorted(exportseed.COS_WIDTHS))
        # The CNH crosses' correlation, rung by rung, under both spellings of
        # the HKD/CNH cross.
        corr = {(r["pair"], r["tenor"]): r["correlation"] for r in every["CROSS_CORR"]}
        self.assertEqual(corr[("AUDCNH", "1M")], -0.7)
        self.assertEqual(corr[("AUDCNH", "3Y")], -0.6)
        self.assertEqual(corr[("CNHJPY", "6M")], 0.375)
        self.assertEqual(corr[("CNHHKD", "1M")], 0.325)
        self.assertEqual(corr[("HKDCNH", "1M")], 0.325)
        self.assertEqual(len(every["CROSS_CORR"]), 9 * 12)
        # The HKD legs the G7 tab carried take the G7 add-up by name.
        self.assertIn({"pair": "AUDHKD", "overnight": 0.0, "other": 0.2,
                       "note": "an HKD leg the G7 tab carried, so it takes the G7 add-up"},
                      every["ADD_UPS"])
        self.assertEqual(len(every["WING_WIDTHS"]), 33 * 11 - 2)
        self.assertEqual(exportseed.WING_WIDTHS["AUDCAD"]["O/N"], (3, 6, 2.1, 4.2))
        self.assertEqual(exportseed.MARKET_WIDTHS["USDJPY"]["1M"], 0.6)


class TestBloomberg(_Fixture):
    def test_the_ticker_rule_and_its_one_exception(self):
        self.assertEqual(publish.bbg_ticker("AUDUSD", "atm", "3M"), "ADUSV3M")
        self.assertEqual(publish.bbg_ticker("AUDUSD", "rr25", "3M"), "ADUSRR3M")
        self.assertEqual(publish.bbg_ticker("AUDUSD", "rr10", "3M"), "ADUSRX3M")
        self.assertEqual(publish.bbg_ticker("AUDUSD", "bf25", "3M"), "ADUSB3M")
        self.assertEqual(publish.bbg_ticker("AUDUSD", "bf10", "3M"), "ADUSBX3M")
        self.assertEqual(publish.bbg_ticker("USDJPY", "atm", "O/N"), "USJYV1D")
        self.assertEqual(publish.bbg_ticker("USDCNH", "bf10", "1D"), "USCGBX1D")
        # The legacy ticker: every pair's 1M 25d risk reversal is <prefix>VRR.
        self.assertEqual(publish.bbg_ticker("AUDUSD", "rr25", "1M"), "ADUSVRR")
        self.assertEqual(publish.bbg_ticker("HKDJPY", "rr25", "1M"), "HDJYVRR")
        self.assertEqual(publish.bbg_ticker("AUDUSD", "rr10", "1M"), "ADUSRX1M")
        self.assertEqual(publish.BBG_LEGACY_TICKERS, {("rr25", "1M"): "VRR"})
        with self.assertRaises(publish.PublishError):
            publish.bbg_ticker("USDSGD", "atm", "1M")
        self.assertEqual(
            publish.bbg_formula("B3", "BID", "O3"),
            '=_xll.PLContribFull(B3*100,"BID",O3,"Slot46","TICKER",3,,"Valid")')

    def test_the_sheet_carries_every_cell_shaded_and_widened_as_the_note_says(self):
        b = publish.build("bloomberg", self.book, self.tables)
        self.assertTrue(b.ok, b.refused)
        # 5 pairs x 9 tenors (O/N to 1Y): the workbook marks to 1Y.
        self.assertEqual(len(b.quotes), 5 * 9)
        cells = publish.bloomberg_cells(b)
        self.assertEqual(len(cells), 5 * 9 * 5)
        vrr = [c for c in cells if c["ticker"].endswith("VRR")]
        self.assertEqual(len(vrr), 5)
        self.assertTrue(all(c["tenor"] == "1M" and c["instrument"] == "rr25" for c in vrr))
        by = {(q.pair, q.tenor): q for q in b.quotes}
        q = by[("USDJPY", "1M")]
        self.assertEqual(q.shade, -0.2)
        self.assertAlmostEqual(q.mid, q.atm - 0.2)
        self.assertAlmostEqual(q.ask - q.bid, 0.7)
        self.assertAlmostEqual(q.bid, q.atm - 0.2 - 0.35)
        self.assertEqual(by[("CHFJPY", "1M")].shade, 0.0)
        self.assertAlmostEqual(by[("AUDJPY", "1M")].ask - by[("AUDJPY", "1M")].bid, 0.7)
        # The wings are not shaded; they go out two-way about their own
        # marks, by WING_WIDTHS, as the desk's sheet does (K..N / 200).
        atm = [c for c in cells if c["pair"] == "USDJPY" and c["tenor"] == "1M"]
        rr = next(c for c in atm if c["instrument"] == "rr25")
        self.assertAlmostEqual(rr["ask"] - rr["bid"], 0.4)
        self.assertAlmostEqual((rr["ask"] + rr["bid"]) / 2, q.rr25)
        self.assertEqual(q.wing_widths, {"rr25": 0.4, "rr10": 0.8, "bf25": 0.3, "bf10": 0.6})
        self.assertTrue(b.file_name.endswith(".xlsx"))
        import io
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(b.file_bytes))[publish.BBG_SHEET]
        # The desk's own block layout: pair, header, a tenor row per tenor,
        # two blank rows; the formulas read the value cells beside them.
        self.assertEqual(ws["A1"].value, "USDJPY")
        self.assertEqual(ws["A2"].value, "Bid")
        self.assertEqual(ws["H2"].value, "Ask")
        self.assertEqual(ws["A3"].value, "ON")
        self.assertEqual(ws["O3"].value, "USJYV1D")
        self.assertEqual(ws["P3"].value,
                         '=_xll.PLContribFull(B3*100,"BID",O3,"Slot46","TICKER",3,,"Valid")')
        self.assertEqual(ws["Q3"].value,
                         '=_xll.PLContribFull(I3*100,"ASK",O3,"Slot46","TICKER",3,,"Valid")')
        self.assertEqual(ws["R6"].value, "USJYVRR")
        self.assertAlmostEqual(ws["B6"].value, q.bid / 100.0)
        self.assertAlmostEqual(ws["I6"].value, q.ask / 100.0)
        self.assertAlmostEqual(ws["C6"].value, q.wing_bid("rr25") / 100.0)
        self.assertEqual(ws["A14"].value, "AUDJPY")
        self.assertIn("source: book marks", openpyxl.load_workbook(
            io.BytesIO(b.file_bytes))["volkit"]["A1"].value)

    def test_the_block_matches_the_desks_own_sheet_cell_for_cell(self):
        """`files/reference/BCFO_Vols_bbg_output.xlsx` is the desk's sheet.

        For every pair the fixture builds, the block written here has the
        same tickers and the same formula strings, cell for cell, as the
        block that sheet carries for the pair -- with the row numbers
        shifted, since this puts every block on one tab.  3,610 formula
        cells in the desk's file; the layout and the rule are pinned on the
        five pairs the fixture publishes.
        """
        import io
        import re
        import openpyxl
        ref = openpyxl.load_workbook(REFERENCE / "BCFO_Vols_bbg_output.xlsx")
        blocks: dict[str, list[dict]] = {}
        for name in ("G7 Output", "G7 Cross Output", "EM PM Output"):
            ws = ref[name]
            r = 1
            while r <= ws.max_row:
                pair = ws.cell(row=r, column=1).value
                if isinstance(pair, str) and len(pair) == 6 and ws.cell(row=r + 1, column=1).value == "Bid":
                    rows = []
                    rr = r + 2
                    while rr <= ws.max_row and ws.cell(row=rr, column=1).value not in (None, ""):
                        rows.append({"tenor": ws.cell(row=rr, column=1).value,
                                     "cells": {c: ws.cell(row=rr, column=c).value
                                               for c in range(15, 30)},
                                     "row": rr, "top": r})
                        rr += 1
                    blocks[pair] = rows
                    r = rr
                else:
                    r += 1
        self.assertEqual(len(blocks), 33)
        b = publish.build("bloomberg", self.book, self.tables)
        ws = openpyxl.load_workbook(io.BytesIO(b.file_bytes))[publish.BBG_SHEET]
        mine = {}
        r = 1
        while r <= ws.max_row:
            pair = ws.cell(row=r, column=1).value
            if isinstance(pair, str) and len(pair) == 6 and ws.cell(row=r + 1, column=1).value == "Bid":
                rows = []
                rr = r + 2
                while rr <= ws.max_row and ws.cell(row=rr, column=1).value not in (None, ""):
                    rows.append({"tenor": ws.cell(row=rr, column=1).value,
                                 "cells": {c: ws.cell(row=rr, column=c).value
                                           for c in range(15, 30)}, "row": rr})
                    rr += 1
                mine[pair] = rows
                r = rr
            else:
                r += 1
        self.assertEqual(sorted(mine), sorted(p for p in blocks if p in mine))
        shift = re.compile(r"([A-Z]{1,2})(\d+)")
        compared = 0
        for pair, rows in mine.items():
            theirs = {x["tenor"]: x for x in blocks[pair]}
            for x in rows:
                y = theirs[x["tenor"]]
                for c, value in x["cells"].items():
                    want = y["cells"][c]
                    if isinstance(want, str) and want.startswith("="):
                        # The same formula, with their row number put back.
                        got = shift.sub(lambda m: m.group(1) + str(
                            int(m.group(2)) - x["row"] + y["row"]), value)
                        self.assertEqual(got, want, (pair, x["tenor"], c))
                    else:
                        self.assertEqual(value, want, (pair, x["tenor"], c))
                    compared += 1
        self.assertEqual(compared, 5 * 9 * 15)

    def test_hkdjpy_keeps_usdjpy_sign_convention_with_no_flip(self):
        """The desk's answer (2026-09-10): HKD is pegged to the dollar, so
        HKDJPY's skew goes out the same way round as USDJPY's; the JPYHKD
        label on the old sheets was stale.  The book's rr_25 goes as it
        stands."""
        b = publish.build("bloomberg", self.book, self.tables)
        by = {(q.pair, q.tenor): q for q in b.quotes}
        h, u = by[("HKDJPY", "1M")], by[("USDJPY", "1M")]
        self.assertFalse(h.flipped)
        self.assertEqual(h.feed_from, "")
        marks = {m.tenor: m for m in self.book["HKDJPY"].quoted_marks()}
        self.assertAlmostEqual(h.rr25, marks["1M"].rr_25 * 100.0)
        # Same sign as the dollar leg, as the peg implies.
        self.assertEqual(h.rr25 < 0, u.rr25 < 0)

    def test_a_pair_the_width_table_lacks_is_refused_and_nothing_is_written(self):
        tables = publish.ExportTables.load(self.wb)
        del tables.market_widths["XAUUSD"]
        b = publish.build("bloomberg", self.book, tables)
        self.assertFalse(b.ok)
        self.assertTrue(any("XAUUSD" in r and "MARKET_WIDTHS" in r for r in b.refused), b.refused)
        self.assertEqual(b.file_bytes, b"")
        with self.assertRaises(publish.PublishError):
            publish.write_file(b, self.tmp, log=None, when=ASOF.now)


class TestMurexAndCos(_Fixture):
    def _xls(self, blob: bytes):
        # BIFF8, and the sheet name is in the file whatever reads it back.
        self.assertEqual(blob[:8], b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        self.assertIn(b"Sheet1&TODAY", blob)
        try:
            import xlrd
        except ImportError:
            self.skipTest("xlrd is not installed; the rows are not read back")
        book = xlrd.open_workbook(file_contents=blob)
        ws = book.sheet_by_index(0)
        return ws.name, [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]

    def test_one_murex_build_writes_both_files_off_one_pair_list(self):
        """Murex was two channels and is one destination.

        The ATM file and the broker file come out of a single build -- one
        pair list, one source choice, one preflight, one date -- because a
        Murex load is the pair of them and a desk that wrote one of them
        yesterday and the other today has a surface disagreeing with itself.
        """
        b = publish.build("murex", self.book, self.tables)
        self.assertTrue(b.ok, b.refused)
        self.assertEqual([f.name for f in b.files],
                         ["DRV_MktData_FX_Vol_20240228.xls",
                          "DRV_MktData_FX_Broker_20240228.xls"])
        # "the file" of a two-file destination is an ambiguous question and
        # is refused with both names rather than answered with the first.
        with self.assertRaises(publish.PublishError) as ctx:
            b.file()
        self.assertIn("DRV_MktData_FX_Broker_20240228.xls", str(ctx.exception))
        with self.assertRaises(publish.PublishError):
            b.file("DRV_MktData_FX_Nope.xls")
        # The old names still reach it, from a command line or a workbook row.
        self.assertEqual(publish.channel("murex_vol").key, "murex")
        self.assertEqual(publish.channel("murex_broker").key, "murex")

    def test_the_vol_file_is_the_shape_the_loader_has_always_seen(self):
        b = publish.build("murex", self.book, self.tables)
        self.assertTrue(b.ok, b.refused)
        name, rows = self._xls(b.file("DRV_MktData_FX_Vol_20240228.xls").body)
        self.assertEqual(name, "Sheet1&TODAY")
        self.assertEqual(rows[0], ["ccy pair", "Maturity", "bid", "ask"])
        self.assertEqual(len(rows) - 1, 2 * 9)
        # The file's own pair order, slashes, O/N, bid equal to ask, two decimals.
        self.assertEqual([r[0] for r in rows[1:10]], ["AUD/USD"] * 9)
        self.assertEqual([r[1] for r in rows[1:10]],
                         ["O/N", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"])
        for r in rows[1:]:
            self.assertEqual(r[2], r[3])
            self.assertEqual(round(r[2], 2), r[2])
        self.assertEqual(rows[10][0], "USD/JPY")
        # No shade and no width on Murex: the mid is the mark.
        q = next(q for q in b.quotes if q.pair == "USDJPY" and q.tenor == "1M")
        self.assertEqual((q.shade, q.width), (0.0, 0.0))
        self.assertAlmostEqual(rows[13][2], round(q.atm, 2))

    def test_the_broker_file_carries_the_wings_at_ordinate_10_and_25(self):
        b = publish.build("murex", self.book, self.tables)
        self.assertTrue(b.ok, b.refused)
        name, rows = self._xls(b.file("DRV_MktData_FX_Broker_20240228.xls").body)
        self.assertEqual(rows[0], ["ccy pair", "Maturity", "Ordinate", "fxbflyBid",
                                   "fxbflyAsk", "fxbrrBid", "fxbrrAsk"])
        self.assertEqual(len(rows) - 1, 2 * 9 * 2)
        self.assertEqual([r[2] for r in rows[1:5]], [10.0, 25.0, 10.0, 25.0])
        q = next(q for q in b.quotes if q.pair == "AUDUSD" and q.tenor == "O/N")
        ten, twenty = rows[1], rows[2]
        self.assertEqual((ten[0], ten[1]), ("AUD/USD", "O/N"))
        self.assertAlmostEqual(ten[3], round(q.bf10, 2))
        self.assertAlmostEqual(ten[5], round(q.rr10, 2))
        self.assertAlmostEqual(twenty[3], round(q.bf25, 2))
        self.assertAlmostEqual(twenty[5], round(q.rr25, 2))
        for r in rows[1:]:
            self.assertEqual((r[3], r[5]), (r[4], r[6]))

    def test_the_cos_file_is_one_side_five_tenors_and_cny_labels_fed_cnh(self):
        b = publish.build("cos", self.book, self.tables)
        self.assertTrue(b.ok, b.refused)
        self.assertEqual(b.file_name, "COS_86830_Bid.csv")
        lines = b.file_bytes.decode("utf-8").splitlines()
        self.assertEqual(lines[0], "CUR PAIR,1W,2W,1M,3M,6M")
        self.assertEqual([ln.split(",")[0] for ln in lines[1:]],
                         ["USD/CNY", "EUR/CNY", "HKD/CNY"])
        q = next(q for q in b.quotes if q.pair == "USDCNH" and q.tenor == "1W")
        self.assertEqual(q.width_from, "COS_WIDTHS default 1W")
        # One-sided: the whole width sits under the mid and there is no ask
        # away from it, because the file carries a bid and nothing else.
        self.assertTrue(q.one_sided)
        self.assertAlmostEqual(q.ask, q.mid)
        self.assertAlmostEqual(q.bid, q.mid - 0.8)
        self.assertAlmostEqual(q.ask - q.bid, 0.8)
        self.assertEqual(lines[1].split(",")[1], f"{q.bid:.2f}")
        # HKD/CNY is read off the CNHHKD sheet the other way up: the risk
        # reversals change sign, the ATM does not, and the row says so.
        h = next(q for q in b.quotes if q.pair == "HKDCNH" and q.tenor == "1W")
        self.assertTrue(h.flipped)
        self.assertEqual(h.feed_from, "CNHHKD")
        marks = {m.tenor: m for m in self.book["CNHHKD"].quoted_marks()}
        self.assertAlmostEqual(h.rr25, -marks["1W"].rr_25 * 100.0)
        self.assertAlmostEqual(h.bf25, marks["1W"].st_25 * 100.0)

    def test_the_cos_ladder_has_to_be_typed_before_the_channel_runs(self):
        """No COS_WIDTHS is no file, by name.  A width that defaulted to zero
        would publish the mid as a bid, which is the short file this refuses
        to write."""
        with tempfile.TemporaryDirectory() as tmp:
            wb = _workbook(Path(tmp), cos_widths=False)
            tables = publish.ExportTables.load(wb)
            self.assertIsNone(tables.cos_widths)
            b = publish.build("cos", self.book, tables)
            self.assertFalse(b.ok)
            self.assertTrue(all("COS_WIDTHS" in r for r in b.refused), b.refused)

    def test_the_file_date_is_the_books_and_a_disagreement_is_said(self):
        b = publish.build("murex", self.book, self.tables)
        d = b.preflight["date"]
        self.assertEqual(d["book"], "2024-02-28")
        self.assertEqual(d["file"], "2024-02-28")
        self.assertFalse(d["agree"])          # the machine is not in 2024
        self.assertTrue(any("dated 20240228" in n for n in b.notes))
        from datetime import date
        b2 = publish.build("murex", self.book, self.tables, file_date=date(2024, 3, 1))
        # One date for the destination: both files carry it.
        self.assertEqual([f.name for f in b2.files],
                         ["DRV_MktData_FX_Vol_20240301.xls",
                          "DRV_MktData_FX_Broker_20240301.xls"])
        self.assertTrue(b2.preflight["date"]["overridden"])

    def test_writing_records_the_file_and_a_dry_run_records_nothing(self):
        b = publish.build("cos", self.book, self.tables)
        log = kace.PostLog.at(self.tmp / "publish_log.jsonl")
        dry = publish.write_file(b, self.tmp / "out", log=log, when=ASOF.now, dry_run=True)
        self.assertIsNone(dry[0]["ok"])
        self.assertFalse((self.tmp / "out" / "COS_86830_Bid.csv").exists())
        self.assertFalse(Path(log.path).exists())
        written = publish.write_file(b, self.tmp / "out", log=log, when=ASOF.now)
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0]["ok"])
        self.assertEqual((self.tmp / "out" / "COS_86830_Bid.csv").read_bytes(), b.file_bytes)
        rows = log.entries(channel="cos")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "marks")
        self.assertEqual(rows[0]["sha256"], b.sha256)
        self.assertEqual(rows[0]["channel"], "cos")
        self.assertEqual(log.entries(channel="kace"), [])

    def test_a_murex_run_writes_both_files_and_logs_a_row_for_each(self):
        """The log's row has always been "a file was written", and a Murex
        run writes two: two rows, one channel, each with its own name and
        hash, and the source shared."""
        b = publish.build("murex", self.book, self.tables)
        log = kace.PostLog.at(self.tmp / "murex_log.jsonl")
        out = self.tmp / "mx"
        dry = publish.write_file(b, out, log=log, when=ASOF.now, dry_run=True)
        self.assertEqual([e["ok"] for e in dry], [None, None])
        self.assertFalse(out.exists())
        written = publish.write_file(b, out, log=log, when=ASOF.now)
        self.assertEqual(len(written), 2)
        self.assertTrue(all(e["ok"] for e in written))
        for f, entry in zip(b.files, written):
            self.assertEqual((out / f.name).read_bytes(), f.body)
            self.assertEqual(entry["sha256"], f.sha256)
            self.assertEqual(entry["what"], f.what)
        rows = log.entries(channel="murex")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["source"] for r in rows}, {"marks"})
        # Path().name, not split("/"): the log keeps the path the file was
        # written at, which on Windows is backslash-separated, so splitting on
        # "/" left the whole "C:\\Users\\...\\DRV_MktData_FX_Vol_20240228.xls"
        # and this passed on macOS and failed on the build machine.
        self.assertEqual([Path(r["file"]).name for r in rows],
                         [f.name for f in b.files])


class TestReferenceFiles(unittest.TestCase):
    """The desk's own files reproduced grid for grid.

    An overlay carrying every number in a reference file, laid over the
    shipped book with the seeded tables, has to come back out as that file:
    the same pairs in the same order, the same tenor labels, the same
    values.  Murex carries the mid alone and COS the bid alone, so the two
    are the overlay's numbers straight through; the wings likewise.  What
    this pins is the container -- and that the overlay is not confined to
    the book, which holds 24 of the 32 pairs.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.wb = cls.tmp / "vol_marks.xlsx"
        shutil.copy(WORKBOOK, cls.wb)
        tabs = publish.seed_tables({})
        rows = configsheets.read_rows(cls.wb, "SPREADS", required=("tenor", "default"))
        tabs["SPREADS"] = [dict(r.cells, cos=0.8) for r in rows]
        session.write_config_tabs(cls.wb, tabs)
        cls.book = Book.from_excel(cls.wb, ASOF).load_all()
        cls.tables = publish.ExportTables.load(cls.wb)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _xls(self, blob):
        try:
            import xlrd
        except ImportError:
            self.skipTest("xlrd is not installed")
        ws = xlrd.open_workbook(file_contents=blob).sheet_by_index(0)
        return [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]

    def test_the_murex_files_come_back_out_as_they_went_in(self):
        vol = self._xls((REFERENCE / "DRV_MktData_FX_Vol_20260910.xls").read_bytes())
        broker = self._xls((REFERENCE / "DRV_MktData_FX_Broker_20260910.xls").read_bytes())
        wings: dict[tuple[str, str], dict[str, float]] = {}
        for pair, tenor, ordinate, bf, _, rr, _ in broker[1:]:
            key = (pair.replace("/", ""), tenor)
            w = wings.setdefault(key, {})
            if int(ordinate) == 25:
                w["bf25"], w["rr25"] = bf, rr
            else:
                w["bf10"], w["rr10"] = bf, rr
        lines = ["pair,tenor,atm,rr25,rr10,bf25,bf10"]
        for pair, tenor, bid, _ in vol[1:]:
            w = wings[(pair.replace("/", ""), tenor)]
            lines.append(f"{pair},{tenor},{bid},{w['rr25']},{w['rr10']},{w['bf25']},{w['bf10']}")
        ov = overlay.parse("\n".join(lines) + "\n", path="murex-as-overlay.csv")
        self.assertEqual(len(ov.rows), 32 * 11)
        # Every pair from the overlay, even the ones the book holds.
        sources = {p: "overlay" for p in ov.pairs}
        # One build, both files: the destination is the pair of them.
        b = publish.build("murex", self.book, self.tables, source="overlay", overlay=ov,
                          sources=sources)
        self.assertTrue(b.ok, b.refused[:5])
        self.assertEqual(b.preflight["from_overlay"], 32 * 11)
        self.assertEqual(b.preflight["from_book"], 0)
        self.assertEqual(sorted(b.preflight["sources"]["overlay"]), sorted(ov.pairs))
        self.assertEqual([f.name for f in b.files],
                         ["DRV_MktData_FX_Vol_20240228.xls",
                          "DRV_MktData_FX_Broker_20240228.xls"])
        for f, ref in zip(b.files, (vol, broker)):
            mine = self._xls(f.body)
            self.assertEqual(mine[0], ref[0])
            self.assertEqual(len(mine), len(ref))
            for got, want in zip(mine[1:], ref[1:]):
                self.assertEqual(got[:2], want[:2])
                for g, w in zip(got[2:], want[2:]):
                    self.assertAlmostEqual(g, w, places=6, msg=(f.name, want))

    def test_the_cos_file_comes_back_out_as_it_went_in(self):
        ref = (REFERENCE / "COS_86830_Bid.csv").read_text(encoding="utf-8")
        lines = ["pair,tenor,atm_bid,atm_ask"]
        rows = [ln.split(",") for ln in ref.splitlines() if ln.strip()]
        tenors = rows[0][1:]
        for row in rows[1:]:
            pair = row[0].replace("/", "").replace("CNY", "CNH")
            for t, v in zip(tenors, row[1:]):
                lines.append(f"{pair},{t},{v},{float(v) + 1.0}")
        ov = overlay.parse("\n".join(lines) + "\n", path="cos-as-overlay.csv")
        b = publish.build("cos", self.book, self.tables, source="overlay", overlay=ov)
        self.assertTrue(b.ok, b.refused[:5])
        got = b.file_bytes.decode("utf-8").splitlines()
        want = ref.splitlines()
        self.assertEqual(got[0], want[0])
        self.assertEqual([ln.split(",")[0] for ln in got], [ln.split(",")[0] for ln in want])
        for g, w in zip(got[1:], want[1:]):
            self.assertEqual([f"{float(x):.2f}" for x in g.split(",")[1:]],
                             [f"{float(x):.2f}" for x in w.split(",")[1:]], w)
        self.assertEqual(b.file_name, "COS_86830_Bid.csv")

    def test_the_bloomberg_pair_list_and_tickers_are_the_sheets(self):
        """Every one of the desk's 33 blocks, in its order, and the ticker in
        every one of its 3,610 formula cells, from the rule and its one
        exception."""
        import openpyxl
        from volkit import exportseed
        ref = openpyxl.load_workbook(REFERENCE / "BCFO_Vols_bbg_output.xlsx")
        order, tickers = [], {}
        for name in ("G7 Output", "G7 Cross Output", "EM PM Output"):
            ws = ref[name]
            r = 1
            while r <= ws.max_row:
                pair = ws.cell(row=r, column=1).value
                if isinstance(pair, str) and len(pair) == 6 and ws.cell(row=r + 1, column=1).value == "Bid":
                    order.append(pair)
                    rr = r + 2
                    while rr <= ws.max_row and ws.cell(row=rr, column=1).value not in (None, ""):
                        tenor = ws.cell(row=rr, column=1).value
                        tickers[(pair, tenor)] = [ws.cell(row=rr, column=c).value
                                                  for c in (15, 18, 21, 24, 27)]
                        rr += 1
                    r = rr
                else:
                    r += 1
        self.assertEqual(tuple(order), exportseed.BLOOMBERG_PAIRS)
        self.assertEqual([e.pair for e in self.tables.pairs_for("bloomberg")], order)
        self.assertEqual(len(tickers), 33 * 11 - 2)
        n = 0
        for (pair, tenor), want in tickers.items():
            got = [publish.bbg_ticker(pair, i, "O/N" if tenor == "ON" else tenor)
                   for i in publish.INSTRUMENTS]
            self.assertEqual(got, want, (pair, tenor))
            n += 10                                  # a BID and an ASK cell each
        self.assertEqual(n, 3610)


class TestOverlay(_Fixture):
    def test_the_file_is_read_with_the_workbooks_spellings_and_hashed(self):
        o = self.overlay
        self.assertEqual(len(o.rows), 16)
        self.assertEqual(o.pairs, ["AUDHKD", "EURUSD", "HKDCNH", "USDJPY"])
        self.assertEqual(o.columns, ["pair", "tenor", "atm", "rr25", "rr10", "bf25", "bf10"])
        self.assertEqual(len(o.sha256), 64)
        row = o.get("usdjpy", "1m")
        self.assertEqual(row.values, {"rr25": -2.5})
        self.assertEqual(o.get("AUDHKD", "on").values["atm"], 8.0)
        with self.assertRaises(overlay.OverlayError):
            overlay.parse("pair,atm\nUSDJPY,9\n")
        with self.assertRaises(overlay.OverlayError) as ctx:
            overlay.parse("pair,tenor,atm_bid,atm_ask\nUSDJPY,1M,9.0,8.0\n")
        self.assertIn("above", str(ctx.exception))
        with self.assertRaises(overlay.OverlayError) as ctx:
            overlay.parse("pair,tenor,atm\nUSDJPY,1M,nine\n")
        self.assertIn("not a number", str(ctx.exception))
        # A file on disk reads the same as a paste, and an xlsx the same again.
        p = self.tmp / "ov.csv"
        p.write_text(OVERLAY, encoding="utf-8")
        self.assertEqual(overlay.load(p).rows.keys(), o.rows.keys())
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for line in OVERLAY.splitlines():
            if line.startswith("#"):
                continue
            ws.append([c if c == "" else (float(c) if c.replace(".", "").replace("-", "")
                                                        .isdigit() else c)
                       for c in line.split(",")])
        x = self.tmp / "ov.xlsx"
        wb.save(x)
        self.assertEqual(overlay.load(x).rows.keys(), o.rows.keys())

    def test_a_partial_file_changes_exactly_the_cells_it_names(self):
        plain = publish.build("bloomberg", self.book, self.tables)
        laid = publish.build("bloomberg", self.book, self.tables, source="overlay",
                             overlay=self.overlay)
        self.assertTrue(laid.ok, laid.refused)
        a = {(q.pair, q.tenor): q for q in plain.quotes}
        b = {(q.pair, q.tenor): q for q in laid.quotes}
        self.assertEqual(a.keys(), b.keys())
        for key in a:
            x, y = a[key], b[key]
            if key == ("USDJPY", "1M"):
                self.assertEqual(y.rr25, -2.5)
                self.assertEqual(y.source, "overlay")
                self.assertIn("from the book", y.origin)
                for f in ("atm", "rr10", "bf25", "bf10"):
                    self.assertEqual(getattr(x, f), getattr(y, f))
            else:
                self.assertEqual(x.to_dict(), y.to_dict(), key)
        pf = laid.preflight
        self.assertEqual(pf["from_overlay"], 1)
        self.assertEqual(pf["fell_through"], 1)
        self.assertEqual(len(pf["diffs"]), 1)
        self.assertEqual(pf["diffs"][0]["field"], "rr25")
        self.assertAlmostEqual(pf["diffs"][0]["move"], -2.5 - a[("USDJPY", "1M")].rr25)
        # A tolerance turns that move into a refusal, by name.
        tight = publish.build("bloomberg", self.book, self.tables, source="overlay",
                              overlay=self.overlay, tolerance=0.1)
        self.assertFalse(tight.ok)
        self.assertTrue(any("USDJPY 1M rr25" in r for r in tight.refused), tight.refused)

    def test_the_book_does_not_constrain_it_but_the_channel_does(self):
        """AUDHKD is nowhere in the book and goes out anyway; the EURUSD 3Y
        row and the HKDCNH rows are ignored and counted, because Bloomberg
        does not publish them here."""
        b = publish.build("kace", self.book, self.tables, source="overlay",
                          overlay=self.overlay)
        self.assertTrue(b.ok, b.refused)
        pairs = {q.pair for q in b.quotes}
        self.assertEqual(pairs, {"USDCNH", "AUDHKD"})
        hk = [q for q in b.quotes if q.pair == "AUDHKD"]
        self.assertEqual(len(hk), 9)
        self.assertTrue(all(q.source == "overlay" for q in hk))
        self.assertEqual(sorted(b.preflight["ignored"]),
                         sorted(["USDJPY 1M", "HKDCNH 1W", "HKDCNH 2W", "HKDCNH 1M",
                                 "HKDCNH 3M", "HKDCNH 6M", "EURUSD 3Y"]))
        # kACE: a pair outside the book goes as pillars only and says so;
        # a pair read whole off the book keeps its daily series.
        self.assertTrue(b.feeds["AUDHKD"].pillars_only)
        self.assertFalse(b.feeds["USDCNH"].pillars_only)
        self.assertEqual(len(b.feeds["AUDHKD"].pillars), 9)
        self.assertTrue(any("key tenors only" in n for n in b.notes))
        xml = b.feeds["AUDHKD"].xml("u", "p")
        self.assertIn('<field name="Currency" value="AUD"/>', xml)
        self.assertIn('<field name="CtrCcy" value="HKD"/>', xml)
        self.assertEqual(xml.count("<node "), 45)

    def test_a_value_neither_source_has_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            wb = _workbook(Path(tmp))
            tables = publish.ExportTables.load(wb)
            tables.export_pairs["bloomberg"].append(publish.PairEntry(
                pair="AUDHKD", label="AUDHKD", feed_from="AUDHKD", last_tenor=""))
            tables.market_widths["AUDHKD"] = {t: 0.5 for t in publish.ELEVEN}
            # The overlay carries AUDHKD to 1Y; Bloomberg wants 2Y and 3Y too.
            b = publish.build("bloomberg", self.book, tables, source="overlay",
                              overlay=self.overlay)
            self.assertFalse(b.ok)
            self.assertTrue(any(r.startswith("AUDHKD: no 2Y, 3Y") for r in b.refused), b.refused)
            cov = {c["pair"]: c for c in b.preflight["coverage"]}
            self.assertEqual(cov["AUDHKD"]["missing"], ["2Y", "3Y"])
            self.assertFalse(cov["AUDHKD"]["in_book"])
            self.assertEqual(cov["AUDHKD"]["tenors"]["1M"], "overlay")
            # Without the overlay the whole pair is missing, and says so.
            plain = publish.build("bloomberg", self.book, tables)
            self.assertTrue(any("AUDHKD" in r and "the book has no AUDHKD" in r
                                for r in plain.refused), plain.refused)

    def test_a_run_reads_each_pair_from_the_source_it_was_told(self):
        """Not all from one or all from the other: USDJPY from the book although
        the overlay has a row for it, AUDHKD from the overlay, and the
        coverage names which pair came from where."""
        b = publish.build("kace", self.book, self.tables, source="overlay",
                          overlay=self.overlay, sources={"USDCNH": "book", "AUDHKD": "overlay"})
        self.assertTrue(b.ok, b.refused)
        self.assertEqual(b.preflight["sources"], {"book": ["USDCNH"], "overlay": ["AUDHKD"]})
        cov = {c["pair"]: c for c in b.preflight["coverage"]}
        self.assertEqual(cov["USDCNH"]["source"], "book")
        self.assertEqual(cov["AUDHKD"]["source"], "overlay")
        self.assertEqual(cov["AUDHKD"]["from"], ["overlay"])
        # Bloomberg: USDJPY told to read the book leaves its overlay row unused,
        # and the preflight says so rather than dropping it silently.
        laid = publish.build("bloomberg", self.book, self.tables, source="book",
                             overlay=self.overlay)
        self.assertTrue(laid.ok, laid.refused)
        self.assertEqual(laid.preflight["left_on_book"], ["USDJPY 1M"])
        self.assertEqual(laid.preflight["from_overlay"], 0)
        self.assertEqual(laid.source, "book")
        self.assertIsNone(laid.overlay)
        plain = publish.build("bloomberg", self.book, self.tables)
        self.assertEqual([q.to_dict() for q in laid.quotes], [q.to_dict() for q in plain.quotes])
        # A pair sent to the overlay with no rows there is a book pair in fact.
        mixed = publish.build("bloomberg", self.book, self.tables, source="book",
                              overlay=self.overlay, sources={"AUDJPY": "overlay"})
        self.assertEqual({c["pair"]: c["from"] for c in mixed.preflight["coverage"]}["AUDJPY"],
                         ["book"])
        with self.assertRaises(publish.PublishError):
            publish.build("bloomberg", self.book, self.tables, sources={"USDJPY": "overlay"})

    def test_the_comparison_puts_both_sides_at_the_channels_widths(self):
        cmp = publish.compare("bloomberg", self.book, self.tables, self.overlay)
        self.assertEqual(cmp["pairs"], ["USDJPY"])
        rows = {(r["pair"], r["tenor"]): r for r in cmp["rows"]}
        r = rows[("USDJPY", "1M")]
        self.assertAlmostEqual(r["diff"]["rr25"], -2.5 - r["book"]["rr25"])
        self.assertAlmostEqual(r["diff"]["rr25_bid"], r["diff"]["rr25"])
        self.assertAlmostEqual(r["diff"]["mid"], 0.0)
        self.assertAlmostEqual(r["diff"]["bid"], 0.0)
        # Both sides at the same width and shade: the book's bid is its
        # shaded mid less half the market width, and so is the overlay's.
        self.assertAlmostEqual(r["book"]["ask"] - r["book"]["bid"], 0.7)
        self.assertAlmostEqual(r["overlay"]["ask"] - r["overlay"]["bid"], 0.7)
        self.assertFalse(r["same"])
        self.assertTrue(rows[("USDJPY", "1W")]["same"])
        by = {p["pair"]: p for p in cmp["by_pair"]}
        self.assertEqual(by["USDJPY"]["compared"], 9)
        self.assertAlmostEqual(by["USDJPY"]["max_mid"], 0.0)
        # The overlay's own two-way is compared as given.
        two = overlay.parse("pair,tenor,atm_bid,atm_ask\nUSDCNH,1W,4.0,4.5\n")
        cmp = publish.compare("cos", self.book, self.tables, two)
        r = cmp["rows"][0]
        self.assertEqual((r["overlay"]["bid"], r["overlay"]["ask"]), (4.0, 4.5))
        self.assertAlmostEqual(r["book"]["ask"] - r["book"]["bid"], 0.8)
        with self.assertRaises(publish.PublishError):
            publish.compare("murex", self.book, self.tables, two)   # no USDCNH there

    def test_a_row_with_its_own_two_way_bypasses_the_tier(self):
        o = overlay.parse("pair,tenor,atm_bid,atm_ask\nUSDCNH,1W,4.0,4.5\n")
        b = publish.build("cos", self.book, self.tables, source="overlay", overlay=o)
        q = next(q for q in b.quotes if q.pair == "USDCNH" and q.tenor == "1W")
        self.assertEqual((q.bid, q.ask), (4.0, 4.5))
        # The two-way is the file's; the wings fell through to the book.
        self.assertTrue(q.origin.startswith("overlay two-way ("), q.origin)
        self.assertIn("from the book", q.origin)
        self.assertIn("own two-way", q.width_from)
        self.assertTrue(q.to_dict()["two_way_given"])

    def test_a_session_save_takes_the_intersection_and_reports_the_rest(self):
        report = overlay.overflow_report(self.overlay, self.book)
        self.assertEqual((report["inside"], report["outside"], report["rows"]), (1, 15, 16))
        self.assertEqual(report["pairs"], ["AUDHKD", "HKDCNH"])
        self.assertEqual(report["tenors"], ["3Y"])
        self.assertIn("15 outside it", report["message"])


class TestOverlayOnTheService(unittest.TestCase):
    """Export only versus applied to the session, on the server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.wb = _workbook(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def service(self, **kw):
        from volkit.webapp import BookService
        return BookService(str(self.wb), ASOF, kace_user="feeuser", kace_password="pw",
                           kace_log_path=str(self.tmp / "publish_log.jsonl"),
                           session_path=str(self.tmp / "vol_session.json"),
                           export_dir=str(self.tmp / "exports"), **kw)

    def test_export_only_leaves_every_other_screen_on_the_marks(self):
        svc = self.service()
        before = svc.book["USDJPY"].quoted_marks()
        out = svc.export_overlay({"action": "load", "text": OVERLAY, "path": "run.csv"})
        self.assertTrue(out["ok"])
        self.assertTrue(svc.overlay_state()["loaded"])
        self.assertFalse(svc.overlay_state()["applied"])
        # The book is untouched: same quotes, nothing dirty, the kACE tab
        # posts the marks.
        self.assertEqual(svc.book["USDJPY"].quoted_marks(), before)
        self.assertEqual(svc.book["USDJPY"].quote_overwrites, {})
        feed = svc.kace({"pair": "USDJPY", "scenario": "Test", "pillars_only": "1"})
        one_m = next(p for p in feed["pillars"] if p["tenor"] == "1M")
        self.assertNotEqual(one_m["rr25"], -2.5)
        # The export, though, sees it.
        built = svc.export_build({"channel": "bloomberg", "sources": {"USDJPY": "overlay"}})
        q = next(q for q in built["quotes"] if q["pair"] == "USDJPY" and q["tenor"] == "1M")
        self.assertEqual(q["rr25"], -2.5)
        self.assertEqual(built["overlay"]["rows"], 16)
        self.assertEqual(built["preflight"]["sources"]["overlay"], ["USDJPY"])
        # The comparison route, and the state's per-pair view of the file.
        cmp = svc.export_compare({"channel": "bloomberg"})
        self.assertEqual(cmp["pairs"], ["USDJPY"])
        per = {e["pair"]: e for e in svc.overlay_state()["per_pair"]}
        self.assertTrue(per["USDJPY"]["in_book"])
        self.assertFalse(per["AUDHKD"]["in_book"])
        self.assertEqual(per["EURUSD"]["tenors_in_book"], [])
        # The state every screen reads says an overlay is loaded, export only.
        self.assertTrue(svc.state()["export"]["overlay"]["loaded"])
        svc.export_overlay({"action": "clear"})
        self.assertFalse(svc.overlay_state()["loaded"])

    def test_applied_it_is_the_book_until_revert_and_the_workbook_write_refuses(self):
        svc = self.service()
        svc.export_overlay({"action": "load", "text": OVERLAY, "path": "run.csv"})
        snapshot = session.capture(svc.book)
        # Only the ticked pairs go on; a pair the overlay does not carry is
        # refused by name, and a pair the book cannot hold applies nothing.
        with self.assertRaises(publish.PublishError):
            svc.export_overlay({"action": "apply", "pairs": ["GBPUSD"]})
        nothing = svc.export_overlay({"action": "apply", "pairs": ["AUDHKD"]})
        self.assertEqual(nothing["applied"], [])
        self.assertIsNone(svc.overlay_applied)
        self.assertEqual(list(self.tmp.glob("pre-overlay-*.json")), [])
        out = svc.export_overlay({"action": "apply", "pairs": ["USDJPY"]})
        self.assertTrue(svc.overlay_applied)
        self.assertEqual(svc.overlay_applied["pairs"], ["USDJPY"])
        self.assertEqual(out["applied"], ["USDJPY 1M"])
        st = svc.overlay_state()
        self.assertEqual([e["applied"] for e in st["per_pair"] if e["pair"] == "USDJPY"], [True])
        self.assertTrue(Path(out["wrote"]).name.startswith("pre-overlay-"))
        self.assertEqual(Path(out["wrote"]).parent, self.tmp)
        self.assertIn("15 outside", out["message"])
        # Every screen sees it now, the kACE feed tab included.
        self.assertAlmostEqual(svc.book["USDJPY"].quote_overwrites["1M"]["rr_25"], -0.025)
        feed = svc.kace({"pair": "USDJPY", "scenario": "Test", "pillars_only": "1"})
        one_m = next(p for p in feed["pillars"] if p["tenor"] == "1M")
        self.assertAlmostEqual(one_m["rr25"], -2.5)
        # The workbook write path refuses by name while it is on the book.
        svc.session_save({"path": str(self.tmp / "s.json")})
        with self.assertRaises(ValueError) as ctx:
            svc.session_export({"path": str(self.tmp / "s.json")})
        self.assertIn("run.csv", str(ctx.exception))
        self.assertIn("Revert", str(ctx.exception))
        # The save said what it dropped.
        saved = svc.session_save({"path": str(self.tmp / "s.json")})
        self.assertIn("saved 1 of 16 overlay rows", saved["note"])
        self.assertIn("AUDHKD", saved["note"])
        # A build that asks for the overlay on an applied pair reads the
        # book -- the rows are on it -- and says so.
        built = svc.export_build({"channel": "bloomberg", "sources": {"USDJPY": "overlay"}})
        self.assertIn("USDJPY", built["preflight"]["sources"]["book"])
        self.assertEqual(built["preflight"]["sources"]["overlay"], [])
        self.assertTrue(any("written over the book" in n for n in built["notes"]))
        q = next(q for q in built["quotes"] if q["pair"] == "USDJPY" and q["tenor"] == "1M")
        self.assertEqual(q["rr25"], -2.5)
        # Revert restores the captured session exactly.
        back = svc.export_overlay({"action": "revert"})
        self.assertFalse(svc.overlay_applied)
        self.assertEqual(svc.book["USDJPY"].quote_overwrites, {})
        # Byte for byte, but for an event label the session round trip has
        # never carried (the workbook's EVENTS row has none and the pair's
        # schedule names the date): every mark is what it was.
        def marks(doc):
            return {p: {k: v for k, v in blk.items() if k != "events"}
                    for p, blk in doc["pairs"].items()}
        self.assertEqual(marks(session.capture(svc.book)), marks(snapshot))
        self.assertTrue(svc.overlay_state()["loaded"])   # the file stays loaded
        svc.session_export({"path": str(self.tmp / "s.json"),
                            "out": str(self.tmp / "written.xlsx")})
        with self.assertRaises(publish.PublishError):
            svc.export_overlay({"action": "revert"})

    def test_a_reload_that_discards_the_session_drops_the_applied_overlay(self):
        svc = self.service()
        svc.export_overlay({"action": "load", "text": OVERLAY, "path": "run.csv"})
        svc.export_overlay({"action": "apply", "pairs": ["USDJPY"]})
        svc.reload(discard=True)
        self.assertIsNone(svc.overlay_applied)
        self.assertTrue(svc.overlay_state()["loaded"])
        self.assertEqual(svc.book["USDJPY"].quote_overwrites, {})


class TestExportScreen(unittest.TestCase):
    """The routes behind the Vol exporting bulk screen, end to end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.wb = _workbook(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def service(self, **kw):
        from volkit.webapp import BookService
        return BookService(str(self.wb), ASOF, kace_user="feeuser", kace_password="pw",
                           kace_log_path=str(self.tmp / "publish_log.jsonl"),
                           export_dir=str(self.tmp / "exports"), **kw)

    def test_the_state_names_every_channel_its_pairs_and_the_tables(self):
        svc = self.service()
        st = svc.export_state()
        self.assertEqual([c["key"] for c in st["channels"]], list(publish.CHANNELS))
        cos = next(c for c in st["channels"] if c["key"] == "cos")
        self.assertEqual([p["pair"] for p in cos["pairs"]], ["USDCNH", "EURCNH", "HKDCNH"])
        self.assertEqual(cos["pairs"][2]["feed_from"], "CNHHKD")
        self.assertTrue(cos["pairs"][2]["in_book"])
        kace_ch = next(c for c in st["channels"] if c["key"] == "kace")
        self.assertFalse(kace_ch["pairs"][1]["in_book"])          # AUDHKD
        self.assertFalse(kace_ch["pairs"][1]["in_overlay"])
        self.assertEqual(kace_ch["tenors"][0], "O/N")
        svc.export_overlay({"action": "load", "text": OVERLAY, "path": "run.csv"})
        kace_ch = next(c for c in svc.export_state()["channels"] if c["key"] == "kace")
        self.assertTrue(kace_ch["pairs"][1]["in_overlay"])
        # With no EXPORT_PAIRS rows of its own, kACE publishes the book's pairs.
        bare = publish.ExportTables.load(self.wb)
        bare.export_pairs.pop("kace")
        self.assertEqual([e.pair for e in bare.pairs_for("kace", svc.book)], svc.book.pairs)
        self.assertTrue(all(st["tables"]["present"].values()))
        # The COS width is COS_WIDTHS' now, not a tier of SPREADS.
        self.assertEqual(st["tables"]["tiers"], ["default", "wide", "thin"])
        # The config route says which tabs the export screen edits.
        tabs = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}
        for sheet in configsheets.EXPORT_TABS:
            self.assertEqual(tabs[sheet]["where"], "export")
        self.assertEqual(tabs["PEG_BANDS"]["where"], "config")

    def test_a_file_run_writes_into_the_export_folder_and_the_log(self):
        svc = self.service()
        built = svc.export_build({"channel": "murex", "pairs": ["USDJPY"]})
        self.assertTrue(built["ok"], built["refused"])
        self.assertEqual(built["rows"], 9)
        self.assertEqual([f["name"] for f in built["files"]],
                         ["DRV_MktData_FX_Vol_20240228.xls",
                          "DRV_MktData_FX_Broker_20240228.xls"])
        # The date disagrees with the machine's, so the run asks first.
        with self.assertRaises(publish.PublishError):
            svc.export_run({"channel": "murex", "pairs": ["USDJPY"]})
        out = svc.export_run({"channel": "murex", "pairs": ["USDJPY"], "confirm_date": True})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["paths"]), 2)
        for path in out["paths"]:
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).parent, self.tmp / "exports")
        self.assertEqual(out["entries"][-1]["channel"], "murex")
        self.assertEqual(out["entries"][-1]["pairs"], ["USDJPY"])
        # The download names which of the two it wants; naming neither is refused.
        for path, f in zip(out["paths"], built["files"]):
            name, body, kind = svc.export_download({"channel": "murex", "pairs": "USDJPY",
                                                    "file": f["name"]})
            self.assertEqual(name, f["name"])
            self.assertEqual(body, Path(path).read_bytes())
            self.assertEqual(kind, "application/vnd.ms-excel")
        with self.assertRaises(publish.PublishError) as ctx:
            svc.export_download({"channel": "murex", "pairs": "USDJPY"})
        self.assertIn("DRV_MktData_FX_Vol_20240228.xls", str(ctx.exception))
        # A one-file destination needs no name.
        name, _, kind = svc.export_download({"channel": "cos", "pairs": "USDCNH"})
        self.assertEqual((name, kind), ("COS_86830_Bid.csv", "text/csv"))
        # A pair not on the channel's list is refused by name.
        with self.assertRaises(publish.PublishError) as ctx:
            svc.export_build({"channel": "murex", "pairs": ["GBPUSD"]})
        self.assertIn("GBPUSD", str(ctx.exception))

    def test_a_kace_run_posts_one_message_per_pair_and_records_the_source(self):
        posted = []
        reply = ('<gfi_message><header><processingTime>7</processingTime></header>'
                 '<body><response>ok</response></body></gfi_message>')

        def opener(url, body, headers, *, timeout, ca, insecure):
            posted.append((url, body))
            return 200, reply.encode()

        svc = self.service(kace_url="https://kace.example/xmlposter")
        svc.kace_opener = opener
        svc.export_overlay({"action": "load", "text": OVERLAY, "path": "run.csv"})
        dry = svc.export_run({"channel": "kace", "source": "overlay", "scenario": "Test",
                              "dry_run": True})
        self.assertEqual(posted, [])
        self.assertEqual([r["pair"] for r in dry["results"]], ["USDCNH", "AUDHKD"])
        self.assertEqual([r["source"] for r in dry["results"]], ["book", "overlay"])
        out = svc.export_run({"channel": "kace", "source": "overlay", "scenario": "Test"})
        self.assertEqual(len(posted), 2)
        self.assertEqual((out["sent"], out["failed"]), (2, 0))
        entries = [e for e in svc.kace_log.entries(limit=50) if e["channel"] == "kace"]
        self.assertEqual([e["pair"] for e in entries[-2:]], ["USDCNH", "AUDHKD"])
        self.assertEqual(entries[-1]["source"]["file"], "run.csv")
        self.assertEqual(entries[-1]["source"]["rows"], 16)
        self.assertEqual(entries[-2]["source"], "marks")     # USDCNH came off the book
        self.assertTrue(entries[-1]["pillars_only"])
        self.assertFalse(entries[-2]["pillars_only"])
        # A refused build is on the record too, with what refused it.
        tables = svc.export_tables
        del tables.market_widths["XAUUSD"]
        refused = svc.export_run({"channel": "bloomberg"})
        self.assertFalse(refused["ok"])
        self.assertEqual(svc.kace_log.entries(limit=1)[0]["channel"], "bloomberg")
        self.assertIn("XAUUSD", svc.kace_log.entries(limit=1)[0]["message"])

    def test_the_old_kace_log_is_carried_over_once_with_its_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / kace.LEGACY_LOG_FILENAME
            old.write_text(json.dumps({"at": "2026-09-01T09:00:00", "pair": "USDCNH",
                                       "ok": True, "message": "taken"}) + "\n",
                           encoding="utf-8")
            log = kace.PostLog.at(Path(tmp) / kace.POST_LOG_FILENAME)
            rows = log.entries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["channel"], "kace")
            self.assertFalse(old.exists())
            self.assertTrue((Path(tmp) / (kace.LEGACY_LOG_FILENAME + ".migrated")).exists())
            # Not again: the log exists now.
            (Path(tmp) / kace.LEGACY_LOG_FILENAME).write_text("{}\n", encoding="utf-8")
            self.assertEqual(kace.PostLog.at(log.path).carry_over(), 0)
            self.assertEqual(kace.POST_LOG_FILENAME, "publish_log.jsonl")

    def test_seeding_puts_the_missing_tables_into_the_session(self):
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            wb = Path(tmp) / "vol_marks.xlsx"
            shutil.copy(WORKBOOK, wb)
            svc = BookService(str(wb), ASOF)
            self.assertFalse(svc.export_tables.summary()["present"]["SHADES"])
            out = svc.export_seed_tables({})
            self.assertEqual(out["wrote"]["tabs"], ["ADD_UPS", "COS_WIDTHS", "CROSS_CORR",
                                                    "EXPORT_PAIRS", "MARKET_WIDTHS",
                                                    "SHADES", "WING_WIDTHS"])
            self.assertTrue(svc.export_tables.summary()["present"]["SHADES"])
            self.assertEqual(svc.export_tables.shade("bloomberg", "CHFJPY")[0], -0.2)
            self.assertEqual(svc.export_tables.shade("bloomberg", "USDCNH"),
                             (0.0, "bloomberg USDCNH shade"))
            self.assertEqual(svc.export_tables.cos_width("USDJPY", "1W")[0], 1.0)
            self.assertEqual(svc.export_tables.cos_width("EURUSD", "1W")[0], 0.8)
            self.assertEqual(svc.export_tables.wing_width("AUDCAD", "O/N")[0],
                             {"rr25": 3, "rr10": 6, "bf25": 2.1, "bf10": 4.2})
            self.assertAlmostEqual(svc.export_tables.market_width("AUDHKD", "1M")[0], 0.75)
            self.assertAlmostEqual(svc.export_tables.market_width("AUDCAD", "1M")[0], 0.48)
            self.assertEqual(sorted(svc.config_edits), out["wrote"]["tabs"])
            self.assertTrue(svc.dirty)

    def test_the_command_line_writes_the_same_file(self):
        import io
        import sys
        from volkit.cli import main
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            # The old name still reaches the merged destination.
            rc = main(["export", "murex_broker", "-w", str(self.wb), "--asof", "2024-02-28T12:00",
                       "--pairs", "AUDUSD", "--out-dir", str(self.tmp / "cli"),
                       "--confirm-date", "--kace-log", str(self.tmp / "cli-log.jsonl")])
        finally:
            sys.stderr = old
        self.assertEqual(rc, 0, err.getvalue())
        book = Book.from_excel(self.wb, ASOF).load_all()
        b = publish.build("murex", book, publish.ExportTables.load(self.wb), pairs=["AUDUSD"])
        for f in b.files:
            written = self.tmp / "cli" / f.name
            self.assertTrue(written.exists(), err.getvalue())
            self.assertEqual(written.read_bytes(), f.body)
        self.assertIn("Murex: 1 pair(s)", err.getvalue())


class TestMarkedCorrelation(unittest.TestCase):
    """`CROSS_CORR`: a cross's correlation typed rung by rung.

    The desk's own CNH cross workbook carries a correlation per tenor between
    the two leg volatilities and the cross -- USDCNH against AUDUSD is -0.700
    out to 1M, then -0.675, -0.650, -0.625, -0.600 -- and an exponential
    fitted through that ladder reproduces none of its rungs exactly.
    """

    def test_the_ladder_interpolates_in_time_and_is_flat_outside_it(self):
        from volkit import cross
        c = cross.marked_correlation("AUDCNH", {"1M": -0.7, "3M": -0.65, "1Y": -0.6})
        self.assertEqual(c.marks, {"1M": -0.7, "3M": -0.65, "1Y": -0.6})
        self.assertAlmostEqual(float(c(0.25)), -0.65)
        # Between two rungs, linear in time; outside them, flat.
        mid = float(c((1 / 12 + 0.25) / 2))
        self.assertTrue(-0.7 < mid < -0.65, mid)
        self.assertAlmostEqual(float(c(0.0)), -0.7)
        self.assertAlmostEqual(float(c(30.0)), -0.6)
        # One rung is a flat correlation, not an error.
        self.assertAlmostEqual(float(cross.marked_correlation("X", {"1Y": 0.4})(5.0)), 0.4)

    def test_a_ladder_that_cannot_be_placed_or_believed_is_refused_by_name(self):
        from volkit import cross
        for marks, word in (({"1M": 1.4}, "[-1, 1]"),
                            ({"banana": 0.1}, "CROSS_CORR"),
                            ({}, "no correlation")):
            with self.assertRaises(ValueError) as ctx:
                cross.marked_correlation("AUDCNH", marks)
            self.assertIn(word, str(ctx.exception))

    def test_the_seeded_ladders_reproduce_the_desks_cross_workbook(self):
        """The rungs `exportseed.CROSS_CORR` carries, put through the triangle
        in `cross.py` with the legs the desk's `Input` grid held on
        2026-09-11, give that workbook's own cross volatilities."""
        import math
        from volkit import cross, exportseed
        legs = {"AUDUSD": {"1M": 6.95, "1Y": 8.45}, "USDCAD": {"1M": 4.62, "1Y": 5.19},
                "USDCHF": {"1M": 6.68, "1Y": 7.52}, "USDJPY": {"1M": 10.41, "1Y": 8.90},
                "EURUSD": {"1M": 4.99, "1Y": 6.30}, "GBPUSD": {"1M": 5.28, "1Y": 7.26},
                "NZDUSD": {"1M": 7.71, "1Y": 8.82}, "USDHKD": {"1M": 0.75, "1Y": 0.75},
                "USDCNH": {"1M": 1.75, "1Y": 3.25}}
        desk = {("AUDCNH", "1M"): 5.8598, ("AUDCNH", "1Y"): 7.0007,
                ("CADCNH", "1M"): 3.9387, ("CADCNH", "1Y"): 4.5421,
                ("CHFCNH", "1M"): 5.9013, ("CHFCNH", "1Y"): 6.5324,
                ("CNHJPY", "1M"): 9.8416, ("CNHJPY", "1Y"): 8.3382,
                ("EURCNH", "1M"): 4.1813, ("EURCNH", "1Y"): 5.3623,
                ("GBPCNH", "1M"): 4.5582, ("GBPCNH", "1Y"): 6.3918,
                ("NZDCNH", "1M"): 7.0490, ("NZDCNH", "1Y"): 7.9994,
                ("CNHHKD", "1M"): 1.6649, ("CNHHKD", "1Y"): 3.1859}
        for (pair, tenor), expected in desk.items():
            a, b = cross.dollar_legs(pair)
            sign = math.prod(cross.infer_leg_signs(pair, a, b))
            rho = exportseed.CROSS_CORR[pair][tenor]
            va, vb = legs[a][tenor], legs[b][tenor]
            got = math.sqrt(va * va + vb * vb - 2.0 * sign * rho * va * vb)
            self.assertAlmostEqual(got, expected, places=3, msg=f"{pair} {tenor}")

    def test_the_book_takes_the_ladder_over_the_fit_and_says_so(self):
        """A pair CROSS_CORR names is built off its rungs, and the warning
        says so, because the marking screen's three correlation boxes are then
        showing numbers nothing is using."""
        from volkit.cross import CrossAtmCurve, MarkedCorrelation
        with tempfile.TemporaryDirectory() as tmp:
            wb = _workbook(Path(tmp))
            book = Book.from_excel(wb, ASOF).load_all()
            atm = book["CNHHKD"].atm
            self.assertIsInstance(atm, CrossAtmCurve)
            self.assertIsInstance(atm.correlation, MarkedCorrelation)
            self.assertEqual(atm.correlation.marks["1M"], 0.325)
            self.assertTrue(any("CNHHKD" in w and "CROSS_CORR" in w for w in book.warnings),
                            book.warnings)
            # Read as the three boxes the screen has always had: the first
            # rung, the last, and no exponential to report.
            self.assertEqual((atm.correlation.initial, atm.correlation.final,
                              atm.correlation.decay), (0.325, 0.2, 0.0))
            # A re-mark by hand replaces the ladder for the session.
            self.assertEqual(atm.set_correlation(0.4, 0.4, 1.0), [])
            self.assertNotIsInstance(atm.correlation, MarkedCorrelation)

    def test_a_workbook_without_the_tab_fits_every_cross_as_before(self):
        from volkit.cross import CorrelationCurve
        book = Book.from_excel(WORKBOOK, ASOF).load_all()
        self.assertEqual(book.cross_correlations, {})
        crosses = [p for p in book.pairs if book.data.pairs[p].is_cross and p in book.surfaces]
        self.assertTrue(crosses)
        for pair in crosses:
            self.assertIsInstance(book[pair].atm.correlation, CorrelationCurve)


if __name__ == "__main__":
    unittest.main()
