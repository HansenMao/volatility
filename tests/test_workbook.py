"""The workbook as the database: config tabs, sessions, reload and vega weights.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestConfigurationTabs(unittest.TestCase):
    """The settings that used to be a CSV each, now tabs of the workbook.

    Three things are pinned: the reader's own conventions (a header found
    below prose, a '#' row skipped wherever it sits, an absent tab answered
    with None rather than an empty table), that the shipped workbook actually
    carries all three tabs, and that the holidays it lists reach the book
    without reaching every *other* book in the process.
    """

    def _workbook(self, rows, sheet="PEG_BANDS"):
        import tempfile
        import openpyxl
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = openpyxl.Workbook()
        wb.active.title = sheet
        for row in rows:
            wb.active.append(row)
        path = d / "marks.xlsx"
        wb.save(path)
        return path

    def test_the_conventions_tab_reaches_the_pair_and_a_bad_row_is_a_problem(self):
        """A desk states a pair's premium currency and ATM boundary on a tab.

        Without a row the pair takes the market's conventions -- and those
        make every cross premium adjusted, which the shipped workbook's
        EURJPY now is.
        """
        import tempfile
        from volkit import session
        from volkit.marketdata import load_conventions, ExcelSource
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb_path = d / "marks.xlsx"
        shutil.copy(BOOK, wb_path)
        # written the way the configuration screen writes it
        session.write_config_tabs(wb_path, {"CONVENTIONS": [
            {"pair": "EURJPY", "premium": "JPY", "atmf beyond": "2y", "delta": ""},
            {"pair": "USDCNH", "premium": "", "atmf beyond": "never", "delta": "forward"},
            {"pair": "GBPCHF", "premium": "GBP", "atmf beyond": "", "delta": ""},   # not in CONFIG
        ]})
        found = load_conventions(wb_path)
        self.assertEqual(found["EURJPY"], {"premium": "JPY", "atmf_beyond": "2y", "delta": ""})
        data = ExcelSource(wb_path).load()
        self.assertEqual(data.problems, [])
        eurjpy = data.pairs["EURJPY"].conventions()
        self.assertFalse(eurjpy.premium_adjusted)          # the tab says JPY
        self.assertEqual(eurjpy.atmf_beyond, "2y")
        usdcnh = data.pairs["USDCNH"].conventions()
        self.assertTrue(usdcnh.premium_adjusted)           # blank: the default, USD
        self.assertFalse(usdcnh.atm_is_forward(30.0))      # never
        self.assertFalse(usdcnh.spot_delta)                # forward delta at every tenor
        self.assertTrue(eurjpy.spot_delta)                 # blank: spot, the majors' rule
        self.assertTrue(data.pairs["AUDJPY"].conventions().premium_adjusted)   # no row: market
        self.assertTrue(data.pairs["USDJPY"].conventions().atm_is_forward(1.5))
        self.assertTrue(any("GBPCHF" in n and "CONFIG does not list it" in n for n in data.notes))
        # and it reaches the surface, boundary resolved on the calendar
        book = Book.from_excel(wb_path, ASOF).load_all(["EURJPY"])
        self.assertFalse(book["EURJPY"].conv.premium_adjusted)
        self.assertFalse(book["EURJPY"].conv.atm_is_forward(book["EURJPY"].tenor_years("2y")))
        # a premium currency that is not the pair's is a problem, named by row
        session.write_config_tabs(wb_path, {"CONVENTIONS": [
            {"pair": "EURJPY", "premium": "USD", "atmf beyond": "", "delta": ""}]})
        data = ExcelSource(wb_path).load()
        self.assertTrue(any("CONVENTIONS row" in p and "USD" in p for p in data.problems))

    def test_an_absent_tab_is_none_and_an_empty_one_is_an_empty_list(self):
        from volkit import configsheets
        path = self._workbook([["pair", "lower", "upper"]])
        self.assertIsNone(configsheets.read_rows(path, "HOLIDAYS",
                                                 required=("country", "date")))
        self.assertEqual(configsheets.read_rows(path, "PEG_BANDS",
                                                required=("pair", "lower", "upper")), [])

    def test_the_header_is_found_below_prose_and_comments_are_skipped(self):
        from volkit import configsheets
        path = self._workbook([
            ["# what this tab is for"],
            ["# and a second line of it"],
            ["Pair", "Lower", "Upper", "Note"],
            ["USDHKD", 7.75, 7.85, "the peg"],
            ["# deliberately not listed: USDCNY"],
            ["USDXXX", 1.0, 2.0, None],
        ])
        rows = configsheets.read_rows(path, "PEG_BANDS",
                                      required=("pair", "lower", "upper"))
        self.assertEqual([r.text("pair") for r in rows], ["USDHKD", "USDXXX"])
        # The row number is the one Excel shows, so an error can be looked up.
        self.assertEqual([r.number for r in rows], [4, 6])
        self.assertEqual(rows[0].real("lower"), 7.75)
        self.assertEqual(rows[0].text("note"), "the peg")
        self.assertEqual(rows[1].text("note"), "")

    def test_a_tab_with_no_header_at_all_is_refused_by_name(self):
        from volkit import configsheets
        path = self._workbook([["# nothing but prose"], ["# and more of it"]])
        with self.assertRaises(configsheets.ConfigSheetError) as ctx:
            configsheets.read_rows(path, "PEG_BANDS", required=("pair", "lower", "upper"))
        self.assertIn("PEG_BANDS", str(ctx.exception))

    def test_the_shipped_workbook_carries_every_configuration_tab(self):
        """Every tab the tool needs -- which is not quite every tab it reads.

        ``WING_RATIOS`` is the exception on purpose: a workbook that has not
        been through ``volkit migrate-wings`` quotes all four wings itself,
        which is what every workbook did before the tab existed, and a
        configuration a workbook is allowed not to have must not be asserted
        into existence by a test.  The rest are not optional -- a missing
        ``PEG_BANDS`` is a managed pair with no band and a screen that says
        nothing about it.
        """
        from volkit import configsheets
        # CONVENTIONS is optional for the same reason: a pair with no row takes
        # the market's conventions, and most pairs never need a row.
        # CROSS_CORR is optional for the same reason as WING_RATIOS: a cross
        # with no rows is fitted from its own three coefficients, which is
        # what every workbook did before the tab existed.
        optional = {"WING_RATIOS", "CONVENTIONS", "CROSS_CORR"}
        # The export-policy tables are optional too: a desk that publishes
        # nothing has no business carrying them, and the channels refuse by
        # name until they are typed (`volkit export --init-tables` seeds).
        optional |= set(configsheets.EXPORT_TABS) - {"SPREADS"}
        needed = [s for s in configsheets.SHEETS if s not in optional]
        self.assertEqual([s for s in configsheets.present(BOOK) if s not in optional],
                         needed)

    def test_an_open_tabs_extra_columns_are_pairs_on_one_tab_and_tiers_on_the_other(self):
        """"Open" was never one rule.

        `Vega Weights` grows a column per pair and `SPREADS` one per kACE
        spreading tier, so the *kind* is what `OPEN_COLUMNS` holds.  A checker
        that knew only the pair rule -- which is what it used to be -- would
        refuse every tier a desk named, and the tab would be unwritable.
        """
        from volkit import configsheets as cs

        self.assertEqual(cs.OPEN_COLUMNS,
                         {"Vega Weights": "pair", "SPREADS": "tier", "MARKET_WIDTHS": "pair"})
        # A pair is six letters and is written as a proper noun; a tier is the
        # name the desk chose, written the way the reader spells it, so the
        # dropdown, --kace-tier and the heading are one string.
        self.assertTrue(cs.open_column_ok("pair", "usdjpy"))
        self.assertFalse(cs.open_column_ok("pair", "wide"))
        self.assertEqual(cs.spell_open_column("pair", "usdjpy"), "USDJPY")
        self.assertTrue(cs.open_column_ok("tier", "Wide EM"))
        self.assertEqual(cs.spell_open_column("tier", "Wide EM"), "wide_em")
        self.assertFalse(cs.open_column_ok("tier", "2nd"))       # reads back as a number
        self.assertFalse(cs.open_column_ok("tier", "a/b"))

        cs.check_open_columns("SPREADS", ("tenor", "default", "wide", "note"))
        cs.check_open_columns("Vega Weights", ("tenor", "default", "USDJPY"))
        with self.assertRaises(cs.ConfigSheetError) as ctx:
            cs.check_open_columns("Vega Weights", ("tenor", "default", "wide"))
        self.assertIn("six letters", str(ctx.exception))
        with self.assertRaises(cs.ConfigSheetError) as ctx:
            cs.check_open_columns("SPREADS", ("tenor", "default", "a/b"))
        self.assertIn("tier", str(ctx.exception))
        # A tab whose columns are fixed takes whatever it is given here, and
        # is refused by its own writer instead.
        cs.check_open_columns("PEG_BANDS", ("pair", "lower", "upper", "anything"))

        # `columns_for` puts the fixed ones first and keeps `note` last
        # however many tiers the tab grows.
        self.assertEqual(
            cs.columns_for("SPREADS",
                           [{"tenor": "1W", "default": 0.8, "Wide": 1.2, "note": "x"}]),
            ("tenor", "default", "wide", "note"))

    def test_a_retired_tab_is_named_once_at_load_and_read_no_further(self):
        """``RATES`` is still a tab in somebody's workbook, and reads like one.

        A setting that has moved leaves its tab behind -- deleting a desk's
        sheet is not this tool's business -- and a tab full of rates that
        nothing reads is worse than no tab at all, because it looks like it is
        working.  So it is said once, at load, and the numbers on it change
        nothing: the discount factors come off the feed's OIS rows.
        """
        import tempfile
        from volkit import configsheets
        self.assertNotIn("RATES", configsheets.SHEETS)
        self.assertNotIn("RATES", configsheets.EDITABLE)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = d / "marks.xlsx"
        shutil.copy(BOOK, wb)
        self.assertEqual(configsheets.retired(wb), [])
        wb2 = d / "old.xlsx"
        shutil.copy(BOOK, wb2)
        import openpyxl
        book = openpyxl.load_workbook(wb2)
        try:
            sheet = book.create_sheet("RATES")
            sheet.append(["currency", "tenor", "rate"])
            sheet.append(["USD", "1Y", 5.0])
            book.save(wb2)
        finally:
            book.close()
        said = configsheets.retired(wb2)
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("RATES: discount rates come from the market feed"))
        loaded = Book.from_excel(wb2, ASOF)
        self.assertTrue(any(w == f"workbook: {said[0]}" for w in loaded.warnings))
        # and the number on it is not a discount factor
        self.assertIsNone(loaded.discount_factor("USD", 1.0))

    def test_the_holidays_tab_reaches_the_book_and_no_further(self):
        """A lunar holiday belongs to the workbook that lists it.

        The overrides were loadable but never loaded before this: a Chinese
        New Year in the file moved no expiry.  They are the book's calendars
        now -- and a *copy*, because a book that added dates to the shared set
        would change the expiry of every book loaded after it.
        """
        from volkit.calendars import DEFAULT_CALENDARS
        book = Book.from_excel(BOOK, ASOF)
        cny = date(2026, 2, 17)
        self.assertTrue(book.calendars.is_holiday("CNH", cny))
        self.assertIsNot(book.calendars, DEFAULT_CALENDARS)
        self.assertEqual(DEFAULT_CALENDARS.overrides, {})

    def test_a_workbook_with_no_holidays_tab_keeps_the_shared_calendars(self):
        from volkit.calendars import DEFAULT_CALENDARS
        legacy = WORKBOOK.parent / "vol_marks_legacy_format.xlsx"
        self.assertIs(Book.from_excel(legacy, ASOF).calendars, DEFAULT_CALENDARS)


class TestConfigurationIsMarkedNotWritten(unittest.TestCase):
    """The workbook's settings go into the session, and into the file with the marks.

    A peg band, a kACE pillar, a holiday and a wing ratio used to be written
    into the workbook the moment they were typed, which re-read the book and
    was refused while there were any marks -- a morning's marking or a band,
    and the answer was to save first and hope.  They are **marked** now: the
    configuration window applies one to this session, the book is read again
    on top of it so every screen shows what it does, and it reaches the file
    only when the session does.  What is pinned here is that pair -- applying
    changes the book and not the file, and one write changes the file -- and
    that the marks live through it.
    """

    #: What this class actually marks.  ``book_copy`` writes the settings tabs
    #: whole -- they are the subject here -- but not every pair sheet: applying
    #: a tab re-reads the workbook, and a read calibrates every pair CONFIG
    #: names, so pinning a band while fourteen smiles were refitted made this
    #: the slowest class in the suite by a factor of two.  Add a pair here if a
    #: test starts needing one.
    PAIRS = ("USDJPY", "EURUSD")

    def _service(self):
        import shutil
        import tempfile
        from volkit.webapp import BookService
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = book_copy(d, pairs=self.PAIRS)
        return BookService(str(wb), ASOF), wb

    def _bands(self, svc):
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["PEG_BANDS"]
        return [dict(r) for r in tab["rows"]]

    def test_a_tab_applied_on_the_window_changes_the_book_and_not_the_file(self):
        svc, wb = self._service()
        rows = self._bands(svc) + [{"pair": "USDSGD", "lower": 1.30, "upper": 1.40,
                                    "note": "a band nobody has written yet"}]
        out = svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        self.assertEqual(out["wrote"]["written"], "")
        self.assertTrue(out["wrote"]["pending"])
        # The loaded book has it...
        self.assertIn("USDSGD", svc.book.bands)
        self.assertEqual(svc.book.bands["USDSGD"].upper, 1.40)
        # ...and the workbook on disk does not.
        self.assertNotIn("USDSGD", Book.from_excel(wb, ASOF).bands)
        self.assertEqual(svc.config_tabs()["pending"], ["PEG_BANDS"])
        self.assertTrue(svc.dirty)

    def test_a_kace_tier_added_on_the_window_is_posted_from_before_it_is_written(self):
        """A tier is a column of `SPREADS`, so it is marked like a band:
        applied here, read again under the book, and offered by the feed tab
        straight away -- with the file still saying what it said."""
        svc, wb = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["SPREADS"]
        self.assertEqual(tab["open"], "tier")
        self.assertEqual(tab["fixed"], ["tenor", "default", "note"])
        self.assertIn("wide", tab["columns"])
        rows = [dict(r) for r in tab["rows"]]
        for r in rows:
            r["client_a"] = None            # a tier that takes default everywhere
        rows[0]["client_a"] = 2.5           # except at its front pillar
        svc.config_save({"sheet": "SPREADS", "rows": rows})
        self.assertIn("client_a", svc.kace_spreads.names)
        self.assertIn("client_a", svc.state()["kace"]["tiers"])
        ladder, base = svc.kace_spreads.for_tier("client_a"), svc.kace_spreads.for_tier()
        self.assertEqual(sorted(ladder), sorted(base))            # same pillars
        self.assertEqual(ladder["O/N"], 2.5)
        self.assertEqual(ladder["1M"], base["1M"])                # blank falls back
        # The workbook still holds the tiers it held.
        from volkit import kace as kace_mod
        self.assertNotIn("client_a", kace_mod.SpreadTable.load(wb).names)
        self.assertEqual(svc.config_tabs()["pending"], ["SPREADS"])

    def test_the_marks_this_session_made_survive_a_tab_being_applied(self):
        """The reason this used to be refused.  Applying a setting re-reads
        the workbook -- there is no other way to apply a band -- and a reload
        is what throws marks away, so the marks are captured and put back."""
        svc, _ = self._service()
        svc.overwrite({"pair": "USDJPY", "cut": "NY", "kind": "atm",
                       "tenor": "1M", "value": 9.5})
        before = svc.marks({"pair": "USDJPY", "cut": "NY"})["atm"]
        svc.config_save({"sheet": "PEG_BANDS", "rows": self._bands(svc)})
        after = svc.marks({"pair": "USDJPY", "cut": "NY"})["atm"]
        self.assertEqual([r["marked"] for r in after], [r["marked"] for r in before])
        one = [r for r in after if r["tenor"].upper() == "1M"][0]
        self.assertAlmostEqual(one["marked"], 9.5, places=12)

    def test_the_session_file_carries_the_tab_and_puts_it_back(self):
        from volkit import session
        svc, wb = self._service()
        rows = self._bands(svc) + [{"pair": "USDSGD", "lower": 1.30, "upper": 1.40,
                                    "note": ""}]
        svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        path = svc.session_save({"path": str(Path(wb).with_name("marks.json"))})["written"]
        doc = session.load(path)
        self.assertEqual(sorted(session.config_tabs_from_doc(doc)), ["PEG_BANDS"])

        # A fresh service on the same workbook has the file's bands; loading
        # the session gives it the session's, because the book is built again
        # on them rather than layered.
        from volkit.webapp import BookService
        other = BookService(str(wb), ASOF)
        self.assertNotIn("USDSGD", other.book.bands)
        out = other.session_load({"path": path})
        self.assertEqual(out["problems"], [])
        self.assertIn("USDSGD", other.book.bands)
        self.assertEqual(other.config_tabs()["pending"], ["PEG_BANDS"])

    def test_the_one_write_puts_the_tab_and_the_marks_in_together(self):
        """The consolidation: one route writes the workbook, and it writes
        the marks and the settings in the same pass and the same backup."""
        svc, wb = self._service()
        svc.overwrite({"pair": "USDJPY", "cut": "NY", "kind": "atm",
                       "tenor": "1M", "value": 9.5})
        rows = self._bands(svc) + [{"pair": "USDSGD", "lower": 1.30, "upper": 1.40,
                                    "note": ""}]
        svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        path = svc.session_save({"path": str(Path(wb).with_name("marks.json"))})["written"]
        out = svc.session_export({"path": path})
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["tabs"], ["PEG_BANDS"])
        self.assertTrue(out["backup"])
        # Both are in the file now, read back by the ordinary reader.
        again = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        self.assertIn("USDSGD", again.bands)
        self.assertAlmostEqual(
            again["USDJPY"].atm.term_vol(again["USDJPY"].tenor_years("1M")), 0.095, places=9)
        # And the session no longer holds anything the workbook does not.
        self.assertEqual(svc.config_tabs()["pending"], [])
        self.assertFalse(svc.dirty)

    def test_reloading_the_workbook_throws_the_held_tab_away(self):
        """`Reload workbook` is the button that goes back to what the file
        says.  It drops the marks -- that is what a reload is -- and it drops
        a setting that has not been written for the same reason."""
        svc, _ = self._service()
        rows = self._bands(svc) + [{"pair": "USDSGD", "lower": 1.30, "upper": 1.40,
                                    "note": ""}]
        svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        self.assertIn("USDSGD", svc.book.bands)
        svc.reload(discard=True)
        self.assertNotIn("USDSGD", svc.book.bands)
        self.assertEqual(svc.config_tabs()["pending"], [])
        self.assertFalse(svc.dirty)
        # Every other reload is one made *in order* to apply the session's
        # tabs, so it keeps them.
        svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        svc.reload()
        self.assertIn("USDSGD", svc.book.bands)

    def test_a_holiday_marked_here_moves_the_dates_it_should(self):
        """Every reader of a configuration tab reads the session's copy, not
        just the one that happens to be easy to see.  A holiday goes through
        `CalendarSet.load_overrides_sheet` rather than the band loader, and a
        tenor is a settlement date first (§4) -- so marking one moves the
        expiry the whole screen is priced on."""
        svc, _ = self._service()
        before = svc.book.tenor_years("USDJPY", "1M")
        svc.config_save({"sheet": "HOLIDAYS", "rows": [
            {"country": "JP", "date": "2024-03-28", "remove": ""}]})
        after = svc.book.tenor_years("USDJPY", "1M")
        self.assertLess(after, before)
        # And it is still only in the session.
        self.assertAlmostEqual(Book.from_excel(svc.path, ASOF).tenor_years("USDJPY", "1M"),
                               before, places=12)

    def test_a_column_no_reader_would_take_is_refused_before_it_is_applied(self):
        """The same rule the write had.  Held in a session instead, a heading
        the tab's own reader refuses is a setting that is accepted now and
        found to be unreadable at the next load."""
        svc, _ = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        rows = [dict(r) for r in tab["rows"]]
        rows[0]["USD"] = 0.5
        with self.assertRaises(ValueError) as ctx:
            svc.config_save({"sheet": "Vega Weights", "rows": rows})
        self.assertIn("six letters", str(ctx.exception))
        self.assertEqual(svc.config_tabs()["pending"], [])
        self.assertEqual(svc.book.vega_weights.weight_for("USDCNH", "1W"), (2.6, "USDCNH"))

    def test_the_command_line_builds_the_book_with_the_sessions_tabs(self):
        """`--session` is the screen's marks on the command line, so it is
        the screen's configuration too -- read before the book is built,
        because a band cannot be layered onto one that is already made."""
        import argparse
        from volkit import cli
        svc, wb = self._service()
        rows = self._bands(svc) + [{"pair": "USDSGD", "lower": 1.30, "upper": 1.40,
                                    "note": ""}]
        svc.config_save({"sheet": "PEG_BANDS", "rows": rows})
        path = svc.session_save({"path": str(Path(wb).with_name("marks.json"))})["written"]
        args = argparse.Namespace(workbook=str(wb), session=path,
                                  asof=ASOF.now.isoformat())
        book = cli._book(args, ["USDJPY"])
        self.assertIn("USDSGD", book.bands)

    def test_a_pair_is_still_written_when_it_is_added_and_the_marks_come_back(self):
        """The one thing here that is not a setting.  A pair is a CONFIG row,
        a PARAMS column and a sheet, so it cannot be held in memory -- there
        would be nothing for the reader to read.  It writes, and what this
        session has marked goes back on the book afterwards, which is what it
        used to refuse rather than do."""
        svc, wb = self._service()
        svc.overwrite({"pair": "USDJPY", "cut": "NY", "kind": "atm",
                       "tenor": "1M", "value": 9.5})
        out = svc.config_pair({"action": "add", "pair": "USDSEK", "atm": 8.0})
        self.assertEqual(Path(out["wrote"]["written"]), Path(wb))
        self.assertIn("USDSEK", svc.book.pairs)
        one = [r for r in svc.marks({"pair": "USDJPY", "cut": "NY"})["atm"]
               if r["tenor"].upper() == "1M"][0]
        self.assertAlmostEqual(one["marked"], 9.5, places=12)


class TestConfigTenorsGovern(unittest.TestCase):
    """CONFIG's ``TENORS`` column is the pillar set, both ways.

    A tenor the pair sheet quotes and CONFIG does not list is not read: not
    shown, not fitted, not markable.  A tenor CONFIG lists and the sheet does
    not quote is on the table with its four numbers read off the fitted smile,
    and typing into one turns the reading into a mark.  The two halves are one
    decision -- CONFIG says which tenors the desk marks -- and this class pins
    both, because keeping a quote in the fit while hiding it from the screen
    would leave a number shaping every smile that nobody can see or take off.
    """

    def book(self, pairs):
        return Book.from_excel(BOOK, ASOF).load_all(pairs)

    def test_a_quoted_tenor_config_does_not_list_is_not_read(self):
        data = ExcelSource(BOOK).load()
        self.assertNotIn("2y", [t.lower() for t in data.tenor_points])
        self.assertTrue(data.tenors_stated)
        # The sheet still has the row; the book does not.
        self.assertNotIn("2Y", {m.tenor.upper() for m in data.marks["USDJPY"]})
        self.assertTrue(any("2Y" in n and "TENORS" in n for n in data.notes), data.notes)
        # Not a problem: the workbook is fine.  A desk that quotes further out
        # than it marks has not made a mistake.
        self.assertEqual(data.problems, [])

    def test_a_tenor_config_does_not_list_is_not_fitted_either(self):
        s = self.book(["USDJPY"])["USDJPY"]
        self.assertNotIn("2Y", {f.tenor.upper() for f in s.fits})
        self.assertNotIn("2Y", {r["tenor"] for r in s.quote_rows()})

    def test_a_tenor_config_does_not_list_cannot_be_marked(self):
        s = self.book(["USDJPY"])["USDJPY"]
        with self.assertRaises(ValueError) as cm:
            s.overwrite_quote("2Y", "rr_25", -0.012)
        self.assertIn("CONFIG", str(cm.exception))
        self.assertIn("TENORS", str(cm.exception))
        self.assertEqual(s.quote_overwrites, {})

    def test_a_workbook_that_lists_no_tenors_governs_nothing(self):
        """An absent ``TENORS`` column is not an empty one.

        The nine default points are an order to show things in, not a desk's
        decision, and cutting a sheet down to a list nobody wrote would be
        this tool inventing the policy.
        """
        data = MarketData()
        self.assertFalse(data.tenors_stated)
        marks = [SmileMark(tenor="2Y", rr_25=0.01, rr_10=0.02, st_25=0.003, st_10=0.008)]
        self.assertEqual(ExcelSource._config_tenors_only("USDJPY", marks, data), marks)
        self.assertEqual(data.notes, [])

    def test_config_and_the_sheet_are_matched_however_each_spells_it(self):
        """CONFIG is maintained in lower case and the sheets in upper."""
        data = MarketData(tenor_points=("1m", "3 m"), tenors_stated=True)
        marks = [SmileMark(tenor=t, rr_25=0.01, rr_10=0.02, st_25=0.003, st_10=0.008)
                 for t in ("1M", "3M", "6M")]
        kept = ExcelSource._config_tenors_only("USDJPY", marks, data)
        self.assertEqual([m.tenor for m in kept], ["1M", "3M"])
        self.assertEqual(tenor_key("3 m"), "3M")

    def test_a_config_tenor_the_sheet_does_not_quote_is_implied_from_the_fit(self):
        """USDCNH is quoted 1W, 2W, 1M ... and CONFIG lists a 3W between them."""
        s = self.book(["USDCNH"])["USDCNH"]
        self.assertNotIn("3W", {m.tenor.upper() for m in s.marks})
        implied = s.implied_marks()
        self.assertEqual(list(implied), ["3W"])
        row = {r["tenor"]: r for r in s.quote_rows()}["3W"]
        # Nothing quoted, nothing marked, nothing fitted -- a reading.
        self.assertTrue(row["implied"])
        self.assertFalse(row["quoted"])
        self.assertFalse(row["fitted"])
        self.assertFalse(row["marked"])
        for f in QUOTE_FIELDS:
            self.assertIsNone(row[f])
            self.assertIsNone(row[f + "_sheet"])
            self.assertIsNotNone(row[f + "_implied"])
        # And it is an interpolation, so it sits between the tenors either side
        # rather than off on its own.
        by = {m.tenor.upper(): m for m in s.marks}
        self.assertLess(by["2W"].rr_25, row["rr_25_implied"])
        self.assertLess(row["rr_25_implied"], by["1M"].rr_25)
        self.assertLess(by["2W"].st_25, row["st_25_implied"])
        self.assertLess(row["st_25_implied"], by["1M"].st_25)

    def test_an_implied_tenor_is_never_fitted_from_its_own_reading(self):
        """The numbers came out of the fit; feeding them back in would make
        the surface a function of its own output."""
        s = self.book(["USDCNH"])["USDCNH"]
        before = [(f.tenor, f.rho25, f.slog25) for f in s.fits]
        s.quote_rows()
        s.calibrate()
        self.assertEqual(before, [(f.tenor, f.rho25, f.slog25) for f in s.fits])

    def test_typing_into_an_implied_row_marks_the_whole_row(self):
        s = self.book(["USDCNH"])["USDCNH"]
        reading = s.implied_marks()["3W"]
        s.warnings.clear()
        s.overwrite_quote("3W", "rr_25", 0.0050)
        # The three that were not typed are the numbers that were already on
        # the row, so one box turns a reading into a pillar rather than into
        # one number and three blanks.
        self.assertEqual(set(s.quote_overwrites["3W"]), set(QUOTE_FIELDS))
        self.assertAlmostEqual(s.quote_overwrites["3W"]["rr_25"], 0.0050)
        for f in ("rr_10", "st_25", "st_10"):
            self.assertAlmostEqual(s.quote_overwrites["3W"][f], getattr(reading, f))
        self.assertTrue(any("taken off the fitted smile" in w for w in s.warnings), s.warnings)
        s.calibrate()
        row = {r["tenor"]: r for r in s.quote_rows()}["3W"]
        self.assertTrue(row["fitted"])
        self.assertTrue(row["marked"])
        self.assertFalse(row["implied"])

    def test_clearing_a_materialised_row_gives_the_reading_back(self):
        s = self.book(["USDCNH"])["USDCNH"]
        was = s.implied_marks()["3W"].rr_25
        s.overwrite_quote("3W", "rr_25", 0.0050)
        s.calibrate()
        s.clear_quote_overwrite("3W")
        s.calibrate()
        row = {r["tenor"]: r for r in s.quote_rows()}["3W"]
        self.assertTrue(row["implied"])
        self.assertAlmostEqual(row["rr_25_implied"], was)

    def test_the_ratio_table_reaches_every_config_tenor(self):
        """A multiple can be set on a listed tenor the sheet has not quoted."""
        s = self.book(["USDCNH"])["USDCNH"]
        self.assertIn("3W", {r["tenor"] for r in s.ratio_rows()})

    def test_the_marking_screen_sends_the_readings_beside_the_sheets_numbers(self):
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDCNH"})["atm"]}
        # CONFIG's list, whole, and nothing beyond it.
        self.assertEqual([r["tenor"].upper() for r in service.marks({"pair": "USDCNH"})["atm"]],
                         [t.upper() for t in service.book.data.tenor_points])
        self.assertTrue(rows["3W"]["implied"])
        self.assertFalse(rows["3W"]["quoted"])
        for f in QUOTE_FIELDS:
            self.assertIsNone(rows["3W"]["quotes"][f])
            self.assertIsNone(rows["3W"]["quotes_sheet"][f])
            self.assertIsNotNone(rows["3W"]["quotes_implied"][f])
        # In points, like every volatility the screen shows.
        self.assertGreater(rows["3W"]["quotes_implied"]["rr_25"], 0.05)
        self.assertFalse(rows["1M"]["implied"])
        self.assertIsNone(rows["1M"]["quotes_implied"]["rr_25"])


class TestWorkbookAsDatabase(unittest.TestCase):
    """The workbook written by the screens rather than opened by a person.

    A store has obligations a book of record does not: a write must not lose
    somebody else's, a clear must actually clear, and the copies it keeps must
    not grow without end.
    """

    def workbook(self) -> Path:
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = d / "vol_marks.xlsx"
        shutil.copy(BOOK, wb)
        return wb

    def marked(self, wb):
        from volkit import session
        book = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        book["USDJPY"].atm.overwrite_tenor("1m", 0.0925)
        return session.capture(book, ["USDJPY"])

    def test_a_write_over_a_workbook_that_moved_is_refused(self):
        """Two volkits, or somebody saving from Excel. Last writer wins is how
        a store loses a morning."""
        from volkit import session
        wb = self.workbook()
        stamp = session.workbook_stamp(wb)
        doc = self.marked(wb)
        session.export_workbook(doc, wb, in_place=True, expect=stamp)
        with self.assertRaises(session.SessionError) as cm:
            session.export_workbook(doc, wb, in_place=True, expect=stamp)
        self.assertIn("changed since", str(cm.exception))
        # A copy takes nothing from anybody and is never refused.
        out = session.export_workbook(doc, wb, wb.with_name("copy.xlsx"), expect=stamp)
        self.assertEqual(out["problems"], [])
        # And a person who has looked can say so.
        forced = session.export_workbook(doc, wb, in_place=True, expect=stamp, force=True)
        self.assertTrue(forced["stale"])

    def test_a_cleared_overwrite_is_cleared_in_the_workbook(self):
        """``ws.cell(row, column, value=None)`` does not blank a cell --
        openpyxl assigns only when the value is not None -- so every clear on
        this path was a no-op and an overwrite taken off on the screen came
        back on the next load."""
        from volkit import session
        wb = self.workbook()
        book = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        book["USDJPY"].atm.overwrite_tenor("1m", 0.0925)
        book["USDJPY"].anchor_tenors = True
        session.export_workbook(session.capture(book, ["USDJPY"]), wb, in_place=True)
        back = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        self.assertEqual(back["USDJPY"].atm.tenor_overwrites, {"1m": 0.0925})
        self.assertTrue(back["USDJPY"].anchor_tenors)

        back["USDJPY"].atm.clear_overwrite("1m")
        back["USDJPY"].anchor_tenors = False
        session.export_workbook(session.capture(back, ["USDJPY"]), wb, in_place=True,
                                force=True)
        after = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        self.assertEqual(after["USDJPY"].atm.tenor_overwrites, {})
        self.assertFalse(after["USDJPY"].anchor_tenors)

    def test_the_copies_go_in_a_folder_and_a_repeated_write_makes_no_second_one(self):
        """Two things that used to fill the workbook's own folder.

        They sat beside the workbook -- the folder a person opens to find the
        workbook -- and a write with nothing new in it left a byte-identical
        copy next to the last one.
        """
        from volkit import session
        wb = self.workbook()
        doc = self.marked(wb)
        for _ in range(4):
            session.export_workbook(doc, wb, in_place=True, force=True)
        self.assertEqual(sorted(wb.parent.glob(f"{wb.stem}{session.BACKUP_INFIX}*")), [],
                         "copies are still beside the workbook")
        rows = session.list_backups(wb)
        self.assertTrue(rows)
        self.assertTrue(all(Path(r["path"]).parent == session.backup_dir(wb) for r in rows))
        # The last write repeated bytes that were already kept, so it kept no
        # second copy of them and said so.
        out = session.export_workbook(doc, wb, in_place=True, force=True)
        self.assertTrue(out["reused"], out["notes"])
        self.assertEqual(len(session.list_backups(wb)), len(rows))
        self.assertTrue(any("already the file kept at" in n for n in out["notes"]),
                        out["notes"])

    def test_two_saves_of_an_unchanged_book_are_the_same_workbook(self):
        """The stamp the library writes is not a change to the workbook.

        openpyxl puts the moment it saved into ``docProps/core.xml``, so two
        saves of a book nobody touched are never the same *bytes*.  Comparing
        the files whole found a difference every time and kept a copy every
        time -- the dedupe passed its first test only because both writes
        landed inside one second.
        """
        import time
        from volkit import session
        wb = self.workbook()
        doc = self.marked(wb)
        session.export_workbook(doc, wb, in_place=True, force=True)
        first = wb.read_bytes()
        time.sleep(1.1)
        session.export_workbook(doc, wb, in_place=True, force=True)
        second = wb.read_bytes()
        self.assertNotEqual(first, second, "openpyxl stopped stamping the save time")
        self.assertEqual(session.content_digest(first), session.content_digest(second))
        # A cell that actually moved is a different workbook, stamp or no.
        book = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        book["USDJPY"].atm.overwrite_tenor("3m", 0.10)
        session.export_workbook(session.capture(book, ["USDJPY"]), wb,
                                in_place=True, force=True)
        self.assertNotEqual(session.content_digest(second),
                            session.content_digest(wb.read_bytes()))

    def test_copies_made_before_there_was_a_folder_are_moved_into_it(self):
        """A rule that could not see them would be a second rule."""
        from volkit import session
        wb = self.workbook()
        loose = wb.with_name(f"{wb.stem}{session.BACKUP_INFIX}20200101-090000{wb.suffix}")
        loose.write_bytes(b"an old copy")
        self.assertEqual([r["name"] for r in session.list_backups(wb)], [loose.name])
        self.assertTrue(session.list_backups(wb)[0]["loose"])
        session.export_workbook(self.marked(wb), wb, in_place=True, force=True)
        self.assertFalse(loose.exists())
        self.assertIn(loose.name, [r["name"] for r in session.list_backups(wb)])
        self.assertFalse(session.list_backups(wb)[-1]["loose"])

    def test_the_copies_are_thinned_by_age_and_not_by_count(self):
        """Twenty saves between lunch and the close used to push out
        yesterday's file, which is the one somebody asks for."""
        from datetime import timedelta
        from volkit import session
        wb = self.workbook()
        d = session.backup_dir(wb, create=True)
        now = datetime.now()
        whens = [now - timedelta(minutes=6 * i) for i in range(40)]          # one afternoon
        whens += [now - timedelta(days=k, hours=3) for k in range(1, 61)]    # two months
        for when in whens:
            (d / f"{wb.stem}{session.BACKUP_INFIX}"
                 f"{when.strftime('%Y%m%d-%H%M%S')}{wb.suffix}").write_bytes(b"x")
        self.assertEqual(len(session.list_backups(wb)), 100)
        gone = session.prune_backups(wb)
        kept = session.list_backups(wb)
        self.assertTrue(30 < len(gone) < 90)
        self.assertLess(len(kept), 40, "thinning kept too many")
        self.assertGreater(len(kept), 10, "thinning kept too few")
        # Fewer files *and* more history: the oldest survivor is weeks back,
        # where the count rule would have stopped inside the afternoon.
        oldest = min(r["when"] for r in kept)
        self.assertLess(oldest, (now - timedelta(days=30)).isoformat(timespec="seconds"))
        # ...and the newest few are all there, because an undo reaches for one
        # of those.
        newest = sorted((r["when"] for r in kept), reverse=True)
        self.assertEqual(len(newest[:session.BACKUP_KEEP_LATEST]),
                         session.BACKUP_KEEP_LATEST)
        # The old rule is still there for a caller that asks for a number.
        session.prune_backups(wb, keep=3)
        self.assertEqual(len(session.list_backups(wb)), 3)

    def test_the_copy_that_cannot_be_rebuilt_is_kept_for_good(self):
        """openpyxl carries no chart through a round trip, so the copy taken
        before the write that flattens one is the only place it still exists.
        Every copy after it is the previous one plus a session document, which
        is why the rest can be thinned hard."""
        import zipfile
        from volkit import session
        wb = self.workbook()
        # A workbook with a part a write would drop.
        blob = wb.read_bytes()
        with zipfile.ZipFile(wb, "a") as z:
            z.writestr("xl/charts/chart1.xml", "<c/>")
        self.assertTrue(session.round_trip_losses(wb))
        kept = session.keep_backup(wb, wb.read_bytes())
        self.assertTrue(kept["origin"])
        self.assertIn(session.ORIGIN_INFIX, Path(kept["origin"]).stem)
        self.assertTrue(any("kept for good" in n for n in kept["notes"]), kept["notes"])
        # Now flat, so later copies are ordinary ones...
        wb.write_bytes(blob)
        session.keep_backup(wb, blob)
        rows = session.list_backups(wb)
        self.assertEqual(sum(1 for r in rows if r["origin"]), 1)
        # ...and no amount of thinning touches the one that matters.
        session.prune_backups(wb, keep=0)
        rows = session.list_backups(wb)
        self.assertEqual([r["origin"] for r in rows], [True])

    def test_the_screen_and_the_command_read_the_same_versions(self):
        """The four pieces: the model, a route, a subcommand on the same
        function, and the card.  A history the GUI could see and the CLI could
        not would be two histories."""
        import io as _io
        from contextlib import redirect_stdout
        from volkit import cli, session
        from volkit.webapp import BookService
        wb = self.workbook()
        service = BookService(str(wb), ASOF)
        service.overwrite({"pair": "USDJPY", "kind": "atm", "tenor": "1M", "value": 9.25})
        service.session_save({"path": str(wb.parent / "marks.json")})
        service.session_export({"path": str(wb.parent / "marks.json")})
        seen = service.workbook_versions()
        self.assertEqual([r["name"] for r in seen["backups"]],
                         [r["name"] for r in session.list_backups(wb)])
        self.assertEqual(seen["folder"], session.backup_dir(wb).name)
        self.assertEqual(len(seen["writes"]), 1)
        buf = _io.StringIO()
        with redirect_stdout(buf):
            cli.main(["-w", str(wb), "versions"])
        text = buf.getvalue()
        self.assertIn(seen["backups"][0]["name"], text)
        self.assertIn("write(s), newest first", text)
        # And the restore is the same function under both.
        name = seen["backups"][0]["name"]
        out = service.workbook_restore({"name": name})
        self.assertTrue(out["ok"])
        self.assertEqual(service.book["USDJPY"].atm.tenor_overwrites, {},
                         "the restore did not put the workbook back")

    def test_a_copy_can_be_put_back_and_that_is_undoable_too(self):
        from volkit import session
        wb = self.workbook()
        before = wb.read_bytes()
        session.export_workbook(self.marked(wb), wb, in_place=True, force=True)
        self.assertNotEqual(wb.read_bytes(), before)
        name = [r["name"] for r in session.list_backups(wb)][0]
        out = session.restore_backup(wb, name)
        self.assertEqual(wb.read_bytes(), before)
        self.assertEqual(out["restored"], name)
        # What the restore replaced is kept, so the undo has an undo.
        self.assertTrue(Path(out["backup"]).exists())
        self.assertNotEqual(Path(out["backup"]).read_bytes(), before)
        with self.assertRaises(session.SessionError):
            session.restore_backup(wb, "not-a-copy.xlsx")

    def test_every_write_is_logged_and_an_export_keeps_the_marks_beside_it(self):
        """The cheap half of the history.

        A copy of the workbook is tens or hundreds of kilobytes and there are
        a couple of dozen of them; a line of the log is a few hundred bytes
        and there is one per write, for good.  So a write whose copy has been
        thinned away is still on the record, and where the write was an export
        the session document beside it says what was marked.
        """
        from volkit import session
        wb = self.workbook()
        doc = self.marked(wb)
        out = session.export_workbook(doc, wb, in_place=True, force=True)
        session.write_config_tabs(wb, {"PEG_BANDS": [
            {"pair": "USDHKD", "lower": 7.75, "upper": 7.85, "note": "HKMA"}]})
        log = session.read_history(wb)
        self.assertEqual([r["what"] for r in log], ["configuration tab", "marks"])
        self.assertEqual(log[1]["pairs"], ["USDJPY"])
        self.assertEqual(log[0]["tabs"], ["PEG_BANDS"])
        # The document is the marks, and it loads as the session it came from.
        snap = session.history_dir(wb) / log[1]["document"]
        self.assertTrue(snap.exists())
        self.assertLess(snap.stat().st_size, wb.stat().st_size / 4,
                        "a session document should be a fraction of a workbook")
        back = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        back["USDJPY"].atm.clear_overwrite("1m")
        session.apply_document(back, session.load(snap), ["USDJPY"])
        self.assertEqual(back["USDJPY"].atm.tenor_overwrites, {"1m": 0.0925})
        # A copy written somewhere else is that file's history, not this one's.
        n = len(session.read_history(wb))
        session.export_workbook(doc, wb, wb.with_name("elsewhere.xlsx"))
        self.assertEqual(len(session.read_history(wb)), n)

    def test_a_configuration_tab_is_written_and_read_back(self):
        from volkit import session
        wb = self.workbook()
        rows = [{"pair": "USDHKD", "lower": 7.75, "upper": 7.85, "note": "HKMA"},
                {"pair": "USDTRY", "lower": 30.0, "upper": 45.0, "note": "made up"}]
        session.write_config_tabs(wb, {"PEG_BANDS": rows})
        book = Book.from_excel(wb, ASOF)
        self.assertEqual(sorted(book.bands), ["USDHKD", "USDTRY"])
        # A tab this does not write is refused by name rather than written
        # somewhere it would not be read.
        with self.assertRaises(session.SessionError):
            session.write_config_tabs(wb, {"PARAMS": []})

    def test_a_pair_is_added_and_removed_through_the_workbook(self):
        from volkit import session
        wb = self.workbook()
        out = session.add_pair(wb, "USDSGD", atm=6.5, quotes={
            "3M": {"rr_25": -0.40, "st_25": 0.24, "rr_10": -0.74, "st_10": 0.72}})
        self.assertEqual(out["quoted"], 1)
        book = Book.from_excel(wb, ASOF).load_all(["USDSGD"])
        self.assertEqual(book.data.problems, [])
        self.assertIn("USDSGD", book.pairs)
        self.assertEqual([f.tenor for f in book["USDSGD"].fits], ["3M"])

        # A cross is its correlation, not a volatility, and says so.
        with self.assertRaises(session.SessionError) as cm:
            session.add_pair(wb, "SGDJPY", atm=6.5)
        self.assertIn("correlation", str(cm.exception))

        # Removing takes it out of CONFIG and leaves its work where it is, so
        # adding it back finds what it had.
        session.remove_pair(wb, "USDSGD")
        self.assertNotIn("USDSGD", Book.from_excel(wb, ASOF).data.pairs)
        session.add_pair(wb, "USDSGD", atm=6.5)
        again = Book.from_excel(wb, ASOF).load_all(["USDSGD"])
        self.assertEqual([m.tenor for m in again["USDSGD"].marks], ["3M"])

    def test_a_pair_with_its_columns_and_no_quotes_is_said_not_refused(self):
        """A pair created on the screen and not yet marked is a real state
        now; a check that goes red on it is a check people stop reading."""
        from volkit import session
        wb = self.workbook()
        session.add_pair(wb, "USDSGD", atm=6.5)
        data = Book.from_excel(wb, ASOF).data
        self.assertEqual(data.problems, [])
        self.assertTrue(any("no quotes yet" in n for n in data.notes), data.notes)


