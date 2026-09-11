"""Pricing, the book, and banded/peg smiles.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestPricing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])

    def test_strike_spec_forms(self):
        self.assertEqual(parse_strike("ATM").kind, "atm")
        self.assertEqual(parse_strike("").kind, "atm")
        self.assertEqual(parse_strike("1.0234").value, 1.0234)
        self.assertEqual((parse_strike("25d").value, parse_strike("25d").is_call), (0.25, True))
        self.assertFalse(parse_strike("10dp").is_call)
        self.assertFalse(parse_strike("-25d").is_call)
        self.assertTrue(parse_strike("25dp").side_explicit)
        self.assertFalse(parse_strike("25d").side_explicit)

    # ---- the marking screen's vol query --------------------------------
    # Two boxes and one number, sharing the pricing screen's strike and
    # expiry vocabulary through `resolve_strike` / `expiry_datetime`.  These
    # pin the sharing: a strike read two ways is a strike that can be read
    # two different ways.

    def test_the_vol_query_reads_the_same_strike_as_a_priced_leg(self):
        """The card and the pricing grid must land on one strike and one vol.

        Both go through `pricing.resolve_strike`; before it there were two
        copies of the same six lines.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        for strike in ("ATM", "25d", "10dp", "151.5"):
            q = quick_vol(book, "USDJPY", "1M", strike)
            leg = price_strip(book, [OptionLeg("USDJPY", "1M", strike)])["legs"][0]
            self.assertTrue(leg["ok"], leg.get("error"))
            self.assertAlmostEqual(q["strike"], leg["strike"], places=10, msg=strike)
            self.assertAlmostEqual(q["vol"], leg["vol"], places=10, msg=strike)
            self.assertAlmostEqual(q["forward"], leg["forward"], places=10, msg=strike)

    def test_the_vol_query_takes_its_forward_from_the_feed(self):
        """There is no third box: the level is `Book.market_level_for`'s.

        ``market_level_for`` and not ``market_level``: the forward is the one
        to this option's own **settlement** date, two business days past its
        expiry, which is the date a forward is a price for.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        q = quick_vol(book, "USDJPY", "1M", "ATM")
        level = book.market_level_for("USDJPY", date.fromisoformat(q["expiry"]))
        self.assertEqual(q["settle"], level["settle"])
        self.assertTrue(q["scaled"])
        self.assertEqual(q["forward_source"], "feed")
        self.assertAlmostEqual(q["forward"], level["forward"], places=12)
        self.assertAlmostEqual(q["spot"], level["spot"], places=12)

    def test_the_vol_query_answers_in_moneyness_with_no_feed(self):
        """ATM and a delta are moneyness questions and need no level at all.

        Same rule as the smile chart's axis: without a feed it stays in K/F
        and says so, rather than refusing a question it can answer.
        """
        for strike in ("ATM", "25d"):
            q = quick_vol(self.book, "USDJPY", "1M", strike)
            self.assertFalse(q["scaled"], strike)
            self.assertIsNone(q["strike"], strike)
            self.assertIsNone(q["forward"], strike)
            self.assertEqual(q["forward_source"], "none")
            self.assertAlmostEqual(
                q["vol"],
                float(self.book["USDJPY"].vol(q["strike_ratio"],
                                              expiry_datetime(self.book, "USDJPY", "1M"))) * 100,
                places=12)

    def test_an_absolute_strike_with_no_feed_is_refused_by_name(self):
        """The marks are in K/F, so 151.5 cannot be placed against them.

        Read as a ratio it would be a wing nobody asked about, silently.
        """
        with self.assertRaises(ValueError) as ctx:
            quick_vol(self.book, "USDJPY", "1M", "151.5")
        self.assertIn("the feed does not quote USDJPY", str(ctx.exception))

    def test_the_reported_strike_is_the_one_the_vol_was_read_at(self):
        """The card keeps the request in its box and reports the resolution
        under it, so the two must agree: asking again at the strike it named
        is the same read, and the date it named resolves to itself."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        asked = quick_vol(book, "USDJPY", "1M", "25d")
        again = quick_vol(book, "USDJPY", "1M", repr(asked["strike"]))
        self.assertAlmostEqual(asked["vol"], again["vol"], places=12)
        self.assertEqual(quick_vol(book, "USDJPY", asked["expiry"], "ATM")["expiry"],
                         asked["expiry"])

    def test_the_strike_box_is_the_only_place_the_wing_is_said(self):
        """A bare `25d` names two strikes and is read on the call, as on the
        pricing screen; `25dp` and `-25d` are how the other one is asked for.

        The card briefly had a wing toggle beside the strike box.  It was a
        second place to say one thing -- and a place that could be set to Call
        against a strike that already said put -- so the strike text is the
        whole of it.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        call = quick_vol(book, "USDJPY", "1M", "25d")
        self.assertIs(call["is_call"], True)
        self.assertFalse(call["side_explicit"])
        for text in ("25dp", "-25d"):
            put = quick_vol(book, "USDJPY", "1M", text)
            self.assertIs(put["is_call"], False, text)
            self.assertTrue(put["side_explicit"], text)
            self.assertLess(put["strike"], call["strike"], text)
            self.assertNotAlmostEqual(call["vol"], put["vol"], places=6, msg=text)

    def test_no_side_is_reported_where_there_are_not_two_strikes(self):
        """At the at-the-money and at an absolute strike the volatility is one
        number for the call and the put, so the row names no wing at all."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        for text in ("ATM", "151.5"):
            self.assertIsNone(quick_vol(book, "USDJPY", "1M", text)["is_call"], text)

    def test_the_vol_query_reports_the_delta_of_the_strike_it_read(self):
        """The card takes a strike or a delta and answers with both.

        A strike and a delta name one point on the smile; the desk has
        whichever of the two the market gave it, so the card must be able to
        be asked either way and report the other.  The round trip is the
        pin: the delta reported at a 25-delta request is 25, and asking again
        at the strike that came back reports the same delta.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        asked = quick_vol(book, "USDJPY", "1M", "25d")
        self.assertAlmostEqual(asked["delta"], 25.0, places=6)
        self.assertIs(asked["delta_is_call"], True)
        back = quick_vol(book, "USDJPY", "1M", repr(asked["strike"]))
        self.assertAlmostEqual(back["delta"], asked["delta"], places=6)
        put = quick_vol(book, "USDJPY", "1M", "10dp")
        self.assertAlmostEqual(put["delta"], -10.0, places=6)
        self.assertIs(put["delta_is_call"], False)

    def test_the_feeds_ois_rows_make_the_deltas_spot_and_the_premium_paid(self):
        """With a USD OIS curve a 1Y USDJPY 25-delta quote lands on a lower strike.

        Spot delta is forward delta times the USD discount factor, so the 25
        the market quotes is a 26-and-a-bit forward delta and the strike is
        nearer the money.  The premium as paid is the forward premium at the
        JPY discount factor -- and *nobody types a JPY rate*: it is implied
        from the USD curve and the USDJPY forward in the same file, so the two
        factors a pair is priced with reproduce that forward exactly instead
        of disagreeing with it by the basis.  A currency the file cannot reach
        stays a forward delta and an undiscounted premium, said in so many
        words.
        """
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)

        feed = MarketFeed.load(FEED)
        self.assertEqual(feed.problems, [])
        self.assertEqual(sorted(feed.ois), ["JPY", "USD"])
        self.assertAlmostEqual(feed.ois["USD"].rate(1.0), 0.0400, places=12)
        # linear in years between the pillars listed, flat outside them
        self.assertAlmostEqual(feed.ois["USD"].rate(0.375),
                               0.0425 + (0.0415 - 0.0425) * 0.5, places=12)
        self.assertAlmostEqual(feed.ois["USD"].rate(30.0), 0.0380, places=12)
        # annually compounded, which is how an OIS is quoted
        self.assertAlmostEqual(feed.ois["USD"].df(1.0), 1 / 1.04, places=12)

        book = Book.from_excel(BOOK, ASOF)
        book.feed = feed
        book.load_all(["USDJPY", "AUDUSD"])
        disc = book.discount
        self.assertTrue(disc.anchored)
        self.assertEqual(disc.factor("USD", 1.0)[1], "USDOIS")
        self.assertIn("implied from USDOIS and the USDJPY forward",
                      disc.factor("JPY", 1.0)[1])
        self.assertIn("implied from USDOIS and the EURUSD forward",
                      disc.factor("EUR", 1.0)[1])
        # The identity, which is the whole reason the rate comes off the feed:
        # F = S x DF_base / DF_term, on a quoted pair and on a cross the file
        # does not quote and builds from its legs.
        for pair in ("USDJPY", "EURUSD", "AUDUSD", "EURJPY"):
            level = feed.level(pair, 1.0)
            self.assertAlmostEqual(
                level["spot"] * disc.df(pair[:3], 1.0) / disc.df(pair[3:6], 1.0),
                level["forward"], places=10, msg=pair)
        # A stated non-anchor curve is read for the basis and never to
        # discount: JPY still discounts off the forward.
        self.assertAlmostEqual(
            disc.basis("JPY", 1.0),
            feed.ois["JPY"].rate(1.0) - discount.implied_rate(disc.df("JPY", 1.0), 1.0),
            places=12)
        self.assertEqual([r["currency"] for r in disc.basis_report(1.0)], ["JPY"])
        # A currency the file does not quote against the dollar has no factor,
        # and says which pair it went looking for.
        self.assertIsNone(disc.df("CHF", 1.0))
        self.assertIn("does not quote USDCHF", disc.factor("CHF", 1.0)[1])

        with_rate = quick_vol(book, "USDJPY", "1Y", "25d")
        without = quick_vol(self.book, "USDJPY", "1Y", "25d")
        self.assertEqual(with_rate["delta_kind"], "spot delta")
        self.assertTrue(without["delta_kind"].startswith(
            "forward delta (no USD discount factor"))
        self.assertLess(with_rate["strike_ratio"], without["strike_ratio"])
        self.assertAlmostEqual(with_rate["delta"], 25.0, places=6)
        # forward delta beyond the boundary, curve or no curve
        self.assertEqual(quick_vol(book, "USDJPY", "2Y", "25d")["delta_kind"],
                         "forward delta")
        # a 1M is barely moved: the factor is a month of USD rate
        near = quick_vol(book, "USDJPY", "1M", "25d")["strike_ratio"]
        near0 = quick_vol(self.book, "USDJPY", "1M", "25d")["strike_ratio"]
        self.assertLess(abs(near - near0), abs(with_rate["strike_ratio"] - without["strike_ratio"]))
        # the premium as paid, at the *implied* JPY factor
        r = price_strip(book, [OptionLeg(pair="USDJPY", expiry="1Y", strike="ATM",
                                         notional=10, direction=1)])["legs"][0]
        self.assertTrue(r["discounted"])
        self.assertAlmostEqual(r["df_domestic"], book.discount_factor("JPY", r["t"]),
                               places=12)
        self.assertAlmostEqual(r["premium_pv_dom"], r["premium_dom"] * r["df_domestic"])
        self.assertAlmostEqual(r["pv_amount"], r["premium_amount"] * r["df_domestic"])
        self.assertEqual(r["delta_kind"], "spot delta")
        out0 = price_strip(self.book, [OptionLeg(pair="USDJPY", expiry="1Y", strike="ATM",
                                                 notional=10, direction=1)])
        r0 = out0["legs"][0]
        self.assertFalse(r0["discounted"])
        self.assertIsNone(r0["premium_pv_dom"])
        # the pair total follows: a sum of paid premiums, or None if a leg has none
        out = price_strip(book, [OptionLeg(pair="USDJPY", expiry="1Y", strike="ATM",
                                           notional=10, direction=1),
                                 OptionLeg(pair="USDJPY", expiry="3M", strike="25d",
                                           notional=5, direction=-1)])
        legs = out["legs"]
        self.assertAlmostEqual(out["totals"]["USDJPY"]["pv_premium"],
                               legs[0]["pv_amount"] + legs[1]["pv_amount"])
        self.assertAlmostEqual(out["totals"]["USDJPY"]["premium"],
                               legs[0]["premium_amount"] + legs[1]["premium_amount"])
        self.assertIsNone(out0["totals"]["USDJPY"]["pv_premium"])

        # A base currency the anchor cannot reach: the warning is at load,
        # where somebody is watching, and names what it costs.
        thin = d / "no_aud.csv"
        rows = FEED.read_text(encoding="utf-8").splitlines()
        thin.write_text("\n".join(line for line in rows
                                   if not line.startswith("AUDUSD")) + "\n",
                        encoding="utf-8")
        lean = Book.from_excel(BOOK, ASOF)
        lean.feed = MarketFeed.load(thin)
        lean.load_all(["USDJPY", "AUDUSD"])
        self.assertTrue(any(w.startswith("AUDUSD: quotes spot delta but the feed "
                                         "cannot discount AUD") for w in lean.warnings))
        self.assertFalse(any(w.startswith("USDJPY: quotes spot delta")
                             for w in lean.warnings))
        # And asked again once the feed is on, because every command that takes
        # --feed loads it *after* the build: one line per currency, not per pair.
        late = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "AUDUSD"])
        self.assertEqual(late.discount_warnings(), [])       # no feed, nothing to say
        late.feed = MarketFeed.load(thin)
        said = late.discount_warnings()
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("AUD: the feed does not quote AUDUSD"))
        self.assertIn("the quoted deltas on AUDUSD are read as forward deltas", said[0])
        late.feed = feed
        self.assertEqual(late.discount_warnings(), [])

        # A row that cannot be read is named by its line, and does not take
        # the rest of the file with it.
        bad = d / "bad.csv"
        bad.write_text("USDJPY,SPOT,150.0\nUSDOIS,1x,5\nEUROIS,1M,zero\n"
                       "JPYOIS,1M,400\nUSDOIS,7D,4.3\nUSDOIS,1W,4.4\n",
                       encoding="utf-8")
        broken = MarketFeed.load(bad)
        joined = " | ".join(broken.problems)
        self.assertIn("line 2: USDOIS '1x' is not a tenor", joined)
        self.assertIn("line 3: EUROIS 1M rate 'zero' is not a number", joined)
        self.assertIn("is not a percentage per annum", joined)
        self.assertIn("USDOIS quotes 7D and 1W, which are the same tenor", joined)
        self.assertEqual(sorted(broken.ois), ["USD"])
        self.assertEqual(sorted(broken.pairs), ["USDJPY"])

    def test_a_csa_moves_the_premium_and_nothing_else(self):
        """Collateral changes what a cashflow is worth, not what a hedge is.

        Discounting a JPY premium at the FX-implied factor *is* a USD CSA --
        convert at the forward, discount at USD OIS, convert back at spot --
        so the default is not a convention with no name and the blank stays
        the number it always was.  A JPY CSA discounts the same premium on
        JPY OIS instead, and the two differ by exactly the basis.  The delta,
        the strike and the volatility must not move for any of it: a delta is
        a hedge ratio, and the spot delta the market quotes is defined off
        ``F = S x DF_base/DF_term``, which is the *forward's* factor and not
        the collateral's.
        """
        feed = MarketFeed.load(FEED)
        book = Book.from_excel(BOOK, ASOF)
        book.feed = feed
        book.load_all(["USDJPY"])

        def leg(csa):
            return price_strip(book, [OptionLeg(pair="USDJPY", expiry="1Y", strike="ATM",
                                                notional=10, direction=1,
                                                csa=csa)])["legs"][0]

        base, usd, jpy, eur = leg(""), leg("USD"), leg("JPY"), leg("EUR")
        # the anchor's CSA is the implied factor, and there is one arithmetic
        self.assertEqual(usd["df_domestic"], base["df_domestic"])
        self.assertEqual(usd["csa_source"], base["csa_source"])
        self.assertAlmostEqual(base["df_domestic"],
                               book.discount_factor("JPY", base["t"]), places=12)
        # the term currency's own CSA is its own curve, used directly -- the
        # one place a stated non-anchor curve discounts anything
        self.assertAlmostEqual(jpy["df_domestic"], feed.ois["JPY"].df(jpy["t"]), places=12)
        self.assertEqual(jpy["csa_source"], "JPYOIS, on a JPY CSA")
        self.assertNotAlmostEqual(jpy["df_domestic"], base["df_domestic"], places=6)
        # and the gap between them is the basis, which is the whole claim
        t = base["t"]
        self.assertAlmostEqual(
            discount.implied_rate(jpy["df_domestic"], t)
            - discount.implied_rate(base["df_domestic"], t),
            book.discount.basis("JPY", t), places=12)
        # a collateral currency the feed cannot price falls back and says so
        self.assertEqual(eur["df_domestic"], base["df_domestic"])
        self.assertIn("no EUROIS rows", eur["csa_source"])
        self.assertIn("falls back", eur["csa_source"])

        # Nothing else moves.  This is the point of keeping ``csa_df`` apart
        # from ``df``: put the collateral curve in ``df_foreign`` and every
        # 25-delta wing lands somewhere nobody quoted.
        for other in (usd, jpy, eur):
            for key in ("strike", "vol", "atm_vol", "delta_pct", "smile_delta_pct",
                        "premium_dom", "forward", "delta_kind"):
                self.assertEqual(other[key], base[key], f"{key} moved with the CSA")
        # the premium as paid is the only number that follows
        self.assertAlmostEqual(jpy["pv_amount"], jpy["premium_amount"] * jpy["df_domestic"])
        self.assertGreater(abs(jpy["pv_amount"] - base["pv_amount"]), 0.0)

        # The forward identity survives inside a CSA: whatever the collateral,
        # the two factors a pair is priced with still reproduce its forward.
        disc = book.discount
        level = feed.level("USDJPY", 1.0)
        for collateral in ("", "USD", "JPY"):
            b, _ = disc.csa_df("USD", 1.0, collateral)
            q, _ = disc.csa_df("JPY", 1.0, collateral)
            self.assertAlmostEqual(level["spot"] * b / q, level["forward"], places=10)
        # a third currency's CSA reaches a pair through the cross it quotes
        df, why = disc.csa_df("JPY", 1.0, "JPY")
        self.assertIn("JPYOIS", why)
        df2, why2 = disc.csa_df("EUR", 1.0, "JPY")
        self.assertIn("EURJPY", why2)
        self.assertAlmostEqual(df2, df * (feed.level("EURJPY", 1.0)["forward"]
                                          / feed.level("EURJPY", 1.0)["spot"]), places=12)

    def test_the_three_spellings_of_the_money_and_the_long_dated_atm(self):
        """``ATM`` is the convention, ``ATMF`` the forward, ``DNS`` the straddle.

        On a 2Y the convention *is* the forward, so ATM and ATMF agree there
        and DNS is the one that differs; on a 3M it is the other way round.
        """
        from volkit.black import atm_strike
        for text, kind, label in (("atm", "convention", "ATM"), ("ATMF", "forward", "ATMF"),
                                  ("dns", "straddle", "DNS"), ("50d", "straddle", "DNS")):
            spec = parse_strike(text)
            self.assertEqual((spec.kind, spec.atm_kind, spec.text), ("atm", kind, label), text)
        short = {k: quick_vol(self.book, "USDJPY", "3M", k)["strike_ratio"]
                 for k in ("ATM", "ATMF", "DNS")}
        self.assertEqual(short["ATMF"], 1.0)
        self.assertLess(short["DNS"], 1.0)               # premium adjusted: below F
        self.assertEqual(short["ATM"], short["DNS"])
        long = {k: quick_vol(self.book, "USDJPY", "2Y", k)["strike_ratio"]
                for k in ("ATM", "ATMF", "DNS")}
        self.assertEqual(long["ATM"], 1.0)
        self.assertEqual(long["ATMF"], 1.0)
        self.assertLess(long["DNS"], 1.0)
        r = quick_vol(self.book, "USDJPY", "2Y", "ATM")
        self.assertEqual(r["atm_kind"], "ATMF")
        self.assertEqual(quick_vol(self.book, "USDJPY", "1Y", "ATM")["atm_kind"], "DNS")
        self.assertEqual(r["delta_kind"], "forward delta")
        self.assertIn("premium adjusted", r["convention"])
        # and the smile's own ATM anchor is at the forward there, so the
        # quoted ATM vol is read at the strike the market quotes it for
        surface = self.book["USDJPY"]
        sl = surface.slice_at(self.book.clock.datetime_from_years(2.0))
        self.assertEqual(float(sl.strikes[2]), 1.0)
        self.assertEqual(float(atm_strike(1.0, sl.atm_vol, sl.t, surface.conv)), 1.0)
        row = [r for r in surface.smile_table(self.book.clock.datetime_from_years(2.0))
               if r["label"] in ("ATM", "ATMF")][0]
        self.assertEqual(row["label"], "ATMF")
        self.assertNotAlmostEqual(row["delta"], 0.5, places=3)   # the forward is not 50 delta

    def test_the_delta_is_reported_under_the_pairs_own_convention(self):
        """Premium adjusted for a USD-base pair, unadjusted otherwise.

        The browser has no business knowing which, so the answer says: a
        premium-adjusted delta-neutral straddle is not at 50 delta and a page
        that assumed it was would report a strike nobody asked about.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY", "EURUSD"])
        book.feed = MarketFeed.load(FEED)
        usd = quick_vol(book, "USDJPY", "1M", "ATM")
        eur = quick_vol(book, "EURUSD", "1M", "ATM")
        self.assertTrue(usd["premium_adjusted"])
        self.assertFalse(eur["premium_adjusted"])
        self.assertLess(usd["delta"], 50.0)
        # Unadjusted, the delta-neutral straddle *is* the 50-delta strike --
        # in the forward delta it is defined in.  The feed now carries the
        # discount curve too, so what a EURUSD delta box shows is the *spot*
        # delta at that strike, which is 50 times the EUR discount factor: a
        # tenth of a delta at a month, and the number is exact rather than
        # nearly-50.  (The old ``assertGreater(…, 50.0)`` was passing on 1e-13
        # of floating-point noise and flipped sign when the weights moved.)
        self.assertEqual(eur["delta_kind"], "spot delta")
        self.assertAlmostEqual(eur["delta"],
                               50.0 * book.discount_factor("EUR", eur["t"]), places=9)
        self.assertLess(eur["delta"], 50.0)

    def test_the_delta_comes_back_for_a_pair_with_no_feed(self):
        """Delta is a function of moneyness, so it needs no level at all.

        The strike cannot be placed without a feed and comes back as None;
        the delta beside it must not disappear with it, because a delta is
        exactly what a marker asks for when there is no level to hand.
        """
        q = quick_vol(self.book, "USDJPY", "1M", "25d")
        self.assertFalse(q["scaled"])
        self.assertIsNone(q["strike"])
        self.assertAlmostEqual(q["delta"], 25.0, places=6)

    def test_the_vol_query_flags_a_strike_outside_a_managed_band(self):
        """Same rule as a pricing leg: the level the payout depends on is
        checked, and a lognormal wing outside a defended band says so."""
        book = Book.from_excel(BOOK, ASOF).load_all(["USDHKD"])
        book.feed = MarketFeed.load(FEED)
        if "USDHKD" not in book.banded_pairs():
            self.skipTest("no USDHKD band on the PEG_BANDS tab")
        band = book["USDHKD"].band
        q = quick_vol(book, "USDHKD", "1M", str(band.upper * 1.02), forward=band.upper)
        self.assertTrue(any("outside the managed band" in w for w in q["warnings"]), q["warnings"])

    def test_bad_strike_spec_raises(self):
        for bad in ("xyz", "60d", "0d"):
            with self.assertRaises(ValueError, msg=bad):
                parse_strike(bad)

    def test_bare_delta_takes_its_wing_from_the_option_type(self):
        """'25d' with type P must resolve the put strike, not the call strike."""
        call = price_strip(self.book, [OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25)])["legs"][0]
        put = price_strip(self.book, [OptionLeg("USDJPY", "1M", "25d", "P", spot=150.25)])["legs"][0]
        self.assertAlmostEqual(call["delta_pct"], 25.0, places=6)
        self.assertAlmostEqual(put["delta_pct"], -25.0, places=6)
        self.assertLess(put["strike"], call["strike"])

    def test_explicit_and_signed_delta_agree(self):
        legs = [OptionLeg("USDJPY", "1M", s, "P", spot=150.25) for s in ("25dp", "-25d")]
        a, b = price_strip(self.book, legs)["legs"]
        self.assertAlmostEqual(a["strike"], b["strike"], places=12)

    def test_tenor_expiry_resolves_on_the_calendar(self):
        d = resolve_expiry(self.book, "USDJPY", "1M")
        self.assertTrue(self.book.calendars.is_business_day("USDJPY", d))
        self.assertEqual(resolve_expiry(self.book, "USDJPY", "2024-05-28"), date(2024, 5, 28))

    def test_forward_points_applied_with_the_pip_divisor(self):
        leg = OptionLeg("USDJPY", "1M", "ATM", spot=150.25, forward_points=-45, pip=100)
        r = price_strip(self.book, [leg])["legs"][0]
        self.assertAlmostEqual(r["forward"], 150.25 - 0.45, places=10)

    def test_one_bad_leg_does_not_break_the_strip(self):
        legs = [
            OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25, label="good"),
            OptionLeg("USDJPY", "not-a-tenor", "25d", "C", spot=150.25, label="bad"),
            OptionLeg("EURUSD", "3M", "ATM", "C", spot=1.0842, label="also good"),
        ]
        out = price_strip(self.book, legs)
        self.assertEqual(out["errors"], 1)
        self.assertTrue(out["legs"][0]["ok"])
        self.assertFalse(out["legs"][1]["ok"])
        self.assertTrue(out["legs"][2]["ok"])
        self.assertIn("not-a-tenor", out["legs"][1]["error"])

    def test_unknown_pair_is_reported_per_leg(self):
        out = price_strip(self.book, [OptionLeg("XXXYYY", "1M", "ATM", spot=1.0)])
        self.assertFalse(out["legs"][0]["ok"])

    def test_put_call_parity_across_two_legs(self):
        legs = [OptionLeg("USDJPY", "3M", "150.0", t, spot=150.25, forward_points=-140)
                for t in ("C", "P")]
        c, p = price_strip(self.book, legs)["legs"]
        self.assertAlmostEqual(c["premium_dom"] - p["premium_dom"],
                               c["forward"] - c["strike"], places=10)

    def test_direction_flips_the_signed_amounts(self):
        base = OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25, notional=10)
        sold = OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25, notional=10, direction=-1)
        b, s = price_strip(self.book, [base, sold])["legs"]
        self.assertAlmostEqual(b["premium_amount"], -s["premium_amount"], places=12)
        self.assertAlmostEqual(b["vega_amount"], -s["vega_amount"], places=12)

    def test_totals_bucket_by_pair(self):
        legs = [
            OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25, notional=10),
            OptionLeg("USDJPY", "1M", "25d", "P", spot=150.25, notional=10, direction=-1),
            OptionLeg("EURUSD", "3M", "ATM", "C", spot=1.0842, notional=20),
        ]
        out = price_strip(self.book, legs)
        self.assertEqual(set(out["totals"]), {"USDJPY", "EURUSD"})
        jpy = [l for l in out["legs"] if l["pair"] == "USDJPY"]
        self.assertAlmostEqual(out["totals"]["USDJPY"]["premium"],
                               sum(l["premium_amount"] for l in jpy), places=10)

    def test_premium_scales_with_notional(self):
        one = price_strip(self.book, [OptionLeg("USDJPY", "1M", "ATM", "C", spot=150.25, notional=1)])
        ten = price_strip(self.book, [OptionLeg("USDJPY", "1M", "ATM", "C", spot=150.25, notional=10)])
        self.assertAlmostEqual(ten["legs"][0]["premium_amount"],
                               10 * one["legs"][0]["premium_amount"], places=10)


