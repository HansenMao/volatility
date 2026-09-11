"""The market-maker model, its API and the panel.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestMarketMakerApi(unittest.TestCase):
    """The endpoints, and the one piece of server state on the whole tool."""

    # The pair is named in the box, on a heading line, because the tab has no
    # pair selector any more (§11): the check and the quote read every pair the
    # box names and refuse a line that names none.
    RUN = ("EURUSD\n"
           "1M ATM 6.05/6.35 in 100mm vega\n"
           "3M ATM 6.25/6.55\n"
           "6M ATM 6.60/6.90\n"
           "1Y atm 7.00/7.30\n"
           "1M 25d rr -0.30/-0.10\n")

    #: The two pairs this class quotes.  A service reads the whole workbook and
    #: calibrates every pair in it, so pointing it at the two that are actually
    #: quoted is the difference between a slow class and a fast one.
    PAIRS = ("EURUSD", "USDJPY")

    def service(self, tmp):
        from volkit.webapp import BookService
        return BookService(str(book_for(*self.PAIRS)), ASOF,
                           bank_path=str(Path(tmp) / "bank.json"))

    def test_the_payload_carries_no_number_a_browser_cannot_parse(self):
        """Python's json writes NaN, which JSON.parse refuses."""
        import json as _json, tempfile
        from volkit.webapp import _finite
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.service(tmp).mm_check({"pair": "EURUSD", "text": self.RUN})
        text = _json.dumps(_finite(payload), default=str)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(_json.loads(text)["pair"], "EURUSD")

    def test_check_market_moves_nothing_and_names_what_is_off(self):
        """The button that replaced the fit, and the reason it replaced it.

        The fit read this paste and *moved the curve to it*; there were then two
        places on one screen where a mark could change and only one of them was
        written into the journal.  So this route reads the same paste and moves
        nothing: the book is untouched, `dirty` stays where it was, and what it
        hands back is where the marks sit -- with the gap in volatility points
        and in units of the market's own width, which is the number that says
        whether being outside matters.
        """
        import tempfile
        from volkit.session import capture_pair
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            before = capture_pair(service.book, "EURUSD")
            out = service.mm_check({"pair": "EURUSD", "text": self.RUN})
            self.assertEqual(capture_pair(service.book, "EURUSD"), before)
            self.assertFalse(service.dirty)
        # No fit's vocabulary anywhere on the answer.
        for gone in ("curve", "wings", "applied"):
            self.assertNotIn(gone, out)
        market = out["market"]
        self.assertEqual(market["n_quotes"], 5)
        self.assertEqual(market["checked"],
                         market["inside"] + market["through"])
        # This run is marked well below the pasted at-the-monies, so the ATM
        # lines are through their bid and the answer says so once, in a list a
        # desk can read without going through twenty rows.
        self.assertTrue(market["through"])
        self.assertTrue(all(a["severity"] in ("through", "edge")
                            for a in market["alerts"]))
        low = next(r for r in market["rows"] if r["tenor"] == "3M")
        self.assertEqual(low["position"], "below")
        self.assertEqual(low["severity"], "through")
        self.assertLess(low["gap"], 0.0)          # signed the way a desk reads it
        self.assertAlmostEqual(low["widths"],
                               low["gap"] / (low["market_ask"] - low["market_bid"]))
        # And it read the book, because it was handed no marks.
        self.assertFalse(out["marks"]["on_the_marks"])

    def test_a_choice_price_is_never_called_near_an_edge(self):
        """Warning about a market that quoted no width to be near the edge of
        is how a screen teaches a desk to stop reading its alerts."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self.service(tmp).mm_check(
                {"text": "EURUSD 1M ATM 5.831\n", "near_edge": "0.4"})
        row = out["market"]["rows"][0]
        self.assertEqual(row["market_bid"], row["market_ask"])
        self.assertIn(row["severity"], ("in line", "through"))
        self.assertIsNone(row["widths"])

    def test_the_hand_fit_is_the_only_market_maker_route_that_moves_a_mark(self):
        """The marks the check and the quote stand on come from one place.

        `mm_mark_fit` is the old fit panel, on the marking card.  What it hands
        back is checked and quoted off exactly as before -- that hand-off is the
        thing the split had to preserve -- and `apply` is the only way anything
        of it reaches the loaded book.
        """
        import tempfile
        from volkit.session import capture_pair
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            before = capture_pair(service.book, "EURUSD")
            fit = service.mm_mark_fit({"pair": "EURUSD", "text": self.RUN,
                                       "target_source": "quotes"})
            self.assertEqual(capture_pair(service.book, "EURUSD"), before)
            self.assertFalse(service.dirty)
            self.assertTrue(fit["marks"]["fitted"])
            # Checked against what it arrived at, the run it was fitted to is
            # inside its own market -- which is the whole point of the fit, and
            # is now measured by the *other* button.
            on = service.mm_check({"pair": "EURUSD", "text": self.RUN,
                                   "marks": fit["marks"]})
            self.assertTrue(on["marks"]["on_the_marks"])
            self.assertEqual(on["market"]["through"], 0)
            # And keeping them is the one route here that dirties the book.
            kept = service.mm_mark_fit({"pair": "EURUSD", "text": self.RUN,
                                        "target_source": "quotes", "apply": True})
            self.assertTrue(kept["applied"])
            self.assertTrue(service.dirty)
            self.assertNotEqual(capture_pair(service.book, "EURUSD"), before)

    def test_the_bank_is_the_only_state_the_server_keeps(self):
        """The panel is posted whole every time, like the listed screen; the
        bank is a file the desk owns, so it lives on the server."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            saved = service.mm_save_bank({
                "pair": "EURUSD",
                "rules": [{"kind": "spread", "value": "0.28", "instrument": "atm"},
                          {"kind": "note", "text": "wider into the ECB"}]})
            self.assertTrue(saved["ok"], saved.get("problems"))
            self.assertTrue(Path(saved["written"]).exists())
            # The bank is the quote route's alone: a width is a property of
            # what we show, and neither the check nor a fit shows anything.
            out = service.mm_quote({"request_text": "EURUSD 1M ATM\n"})
            atm = next(r for r in out["sheet"]["rows"] if r["instrument"] == "atm")
            self.assertAlmostEqual(atm["width"], 0.28)
            # A note is advice, kept apart from the reader's own notes so it
            # cannot get buried: it exists to be read.
            self.assertIn("wider into the ECB", atm["advice"])
            # And it survives a fresh service reading the same file.
            self.assertEqual(len(self.service(tmp).bank.for_pair("EURUSD").rules), 2)

    def test_the_marking_card_never_holds_the_book_while_it_reads_the_archive(self):
        """The archive is read on its own lock and let go before the book's is
        taken.  Held the other way round -- the book under the archive -- a
        folder scan or a download (minutes, and a language model behind them)
        held the book too, and the Fit button beside the card, which asks the
        archive nothing, sat there until they finished."""
        import tempfile, threading

        class Watched:
            """A lock that knows how deep it is held, and by whom."""

            def __init__(self):
                self._lock = threading.RLock()
                self.depth = 0

            def __enter__(self):
                self._lock.acquire()
                self.depth += 1
                return self

            def __exit__(self, *exc):
                self.depth -= 1
                self._lock.release()

        class WatchedArchive(Watched):
            def __init__(self, book_lock):
                super().__init__()
                self.book_lock = book_lock
                self.book_held = []

            def __enter__(self):
                super().__enter__()
                self.book_held.append(self.book_lock.depth)
                return self

        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            service._lock = Watched()
            service._archive_lock = WatchedArchive(service._lock)
            out = service.mm_mark({"pair": "EURUSD", "text": self.RUN,
                                   "target_source": "quotes"})
            self.assertIn("proposal", out)
            # It did read the archive, and the book was not held when it did.
            self.assertTrue(service._archive_lock.book_held)
            self.assertEqual(set(service._archive_lock.book_held), {0})
            self.assertEqual(service._lock.depth, 0)

    def test_the_tape_leans_the_mid_only_when_a_weight_says_so(self):
        """The one inference in the package -- a print's side, decided against
        our own mark -- may not move a price until a desk has said it may.
        With a weight it leans the level and nothing else: a risk reversal is
        a statement about shape, and what the tape paid for says nothing about
        where the skew belongs."""
        import tempfile
        from datetime import timedelta
        from volkit import archive as arch, black
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "archive.jsonl")
            a = arch.Archive(path=path)
            when = ASOF.now - timedelta(days=1)
            expiry = (when + timedelta(days=30)).date().isoformat()
            rows = [arch.Observation(
                kind="forward", pair="EURUSD", at=when.isoformat(timespec="seconds"),
                instrument="atm", tenor=expiry, expiry_date=expiry, rate=1.10,
                action="NEWT", external_id="f1", via="sdr", source="sdr")]
            # Six prints well above any mark this book carries: the tape paid.
            px = float(black.price(1.10, 1.10, 0.25, 30 / 365.2425, True))
            for i in range(6):
                rows.append(arch.Observation(
                    kind="trade", pair="EURUSD", at=when.isoformat(timespec="seconds"),
                    instrument="outright", tenor=expiry, expiry_date=expiry,
                    strike=1.10, is_call=True, premium=px * 100_000_000.0,
                    premium_ccy="USD", notional=100_000_000.0, notional_ccy="EUR",
                    action="NEWT", event="TRAD", external_id=str(i), via="sdr",
                    source="sdr"))
            a.extend(rows)
            a.flush()
            from volkit.webapp import BookService
            service = BookService(str(book_for(*self.PAIRS)), ASOF, bank_path=str(Path(tmp) / "bank.json"),
                                  archive_path=path)
            ask = {"request_text": "EURUSD 1M ATM\nEURUSD 1M 25d RR\n",
                   "fallback_tier": "default"}
            # The tape, the axe and the fair value are read per pair, and the
            # sheet keeps each pair's answer under `by_pair`: a sheet may hold
            # three currencies and there is no one tape across them.
            quiet = service.mm_quote(dict(ask))
            off = quiet["by_pair"]["EURUSD"]
            self.assertTrue(off["flow"]["buckets"], off["flow"]["reason"])
            self.assertIn("set a flow weight", off["flow"]["reason"])
            self.assertEqual([r["skew_flow"] for r in quiet["sheet"]["rows"]], [0.0, 0.0])

            leaning = service.mm_quote(dict(ask, flow_weight="0.5", flow_scale="100000"))
            on = leaning["by_pair"]["EURUSD"]
            rows_on = {r["instrument"]: r for r in leaning["sheet"]["rows"]}
            # Paid means marked up, by half a weight of the half width.
            self.assertGreater(rows_on["atm"]["skew_flow"], 0.0)
            self.assertAlmostEqual(rows_on["atm"]["skew_flow"], 0.5 * 0.5 * 0.4, places=6)
            self.assertGreater(rows_on["atm"]["our_bid"], quiet["sheet"]["rows"][0]["our_bid"])
            # And a risk reversal is left alone.
            self.assertEqual(rows_on["rr"]["skew_flow"], 0.0)
            # The tape's age weight and window are the archive card's, not a
            # second pair of boxes: one evidence clock on the sheet.
            self.assertEqual(on["flow"]["half_life"], on["archive"]["half_life"])
            self.assertEqual(on["flow"]["lookback_days"], on["archive"]["lookback_days"])
            self.assertNotIn("flow_half_life", _inspect.getsource(marketmaker.QuotePanel))
            # The same tape, read out in words by the record agent off the
            # same surface: it says the market has been paying, names the
            # mark each print was judged against, and writes nothing.
            told = service.mm_ask({"pair": "EURUSD", "text": "who has been paying in the 1M",
                                   "transcript": []})
            self.assertFalse(told.get("refused"), told)
            lines = [f["text"] for f in told["facts"]]
            self.assertTrue(any("paid" in x and "vega" in x for x in lines), lines)
            self.assertTrue(any("has been paying" in x for x in lines), lines)
            self.assertTrue(any("against a mark of" in x for x in lines), lines)
            self.assertEqual(len(arch.Archive.load(path).records), len(rows))

    def test_a_bad_rule_set_is_rejected_without_touching_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            service.mm_save_bank({"pair": "EURUSD",
                                  "rules": [{"kind": "spread", "value": "0.28"}]})
            bad = service.mm_save_bank({"pair": "EURUSD",
                                        "rules": [{"kind": "spread", "value": "-3"}]})
            self.assertFalse(bad["ok"])
            # And the refusal says why *this set* was refused.  `bank_state`
            # carries a `problems` of its own -- what the file said when it
            # was read -- and it used to be spread over these, so a bad rule
            # came back as a note about the file.
            self.assertTrue(any("positive width" in p for p in bad["problems"]),
                            bad["problems"])
            self.assertEqual(len(self.service(tmp).bank.for_pair("EURUSD").rules), 1)

            # Every pair at once, which is how the card saves: one bad rule on
            # one pair saves nothing, and the pair is named.
            both = service.mm_save_bank({"banks": {
                "EURUSD": {"rules": [{"kind": "spread", "value": "0.30",
                                      "instrument": "atm"}]},
                "USDJPY": {"rules": [{"kind": "spread", "value": "0.50",
                                      "instrument": "atm"}]}}})
            self.assertTrue(both["ok"], both["problems"])
            self.assertEqual(len(both["by_pair"]["USDJPY"]["rules"]), 1)
            self.assertEqual(len(self.service(tmp).bank.for_pair("USDJPY").rules), 1)
            half = service.mm_save_bank({"banks": {
                "EURUSD": {"rules": [{"kind": "spread", "value": "0.40",
                                      "instrument": "atm"}]},
                "USDJPY": {"rules": [{"kind": "spread", "value": "-1"}]}}})
            self.assertFalse(half["ok"])
            self.assertTrue(any(p.startswith("USDJPY:") for p in half["problems"]),
                            half["problems"])
            # Nothing was written: EURUSD is still the value from the good save.
            kept = self.service(tmp).bank.for_pair("EURUSD").rules
            self.assertEqual([r.value for r in kept], [0.30])

    def test_learning_proposes_and_does_not_save_and_does_not_file(self):
        """A paste that happens to hold one wide quote must not be able to
        rewrite the desk's ladder without somebody looking at it -- and
        learning from it must not quietly file it either: one pipeline,
        the archive's, with the paste counted unfiled."""
        import tempfile
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            service = BookService(str(book_for(*self.PAIRS)), ASOF, bank_path=str(Path(tmp) / "bank.json"),
                                  archive_path=str(Path(tmp) / "arc.jsonl"))
            got = service.mm_learn({"pair": "EURUSD", "text": self.RUN,
                                    "archive_min_effective": 0.5})
            self.assertTrue(got["rules"], got["notes"])
            self.assertTrue(all(r["kind"] == "spread" for r in got["rules"]))
            self.assertEqual(got["from_paste"], 5)
            self.assertEqual(service.bank.for_pair("EURUSD").rules, [])
            self.assertEqual(len(service.archive.records), 0)
            self.assertFalse((Path(tmp) / "arc.jsonl").exists())
            # Filed, the same run teaches once and not twice.
            service.mm_agent_file({"pair": "EURUSD", "text": self.RUN})
            again = service.mm_learn({"pair": "EURUSD", "text": self.RUN,
                                      "archive_min_effective": 0.5})
            self.assertEqual(again["from_paste"], 0)
            self.assertEqual(again["counted"], got["counted"])
            self.assertEqual([r["value"] for r in again["rules"]],
                             [r["value"] for r in got["rules"]])

    def test_the_state_endpoint_tells_the_browser_what_it_may_choose_from(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = self.service(tmp).state()["marketmaker"]
        for key in ("target_sources", "backbone_knobs", "cross_knobs", "smile_params",
                    "fly_conventions", "vol_units", "rule_kinds", "rule_instruments",
                    "size_bases", "bank"):
            self.assertIn(key, state)
        self.assertIn("overwrites", state["target_sources"])
        self.assertIn("rho25", state["smile_params"])

    def test_a_quote_is_recorded_under_a_client_and_answered_and_the_next_quote_knows(self):
        """The loop the whole agent exists to close, through the two routes.

        Quote for a client, record it, answer it as lifted four times over,
        and the next quote for that client is leaned up -- off the same
        archive the routes wrote, under the archive's own lock, with the book
        untouched and `dirty` where it was.  A price recorded from a shell
        goes through the same two functions.
        """
        import tempfile
        from volkit.session import capture_pair
        from volkit.webapp import BookService
        with tempfile.TemporaryDirectory() as tmp:
            service = BookService(str(book_for(*self.PAIRS)), ASOF, bank_path=str(Path(tmp) / "bank.json"),
                                  archive_path=str(Path(tmp) / "arc.jsonl"))
            before = capture_pair(service.book, "EURUSD")

            def ask(size):
                return {"request_text": f"EURUSD 1M ATM in {size}mm",
                        "fallback_tier": "default", "client": "Client Q",
                        "client_weight": 0.5, "client_min": 4}
            first = service.mm_quote(ask(100))
            row = first["sheet"]["rows"][0]
            self.assertEqual(first["client"]["name"], "Client Q")
            self.assertIn("no price shown to Client Q", first["client"]["reason"])
            self.assertIsNotNone(row["our_bid"])
            # Four prices shown, four lifted.  Different sizes, because the
            # clock here is pinned and the id is a hash of the content: the
            # same price shown to the same client in the same instant *is*
            # one price, and the route says so rather than counting it twice.
            for size in (100, 50, 75, 25):
                sheet = service.mm_quote(ask(size))
                shown = service.mm_record({"sheet": sheet, "client": "Client Q"})
                self.assertEqual(len(shown["recorded"]), 1, shown)
                done = service.mm_outcome({"ref": shown["recorded"][0]["id"],
                                           "result": "traded_ask", "sign": 1})
                self.assertEqual(done["result"], "traded_ask")
            twice = service.mm_record({"sheet": sheet, "client": "Client Q"})
            self.assertEqual(twice["recorded"], [])
            self.assertIn("already in the archive", twice["refused"][0])
            self.assertEqual(capture_pair(service.book, "EURUSD"), before)
            self.assertFalse(service.dirty)

            again = service.mm_quote(ask(100))
            leaned = again["sheet"]["rows"][0]
            self.assertTrue(again["client"]["applied"])
            self.assertTrue(leaned["client_enough"])
            self.assertAlmostEqual(leaned["client_side"], 1.0)
            self.assertAlmostEqual(leaned["our_mid"], row["our_mid"] + 0.10, places=6)
            self.assertIn("Client Q", again["client"]["known"])
            # The archive is read per pair, and what it holds for this one is
            # under that pair: a sheet of three currencies has three archives'
            # worth of records and no one number across them.
            held_here = again["by_pair"]["EURUSD"]["archive"]
            self.assertEqual(held_here["shown"], 4)
            self.assertEqual(held_here["outcome"], 4)
            # The file on disk is what was written.
            from volkit.archive import Archive
            held = Archive.load(str(Path(tmp) / "arc.jsonl"))
            self.assertEqual(len(held.query(pair="EURUSD", kinds="shown")), 4)
            self.assertEqual(len(held.query(pair="EURUSD", kinds="outcome")), 4)
            # Nothing to record, or nothing that was shown: refused with a reason.
            with self.assertRaises(Exception):
                service.mm_record({"client": "Client Q"})
            with self.assertRaises(Exception):
                service.mm_outcome({"ref": "nosuch", "result": "passed"})


class TestMarketMakerModel(unittest.TestCase):
    """The three stages: the curve, the wings, and the quote."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD", "USDJPY"])

    # -- the fast path is measured, never assumed --------------------------
    def test_the_wing_reproduces_the_interpolated_smile_where_the_check_says_it_does(self):
        """The whole fine tune rests on this.

        The interpolators are fitted *through* five anchor points taken off the
        two SABR wings, so at those deltas the wing and the interpolation are
        normally the same number -- which is what lets the fit skip a 19ms SVI
        solve per expiry per evaluation.  Where ``anchor_gap`` says they agree,
        this holds them to it.
        """
        for pair in ("EURUSD", "USDJPY"):
            surface = self.book[pair]
            for tenor in ("1w", "1m", "3m", "1y"):
                t = tenor_to_years(tenor)
                dt = self.book.clock.datetime_from_years(t)
                gap = marketmaker.anchor_gap(surface, dt, t, "SVI", "NY")
                with self.subTest(pair=pair, tenor=tenor):
                    self.assertLessEqual(gap, marketmaker.ANCHOR_TOLERANCE)
                sl = surface.slice_at(dt, "SVI", "NY")
                ev = marketmaker.Evaluator(surface, "SVI", "NY",
                                           fast_at=frozenset({round(t, 10)}))
                for delta, is_call in ((0.25, True), (0.25, False),
                                       (0.10, True), (0.10, False)):
                    with self.subTest(pair=pair, tenor=tenor, delta=delta, call=is_call):
                        fast = ev.delta_vol(dt, t, delta, is_call)
                        slow = sl.strike_from_delta(delta if is_call else -delta, is_call)[1]
                        self.assertAlmostEqual(fast, slow, delta=marketmaker.ANCHOR_TOLERANCE)
                self.assertEqual(ev.slices_built, 0, "the fast path should build no slice")

    def test_a_slice_that_cannot_pass_through_its_anchors_is_refused_the_shortcut(self):
        """SVI here is arbitrage constrained, so five parameters through five
        points is not a free interpolation.  On USDCNY -- a managed pair whose
        marked wings are the least well behaved in the book -- the constraint
        binds and the fitted smile lands more than a tenth of a volatility
        point off its own anchors.  Assuming the shortcut there would have the
        fit marking to a smile the rest of the tool does not price on.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDCNY"])
        surface = book["USDCNY"]
        t = tenor_to_years("1m")
        dt = book.clock.datetime_from_years(t)
        self.assertGreater(marketmaker.anchor_gap(surface, dt, t, "SVI", "NY"), 1e-4)
        q = quotes.MarketQuote(instrument="rr", expiry="1M", bid=0.001, ask=0.002, delta=0.25)
        expiries = marketmaker.resolve_expiries(book.clock, [q])
        fast_at, notes = marketmaker.verified_fast_expiries(
            surface, [q], expiries, "SVI", "NY")
        self.assertEqual(fast_at, frozenset())
        self.assertTrue(any("does not pass through its own anchor points" in n for n in notes))
        # And with no shortcut, the evaluator agrees with the surface itself.
        ev = marketmaker.Evaluator(surface, "SVI", "NY", fast_at=fast_at)
        self.assertAlmostEqual(ev.value(q, expiries, {}),
                               surface.risk_reversal(dt, 0.25, "SVI", "NY"), places=12)

    def test_the_fast_path_is_declined_where_it_would_not_be_exact(self):
        """Vanna-volga at 25 delta reproduces its own three anchors and says
        nothing about the 10 delta ones, so the shortcut is refused there."""
        self.assertEqual(marketmaker.anchor_wing("SVI", 0.25), 25)
        self.assertEqual(marketmaker.anchor_wing("SVI", 0.10), 10)
        self.assertEqual(marketmaker.anchor_wing("VV25", 0.25), 25)
        self.assertIsNone(marketmaker.anchor_wing("VV25", 0.10))
        self.assertIsNone(marketmaker.anchor_wing("VV10", 0.25))
        self.assertIsNone(marketmaker.anchor_wing("SVI", 0.15))
        self.assertEqual(marketmaker.anchor_wing("SABR25", 0.15), 25)

    def test_the_evaluator_is_exact_unless_it_is_told_otherwise(self):
        """An unverified fast path is how a fit ends up marking to a smile the
        rest of the tool does not use, so it is off by default."""
        surface = self.book["EURUSD"]
        t = tenor_to_years("3m")
        dt = self.book.clock.datetime_from_years(t)
        ev = marketmaker.Evaluator(surface, "SVI", "NY")
        q = quotes.MarketQuote(instrument="rr", expiry="3M", bid=0.0, ask=0.001, delta=0.25)
        got = ev.value(q, {"3M": (dt, t)}, {})
        self.assertGreater(ev.slices_built, 0)
        self.assertAlmostEqual(got, surface.risk_reversal(dt, 0.25, "SVI", "NY"), places=12)

    # -- the curve ---------------------------------------------------------
    def test_segmented_accumulation_matches_integrating_from_zero(self):
        """The fit accumulates the term structure segment by segment rather
        than integrating from zero once per tenor.  Daily variances summing to
        the term variance is what makes that identical, so it is pinned."""
        atm = self.book["EURUSD"].atm
        ts = [tenor_to_years(x) for x in ("1w", "1m", "3m", "6m", "1y")]
        fast = marketmaker._curve_vols(atm, ts)
        slow = [atm.curve_vol(t) for t in ts]
        for a, b in zip(fast, slow):
            self.assertAlmostEqual(a, b, places=14)

    def test_the_curve_fit_recovers_a_curve_it_was_moved_away_from(self):
        import copy
        atm = copy.deepcopy(self.book["EURUSD"].atm)
        tenors = ("1w", "2w", "1m", "2m", "3m", "6m", "9m", "1y")
        want = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t),
                                        atm.curve_vol(tenor_to_years(t)))
                for t in tenors]
        original = dict(vars(atm.params))
        atm.set_params(initial_vol=0.03, long_term_vol=0.15, mean_reversion=40.0)
        fit = marketmaker.fit_atm_curve(atm, want)
        for target, got in zip(want, fit.achieved_after):
            self.assertAlmostEqual(got, target.vol, places=6)
        self.assertAlmostEqual(fit.after["long_term_vol"], original["long_term_vol"], places=4)

    def test_the_curve_fit_leaves_the_curve_it_was_given_alone(self):
        """It runs on a copy, so a failed or unwanted fit cannot leave a
        half-marked curve behind."""
        atm = self.book["EURUSD"].atm
        before = dict(vars(atm.params))
        targets = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t), 0.09)
                   for t in ("1w", "1m", "3m", "6m", "1y")]
        marketmaker.fit_atm_curve(atm, targets)
        self.assertEqual(dict(vars(atm.params)), before)

    def test_a_pinned_parameter_is_not_moved_by_the_sweep(self):
        """The starting-point sweep may only vary free parameters.  Sweeping a
        pinned one and keeping whatever the best node held would change a
        parameter the caller deliberately froze."""
        import copy
        atm = copy.deepcopy(self.book["EURUSD"].atm)
        targets = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t), v)
                   for t, v in (("1m", 0.062), ("3m", 0.064), ("6m", 0.0675), ("1y", 0.0715))]
        fit = marketmaker.fit_atm_curve(atm, targets, free=("initial_vol", "long_term_vol"))
        self.assertEqual(fit.after["short_decay"], atm.params.short_decay)
        self.assertEqual(fit.after["mean_reversion"], atm.params.mean_reversion)

    def test_more_free_parameters_than_targets_is_refused(self):
        import copy
        atm = copy.deepcopy(self.book["EURUSD"].atm)
        targets = [marketmaker.CurveTarget("1M", tenor_to_years("1m"), 0.08)]
        with self.assertRaises(ValueError) as ctx:
            marketmaker.fit_atm_curve(atm, targets)
        self.assertIn("cannot determine", str(ctx.exception))

    def test_the_mean_reversion_is_fitted_inside_the_marked_range(self):
        """The range is a marking judgement, so the fit stays in it and the
        sweep nodes are taken from it.  A node the polish may not reach can
        still win the sweep on cost and is then clipped into the bound, which
        is a different curve from the one that was measured."""
        import copy
        lo, hi = marketmaker.MEAN_REVERSION_RANGE
        self.assertEqual(marketmaker._BOUNDS["mean_reversion"], (lo, hi))
        for node in marketmaker.reversion_nodes():
            self.assertGreaterEqual(node, lo)
            self.assertLessEqual(node, hi)
        atm = copy.deepcopy(self.book["EURUSD"].atm)
        # A target curve that flattens far faster than the range allows: the
        # unconstrained fit would run the reversion well past the top.
        want = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t), v)
                for t, v in (("1w", 0.055), ("2w", 0.062), ("1m", 0.068),
                             ("3m", 0.0715), ("6m", 0.072), ("1y", 0.0721))]
        fit = marketmaker.fit_atm_curve(atm, want)
        self.assertLessEqual(fit.after["mean_reversion"], hi)
        self.assertGreaterEqual(fit.after["mean_reversion"], lo)
        # Held back, it says so, and names the constant that would let it go.
        rest = [w for w in fit.warnings if "mean_reversion came to rest" in w]
        self.assertTrue(rest)
        self.assertIn("marking judgement", rest[0])
        self.assertIn("Widen the range on the fit panel", rest[0])

    def test_a_parameter_on_its_bound_that_met_its_targets_is_not_warned_about(self):
        """AUDUSD is marked at exactly the top of the range, so an ungated
        check warned on every refit of the curve the desk already had.  A
        parameter resting on a bound limits nothing while the targets are
        still met, and a warning that fires when nothing is wrong is one
        nobody reads."""
        import copy
        book = Book.from_excel(BOOK, ASOF).load_all(["AUDUSD"])
        atm = copy.deepcopy(book["AUDUSD"].atm)
        self.assertEqual(atm.params.mean_reversion, marketmaker.MEAN_REVERSION_RANGE[1])
        want = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t),
                                        atm.curve_vol(tenor_to_years(t)))
                for t in ("1m", "2m", "3m", "6m", "1y")]
        fit = marketmaker.fit_atm_curve(atm, want)
        self.assertLessEqual(fit.rmse, marketmaker._BOUND_BINDING_RMSE)
        self.assertAlmostEqual(fit.after["mean_reversion"],
                               marketmaker.MEAN_REVERSION_RANGE[1], places=6)
        self.assertEqual([w for w in fit.warnings if "came to rest" in w], [])

    def test_the_range_is_a_marking_judgement_a_panel_may_override(self):
        """It is the one bound a caller may move, because it is a judgement
        about what a desk marks rather than a property of the model -- and the
        sweep nodes move with it, so the nodes and the polish can never be
        taken from two different ranges."""
        import copy
        atm = copy.deepcopy(self.book["EURUSD"].atm)
        want = [marketmaker.CurveTarget(t.upper(), tenor_to_years(t), v)
                for t, v in (("1w", 0.055), ("2w", 0.062), ("1m", 0.068),
                             ("3m", 0.0715), ("6m", 0.072), ("1y", 0.0721))]
        house = marketmaker.fit_atm_curve(atm, want)
        wide = marketmaker.fit_atm_curve(atm, want, reversion_range=(1.0, 40.0))
        self.assertAlmostEqual(house.after["mean_reversion"],
                               marketmaker.MEAN_REVERSION_RANGE[1], places=6)
        self.assertGreater(wide.after["mean_reversion"],
                           marketmaker.MEAN_REVERSION_RANGE[1])
        # Given room, the same targets are reached better -- which is the whole
        # reason the range is a judgement and not a fact.
        self.assertLess(wide.rmse, house.rmse)
        self.assertEqual(marketmaker.reversion_nodes((1.0, 5.0))[0], 1.0)
        self.assertEqual(marketmaker.reversion_nodes((1.0, 5.0))[-1], 5.0)

    def test_a_half_typed_mean_reversion_range_is_refused(self):
        """Two blanks are the house range -- the same reading as an empty
        market box handing the field back to the feed.  One blank is a range
        somebody meant to type and did not finish, and reading it half way
        would fit in a range nobody chose.

        Read here through the two panels that carry the boxes -- the hand fit
        and the agent, both on the marking card -- because that is where they
        now are, and one reader (`_reversion_from_request`) behind both so a
        range legal on one cannot be illegal on the other."""
        from volkit import marking
        base = {"pair": "EURUSD"}
        for reader in (marking.fit_panel_from_request, marking.panel_from_request):
            with self.subTest(reader.__name__):
                self.assertIsNone(reader(base).reversion_range)
                self.assertIsNone(reader(
                    {**base, "reversion_lo": "", "reversion_hi": ""}).reversion_range)
                self.assertEqual(reader(
                    {**base, "reversion_lo": "2", "reversion_hi": "9"}).reversion_range,
                    (2.0, 9.0))
                for bad, why in ((("2", ""), "both"), (("", "9"), "both"),
                                 (("0", "9"), "above zero"),
                                 (("9", "2"), "above its floor")):
                    with self.assertRaises(ValueError) as ctx:
                        reader({**base, "reversion_lo": bad[0], "reversion_hi": bad[1]})
                    self.assertIn(why, str(ctx.exception))

    def test_a_cross_fits_its_correlation_not_a_level_it_does_not_own(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["AUDUSD", "USDJPY", "AUDJPY"])
        knobs = marketmaker._Knobs(book["AUDJPY"].atm)
        self.assertTrue(knobs.is_cross)
        self.assertIn("corr_initial", knobs.available)
        self.assertNotIn("initial_vol", knobs.available)
        with self.assertRaises(ValueError) as ctx:
            marketmaker.fit_atm_curve(
                book["AUDJPY"].atm,
                [marketmaker.CurveTarget("1M", tenor_to_years("1m"), 0.09)],
                free=("initial_vol",))
        self.assertIn("cross", str(ctx.exception))

    # -- the hinge ---------------------------------------------------------
    def test_the_hinge_is_flat_inside_the_market_and_signed_outside_it(self):
        self.assertEqual(marketmaker._hinge(0.082, 0.080, 0.086), 0.0)
        self.assertAlmostEqual(marketmaker._hinge(0.078, 0.080, 0.086), -0.002)
        self.assertAlmostEqual(marketmaker._hinge(0.090, 0.080, 0.086), 0.004)

    def test_the_wing_tune_pulls_a_quote_it_can_reach_inside_the_market(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        t = tenor_to_years("3m")
        dt = book.clock.datetime_from_years(t)
        model = surface.risk_reversal(dt, 0.25, "SVI", "NY")
        target = model - 0.004                      # 0.4 vol points away
        q = quotes.MarketQuote(instrument="rr", expiry="3M", bid=target - 0.0005,
                               ask=target + 0.0005, delta=0.25)
        expiries = marketmaker.resolve_expiries(book.clock, [q])
        # One quote determines one parameter.  Freeing the pair as well would be
        # refused, which is a separate test.
        res = marketmaker.tune_smile_shifts(surface, [q], expiries, {}, method="SVI", cut="NY",
                                            free=("rho25",))
        self.assertEqual(res.inside_before, 0)
        self.assertEqual(res.inside_after, 1)
        self.assertLess(res.worst_after, res.worst_before)

    def test_more_free_parameters_than_wing_quotes_is_refused(self):
        """The same rule the curve fit applies to its targets.  An
        under-determined hinge has a flat manifold of answers and the optimiser
        wanders along it burning its whole budget on the tie-breakers."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        q = quotes.MarketQuote(instrument="rr", expiry="3M", bid=-0.002, ask=-0.001, delta=0.25)
        expiries = marketmaker.resolve_expiries(book.clock, [q])
        with self.assertRaises(ValueError) as ctx:
            marketmaker.tune_smile_shifts(book["EURUSD"], [q], expiries, {},
                                          method="SVI", cut="NY")
        self.assertIn("cannot determine", str(ctx.exception))

    def test_a_parameter_no_quote_reads_off_is_left_where_it_is(self):
        """A 25-delta quote reads the 25-delta anchor, which is built from
        rho25 and slog25 alone.  Freeing the ten-delta pair would not inform
        them; it would only make the objective flat in two more directions."""
        informed, _ = marketmaker.informative_params(
            [quotes.MarketQuote(instrument="rr", expiry="3M", bid=-0.002, ask=-0.001,
                                delta=0.25)], "SVI")
        self.assertEqual(informed, {"rho25", "slog25"})
        both, _ = marketmaker.informative_params(
            [quotes.MarketQuote(instrument="outright", expiry="3M", bid=0.06, ask=0.065,
                                strike=1.1, is_call=True)], "SVI")
        self.assertEqual(both, set(PARAM_NAMES))

    def test_the_wing_tune_reports_what_a_curve_wide_shift_cannot_reach(self):
        """Two tenors asking for opposite skews cannot both be met by one
        shift.  Saying so beats bending the surface to whichever quote the
        optimiser happened to weight most."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        made = []
        for tenor, offset in (("1M", -0.006), ("1Y", +0.006)):
            t = tenor_to_years(tenor)
            dt = book.clock.datetime_from_years(t)
            base = surface.risk_reversal(dt, 0.25, "SVI", "NY") + offset
            made.append(quotes.MarketQuote(instrument="rr", expiry=tenor, bid=base - 0.0002,
                                           ask=base + 0.0002, delta=0.25))
        expiries = marketmaker.resolve_expiries(book.clock, made)
        res = marketmaker.tune_smile_shifts(surface, made, expiries, {}, method="SVI", cut="NY")
        self.assertLess(res.inside_after, 2)
        self.assertTrue(any("still outside their market" in w for w in res.warnings))

    # -- shading the mid ----------------------------------------------------
    def test_richness_and_a_long_position_both_shade_the_mid_down(self):
        """Both are reasons to want to sell, and you attract a seller's trade
        by shading the price down.  A sign error here quotes the wrong way
        round on every row."""
        q = quotes.MarketQuote(instrument="atm", expiry="1M", bid=0.080, ask=0.086)
        rich = marketmaker.skew_for(q, 0.08, half_width=0.003, richness=0.004, axe=None,
                                    fair_weight=0.25, axe_weight=0.5, cap_ratio=1.0,
                                    bank_shift=0.0)
        self.assertLess(rich.fair, 0.0)
        long_vega = marketmaker.skew_for(q, 0.08, half_width=0.003, richness=None, axe=1.0,
                                         fair_weight=0.25, axe_weight=0.5, cap_ratio=1.0,
                                         bank_shift=0.0)
        self.assertAlmostEqual(long_vega.axe, -0.5 * 0.003)
        short_vega = marketmaker.skew_for(q, 0.08, half_width=0.003, richness=None, axe=-1.0,
                                          fair_weight=0.25, axe_weight=0.5, cap_ratio=1.0,
                                          bank_shift=0.0)
        self.assertGreater(short_vega.axe, 0.0)

    def test_the_shading_is_capped_at_a_fraction_of_the_width(self):
        """An axe may lean the price inside the market; it may not walk it out
        of the market on its own, which stops being a quote."""
        q = quotes.MarketQuote(instrument="atm", expiry="1M", bid=0.080, ask=0.086)
        got = marketmaker.skew_for(q, 0.08, half_width=0.003, richness=0.20, axe=1.0,
                                   fair_weight=0.25, axe_weight=0.5, cap_ratio=1.0,
                                   bank_shift=0.0)
        self.assertTrue(got.capped)
        self.assertAlmostEqual(abs(got.total), 0.003)

    def test_neither_lean_is_applied_to_a_risk_reversal(self):
        """A break-even against realized volatility and a vega position are
        both statements about the level.  Neither says where the skew belongs,
        so the row says so instead of inventing a lean."""
        q = quotes.MarketQuote(instrument="rr", expiry="1M", bid=-0.003, ask=-0.001, delta=0.25)
        got = marketmaker.skew_for(q, 0.08, half_width=0.001, richness=0.004, axe=1.0,
                                   fair_weight=0.25, axe_weight=0.5, cap_ratio=1.0,
                                   bank_shift=0.0005)
        self.assertEqual(got.fair, 0.0)
        self.assertEqual(got.axe, 0.0)
        self.assertAlmostEqual(got.total, 0.0005)
        self.assertIn("not a level", got.reason)


class TestMarketMakerPanel(unittest.TestCase):
    """The screen as a whole: what each of its stages reports, and what they
    leave behind.

    Three panels and three routes, so three sets of tests here.  The fit is
    ``marking.FitPanel``: it was this module's own ``Panel`` until every mark
    that moves became the marking card's, and it is tested from here anyway
    because the hand-off it makes to the check and the quote is a market-maker
    screen invariant.  Most of these switch the wing fine tune off; it is
    exercised properly in ``TestMarketMakerModel`` and in the tests here that
    need it, and running a full one in every case would spend a minute of the
    suite re-proving it.
    """

    TEXT = ("1M ATM 6.05/6.35 in 100mm vega\n"
            "3M ATM 6.25/6.55\n"
            "6M ATM 6.60/6.90\n"
            "1Y atm 7.00/7.30\n"
            "1M 25d rr -0.30/-0.10 eur call over\n"
            "3M 25d fly 0.12/0.20\n")
    ASKED = ("1M ATM in 100mm\n"
             "1M 25d rr\n"
             "3M 25d fly\n")

    @classmethod
    def setUpClass(cls):
        # Shared by everything that only reports: both panels put the book
        # back, which is itself two of the tests below.
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])

    def panel(self, **kw):
        """The hand fit, which lives on the marking card."""
        from volkit import marking
        payload = {"pair": "EURUSD", "text": self.TEXT, "target_source": "quotes",
                   "tune_wings": False}
        payload.update(kw)
        return marking.fit_panel_from_request(payload)

    def check(self, **kw):
        payload = {"pair": "EURUSD", "text": self.TEXT}
        payload.update(kw)
        return marketmaker.check_panel_from_request(payload)

    def quote(self, **kw):
        payload = {"pair": "EURUSD", "request_text": self.ASKED, "fallback_tier": "flat"}
        payload.update(kw)
        return marketmaker.quote_panel_from_request(payload)

    @staticmethod
    def spreads(**tiers):
        """A KACE_SPREADS table, as the bottom rung of the width ladder reads it."""
        from volkit import kace
        table = kace.SpreadTable(path="test!KACE_SPREADS")
        table.tiers = {"flat": {"1W": 0.30, "1M": 0.30, "3M": 0.30, "1Y": 0.30}}
        table.tiers.update(tiers)
        return table

    def bank(self):
        bank = KnowledgeBank()
        bank.set_pair("EURUSD", [Rule("spread", 0.28, "atm"), Rule("spread", 0.20, "rr"),
                                 Rule("spread", 0.12, "fly")], ASOF.now)
        return bank

    # -- the fit ----------------------------------------------------------
    def test_reporting_puts_the_book_back_exactly(self):
        """The default is to report.  A screen that quietly re-marked the book
        every time somebody typed in it would be unusable.  This runs the wing
        tune as well, because the shifts have to come back too."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = list(surface.atm.tenor_table())
        shifts = dict(surface.param_shifts)
        out = self.panel(tune_wings=True).run(book)
        self.assertFalse(out["applied"])
        self.assertIsNotNone(out["wings"])
        self.assertNotEqual(out["wings"]["after"], out["wings"]["before"])
        self.assertEqual(list(surface.atm.tenor_table()), before)
        self.assertEqual(dict(surface.param_shifts), shifts)

    def test_keeping_the_marks_writes_them_and_says_it_did(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = surface.atm.term_vol(tenor_to_years("1m"))
        out = self.panel(apply=True).run(book)
        self.assertTrue(out["applied"])
        self.assertAlmostEqual(surface.atm.term_vol(surface.tenor_years("1m")), 0.062, places=5)
        self.assertNotAlmostEqual(surface.atm.term_vol(tenor_to_years("1m")), before)
        self.assertTrue(any("in memory only" in w for w in out["warnings"]))

    def test_the_fit_puts_a_price_on_nothing_and_reads_no_market_table(self):
        """The whole point of the split, twice over.  A fit that also quoted the
        run it was fitted to made a price in every instrument a broker happened
        to show, which is not what anybody asked for.  And a fit that also
        reported where the surface sat against that run was answering Check
        Market's question with a button that moves marks, which is why there
        were two ways to re-mark one curve."""
        out = self.panel().run(self.book)
        self.assertNotIn("sheet", out)
        self.assertNotIn("market", out)
        self.assertIsNotNone(out["curve"])
        self.assertIsNotNone(out["marks"])

    def test_a_section_that_cannot_run_empties_only_itself(self):
        """No pinned tenor means no target curve.  The panel still answers,
        the same way the analysis screen keeps its sections apart."""
        out = self.panel(target_source="overwrites").run(self.book)
        self.assertIsNone(out["curve"])
        self.assertIn("no tenor is pinned", out["unavailable"]["curve"])
        self.assertEqual(out["marks"]["what"], "nothing")

    # -- the check --------------------------------------------------------
    def test_the_check_reports_where_the_marks_sit_against_theirs(self):
        out = self.check().run(self.book)
        for row in out["market"]["rows"]:
            self.assertIn(row["position"], ("inside", "below", "above"))
            self.assertIn(row["severity"], marketmaker.SEVERITIES)
            if row["position"] == "inside":
                self.assertEqual(row["gap"], 0.0)
                self.assertIn(row["severity"], ("in line", "edge"))
            else:
                self.assertNotEqual(row["gap"], 0.0)
                self.assertEqual(row["severity"], "through")
        self.assertEqual(out["market"]["n_quotes"], 6)

    def test_the_check_moves_nothing_and_quotes_nothing(self):
        """It replaced the fit and inherited none of its powers."""
        before = marketmaker.capture_marks(self.book["EURUSD"])
        out = self.check().run(self.book)
        self.assertEqual(marketmaker.capture_marks(self.book["EURUSD"]), before)
        for gone in ("curve", "wings", "applied", "sheet"):
            self.assertNotIn(gone, out)
        for row in out["market"]["rows"]:
            self.assertNotIn("our_bid", row)
            self.assertNotIn("width", row)

    def test_the_tolerance_decides_what_counts_as_near_an_edge(self):
        """Zero is the honest setting for a desk that does not want amber, and
        it must not silently mean the default."""
        loose = self.check(near_edge="0.45").run(self.book)["market"]
        off = self.check(near_edge="0").run(self.book)["market"]
        self.assertEqual(off["edge"], 0)
        self.assertGreaterEqual(loose["edge"], off["edge"])
        self.assertEqual(loose["through"], off["through"])
        with self.assertRaises(ValueError) as got:
            self.check(near_edge="0.5")
        self.assertIn("fraction of the quoted width", str(got.exception))

    def test_a_paste_the_reader_cannot_use_is_listed_not_silently_shortened(self):
        out = self.check(text=self.TEXT + "3M 25d rr 0.4/0.6 jpy call over\n").run(self.book)
        self.assertEqual(out["market"]["n_quotes"], 6)
        self.assertEqual(len(out["market"]["skipped"]), 1)
        self.assertIn("not a leg of EURUSD", out["market"]["skipped"][0]["why"])

    # -- the hand-off -----------------------------------------------------
    def test_the_handoff_reproduces_the_fit_exactly(self):
        """The two halves of the split have to meet at the same numbers.

        Quoting off the marks a fit handed back must give what quoting off a
        book the same fit was *applied* to gives.  Anything less and the
        screen's price would depend on whether somebody ticked "keep the
        marks", which is a decision about the book and not about the price.
        """
        applied_book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        fit = self.panel(apply=True, tune_wings=True).run(applied_book)
        on_book = self.quote().run(applied_book, bank=self.bank())

        reported_book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        reported = self.panel(tune_wings=True).run(reported_book)
        handed = self.quote(marks=reported["marks"]).run(reported_book, bank=self.bank())

        self.assertEqual([r["our_bid"] for r in handed["sheet"]["rows"]],
                         [r["our_bid"] for r in on_book["sheet"]["rows"]])
        self.assertEqual([r["our_ask"] for r in handed["sheet"]["rows"]],
                         [r["our_ask"] for r in on_book["sheet"]["rows"]])
        self.assertEqual(fit["marks"]["knobs"], reported["marks"]["knobs"])

    def test_keeping_a_fit_keeps_the_marks_it_handed_back(self):
        """The book and the panel must hold one number, not two spellings of it.

        A knob leaves the fit in volatility points and comes back divided by a
        hundred, and ``x * 100 / 100`` differs from ``x`` in the last place for
        about an eighth of all values.  Keeping the raw fitted numbers on the
        surface therefore left the book a bit away from the marks the quote
        panel was posting, and the price depended on whether "keep the marks"
        had been ticked -- a nanovol apart, which is nothing to a market and
        everything to a screen that has to reproduce itself.
        """
        applied = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        fit = self.panel(apply=True, tune_wings=True).run(applied)
        self.assertEqual(marketmaker.capture_marks(applied["EURUSD"]),
                         {"knobs": fit["marks"]["knobs"], "shifts": fit["marks"]["shifts"]})

    def test_a_quote_standing_on_a_fit_puts_the_book_back(self):
        """The marks go on for the length of one call and come off again.  A
        surface left half-marked by a price nobody kept, priced off all
        morning, is the worst outcome available to this tool."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        before = marketmaker.capture_marks(book["EURUSD"])
        fit = self.panel(tune_wings=True).run(book)
        self.assertNotEqual(fit["marks"]["knobs"], before["knobs"])
        out = self.quote(marks=fit["marks"]).run(book, bank=self.bank())
        self.assertEqual(marketmaker.capture_marks(book["EURUSD"]), before)
        self.assertEqual([w for w in out["warnings"] if "put back" in w], [])

    def test_the_check_says_which_marks_it_read(self):
        """A market checked against this morning's proposal and one checked
        against last night's marks must never read the same."""
        fit = self.panel().run(self.book)
        on = self.check(marks=fit["marks"]).run(self.book)
        self.assertTrue(on["marks"]["on_the_marks"])
        self.assertIn("handed", on["marks"]["note"])
        plain = self.check().run(self.book)
        self.assertFalse(plain["marks"]["on_the_marks"])
        self.assertIn("as they stand", plain["marks"]["note"])
        # And a different answer, which is the point of saying which.
        self.assertNotEqual([r["model"] for r in on["market"]["rows"]],
                            [r["model"] for r in plain["market"]["rows"]])

    def test_a_quote_says_which_marks_it_stood_on(self):
        """A price made on this morning's fit and one made on last night's
        marks must never read the same."""
        fit = self.panel().run(self.book)
        handed = self.quote(marks=fit["marks"]).run(self.book, bank=self.bank())
        self.assertTrue(handed["marks"]["on_the_marks"])
        self.assertIn("handed", handed["marks"]["note"])
        plain = self.quote().run(self.book, bank=self.bank())
        self.assertFalse(plain["marks"]["on_the_marks"])
        self.assertIn("as they stand", plain["marks"]["note"])
        # And they are different prices, which is the point of saying which.
        self.assertNotEqual([r["our_bid"] for r in handed["sheet"]["rows"]],
                            [r["our_bid"] for r in plain["sheet"]["rows"]])

    def test_marks_fitted_on_another_pair_are_refused(self):
        """The browser holds the fit and the pair selector apart, and the two
        can be moved apart.  Quoting EURUSD off a USDJPY fit is a wrong answer
        that reads perfectly well."""
        fit = self.panel().run(self.book)
        marks = dict(fit["marks"], pair="USDJPY")
        with self.assertRaises(ValueError) as got:
            marketmaker.quote_panel_from_request(
                {"pair": "EURUSD", "request_text": self.ASKED, "marks": marks})
        self.assertIn("fitted on USDJPY", str(got.exception))

    def test_a_knob_the_curve_does_not_have_is_refused_rather_than_skipped(self):
        marks = marketmaker.capture_marks(self.book["EURUSD"])
        marks["knobs"]["corr_initial"] = 0.5
        with self.assertRaises(ValueError) as got:
            marketmaker.apply_marks(self.book["EURUSD"], marks)
        self.assertIn("corr_initial", str(got.exception))

    # -- the quote --------------------------------------------------------
    def test_the_width_a_rule_states_is_the_width_that_comes_out(self):
        """The bank is written in volatility points and the model in decimals.
        Reading a 0.28 rule as a decimal produced a 28 vol point market."""
        out = self.quote().run(self.book, bank=self.bank())
        atm = next(r for r in out["sheet"]["rows"] if r["instrument"] == "atm")
        self.assertAlmostEqual(atm["width"], 0.28)
        self.assertAlmostEqual(atm["our_ask"] - atm["our_bid"], 0.28, places=9)
        self.assertIn("ATM", atm["width_source"])

    def test_a_request_with_no_rule_and_no_fallback_gets_no_price(self):
        out = self.quote(fallback_tier="").run(self.book, bank=KnowledgeBank(),
                                               spreads=self.spreads())
        for row in out["sheet"]["rows"]:
            self.assertIsNone(row["our_bid"])
            self.assertEqual(row["verdict"], "no width")
            self.assertTrue(any("no width rule" in w for w in row["warnings"]))
        self.assertEqual(out["sheet"]["fallback"]["tier"], "")
        self.assertFalse(out["sheet"]["fallback"]["error"])

    def test_the_fallback_is_a_tier_read_at_each_rows_own_maturity(self):
        """The bottom rung is a ladder, not one width for every tenor."""
        table = self.spreads(steep={"1W": 0.10, "1M": 0.20, "3M": 0.60, "1Y": 1.00})
        out = self.quote(request_text="1M ATM\n3M ATM\n",
                         fallback_tier="steep").run(self.book, bank=KnowledgeBank(),
                                                    spreads=table)
        rows = {r["tenor"]: r for r in out["sheet"]["rows"]}
        self.assertAlmostEqual(rows["1M"]["width"], 0.20)
        self.assertAlmostEqual(rows["3M"]["width"], 0.60)
        for r in rows.values():
            self.assertEqual(r["width_rung"], "fallback")
            self.assertIn("steep", r["width_source"])
            self.assertAlmostEqual(r["our_ask"] - r["our_bid"], r["width"], places=9)
        block = out["sheet"]["fallback"]
        self.assertEqual(block["tier"], "steep")
        self.assertEqual(block["multiplier"], 1.0)
        self.assertFalse(block["interpolate"])

    def test_the_fallback_tier_is_multiplied_and_may_be_read_across(self):
        table = self.spreads(steep={"1W": 0.10, "1M": 0.20, "3M": 0.60, "1Y": 1.00})
        asked = "2M ATM\n"
        stepped = self.quote(request_text=asked, fallback_tier="steep").run(
            self.book, bank=KnowledgeBank(), spreads=table)
        across = self.quote(request_text=asked, fallback_tier="steep",
                            fallback_interpolate="1").run(
            self.book, bank=KnowledgeBank(), spreads=table)
        wide = self.quote(request_text=asked, fallback_tier="steep",
                          fallback_multiplier="1.5").run(
            self.book, bank=KnowledgeBank(), spreads=table)
        # Stepped, 2M takes 1M's width; read across it sits between 1M and 3M.
        self.assertAlmostEqual(stepped["sheet"]["rows"][0]["width"], 0.20)
        self.assertGreater(across["sheet"]["rows"][0]["width"], 0.20)
        self.assertLess(across["sheet"]["rows"][0]["width"], 0.60)
        self.assertAlmostEqual(wide["sheet"]["rows"][0]["width"], 0.30)
        self.assertIn("1.5", wide["sheet"]["rows"][0]["width_source"])
        self.assertTrue(across["sheet"]["fallback"]["interpolate"])
        # And the mid is where it was: a multiplier widens, it does not move.
        self.assertAlmostEqual((wide["sheet"]["rows"][0]["our_bid"]
                                + wide["sheet"]["rows"][0]["our_ask"]) / 2.0,
                               (stepped["sheet"]["rows"][0]["our_bid"]
                                + stepped["sheet"]["rows"][0]["our_ask"]) / 2.0, places=9)

    def test_a_fallback_tier_that_is_not_there_is_one_message_not_a_crash(self):
        """A quote run still prices what the bank can answer."""
        out = self.quote(fallback_tier="fat").run(self.book, bank=self.bank(),
                                                  spreads=self.spreads())
        self.assertIn("fat", out["sheet"]["fallback"]["error"])
        for row in out["sheet"]["rows"]:
            self.assertIsNotNone(row["our_bid"], row["tenor"])
            self.assertEqual(row["width_rung"], "bank")
        # And with no table loaded at all it says that instead.
        none = self.quote().run(self.book, bank=self.bank())
        self.assertIn("SPREADS", none["sheet"]["fallback"]["error"])

    def test_an_absolute_strike_without_a_feed_is_reported_not_priced_at_one(self):
        """Without a forward there is no moneyness, and pricing it at a forward
        of 1 would be a silent, badly wrong answer."""
        out = self.quote(request_text="6M 1.1000 call\n").run(self.book, bank=self.bank())
        row = out["sheet"]["rows"][0]
        self.assertEqual(row["verdict"], "not priced")
        self.assertIsNone(row["model"])
        self.assertTrue(any("forward feed" in w for w in row["warnings"]))

    def test_a_quote_needs_no_market_at_all(self):
        """The reason the request box exists.  A request does not arrive with a
        broker run attached to it, and the price is a property of the marks and
        the bank rather than of what somebody happened to show."""
        out = self.quote(text="").run(self.book, bank=self.bank())
        self.assertEqual(out["sheet"]["n_quotes"], 3)
        self.assertEqual(out["sheet"]["priced"], 3)
        self.assertEqual(out["sheet"]["matched"], 0)
        for row in out["sheet"]["rows"]:
            self.assertIsNone(row["market_mid"])
            self.assertEqual(row["verdict"], "quoted")

    def test_a_request_the_market_also_quoted_carries_their_market(self):
        """So "inside their market" survives the split.  The match is on the
        instrument, which is what makes two lines the same quote -- not on the
        text, which is written differently in the two boxes."""
        out = self.quote(text=self.TEXT).run(self.book, bank=self.bank())
        rows = {r["describe"]: r for r in out["sheet"]["rows"]}
        atm = rows["1M ATM"]
        self.assertAlmostEqual(atm["market_bid"], 6.05)
        self.assertAlmostEqual(atm["market_ask"], 6.35)
        self.assertIn(atm["position"], ("inside", "below", "above"))
        self.assertNotEqual(atm["verdict"], "quoted")
        self.assertEqual(out["sheet"]["matched"], 3)

    def test_a_risk_reversal_is_quoted_in_the_convention_it_was_asked_in(self):
        """Asked as 'JPY call over', answered as 'JPY call over'.  Quoting a
        skew back in the opposite sign is the §5 class of error, so the flip is
        applied once, at the row, and every number on the row turns with it."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        bank = KnowledgeBank()
        bank.set_pair("USDJPY", [Rule("spread", 0.20, "rr")], ASOF.now)
        ours = marketmaker.quote_panel_from_request(
            {"pair": "USDJPY", "request_text": "3M 25d rr\n"}).run(book, bank=bank)
        theirs = marketmaker.quote_panel_from_request(
            {"pair": "USDJPY", "request_text": "3M 25d rr jpy call over\n"}).run(book, bank=bank)
        a, b = ours["sheet"]["rows"][0], theirs["sheet"]["rows"][0]
        self.assertEqual(a["sign"], 1.0)
        self.assertEqual(b["sign"], -1.0)
        self.assertAlmostEqual(b["model"], -a["model"], places=12)
        # A bid is still the low side of what we show, in whichever convention.
        self.assertLess(b["our_bid"], b["our_ask"])
        self.assertAlmostEqual(b["our_bid"], -a["our_ask"], places=12)
        self.assertIn("JPY call over", b["describe"])

    def test_the_panel_and_the_command_line_share_one_entry_point(self):
        """A panel set up in the browser and the same panel run from a shell
        must produce the same numbers, which is only guaranteed if there is one
        function -- and now three stages, so three of them, across two
        commands: `volkit mm` is the two that read the curve and `volkit mark`
        is the one that moves it."""
        import inspect
        from volkit import cli
        source = inspect.getsource(cli.cmd_mm)
        self.assertIn("check_panel_from_request", source)
        self.assertIn("quote_panel_from_request", source)
        # And nothing in `mm` fits, because nothing in `mm` may move a mark.
        for gone in ("fit_panel_from_request", "fit_atm_curve", "tune_smile_shifts"):
            self.assertNotIn(gone, source, f"`volkit mm` still reaches {gone}")
        self.assertIn("fit_panel_from_request", inspect.getsource(cli.cmd_mark))


class TestSeveralPairsOnOneScreen(unittest.TestCase):
    """The market-maker tab has no pair selector, so a box names its pairs.

    The pair moved onto the marking agent card -- a fit is of one curve --
    and everything else on the tab reads the pair off the line it is on, or
    off the heading above it.  That makes one paste and one request box a
    morning's whole book rather than one currency at a time, and it makes a
    line that names *no* pair a refusal: with nothing on the screen saying
    which pair is meant, pricing it against whichever one a selector happened
    to show is exactly the silent default this tool exists to remove.
    """

    RUN = ("EURUSD 1M ATM 6.05/6.35\n"
           "USDJPY\n"
           "1M ATM 9.00/9.40\n"
           "3M ATM 9.20/9.60\n")

    def sheet(self, text=None, **kw):
        from volkit import marketmaker as mm
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD", "USDJPY"])
        payload = {"text": self.RUN if text is None else text}
        payload.update(kw)
        return mm.check_sheet_from_request(payload).run(book), book

    def test_every_pair_the_box_names_is_checked_against_its_own_curve(self):
        out, book = self.sheet()
        self.assertEqual(out["pairs"], ["EURUSD", "USDJPY"])
        # Every row says which pair it is, and they come back in the order
        # they were written rather than pair by pair: a run is read down the
        # page.
        self.assertEqual([(r["pair"], r["line"]) for r in out["market"]["rows"]],
                         [("EURUSD", 1), ("USDJPY", 3), ("USDJPY", 4)])
        self.assertEqual(out["market"]["n_quotes"], 3)
        # And each was read at its own pair's interpolation, off the book,
        # because the tab sends none.
        self.assertEqual(out["methods"],
                         {p: book[p].method for p in ("EURUSD", "USDJPY")})
        # Nothing was "passed over": on a sheet that prices both, the other
        # pair's lines are the other pair's.
        self.assertFalse([n for n in out["market"]["notes"] if "passed over" in n])

    def test_a_line_that_names_no_pair_is_refused_and_named(self):
        out, _ = self.sheet("1M ATM 6.05/6.35\n" + self.RUN)
        self.assertEqual([r["pair"] for r in out["market"]["rows"]],
                         ["EURUSD", "USDJPY", "USDJPY"])
        refused = out["market"]["skipped"]
        self.assertEqual([x["line"] for x in refused], [1])
        self.assertIn("names no pair", refused[0]["why"])
        self.assertEqual(refused[0]["text"], "1M ATM 6.05/6.35")
        self.assertTrue(any("name no pair" in w for w in out["warnings"]), out["warnings"])
        # It is reported once, not once per pair the sheet holds.
        self.assertEqual(len(refused), 1)

    def test_a_box_that_names_nothing_is_an_empty_sheet_and_not_an_error(self):
        out, _ = self.sheet("1M ATM 6.05/6.35\n")
        self.assertEqual(out["pairs"], [])
        self.assertEqual(out["market"]["rows"], [])
        self.assertIn("nothing names a pair", out["empty"])

    def test_one_pairs_refusal_does_not_take_the_others_down(self):
        """A pair the book does not build is that pair's line, not the sheet's.

        A single panel raises, and with one pair on the screen that was the
        whole answer.  A sheet answers for every pair it can and says what
        happened to the rest -- otherwise one mistyped pair in a broker run
        blanks a screen that could have checked the other four.
        """
        out, _ = self.sheet(self.RUN + "GBPNOK 1M ATM 7.0/7.4\n")
        self.assertEqual([r["pair"] for r in out["market"]["rows"]],
                         ["EURUSD", "USDJPY", "USDJPY"])
        self.assertIn("GBPNOK", out["by_pair"])
        self.assertIn("not built in this book", out["by_pair"]["GBPNOK"]["error"])
        self.assertTrue(any("GBPNOK" in w for w in out["warnings"]))

    def test_the_marking_cards_marks_go_to_its_own_pair_and_nowhere_else(self):
        """The card holds one pair's marks; the sheet holds several pairs.

        Laying a EURUSD fit over USDJPY's rows would be a wrong answer that
        reads perfectly well, and refusing the whole sheet because one pair on
        it was not fitted would make the marking card unusable beside it.  So
        the marks reach the pair they were made on and every other pair is
        read off the book, and each pair's line says which it was.
        """
        from volkit.webapp import BookService
        svc = BookService(str(BOOK), ASOF)
        fit = svc.mm_mark_fit({"pair": "EURUSD", "text": self.RUN,
                               "target_source": "current",
                               "free": ["initial_vol"], "fit_curve": True})
        out = svc.mm_check({"text": self.RUN, "marks": fit["marks"]})
        self.assertTrue(out["by_pair"]["EURUSD"]["marks"]["on_the_marks"])
        self.assertFalse(out["by_pair"]["USDJPY"]["marks"]["on_the_marks"])
        self.assertIn("EURUSD", out["marks"]["note"])
        self.assertIn("USDJPY", out["marks"]["note"])
        self.assertTrue(out["marks"]["on_the_marks"])

    def test_the_quote_prices_each_pair_off_its_own_panel(self):
        """A sheet is a loop over the one pricing engine, not a second one.

        Every row a sheet produces has to be the row that pair's own panel
        would have produced alone, or there are two pricing engines again --
        which is the thing §17 exists to prevent.
        """
        from volkit import marketmaker as mm
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD", "USDJPY"])
        ask = {"request_text": "EURUSD 1M ATM\nUSDJPY 1M ATM\n",
               "fallback_tier": "default"}
        table = kace.SpreadTable.load(BOOK)
        sheet = mm.quote_sheet_from_request(ask).run(book, spreads=table)
        self.assertEqual([r["pair"] for r in sheet["sheet"]["rows"]], ["EURUSD", "USDJPY"])
        for pair in ("EURUSD", "USDJPY"):
            alone = mm.quote_panel_from_request(
                {"pair": pair, "request_text": f"{pair} 1M ATM\n",
                 "fallback_tier": "default"}).run(book, spreads=table)
            one = alone["sheet"]["rows"][0]
            both = next(r for r in sheet["sheet"]["rows"] if r["pair"] == pair)
            for field in ("model", "our_bid", "our_ask", "width", "width_rung"):
                self.assertEqual(both[field], one[field], f"{pair} {field}")

    def test_the_cut_and_the_interpolation_come_from_the_marking_tab(self):
        """Two boxes for one decision is one of them going stale.

        There is no cut or interpolation on this tab: the cut is the Vol
        marking tab's, and the interpolation is that tab's for the pair it is
        showing and each other pair's own default.  A method for a pair
        nobody asked about is not an error; a method that is not a method is.
        """
        from volkit import marketmaker as mm
        out, book = self.sheet(cut="TK", methods={"USDJPY": "VV25"})
        self.assertEqual(out["cut"], "TK")
        self.assertEqual(out["methods"]["USDJPY"], "VV25")
        self.assertEqual(out["methods"]["EURUSD"], book["EURUSD"].method)
        with self.assertRaises(ValueError):
            mm.check_sheet_from_request({"text": self.RUN, "methods": {"EURUSD": "nope"}})
        with self.assertRaises(ValueError):
            mm.check_sheet_from_request({"text": self.RUN, "methods": "SVI"})

    def test_the_sheet_readers_take_everything_the_panel_readers_take(self):
        """The sheet builds a panel, so a panel's setting cannot go missing.

        `quote_sheet_from_request` reads the payload through
        `quote_panel_from_request` and keeps what it made, which is what stops
        the two readers drifting apart -- a field added to the panel is a
        field the sheet takes on the same day.
        """
        from volkit import marketmaker as mm
        payload = {"request_text": "EURUSD 1M ATM", "client": "Fund A",
                   "client_weight": "0.25", "client_min": "7", "skew_cap": "0.5",
                   "flow_weight": "0.3", "fallback_tier": "default",
                   "fallback_multiplier": "1.5", "tolerance": "0.2"}
        sheet = mm.quote_sheet_from_request(payload)
        panel = sheet.panel("EURUSD")
        alone = mm.quote_panel_from_request({**payload, "pair": "EURUSD"})
        for field in ("client", "client_weight", "client_min", "skew_cap", "flow_weight",
                      "fallback_tier", "fallback_multiplier", "tolerance"):
            self.assertEqual(getattr(panel, field), getattr(alone, field), field)
        # And the panel a sheet builds refuses a bare line, where one run on
        # its own does not: `volkit mm EURUSD` names the pair itself.
        self.assertTrue(panel.require_pair)
        self.assertFalse(alone.require_pair)


class TestKnowledgeBank(unittest.TestCase):
    """Desk knowledge as an overlay, and the rules for resolving it."""

    def bank(self):
        pk = PairKnowledge(rules=[
            Rule("spread", 0.30, "any"),
            Rule("spread", 0.25, "atm", max_days=31),
            Rule("spread", 0.45, "atm", max_days=31, max_size=200),
            Rule("floor", 0.15),
            Rule("floor", 0.20, "fly"),
            Rule("shift", 0.05, "atm", tenor="1W"),
            Rule("note", text="wings always wider into an ECB week"),
        ])
        return pk

    def test_the_narrowest_matching_rule_wins_and_is_named(self):
        pk = self.bank()
        wide = pk.overlay(instrument="atm", days=20, tenor="1M", size=150, size_basis="vega")
        self.assertEqual(wide.spread, 0.45)
        self.assertIn("200mm", wide.spread_rule)
        # Over the size band, the size-conditioned rule no longer matches.
        small = pk.overlay(instrument="atm", days=20, tenor="1M", size=500, size_basis="vega")
        self.assertEqual(small.spread, 0.25)
        far = pk.overlay(instrument="atm", days=200, tenor="6M")
        self.assertEqual(far.spread, 0.30)

    def test_a_floor_is_the_widest_matching_one_not_the_narrowest(self):
        """Every floor applies and the widest wins, because that is what a
        floor means; widths take the narrowest rule instead."""
        pk = PairKnowledge(rules=[Rule("spread", 0.05, "fly"), Rule("floor", 0.15),
                                  Rule("floor", 0.22, "fly")])
        got = pk.overlay(instrument="fly", days=30, tenor="1M", delta=0.25)
        self.assertEqual(got.floor, 0.22)
        self.assertEqual(got.spread, 0.22)

    def test_a_note_is_shown_and_never_applied(self):
        got = self.bank().overlay(instrument="rr", days=30, tenor="1M", delta=0.25)
        self.assertEqual(got.notes, ("wings always wider into an ECB week",))
        self.assertEqual(got.shift, 0.0)

    def test_no_matching_rule_means_no_width_not_an_invented_one(self):
        """There is no built-in default anywhere.  A number on a screen with no
        source is the same failure as a silent zero."""
        pk = PairKnowledge(rules=[Rule("spread", 0.25, "atm")])
        got = pk.overlay(instrument="rr", days=30, tenor="1M", delta=0.25)
        self.assertIsNone(got.spread)
        self.assertIn("no width rule", got.reason)
        fell_back = pk.overlay(instrument="rr", days=30, tenor="1M", delta=0.25, fallback=0.4)
        self.assertEqual(fell_back.spread, 0.4)
        self.assertIsNone(fell_back.spread_rule)
        self.assertIn("fallback tier", fell_back.reason)

    def test_a_bad_rule_set_is_rejected_whole(self):
        bank = KnowledgeBank()
        problems = bank.set_pair("EURUSD", [Rule("spread", 0.25, "atm"), Rule("spread", -1.0)],
                                 ASOF.now)
        self.assertTrue(problems)
        self.assertEqual(bank.pairs, {})

    def test_the_bank_round_trips_through_its_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mm_knowledge.json"
            bank = KnowledgeBank()
            self.assertEqual(bank.set_pair("USDJPY", self.bank().rules, ASOF.now, "test"), [])
            bank.save(path)
            back = KnowledgeBank.load(path)
            self.assertEqual([r.describe() for r in back.for_pair("usdjpy").rules],
                             [r.describe() for r in self.bank().rules])
            self.assertEqual(back.for_pair("USDJPY").source_note, "test")

    def test_a_missing_bank_is_empty_and_says_so_rather_than_failing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bank = KnowledgeBank.load(Path(tmp) / "nothing.json")
            self.assertEqual(bank.pairs, {})
            self.assertTrue(any("will be created" in p for p in bank.problems))

    def test_learning_measures_the_paste_and_ignores_choice_prices(self):
        """A quote written as a single mid has no width; averaging its zero in
        would quietly tighten the whole ladder.

        The two 1M lines are the same quote twice, so only the later one is
        live -- but both are evidence of how wide this market is shown, which
        is why the bank reads ``all_quotes`` and the fit reads ``quotes``.
        """
        from volkit import agent, archive as arch
        from volkit.timeutil import Clock
        run = quotes.parse_quotes(
            "1M atm 8.20/8.60\n1M atm 8.30/8.70\n2M atm 9.00\n", pair="EURUSD")
        self.assertEqual(len(run.quotes), 2)
        self.assertEqual(len(run.superseded), 1)
        # Learning a width is the archive's job, and the paste is counted on
        # its way through: both 1M lines are evidence, the choice price is
        # not, and nothing is written.
        empty = arch.Archive(path="")
        got = agent.learn_widths(empty, "EURUSD", clock=Clock(ASOF.now),
                                 paste=agent.Paste(pair="EURUSD", text=(
                                     "1M atm 8.20/8.60\n1M atm 8.30/8.70\n2M atm 9.00\n")))
        self.assertEqual(len(got.rules), 1)
        self.assertAlmostEqual(got.rules[0].value, 0.40, places=6)
        self.assertEqual(got.from_paste, 3, "the choice price is filed, and sets no width")
        self.assertIn("2 observation(s)", got.rules[0].text)
        self.assertEqual(len(empty.records), 0, "proposing is not filing")


if __name__ == "__main__":
    unittest.main()