class TestSessionFile(unittest.TestCase):
    """Saving the marks a session made, and putting them back.

    The workbook is never written to (a standing decision), so a morning's
    marking only survives in this file.  What is pinned here is that the round
    trip is exact, that the file is in the units the screen shows, and that a
    file with a bad number in it still restores everything else and says what
    it could not.
    """

    def marked_book(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        surface = book["USDJPY"]
        surface.atm.overwrite_tenor("1m", 0.0925)
        surface.overwrite_param("slog25", "3M", 0.61)
        surface.set_param_term("rho10", -0.2, -0.05, 1.5)
        surface.set_param_shifts({"rho25": 0.05})
        surface.anchor_tenors = True
        surface.atm.set_params(long_term_vol=0.081)
        return book

    def test_a_session_round_trips_onto_a_fresh_book(self):
        from volkit import session
        marked = self.marked_book()
        doc = session.capture(marked)
        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        expiry = ASOF.datetime_from_years(0.25)
        self.assertNotAlmostEqual(float(marked["USDJPY"].vol(1.0, expiry)),
                                  float(fresh["USDJPY"].vol(1.0, expiry)), places=6)
        out = session.apply_document(fresh, doc)
        self.assertEqual(out["problems"], [])
        self.assertIn("USDJPY", out["applied"])
        self.assertEqual(float(marked["USDJPY"].vol(1.0, expiry)),
                         float(fresh["USDJPY"].vol(1.0, expiry)))
        self.assertEqual(fresh["USDJPY"].param_shifts, {"rho25": 0.05})
        self.assertTrue(fresh["USDJPY"].anchor_tenors)

    def test_the_file_is_in_the_units_the_screen_shows(self):
        """Volatility points at the edges, decimals in the middle (§4).

        A file written in decimals and read back in points is the bank-width
        bug again: a 0.28 market read as 28 points.
        """
        from volkit import session
        doc = session.capture(self.marked_book(), ["USDJPY"])
        block = doc["pairs"]["USDJPY"]
        self.assertAlmostEqual(block["atm_overwrites"]["1m"], 9.25)
        self.assertAlmostEqual(block["curve"]["long_term_vol"], 8.1)
        # A smile parameter is not a volatility and is not scaled.
        self.assertAlmostEqual(block["smile_overwrites"]["slog25"]["3M"], 0.61)

    def test_the_screen_and_the_file_read_the_curve_the_same_way(self):
        """One conversion, shared, so the two cannot come to disagree."""
        from volkit import session
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        shown = service.curve({"pair": "USDJPY"})["params"]
        self.assertEqual(shown, session.curve_params(service.book["USDJPY"].atm))

    def test_a_bad_value_does_not_take_the_rest_of_the_file_down(self):
        from volkit import session
        doc = session.capture(self.marked_book())
        doc["pairs"]["USDJPY"]["atm_overwrites"]["1m"] = "not a number"
        doc["pairs"]["ZZZFAKE"] = {"curve": {}}
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        out = session.apply_document(book, doc)
        self.assertIn("EURUSD", out["applied"])
        self.assertTrue(any("ZZZFAKE" in x for x in out["problems"]))
        self.assertTrue(any("not a number" in x for x in out["problems"]))
        # The rest of USDJPY still went on.
        self.assertEqual(book["USDJPY"].param_shifts, {"rho25": 0.05})

    def test_event_weights_and_adjustment_survive_the_round_trip(self):
        """The file holds the whole event table -- a row per release, weights
        per currency and an adjustment per pair -- and each pair block keeps
        its resolved schedule for the record."""
        from volkit import session
        from volkit.events import EventEntry
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        when = (ASOF.now + timedelta(days=13)).replace(hour=16, minute=0, second=0, microsecond=0)
        # A mark goes onto the table, which is what reaches the curves; a
        # currency weight is shared and has nowhere else to live.
        book.events.set_pair("USDJPY", [
            EventEntry(when, None, "FOMC", {"USD": 0.015, "JPY": 0.003}, 0.002)],
            pairs=book.data.pairs)
        book.apply_events()
        doc = session.capture(book, ["USDJPY"])
        row = doc["pairs"]["USDJPY"]["events"][0]
        self.assertAlmostEqual(row["bump"], 2.0)
        self.assertAlmostEqual(row["weights"]["JPY"], 0.3)
        self.assertAlmostEqual(row["adjust"], 0.2)
        table = doc["event_table"][0]
        self.assertAlmostEqual(table["weights"]["USD"], 1.5)
        self.assertAlmostEqual(table["adjust"]["USDJPY"], 0.2)

        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        out = session.apply_document(fresh, doc)
        self.assertEqual(out["problems"], [])
        ev = fresh["USDJPY"].atm.events.events[0]
        self.assertAlmostEqual(ev.bump, 0.020)
        self.assertAlmostEqual(ev.weights["JPY"], 0.003)
        self.assertAlmostEqual(ev.adjust, 0.002)

    def test_a_file_from_before_the_event_table_is_rebuilt_from_its_pairs(self):
        """An older session file spread the same events across its pairs.  A
        currency weight was shared even then, so the table is unioned back
        out of them rather than the events being dropped."""
        from volkit import session
        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        when = (ASOF.now + timedelta(days=13)).replace(hour=16, minute=0, second=0, microsecond=0)
        old = {"pairs": {"USDJPY": {"events": [
            {"when": when.strftime("%Y-%m-%dT%H:%M"), "bump": 1.25, "label": "OLD"}]}}}
        out = session.apply_document(fresh, old)
        self.assertEqual(out["problems"], [])
        self.assertTrue(any("rebuilt from its pairs" in n for n in out["notes"]), out["notes"])
        ev = fresh["USDJPY"].atm.events.events[0]
        self.assertAlmostEqual(ev.bump, 0.0125)
        self.assertAlmostEqual(ev.adjust, 0.0125)

    def test_two_pairs_disagreeing_about_a_currency_weight_are_reported(self):
        """In an older file the same weight sat in two pair blocks and could
        drift apart.  Rebuilding the table names the disagreement rather than
        averaging it -- §4, a total that disagrees with its parts is refused."""
        from volkit import session
        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        when = (ASOF.now + timedelta(days=13)).replace(hour=16, minute=0, second=0, microsecond=0)
        stamp = when.strftime("%Y-%m-%dT%H:%M")
        old = {"pairs": {
            "USDJPY": {"events": [{"when": stamp, "weights": {"USD": 1.5, "JPY": 0.0},
                                   "adjust": 0.0, "label": "FOMC"}]},
            "EURUSD": {"events": [{"when": stamp, "weights": {"USD": 2.0, "EUR": 0.0},
                                   "adjust": 0.0, "label": "FOMC"}]}}}
        out = session.apply_document(fresh, old)
        self.assertTrue(any("disagrees" in p and "USD" in p for p in out["problems"]),
                        out["problems"])

    def test_the_event_table_is_saved_with_the_session(self):
        from volkit import session
        from volkit.events import EventEntry
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        when = (ASOF.now + timedelta(days=13)).replace(hour=16, minute=0, second=0, microsecond=0)
        book.events.set_pair("USDJPY", [EventEntry(when, None, "FOMC", {"USD": 0.015}, 0.0)],
                             pairs=book.data.pairs)
        book.apply_events()
        doc = session.capture(book, ["USDJPY"])
        self.assertEqual(len(doc["event_table"]), 1)
        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        out = session.apply_document(fresh, doc)
        self.assertEqual(out["problems"], [])
        # Saved for one pair, restored for the book: the weight is the
        # dollar's, so EURUSD takes it too.
        self.assertAlmostEqual(fresh["EURUSD"].atm.events.events[0].bump, 0.015)
        # A file from before the table existed leaves the book's alone.
        older = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        before = len(older.events.rows)
        session.apply_document(older, {"pairs": {}})
        self.assertEqual(len(older.events.rows), before)

    def test_events_are_replaced_and_not_merged(self):
        """A saved table is the whole table.

        Merging would double every release that appears in both the workbook
        and the file, which nothing downstream could tell from a real bump.
        """
        from volkit import session
        from volkit.events import EventEntry
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        when = ASOF.now + timedelta(days=10)
        book.events.set_pair("EURUSD", [EventEntry(when, None, "TEST", {}, 0.004)],
                             pairs=book.data.pairs)
        book.apply_events()
        doc = session.capture(book, ["EURUSD"])
        self.assertEqual(len(doc["event_table"]), 1)
        fresh = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        session.apply_document(fresh, doc)
        self.assertEqual(len(fresh["EURUSD"].atm.events.events), 1)
        # Applying it twice must not stack the same release up.
        session.apply_document(fresh, doc)
        self.assertEqual(len(fresh["EURUSD"].atm.events.events), 1)

    def test_the_file_is_written_atomically_and_read_back(self):
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "marks.json"
            written = session.save(self.marked_book(), path, note="morning")
            self.assertEqual(Path(written), path)
            doc = session.load(path)
            self.assertEqual(doc["note"], "morning")
            self.assertIn("USDJPY", doc["pairs"])
        with self.assertRaises(session.SessionError):
            session.load(Path(tmp) / "gone.json")

    def test_a_pair_the_workbook_does_not_build_is_reported_not_skipped(self):
        from volkit import session
        doc = {"pairs": {"NOTAPAIR": {"curve": {}}}, "version": 1}
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        out = session.apply_document(book, doc)
        self.assertEqual(out["applied"], [])
        self.assertTrue(any("NOTAPAIR" in x for x in out["problems"]))

    def test_the_service_saves_and_restores_over_the_api(self):
        import tempfile
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "marks.json")
            service = BookService(str(BOOK), ASOF)
            service.overwrite({"pair": "USDJPY", "kind": "atm", "tenor": "1m", "value": "9.25"})
            saved = service.session_save({"path": path})
            self.assertTrue(saved["ok"])
            service.reload()                       # back to the workbook's own marks
            self.assertEqual(service.book["USDJPY"].atm.tenor_overwrites, {})
            out = service.session_load({"path": path})
            self.assertTrue(out["ok"], out["problems"])
            self.assertAlmostEqual(service.book["USDJPY"].atm.tenor_overwrites["1m"], 0.0925)


