"""The kACE feed and the bulk export.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestKaceFeed(unittest.TestCase):
    """The kACE feed (`kace.py`): the XML_poster workbook, done from the book.

    The sheet is the specification, so the first test pins strings the sheet
    itself produced -- `XML_poster_DailyVol_v3.1_USDCNH_JL.xlsx`, recalculated
    and read back -- against a Feed built from the same inputs.  The rest pin
    the three things done differently on purpose (the horizon, `horDate`,
    the spread table naming the pillars) and the desk's conventions.
    """

    # The pillars and widths are a tab of the workbook now, not a file beside it.
    SPREADS = Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx"

    # The sheet's nine pillars (USDCNHData!K3:S11), as of a 2026-01-22 valuation.
    SHEET_PILLARS = [
        ("O/N", date(2026, 1, 23), 1.0, -0.25, -0.45, 0.125, 0.39375),
        ("1W", date(2026, 1, 29), 0.8, -0.225, -0.405, 0.125, 0.39375),
        ("2W", date(2026, 2, 5), 0.6, -0.2125, -0.3825, 0.125, 0.39375),
        ("1M", date(2026, 2, 24), 0.4, -0.2, -0.36, 0.125, 0.39375),
        ("2M", date(2026, 3, 24), 0.3, -0.175, -0.315, 0.1275, 0.401625),
        ("3M", date(2026, 4, 23), 0.3, -0.175, -0.315, 0.15, 0.4725),
        ("6M", date(2026, 7, 23), 0.2, -0.175, -0.35, 0.1875, 0.590625),
        ("9M", date(2026, 10, 22), 0.2, -0.175, -0.3675, 0.2125, 0.669375),
        ("1Y", date(2027, 1, 22), 0.2, -0.156, -0.37, 0.25, 0.7875),
    ]
    # Rows of the sheet's daily series (USDCNHData!A:B), and what its formulas
    # wrote for them.  Row 1 is before every pillar (the ISERROR fallback to
    # the O/N spread); 28 Jan is still on O/N's 1.0; 29 Jan is the 1W expiry
    # and takes 0.8; 5 Feb is the 2W expiry; 30 Jan 2027 is the last row.
    SHEET_DAYS = [
        (date(2026, 1, 23), 2.25820980020178, "0.0175820980020178/0.0275820980020178"),
        (date(2026, 1, 24), 2.38916548538905, "0.0188916548538905/0.0288916548538905"),
        (date(2026, 1, 28), 1.80166787406075, "0.0130166787406075/0.0230166787406075"),
        (date(2026, 1, 29), 2.29288067023934, "0.0189288067023934/0.0269288067023934"),
        (date(2026, 1, 30), 2.25351386681335, "0.0185351386681335/0.0265351386681335"),
        (date(2026, 2, 5), 2.18646548219792, "0.0188646548219792/0.0248646548219792"),
        (date(2026, 2, 6), 2.19092359623445, "0.0189092359623445/0.0249092359623445"),
        (date(2027, 1, 30), 3.77727687403344, "0.0367727687403344/0.0387727687403344"),
    ]

    def _sheet_feed(self):
        from volkit import kace
        daily = {d: v for d, v, _ in self.SHEET_DAYS}
        # Every pillar has to be a row of the series; the sheet's were.
        for tenor, expiry, spread, rr25, rr10, fly25, fly10 in self.SHEET_PILLARS:
            daily.setdefault(expiry, 3.7)
        daily[date(2027, 1, 22)] = 3.76476480984279      # S41 in the sheet
        pillars = [kace.Pillar(tenor=t, expiry=e, spread=sp, atm=daily[e], rr25=a, rr10=b,
                               fly25=c, fly10=d, wings="marks")
                   for t, e, sp, a, b, c, d in self.SHEET_PILLARS]
        return kace.Feed(pair="USDCNH", hor_date=date(2026, 1, 22), cut="NY",
                         source="marks", daily=daily, pillars=pillars)

    @staticmethod
    def _nodes(text):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text.encode("utf-8"))
        return root, {n.get("name"): {f.get("name"): f.get("value") for f in n.findall("field")}
                      for n in root.iter("node")}

    def test_the_message_is_what_the_sheet_wrote(self):
        """Strings the workbook's own formulas produced, reproduced exactly."""
        feed = self._sheet_feed()
        text = feed.xml("feeuser", "password1", timestamp=datetime(2026, 1, 22, 9, tzinfo=UTC))
        root, nodes = self._nodes(text)
        by_day = {n["Maturity"]: n for k, n in nodes.items() if not k.startswith("S")}
        for day, _, want in self.SHEET_DAYS:
            label = f"{day.day:02d} {day:%b} {day.year}"
            self.assertEqual(by_day[label]["Volity"], want, label)
            self.assertEqual(by_day[label]["VolType"], "ATM")
        # The pillar block: five nodes each, in the sheet's order and naming.
        self.assertEqual(nodes["S1"], {"RateType": "Volatility", "Currency": "USD",
                                       "CtrCcy": "CNH", "Maturity": "23 Jan 2026",
                                       "VolType": "ATM",
                                       "Volity": "0.0175820980020178/0.0275820980020178"})
        self.assertEqual(nodes["S2"]["Volity"], "-0.0025")
        self.assertEqual((nodes["S2"]["PctDelta"], nodes["S2"]["VolType"]), ("0.25", "RR"))
        self.assertEqual(nodes["S3"]["Volity"], "-0.0045")
        self.assertEqual((nodes["S3"]["PctDelta"], nodes["S3"]["VolType"]), ("0.10", "RR"))
        self.assertEqual(nodes["S4"]["Volity"], "0.00125")
        self.assertEqual((nodes["S4"]["PctDelta"], nodes["S4"]["VolType"]), ("0.25", "S"))
        self.assertEqual(nodes["S5"]["Volity"], "0.0039375")
        self.assertEqual(nodes["S41"]["Volity"], "0.0366476480984279/0.0386476480984279")
        self.assertEqual(nodes["S42"]["Volity"], "-0.00156")
        self.assertEqual(nodes["S45"]["Volity"], "0.007875")
        self.assertEqual(nodes["S45"]["Maturity"], "22 Jan 2027")
        self.assertEqual(len(nodes), len(feed.daily) + 45)
        # The envelope the poster page takes.
        header = {e.tag: (e.text or "").strip() for e in root.find("header")}
        self.assertEqual(header["username"], "feeuser")
        self.assertEqual(header["password"], "password1")
        self.assertEqual(header["timestamp"], "2026-01-22T09:00:00+00:00")
        action = root.find("body").find("action")
        self.assertEqual(action.get("function"), "RATE_FEED")
        opts = {o.get("name"): o.get("value") for o in action}
        self.assertEqual(opts["scenario"], "Xyz")
        self.assertEqual(opts["horDate"], "22 Jan 2026")
        self.assertNotIn("clearRate", opts)

    def test_the_day_takes_the_spread_of_the_last_pillar_on_or_before_it(self):
        """The sheet's approximate VLOOKUP, and its ISERROR fallback, as a rule."""
        from volkit import kace
        pillars = self._sheet_feed().pillars
        self.assertEqual(kace.spread_for(date(2026, 1, 1), pillars), 1.0)    # before O/N
        self.assertEqual(kace.spread_for(date(2026, 1, 23), pillars), 1.0)   # the O/N expiry
        self.assertEqual(kace.spread_for(date(2026, 1, 28), pillars), 1.0)   # still O/N's
        self.assertEqual(kace.spread_for(date(2026, 1, 29), pillars), 0.8)   # the 1W expiry
        self.assertEqual(kace.spread_for(date(2026, 2, 4), pillars), 0.8)    # 1W's until 2W
        self.assertEqual(kace.spread_for(date(2026, 2, 5), pillars), 0.6)
        self.assertEqual(kace.spread_for(date(2027, 6, 1), pillars), 0.2)    # past 1Y: 1Y's

    def test_a_day_between_two_pillars_can_be_read_across_instead_of_stepped(self):
        """The alternative to the sheet's rule: interpolate, and only between pillars."""
        from volkit import kace
        pillars = self._sheet_feed().pillars
        cross = lambda d: kace.spread_for(d, pillars, interpolate=True)
        # A pillar's own day is the pillar's width either way, and outside the
        # ladder there is nothing to read across to.
        self.assertEqual(cross(date(2026, 1, 1)), 1.0)                    # before O/N
        self.assertEqual(cross(date(2026, 1, 23)), 1.0)                   # the O/N expiry
        self.assertEqual(cross(date(2026, 1, 29)), 0.8)                   # the 1W expiry
        self.assertEqual(cross(date(2027, 6, 1)), 0.2)                    # past 1Y
        # Between them it is a straight line in date: O/N 23 Jan at 1.0 to
        # 1W 29 Jan at 0.8 is six days, so 26 Jan is halfway.
        self.assertAlmostEqual(cross(date(2026, 1, 26)), 0.9)
        self.assertAlmostEqual(cross(date(2026, 1, 24)), 1.0 - 0.2 / 6)
        # And it is monotone between two pillars, where the step rule is flat
        # and then drops.
        walk = [cross(date(2026, 1, 23) + timedelta(days=i)) for i in range(7)]
        self.assertEqual(walk, sorted(walk, reverse=True))
        self.assertEqual(kace.spread_for(date(2026, 1, 26), pillars), 1.0)  # still stepped

    def test_the_multiplier_scales_the_width_and_leaves_the_mid_alone(self):
        from volkit import kace
        book = self._book()
        plain = kace.build(book, "USDCNH", kace.SpreadTable.load(self.SPREADS))
        wide = kace.build(book, "USDCNH", kace.SpreadTable.load(self.SPREADS),
                          multiplier=1.5)
        self.assertEqual(plain.multiplier, 1.0)
        self.assertEqual(wide.multiplier, 1.5)
        for a, b in zip(plain.pillars, wide.pillars):
            self.assertAlmostEqual(b.spread, a.spread * 1.5, msg=a.tenor)
            self.assertAlmostEqual(b.atm, a.atm)
            # The two-way widens around the mark; it does not move it.
            self.assertAlmostEqual((b.bid + b.offer) / 2.0, (a.bid + a.offer) / 2.0)
        self.assertTrue(any("1.5" in n for n in wide.notes))
        self.assertEqual(wide.summary()["multiplier"], 1.5)
        # The daily nodes are written off the multiplied pillars too, so the
        # screen and the message cannot show two different widths.
        self.assertAlmostEqual(kace.spread_for(min(wide.daily), wide.pillars),
                               kace.spread_for(min(plain.daily), plain.pillars) * 1.5)

    def test_a_multiplier_that_is_not_a_positive_number_is_refused(self):
        from volkit import kace
        self.assertEqual(kace.spread_multiplier(None), 1.0)
        self.assertEqual(kace.spread_multiplier(""), 1.0)
        self.assertEqual(kace.spread_multiplier("  "), 1.0)
        self.assertEqual(kace.spread_multiplier("2.5"), 2.5)
        for bad in ("0", "-1", "wide", float("nan"), float("inf")):
            with self.assertRaises(kace.KaceError):
                kace.spread_multiplier(bad)
        with self.assertRaises(kace.KaceError) as ctx:
            kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS),
                       multiplier="0")
        self.assertIn("positive", str(ctx.exception))

    def test_a_day_between_two_pillars_can_be_read_across_instead_of_stepped(self):
        """The alternative to the sheet's rule: interpolate, and only between pillars."""
        from volkit import kace
        pillars = self._sheet_feed().pillars
        cross = lambda d: kace.spread_for(d, pillars, interpolate=True)
        # A pillar's own day is the pillar's width either way, and outside the
        # ladder there is nothing to read across to.
        self.assertEqual(cross(date(2026, 1, 1)), 1.0)                    # before O/N
        self.assertEqual(cross(date(2026, 1, 23)), 1.0)                   # the O/N expiry
        self.assertEqual(cross(date(2026, 1, 29)), 0.8)                   # the 1W expiry
        self.assertEqual(cross(date(2027, 6, 1)), 0.2)                    # past 1Y
        # Between them it is a straight line in date: O/N 23 Jan at 1.0 to
        # 1W 29 Jan at 0.8 is six days, so 26 Jan is halfway.
        self.assertAlmostEqual(cross(date(2026, 1, 26)), 0.9)
        self.assertAlmostEqual(cross(date(2026, 1, 24)), 1.0 - 0.2 / 6)
        # And it is monotone between two pillars, where the step rule is flat
        # and then drops.
        walk = [cross(date(2026, 1, 23) + timedelta(days=i)) for i in range(7)]
        self.assertEqual(walk, sorted(walk, reverse=True))
        self.assertEqual(kace.spread_for(date(2026, 1, 26), pillars), 1.0)  # still stepped

    def test_the_multiplier_scales_the_width_and_leaves_the_mid_alone(self):
        from volkit import kace
        book = self._book()
        plain = kace.build(book, "USDCNH", kace.SpreadTable.load(self.SPREADS))
        wide = kace.build(book, "USDCNH", kace.SpreadTable.load(self.SPREADS),
                          multiplier=1.5)
        self.assertEqual(plain.multiplier, 1.0)
        self.assertEqual(wide.multiplier, 1.5)
        for a, b in zip(plain.pillars, wide.pillars):
            self.assertAlmostEqual(b.spread, a.spread * 1.5, msg=a.tenor)
            self.assertAlmostEqual(b.atm, a.atm)
            # The two-way widens around the mark; it does not move it.
            self.assertAlmostEqual((b.bid + b.offer) / 2.0, (a.bid + a.offer) / 2.0)
        self.assertTrue(any("1.5" in n for n in wide.notes))
        self.assertEqual(wide.summary()["multiplier"], 1.5)
        # The daily nodes are written off the multiplied pillars too, so the
        # screen and the message cannot show two different widths.
        self.assertAlmostEqual(kace.spread_for(min(wide.daily), wide.pillars),
                               kace.spread_for(min(plain.daily), plain.pillars) * 1.5)

    def test_a_multiplier_that_is_not_a_positive_number_is_refused(self):
        from volkit import kace
        self.assertEqual(kace.spread_multiplier(None), 1.0)
        self.assertEqual(kace.spread_multiplier(""), 1.0)
        self.assertEqual(kace.spread_multiplier("  "), 1.0)
        self.assertEqual(kace.spread_multiplier("2.5"), 2.5)
        for bad in ("0", "-1", "wide", float("nan"), float("inf")):
            with self.assertRaises(kace.KaceError):
                kace.spread_multiplier(bad)
        with self.assertRaises(kace.KaceError) as ctx:
            kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS),
                       multiplier="0")
        self.assertIn("positive", str(ctx.exception))

    def test_decimals_are_plain_and_dates_are_english(self):
        from volkit import kace
        self.assertEqual(kace._decimal(0.0175820980020178), "0.0175820980020178")
        self.assertEqual(kace._decimal(5e-05), "0.00005")
        self.assertEqual(kace._decimal(-0.0025), "-0.0025")
        self.assertEqual(kace._date(date(2026, 2, 5)), "05 Feb 2026")

    def test_a_non_positive_bid_is_refused(self):
        from volkit import kace
        feed = self._sheet_feed()
        feed.daily[date(2026, 1, 24)] = 0.4          # spread 1.0 straddles zero
        with self.assertRaises(kace.KaceError) as ctx:
            feed.xml("u", "p")
        self.assertIn("24 Jan 2026", str(ctx.exception))

    def test_no_username_no_message(self):
        from volkit import kace
        with self.assertRaises(kace.KaceError) as ctx:
            self._sheet_feed().xml("", "")
        self.assertIn(kace.ENV_USER, str(ctx.exception))
        with self.assertRaises(kace.KaceError):
            kace.clear_message("USDCNH", date(2026, 1, 22), "", "")

    def test_the_clear_message(self):
        from volkit import kace
        text = kace.clear_message("USDCNH", date(2026, 1, 22), "u", "p",
                                  timestamp=datetime(2026, 1, 22, tzinfo=UTC))
        root, nodes = self._nodes(text)
        opts = {o.get("name"): o.get("value") for o in root.find("body").find("action")}
        self.assertEqual(opts["clearRate"], "true")
        self.assertEqual(opts["horDate"], "22 Jan 2026")
        self.assertEqual(list(nodes), ["USDCNH"])
        self.assertEqual(nodes["USDCNH"], {"RateType": "Volatility", "Currency": "USD",
                                           "CtrCcy": "CNH"})

    # -- the spread table -------------------------------------------------
    def test_the_shipped_table_is_the_sheets_column_l(self):
        """The widths are tiers now, and `default` is still column L.

        The table used to be `pair, tenor, spread`, which tied a width to a
        currency: the same pair could not be posted at two widths and a new
        pair could not be posted at all until somebody typed a ladder for it.
        A tier is a quoting policy, so the pair is chosen on the screen.
        """
        from volkit import kace
        table = kace.SpreadTable.load(self.SPREADS)
        self.assertEqual(table.names, ["default", "wide", "thin"])
        self.assertEqual(table.for_tier(),
                         {"O/N": 1.0, "1W": 0.8, "2W": 0.6, "1M": 0.4, "2M": 0.3, "3M": 0.3,
                          "6M": 0.2, "9M": 0.2, "1Y": 0.2})
        # Every tier posts the same pillars; only the widths differ.
        self.assertEqual(sorted(table.for_tier("wide")), sorted(table.for_tier()))
        self.assertEqual(table.for_tier("wide")["1M"], 0.6)
        # Named however it is typed, and resolved to the tab's own spelling.
        self.assertEqual(table.resolve_tier(" Thin "), "thin")
        self.assertEqual(table.resolve_tier(""), kace.DEFAULT_TIER)
        with self.assertRaises(kace.KaceError) as ctx:
            table.for_tier("EURUSD")
        self.assertIn("eurusd", str(ctx.exception))
        self.assertIn("default, wide, thin", str(ctx.exception))

    def test_a_bad_table_is_refused_by_row(self):
        """The table is a tab now, so a bad cell is reported by the row Excel
        shows -- which is the number somebody goes and looks at."""
        import tempfile
        import openpyxl
        from volkit import kace

        def workbook(path, rows):
            wb = openpyxl.Workbook()
            wb.active.title = kace.SPREADS_SHEET
            for row in rows:
                wb.active.append(row)
            wb.save(path)
            return path

        with tempfile.TemporaryDirectory() as tmp:
            p = workbook(Path(tmp) / "marks.xlsx", [
                ["tenor", "default", "wide"],
                ["on", 1, 1.5],
                ["1W", "wide", 1.2],
                ["1W", 0.8, None],
                ["2W", None, 0.9],
                ["7Q", 0.1, 0.2],
                ["1M", -0.1, 0.2],
            ])
            with self.assertRaises(kace.KaceError) as ctx:
                kace.SpreadTable.load(p)
            msg = str(ctx.exception)
            self.assertIn(kace.SPREADS_SHEET, msg)
            self.assertIn("row 3", msg)           # not a number
            self.assertIn("row 5", msg)           # no default width
            self.assertIn("row 6", msg)           # not a tenor
            self.assertIn("row 7", msg)           # negative

            # Notes above the header, a '#' row anywhere, and a heading or a
            # cell in whatever case somebody typed it.  A tier that leaves a
            # cell blank takes `default` for that tenor, cell by cell, the way
            # a pair column on `Vega Weights` does.
            good = workbook(Path(tmp) / "good.xlsx", [
                ["# the desk's pillars"],
                ["Tenor", "Default", "Wide"],
                [" on ", 1, 1.4],
                ["# and the rest"],
                ["1w", 0.8, None],
            ])
            table = kace.SpreadTable.load(good)
            self.assertEqual(table.names, ["default", "wide"])
            self.assertEqual(table.for_tier(), {"O/N": 1.0, "1W": 0.8})
            self.assertEqual(table.for_tier("wide"), {"O/N": 1.4, "1W": 0.8})

            # The old `pair, tenor, spread` layout is every workbook this tool
            # has ever written, so it is named rather than reported as a tab
            # with no header -- which is true and no help at all to somebody
            # looking at a tab full of tenors.
            legacy = workbook(Path(tmp) / "legacy.xlsx", [
                ["pair", "tenor", "spread"],
                ["USDCNH", "1W", 0.8],
            ])
            with self.assertRaises(kace.KaceError) as ctx:
                kace.SpreadTable.load(legacy)
            self.assertIn("old 'pair, tenor, spread' layout", str(ctx.exception))

            # A workbook without the tab, and no workbook at all, are both
            # said by name rather than answered with an empty table.
            bare = Path(tmp) / "bare.xlsx"
            openpyxl.Workbook().save(bare)
            with self.assertRaises(kace.KaceError) as ctx:
                kace.SpreadTable.load(bare)
            self.assertIn(kace.SPREADS_SHEET, str(ctx.exception))
            with self.assertRaises(kace.KaceError) as ctx:
                kace.SpreadTable.load(Path(tmp) / "missing.xlsx")
            self.assertIn("missing.xlsx", str(ctx.exception))

    # -- off the book ---------------------------------------------------------
    @classmethod
    def _book(cls):
        if not hasattr(cls, "_cached_book"):
            cls._cached_book = Book.from_excel(str(BOOK), ASOF).load_all(["USDCNH"])
        return cls._cached_book

    def _table(self, rows, tier=None):
        from volkit import kace
        t = kace.SpreadTable(path="test")
        t.tiers = {tier or kace.DEFAULT_TIER: dict(rows)}
        return t

    def test_the_series_reaches_the_last_pillar_and_hordate_is_the_books(self):
        """The sheet's two quiet failures: #N/A at 1Y, and TODAY() in horDate."""
        from volkit import kace
        feed = kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS))
        self.assertEqual(feed.hor_date, date(2024, 2, 28))       # ASOF, not date.today()
        self.assertNotEqual(feed.hor_date, date.today())
        for p in feed.pillars:
            self.assertIn(p.expiry, feed.daily, p.tenor)
            self.assertAlmostEqual(p.atm, feed.daily[p.expiry])
        last = max(p.expiry for p in feed.pillars)
        self.assertEqual(last, date(2025, 2, 27))                # the 1Y expiry
        self.assertGreaterEqual(max(feed.daily), last)
        self.assertLessEqual((max(feed.daily) - last).days, kace.MARGIN_DAYS + 1)
        # Valued at 12:00 UTC, before the 14:00 NY cut: today's own bucket
        # is not a cumulative vol, and is left out with a note.
        self.assertNotIn(date(2024, 2, 28), feed.daily)
        self.assertEqual(min(feed.daily), date(2024, 2, 29))
        self.assertTrue(any("28 Feb 2024" in n for n in feed.notes))
        self.assertEqual(feed.node_count(), len(feed.daily) + 45)

    def test_overnight_is_the_next_business_day_and_borrows_the_shortest_wings(self):
        from volkit import kace
        feed = kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS))
        on, w1 = feed.pillars[0], feed.pillars[1]
        self.assertEqual(on.tenor, "O/N")
        self.assertEqual(on.expiry, date(2024, 2, 29))
        self.assertEqual(w1.tenor, "1W")
        self.assertEqual(w1.expiry, date(2024, 3, 6))
        self.assertEqual((on.rr25, on.rr10, on.fly25, on.fly10),
                         (w1.rr25, w1.rr10, w1.fly25, w1.fly10))
        self.assertEqual(on.wings, "marks at 1W")
        self.assertEqual(w1.wings, "marks")
        self.assertTrue(any("O/N" in n and "1W" in n for n in feed.notes))
        # The desk's convention: RR is the USD call over the put, in vol
        # points, straight off the marks sheet; S is the strangle mark.  The
        # numbers are the shipped workbook's own USDCNH sheet -- 1W quotes
        # RR 25D 0.385 and ST 25D 0.1825, 1Y quotes RR 10D 2.645 -- so this
        # pins the reading, not a book somebody happened to have open.
        self.assertAlmostEqual(w1.rr25, 0.385)
        self.assertAlmostEqual(w1.fly25, 0.1825)
        self.assertAlmostEqual(feed.pillars[-1].rr10, 2.645)

    def test_fitted_wings_come_off_the_surface_near_the_marks(self):
        from volkit import kace
        feed = kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS),
                          source="fitted")
        marks = kace.build(self._book(), "USDCNH", kace.SpreadTable.load(self.SPREADS))
        for f, m in zip(feed.pillars, marks.pillars):
            self.assertEqual(f.wings, "fitted")
            for v in (f.rr25, f.rr10, f.fly25, f.fly10):
                self.assertTrue(math.isfinite(v))
            if f.tenor != "O/N":
                self.assertLess(abs(f.rr25 - m.rr25), 0.15, f.tenor)
                self.assertLess(abs(f.fly25 - m.fly25), 0.1, f.tenor)
        self.assertEqual(feed.notes, [n for n in feed.notes if "O/N" not in n])

    def test_a_pillar_with_no_mark_is_refused_by_name(self):
        from volkit import kace
        with self.assertRaises(kace.KaceError) as ctx:
            kace.build(self._book(), "USDCNH", self._table([("1W", 0.8), ("3W", 0.5)]))
        self.assertIn("3W", str(ctx.exception))
        with self.assertRaises(kace.KaceError):
            kace.build(self._book(), "USDCNH", self._table([("O/N", 1.0)]))
        with self.assertRaises(kace.KaceError):
            kace.build(self._book(), "USDCNH", self._table([("1W", 0.8)]), source="murex")
        # A tier the tab does not hold is refused by name, not answered with
        # the default's widths under another tier's label.
        with self.assertRaises(kace.KaceError) as ctx:
            kace.build(self._book(), "USDCNH", self._table([("1W", 0.8)]), tier="wide")
        self.assertIn("wide", str(ctx.exception))

    def test_the_web_service_and_the_download(self):
        from volkit import kace
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF, kace_spreads_path=str(self.SPREADS),
                              kace_user="feeuser", kace_password="pw")
        state = service.state()["kace"]
        self.assertEqual(state["tiers"], ["default", "wide", "thin"])
        self.assertEqual(state["tier"], "default")
        self.assertTrue(state["credentials"])
        self.assertIsNone(state["error"])
        out = service.kace({"pair": "USDCNH"})
        self.assertEqual(out["nodes"], out["xml"].count("<node "))
        self.assertEqual(out["hor_date"], "2024-02-28")
        self.assertEqual([p["tenor"] for p in out["pillars"]][:2], ["O/N", "1W"])
        self.assertEqual(out["scenario"], "Xyz")                 # the start-up default
        self.assertEqual(out["tier"], "default")
        self.assertIn('<option name="scenario" value="Xyz"/>', out["xml"])
        # The tier is the page's dropdown: it widens every pillar and nothing
        # else -- same pillars, same wings, same nodes.
        wide = service.kace({"pair": "USDCNH", "tier": "wide"})
        self.assertEqual(wide["tier"], "wide")
        self.assertEqual([p["tenor"] for p in wide["pillars"]],
                         [p["tenor"] for p in out["pillars"]])
        self.assertEqual(wide["nodes"], out["nodes"])
        for a, b in zip(wide["pillars"], out["pillars"]):
            self.assertGreater(a["spread"], b["spread"], a["tenor"])
            self.assertEqual(a["rr25"], b["rr25"])
        with self.assertRaises(kace.KaceError) as ctx:
            service.kace({"pair": "USDCNH", "tier": "fat"})
        self.assertIn("default, wide, thin", str(ctx.exception))
        # The multiplier and the interpolation switch travel on the query
        # string like the tier, and a query string has no booleans -- the
        # words a browser sends for *off* have to mean off.
        times = service.kace({"pair": "USDCNH", "multiplier": "1.5"})
        self.assertEqual(times["multiplier"], 1.5)
        self.assertFalse(times["interpolate"])
        for a, b in zip(times["pillars"], out["pillars"]):
            self.assertAlmostEqual(a["spread"], b["spread"] * 1.5, msg=a["tenor"])
            self.assertAlmostEqual((a["bid"] + a["offer"]) / 2.0,
                                   (b["bid"] + b["offer"]) / 2.0)
        self.assertEqual(times["nodes"], out["nodes"])
        for blank in ("", None):
            self.assertEqual(service.kace({"pair": "USDCNH", "multiplier": blank}
                                          )["multiplier"], 1.0)
        with self.assertRaises(kace.KaceError):
            service.kace({"pair": "USDCNH", "multiplier": "-2"})
        for off in ("", "0", "false", "off"):
            self.assertFalse(service.kace({"pair": "USDCNH", "interpolate": off}
                                          )["interpolate"], off)
        for on in ("1", "true", "yes"):
            self.assertTrue(service.kace({"pair": "USDCNH", "interpolate": on}
                                         )["interpolate"], on)
        # Interpolating changes the days between pillars and nothing else:
        # the pillars, the wings and the node count are what they were.
        across = service.kace({"pair": "USDCNH", "interpolate": "1"})
        self.assertEqual(across["nodes"], out["nodes"])
        self.assertEqual([p["spread"] for p in across["pillars"]],
                         [p["spread"] for p in out["pillars"]])
        self.assertNotEqual(across["xml"], out["xml"])
        name, text = service.export_kace({"pair": "USDCNH"})
        self.assertEqual(name, "USDCNH_kace_vols_Xyz_2024-02-28.xml")
        self.assertEqual(text, out["xml"])
        name, text = service.export_kace({"pair": "USDCNH", "clear": "1"})
        self.assertEqual(name, "USDCNH_kace_clear_Xyz_2024-02-28.xml")
        self.assertIn('name="clearRate"', text)
        # The scenario is the page's box: it goes into the message, the
        # file name, and the clear message alike; blank is refused, and a
        # character XML cannot carry in an attribute is escaped.
        out = service.kace({"pair": "USDCNH", "scenario": " UAT-2 "})
        self.assertEqual(out["scenario"], "UAT-2")
        self.assertIn('<option name="scenario" value="UAT-2"/>', out["xml"])
        name, _ = service.export_kace({"pair": "USDCNH", "scenario": "UAT-2"})
        self.assertEqual(name, "USDCNH_kace_vols_UAT_2_2024-02-28.xml")
        _, text = service.export_kace({"pair": "USDCNH", "clear": "1", "scenario": "UAT-2"})
        self.assertIn('<option name="scenario" value="UAT-2"/>', text)
        self.assertIn('<option name="clearRate" value="true"/>', text)
        with self.assertRaises(kace.KaceError) as ctx:
            service.kace({"pair": "USDCNH", "scenario": "  "})
        self.assertIn("blank", str(ctx.exception))
        out = service.kace({"pair": "USDCNH", "scenario": 'A&B"c'})
        self.assertIn('value="A&amp;B&quot;c"', out["xml"])
        self._nodes(out["xml"])                                   # still parses
        # Without a username the table is still shown; the message is not.
        import os
        had = os.environ.pop(kace.ENV_USER, None)
        if had is not None:
            self.addCleanup(os.environ.__setitem__, kace.ENV_USER, had)
        bare = BookService(str(BOOK), ASOF, kace_spreads_path=str(self.SPREADS))
        self.assertFalse(bare.state()["kace"]["credentials"])
        out = bare.kace({"pair": "USDCNH"})
        self.assertIsNone(out["xml"])
        self.assertIn(kace.ENV_USER, out["xml_error"])
        self.assertEqual(len(out["pillars"]), 9)
        with self.assertRaises(kace.KaceError):
            bare.export_kace({"pair": "USDCNH"})
        # A table that is wrong is the card's error, not a crash at startup.
        broken = BookService(str(BOOK), ASOF, kace_spreads_path="/nowhere/kace.csv")
        self.assertIn("kace.csv", broken.state()["kace"]["error"])
        with self.assertRaises(kace.KaceError):
            broken.kace({"pair": "USDCNH"})

    def test_the_routes_and_the_command_belong_to_the_marking_screen(self):
        from volkit import screens
        from volkit.cli import build_parser
        owner = {r: sc.name for sc in screens.SCREENS for r in sc.routes}
        self.assertEqual(owner["/api/kace"], "marking")
        self.assertEqual(owner["/api/export/kace"], "marking")
        self.assertEqual(screens.command_screen("kace"), "marking")
        args = build_parser().parse_args(["kace", "USDCNH", "--clear", "--kace-user", "u"])
        self.assertTrue(args.clear)
        self.assertEqual(args.kace_user, "u")
        self.assertEqual(args.source, "marks")
        args = build_parser().parse_args(["serve", "--kace-spreads", "s.csv",
                                          "--kace-scenario", "Prod", "--kace-tier", "wide"])
        self.assertEqual((args.kace_spreads, args.kace_scenario, args.kace_tier),
                         ("s.csv", "Prod", "wide"))
        args = build_parser().parse_args(["kace", "USDCNH", "--kace-tier", "thin"])
        self.assertEqual(args.kace_tier, "thin")
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web"
                / "index.html").read_text(encoding="utf-8")
        self.assertIn("kace", html)
        # The tier is a dropdown on the tab, filled from what the tab holds,
        # and it travels with every request the tab makes -- the table shown
        # and the message posted are one call, so they cannot be two tiers.
        self.assertIn('<select id="kacetier"', html)
        self.assertIn("source:KACE_SRC,scenario:scn,tier:tier", html)

    # -- posting ----------------------------------------------------------
    REPLY_OK = ("<?xml version='1.0' encoding='UTF-8'?>\n"
                '<gfi_message version="2.0">\n  <header>\n    <transactionId>1234567890</transactionId>\n'
                "    <timestamp>2023-06-12T16:15:26+08:00</timestamp>\n"
                "    <processingTime>0.298</processingTime>\n  </header>\n  <body>\n"
                '    <response name="action1" function="RATE_FEED" version="1.0" />\n'
                "  </body>\n</gfi_message>\n")

    def test_the_reply_is_read_as_the_poster_page_shows_it(self):
        """The one shape known to mean success, and everything else refused."""
        from volkit import kace
        ok, took, msg = kace.read_reply(self.REPLY_OK)
        self.assertTrue(ok)
        self.assertAlmostEqual(took, 0.298)
        self.assertIn("0.298", msg)
        ok, _, msg = kace.read_reply("<html><body>Please log in</body></html>")
        self.assertFalse(ok)
        # A page is named as a page, with its title -- see
        # test_an_html_reply_is_named_by_its_title.  "not a gfi_message" is
        # what an XML reply with some *other* root tag says, and only that.
        self.assertIn("not a kACE message", msg)
        ok, _, msg = kace.read_reply("502 Bad Gateway\nnginx")
        self.assertFalse(ok)
        self.assertIn("not XML", msg)
        self.assertIn("502 Bad Gateway", msg)
        ok, _, msg = kace.read_reply('<gfi_message><header/><body><error>unknown scenario '
                                     'Prod</error></body></gfi_message>')
        self.assertFalse(ok)
        self.assertIn("unknown scenario Prod", msg)
        ok, _, msg = kace.read_reply('<gfi_message><header/><body><response status="ERROR: bad"/>'
                                     '</body></gfi_message>')
        self.assertFalse(ok)
        ok, _, msg = kace.read_reply('<gfi_message><header><processingTime>0.1</processingTime>'
                                     '</header><body/></gfi_message>')
        self.assertFalse(ok)
        self.assertIn("no <response>", msg)

    def test_an_html_reply_is_named_by_its_title(self):
        """A page instead of a message says so, and says which page."""
        from volkit import kace
        for page in ('<html><head><title>kACE - Login</title></head><body>hi</body></html>',
                     '<!DOCTYPE html>\n<html lang="en"><title>kACE - Login</title>'
                     '<body><div ng-app>'):
            ok, took, msg = kace.read_reply(page)
            self.assertFalse(ok)
            self.assertIn("web page", msg)
            self.assertIn("kACE - Login", msg)
        ok, _, msg = kace.read_reply("<other/>")
        self.assertFalse(ok)
        self.assertIn("not a gfi_message", msg)

    def test_the_body_is_the_forms_the_vba_sent(self):
        from volkit import kace
        body = kace.form_body('<a b="1"> x&y</a>')
        self.assertTrue(body.startswith(b"xml="))
        import urllib.parse
        self.assertEqual(urllib.parse.parse_qs(body.decode())["xml"], ['<a b="1"> x&y</a>'])
        # Percent-encoded throughout, as the VBA's URLEncode is: a space is
        # %20, never the "+" urlencode would write.  Correct form encoding
        # either way, but only one spelling has ever been seen to work here.
        self.assertNotIn(b"+", body)
        self.assertIn(b"%20", body)
        self.assertEqual(len(kace.message_hash("m")), 16)
        self.assertNotEqual(kace.message_hash("m"), kace.message_hash("n"))

    def test_post_message_through_an_injected_network(self):
        from volkit import kace
        seen = {}

        def fake(url, body, headers, *, timeout, ca, insecure):
            seen.update(url=url, body=body, headers=headers, timeout=timeout, ca=ca,
                        insecure=insecure)
            return 200, self.REPLY_OK.encode()

        r = kace.post_message("<gfi_message/>", "https://kace:8500/pricing", opener=fake,
                              ca="/desk/ca.pem")
        self.assertTrue(r.ok)
        self.assertEqual(r.status, 200)
        self.assertAlmostEqual(r.processing_time, 0.298)
        self.assertEqual(seen["headers"]["Content-Type"], kace.FORM_CONTENT_TYPE)
        self.assertEqual(seen["body"], b"xml=%3Cgfi_message%2F%3E")
        self.assertEqual(seen["headers"]["Content-Length"], str(len(seen["body"])))
        self.assertEqual((seen["ca"], seen["insecure"], seen["timeout"]),
                         ("/desk/ca.pem", False, kace.POST_TIMEOUT))
        self.assertEqual(r.bytes_sent, len(seen["body"]))
        # A status that is not 200 is the failure, with the first line of the body.
        r = kace.post_message("<m/>", "http://kace:8500/x",
                              opener=lambda *a, **k: (500, b"<html>Internal Server Error</html>"))
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 500)
        self.assertIn("HTTP 500", r.message)
        self.assertIn("Internal Server Error", r.message)
        # No address, or not an http one, is refused before anything is sent.
        with self.assertRaises(kace.KacePostError) as ctx:
            kace.post_message("<m/>", "", opener=fake)
        self.assertIn(kace.ENV_URL, str(ctx.exception))
        with self.assertRaises(kace.KacePostError):
            kace.post_message("<m/>", "ftp://kace/x", opener=fake)

    def test_an_old_tls_server_is_reached_by_stepping_down_and_the_post_says_how(self):
        """kACE offers 1024-bit Diffie-Hellman and OpenSSL 3 refuses it by default.

        The desk saw ``[SSL: DH_KEY_TOO_SMALL] dh key too small`` from the
        Post button while the poster web page worked, because a browser never
        offers finite-field DH and the server falls back to ECDHE or RSA with
        it.  So the poster does the same, one step at a time, keeps the step
        that worked for the host, and reports it with the post.
        """
        import urllib.error
        from unittest import mock
        from volkit import kace
        calls: list[int] = []

        def fake_post_once(url, body, headers, *, timeout, context):
            step = len(calls)
            calls.append(step)
            if step == 0:
                raise urllib.error.URLError("[SSL: DH_KEY_TOO_SMALL] dh key too small (_ssl.c:1010)")
            return 200, self.REPLY_OK.encode()

        kace._TLS_STEP_BY_HOST.clear(); kace._TLS_NOTE_BY_HOST.clear()
        with mock.patch.object(kace, "_post_once", fake_post_once):
            r = kace.post_message("<gfi_message/>", "https://kace:8500/pricing", insecure=True)
            self.assertTrue(r.ok)
            self.assertEqual(calls, [0, 1])
            self.assertIn("without finite-field Diffie-Hellman", r.message)
            self.assertEqual(kace._TLS_STEP_BY_HOST["kace:8500"], 1)
            # the next post to the same host starts at the step that worked
            r = kace.post_message("<gfi_message/>", "https://kace:8500/pricing", insecure=True)
            self.assertEqual(calls, [0, 1, 2])   # one call, no re-learning
            self.assertTrue(r.ok)
        # A certificate problem is never stepped around: it is the error, with the hint.
        kace._TLS_STEP_BY_HOST.clear(); kace._TLS_NOTE_BY_HOST.clear()

        def bad_cert(*a, **k):
            raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        with mock.patch.object(kace, "_post_once", bad_cert):
            with self.assertRaises(kace.KacePostError) as ctx:
                kace.post_message("<gfi_message/>", "https://kace:8500/pricing")
        self.assertIn("--kace-ca", str(ctx.exception))
        # And a server nothing reaches says what was tried.
        def never(*a, **k):
            raise urllib.error.URLError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure")
        with mock.patch.object(kace, "_post_once", never):
            with self.assertRaises(kace.KacePostError) as ctx:
                kace.post_message("<gfi_message/>", "https://kace:8500/pricing")
        self.assertIn("after also trying", str(ctx.exception))
        self.assertIn("security level 0", str(ctx.exception))
        # The plain http route has no TLS to step down and is untouched.
        self.assertEqual(kace.tls_note("http://kace:8500/x"), "")

    def test_the_post_log_and_a_refused_post(self):
        import tempfile
        from volkit import kace
        with tempfile.TemporaryDirectory() as tmp:
            log = kace.PostLog.at(Path(tmp) / "posts.jsonl")
            when = datetime(2024, 2, 28, 9, 12, tzinfo=UTC)
            # A dry run says what would go and writes nothing.
            e = kace.post_feed("<m/>", pair="USDCNH", scenario="Xyz", clear=False,
                               hor_date=date(2024, 2, 28), nodes=3, url="http://kace/x",
                               log=log, when=when, dry_run=True)
            self.assertIsNone(e["ok"])
            self.assertIn("dry run", e["message"])
            self.assertIn("http://kace/x", e["message"])
            self.assertEqual(log.entries(), [])
            # A post that cannot reach the server is recorded, as a failure.
            def down(*a, **k):
                raise kace.KacePostError("could not reach http://kace/x: refused")
            e = kace.post_feed("<m/>", pair="USDCNH", scenario="Xyz", clear=True,
                               hor_date=date(2024, 2, 28), nodes=1, url="http://kace/x",
                               log=log, when=when, opener=down)
            self.assertFalse(e["ok"])
            self.assertIn("refused", e["message"])
            self.assertEqual(e["logged"], log.path)
            # And one that lands.
            e = kace.post_feed("<m/>", pair="usdcnh", scenario="Live", clear=False,
                               hor_date=date(2024, 2, 28), nodes=413, url="http://kace/x",
                               log=log, when=when,
                               opener=lambda *a, **k: (200, self.REPLY_OK.encode()))
            self.assertTrue(e["ok"])
            self.assertEqual(e["pair"], "USDCNH")
            rows = log.entries()
            self.assertEqual([r["ok"] for r in rows], [False, True])
            self.assertEqual(rows[1]["scenario"], "Live")
            self.assertEqual(rows[1]["hash"], kace.message_hash("<m/>"))
            self.assertEqual(rows[1]["at"], "2024-02-28T09:12:00+00:00")
            # A line that will not parse is skipped, not fatal; the pair filter works.
            with Path(log.path).open("a", encoding="utf-8") as fh:
                fh.write("{not json\n")
            self.assertEqual(len(log.entries()), 2)
            self.assertEqual(log.entries(pair="EURUSD"), [])
            self.assertEqual(len(log.entries(pair="usdcnh", limit=1)), 1)

    def test_the_post_button_sends_what_the_table_shows_and_nothing_else(self):
        import tempfile
        from volkit import kace
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "kace_posts.jsonl"
            service = BookService(str(BOOK), ASOF, kace_spreads_path=str(self.SPREADS),
                                  kace_user="feeuser", kace_password="pw",
                                  kace_url="https://kace:8500/pricing", kace_insecure=True,
                                  kace_log_path=str(log_path))
            sent = []

            def fake(url, body, headers, *, timeout, ca, insecure):
                sent.append((url, body, headers, insecure))
                return 200, self.REPLY_OK.encode()

            service.kace_opener = fake
            state = service.state()["kace"]
            self.assertEqual(state["url"], "https://kace:8500/pricing")
            self.assertTrue(state["insecure"])
            self.assertEqual(state["posts"], [])
            shown = service.kace({"pair": "USDCNH", "scenario": "UAT"})
            r = service.kace_post({"pair": "USDCNH", "scenario": "UAT"})
            self.assertTrue(r["ok"])
            self.assertAlmostEqual(r["processing_time"], 0.298)
            self.assertEqual(r["nodes"], 413)
            self.assertEqual(r["scenario"], "UAT")
            self.assertEqual(r["hor_date"], "2024-02-28")
            self.assertEqual(r["at"], "2024-02-28T12:00:00+00:00")   # the book's clock
            import urllib.parse
            url, body, headers, insecure = sent[0]
            self.assertEqual(url, "https://kace:8500/pricing")
            self.assertTrue(insecure)
            self.assertEqual(headers["Content-Type"], kace.FORM_CONTENT_TYPE)
            posted = urllib.parse.parse_qs(body.decode())["xml"][0]
            self.assertEqual(posted, shown["xml"])               # exactly the table's message
            self.assertEqual(r["hash"], kace.message_hash(shown["xml"]))
            self.assertEqual(len(r["posts"]), 1)
            self.assertEqual(log_path.read_text(encoding="utf-8").count("\n"), 1)
            # The clear goes the same way, and says so in the record.
            r = service.kace_post({"pair": "USDCNH", "scenario": "UAT", "clear": "1"})
            self.assertTrue(r["clear"])
            self.assertIn('name="clearRate"', urllib.parse.parse_qs(sent[1][1].decode())["xml"][0])
            # A dry run reaches no network and writes no line.
            r = service.kace_post({"pair": "USDCNH", "dry_run": True})
            self.assertTrue(r["dry_run"])
            self.assertEqual(len(sent), 2)
            self.assertEqual(len(service.kace_log.entries()), 2)
            self.assertEqual(service.state()["kace"]["posts"][-1]["clear"], True)
            # A server that answers with something else is a failure the page shows.
            service.kace_opener = lambda *a, **k: (200, b"<html>Session expired</html>")
            r = service.kace_post({"pair": "USDCNH"})
            self.assertFalse(r["ok"])
            self.assertIn("not a kACE message", r["message"])
            self.assertIn("Session expired", r["reply"])
            # No address: refused by name, and recorded as refused.
            bare = BookService(str(BOOK), ASOF, kace_spreads_path=str(self.SPREADS),
                               kace_user="u", kace_log_path=str(Path(tmp) / "bare.jsonl"))
            import os
            os.environ.pop(kace.ENV_URL, None)
            bare.kace_url = ""
            self.assertFalse(bare.state()["kace"]["url"])
            r = bare.kace_post({"pair": "USDCNH"})
            self.assertFalse(r["ok"])
            self.assertIn(kace.ENV_URL, r["message"])
            # Without a username there is no message to post at all.
            nouser = BookService(str(BOOK), ASOF, kace_spreads_path=str(self.SPREADS),
                                 kace_url="http://kace/x")
            nouser.kace_user = ""
            with self.assertRaises(kace.KaceError):
                nouser.kace_post({"pair": "USDCNH"})

    def test_the_post_route_and_options(self):
        from volkit import screens
        from volkit.cli import build_parser
        owner = {r: sc.name for sc in screens.SCREENS for r in sc.routes}
        self.assertEqual(owner["/api/kace/post"], "marking")
        args = build_parser().parse_args(["kace", "USDCNH", "--post", "--dry-run",
                                          "--kace-url", "https://k:8500/p", "--kace-insecure"])
        self.assertTrue(args.post and args.dry_run and args.kace_insecure)
        self.assertEqual(args.kace_url, "https://k:8500/p")
        args = build_parser().parse_args(["serve", "--kace-url", "http://k/x", "--kace-ca",
                                          "ca.pem", "--kace-log", "p.jsonl"])
        self.assertEqual((args.kace_url, args.kace_ca, args.kace_log),
                         ("http://k/x", "ca.pem", "p.jsonl"))


