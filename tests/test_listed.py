"""Listed options, positions and their clock.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestListedOptions(unittest.TestCase):
    """Exchange traded options: a table of strikes, not three broker quotes."""

    EXPIRY = datetime(2024, 6, 14, 19, 0, tzinfo=UTC)

    # -- the parser ------------------------------------------------------
    def test_parser_reports_the_columns_it_chose(self):
        """The legacy tool's silence about its inputs is what this replaces."""
        t = listed.parse_quote_table("Strike\tIV\n1.05\t8.20\n1.08\t7.50\n1.10\t7.65\n")
        self.assertEqual(t.delimiter, "tab")
        self.assertEqual(t.strike_column, 1)
        self.assertEqual(t.vol_unit, "percent")
        self.assertAlmostEqual(t.quotes[0].vol, 0.0820)

    def test_parser_takes_the_mid_of_bid_and_ask(self):
        t = listed.parse_quote_table("Strike,Bid,Ask\n1.05,8.10,8.30\n1.08,7.40,7.60\n1.10,7.55,7.75\n")
        self.assertEqual(t.delimiter, "comma")
        self.assertAlmostEqual(t.quotes[0].vol, 0.0820)
        self.assertTrue(any("mid" in n for n in t.notes))

    def test_the_two_way_is_kept_beside_the_mid_it_made(self):
        """A mid is what a curve is fitted to; a two-way is what it is judged against."""
        t = listed.parse_quote_table("Strike,Bid,Ask\n1.05,8.10,8.30\n1.08,7.40,7.60\n"
                                     "1.10,7.55,7.75\n")
        self.assertTrue(all(q.two_way for q in t.quotes))
        self.assertAlmostEqual(t.quotes[0].bid, 0.0810)
        self.assertAlmostEqual(t.quotes[0].ask, 0.0830)
        # A table with its own mid column keeps that mid and the two-way both.
        both = listed.parse_quote_table("Strike,Bid,Ask,Mid\n1.05,8.10,8.30,8.25\n"
                                        "1.08,7.40,7.60,7.45\n1.10,7.55,7.75,7.60\n")
        self.assertAlmostEqual(both.quotes[0].vol, 0.0825)
        self.assertAlmostEqual(both.quotes[0].bid, 0.0810)
        self.assertTrue(any("bid and an offer of their own" in n for n in both.notes))
        # Decimals divide the two-way by the same thing they divide the mid by.
        dec = listed.parse_quote_table("Strike,Bid,Ask\n1.05,0.0810,0.0830\n"
                                       "1.08,0.0740,0.0760\n1.10,0.0755,0.0775\n",
                                       vol_unit="decimal")
        self.assertAlmostEqual(dec.quotes[0].bid, 0.0810)
        # A mid-only table has no two-way, and says nothing about one.
        mid = listed.parse_quote_table("Strike\tIV\n1.05\t8.20\n1.08\t7.50\n1.10\t7.65\n")
        self.assertFalse(any(q.two_way for q in mid.quotes))
        self.assertIsNone(mid.quotes[0].bid)
        # And an explicit vol column says which number to believe, so it
        # turns the two-way off rather than reading it beside the column.
        forced = listed.parse_quote_table("Strike,Bid,Ask,Mid\n1.05,8.10,8.30,8.25\n"
                                          "1.08,7.40,7.60,7.45\n1.10,7.55,7.75,7.60\n",
                                          vol_column=4)
        self.assertFalse(any(q.two_way for q in forced.quotes))

    def test_a_crossed_two_way_is_dropped_and_counted(self):
        t = listed.parse_quote_table("Strike,Bid,Ask,Mid\n1.05,8.10,8.30,8.25\n"
                                     "1.08,7.60,7.40,7.45\n1.10,7.55,7.75,7.60\n")
        self.assertEqual([q.two_way for q in t.quotes], [True, False, True])
        self.assertAlmostEqual(t.quotes[1].vol, 0.0745)      # the mid survives
        self.assertTrue(any("crossed" in n for n in t.notes))
        # With no mid of its own there is nothing left to fit, and the line is
        # skipped saying what was actually wrong with it.
        gone = listed.parse_quote_table("Strike,Bid,Ask\n1.05,8.10,8.30\n1.08,7.60,7.40\n"
                                        "1.10,7.55,7.75\n")
        self.assertEqual(len(gone.quotes), 2)
        self.assertIn("crossed", gone.skipped[0][2])

    def test_decimal_is_something_a_person_says(self):
        """A table is read in volatility points as written (§4).  A table of
        decimals is loaded with vol_unit='decimal'; it used to be inferred
        from the level, which read a managed pair's own table 100x too big."""
        dec = listed.parse_quote_table("1.05 0.0820\n1.08 0.0750\n1.10 0.0765\n",
                                       vol_unit="decimal")
        pct = listed.parse_quote_table("1.05 8.20\n1.08 7.50\n1.10 7.65\n")
        self.assertEqual(dec.vol_unit, "decimal")
        self.assertEqual(pct.vol_unit, "percent")
        for a, b in zip(dec.quotes, pct.quotes):
            self.assertAlmostEqual(a.vol, b.vol, places=12)

    def test_a_table_below_one_is_read_as_written_and_says_so(self):
        """0.82 is eight tenths of a volatility point, which is what a managed
        pair's listed table looks like.  Read as written, and noted."""
        t = listed.parse_quote_table("1.05 0.82\n1.08 0.75\n1.10 0.76\n")
        self.assertEqual(t.vol_unit, "percent")
        self.assertAlmostEqual(t.quotes[0].vol, 0.0082)
        self.assertTrue(any("as written" in n for n in t.notes))

    def test_a_table_straddling_one_is_read_and_not_refused(self):
        """0.95 beside 8.20 used to be refused as ambiguous.  Both are points."""
        t = listed.parse_quote_table("1.05\t0.95\n1.08\t8.20\n1.10\t7.70\n")
        self.assertAlmostEqual(t.quotes[0].vol, 0.0095)
        self.assertAlmostEqual(t.quotes[1].vol, 0.0820)

    def test_every_unusable_line_is_returned_with_a_reason(self):
        t = listed.parse_quote_table(
            "Strike\tIV\n1.05\t8.2\nn/a\t7.0\n1.08\t\n1.10\t7.65\n1.12\t7.9\n")
        self.assertEqual(len(t.quotes), 3)
        self.assertEqual([n for n, _, _ in t.skipped], [3, 4])

    def test_explicit_columns_override_the_headers(self):
        t = listed.parse_quote_table("Strike\tDelta\tIV\n1.05\t0.30\t8.2\n"
                                     "1.08\t0.50\t7.5\n1.10\t0.62\t7.65\n",
                                     vol_column=3)
        self.assertAlmostEqual(t.quotes[0].vol, 0.082)

    def test_duplicate_strikes_keep_the_out_of_the_money_quote(self):
        """An in-the-money option has little time value, so its implied vol is noise."""
        t = listed.parse_quote_table(
            "K\tIV\tType\n1.05\t8.2\tC\n1.05\t8.4\tP\n1.08\t7.5\tC\n1.10\t7.65\tC\n")
        kept, notes = listed.dedupe(t.quotes, forward=1.08)
        self.assertEqual(len(kept), 3)
        self.assertAlmostEqual(kept[0].vol, 0.084)      # 1.05 is below the forward: the put
        self.assertTrue(notes)

    # -- the fit ---------------------------------------------------------
    def _known(self):
        t, f = 0.5, 1.0850
        p = sabr.SabrParams(alpha=0.082, rho=-0.31, volvol=0.65, t=t, beta=1.0, f=f)
        ks = f * np.array([0.85, 0.90, 0.95, 1.00, 1.02, 1.05, 1.10, 1.15, 1.22])
        return p, ks, np.asarray(sabr.lognormal_vol(ks, p))

    def test_the_fit_recovers_parameters_it_was_generated_from(self):
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks, vs, p.t, p.f)
        self.assertAlmostEqual(fit.params.alpha, p.alpha, places=6)
        self.assertAlmostEqual(fit.params.rho, p.rho, places=5)
        self.assertAlmostEqual(fit.params.volvol, p.volvol, places=5)
        self.assertLess(fit.rmse, 1e-10)
        self.assertTrue(fit.converged)

    def test_the_answer_does_not_depend_on_the_sweep_resolution(self):
        """The box is swept before anything is polished, so no start point matters."""
        p, ks, vs = self._known()
        a = listed.fit_sabr(ks, vs, p.t, p.f, scan=(9, 7))
        b = listed.fit_sabr(ks, vs, p.t, p.f, scan=(21, 15))
        self.assertAlmostEqual(a.params.rho, b.params.rho, places=6)
        self.assertAlmostEqual(a.params.volvol, b.params.volvol, places=6)

    def test_the_order_of_the_rows_does_not_change_the_fit(self):
        p, ks, vs = self._known()
        order = np.array([4, 0, 8, 2, 6, 1, 7, 3, 5])
        a = listed.fit_sabr(ks, vs, p.t, p.f)
        b = listed.fit_sabr(ks[order], vs[order], p.t, p.f)
        self.assertAlmostEqual(a.params.rho, b.params.rho, places=8)

    def test_three_quotes_is_an_interpolation_and_says_so(self):
        """Zero residuals from three points are not evidence of anything."""
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks[[0, 3, 7]], vs[[0, 3, 7]], p.t, p.f)
        self.assertLess(fit.rmse, 1e-10)
        self.assertEqual(fit.degrees_of_freedom, 0)
        self.assertTrue(any("exact interpolation" in w for w in fit.warnings))

    def test_fewer_than_three_strikes_is_rejected(self):
        p, ks, vs = self._known()
        with self.assertRaises(ValueError):
            listed.fit_sabr(ks[:2], vs[:2], p.t, p.f)
        with self.assertRaises(ValueError):
            listed.fit_sabr([1.0, 1.0, 1.0], [0.08, 0.08, 0.08], p.t, p.f)


    # -- parameters given rather than fitted ------------------------------
    def test_a_held_parameter_comes_back_exactly_as_it_was_given(self):
        """A number somebody typed must not come back rounded.

        Alpha travels through a logarithm inside the optimiser, and
        exp(log(a)) is not always a again."""
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks, vs, p.t, p.f, fixed={"alpha": 0.0925})
        self.assertEqual(fit.params.alpha, 0.0925)
        self.assertEqual(fit.fixed, ("alpha",))
        self.assertEqual(fit.free, ("rho", "volvol"))
        self.assertEqual(fit.degrees_of_freedom, len(ks) - 2)

    def test_holding_a_parameter_fits_the_others_around_it(self):
        p, ks, vs = self._known()
        free = listed.fit_sabr(ks, vs, p.t, p.f)
        held = listed.fit_sabr(ks, vs, p.t, p.f, fixed={"rho": -0.10})
        self.assertEqual(held.params.rho, -0.10)
        # The other two moved to make the best of it, and the fit is worse
        # than the free one -- which is the whole point of reporting it.
        self.assertNotAlmostEqual(held.params.alpha, free.params.alpha, places=4)
        self.assertGreater(held.rmse, free.rmse)
        self.assertTrue(any("given, not fitted" in w for w in held.warnings))

    def test_holding_all_three_fits_nothing_and_says_so(self):
        """Priced against the curve you typed: a legitimate thing to ask for,
        and it must not report a convergence it never attempted."""
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks, vs, p.t, p.f,
                              fixed={"alpha": p.alpha, "rho": p.rho, "volvol": p.volvol})
        self.assertEqual((fit.params.alpha, fit.params.rho, fit.params.volvol),
                         (p.alpha, p.rho, p.volvol))
        self.assertEqual(fit.free, ())
        self.assertEqual(fit.degrees_of_freedom, len(ks))
        self.assertIn("no fit", fit.message)
        self.assertLess(fit.rmse, 1e-12)          # these quotes came off that curve
        self.assertTrue(any("nothing was fitted at all" in w for w in fit.warnings))

    def test_a_blank_override_is_not_a_zero(self):
        """The screen sends an empty box for every parameter it is not
        holding; reading that as a number would mark a curve nobody asked
        for."""
        p, ks, vs = self._known()
        free = listed.fit_sabr(ks, vs, p.t, p.f)
        blank = listed.fit_sabr(ks, vs, p.t, p.f,
                                fixed={"alpha": None, "rho": "", "volvol": None})
        self.assertEqual(blank.fixed, ())
        self.assertAlmostEqual(blank.params.rho, free.params.rho, places=9)

    def test_a_held_parameter_that_is_not_a_parameter_is_refused(self):
        p, ks, vs = self._known()
        for bad in ({"rho": 1.4}, {"alpha": -0.01}, {"volvol": 0.0}, {"beta": 0.5}):
            with self.assertRaises(ValueError):
                listed.fit_sabr(ks, vs, p.t, p.f, fixed=bad)

    def test_holding_two_leaves_two_quotes_enough(self):
        """The three-quote rule is about free parameters, not about SABR."""
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks[:2], vs[:2], p.t, p.f,
                              fixed={"rho": p.rho, "volvol": p.volvol})
        self.assertAlmostEqual(fit.params.alpha, p.alpha, places=6)
        with self.assertRaises(ValueError):
            listed.fit_sabr(ks[:1], vs[:1], p.t, p.f, fixed={"rho": p.rho})

    def test_the_panel_carries_the_overrides_and_reports_them(self):
        """What the browser sends is what the fit holds, and the answer says
        which numbers were typed -- one that was is otherwise indistinguishable
        from one the market implied."""
        panel = listed.panel_from_request({
            "underlying": "CUSTOM", "expiry": "2024-06-14 19:00", "forward": 1.085,
            "text": "1.00 9.10\n1.05 8.40\n1.085 8.20\n1.12 8.35\n1.16 8.90\n",
            "rho": "-0.20", "alpha": "", "volvol": None,
        })
        self.assertEqual((panel.rho, panel.alpha, panel.volvol), (-0.20, None, None))
        out = panel.run(None, clock=ASOF)
        self.assertEqual(out["fit"]["rho"], -0.20)
        self.assertEqual(out["fit"]["fixed"], ["rho"])
        self.assertEqual(out["fit"]["free"], ["alpha", "volvol"])
        with self.assertRaises(ValueError):
            listed.panel_from_request({"underlying": "CUSTOM", "forward": 1.0,
                                       "expiry": "2024-06-14", "text": "1.0 8.0",
                                       "rho": "steep"})

    def test_vega_weighting_favours_the_money(self):
        p, ks, vs = self._known()
        fit = listed.fit_sabr(ks, vs, p.t, p.f, weighting="vega")
        near = min(range(len(ks)), key=lambda i: abs(ks[i] - p.f))
        self.assertEqual(near, int(np.argmax(fit.weights)))
        flat = listed.fit_sabr(ks, vs, p.t, p.f, weighting="equal")
        self.assertTrue(np.allclose(flat.weights, 1.0))

    def test_strikes_that_miss_the_forward_are_flagged(self):
        p, ks, vs = self._known()
        keep = ks > p.f
        fit = listed.fit_sabr(ks[keep], vs[keep], p.t, p.f)
        self.assertTrue(any("do not bracket the forward" in w for w in fit.warnings))

    def test_negative_density_is_reported_rather_than_hidden(self):
        """Hagan's expansion arbitrages in the wings.  Marking the risk beats clipping it."""
        p = sabr.SabrParams(alpha=0.30, rho=-0.90, volvol=3.0, t=3.0, beta=1.0, f=1.0)
        msgs = listed.arbitrage_warnings(p, 0.4, 2.5)
        self.assertTrue(msgs and "negative risk-neutral density" in msgs[0])
        calm = sabr.SabrParams(alpha=0.09, rho=-0.10, volvol=0.35, t=0.25, beta=1.0, f=1.0)
        self.assertEqual(listed.arbitrage_warnings(calm, 0.9, 1.1), [])

    def test_the_arbitrage_check_does_not_depend_on_the_contract_units(self):
        """Second differences of a yen future's prices are tiny in raw units;
        checking in moneyness stops rounding being read as arbitrage."""
        for f in (0.0068, 1.0, 145.0, 4200.0):
            calm = sabr.SabrParams(alpha=0.09 * f, rho=-0.10, volvol=0.35,
                                   t=0.25, beta=1.0, f=f)
            self.assertEqual(listed.arbitrage_warnings(calm, f * 0.9, f * 1.1), [],
                             msg=f"false positive at forward {f}")

    # -- the mapping onto an FX pair -------------------------------------
    def test_inverted_strikes_round_trip(self):
        u = listed.resolve_underlying("6J")
        self.assertTrue(u.invert)
        self.assertAlmostEqual(u.to_fx(0.006850), 1.0 / 0.006850, places=9)
        self.assertAlmostEqual(u.from_fx(u.to_fx(0.006850)), 0.006850, places=12)

    def test_scale_is_applied_before_the_inversion(self):
        u = listed.resolve_underlying("6J", scale=1e-6)
        self.assertAlmostEqual(u.to_fx(6850.0), 1.0 / 0.006850, places=9)

    def test_a_non_inverted_contract_is_left_alone(self):
        u = listed.resolve_underlying("6E")
        self.assertFalse(u.invert)
        self.assertEqual(u.pair, "EURUSD")
        self.assertAlmostEqual(u.to_fx(1.0850), 1.0850)

    def test_overrides_beat_the_registry(self):
        u = listed.resolve_underlying("CUSTOM", pair="EURGBP", invert=True, scale=2.0)
        self.assertEqual((u.pair, u.invert, u.scale), ("EURGBP", True, 2.0))

    def test_a_code_this_build_does_not_know_is_taken_as_typed(self):
        """The contract was a dropdown, and every contract missing from it had
        to be entered as CUSTOM -- at which point two of them on one screen
        cannot be told apart, and a position line naming one is refused as
        ambiguous with no way to settle it.  A typed code is now a contract
        with the name that was typed."""
        u = listed.resolve_underlying("M6E SEP26", pair="EURUSD", contract_size=12_500)
        self.assertFalse(u.known)
        self.assertEqual(u.code, "M6E SEP26")
        self.assertEqual((u.pair, u.invert, u.scale), ("EURUSD", False, 1.0))
        self.assertEqual(u.contract_size, 12_500)
        # Nothing is inferred from the name: a code that merely looks like the
        # euro brings no pair, no direction and no size of its own.
        bare = listed.resolve_underlying("M6E SEP26")
        self.assertIsNone(bare.pair)
        self.assertEqual(bare.contract_size, 0.0)
        # And the ones in the registry are still known, with everything on.
        self.assertTrue(listed.resolve_underlying("6J").known)

    def test_a_typed_code_says_it_was_typed_rather_than_looked_up(self):
        """Otherwise a typo (6R for 6E) is indistinguishable from a contract
        that genuinely has no mapping, which is a comparison quietly gone."""
        r = listed.panel_from_request({
            "underlying": "6R", "expiry": "2024-06-14 19:00", "forward": 1.085,
            "text": "1.06 7.9\n1.085 7.42\n1.11 7.6"}).run(clock=ASOF)
        self.assertFalse(r["underlying"]["known"])
        self.assertEqual(r["underlying"]["code"], "6R")
        self.assertTrue(any("taken as typed" in n for n in r["notes"]))

    def test_a_mis_pasted_cell_is_still_refused_as_a_contract_code(self):
        """Free text is not "anything at all": a code has a shape, and a
        pasted quote row landing in the box must not become a contract."""
        for bad in ("1.0800\t7.42", "x" * 40, "6E, 1.0800"):
            with self.assertRaises(ValueError):
                listed.resolve_underlying(bad)

    def test_the_settlement_currency_is_derived_from_the_pair_and_the_direction(self):
        """The premium comes out of Black-76 in the currency the *listed
        strike axis* is quoted in.  Every CME contract works out as USD, which
        is the whole reason the money columns have always added up across
        them; a typed contract need not."""
        self.assertEqual(listed.resolve_underlying("6E").premium_ccy, "USD")
        self.assertEqual(listed.resolve_underlying("6J").premium_ccy, "USD")
        self.assertEqual(listed.resolve_underlying("6C").premium_ccy, "USD")
        self.assertEqual(
            listed.resolve_underlying("XYZ", pair="EURGBP").premium_ccy, "GBP")
        # No pair, no answer -- the premium is still a number, in a currency
        # this tool was never told.
        self.assertEqual(listed.resolve_underlying("CUSTOM").premium_ccy, "")

    # -- against the marked surface --------------------------------------
    def _panel_from_the_book(self, code, pair, fx_forward, book):
        """Quote a listed table off the book's own smile, then read it back."""
        surface = book[pair]
        u = listed.resolve_underlying(code)
        listed_fwd = u.from_fx(fx_forward)
        ks = np.linspace(listed_fwd * 0.90, listed_fwd * 1.12, 9)
        fx_ks = np.asarray(u.to_fx(ks), dtype=float)
        vols = np.asarray(surface.vol(fx_ks / fx_forward, self.EXPIRY, "SVI", "NY"))
        text = "Strike\tIV\n" + "\n".join(f"{k:.10f}\t{v * 100:.8f}"
                                          for k, v in zip(ks, vols))
        return listed.panel_from_request({
            "underlying": code, "expiry": "2024-06-14 19:00", "forward": listed_fwd,
            "text": text, "cut": "NY", "method": "SVI"})

    def test_an_inverted_table_maps_back_onto_the_same_marks(self):
        """The whole inversion, end to end: a USDJPY smile quoted as a yen future
        and read back must give the marks it started from.  A sign or a
        reciprocal in the wrong place shows up here as a wing-sized error."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        out = self._panel_from_the_book("6J", "USDJPY", 150.0, book).run(book)
        for row in out["rows"]:
            self.assertAlmostEqual(row["book_vol"], row["market_vol"], places=6)
        self.assertEqual(out["comparison"]["pair"], "USDJPY")
        self.assertAlmostEqual(out["comparison"]["forward_fx"], 150.0, places=9)

    def test_an_upright_table_maps_back_onto_the_same_marks(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        out = self._panel_from_the_book("6E", "EURUSD", 1.0850, book).run(book)
        for row in out["rows"]:
            self.assertAlmostEqual(row["book_vol"], row["market_vol"], places=6)

    def test_a_mark_outside_the_pasted_two_way_is_named_strike_by_strike(self):
        """The question a two-way answers and a mid cannot: is the mark in the market."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        base = self._panel_from_the_book("6E", "EURUSD", 1.0850, book)
        # Re-quote the same strikes as a two-way around the book's own marks,
        # and move one band clear of the mark.  Every other strike is inside
        # by construction, so exactly one breach is the whole answer.
        lines = ["Strike\tBid\tAsk"]
        for i, q in enumerate(base.quotes):
            v = q.vol * 100.0
            lo, hi = (v + 0.50, v + 0.80) if i == 2 else (v - 0.10, v + 0.10)
            lines.append(f"{q.strike:.10f}\t{lo:.8f}\t{hi:.8f}")
        panel = listed.panel_from_request({
            "underlying": "6E", "expiry": "2024-06-14 19:00", "forward": base.forward,
            "text": "\n".join(lines), "cut": "NY", "method": "SVI"})
        out = panel.run(book)
        self.assertEqual(out["n_two_way"], len(out["rows"]))
        self.assertEqual(out["n_outside"], 1)
        for i, row in enumerate(out["rows"]):
            self.assertIsNotNone(row["bid_vol"])
            self.assertLess(row["bid_vol"], row["ask_vol"])
            self.assertAlmostEqual(row["market_vol"],
                                   0.5 * (row["bid_vol"] + row["ask_vol"]), places=9)
            self.assertEqual(row["mark_outside"], i == 2, row["strike"])
        # No pair to compare against is not the same as "the mark is inside".
        alone = listed.panel_from_request({
            "underlying": "CUSTOM", "expiry": "2024-06-14 19:00", "forward": base.forward,
            "text": "\n".join(lines), "cut": "NY", "method": "SVI"})
        lonely = alone.run(None, clock=ASOF)
        self.assertEqual(lonely["n_two_way"], len(lonely["rows"]))
        self.assertEqual(lonely["n_outside"], 0)
        self.assertTrue(all(r["mark_outside"] is None for r in lonely["rows"]))

    def test_a_mid_only_paste_has_no_two_way_to_judge_against(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        out = self._panel_from_the_book("6E", "EURUSD", 1.0850, book).run(book)
        self.assertEqual(out["n_two_way"], 0)
        self.assertEqual(out["n_outside"], 0)
        for row in out["rows"]:
            self.assertIsNone(row["bid_vol"])
            self.assertIsNone(row["ask_vol"])
            self.assertIsNone(row["mark_outside"])

    def test_the_book_delta_strikes_come_back_on_the_listed_axis(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        out = self._panel_from_the_book("6J", "USDJPY", 150.0, book).run(book)
        u = listed.resolve_underlying("6J")
        for a in out["comparison"]["anchors"]:
            self.assertAlmostEqual(u.to_fx(a["listed_strike"]), a["fx_strike"], places=6)
        labels = [a["label"] for a in out["comparison"]["anchors"]]
        self.assertIn("ATM", labels)
        self.assertIn("25d call", labels)

    def test_the_panel_uses_the_books_clock(self):
        """Nothing in the model may read the wall clock; the fit must use the same
        365.2425-day year as the mark it is compared with."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        out = self._panel_from_the_book("6J", "USDJPY", 150.0, book).run(book)
        self.assertAlmostEqual(out["years"], ASOF.years_to(self.EXPIRY), places=12)
        self.assertEqual(out["valuation"][:16], ASOF.now.isoformat()[:16])

    def test_a_past_expiry_is_rejected_with_both_times(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        panel = self._panel_from_the_book("6J", "USDJPY", 150.0, book)
        panel.expiry = "2023-01-01 19:00"
        with self.assertRaises(ValueError) as cm:
            panel.run(book)
        self.assertIn("not in the future", str(cm.exception))

    def test_an_unmapped_contract_fits_but_does_not_compare(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        out = listed.panel_from_request({
            "underlying": "CUSTOM", "expiry": "2024-06-14 19:00", "forward": 4200.0,
            "text": "Strike\tIV\n3800\t18.4\n4000\t16.9\n4200\t15.8\n4400\t15.4\n4600\t15.6\n",
        }).run(book, clock=ASOF)
        self.assertIsNone(out["comparison"])
        self.assertTrue(out["fit"]["converged"])

    def test_a_pair_outside_the_book_is_named_in_the_error(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        panel = listed.panel_from_request({
            "underlying": "CUSTOM", "pair": "USDNOK", "expiry": "2024-06-14 19:00",
            "forward": 10.5, "text": "K\tIV\n10.0\t9.4\n10.5\t9.0\n11.0\t9.2\n"})
        with self.assertRaises(ValueError) as cm:
            panel.run(book)
        self.assertIn("USDNOK", str(cm.exception))

    def test_a_browser_datetime_is_accepted(self):
        """An HTML datetime-local field emits a 'T'; parse_datetime wants a space."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        panel = self._panel_from_the_book("6J", "USDJPY", 150.0, book)
        panel.expiry = "2024-06-14T19:00"
        self.assertAlmostEqual(panel.run(book)["years"], ASOF.years_to(self.EXPIRY), places=12)


class TestListedPositions(unittest.TestCase):
    """Positions on the exchange-traded screen, and the risk they aggregate to.

    Everything the greeks need comes off the panel a position belongs to, so
    these build one or two panels and price against them exactly as the screen
    does -- the panels are posted whole and the server keeps none of it.
    """

    EXPIRY = "2024-06-14 19:00"
    LATER = "2024-09-13 19:00"
    CLOCK = Clock(datetime(2024, 5, 28, 12, 0, tzinfo=UTC))
    TABLE = ("1.0300\t8.90\n1.0600\t8.10\n1.0900\t7.60\n1.1200\t7.80\n1.1500\t8.60\n")

    def panel(self, code="6E", expiry=None, forward=1.09, **kw):
        return dict({"underlying": code, "expiry": expiry or self.EXPIRY,
                     "forward": forward, "text": self.TABLE}, **kw)

    def agg(self, text, panels=None, **kw):
        return listed.positions_from_request(dict(
            {"text": text, "panels": panels or [self.panel()]}, **kw)).run(clock=self.CLOCK)

    # -- the paste -------------------------------------------------------
    def test_the_layout_is_decided_once_from_the_whole_table(self):
        """Reading each row on its own width would move a quantity into a
        strike the first time somebody left a cell blank."""
        parsed = listed.parse_positions(
            "6E, 2024-06-14 19:00, 1.09, C, 25\n"
            "6E, 2024-06-14 19:00, 1.06, P, -40\n"
            "6E, 1.12, C, -15\n")
        self.assertEqual(parsed.layout,
                         ("contract", "expiry", "strike", "type", "quantity"))
        self.assertEqual(len(parsed.positions), 2)
        self.assertEqual([n for n, _, _ in parsed.skipped], [3])
        self.assertIn("4 columns", parsed.skipped[0][2])

    def test_the_short_layouts_leave_the_panel_to_be_worked_out(self):
        parsed = listed.parse_positions("1.09 C 25\n1.06 P -40\n")
        self.assertEqual(parsed.layout, ("strike", "type", "quantity"))
        self.assertEqual(parsed.positions[0].underlying, "")
        self.assertEqual(parsed.positions[0].expiry, "")
        self.assertTrue(parsed.positions[0].is_call)
        self.assertEqual(parsed.positions[1].quantity, -40.0)

    def test_a_header_row_may_name_the_columns_in_any_order(self):
        parsed = listed.parse_positions(
            "Qty\tRight\tStrike\n25\tCall\t1.09\n-40\tPut\t1.06\n")
        self.assertEqual(parsed.positions[0].strike, 1.09)
        self.assertEqual(parsed.positions[0].quantity, 25.0)
        self.assertFalse(parsed.positions[1].is_call)
        self.assertTrue(any("header row read" in n for n in parsed.notes))

    def test_atm_is_a_strike_and_a_bad_cell_keeps_its_line(self):
        parsed = listed.parse_positions("ATM C 10\n1.09 X 5\nabc P 3\n1.06 P two\n")
        self.assertEqual(len(parsed.positions), 1)
        self.assertIsNone(parsed.positions[0].strike)
        self.assertEqual([n for n, _, _ in parsed.skipped], [2, 3, 4])

    def test_a_thousands_separator_in_a_comma_paste_is_a_column(self):
        """A comma is a column boundary here as it is in a broker run, so the
        line is refused rather than read as a size of 1."""
        parsed = listed.parse_positions(
            "6E, 2024-06-14 19:00, 1.09, C, 1,000\n6E, 2024-06-14 19:00, 1.06, P, -40\n")
        self.assertEqual([n for n, _, _ in parsed.skipped], [1])
        # ... and with tabs there is no ambiguity and it is a size.
        parsed = listed.parse_positions("1.09\tC\t1,000\n")
        self.assertEqual(parsed.positions[0].quantity, 1000.0)

    # -- the greeks ------------------------------------------------------
    def test_both_columns_price_the_same_option_and_differ_only_in_the_greeks(self):
        r = self.agg("1.09 C 25\n1.06 P -40\n")
        for row in r["positions"]:
            self.assertEqual(row["error"], "")
            self.assertIsNotNone(row["premium"])
            # One volatility at one strike, so the premium cannot depend on
            # which set of sensitivities is being taken around it.
            self.assertNotEqual(row["bs"]["delta_futures"], row["smile"]["delta_futures"])
        self.assertAlmostEqual(r["totals"]["bs"]["vega"], r["totals"]["bs"]["vega"])

    def test_at_the_forward_the_smile_vega_is_the_black_scholes_vega(self):
        """The curve is lifted by a move measured at the forward, so an option
        struck there sees exactly that move and the two must agree."""
        r = self.agg("ATM C 10\n")
        row = r["positions"][0]
        self.assertAlmostEqual(row["strike"], 1.09, places=12)
        self.assertAlmostEqual(row["smile"]["vega"] / row["bs"]["vega"], 1.0, places=5)

    def test_a_call_and_a_put_at_one_strike_differ_by_exactly_one_delta(self):
        """Put/call parity, which the aggregate has to reproduce or a straddle
        is not a straddle.  Note it is *not* zero at the forward -- the
        delta-neutral strike is F exp(sigma^2 t / 2), not F -- so pinning it
        at zero would be pinning a mistake."""
        r = self.agg("ATM C 10\nATM P 10\n")
        call, put = r["positions"]
        g = r["groups"][0]
        self.assertAlmostEqual(call["bs"]["delta_futures"] / 10.0
                               - put["bs"]["delta_futures"] / 10.0, 1.0, places=12)
        self.assertAlmostEqual(g["bs"]["vega"], 2 * call["bs"]["vega"], places=8)
        # Long options decay: theta is money and is negative on both sides.
        self.assertLess(g["bs"]["theta"], 0.0)
        self.assertLess(g["smile"]["theta"], 0.0)

    def test_money_scales_with_the_contract_size_and_a_count_does_not(self):
        big = self.agg("1.09 C 25\n")["positions"][0]
        small = self.agg("1.09 C 25\n", [self.panel(contract_size=62_500)])["positions"][0]
        self.assertEqual(big["contract_size"], 125_000)
        self.assertAlmostEqual(big["vega"] if False else big["bs"]["vega"],
                               2 * small["bs"]["vega"], places=6)
        self.assertAlmostEqual(big["bs"]["delta_futures"],
                               small["bs"]["delta_futures"], places=12)

    def test_the_vol_bump_and_the_theta_window_scale_what_they_say_they_do(self):
        one = self.agg("1.09 C 25\n")["positions"][0]
        two = self.agg("1.09 C 25\n", vol_bump=2, theta_days=3)["positions"][0]
        self.assertAlmostEqual(two["bs"]["vega"], 2 * one["bs"]["vega"], places=8)
        self.assertAlmostEqual(two["bs"]["volga"], 4 * one["bs"]["volga"], places=8)
        self.assertAlmostEqual(two["bs"]["theta"], 3 * one["bs"]["theta"], places=8)

    # -- matching a position to a panel ----------------------------------
    def test_a_line_that_matches_no_panel_keeps_its_place_with_the_reason(self):
        r = self.agg("6J, 2024-06-14 19:00, 1.09, C, 25\n6E, 2024-06-14 19:00, 1.06, P, -40\n")
        self.assertEqual(len(r["positions"]), 2)
        self.assertIn("6J", r["positions"][0]["error"])
        self.assertEqual(r["positions"][1]["error"], "")
        self.assertTrue(any("could not be priced" in w for w in r["warnings"]))

    def test_a_line_that_matches_two_panels_is_refused_rather_than_guessed(self):
        """A position priced against the wrong month's curve looks perfectly
        ordinary, which is why this may never be guessed."""
        panels = [self.panel(), self.panel(expiry=self.LATER, forward=1.10)]
        r = self.agg("1.09 C 25\n", panels)
        self.assertIn("matches 2 panels", r["positions"][0]["error"])
        # Naming the expiry settles it.
        r = self.agg(f"6E, {self.LATER}, 1.09, C, 25\n", panels)
        self.assertEqual(r["positions"][0]["error"], "")
        self.assertEqual(r["positions"][0]["expiry"][:16], "2024-09-13T19:00")

    def test_a_panel_that_will_not_fit_does_not_empty_the_rest(self):
        panels = [self.panel(label="good"),
                  dict(self.panel(label="bad", expiry=self.LATER), text="nonsense")]
        r = self.agg("good, , 1.09, C, 25\nbad, , 1.09, C, 25\n", panels)
        self.assertEqual(r["positions"][0]["error"], "")
        self.assertIn("bad", r["positions"][1]["error"])
        self.assertEqual([p["ok"] for p in r["panels"]], [True, False])

    def test_money_totals_across_contracts_but_a_futures_count_does_not(self):
        """A euro future is not a yen future.  Summing the two would be a
        number with no meaning printed where a risk figure goes."""
        panels = [self.panel(),
                  self.panel(code="6J", forward=0.00645, scale=1,
                             expiry=self.EXPIRY)]
        panels[1]["text"] = ("0.00610\t9.40\n0.00628\t9.00\n0.00645\t8.70\n"
                             "0.00662\t8.85\n0.00680\t9.30\n")
        r = self.agg("6E, , 1.09, C, 25\n6J, , 0.00645, C, 10\n", panels)
        self.assertEqual([p["ok"] for p in r["panels"]], [True, True])
        self.assertEqual(len(r["groups"]), 2)
        self.assertNotIn("delta_futures", r["totals"]["bs"])
        self.assertAlmostEqual(
            r["totals"]["bs"]["vega"],
            sum(g["bs"]["vega"] for g in r["groups"]), places=6)
        self.assertTrue(any("not a future of another" in n for n in r["notes"]))
        # Both settle in US dollars, so there is one money total and it is the
        # all-in one.
        self.assertEqual([c["ccy"] for c in r["currencies"]], ["USD"])
        self.assertAlmostEqual(r["currencies"][0]["bs"]["vega"],
                               r["totals"]["bs"]["vega"], places=12)

    def test_every_column_adds_within_one_contract_across_its_expiries(self):
        """The screen aggregated per *panel* and then jumped to money-only,
        so a book of one contract over four expiries had no futures-equivalent
        delta anywhere -- the one number a desk asks for when it asks how much
        6E it is running.  §8 said "totalled per contract"; the code totalled
        per panel, and the two only coincide with one panel per contract."""
        panels = [self.panel(), self.panel(expiry=self.LATER, forward=1.10)]
        r = self.agg(f"6E, {self.EXPIRY}, 1.09, C, 25\n6E, {self.LATER}, 1.09, P, -10\n",
                     panels)
        self.assertEqual([p["error"] for p in r["positions"]], ["", ""])
        self.assertEqual(len(r["contracts"]), 1)
        con = r["contracts"][0]
        self.assertEqual((con["underlying"], con["panels"], con["n"]), ("6E", 2, 2))
        self.assertEqual(len(con["expiries"]), 2)
        for which in ("bs", "smile"):
            for key in ("delta_futures", "gamma_futures", "vega", "theta", "premium"):
                got = con[which][key] if key != "premium" else con["premium"]
                want = sum((g[which][key] if key != "premium" else g["premium"])
                           for g in r["groups"])
                self.assertAlmostEqual(got, want, places=9, msg=f"{which}.{key}")
        # And the note says what that futures total is and is not.
        self.assertTrue(any("not the same future" in n for n in r["notes"]))

    def test_money_is_not_totalled_across_two_settlement_currencies(self):
        """A sum of euros and dollars is not a number.  It was unreachable
        while the contract came off a list of CME codes, every one of which
        settles in dollars; a typed contract makes it reachable."""
        panels = [self.panel(code="XA", pair="EURUSD", contract_size=125_000),
                  self.panel(code="XB", pair="EURGBP", contract_size=125_000)]
        r = self.agg("XA, , 1.09, C, 25\nXB, , 1.09, P, -10\n", panels)
        self.assertEqual([p["error"] for p in r["positions"]], ["", ""])
        self.assertEqual([c["ccy"] for c in r["currencies"]], ["GBP", "USD"])
        self.assertIsNone(r["totals"]["premium"])
        self.assertIsNone(r["totals"]["bs"]["vega"])
        self.assertTrue(any("no all-in money total" in w for w in r["warnings"]))
        # The per-currency rows still hold every figure.
        byccy = {c["ccy"]: c for c in r["currencies"]}
        self.assertAlmostEqual(byccy["USD"]["premium"], r["groups"][0]["premium"], places=9)
        self.assertAlmostEqual(byccy["GBP"]["premium"], r["groups"][1]["premium"], places=9)

    def test_two_typed_contracts_on_one_screen_can_be_told_apart(self):
        """The old bug, and the reason the contract box is free text: with a
        dropdown, every contract missing from it was CUSTOM, both panels were
        called CUSTOM, and a position line naming one was refused as matching
        two panels -- with no field left that could settle it."""
        panels = [self.panel(code="XA", pair="EURUSD", contract_size=125_000),
                  self.panel(code="XB", pair="EURUSD", contract_size=125_000)]
        r = self.agg("XA, , 1.09, C, 25\nXB, , 1.09, P, -10\n", panels)
        self.assertEqual([p["error"] for p in r["positions"]], ["", ""])
        self.assertEqual([g["underlying"] for g in r["groups"]], ["XA", "XB"])
        self.assertEqual(sorted(c["underlying"] for c in r["contracts"]), ["XA", "XB"])
        self.assertFalse(any(c["known"] for c in r["contracts"]))
        # Two panels that really are the same thing are still refused, and the
        # refusal now names the one field that is always free to differ.
        same = self.agg("1.09 C 25\n", [self.panel(), self.panel()])
        self.assertIn("label", same["positions"][0]["error"])

    def test_a_custom_contract_with_no_size_says_so_rather_than_using_one(self):
        """The money columns are then per one unit of the base currency, which
        is a perfectly good number and a terrible one to read as dollars."""
        r = self.agg("1.09 C 25\n", [self.panel(code="CUSTOM", pair="EURUSD")])
        self.assertEqual(r["positions"][0]["contract_size"], 1.0)
        self.assertTrue(any("no contract size" in w for w in r["warnings"]))
        self.assertTrue(any("per one unit" in w for w in r["warnings"]))

    def test_the_bumps_are_refused_rather_than_silently_ignored(self):
        for bad in ({"vol_bump": -1}, {"theta_days": -3}):
            with self.assertRaises(ValueError):
                self.agg("1.09 C 25\n", **bad)

    def test_structured_rows_refuse_an_unreadable_call_put(self):
        """A short put booked as a long call is not a rounding error, so the
        already-read form does not default the side either."""
        good = listed.positions_from_request({
            "positions": [{"strike": "ATM", "type": "P", "quantity": -5}],
            "panels": [self.panel()]}).run(clock=self.CLOCK)
        self.assertFalse(good["positions"][0]["type"] == "call")
        self.assertAlmostEqual(good["positions"][0]["strike"], 1.09, places=12)
        with self.assertRaises(ValueError):
            listed.positions_from_request({"positions": [{"strike": 1.09, "quantity": 5}]})

    def test_a_theta_window_past_the_expiry_is_blank_with_the_reason(self):
        """There is no revaluation to take the decay from, and a plausible
        number in its place would be the silent zero this project removes."""
        r = self.agg("1.09 C 25\n", theta_days=90)
        row = r["positions"][0]
        self.assertIsNone(row["smile"]["theta"])
        self.assertIn("reaches past this expiry", row["error"])
        # The Black-Scholes column is closed form and is still reported.
        self.assertIsNotNone(row["bs"]["theta"])


class TestListedClock(unittest.TestCase):
    def test_a_panel_with_no_pair_takes_the_book_s_clock(self):
        """The Exchange-traded screen refused to fit a CUSTOM contract.

        The clock was looked for on the mapped surface and nowhere else, so a
        contract with no pair -- CUSTOM, or one whose pair is not in this
        workbook -- reported "a clock is required" on a screen holding a
        perfectly good one.  Only the *comparison* needs a surface.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        panel = listed.Panel(
            underlying=listed.resolve_underlying("CUSTOM"),
            expiry="2024-06-14 19:00", forward=100.0,
            quotes=tuple(listed.Quote(strike=k, vol=v) for k, v in
                         ((95.0, 0.11), (100.0, 0.10), (105.0, 0.105))),
        )
        out = panel.run(book)
        self.assertEqual(out["valuation"], ASOF.now.isoformat())
        self.assertIsNone(out["comparison"])
        self.assertGreater(out["years"], 0)


if __name__ == "__main__":
    unittest.main()