class TestSessionIntoWorkbook(unittest.TestCase):
    """A session file written into a workbook's own cells.

    The one deliberate exception to "nothing writes to the workbook", so
    what is pinned is what makes it safe: it writes a copy unless told
    otherwise, the original's bytes do not move, and the copy loads as the
    session it came from -- every kind of mark, to the last digit, through
    the ordinary reader.  Cells the tool would not read back would be the
    silent zero this project exists to remove.
    """

    PAIRS = ["USDJPY", "EURUSD", "EURJPY"]

    def marked_session(self, tmp: Path):
        from volkit import session
        from volkit.banded import BandTreatment
        from volkit.events import EventEntry
        book = Book.from_excel(BOOK, ASOF).load_all(self.PAIRS)
        s = book["USDJPY"]
        s.atm.overwrite_tenor("1m", 0.0925)
        s.overwrite_param("slog25", "3M", 0.61)
        s.set_param_term("rho10", -0.2, -0.05, 1.5)
        s.set_param_shifts({"rho25": 0.05})
        s.anchor_tenors = True
        s.atm.set_params(long_term_vol=0.081)
        # A Tuesday release, with weights on both legs and an adjustment.
        # It goes on the book's event table, which is where every event lives:
        # the USD weight is EURUSD's too, and the 0.2 adjustment is USDJPY's
        # alone.
        when = (ASOF.now + timedelta(days=13)).replace(hour=13, minute=30)
        book.events.set_pair("USDJPY", [
            EventEntry(when, None, "NFP", {"USD": 0.004, "JPY": 0.001}, 0.002)],
            pairs=book.data.pairs)
        book.apply_events()
        s.set_band_treatment(BandTreatment.from_request({"mode": "warn", "hazard": 3}))
        book["EURJPY"].atm.correlation  # a cross: its curve is a correlation
        path = tmp / "marks.json"
        session.save(book, path)
        return book, session.load(path)

    def test_the_copy_loads_as_the_session_and_the_original_does_not_move(self):
        import hashlib
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            # The shipped file, not the fixture: the last two assertions are
            # about the export keeping the sheet's *array formulas* and their
            # cached values, and a workbook rebuilt by openpyxl carries the
            # values with no formulas behind them -- nothing to preserve.
            shutil.copy(WORKBOOK, wb)
            before = hashlib.md5(wb.read_bytes()).hexdigest()
            book, doc = self.marked_session(tmp)
            out = session.export_workbook(doc, wb)
            self.assertEqual(out["problems"], [])
            self.assertEqual(Path(out["written"]), tmp / "vol_marks_marked.xlsx")
            self.assertEqual(hashlib.md5(wb.read_bytes()).hexdigest(), before)

            # The reference is the session put on a fresh book *and the smiles
            # recalibrated against it*, which is what a workbook load does.
            ref = Book.from_excel(BOOK, ASOF).load_all(self.PAIRS)
            session.apply_document(ref, doc)
            ref.calibrate_smiles()
            copy = Book.from_excel(out["written"], ASOF).load_all(self.PAIRS)
            self.assertEqual(copy.data.problems, [])
            self.assertFalse([w for w in copy.warnings if "session mark" in w], copy.warnings)
            for pair in self.PAIRS:
                for t in (0.02, 0.08, 0.25, 1.0):
                    expiry = ASOF.datetime_from_years(t)
                    for k in (0.97, 1.0, 1.03):
                        self.assertAlmostEqual(float(ref[pair].vol(k, expiry)),
                                               float(copy[pair].vol(k, expiry)), places=9,
                                               msg=(pair, t, k))
            # Every kind of mark came back through the reader, not just the vols.
            s = copy["USDJPY"]
            self.assertAlmostEqual(s.atm.tenor_overwrites["1m"], 0.0925)
            self.assertEqual(s.param_overwrites, {"slog25": {"3M": 0.61}})
            self.assertEqual(s.param_shifts, {"rho25": 0.05})
            self.assertEqual(sorted(s.term_marks), ["rho10"])
            self.assertAlmostEqual(s.term_marks["rho10"].initial, -0.2)
            self.assertAlmostEqual(s.term_marks["rho10"].final, -0.05)
            self.assertAlmostEqual(s.term_marks["rho10"].decay, 1.5)
            self.assertTrue(s.anchor_tenors)
            self.assertAlmostEqual(s.band_treatment.jump.hazard, 0.03)
            ev = s.atm.events.events[-1]
            self.assertAlmostEqual(ev.bump, 0.007)
            self.assertEqual(ev.weights, {"USD": 0.004, "JPY": 0.001})
            self.assertAlmostEqual(ev.adjust, 0.002)
            # ...and the sheet's formulas are still formulas, with their values.
            # Asked as ``startswith("=")`` this missed the shipped workbook's
            # own spelling: Excel saves ``=C2*3`` as an *array* formula and
            # openpyxl returns an ``ArrayFormula`` object, not a string.  The
            # export dropped the cached value of every such cell and the copy
            # came back with 126 blank quotes, while this line read a repr.
            import openpyxl
            ws = openpyxl.load_workbook(out["written"])["USDJPY"]
            self.assertTrue(session._is_formula(ws["B2"].value), ws["B2"].value)
            self.assertIsNotNone(openpyxl.load_workbook(out["written"], data_only=True)
                                 ["USDJPY"]["B2"].value)

    def test_an_array_formula_keeps_its_cached_value_through_a_copy(self):
        """Excel writes ``=C2*3`` as an array formula ({=C2*3}, the CSE
        spelling), openpyxl hands it back as an ``ArrayFormula`` object and
        writes it as ``<f t="array" ref="B2">``.  Both halves of the cache
        restore missed it -- ``_formula_cache`` tested ``startswith("=")`` on
        a non-string, and the substitution matched only a bare ``<f>`` -- so
        the copy came back with every quote blank.  The shipped workbook's
        smile sheets are array formulas throughout, which is how this reached
        the Windows build as 126 "blank quote" problems.
        """
        import io
        import tempfile
        import openpyxl
        from openpyxl.worksheet.formula import ArrayFormula
        from volkit import session

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        path = d / "arrays.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "USDJPY"
        ws["A1"], ws["B1"] = "expiry", "ST 10D"
        ws["C2"] = 0.2175
        ws["B2"] = ArrayFormula("B2", "=C2*3")
        wb.save(path)

        blob = path.read_bytes()
        wb = openpyxl.load_workbook(io.BytesIO(blob))
        vals = openpyxl.load_workbook(io.BytesIO(blob), data_only=True)
        # Nothing has computed it yet, so seed the value the way Excel would.
        vals["USDJPY"]["B2"] = 0.6525
        cached = session._formula_cache(wb, vals)
        self.assertEqual(cached, {1: {"B2": 0.6525}})

        buf = io.BytesIO()
        wb.save(buf)
        out = d / "copy.xlsx"
        out.write_bytes(session._restore_formula_cache(buf.getvalue(), cached))
        # Still a formula, and now readable without opening Excel first.
        kept = openpyxl.load_workbook(out)["USDJPY"]["B2"].value
        self.assertTrue(session._is_formula(kept), kept)
        self.assertAlmostEqual(
            openpyxl.load_workbook(out, data_only=True)["USDJPY"]["B2"].value, 0.6525)

    def test_a_re_quoted_tenor_goes_into_the_sheets_own_cell(self):
        """The one mark that is not written to a row of its own.

        A quote is the sheet's own number, so it replaces the sheet's own
        cell and the pair tab is then the database it is being used as.  The
        copy has to load with the typed quote in it and with every quote
        nobody touched exactly as it was -- including the array formulas the
        shipped workbook's smile sheets are made of, which is the failure
        mode this write is one cell away from at all times.
        """
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            # USDCNH, because the new tenor has to be one CONFIG lists: the
            # sheet is quoted 2W then 1M and the TENORS column names a 3W
            # between them, so that row is a reading until it is typed into.
            book = Book.from_excel(BOOK, ASOF).load_all(["USDCNH"])
            s = book["USDCNH"]
            untouched = {m.tenor.upper(): (m.st_10, m.rr_10) for m in s.marks}
            s.overwrite_quote("3M", "rr_25", 0.009)
            for name, v in (("rr_25", 0.005), ("st_25", 0.002),
                            ("rr_10", 0.009), ("st_10", 0.0065)):
                s.overwrite_quote("3W", name, v)
            s.calibrate()
            doc = session.capture(book, ["USDCNH"])
            out = session.export_workbook(doc, wb)
            self.assertEqual(out["problems"], [])
            self.assertTrue(any("newly quoted tenor" in n for n in out["notes"]), out["notes"])

            copy = Book.from_excel(out["written"], ASOF).load_all(["USDCNH"])
            self.assertEqual(copy.data.problems, [], copy.data.problems)
            marks = {m.tenor.upper(): m for m in copy["USDCNH"].marks}
            self.assertAlmostEqual(marks["3M"].rr_25, 0.009)
            self.assertAlmostEqual(marks["3W"].st_10, 0.0065)
            # Everything nobody typed into is the number it was, formulas
            # included -- 1W's strangle is an array formula on this workbook.
            for tenor, (st10, rr10) in untouched.items():
                self.assertAlmostEqual(marks[tenor].st_10, st10, msg=tenor)
                self.assertAlmostEqual(marks[tenor].rr_10, rr10, msg=tenor)
            # And the copy is the session: the workbook's quotes now say what
            # the session's overwrites said, so the two fit the same smile.
            for t in (0.08, 0.25, 1.0):
                expiry = ASOF.datetime_from_years(t)
                for k in (0.97, 1.0, 1.03):
                    self.assertAlmostEqual(float(s.vol(k, expiry)),
                                           float(copy["USDCNH"].vol(k, expiry)),
                                           places=9, msg=(t, k))

    def test_a_half_typed_new_tenor_is_refused_rather_than_half_written(self):
        """A row the reader would call a blank quote is not written at all,
        and the export says so where it happened."""
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            # Uncalibrated on purpose: with a fit behind it, typing one quote
            # at a CONFIG tenor the sheet does not quote takes the other three
            # off the fitted smile and there is no half-typed row to refuse
            # (``TestConfigTenorsGovern``).  This is the other case.
            book = Book.from_excel(BOOK, ASOF).build(["USDCNH"])
            book["USDCNH"].overwrite_quote("3W", "rr_25", 0.005)
            doc = session.capture(book, ["USDCNH"])
            out = session.export_workbook(doc, wb)
            self.assertTrue(any("all four" in p for p in out["problems"]), out["problems"])
            copy = Book.from_excel(out["written"], ASOF).load_all(["USDCNH"])
            self.assertEqual(copy.data.problems, [], copy.data.problems)
            self.assertNotIn("3W", {m.tenor.upper() for m in copy["USDCNH"].marks})

    def test_the_events_sheet_is_written_whole_and_a_weight_reaches_every_pair(self):
        """USD 0.4 on the NFP row belongs to every pair with a dollar in it,
        so the sheet is written once from the file's one table rather than
        pair by pair.  The old export wrote a pair's view of a shared weight
        and then had to cancel it in every other pair's column; a table
        written whole has nothing to cancel."""
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            _, doc = self.marked_session(tmp)
            out = session.export_workbook(doc, wb)
            self.assertEqual(out["problems"], [])
            self.assertTrue(any("EVENTS" in n and "written whole" in n for n in out["notes"]),
                            out["notes"])
            copy = Book.from_excel(out["written"], ASOF).load_all(["EURUSD", "USDJPY"])
            self.assertEqual(copy.data.problems, [], copy.data.problems)
            # One row, and both pairs read it: EURUSD takes the dollar leg
            # alone, USDJPY takes both legs and its own cell.
            eu = [e for e in copy["EURUSD"].atm.events.events if e.when > ASOF.now]
            self.assertEqual([round(e.bump, 12) for e in eu], [0.004])
            self.assertAlmostEqual(eu[0].adjust, 0.0)
            uj = copy["USDJPY"].atm.events.events[-1]
            # A named release keeps its name through the sheet.
            self.assertEqual(uj.label, "NFP")
            self.assertEqual(uj.weights, {"USD": 0.004, "JPY": 0.001})
            self.assertAlmostEqual(uj.bump, 0.007)
            self.assertAlmostEqual(uj.adjust, 0.002)

    def test_writing_over_the_workbook_itself_needs_in_place(self):
        import hashlib
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            before = hashlib.md5(wb.read_bytes()).hexdigest()
            _, doc = self.marked_session(tmp)
            with self.assertRaises(session.SessionError):
                session.export_workbook(doc, wb, wb)
            out = session.export_workbook(doc, wb, wb, in_place=True)
            self.assertEqual(Path(out["written"]), wb)
            # The bytes it replaced are kept beside it: openpyxl does not
            # carry images or charts through a round trip, so the backup is
            # the only way back from an export.
            self.assertTrue(out["backup"], out["notes"])
            bak = Path(out["backup"])
            self.assertTrue(bak.exists())
            self.assertEqual(hashlib.md5(bak.read_bytes()).hexdigest(), before)
            self.assertTrue(bak.name.startswith("vol_marks.bak-"))
            self.assertTrue(any("kept at" in n for n in out["notes"]), out["notes"])
            copy = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
            self.assertAlmostEqual(copy["USDJPY"].atm.tenor_overwrites["1m"], 0.0925)
            # A second export onto the same file reuses its rows rather than
            # adding another 'atm 1m' under the first.  It also names no
            # output: in_place *is* the destination -- naming none and asking
            # for in place used to write the ``_marked`` copy and report it
            # as having gone into the workbook.
            again = session.export_workbook(doc, wb, in_place=True)
            self.assertEqual(Path(again["written"]), wb)
            self.assertFalse((tmp / "vol_marks_marked.xlsx").exists())
            import openpyxl
            labels = [r[0] for r in openpyxl.load_workbook(wb)["PARAMS"]
                      .iter_rows(min_row=2, values_only=True) if r[0] is not None]
            self.assertEqual(labels.count("atm 1m"), 1)
            self.assertEqual(labels.count("slog25 3m"), 1)
            self.assertEqual(labels.count("shift rho25"), 1)
            self.assertEqual(labels.count("anchor"), 1)

    def test_a_pair_the_workbook_has_no_column_for_is_reported_not_added(self):
        import shutil
        import tempfile
        from volkit import session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            doc = {"pairs": {"USDTRY": {"curve": {"initial_vol": 20.0}}}, "version": 1}
            out = session.export_workbook(doc, wb)
            self.assertEqual(out["written"], "")
            self.assertTrue(any("USDTRY" in p and "no PARAMS column" in p
                                for p in out["problems"]), out["problems"])
            self.assertFalse((tmp / "vol_marks_marked.xlsx").exists())

    def test_the_reader_reads_the_rows_the_export_writes(self):
        """The vocabulary, pinned on its own: a label the reader does not
        know is still reported, and a blank row is not a label."""
        from volkit.marketdata import overlay_label
        self.assertEqual(overlay_label("atm 1m"), ("atm", "1m"))
        self.assertEqual(overlay_label("ATM 1M"), ("atm", "1m"))
        self.assertEqual(overlay_label("slog25 3m"), ("smile", "slog25", "3m"))
        self.assertEqual(overlay_label("shift rho25"), ("shift", "rho25"))
        self.assertEqual(overlay_label("Anchor"), ("anchor",))
        self.assertIsNone(overlay_label("shift nothing"))
        self.assertIsNone(overlay_label("atm soon"))
        self.assertIsNone(overlay_label("initial"))

    def test_the_cli_writes_a_copy_and_the_route_writes_the_workbook_itself(self):
        import hashlib
        import io
        import shutil
        import tempfile
        from contextlib import redirect_stdout
        from volkit import cli
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wb = tmp / "vol_marks.xlsx"
            shutil.copy(BOOK, wb)
            before = hashlib.md5(wb.read_bytes()).hexdigest()
            _, doc = self.marked_session(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["-w", str(wb), "session", str(tmp / "marks.json"),
                               "--to-workbook", "--pair", "USDJPY"])
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertIn("vol_marks_marked.xlsx", buf.getvalue())
            self.assertIn("is unchanged", buf.getvalue())
            self.assertEqual(hashlib.md5(wb.read_bytes()).hexdigest(), before)

            service = BookService(str(wb), ASOF)
            # A named output is still a copy, and leaves the workbook alone.
            out = service.session_export({"path": str(tmp / "marks.json"),
                                          "out": str(tmp / "via_route.xlsx")})
            self.assertTrue(out["ok"], out["problems"])
            self.assertEqual(Path(out["written"]), tmp / "via_route.xlsx")
            self.assertFalse(out["backup"])
            self.assertEqual(hashlib.md5(wb.read_bytes()).hexdigest(), before)
            # Naming a *named* output that is the workbook is still refused:
            # `out` means a copy there, and the in-place write is the one the
            # button asks for by naming nothing.
            with self.assertRaises(Exception):
                service.session_export({"path": str(tmp / "marks.json"), "out": str(wb)})

            # With no output named the route writes the loaded workbook, and
            # the marks it wrote read back through the ordinary reader.
            out = service.session_export({"path": str(tmp / "marks.json")})
            self.assertTrue(out["ok"], out["problems"])
            self.assertEqual(Path(out["written"]), wb)
            self.assertTrue(out["in_place"])
            self.assertNotEqual(hashlib.md5(wb.read_bytes()).hexdigest(), before)
            self.assertEqual(hashlib.md5(Path(out["backup"]).read_bytes()).hexdigest(),
                             before)
            back = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
            self.assertAlmostEqual(back["USDJPY"].atm.tenor_overwrites["1m"], 0.0925)