class TestBook(unittest.TestCase):
    def test_build_order_puts_legs_first(self):
        book = Book.from_excel(BOOK, ASOF)
        order = book.build_order()
        self.assertLess(order.index("AUDUSD"), order.index("AUDJPY"))
        self.assertLess(order.index("USDJPY"), order.index("AUDJPY"))

    def test_requesting_a_cross_pulls_in_its_legs(self):
        book = Book.from_excel(BOOK, ASOF).build(["AUDJPY"])
        self.assertIn("AUDUSD", book.surfaces)
        self.assertIn("USDJPY", book.surfaces)

    def test_past_events_are_reported_not_silently_used(self):
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        self.assertTrue(any("before the valuation time" in w for w in book.warnings))

    def test_tenor_points_available_without_crosses(self):
        """Legacy set self.tenor_points inside the cross loop only."""
        book = Book.from_excel(BOOK, ASOF)
        self.assertTrue(book.data.tenor_points)

    def test_unknown_pair_raises_with_a_useful_message(self):
        """And with the *right* list.

        ``build``/``load_all`` may be narrowed to a few pairs -- ``volkit band
        USDHKD`` narrows them to one -- so when the asked-for pair is not in
        the workbook nothing is built and "available: []" read as an empty
        workbook rather than as a pair the workbook does not carry.  A pair
        the workbook has never heard of is told what the workbook holds; a
        pair it has, but which this book was not asked to build, is told that
        instead.
        """
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        with self.assertRaises(KeyError) as ctx:
            book["NOPE"]
        message = str(ctx.exception)
        self.assertIn("is not in", message)
        self.assertIn("USDJPY", message)          # what the workbook does hold
        self.assertIn("EURUSD", message)

        narrowed = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        with self.assertRaises(KeyError) as ctx:
            narrowed["EURGBP"]
        message = str(ctx.exception)
        self.assertIn("is in the workbook but is not built", message)
        self.assertIn("USDJPY", message)