class TestBulkExport(unittest.TestCase):
    """Several pairs' marks out to the platform in one go.

    The message per pair is the kACE feed tab's, built by the same function
    from the same tab's settings.  The bulk path itself -- which pairs, which
    channel, the overlay, the preflight -- is the Vol exporting bulk screen's
    and is tested in `tests/test_publish.py`; what stays here is the key-tenors
    message and the fact that the bar left the Market maker screen.
    """

    def test_key_tenors_only_posts_the_pillars_and_no_calendar_days(self):
        """The daily series is still built -- a pillar's ATM is read off it --
        and simply not written: five nodes a pillar and nothing else."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDCNH"])
        table = kace.SpreadTable.load(BOOK)
        whole = kace.build(book, "USDCNH", table)
        keys = kace.build(book, "USDCNH", table, pillars_only=True)
        self.assertEqual(keys.node_count(), 5 * len(keys.pillars))
        self.assertEqual(whole.node_count(), len(whole.daily) + 5 * len(whole.pillars))
        text = keys.xml("feeuser", "pw")
        self.assertEqual(text.count("<node "), keys.node_count())
        # Every node is a pillar's: nothing is named "1", the daily numbering.
        self.assertNotIn('<node name="1"', text)
        self.assertIn('<node name="S1"', text)
        # The pillars themselves are untouched -- same widths, same wings.
        self.assertEqual([(p.tenor, p.bid, p.offer) for p in keys.pillars],
                         [(p.tenor, p.bid, p.offer) for p in whole.pillars])
        self.assertTrue(keys.summary()["pillars_only"])
        self.assertEqual(keys.summary()["days"], 0)
        self.assertTrue(any("key tenors only" in n for n in keys.notes))

    def test_the_bulk_export_left_the_market_maker_tab_for_its_own_screen(self):
        """The bar came off the Market maker screen and is not replaced there:
        bulk publishing is the Vol exporting bulk screen's, with its own
        routes, and the single-pair feed tab's routes stay the marking
        screen's.  No back-compat for the bulk path, in the pattern of the
        last two reorganisations."""
        from volkit import screens
        self.assertNotIn("/api/kace/bulk", screens.BY_NAME["mm"].routes)
        self.assertIn("/api/kace", screens.BY_NAME["marking"].routes)
        self.assertIn("/api/kace/post", screens.BY_NAME["marking"].routes)
        for route in ("/api/export/build", "/api/export/run", "/api/export/overlay",
                      "/api/export/state", "/api/export/file"):
            self.assertIn(route, screens.BY_NAME["export"].routes)
        py = _source("volkit", "webapp.py")
        self.assertNotIn("/api/kace/bulk", py)
        self.assertNotIn("BULK_DESTINATIONS", py)
        html = _source("volkit", "web", "index.html")
        self.assertNotIn("/api/kace/bulk", html)
        self.assertNotIn('id="xbar"', html)
        self.assertIn('id="p-export"', html)
        # Between Analysis and Market maker, as the design note places it.
        names = list(screens.ALL)
        self.assertEqual(names.index("export"), names.index("analysis") + 1)
        self.assertEqual(names.index("mm"), names.index("export") + 1)


if __name__ == "__main__":
    unittest.main()