class TestWorkbooksAreNotHeldOpen(unittest.TestCase):
    """This tool reads other people's files; it must not lock them.

    ``pd.ExcelFile(path)`` keeps the file open for as long as the reader is
    alive, and openpyxl's workbook is full of parent/child cycles, so the
    handle outlived the call that made it.  On Windows that is enough to stop
    Excel saving the very sheet the tool had just read: the reported bug was
    that a loaded historical workbook could no longer be saved.
    """

    class _Tracked:
        """Patches ``pd.ExcelFile`` and records every reader that is made."""

        def __enter__(self):
            import pandas as pd
            self.pd, self.real, self.made = pd, pd.ExcelFile, []
            made = self.made

            class Tracking(self.real):
                def __init__(inner, io, *a, **kw):
                    # What was handed to the reader is kept here rather than
                    # read back off it afterwards: pandas 3.0 dropped the
                    # ``ExcelFile.io`` attribute this used to look at, and the
                    # whole suite failed on the runner with "'Tracking' object
                    # has no attribute 'io'".  The argument is the subject of
                    # the test and this is the one place that has it whatever
                    # pandas does with it next.
                    inner.volkit_io = io
                    inner.volkit_closed = False
                    made.append(inner)
                    super().__init__(io, *a, **kw)

                def close(inner):
                    inner.volkit_closed = True
                    return super().close()

            pd.ExcelFile = Tracking
            return self

        def __exit__(self, *exc):
            self.pd.ExcelFile = self.real
            return False

    def check(self, made):
        import io as _io
        self.assertTrue(made, "nothing was read, so this proves nothing")
        for reader in made:
            # Over a copy in memory, never over the path: the file itself is
            # opened, copied and closed before any parsing starts.
            self.assertIsInstance(reader.volkit_io, _io.BytesIO)
            self.assertTrue(reader.volkit_closed,
                            "a reader was left open after the file had been read")

    def test_the_marks_workbook_is_not_left_open(self):
        with self._Tracked() as t:
            ExcelSource(BOOK).load()
        self.check(t.made)

    def test_the_historical_workbook_is_not_left_open(self):
        sample = Path(__file__).resolve().parents[1] / "files" / "history_sample.xlsx"
        with self._Tracked() as t:
            hist = history.load_history(sample)
        self.assertTrue(hist.pairs)
        self.check(t.made)

    def test_a_forward_sheet_is_not_left_open(self):
        sample = Path(__file__).resolve().parents[1] / "files" / "history_sample.xlsx"
        with self._Tracked() as t:
            with self.assertRaises(Exception):
                # Whatever the sheet turns out to be, the reader closes: a
                # file left open by a failed read is the same lock.
                analytics.ForwardCurve.from_excel(sample, "NOT_A_SHEET")
        self.check(t.made)

    def test_a_loaded_workbook_can_still_be_replaced(self):
        """What the desk actually does: load it here, then save it there."""
        import shutil, tempfile
        sample = Path(__file__).resolve().parents[1] / "files" / "history_sample.xlsx"
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "vol_history.xlsx"
            shutil.copy(sample, live)
            history.load_history(live)
            spare = Path(tmp) / "next.xlsx"
            shutil.copy(sample, spare)
            spare.replace(live)              # Excel's own save is a replace
            self.assertTrue(history.load_history(live).pairs)