class TestBandedSmile(unittest.TestCase):
    def setUp(self):
        self.band = Band("USDHKD", 7.75, 7.85)
        self.F, self.t, self.atm = 7.8020, 0.25, 0.004424

    def _fit(self, t=None, **kw):
        return calibrate_band_smile(self.band, self.F, t or self.t, self.atm,
                                    conv=DeltaConvention(True), **kw)

    def test_band_validation(self):
        for lo, hi in ((8.0, 7.0), (0.0, 0.0)):
            with self.assertRaises(ValueError):
                Band("X", lo, hi)

    def test_forward_outside_the_band_is_rejected(self):
        with self.assertRaises(ValueError):
            calibrate_band_smile(self.band, 7.90, self.t, self.atm)

    def test_atm_and_forward_are_matched_exactly(self):
        smile, rep = self._fit()
        self.assertTrue(rep["converged"], rep)
        self.assertAlmostEqual(smile.mean, self.F, places=9)
        self.assertLess(abs(rep["atm_residual_vol"]), 1e-8)

    def test_breach_probability_is_positive_and_real(self):
        """The peg can break; that probability belongs in the price.  It is an
        output of the hazard, not something forced to zero."""
        _, rep = self._fit(jump=JumpSpec(hazard=0.02))
        self.assertGreater(rep["prob_outside_band"], 0.0)
        self.assertLess(rep["prob_outside_band"], rep["prob_broken"])

    def test_breach_probability_compounds_with_the_hazard(self):
        """A hazard rate composes across the term structure; a per-tenor
        probability would not."""
        probs = []
        for t in (1 / 12, 0.25, 0.5, 1.0):
            _, rep = self._fit(t=t, jump=JumpSpec(hazard=0.02))
            probs.append(rep["prob_broken"])
            self.assertAlmostEqual(rep["prob_broken"], 1 - math.exp(-0.02 * t), places=12)
        self.assertTrue(all(a < b for a, b in zip(probs, probs[1:])))

    def test_analytic_breach_matches_monte_carlo(self):
        smile, rep = self._fit(jump=JumpSpec(hazard=0.05))
        rng = np.random.default_rng(4)
        hold, pw, ps = smile.weights
        mw, ms = smile.break_levels
        n = 400_000
        outside = 0.0
        for p, level, vol in ((pw, mw, smile.jump.weak_vol), (ps, ms, smile.jump.strong_vol)):
            sq = vol * math.sqrt(self.t)
            S = level * np.exp(-0.5 * sq * sq + sq * rng.standard_normal(n))
            outside += p * float(((S < self.band.lower) | (S > self.band.upper)).mean())
        self.assertAlmostEqual(rep["prob_outside_band"], outside, places=4)

    def test_options_outside_the_band_carry_jump_value(self):
        smile, _ = self._fit(jump=JumpSpec(hazard=0.02))
        prices = [float(smile.call_price(K)) for K in (7.86, 7.90, 8.00, 8.50)]
        self.assertTrue(all(p > 0 for p in prices), prices)
        self.assertTrue(all(a > b for a, b in zip(prices, prices[1:])))

    def test_expected_devaluation_shifts_the_peg_intact_distribution(self):
        """With an asymmetric break the forward constraint pushes the in-band
        mean to the strong side; that is a real effect, not an artefact."""
        _, rep = self._fit(jump=JumpSpec(hazard=0.01, weak_share=1.0, weak_jump=0.05))
        self.assertLess(rep["in_band_mean_shift"], 0.0)
        _, flat = self._fit(jump=JumpSpec(hazard=0.0))
        self.assertAlmostEqual(flat["in_band_mean_shift"], 0.0, places=9)

    def test_beta_can_represent_a_u_shape(self):
        """A logit-normal or lognormal cannot; the realised peg distribution is
        U-shaped because the authority defends the edges."""
        u = BetaBandSmile(self.band, 0.6, 0.6, self.t, self.F, JumpSpec(hazard=0.0))
        self.assertTrue(u.u_shaped)
        d = u.density(np.array([7.755, 7.80, 7.845]))
        self.assertGreater(d[0], d[1])
        self.assertGreater(d[2], d[1])

    def test_call_put_parity_holds_against_the_model_forward(self):
        smile, _ = self._fit(jump=JumpSpec(hazard=0.02))
        for K in (7.60, 7.76, 7.80, 7.84, 8.10):
            c, p = float(smile.call_price(K)), float(smile.put_price(K))
            self.assertAlmostEqual(c - p, smile.mean - K, places=12)

    def test_implied_vol_reprices_the_model_everywhere(self):
        smile, _ = self._fit(jump=JumpSpec(hazard=0.02))
        for K in (7.70, 7.77, 7.80, 7.83, 7.95):
            v = smile.implied_vol(K)
            self.assertTrue(np.isfinite(v), K)
            is_call = K >= smile.mean
            ref = float(smile.call_price(K)) if is_call else float(smile.put_price(K))
            self.assertAlmostEqual(float(black.price(smile.mean, K, v, self.t, is_call)),
                                   ref, places=10)

    def test_density_integrates_to_one_including_the_tails(self):
        smile, _ = self._fit(jump=JumpSpec(hazard=0.05))
        g = np.linspace(4.0, 16.0, 300_001)
        self.assertAlmostEqual(float(np.trapezoid(smile.density(g), g)), 1.0, places=4)

    def test_atm_floor_from_the_hazard_is_diagnosed(self):
        """Break risk alone sets a floor under the ATM volatility.  A quote
        below it is not a solver failure but a statement about the marks."""
        with self.assertRaises(ConvergenceError) as ctx:
            self._fit(jump=JumpSpec(hazard=0.05, weak_share=1.0, weak_jump=0.08))
        self.assertIn("inconsistent", str(ctx.exception))
        self.assertIn("at least", str(ctx.exception))

    def test_forward_constraint_failure_is_diagnosed_separately(self):
        """Enough expected devaluation and the peg-intact mean would have to sit
        outside the band for the forward to match at all."""
        with self.assertRaises(ValueError) as ctx:
            self._fit(t=5.0, jump=JumpSpec(hazard=0.05))
        self.assertIn("outside the band", str(ctx.exception))

    def test_hazard_inversion_responds_to_the_assumed_jump(self):
        """Backing the hazard out of the wings must actually move with the
        assumption, and say so when it cannot be done."""
        got = []
        for wj, sv in ((0.03, 0.06), (0.06, 0.10), (0.12, 0.18)):
            _, rep = self._fit(risk_reversal=0.0022, strangle=0.0014, solve_hazard=True,
                               jump=JumpSpec(hazard=0.02, weak_jump=wj,
                                             strong_jump=wj * 0.7, weak_vol=sv,
                                             strong_vol=sv * 0.8))
            got.append(rep["hazard"])
        self.assertTrue(all(a > b for a, b in zip(got, got[1:])), got)
        self.assertGreater(got[0] / got[-1], 2.0)

    def test_jump_spec_validation(self):
        for kw in ({"hazard": -1.0}, {"weak_share": 1.5}, {"weak_vol": 0.0}):
            with self.assertRaises(ValueError):
                JumpSpec(**kw)

    def test_the_peg_bands_tab_loads_and_rejects_degenerate_rows(self):
        bands = load_bands(Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx")
        self.assertIn("USDHKD", bands)
        self.assertEqual(bands["USDHKD"].lower, 7.75)

    def test_a_workbook_with_no_peg_bands_tab_says_so_by_name(self):
        """The tab is asked for by name, so its absence is named, not shrugged at."""
        legacy = Path(__file__).resolve().parents[1] / "files" / "vol_marks_legacy_format.xlsx"
        with self.assertRaises(ValueError) as ctx:
            load_bands(legacy)
        self.assertIn("PEG_BANDS", str(ctx.exception))


class TestBandGuard(unittest.TestCase):
    def test_surface_flags_a_strike_outside_the_band(self):
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        self.assertEqual(surface.band_check(7.90, 7.80), [])   # no band set
        surface.band = Band("USDHKD", 7.75, 7.85)
        self.assertEqual(surface.band_check(7.80, 7.80), [])
        warn = surface.band_check(7.90, 7.80)
        self.assertTrue(warn)
        self.assertIn("managed band", warn[0])

    def test_the_treatment_decides_whether_the_warning_is_said_at_all(self):
        """'off' is a marking, not an oversight, and a BAND price already has
        the peg in it."""
        from volkit.banded import BandTreatment
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        surface.band = Band("USDHKD", 7.75, 7.85)
        self.assertTrue(surface.band_check(7.90, 7.80))
        surface.set_band_treatment(BandTreatment(mode="off"))
        self.assertEqual(surface.band_check(7.90, 7.80), [])
        surface.set_band_treatment(BandTreatment(mode="mixture"))
        self.assertEqual(surface.band_check(7.90, 7.80, method="BAND"), [])
        # Marked as a mixture but priced lognormally: that is worth saying.
        self.assertIn("switch the method to BAND", surface.band_check(7.90, 7.80, "SVI")[0])

    def test_a_leg_is_checked_at_the_level_its_payout_depends_on(self):
        """A vanilla struck outside a managed band used to go unflagged.

        The check only ever looked at ``leg.barrier``, so the one product a
        band matters most for -- an option struck out at the edge -- was the
        one product nothing was said about, while a barrier left behind on a
        leg that had since been switched to a vanilla was checked instead.
        The level checked is now the one the payout actually depends on.
        """
        from volkit.pricing import OptionLeg, price_leg
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book["EURUSD"].band = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")

        def warnings(*, method="SVI", **kw):
            leg = OptionLeg(pair="EURUSD", expiry="2024-05-28", spot=1.0842,
                            method=method, cut="TK", **kw)
            out = price_leg(book, leg)
            self.assertTrue(out.ok, out.error)
            return [w for w in out.warnings if "managed band" in w]

        self.assertTrue(warnings(strike="1.3000"))          # outside: said
        self.assertEqual(warnings(strike="1.0900"), [])     # inside: nothing
        # A barrier left on a leg whose product no longer uses one is not read.
        self.assertEqual(warnings(strike="1.0900", barrier="1.3000"), [])
        # The touch products are checked at their barrier, as before.
        self.assertTrue(warnings(product="one_touch", barrier="1.3000"))
        # A leg already priced with BAND is not told to switch to it.
        book["EURUSD"].forward_lookup = lambda t: 1.0859825226390687
        self.assertEqual(warnings(strike="1.3000", method="BAND"), [])

    def test_the_band_edges_can_be_overridden_on_the_screen(self):
        from volkit.banded import BandTreatment
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        surface.band = Band("USDHKD", 7.75, 7.85)
        surface.set_band_treatment(BandTreatment(upper=7.90))
        self.assertEqual(surface.band_check(7.88, 7.80), [])
        self.assertIn("7.9", surface.band_check(7.95, 7.80)[0])


class TestBandInterpolation(unittest.TestCase):
    """The BAND method: the regime mixture priced through the surface.

    The plumbing this pins is the piece the band model was missing.  A band is
    an absolute price range and the surface works in strike over forward, so
    a slice read in moneyness has to divide the band by an outright forward --
    and refuse, rather than guess a level, when there is no feed to divide by.
    """

    BAND = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")
    EXPIRY = datetime(2024, 5, 28, tzinfo=UTC)

    def surface(self, *, feed=True):
        from volkit.feed import MarketFeed
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        if feed:
            book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = self.BAND
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        return book, surface

    def test_the_band_is_divided_into_the_surfaces_own_space(self):
        _, surface = self.surface()
        band = surface.band_for_slice(0.25, 1.0)
        self.assertAlmostEqual(band.lower * 1.08602, 1.05, places=4)
        self.assertAlmostEqual(band.upper * 1.08602, 1.12, places=4)
        # A slice built at the outright forward is already in the band's space.
        self.assertEqual(surface.band_for_slice(0.25, 1.086).lower, 1.05)

    def test_without_a_feed_it_refuses_rather_than_guessing_a_level(self):
        _, surface = self.surface(feed=False)
        with self.assertRaises(ValueError) as ctx:
            surface.vol(1.0, self.EXPIRY, "BAND", "NY")
        self.assertIn("moneyness", str(ctx.exception))
        self.assertIn("feed", str(ctx.exception))

    def test_the_wings_collapse_outside_the_band(self):
        """The whole point: a lognormal wing pays for a level the peg forbids."""
        _, surface = self.surface()
        inside = float(surface.vol(1.0, self.EXPIRY, "BAND", "NY"))
        outside = float(surface.vol(1.03, self.EXPIRY, "BAND", "NY"))
        lognormal = float(surface.vol(1.03, self.EXPIRY, "SVI", "NY"))
        self.assertGreater(inside, outside)
        self.assertLess(outside, lognormal * 0.75)

    def test_the_treatment_is_part_of_the_cache_key(self):
        """Two hazards are two smiles; a cache that could not tell them apart
        would serve the first answer for the rest of the session."""
        from volkit.banded import BandTreatment, JumpSpec
        _, surface = self.surface()
        low = float(surface.vol(1.02, self.EXPIRY, "BAND", "NY"))
        surface.set_band_treatment(BandTreatment(mode="mixture",
                                                 jump=JumpSpec(hazard=0.40)))
        high = float(surface.vol(1.02, self.EXPIRY, "BAND", "NY"))
        self.assertNotAlmostEqual(low, high, places=6)
        self.assertGreater(high, low)          # more break risk, more wing value

    def test_the_feed_level_is_part_of_the_cache_key_as_well(self):
        """The old bug: it was not.

        The feed is a publication and is re-read all morning (the auto-reload
        switch exists for exactly that), and a band is placed against whatever
        it then says -- so two spots are two smiles, the same way two hazards
        are.  Nothing in the key moved when the feed did, so the marking
        screen's band card printed the *republished* forward in its own column
        beside probabilities still calibrated against the old one.
        """
        _, surface = self.surface()
        level = {"f": 1.086}
        surface.forward_lookup = lambda t: level["f"]
        before = float(surface.vol(1.01, self.EXPIRY, "BAND", "NY"))
        level["f"] = 1.09              # the market is republished higher
        after = float(surface.vol(1.01, self.EXPIRY, "BAND", "NY"))
        self.assertNotAlmostEqual(before, after, places=6)
        # and it is still a cache: the same level gives the same slice back.
        level["f"] = 1.086
        self.assertAlmostEqual(
            float(surface.vol(1.01, self.EXPIRY, "BAND", "NY")), before, places=12)

    def test_a_blend_is_between_the_two_and_says_it_is_not_a_model(self):
        from volkit.banded import BandTreatment
        _, surface = self.surface()
        band = float(surface.vol(1.02, self.EXPIRY, "BAND", "NY"))
        logn = float(surface.vol(1.02, self.EXPIRY, "SVI", "NY"))
        warnings = surface.set_band_treatment(BandTreatment(mode="mixture", blend=0.5))
        mixed = float(surface.vol(1.02, self.EXPIRY, "BAND", "NY"))
        self.assertAlmostEqual(mixed, 0.5 * band + 0.5 * logn, places=10)
        self.assertTrue(any("arbitrage free" in w for w in warnings))

    def test_a_delta_strike_is_found_where_the_fixed_point_will_not_contract(self):
        """v -> vol(K(v)) contracts only while the smile is gentle.

        A band smile is not: its wings fall away where the peg's support runs
        out, so the 10 delta strikes came back as "did not converge" and took
        the whole smile table with them. Delta is still monotone in strike, so
        the bracketed solve one level down finds them.
        """
        from volkit.numerics import fixed_point
        _, surface = self.surface()
        sl = surface.slice_at(self.EXPIRY, "BAND", "NY")
        with self.assertRaises(ConvergenceError):        # the primary path alone
            fixed_point(lambda v: float(sl.vol(black.strike_from_delta(
                -0.10, sl.forward, v, sl.t, False, sl.conv))),
                sl.atm_vol, tol=1e-11, max_iter=80, what="10d put")
        table = {r["label"]: r["strike"] for r in
                 surface.smile_table(self.EXPIRY, method="BAND", cut="NY")}
        self.assertEqual(len(table), 5)
        self.assertLess(table["10d put"], table["25d put"])
        self.assertLess(table["25d put"], table["ATM"])
        self.assertLess(table["ATM"], table["25d call"])
        self.assertLess(table["25d call"], table["10d call"])
        # And the wings sit inside the band, which is the whole point of it.
        self.assertGreater(table["10d put"], sl.band.lower)
        self.assertLess(table["10d call"], sl.band.upper)

    def test_a_delta_the_smile_never_reaches_says_so(self):
        """Not "did not converge": the peg's support ran out.

        With the hazard marked at zero -- a peg that cannot break, which §6 of
        CLAUDE.md says is not a thing to believe, but is a thing to be able to
        ask for -- the distribution has compact support and there is no 10
        delta call at all. That is a statement about the marks.
        """
        from volkit.banded import BandTreatment, JumpSpec
        _, surface = self.surface()
        surface.set_band_treatment(BandTreatment(mode="mixture",
                                                 jump=JumpSpec(hazard=0.0)))
        sl = surface.slice_at(self.EXPIRY, "BAND", "NY")
        with self.assertRaises(ConvergenceError) as ctx:
            sl.strike_from_delta(0.10, True)
        self.assertIn("never reaches", str(ctx.exception))
        self.assertIn("peg", str(ctx.exception))

    def test_a_pair_with_no_band_is_told_which_pairs_have_one(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        with self.assertRaises(ValueError) as ctx:
            book["EURUSD"].vol(1.0, self.EXPIRY, "BAND", "NY")
        self.assertIn("PEG_BANDS", str(ctx.exception))

    def test_the_panel_reports_breach_probability_and_keeps_a_failed_row(self):
        from volkit.banded import band_panel
        _, surface = self.surface()
        panel = band_panel(surface, ["3M", "1Y"], cut="NY")
        self.assertTrue(panel["has_band"])
        good = panel["rows"][0]
        self.assertEqual(good["message"], "")
        self.assertGreater(good["prob_outside_band"], 0.0)
        self.assertLess(good["prob_outside_band"], good["prob_broken"])
        # 1Y cannot be fitted inside a 6% band, and the row says so in place.
        self.assertEqual(len(panel["rows"]), 2)
        self.assertIn("band", panel["rows"][1]["message"])

    def test_the_calibration_names_the_bound_that_was_missed(self):
        """Reporting only the break-risk floor named one cause for both
        failures, and sent a marker to lower a hazard that was not the
        problem."""
        from volkit.banded import calibrate_band_smile
        with self.assertRaises(ConvergenceError) as narrow:
            calibrate_band_smile(Band("X", 0.99, 1.01), 1.0, 1.0, 0.20)
        self.assertIn("wide", str(narrow.exception))
        with self.assertRaises(ConvergenceError) as jumpy:
            calibrate_band_smile(Band("X", 0.90, 1.10), 1.0, 1.0, 0.005,
                                 jump=JumpSpec(hazard=0.5, weak_jump=0.005,
                                               strong_jump=0.005, weak_vol=0.30,
                                               strong_vol=0.30))
        self.assertIn("at least", str(jumpy.exception))


class TestBandTreatmentRequest(unittest.TestCase):
    """Percentages at the edge, decimals in the middle -- converted once."""

    def test_the_screens_units_are_converted_exactly_once(self):
        from volkit.banded import BandTreatment
        t = BandTreatment.from_request({"mode": "mixture", "hazard": "3", "weak_jump": 8,
                                        "blend": "80", "delta": 10, "lower": "7.74"})
        self.assertAlmostEqual(t.jump.hazard, 0.03)
        self.assertAlmostEqual(t.jump.weak_jump, 0.08)
        self.assertAlmostEqual(t.blend, 0.80)
        self.assertAlmostEqual(t.delta, 0.10)
        self.assertEqual(t.lower, 7.74)
        self.assertEqual(t.to_request()["hazard"], 3.0)

    def test_a_blank_field_keeps_the_default_rather_than_becoming_zero(self):
        """A hazard silently set to zero is a peg that cannot break."""
        from volkit.banded import BandTreatment, JumpSpec
        t = BandTreatment.from_request({"hazard": "", "weak_vol": None})
        self.assertEqual(t.jump.hazard, JumpSpec().hazard)
        self.assertEqual(t.jump.weak_vol, JumpSpec().weak_vol)

    def test_nonsense_is_refused_by_name(self):
        from volkit.banded import BandTreatment
        with self.assertRaises(ValueError) as ctx:
            BandTreatment.from_request({"hazard": "soon"})
        self.assertIn("hazard", str(ctx.exception))
        with self.assertRaises(ValueError):
            BandTreatment.from_request({"mode": "ignore"})
        with self.assertRaises(ValueError):
            BandTreatment.from_request({"blend": "140"})


class TestBreakRegimeFit(unittest.TestCase):
    """Stage B of the band calibration: the break regime from the wings.

    The body is exact from the ATM and the forward, as it always was; what is
    new is that the hazard and the share of breaks (and any other break
    parameter somebody frees) come out of both wings at both deltas by least
    squares, per tenor or across the whole curve, with the residual of every
    quote reported and the identifiability *measured* at the answer.
    """

    BAND = Band("USDHKD", 7.75, 7.85)
    F = 7.8020
    TRUTH = JumpSpec(hazard=0.03, weak_share=0.7)
    START = JumpSpec(hazard=0.01, weak_share=0.85)

    def quotes(self, t, atm, deltas=(0.25, 0.10), truth=None):
        """Wings a surface under ``truth`` would quote: the model's own implied
        volatility at the model's own delta strikes, iterated to a fixed point."""
        from volkit.banded import WingQuote
        conv = DeltaConvention(True)
        sm, _ = calibrate_band_smile(self.BAND, self.F, t, atm, jump=truth or self.TRUTH, conv=conv)
        out = []
        for d in deltas:
            vc = vp = atm
            for _ in range(40):
                kc = black.strike_from_delta(d, self.F, vc, t, True, conv)
                kp = black.strike_from_delta(-d, self.F, vp, t, False, conv)
                vc, vp = sm.implied_vol(kc), sm.implied_vol(kp)
            out.append(WingQuote(d, vc - vp, 0.5 * (vc + vp) - atm))
        return tuple(out)

    def test_solve_hazard_is_the_one_parameter_one_instrument_case(self):
        """The wrapper: hazard alone against the strangle premium, bracketed,
        and the number it gives is the number it gave before the refit."""
        _, rep = calibrate_band_smile(self.BAND, self.F, 0.25, 0.004424, risk_reversal=0.0022,
                                      strangle=0.0014, solve_hazard=True,
                                      conv=DeltaConvention(True))
        # The number the pre-refit solver gave for this case, to the last digit.
        self.assertAlmostEqual(rep["hazard"], 0.05006201744360903, places=12)
        self.assertEqual(rep["fit"]["method"], "bracketed solve")
        self.assertEqual(rep["fit"]["free"], ["hazard"])
        self.assertTrue(rep["fit"]["converged"])
        self.assertEqual(len(rep["fit"]["residuals"]), 1)
        self.assertLess(abs(rep["fit"]["residuals"][0]["residual"]), 1e-10)

    def test_a_planted_regime_is_recovered_from_one_tenors_wings(self):
        from volkit.banded import calibrate_band_wings
        t, atm = 0.25, 0.0044
        _, rep = calibrate_band_wings(self.BAND, self.F, t, atm, self.quotes(t, atm),
                                      conv=DeltaConvention(True), jump=self.START)
        fit = rep["fit"]
        self.assertAlmostEqual(fit["fitted"]["hazard"], 0.03, places=6)
        self.assertAlmostEqual(fit["fitted"]["weak_share"], 0.7, places=5)
        self.assertLess(fit["rmse"], 1e-7)
        self.assertTrue(fit["converged"], fit["notes"])
        self.assertEqual(fit["n_quotes"], 4)
        self.assertEqual(fit["held"]["weak_jump"], self.START.weak_jump)
        # Every quote reports its own residual, and the strikes their placement.
        self.assertEqual({r["instrument"] for r in fit["residuals"]}, {"rr", "fly"})
        self.assertEqual({w["delta"] for w in rep["wings"]}, {0.25, 0.10})
        self.assertIn("call_in_band", rep["wings"][0])
        # The ATM and the forward are still exact: the body was profiled out.
        self.assertTrue(rep["converged"])

    def test_the_term_structure_shares_one_regime_and_reports_each_tenors_own(self):
        from volkit.banded import TenorQuotes, calibrate_band_term_structure
        curve = (("1M", 1 / 12, 0.0035), ("3M", 0.25, 0.0044), ("6M", 0.5, 0.0055),
                 ("1Y", 1.0, 0.0075))
        tenors = [TenorQuotes(t, self.F, atm, self.quotes(t, atm), name) for name, t, atm in curve]
        out = calibrate_band_term_structure(self.BAND, tenors, conv=DeltaConvention(True),
                                            jump=self.START)
        self.assertAlmostEqual(out["jump"].hazard, 0.03, places=6)
        self.assertAlmostEqual(out["jump"].weak_share, 0.7, places=5)
        self.assertEqual(out["fit"]["n_quotes"], 16)
        self.assertEqual(out["n_tenors"], 4)
        own = [h["hazard"] for h in out["hazard_by_tenor"]]
        self.assertEqual(len(own), 4)
        for h in own:
            self.assertAlmostEqual(h, 0.03, places=5)
        self.assertEqual(out["hazard_slope_note"], "")
        for row in out["rows"]:
            self.assertTrue(row["used"], row)
            self.assertTrue(row["converged"])

    def test_a_tenor_no_hazard_can_fit_sits_out_with_its_reason(self):
        """A band too narrow for a long tenor's ATM would have made the
        shared hazard ceiling zero and taken every other tenor down with it."""
        from volkit.banded import TenorQuotes, calibrate_band_term_structure
        band = Band("X", 0.97, 1.03)
        conv = DeltaConvention(True)
        good = TenorQuotes(0.25, 1.0, 0.03, self.quotes_for(band, 1.0, 0.25, 0.03, conv), "3M")
        bad = TenorQuotes(2.0, 1.0, 0.20, self.quotes_for(band, 1.0, 0.25, 0.03, conv), "2Y")
        out = calibrate_band_term_structure(band, [good, bad], conv=conv, jump=self.START)
        self.assertEqual(out["n_tenors"], 1)
        self.assertTrue(out["rows"][0]["used"])
        self.assertFalse(out["rows"][1]["used"])
        self.assertIn("band", out["rows"][1]["message"])

    def quotes_for(self, band, F, t, atm, conv):
        from volkit.banded import WingQuote
        sm, _ = calibrate_band_smile(band, F, t, atm, jump=self.TRUTH, conv=conv)
        out = []
        for d in (0.25, 0.10):
            vc = vp = atm
            for _ in range(40):
                kc = black.strike_from_delta(d, F, vc, t, True, conv)
                kp = black.strike_from_delta(-d, F, vp, t, False, conv)
                vc, vp = sm.implied_vol(kc), sm.implied_vol(kp)
            out.append(WingQuote(d, vc - vp, 0.5 * (vc + vp) - atm))
        return tuple(out)

    def test_identifiability_is_measured_not_assumed(self):
        """Free more than the wings can see and the Jacobian says so: the
        condition number blows up, the near-degenerate pair is named, and a
        parameter the quotes do not move is marked as not informed."""
        from volkit.banded import DEGENERATE_CONDITION, calibrate_band_wings
        t, atm = 0.25, 0.003
        quotes = self.quotes(t, atm, truth=JumpSpec(hazard=0.02, weak_share=0.7))
        _, rep = calibrate_band_wings(self.BAND, self.F, t, atm, quotes,
                                      conv=DeltaConvention(True),
                                      jump=JumpSpec(hazard=0.01, weak_share=0.7),
                                      free=("hazard", "weak_share", "weak_vol", "strong_vol"))
        fit = rep["fit"]
        cond = fit["condition"]
        self.assertTrue(cond != cond or cond > DEGENERATE_CONDITION, cond)
        self.assertIsNotNone(fit["degenerate"])
        self.assertTrue(any("degenerate" in n for n in fit["notes"]))
        self.assertFalse(all(v["informed"] for v in fit["sensitivity"].values()))
        # And the hazard against the jump size, from strikes inside the band,
        # is *not* degenerate -- the forward constraint moves the body with
        # the jump, and that is visible from inside.  Measured, not argued.
        _, rep = calibrate_band_wings(self.BAND, self.F, t, atm, quotes,
                                      conv=DeltaConvention(True),
                                      jump=JumpSpec(hazard=0.01, weak_share=0.7),
                                      free=("hazard", "weak_jump"))
        self.assertLess(rep["fit"]["condition"], DEGENERATE_CONDITION)
        self.assertIsNone(rep["fit"]["degenerate"])

    def test_more_parameters_than_quotes_is_said(self):
        from volkit.banded import calibrate_band_wings
        t, atm = 0.25, 0.0044
        _, rep = calibrate_band_wings(self.BAND, self.F, t, atm, self.quotes(t, atm, (0.25,)),
                                      conv=DeltaConvention(True), jump=self.START,
                                      free=("hazard", "weak_share", "weak_jump"))
        self.assertTrue(any("underdetermined" in n for n in rep["fit"]["notes"]))

    def test_a_free_name_that_is_not_a_break_parameter_is_refused(self):
        from volkit.banded import WingQuote, calibrate_band_wings
        t, atm = 0.25, 0.0044
        for free in (("hazard", "beta"), (), ("hazard", "hazard")):
            with self.assertRaises(ValueError):
                calibrate_band_wings(self.BAND, self.F, t, atm, self.quotes(t, atm),
                                     conv=DeltaConvention(True), free=free)
        with self.assertRaises(ValueError):
            WingQuote(0.25, 0.001, 0.001, fit=("strangle",))

    def test_the_surface_level_fit_proposes_and_marks_nothing(self):
        """The card's *Fit from the wings*: the proposal is in the card's own
        units, the surface's treatment is untouched, and a tenor the band
        cannot hold keeps its row and its reason."""
        from volkit.banded import BandTreatment, fit_band_treatment
        from volkit.feed import MarketFeed
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        before = surface.band_treatment
        out = fit_band_treatment(surface, ["1M", "3M", "1Y"], cut="NY",
                                 treatment=BandTreatment(mode="mixture", jump=JumpSpec(weak_jump=0.05)))
        self.assertIs(surface.band_treatment, before)
        self.assertEqual(out["free"], ["hazard", "weak_share"])
        prop = out["proposal"]
        self.assertEqual(prop["weak_jump"], 5.0)              # held, in percent
        self.assertEqual(prop["mode"], "mixture")
        self.assertFalse(prop["solve_hazard"])
        self.assertGreater(prop["hazard"], 0.0)
        self.assertEqual([r["tenor"] for r in out["rows"]], ["1M", "3M", "1Y"])
        self.assertFalse(out["rows"][2]["used"])
        self.assertIn("band", out["rows"][2]["message"])
        self.assertEqual(len(out["hazard_by_tenor"]), 2)
        # The proposal round-trips through the same reader Apply uses.
        again = BandTreatment.from_request(prop)
        self.assertAlmostEqual(again.jump.hazard, out["jump"].hazard if "jump" in out
                               else prop["hazard"] / 100.0)

    def test_the_cli_prints_the_same_proposal(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from volkit.cli import _print_band_fit
        from volkit.feed import MarketFeed
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _print_band_fit(argparse.Namespace(fit="hazard,weak_share", cut="NY"),
                                 surface, ["1M", "3M", "1Y"])
        text = buf.getvalue()
        self.assertEqual(rc, 1)                                # the 1Y row failed, in place
        self.assertIn("free: hazard, weak_share", text)
        self.assertIn("own hazard", text)
        self.assertIn("nothing was marked", text)
        self.assertIn("1Y", text)


class TestSmileStrikeScale(unittest.TestCase):
    """The strike axis reads in levels when the feed can supply one.

    The smile is fitted in moneyness whatever happens -- that is the space the
    surface works in -- so this is a *scale* on the way out and never a change
    to a number.  Without a feed there is no honest level to name and it stays
    in K/F rather than inventing one.
    """

    def test_the_smile_carries_the_feed_level_and_no_vol_moves(self):
        from volkit.webapp import BookService
        with_feed = BookService(str(BOOK), ASOF, feed_path=str(FEED))
        without = BookService(str(BOOK), ASOF)
        q = {"pair": "EURUSD", "expiry": "2024-05-28", "method": "SVI", "cut": "TK"}
        a, b = with_feed.smile(dict(q)), without.smile(dict(q))

        self.assertTrue(a["feed"])
        self.assertGreater(a["forward"], 0.0)
        self.assertGreater(a["spot"], 0.0)
        self.assertFalse(b["feed"])
        self.assertIsNone(b["forward"])
        self.assertIsNone(b["spot"])

        # The strikes come back in moneyness either way: the page multiplies.
        # They are not the *same* moneyness any more, and that is the feed's
        # other half rather than the scale: its ``USDOIS`` rows give EUR a
        # discount factor, so the 25-delta quotes are read as the spot deltas
        # the market means and the curve is fitted at those strikes.  Small --
        # two ten-thousandths of moneyness and six thousandths of a vol point
        # at three months -- and not zero, which is the point of having it.
        self.assertEqual(len(a["curve"]), len(b["curve"]))
        moved = [abs(x["k"] - y["k"]) for x, y in zip(a["curve"], b["curve"])]
        self.assertGreater(max(moved), 0.0)
        self.assertLess(max(moved), 0.01)
        # the marked at-the-money is a quote and moves for nothing
        self.assertEqual(a["atm"], b["atm"])

    def test_the_level_is_the_one_the_band_model_would_place_against(self):
        """One lookup for both, so a strike a chart names and a band edge the
        model places can never come from different forwards."""
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED))
        payload = service.smile({"pair": "USDJPY", "expiry": "2024-05-28",
                                 "method": "SVI", "cut": "TK"})
        book = service.book
        expiry = date(2024, 5, 28)
        # One lookup, and it is the settlement-date one: the chart's axis
        # scale, the band model's placement and `market_level_for` are the
        # same number, and the payload says which date it is a price to.
        level = book.market_level_for("USDJPY", expiry)
        self.assertEqual(payload["forward"], level["forward"])
        self.assertEqual(payload["settle"], level["settle"])
        self.assertEqual(payload["settle"],
                         book.settlement_date("USDJPY", expiry).isoformat())
        self.assertEqual(payload["forward"],
                         book.forward_at("USDJPY", payload["t"], expiry=expiry))
        # and it is *not* the plain time reading, which drops the spot lag
        self.assertNotEqual(payload["forward"],
                            book.market_level("USDJPY", payload["t"])["forward"])
        # A pair the feed does not cover says so rather than guessing a level.
        self.assertFalse(service.book.market_level("XXXYYY", 0.25)["feed"])
        self.assertIsNone(service.book.forward_at("XXXYYY", 0.25))


if __name__ == "__main__":
    unittest.main()