class TestAutoReload(unittest.TestCase):
    """Watching the market feed, which is off unless it is asked for.

    Everything here drives ``auto_check`` directly: the watcher thread does
    nothing else, so there is no timing in the test.
    """

    def setUp(self):
        import shutil, tempfile
        from volkit.webapp import BookService
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        # The feed is the subject here, not the smiles: one pair is enough, and
        # a reload of fourteen fitted all fourteen again on every check.
        self.wb, self.feed = book_copy(d, pairs=("USDJPY",)), d / "feed.csv"
        shutil.copy(Path(__file__).resolve().parents[1] / "files" / "market_feed.csv", self.feed)
        self.service = BookService(str(self.wb), ASOF, feed_path=str(self.feed),
                                   auto_reload=1.0)
        self.assertIsNone(self.service.load_error)

    def tearDown(self):
        self.service.stop_watching()
        self.tmp.cleanup()

    def touch(self, path, ahead=5.0):
        import os, time
        when = time.time() + ahead
        os.utime(path, (when, when))

    def settle(self, path, ahead=5.0):
        """Change a file and give the watcher the two passes it waits for."""
        self.touch(path, ahead)
        first = self.service.auto_check()
        self.assertEqual(first, [], "a file is read once its write time has stopped moving")
        return self.service.auto_check()

    def test_nothing_happens_while_nothing_changes(self):
        self.assertEqual(self.service.auto_check(), [])
        self.assertEqual(self.service.auto_check(), [])
        self.assertEqual(self.service.auto_state()["seq"], 0)

    def test_a_rewritten_feed_is_re_read(self):
        text = self.feed.read_text(encoding="utf-8").replace("USDJPY,SPOT,150.25", "USDJPY,SPOT,151.25")
        self.assertNotEqual(text, self.feed.read_text(encoding="utf-8"))
        self.feed.write_text(text, encoding="utf-8")
        events = self.settle(self.feed)
        self.assertEqual([e["what"] for e in events], ["feed"])
        self.assertTrue(events[0]["ok"])
        self.assertAlmostEqual(self.service.book.feed.pairs["USDJPY"].spot, 151.25)
        self.assertFalse(self.service.feed_state()["stale"])

    def test_a_file_still_being_written_is_left_for_the_next_pass(self):
        """One pass sees the write time move; the pass after reads it."""
        self.touch(self.feed, 5.0)
        self.assertEqual(self.service.auto_check(), [])
        self.touch(self.feed, 9.0)                  # still being written
        self.assertEqual(self.service.auto_check(), [])
        self.assertEqual(len(self.service.auto_check()), 1)

    def test_only_the_feed_is_watched(self):
        """The workbook and the historical sheet are deliberately not.

        Re-reading the workbook discards every mark this session has made
        (nothing writes to the workbook), and a historical sheet is a record
        of what happened rather than a market.  Both stay on their buttons;
        the feed is a publication and is the only file worth chasing.
        """
        self.assertEqual([w["what"] for w in self.service.auto_state()["watching"]], ["feed"])
        pair = self.service.book.pairs[0]
        self.service.overwrite({"pair": pair, "kind": "atm", "tenor": "1m", "value": 9.5})
        self.touch(self.wb)
        self.assertEqual(self.service.auto_check(), [])
        self.assertEqual(self.service.auto_check(), [])
        # ... and the mark this session made is exactly where it was put.
        row = next(r for r in self.service.marks({"pair": pair, "cut": "NY"})["atm"]
                   if r["tenor"] == "1m")
        self.assertAlmostEqual(row["overwrite"], 0.095, places=12)
        self.assertEqual(self.service.auto_state()["seq"], 0)

    def test_a_watcher_that_is_off_is_reported_and_does_nothing(self):
        from volkit.webapp import BookService
        quiet = BookService(str(self.wb), ASOF, feed_path=str(self.feed))
        state = quiet.auto_state()
        self.assertFalse(state["enabled"])
        self.assertFalse(quiet.start_watching())
        self.assertEqual([w["what"] for w in state["watching"]], ["feed"])
        self.touch(self.feed)
        # Off means the loop never runs; a check driven by hand still works,
        # which is what the "check the feed now" button does.
        self.assertEqual(quiet.auto_check(settle=False)[0]["what"], "feed")

    def test_the_switch_turns_the_watcher_on_and_off(self):
        """The pricing screen's checkbox, which posts to the same method."""
        from volkit.webapp import BookService
        quiet = BookService(str(self.wb), ASOF, feed_path=str(self.feed))
        self.addCleanup(quiet.stop_watching)
        self.assertFalse(quiet.auto_state()["enabled"])
        self.assertTrue(quiet.auto_state()["available"])
        state = quiet.set_auto({"enabled": True, "interval": 3})
        self.assertTrue(state["enabled"])
        self.assertEqual(state["interval"], 3)
        self.assertIsNotNone(quiet._watcher)
        # Off again, and the thread really stops rather than the flag alone.
        state = quiet.set_auto({"enabled": False})
        self.assertFalse(state["enabled"])
        self.assertIsNone(quiet._watcher)
        # The interval it was given survives being switched off and on.
        self.assertEqual(quiet.set_auto({"enabled": True})["interval"], 3)
        with self.assertRaises(ValueError):
            quiet.set_auto({"interval": 0})

    def test_no_feed_file_means_there_is_nothing_to_auto_load(self):
        """A switch that can be turned on and then does nothing is worse than
        one that says why it is greyed out."""
        from volkit.webapp import BookService
        quiet = BookService(str(self.wb), ASOF)
        self.assertFalse(quiet.auto_state()["available"])
        self.assertEqual(quiet.auto_state()["watching"], [])

    def test_the_sequence_number_moves_only_when_something_happened(self):
        before = self.service.auto_state()["seq"]
        self.assertEqual(self.service.auto_check(), [])
        self.assertEqual(self.service.auto_state()["seq"], before)
        self.settle(self.feed)
        self.assertGreater(self.service.auto_state()["seq"], before)


class TestVegaWeights(unittest.TestCase):
    """The shape a move of one tenor is shared out by.

    The tab existed on the desk's workbook long before anything read it -- one
    unheaded column of numbers under ``USDCNH`` -- and a marker typed the
    result of it into the overwrite column tenor by tenor.  What is pinned
    here is the three things that makes it a mark rather than arithmetic: that
    a pair reads its own column and falls back to the default *cell by cell*,
    that the anchor moves exactly what was asked whatever its own weight is,
    and that a tenor the tab cannot weight keeps its place and says so instead
    of quietly not moving.
    """

    def _sheet(self, rows, sheet="Vega Weights"):
        import tempfile
        import openpyxl
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = openpyxl.Workbook()
        wb.active.title = sheet
        for row in rows:
            wb.active.append(row)
        path = d / "marks.xlsx"
        wb.save(path)
        return path

    def _weights(self):
        from volkit.vegaweights import load_vega_weights
        return load_vega_weights(self._sheet([
            ["# how far each tenor moves when the anchor moves one point"],
            ["tenor", "default", "USDJPY", "note"],
            ["1W", 1.60, 2.10, "front end"],
            ["1M", 1.00, 1.00, None],
            ["3M", 0.65, None, "USDJPY takes the default here"],
            ["1Y", 0.40, 0.55, None],
        ]))

    # -- the tab ----------------------------------------------------------
    def test_a_pair_column_wins_and_a_blank_cell_in_it_falls_back(self):
        """Per cell, not per column.

        A desk with a view on the front end of USDJPY and none on its back end
        should not have to retype the back end to say so, and a column that
        fell back as a whole would make an opinion about 1W an opinion about
        every tenor on the tab.
        """
        w = self._weights()
        self.assertEqual(w.pairs, ("USDJPY",))
        self.assertEqual(w.weight_for("USDJPY", "1W"), (2.10, "USDJPY"))
        self.assertEqual(w.weight_for("USDJPY", "3M"), (0.65, "default"))
        self.assertEqual(w.weight_for("EURUSD", "1W"), (1.60, "default"))
        # A tenor the tab has no row for is an absence, not a one.
        self.assertEqual(w.weight_for("USDJPY", "2Y"), (None, ""))
        self.assertEqual(w.for_pair("USDJPY"),
                         {"1W": 2.10, "1M": 1.00, "3M": 0.65, "1Y": 0.55})

    def test_a_column_that_is_not_a_pair_is_refused_by_name(self):
        """The columns are tenor, default, note and pairs.

        A heading nobody meant -- a stray ``USD``, a pasted date -- read as a
        pair column is a weight that silently applies to nothing, which is the
        shape of every bug this project was written to remove.
        """
        from volkit import configsheets
        from volkit.vegaweights import load_vega_weights
        path = self._sheet([["tenor", "default", "USD"], ["1M", 1.0, 1.0]])
        with self.assertRaises(configsheets.ConfigSheetError) as ctx:
            load_vega_weights(path)
        self.assertIn("'usd'", str(ctx.exception))

    def test_a_tenor_written_twice_is_refused(self):
        from volkit import configsheets
        from volkit.vegaweights import load_vega_weights
        path = self._sheet([["tenor", "default"], ["1M", 1.0], ["1m", 0.8]])
        with self.assertRaises(configsheets.ConfigSheetError) as ctx:
            load_vega_weights(path)
        self.assertIn("twice", str(ctx.exception))

    def test_a_negative_weight_is_allowed_and_a_word_is_not(self):
        """A measured beta can come out negative when the back end has been
        trading against the front.  Refusing it on the tab would make the
        realized table on the same screen suggest a number the tab cannot
        hold, so it is a mark like any other."""
        from volkit import configsheets
        from volkit.vegaweights import load_vega_weights
        w = load_vega_weights(self._sheet([["tenor", "default"], ["1M", 1.0], ["1Y", -0.2]]))
        self.assertEqual(w.weight_for("EURUSD", "1Y"), (-0.2, "default"))
        with self.assertRaises(configsheets.ConfigSheetError):
            load_vega_weights(self._sheet([["tenor", "default"], ["1M", "steep"]]))

    def test_an_absent_tab_is_absent_rather_than_empty(self):
        from volkit.vegaweights import load_vega_weights
        w = load_vega_weights(self._sheet([["pair", "lower", "upper"]], sheet="PEG_BANDS"))
        self.assertFalse(w.present)
        self.assertEqual(w.tenors, ())

    def test_the_tab_is_found_however_the_workbook_capitalises_it(self):
        """``Vega Weights`` is what the desk called it.  A workbook where
        somebody has since typed ``VEGA_WEIGHTS`` is the same tab."""
        from volkit.vegaweights import load_vega_weights
        for name in ("Vega Weights", "VEGA_WEIGHTS", "vega weights"):
            w = load_vega_weights(self._sheet([["tenor", "default"], ["1M", 1.0]],
                                              sheet=name))
            self.assertTrue(w.present, name)

    # -- the bump ---------------------------------------------------------
    def test_the_anchor_moves_what_was_asked_and_the_scale_cancels(self):
        """Only ratios matter.  A tab anchored at 1.00 and the same shape
        multiplied by 100 are one view, and a bump that divided by the wrong
        one of them would move the whole curve by a factor of a hundred."""
        from volkit.vegaweights import bump_levels, load_vega_weights
        levels = {"1W": 0.09, "1M": 0.08, "3M": 0.075, "1Y": 0.07}
        rows = bump_levels(self._weights(), "USDJPY", "1M", 0.01, levels)
        by = {r.tenor: r for r in rows}
        self.assertAlmostEqual(by["1M"].move, 0.01, places=15)
        self.assertAlmostEqual(by["1W"].move, 0.021, places=15)
        self.assertAlmostEqual(by["1Y"].move, 0.0055, places=15)
        scaled = load_vega_weights(self._sheet([
            ["tenor", "default", "USDJPY"],
            ["1W", 160.0, 210.0], ["1M", 100.0, 100.0],
            ["3M", 65.0, None], ["1Y", 40.0, 55.0]]))
        for a, b in zip(rows, bump_levels(scaled, "USDJPY", "1M", 0.01, levels)):
            self.assertAlmostEqual(a.after, b.after, places=15)

    def test_a_tenor_the_tab_cannot_weight_keeps_its_place_and_says_why(self):
        from volkit.vegaweights import bump_levels
        rows = bump_levels(self._weights(), "EURUSD", "1M", 0.01,
                           {"1M": 0.08, "2Y": 0.07})
        self.assertEqual([r.tenor for r in rows], ["1M", "2Y"])
        self.assertIsNone(rows[1].after)
        self.assertIn("no weight", rows[1].reason)

    def test_a_bump_through_zero_is_reported_rather_than_marked(self):
        from volkit.vegaweights import bump_levels
        rows = bump_levels(self._weights(), "EURUSD", "1M", -0.20,
                           {"1M": 0.08, "1Y": 0.07})
        self.assertTrue(all(r.after is None for r in rows))
        self.assertIn("vol points", rows[0].reason)

    def test_an_anchor_off_the_curve_or_off_the_tab_is_refused_whole(self):
        """Before a single row is computed: half a bump is not a curve."""
        from volkit.vegaweights import bump_levels, VegaWeights
        levels = {"1M": 0.08, "2Y": 0.07}
        with self.assertRaises(ValueError):
            bump_levels(self._weights(), "EURUSD", "6M", 0.01, levels)
        with self.assertRaises(ValueError) as ctx:
            bump_levels(self._weights(), "EURUSD", "2Y", 0.01, levels)
        self.assertIn("no weight", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            bump_levels(VegaWeights(), "EURUSD", "1M", 0.01, levels)
        self.assertIn("Vega Weights", str(ctx.exception))

    def test_an_anchor_that_does_not_move_cannot_measure_a_move(self):
        from volkit.vegaweights import bump_levels, load_vega_weights
        w = load_vega_weights(self._sheet([["tenor", "default"], ["1M", 0.0], ["1Y", 0.4]]))
        with self.assertRaises(ValueError) as ctx:
            bump_levels(w, "EURUSD", "1M", 0.01, {"1M": 0.08, "1Y": 0.07})
        self.assertIn("does not move", str(ctx.exception))

    # -- the book and the shipped workbook --------------------------------
    def test_the_shipped_workbook_weights_its_managed_pair(self):
        book = Book.from_excel(BOOK, ASOF)
        self.assertEqual([w for w in book.warnings if "vega" in w.lower()], [])
        self.assertEqual(book.vega_weights.weight_for("USDCNH", "1Y"), (1.0, "USDCNH"))
        self.assertEqual(book.vega_weights.weight_for("USDCNH", "1W")[0], 2.6)

    def test_a_workbook_whose_tab_has_no_header_says_so_and_prices_anyway(self):
        """The old layout: one unheaded column of numbers.  It is a warning
        and no weights -- a tab that is *there* and cannot be read is a
        different thing from one that was never written, and a desk that wrote
        one meant it to apply."""
        legacy = WORKBOOK.parent / "vol_marks_legacy_format.xlsx"
        book = Book.from_excel(legacy, ASOF).load_all(["EURUSD"])
        said = [w for w in book.warnings if "vega weights" in w.lower()]
        self.assertTrue(said and "tenor, default" in said[0], said)
        self.assertFalse(book.vega_weights.present)
        self.assertGreater(book["EURUSD"].atm.term_vol(0.25), 0)


class TestVegaWeightsThroughTheScreens(unittest.TestCase):
    """The route, the workbook card's open columns, and what a bump leaves.

    The bump makes nothing new: what it writes are the per-tenor ATM
    overwrites a marker could have typed one row at a time, so the session
    carries them, the overwrite count reports them and the clear button undoes
    them.  That is the property worth pinning -- a bump that stored a bump
    would be a mark the rest of the tool could not see.
    """

    def _service(self):
        import shutil
        import tempfile
        from volkit.webapp import BookService
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        # USDCNH is what the weights tab manages and EURUSD the pair it is
        # compared against; no other smile is read here.
        wb = book_copy(d, pairs=("USDCNH", "EURUSD"))
        return BookService(str(wb), ASOF), wb

    def test_a_bump_is_shown_before_it_is_written(self):
        svc, _ = self._service()
        shown = svc.atm_bump({"pair": "USDCNH", "anchor": "1M", "move": 1.0})
        self.assertEqual(shown["applied"], 0)
        self.assertEqual(svc.book["USDCNH"].atm.tenor_overwrites, {})
        row = {r["tenor"].upper(): r for r in shown["rows"]}
        self.assertAlmostEqual(row["1M"]["move"], 1.0, places=9)
        # 2.6 / 1.85 at the front, off the workbook's own USDCNH column.
        self.assertAlmostEqual(row["1W"]["move"], 2.6 / 1.85, places=9)
        self.assertEqual(row["1W"]["source"], "USDCNH")
        self.assertEqual(shown["column"], "USDCNH")

    def test_applying_leaves_ordinary_overwrites_and_nothing_else(self):
        svc, _ = self._service()
        out = svc.atm_bump({"pair": "USDCNH", "anchor": "1M", "move": 1.0, "apply": True})
        atm = svc.book["USDCNH"].atm
        self.assertEqual(out["applied"], len(atm.tenor_overwrites))
        marks = svc.marks({"pair": "USDCNH", "cut": "TK"})
        by = {r["tenor"].upper(): r for r in marks["atm"]}
        for r in out["rows"]:
            if r["after"] is None:          # 3W: the tab has no row for it
                self.assertIsNone(by[r["tenor"].upper()]["overwrite"])
            else:
                self.assertAlmostEqual(by[r["tenor"].upper()]["overwrite"] * 100,
                                       r["after"], places=9)
        # Cleared by the button that clears any other overwrite.
        svc.overwrite({"pair": "USDCNH", "kind": "clear_atm"})
        self.assertEqual(atm.tenor_overwrites, {})

    def test_every_level_is_read_before_any_of_them_is_written(self):
        """An overwrite changes what ``term_vol`` interpolates either side of
        it, so a bump applied row by row would move each tenor off a curve the
        previous row had already moved.  Showing and applying must agree."""
        svc, _ = self._service()
        shown = svc.atm_bump({"pair": "USDCNH", "anchor": "1M", "move": 0.75})
        done = svc.atm_bump({"pair": "USDCNH", "anchor": "1M", "move": 0.75, "apply": True})
        for a, b in zip(shown["rows"], done["rows"]):
            self.assertEqual(a["tenor"], b["tenor"])
            if a["after"] is None:
                self.assertIsNone(b["after"])
            else:
                self.assertAlmostEqual(a["after"], b["after"], places=12)

    def test_a_bump_survives_a_session_round_trip(self):
        from volkit import session
        svc, wb = self._service()
        svc.atm_bump({"pair": "USDCNH", "anchor": "1M", "move": 1.0, "apply": True})
        was = dict(svc.book["USDCNH"].atm.tenor_overwrites)
        path = wb.parent / "vol_session.json"
        session.write(session.capture(svc.book, ["USDCNH"]), path)
        fresh = Book.from_excel(wb, ASOF).load_all(["USDCNH"])
        session.apply_document(fresh, session.load(path), ["USDCNH"])
        back = fresh["USDCNH"].atm.tenor_overwrites
        self.assertEqual(sorted(back), sorted(was))
        # To the precision the file is written at, which is far finer than a
        # mark is made to; the point is that the bump left nothing the session
        # does not carry.
        for tenor, vol in was.items():
            self.assertAlmostEqual(back[tenor], vol, places=12)

    def test_a_move_that_was_not_typed_is_refused_rather_than_read_as_zero(self):
        svc, _ = self._service()
        for bad in ({}, {"move": ""}, {"move": "up a bit"}):
            with self.assertRaises(ValueError):
                svc.atm_bump({"pair": "USDCNH", "anchor": "1M", **bad})

    def test_the_weights_a_pair_reads_are_answerable_before_anything_is_bumped(self):
        """A tenor with no weight does not move, and finding that out from the
        result table is finding it out one press late."""
        svc, _ = self._service()
        out = svc.vega_weights({"pair": "USDCNH"})
        self.assertTrue(out["present"])
        self.assertEqual(out["column"], "USDCNH")
        blank = [r["tenor"] for r in out["rows"] if r["weight"] is None]
        self.assertEqual([t.upper() for t in blank], ["3W"])
        # A pair with no column of its own reads the default -- which this
        # workbook leaves empty, so it is weighted by nothing and says so.
        other = svc.vega_weights({"pair": "EURUSD"})
        self.assertEqual(other["column"], "default")
        self.assertTrue(all(r["weight"] is None for r in other["rows"]))

    def test_the_realized_route_says_what_is_missing_rather_than_returning_nothing(self):
        svc, _ = self._service()
        with self.assertRaises(ValueError) as ctx:
            svc.vega_realized({"pair": "USDCNH", "anchor": "1M", "lookback": 180})
        self.assertIn("no historical workbook", str(ctx.exception))
        svc.load_history({"path": str(HISTORY)})
        with self.assertRaises(ValueError) as ctx:
            svc.vega_realized({"pair": "USDCNH", "anchor": "1M", "lookback": 180})
        self.assertIn("no sheet for USDCNH", str(ctx.exception))
        with self.assertRaises(ValueError):
            svc.vega_realized({"pair": "USDCNH", "anchor": "1M", "lookback": "a while"})

    def test_the_workbook_card_offers_the_columns_the_tab_actually_has(self):
        """The pair columns are the desk's, not this build's.  A card that
        showed only the fixed ones would offer to write every one of them
        away on the next save."""
        svc, _ = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        self.assertTrue(tab["open"])
        self.assertEqual(tab["measure"], "vega")
        self.assertEqual(tab["columns"], ["tenor", "default", "USDCNH", "note"])
        self.assertEqual(tab["rows"][0]["tenor"], "1W")
        self.assertEqual(tab["rows"][0]["USDCNH"], 2.6)

    def test_a_pair_column_added_on_the_card_reaches_the_tab_and_comes_back(self):
        svc, _ = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        rows = [dict(r) for r in tab["rows"]]
        for r in rows:
            r["EURUSD"] = 0.5
        svc.config_save({"sheet": "Vega Weights", "rows": rows})
        again = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        self.assertEqual(again["columns"], ["tenor", "default", "USDCNH", "EURUSD", "note"])
        self.assertEqual(svc.book.vega_weights.weight_for("EURUSD", "1M"), (0.5, "EURUSD"))
        # And the column that was already there is still there.
        self.assertEqual(svc.book.vega_weights.weight_for("USDCNH", "1W"), (2.6, "USDCNH"))
        # The tab is the session's now, and says so; the workbook has not
        # been written and still says what it said.
        self.assertTrue(again["pending"])
        self.assertEqual(svc.config_tabs()["pending"], ["Vega Weights"])
        from volkit.book import Book
        self.assertIsNone(
            Book.from_excel(svc.path).vega_weights.weight_for("EURUSD", "1M")[0])

    def test_writing_a_tab_leaves_it_where_it_was_in_the_workbook(self):
        """The tab is replaced, not edited.  Recreated at the end of the
        workbook it is a tab that jumps on somebody every time a setting is
        saved, and the desk opens this file in Excel."""
        import openpyxl
        svc, wb = self._service()
        book = openpyxl.load_workbook(wb, read_only=True)
        order = list(book.sheetnames)
        book.close()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        svc.config_save({"sheet": "Vega Weights", "rows": tab["rows"]})
        path = svc.session_save({"path": str(Path(wb).with_name("s.json"))})["written"]
        svc.session_export({"path": path})
        book = openpyxl.load_workbook(wb, read_only=True)
        self.assertEqual(book.sheetnames, order)
        book.close()

    def test_a_column_that_is_not_a_pair_is_refused_before_it_is_written(self):
        """The screen validates the box; this is the same rule on the route
        behind it.  Written through, a mistyped heading is accepted here,
        refused by the tab's reader on the next load, and the tab is
        unreadable until somebody opens Excel and fixes it by hand."""
        svc, _ = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        rows = [dict(r) for r in tab["rows"]]
        rows[0]["USD"] = 0.5
        with self.assertRaises(ValueError) as ctx:
            svc.config_save({"sheet": "Vega Weights", "rows": rows})
        self.assertIn("six letters", str(ctx.exception))
        # And nothing was written: the tab still reads.
        self.assertEqual(svc.book.vega_weights.weight_for("USDCNH", "1W"), (2.6, "USDCNH"))

    def test_the_tabs_prose_survives_being_written_from_the_screen(self):
        """The '#' lines above the header are the reasoning a desk wrote down.
        The reader treats them as comments precisely so a save can keep
        them."""
        import openpyxl
        svc, wb = self._service()
        tab = {t["sheet"]: t for t in svc.config_tabs()["tabs"]}["Vega Weights"]
        svc.config_save({"sheet": "Vega Weights", "rows": tab["rows"]})
        path = svc.session_save({"path": str(Path(wb).with_name("s.json"))})["written"]
        svc.session_export({"path": path})
        book = openpyxl.load_workbook(wb, read_only=True)
        first = [r[0] for r in book["Vega Weights"].iter_rows(values_only=True)][:3]
        book.close()
        self.assertTrue(all(str(c).startswith("#") for c in first), first)


class TestRealizedVegaWeights(unittest.TestCase):
    """The same shape, measured off the historical book.

    A suggestion and nothing more: it fills the workbook card's boxes and is
    written by a person.  What is pinned is the identity the two extra columns
    exist for -- ``beta == corr * sd_ratio`` -- and that the anchor's own beta
    is exactly one, because a table that showed anything else for it has a bug
    in it rather than a market in it.
    """

    def _hist(self, series, dates=None):
        from volkit.history import PairHistory
        import numpy as np
        n = len(next(iter(series.values())))
        days = dates or [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]
        return PairHistory(pair="EURUSD", dates=days,
                           spot=np.full(n, 1.08),
                           atm={k: np.asarray(v, dtype=float) for k, v in series.items()})

    def test_beta_is_the_correlation_times_the_ratio_and_the_anchor_is_one(self):
        from volkit.vegaweights import realized_weights
        import numpy as np
        rng = np.random.default_rng(7)
        base = 0.08 + np.cumsum(rng.normal(0, 0.0004, 90))
        half = 0.08 + 0.5 * (base - 0.08) + np.cumsum(rng.normal(0, 0.0001, 90))
        out = realized_weights(self._hist({"1M": base, "3M": half}), "1M", 400)
        by = {r.tenor: r for r in out.rows}
        self.assertAlmostEqual(by["1M"].beta, 1.0, places=12)
        self.assertAlmostEqual(by["1M"].sd_ratio, 1.0, places=12)
        self.assertAlmostEqual(by["3M"].beta, by["3M"].corr * by["3M"].sd_ratio, places=12)
        self.assertAlmostEqual(by["3M"].beta, 0.5, delta=0.15)

    def test_a_tenor_that_moved_alone_has_a_big_ratio_and_a_small_beta(self):
        """Which is the whole reason both columns are shown.  A weighting
        taken off the standard-deviation ratio would mark a move that nothing
        said was coming."""
        from volkit.vegaweights import realized_weights
        import numpy as np
        rng = np.random.default_rng(11)
        base = 0.08 + np.cumsum(rng.normal(0, 0.0004, 120))
        alone = 0.09 + np.cumsum(rng.normal(0, 0.0004, 120))
        out = realized_weights(self._hist({"1M": base, "1Y": alone}), "1M", 400)
        row = {r.tenor: r for r in out.rows}["1Y"]
        self.assertLess(abs(row.beta), 0.4)
        self.assertGreater(row.sd_ratio, 0.6)
        self.assertTrue(any("less than half the time" in w for w in out.warnings))

    def test_a_tenor_the_sheet_does_not_quote_keeps_its_place(self):
        from volkit.vegaweights import realized_weights
        import numpy as np
        base = 0.08 + np.arange(60) * 1e-5
        out = realized_weights(self._hist({"1M": base}), "1M", 400,
                               tenors=["1M", "6M"])
        self.assertEqual([r.tenor for r in out.rows], ["1M", "6M"])
        self.assertIsNone(out.rows[1].beta)
        self.assertIn("quotes no at-the-money", out.rows[1].reason)

    def test_too_few_paired_observations_is_a_reason_not_a_number(self):
        from volkit.vegaweights import realized_weights
        import numpy as np
        base = 0.08 + np.arange(10) * 1e-4
        out = realized_weights(self._hist({"1M": base}), "1M", 400)
        self.assertIsNone(out.rows[0].beta)
        self.assertIn("at least 20", out.rows[0].reason)

    def test_the_shipped_history_measures_against_the_shipped_workbook(self):
        from volkit.history import load_history
        from volkit.vegaweights import realized_weights
        hist = load_history(HISTORY)
        out = realized_weights(hist["EURUSD"], "1M", 250)
        self.assertEqual(out.anchor, "1M")
        got = out.suggested()
        self.assertAlmostEqual(got["1M"], 1.0, places=12)
        self.assertTrue(out.first and out.last and out.first < out.last)


class TestCrossVegaSplit(unittest.TestCase):
    """Where a cross's at-the-money vega actually sits."""

    def rows(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["AUDJPY"])
        return book, analytics.triangle_table(book, "AUDJPY", cut="NY", with_noise=False,
                                              tenors=["3m"])

    def test_the_split_is_the_derivative_of_the_variance_triangle(self):
        book, rows = self.rows()
        r = rows[0]
        va, vb = r.leg_atm
        ca, cb = r.coefficients
        x = ca * cb * r.rho
        sigma = r.variance_triangle_atm
        self.assertAlmostEqual(r.leg_vega[0], (va + x * vb) / sigma)
        self.assertAlmostEqual(r.leg_vega[1], (vb + x * va) / sigma)
        self.assertAlmostEqual(r.rho_vega, ca * cb * va * vb / sigma)

    def test_a_bump_in_a_leg_moves_the_cross_by_the_split(self):
        """The number is a hedge ratio, so it is checked against a real bump."""
        book, rows = self.rows()
        r = rows[0]
        va, vb = r.leg_atm
        ca, cb = r.coefficients
        h = 1e-6

        def triangle(a, b):
            return math.sqrt(a * a + b * b + 2.0 * ca * cb * r.rho * a * b)

        self.assertAlmostEqual((triangle(va + h, vb) - triangle(va - h, vb)) / (2 * h),
                               r.leg_vega[0], places=6)
        self.assertAlmostEqual((triangle(va, vb + h) - triangle(va, vb - h)) / (2 * h),
                               r.leg_vega[1], places=6)

    def test_the_two_hedges_satisfy_euler_rather_than_adding_to_one(self):
        """The ratios are hedges, not shares.

        Reading them as a split of something into parts is the mistake: they
        do not add to one.  What is exact is Euler's identity -- the triangle
        is homogeneous of degree one in the two leg volatilities, so weighting
        each ratio by its own leg accounts for the whole of the cross's.
        """
        from volkit.analytics import _vega_split
        for va, vb, rho in ((0.10, 0.10, 0.30), (0.07, 0.13, -0.60), (0.09, 0.11, 0.85)):
            with self.subTest(rho=rho):
                sigma = math.sqrt(va * va + vb * vb + 2 * rho * va * vb)
                da, db, drho = _vega_split(va, vb, rho, 1, 1, sigma)
                self.assertAlmostEqual(va * da + vb * db, sigma)
                self.assertNotAlmostEqual(da + db, 1.0)
                # The correlation term is degree zero and is not in the identity.
                self.assertAlmostEqual(drho, va * vb / sigma)

    def test_the_split_matches_the_triangle_the_book_is_built_on(self):
        """Euler again, on the book's own marks rather than on made-up ones."""
        book, rows = self.rows()
        r = rows[0]
        va, vb = r.leg_atm
        self.assertAlmostEqual(va * r.leg_vega[0] + vb * r.leg_vega[1],
                               r.variance_triangle_atm)

    def test_a_zero_cross_volatility_has_no_hedge_ratio_rather_than_an_infinity(self):
        from volkit.analytics import _vega_split
        out = _vega_split(0.1, 0.1, -1.0, 1, 1, 0.0)
        self.assertTrue(all(v != v for v in out))

    def test_the_row_that_could_not_be_built_carries_no_split_either(self):
        """A failed row keeps its place; it must not carry a made-up ratio."""
        book = Book.from_excel(BOOK, ASOF).load_all(["AUDJPY"])
        rows = analytics.triangle_table(book, "AUDJPY", cut="NY", with_noise=False,
                                        tenors=["3m"])
        self.assertTrue(all(v == v for v in rows[0].leg_vega))


if __name__ == "__main__":
    unittest.main()
