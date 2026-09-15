"""Analysis, relative value and history.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestAnalysis(unittest.TestCase):
    """Carry and roll, fair value, and the cross triangle."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all()
        cls.book.feed = MarketFeed.load(FEED)
        cls.history = history.load_history(HISTORY, cls.book.pairs)

    def test_every_advertised_target_resolves_to_legs(self):
        for key in analytics.TARGETS:
            legs = analytics._target_legs(key)
            self.assertTrue(legs, key)
            self.assertAlmostEqual(sum(w for w, _, _ in legs),
                                   0.0 if key.startswith("rr") else
                                   (0.0 if key.startswith("fly") else 1.0), places=12, msg=key)
        with self.assertRaises(ValueError):
            analytics._target_legs("nonsense")

    def test_the_roll_splits_exactly_into_term_and_smile(self):
        rows = [r for r in analytics.carry_table(self.book, "EURUSD", horizon_days=30, cut="NY")
                if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r.roll, r.roll_term + r.roll_smile, places=15, msg=r.tenor)

    def test_without_a_forward_feed_the_smile_slide_is_zero_and_says_so(self):
        """USDCNY has marks but no feed, so the strike can only be held in moneyness.

        Not a cross: a cross the feed quotes both legs of has a forward built
        from the triangle (``TestCrossLevelsFromTheLegs``), so EURJPY -- which
        this used to be written on -- is no longer a pair with no feed.
        """
        rows = [r for r in analytics.carry_table(self.book, "USDCNY", horizon_days=30, cut="NY")
                if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r.roll_smile, 0.0)
            self.assertTrue(any("forward feed" in w for w in r.warnings), r.tenor)

    def test_a_forward_curve_makes_the_smile_slide_bite(self):
        rows = {r.tenor: r for r in analytics.carry_table(
            self.book, "EURUSD", horizon_days=30, target="25dp", cut="NY") if r.expiry}
        self.assertTrue(any(abs(r.roll_smile) > 1e-6 for r in rows.values()))

    def test_a_tenor_shorter_than_the_horizon_is_reported_not_dropped(self):
        rows = analytics.carry_table(self.book, "EURUSD", horizon_days=30, cut="NY")
        self.assertEqual([r.tenor for r in rows], list(self.book.data.tenor_points))
        short = [r for r in rows if not r.expiry]
        self.assertTrue(short)
        for r in short:
            self.assertTrue(r.warnings and "horizon" in r.warnings[0])

    def test_the_carry_delta_is_the_smile_delta_not_the_black_scholes_one(self):
        """The bug this was written for.

        The carry table reported a Black-Scholes delta -- the sensitivity with
        the volatility *held fixed* as the forward moves -- for a table whose
        entire subject is a fixed strike sliding under a moving forward.  The
        volatility that strike is marked at moves with it, and on a skewed
        surface the difference is several delta.
        """
        moved = 0
        for pair in ("EURUSD", "USDJPY"):
            for target in ("25dc", "25dp", "10dp"):
                rows = [r for r in analytics.carry_table(
                    self.book, pair, horizon_days=7, target=target, cut="NY") if r.expiry]
                self.assertTrue(rows)
                for r in rows:
                    self.assertIsNotNone(r.smile_delta, f"{pair} {target} {r.tenor}")
                    self.assertAlmostEqual(r.skew_delta, r.smile_delta - r.delta, places=15)
                    if abs(r.skew_delta) > 0.01:
                        moved += 1
        self.assertGreater(moved, 20, "the skew moved no delta by a whole point")

    def test_the_smile_delta_is_black_scholes_plus_vega_times_the_skew_slope(self):
        """``dV/dF = dV/dF|sigma + vega * dsigma/dF``, and nothing else.

        Pinned by finite difference so a change to the smile delta that broke
        this identity could not pass as a refinement.
        """
        for pair in ("EURUSD", "USDJPY"):
            surface = self.book[pair]
            rows = [r for r in analytics.carry_table(
                self.book, pair, horizon_days=7, target="25dp", cut="NY") if r.expiry]
            for r in rows[:4]:
                expiry = self.book.clock.datetime_from_years(r.t)
                f, k = r.forward, r.strike
                bs = float(black.delta(f, k, r.level, r.t, False))
                vega = float(black.vega(f, k, r.level, r.t))
                eps = f * 1e-4
                slope = (float(surface.vol(k / (f + eps), expiry, "SVI", "NY"))
                         - float(surface.vol(k / (f - eps), expiry, "SVI", "NY"))) / (2 * eps)
                self.assertAlmostEqual(r.delta, bs, places=12, msg=f"{pair} {r.tenor}")
                # Both sides are central differences at different bumps, so
                # they agree to the second-order term and not beyond it; a
                # short tenor has the most curvature and the widest gap.
                self.assertAlmostEqual(r.smile_delta, bs + vega * slope, delta=1e-3,
                                       msg=f"{pair} {r.tenor}")

    def test_the_smile_delta_values_the_whole_of_what_the_forward_move_does(self):
        """And the Black-Scholes delta values only half the story.

        The forward reaches the position twice: through the price at a fixed
        volatility (``carry_pnl``) and through the mark, because the strike's
        moneyness changed (``vega * roll_smile``).  The smile delta is the
        first-order coefficient of *both*; the Black-Scholes delta is the
        coefficient of the first alone.  Measured over a one-day horizon,
        where the second order terms have not had room to matter.
        """
        checked = 0
        for pair in ("EURUSD", "USDJPY"):
            for target in ("25dc", "25dp", "10dp"):
                rows = [r for r in analytics.carry_table(
                    self.book, pair, horizon_days=1, target=target, cut="NY") if r.expiry]
                for r in rows:
                    # A one-day roll is only "small" against a tenor with room
                    # in it: a week rolled by a day is a sixth of its life and
                    # the second-order terms are no longer negligible.
                    move = r.forward_rolled - r.forward
                    whole = r.vega * r.roll_smile + r.carry_pnl
                    if r.t < 1.0 / 12.0 or abs(whole) < 1e-9 or abs(move) < 1e-12:
                        continue
                    where = f"{pair} {target} {r.tenor}"
                    self.assertAlmostEqual(r.smile_delta * move / whole, 1.0, delta=0.02,
                                           msg=where)
                    self.assertAlmostEqual(r.delta * move / r.carry_pnl, 1.0, delta=0.02,
                                           msg=where)
                    # And the point: the Black-Scholes delta is not a reading
                    # of the whole move at all, by a margin far outside the
                    # tolerance above.
                    self.assertGreater(abs(r.delta * move - whole),
                                       5.0 * abs(r.smile_delta * move - whole), where)
                    checked += 1
        self.assertGreater(checked, 20)

    def test_the_at_the_money_straddle_is_delta_neutral_only_in_black_scholes(self):
        """A straddle is long vega, and on a skewed surface the volatility
        moves with the forward, so the delta-neutral strike is delta neutral
        in one column and not in the other.  Reporting the Black-Scholes zero
        alone said the position had no forward exposure when it had several
        delta of it."""
        # EURUSD quotes an unadjusted delta, so its delta-neutral straddle
        # really is Black-Scholes delta neutral and the whole of the smile
        # delta is the skew.
        rows = [r for r in analytics.carry_table(
            self.book, "EURUSD", horizon_days=7, target="atm", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r.delta, 0.0, places=9, msg=r.tenor)
            self.assertAlmostEqual(r.skew_delta, r.smile_delta, places=12, msg=r.tenor)
        self.assertGreater(max(abs(r.smile_delta) for r in rows), 0.0)

        # USDJPY quotes a premium-adjusted one, so the delta-neutral strike is
        # neutral in *that* convention and carries a little unadjusted delta
        # already.  The skew is still much the larger part of what it runs.
        rows = [r for r in analytics.carry_table(
            self.book, "USDJPY", horizon_days=7, target="atm", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertLess(abs(r.delta), 0.05, r.tenor)
        biggest = max(rows, key=lambda r: abs(r.smile_delta))
        self.assertGreater(abs(biggest.smile_delta), 0.05)
        self.assertGreater(abs(biggest.skew_delta), 2.0 * abs(biggest.delta))

    def test_the_carry_deltas_are_term_currency_not_the_quoted_convention(self):
        """A premium-adjusted delta is a hedge ratio in the *other* currency.

        Multiplying it by a move in the forward does not give the money the
        position made, and money is what this table reports, so both columns
        are dV/dF and the surface's own convention is deliberately not used.
        """
        surface = self.book["USDJPY"]
        self.assertTrue(bool(surface.conv), "USDJPY should be premium adjusted here")
        r = [x for x in analytics.carry_table(
            self.book, "USDJPY", horizon_days=7, target="25dc", cut="NY") if x.expiry][-1]
        quoted = float(black.delta(r.forward, r.strike, r.level, r.t, True,
                                   surface.slice_conv(r.t)))
        self.assertAlmostEqual(quoted, 0.25, places=6)          # the strike is a 25 delta one
        self.assertNotAlmostEqual(r.delta, quoted, places=3)    # and this column is not that
        self.assertAlmostEqual(
            r.delta, float(black.delta(r.forward, r.strike, r.level, r.t, True)), places=12)
        # The override exists for exactly this caller and changes nothing else.
        expiry = self.book.clock.datetime_from_years(r.t)
        own = surface.smile_delta(r.forward, r.strike, expiry, True, "SVI", "NY")
        term = surface.smile_delta(r.forward, r.strike, expiry, True, "SVI", "NY", conv=False)
        self.assertAlmostEqual(term, r.smile_delta, places=12)
        self.assertNotAlmostEqual(own, term, places=3)

    def test_the_forward_carry_is_delta_hedged_before_it_pays_for_a_break_even(self):
        """A break-even volatility is a property of the strike, not of the side.

        ``carry_pnl`` is the whole revaluation at the rolled forward and is
        the right number for a spot-hedged *position*.  It is the wrong one
        for a break-even: put-call parity puts the entire difference between
        the call's and the put's revaluation at one strike into the
        first-order term ``delta * (F2 - F1)``, so read unhedged the same
        strike is "rich" as a call and "cheap" as a put by a quarter of the
        forward move.  ``carry_hedged`` takes that term out and what is left
        is the gamma over the move -- which, being the convexity of a long
        option, cannot be negative whichever side it is written as.  That is
        the bug the relative-value grid's carry signal had: the score flipped
        sign across the strike axis with the option's direction.
        """
        for pair in ("EURUSD", "USDJPY", "EURJPY"):
            for target in ("atm", "25dp", "25dc", "10dp", "10dc"):
                rows = [r for r in analytics.carry_table(
                    self.book, pair, horizon_days=7, target=target, cut="NY")
                    if r.expiry and r.carry_pnl is not None]
                self.assertTrue(rows, f"{pair} {target}")
                for r in rows:
                    where = f"{pair} {target} {r.tenor}"
                    self.assertAlmostEqual(
                        r.carry_hedged,
                        r.carry_pnl - r.delta * (r.forward_rolled - r.forward),
                        places=15, msg=where)
                    # Convexity: V(F2) - V(F1) - delta * (F2 - F1) >= 0.
                    self.assertGreaterEqual(r.carry_hedged, -1e-15, msg=where)

    def test_a_call_and_a_put_at_one_strike_carry_the_same_break_even(self):
        """The exact identity behind the fix, at one strike rather than two.

        The carry columns are read at a 25 delta *call* strike and a 25 delta
        *put* strike, which are two different strikes, so the grid alone
        cannot show that the direction has stopped mattering.  Here both sides
        are priced at one strike: the raw revaluations differ by the whole
        forward move, and the hedged ones are the same number.
        """
        r = [x for x in analytics.carry_table(
            self.book, "USDJPY", horizon_days=7, target="25dc", cut="NY") if x.expiry][-1]
        f1, f2, k, vol, t = r.forward, r.forward_rolled, r.strike, r.level, r.t
        pnl, hedged = {}, {}
        for call in (True, False):
            pnl[call] = float(black.price(f2, k, vol, t, call) - black.price(f1, k, vol, t, call))
            hedged[call] = pnl[call] - float(black.delta(f1, k, vol, t, call)) * (f2 - f1)
        self.assertAlmostEqual(pnl[True] - pnl[False], f2 - f1, places=12)
        self.assertGreater(abs(pnl[True] - pnl[False]), 0.9 * abs(f2 - f1))
        # A yen strike is ~150, so the cancellation leaves a few times 1e-14
        # of dust on a number of order 1e-5; the identity is exact in exact
        # arithmetic and this is the last bit of a double, not a difference.
        self.assertAlmostEqual(hedged[True], hedged[False], delta=1e-12)
        self.assertAlmostEqual(hedged[True], r.carry_hedged, delta=1e-12)

    def test_the_fair_value_carry_is_the_gamma_and_not_the_residual_delta(self):
        """What the delta hedge changed on the fair-value card, and where.

        The at-the-money straddle is delta neutral in the pair's **own**
        quoted convention, so on a pair quoting an unadjusted delta the hedge
        is exactly zero and nothing moves.  On a premium-adjusted pair the
        delta-neutral strike is neutral in *that* convention and its ``dV/dF``
        is not quite zero, so the old ``carry_value`` carried a residual first
        order term.  The tell is that it grew with the tenor -- a delta term
        is linear in the forward move -- while a real gamma term is flat.
        """
        eur = analytics.fair_value_table(self.book, "EURUSD", None, horizon_days=7, cut="NY")
        self.assertFalse(bool(self.book["EURUSD"].conv))
        for r in eur:
            if r.carry_pnl is not None:
                self.assertAlmostEqual(r.carry_hedged, r.carry_pnl, places=15, msg=r.tenor)
        jpy = [r for r in analytics.fair_value_table(
            self.book, "USDJPY", None, horizon_days=7, cut="NY") if r.carry_pnl is not None]
        self.assertTrue(bool(self.book["USDJPY"].conv))
        self.assertTrue(jpy)
        for r in jpy:
            self.assertLess(abs(r.carry_hedged), 0.5 * abs(r.carry_pnl), msg=r.tenor)
        # Flat, not growing: the residual delta was linear in the forward move
        # and so scaled with the tenor, and the gamma over one horizon's move
        # does not.  Unhedged this ratio was better than twenty.
        values = [abs(r.carry_value) for r in jpy]
        unhedged = [abs(r.carry_value * r.carry_pnl / r.carry_hedged) for r in jpy]
        self.assertLess(max(values), 2.0 * min(values))
        self.assertGreater(max(unhedged), 10.0 * min(unhedged))

    def test_the_fair_value_roll_is_the_atm_roll_whatever_is_displayed(self):
        """Feeding an at-the-money implied and a risk-reversal roll into one
        break-even mixes two different positions."""
        a = analytics.fair_value_table(self.book, "EURUSD", None, horizon_days=30, cut="NY")
        b = analytics.fair_value_table(self.book, "EURUSD", None, horizon_days=30, cut="NY")
        carry_rr = {r.tenor: r for r in analytics.carry_table(
            self.book, "EURUSD", horizon_days=30, target="rr25", cut="NY")}
        carry_atm = {r.tenor: r for r in analytics.carry_table(
            self.book, "EURUSD", horizon_days=30, target="atm", cut="NY")}
        self.assertEqual([r.roll for r in a], [r.roll for r in b])
        for r in a:
            self.assertAlmostEqual(r.roll, carry_atm[r.tenor].roll, places=15)
            self.assertNotAlmostEqual(r.roll, carry_rr[r.tenor].roll, places=6)

    def test_richness_is_implied_less_realized_less_the_roll_and_the_carry(self):
        """The break-even gained a third term when the forward curve's
        price-side carry was added; ``carry_value`` is it."""
        rows = analytics.fair_value_table(self.book, "EURUSD", self.history["EURUSD"],
                                          horizon_days=30, cut="NY")
        priced = [r for r in rows if r.fair is not None]
        self.assertTrue(priced)
        for r in priced:
            self.assertAlmostEqual(r.richness,
                                   r.implied - r.realized - r.roll_value - r.carry_value,
                                   places=15)
            self.assertAlmostEqual(r.roll_value, r.roll * r.roll_multiplier, places=15)

    def test_a_tenor_with_too_little_history_keeps_its_row(self):
        rows = analytics.realized_table(self.book, "EURUSD", self.history["EURUSD"], cut="NY")
        self.assertEqual([r.tenor for r in rows], list(self.book.data.tenor_points))
        blank = [r for r in rows if r.observations == 0]
        self.assertTrue(blank)
        self.assertTrue(blank[0].warnings)

    def test_matching_the_window_to_the_tenor_is_the_default(self):
        rows = analytics.realized_table(self.book, "EURUSD", self.history["EURUSD"], cut="NY")
        for r in rows:
            self.assertAlmostEqual(r.window_days, r.t * 365.2425, places=6)
        fixed = analytics.realized_table(self.book, "EURUSD", self.history["EURUSD"],
                                         lookback_days=90, cut="NY")
        self.assertTrue(all(r.window_days == 90.0 for r in fixed))

    def test_the_variance_triangle_reproduces_the_marked_cross(self):
        """The book builds a cross from exactly this expression, so its own
        at-the-money mark must sit on it bar the cross's own add-on."""
        rows = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m", "1y"],
                                        with_noise=False)
        for r in rows:
            self.assertLess(abs(r.variance_triangle_atm - r.marked["atm"]), 0.0015, msg=r.tenor)
            self.assertAlmostEqual(r.implied_correlation, r.rho, delta=0.02, msg=r.tenor)

    def test_the_distribution_triangle_sits_above_the_variance_one(self):
        """It carries the legs' whole densities, whose variance is larger than
        their at-the-money volatilities by the convexity of their own smiles."""
        r = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"],
                                     with_noise=False)[0]
        self.assertGreater(r.smile_convexity, 0.0)
        self.assertLess(r.smile_convexity, 0.01)

    def test_the_triangle_reports_its_own_noise_floor(self):
        r = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"])[0]
        self.assertTrue(r.noise)
        for key, value in r.noise.items():
            self.assertLess(value, 5e-4, msg=key)

    def test_the_triangle_coefficients_reach_the_row(self):
        r = analytics.triangle_table(self.book, "EURGBP", cut="NY", tenors=["3m"],
                                     with_noise=False)[0]
        self.assertEqual(r.coefficients, (1, -1))

    def test_a_cross_loaded_on_its_own_still_has_a_triangle(self):
        """``load_all`` builds a cross's legs but deliberately does not fit
        their smiles -- nothing else needs them.  The triangle does, and it is
        the only thing that does, so it must arrange them itself rather than
        returning an empty table."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURGBP"])
        self.assertFalse(book["EURUSD"].fits)          # not fitted by the load
        rows = analytics.triangle_table(book, "EURGBP", cut="NY", tenors=["3m"],
                                        with_noise=False)
        self.assertEqual(len(rows), 1)
        self.assertIn("rr25", rows[0].triangle)
        self.assertTrue(book["EURUSD"].fits)           # fitted on demand

    def test_a_tenor_the_triangle_cannot_build_keeps_its_row(self):
        """An earlier cut dropped the tenor and its reason together, so when
        every tenor failed the table came back silently empty."""
        def boom(*a, **kw):
            raise ValueError("no density here")

        original = moments.distribution_from_surface
        moments.distribution_from_surface = boom
        try:
            rows = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"])
        finally:
            moments.distribution_from_surface = original
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].marked, {})
        self.assertEqual(rows[0].coefficients, (1, 1))
        self.assertTrue(any("no density here" in w for w in rows[0].warnings))

    def test_the_triangle_reads_its_deltas_in_the_slice_convention(self):
        """The triangle handed ``surface.conv`` to the copula and ``leg.conv``
        to the noise floor -- forward delta, ``df_foreign`` 1 -- while the
        marked smile it is compared with is read in spot delta off
        ``slice_conv(t)``.  Two different 25-delta strikes, compared as one."""
        seen = {"combine": [], "reconstruction_error": []}
        originals = {name: getattr(moments, name) for name in seen}

        def spy(name):
            def call(*args, **kwargs):
                seen[name].append(args[4] if name == "combine" else args[1])
                return originals[name](*args, **kwargs)
            return call

        for name in seen:
            setattr(moments, name, spy(name))
        try:
            analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"])
        finally:
            for name, fn in originals.items():
                setattr(moments, name, fn)
        cross = self.book["EURJPY"]
        t = cross.tenor_years("3m")
        # The noise floor's own reconstruction also goes through ``combine``,
        # each in its own leg's convention; the cross's is the first call.
        self.assertEqual(seen["combine"][0], cross.slice_conv(t))
        self.assertLess(seen["combine"][0].df_foreign, 1.0)
        self.assertEqual(seen["reconstruction_error"],
                         [self.book[leg].slice_conv(t) for leg in ("EURUSD", "USDJPY")])

    def test_a_marked_dependence_fattens_the_triangle_and_keeps_the_gaussian_beside_it(self):
        """The Gaussian copula put every cross butterfly below the market,
        because it has no vol-vol correlation and no correlation vol.  Marked
        on CROSS_DEPENDENCE, both reach the triangle; the ATM stays where the
        Gaussian copula put it, and that copula's answer stays on the row."""
        def book(config=None):
            # Both built the same way: the legs' smiles are fitted on demand,
            # after the feed, so their deltas are read in one convention.
            made = Book.from_excel(BOOK, ASOF, config=config).load_all(["AUDJPY"])
            made.feed = self.book.feed
            return made

        plain = analytics.triangle_table(book(), "AUDJPY", cut="NY", tenors=["3m"],
                                         with_noise=False, implied_vol_vol=False)[0]
        book = book({"CROSS_DEPENDENCE": [
            {"pair": "AUDJPY", "tenor": "3m", "vol vol corr": 0.9, "corr vol": 0.1}]})
        r = analytics.triangle_table(book, "AUDJPY", cut="NY", tenors=["3m"],
                                     with_noise=False, implied_vol_vol=False)[0]
        self.assertEqual((r.vol_vol, r.corr_vol), (0.9, 0.1))
        self.assertEqual((plain.vol_vol, plain.corr_vol, plain.gaussian), (None, 0.0, {}))
        self.assertEqual(plain.copula_rho, plain.rho)
        self.assertNotAlmostEqual(r.copula_rho, r.rho, places=3)
        for key, value in plain.triangle.items():
            self.assertAlmostEqual(r.gaussian[key], value, places=12, msg=key)
        self.assertAlmostEqual(r.triangle["atm"], plain.triangle["atm"], delta=2e-6)
        self.assertGreater(r.triangle["fly25"], plain.triangle["fly25"])
        self.assertGreater(r.triangle["fly10"], plain.triangle["fly10"])
        # The cross's own marks and the variance triangle do not care.
        self.assertEqual(r.marked, plain.marked)
        self.assertEqual(r.variance_triangle_atm, plain.variance_triangle_atm)
        # The marking screen's implied quotes are built from the same law.
        q = analytics.implied_cross_quotes(book, "AUDJPY", cut="NY", tenors=["3m"])[0]
        self.assertEqual((q.vol_vol, q.corr_vol), (0.9, 0.1))
        self.assertAlmostEqual(q.copula_rho, r.copula_rho, places=12)
        self.assertAlmostEqual(q.quotes["rr_25"], r.triangle["rr25"], places=12)

    def test_the_triangle_backs_out_the_vol_vol_correlation_to_mark_against(self):
        r = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"],
                                     with_noise=False)[0]
        self.assertTrue(r.implied_vol_vol is not None or r.implied_vol_vol_note,
                        "neither a vol-vol correlation nor the reason there is none")
        if r.implied_vol_vol is not None:
            self.assertLessEqual(abs(r.implied_vol_vol), 1.0)
        off = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["3m"],
                                       with_noise=False, implied_vol_vol=False)[0]
        self.assertIsNone(off.implied_vol_vol)
        self.assertEqual(off.implied_vol_vol_note, "")

    def test_a_pair_that_is_not_a_cross_has_no_triangle(self):
        with self.assertRaises(ValueError) as cm:
            analytics.triangle_table(self.book, "EURUSD", cut="NY")
        self.assertIn("not a cross", str(cm.exception))

    # -- a cross's quotes implied by its legs ----------------------------

    def test_a_leg_alone_gives_back_its_own_risk_reversal_and_market_strangle(self):
        """The fill writes into the sheet's ST columns, which hold *market*
        strangles; the triangle's ``fly`` is the smile strangle, a different
        number.  Pushed through the copula on its own, a leg must come back
        with the RR and the market strangle its own surface reads."""
        leg = self.book["EURUSD"]
        for tenor in ("1m", "1y"):
            t = leg.tenor_years(tenor)
            expiry = self.book.clock.datetime_from_years(t)
            dist = moments.distribution_from_surface(leg, expiry, cut="NY")
            point = moments.Distribution(x=np.array([-1e-9, 0.0, 1e-9]),
                                         pdf=np.array([0.0, 1e9, 0.0]),
                                         cdf=np.array([0.0, 0.5, 1.0]), t=dist.t, label="point")
            comb = moments.combine(dist, point, (1, 1), 0.0, leg.slice_conv(t))
            got = comb.table((0.10, 0.25))
            self.assertAlmostEqual(got["rr25"], leg.risk_reversal(expiry, 0.25, cut="NY"),
                                   delta=1e-4, msg=tenor)
            for d in (0.25, 0.10):
                self.assertAlmostEqual(comb.market_strangle(d, got["atm"]),
                                       leg.strangle(expiry, d, cut="NY"), delta=2e-4,
                                       msg=(tenor, d))

    def test_the_implied_quotes_are_the_sheets_four_fields(self):
        from volkit.surface import QUOTE_FIELDS
        rows = analytics.implied_cross_quotes(self.book, "EURJPY", cut="NY", tenors=["3m", "1y"])
        tri = {r.tenor: r for r in analytics.triangle_table(
            self.book, "EURJPY", cut="NY", tenors=["3m", "1y"], with_noise=False)}
        self.assertEqual([r.tenor for r in rows], ["3m", "1y"])
        for r in rows:
            self.assertEqual(r.error, "", msg=r.tenor)
            self.assertEqual(set(r.quotes), set(QUOTE_FIELDS))
            self.assertGreater(r.quotes["st_25"], 0.0)
            self.assertGreater(r.quotes["st_10"], r.quotes["st_25"])
            # The same copula as the triangle, at the cross's own correlation.
            self.assertAlmostEqual(r.rho, tri[r.tenor].rho, places=12)
            self.assertAlmostEqual(r.quotes["rr_25"], tri[r.tenor].triangle["rr25"], delta=5e-4)

    def test_a_tenor_the_legs_cannot_imply_keeps_its_row(self):
        def boom(*a, **kw):
            raise ValueError("no density here")

        original = moments.distribution_from_surface
        moments.distribution_from_surface = boom
        try:
            rows = analytics.implied_cross_quotes(self.book, "EURJPY", cut="NY", tenors=["3m"])
        finally:
            moments.distribution_from_surface = original
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quotes, {})
        self.assertIn("no density here", rows[0].error)

    def test_a_pair_that_is_not_a_cross_has_no_implied_quotes(self):
        with self.assertRaises(ValueError) as cm:
            analytics.implied_cross_quotes(self.book, "EURUSD", cut="NY")
        self.assertIn("not a cross", str(cm.exception))

    # -- the forward curve's own carry -----------------------------------

    def test_the_at_the_money_row_carries_no_delta(self):
        """The at-the-money is a straddle, and a straddle at the delta-neutral
        strike has no delta.

        Reading that leg as the call alone -- which is how ``_target_legs``
        marks it, because it only ever needed a *strike* -- handed the
        at-the-money row half a unit of forward carry that nobody is running.
        """
        rows = [r for r in analytics.carry_table(self.book, "EURUSD", horizon_days=30,
                                                 target="atm", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r.delta, 0.0, places=9, msg=r.tenor)
            # ... so its carry is only the gamma over the forward's move.
            self.assertLess(abs(r.carry_pnl), 1e-5, r.tenor)

    def test_a_directional_target_earns_the_forwards_roll_down(self):
        """A 25 delta call is long the forward, and the forward rolls down."""
        rows = [r for r in analytics.carry_table(self.book, "EURUSD", horizon_days=30,
                                                 target="25dc", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r.delta, 0.25, delta=0.02, msg=r.tenor)
            # Full revaluation, so it is delta times the move plus the gamma
            # over it -- close to the linear reading but not equal to it.
            linear = r.delta * (r.forward_rolled - r.forward)
            self.assertAlmostEqual(r.carry_pnl, linear, delta=0.05 * abs(linear), msg=r.tenor)
            self.assertAlmostEqual(r.carry_vols, r.carry_pnl / r.vega, places=12, msg=r.tenor)
            self.assertAlmostEqual(r.total_pnl, r.pnl + r.carry_pnl, places=15, msg=r.tenor)
            # The rate the swap points are quoting, not a price difference.
            self.assertAlmostEqual(
                r.carry_rate,
                (r.forward_rolled - r.forward) / (r.forward * (30.0 / 365.2425)), places=12)

    def test_without_a_feed_the_carry_is_unavailable_not_zero(self):
        """f1 and f2 are both 1.0 without a feed, so every carry figure would
        come out an exact zero -- a silent zero dressed as a measurement.

        USDCNY rather than EURJPY: the feed carries neither, but it carries
        both of EURJPY's legs and so builds its forward from the triangle.
        """
        rows = [r for r in analytics.carry_table(self.book, "USDCNY", horizon_days=30,
                                                 target="25dc", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNone(r.carry_pnl, r.tenor)
            self.assertIsNone(r.delta, r.tenor)
            self.assertIsNone(r.total_pnl, r.tenor)
            self.assertTrue(math.isnan(r.carry_rate), r.tenor)

    def test_a_risk_reversal_declines_to_state_its_carry_in_vols(self):
        """A risk reversal has almost no net vega, so dividing its premium
        carry by that vega is a division by nearly nothing."""
        rows = [r for r in analytics.carry_table(self.book, "EURUSD", horizon_days=30,
                                                 target="rr25", cut="NY") if r.expiry]
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNotNone(r.carry_pnl, r.tenor)
            self.assertIsNone(r.carry_vols, r.tenor)
            self.assertIsNone(r.total_pnl, r.tenor)      # a combination has no single P&L

    def test_fair_value_is_realized_plus_the_roll_and_the_carry(self):
        rows = analytics.fair_value_table(self.book, "EURUSD", self.history["EURUSD"],
                                          horizon_days=30, cut="NY")
        priced = [r for r in rows if r.fair is not None]
        self.assertTrue(priced)
        for r in priced:
            self.assertAlmostEqual(r.fair, r.realized + r.roll_value + r.carry_value, places=15)
            self.assertAlmostEqual(r.richness, r.implied - r.fair, places=15)
            # The straddle is delta neutral, so the price-side carry is second
            # order; the first-order forward effect is in the smile slide.
            self.assertLess(abs(r.carry_value), 0.001, r.tenor)

    # -- what "realized" is measured on ----------------------------------

    def test_realized_defaults_to_the_forward_and_says_so(self):
        rows = analytics.realized_table(self.book, "EURUSD", self.history["EURUSD"])
        live = [r for r in rows if r.observations]
        self.assertTrue(live)
        for r in live:
            self.assertEqual(r.realized_basis, "forward", r.tenor)
            self.assertIsNotNone(r.points_vol, r.tenor)
            # The spot-only figure is kept beside it rather than replaced.
            self.assertTrue(math.isfinite(r.realized_spot), r.tenor)
        spot = [r for r in analytics.realized_table(self.book, "EURUSD",
                                                    self.history["EURUSD"],
                                                    realized_basis="spot") if r.observations]
        self.assertTrue(all(r.realized_basis == "spot" for r in spot))
        self.assertAlmostEqual(spot[-1].realized, live[-1].realized_spot, places=12)

    # -- the wings as a SABR shape ----------------------------------------

    def test_the_marked_wings_read_back_as_a_correlation_and_a_vol_of_vol(self):
        rows = analytics.realized_table(self.book, "USDJPY", self.history["USDJPY"],
                                        with_sabr=True)
        live = [r for r in rows if r.implied_rho is not None]
        self.assertTrue(live)
        for r in live:
            # USDJPY's risk reversal is marked for the downside, so the
            # correlation that produces it is negative.
            self.assertLess(r.marked_rr, 0.0, r.tenor)
            self.assertLess(r.implied_rho, 0.0, r.tenor)
            self.assertGreater(r.implied_nu, 0.0, r.tenor)
            self.assertLess(r.implied_shape_error, 1e-6, r.tenor)
            if r.realized_rho is not None:
                self.assertAlmostEqual(r.rho_difference, r.implied_rho - r.realized_rho, places=15)
                self.assertAlmostEqual(r.nu_difference, r.implied_nu - r.realized_nu, places=15)

    def test_the_sabr_shape_is_off_unless_it_is_asked_for(self):
        rows = analytics.realized_table(self.book, "USDJPY", self.history["USDJPY"])
        self.assertTrue(all(r.implied_rho is None and r.realized_rho is None for r in rows))

    def test_the_dynamics_are_measured_over_their_own_window_not_the_lookback(self):
        """The bug: both difference columns blank at every tenor.

        ``(rho, nu)`` used to be measured over whatever realized lookback the
        screen was set to.  They need more paired observations than a realized
        volatility needs returns, so on a three-week lookback every tenor's
        realized figure came back and every tenor's measured pair was missing
        -- and with it both ``diff`` columns, which is the whole point of the
        card.  The window is a property of the measurement, not of the tenor
        being forecast, and it is never shorter than the lookback.
        """
        short = analytics.realized_table(self.book, "USDJPY", self.history["USDJPY"],
                                         lookback_days=21, with_sabr=True)
        self.assertTrue(short)
        for r in short:
            self.assertEqual(r.window_days, 21.0, r.tenor)
            self.assertGreaterEqual(r.dynamics_days, history.DYNAMICS_DAYS, r.tenor)
            self.assertIsNotNone(r.realized_rho, r.tenor)
            self.assertIsNotNone(r.rho_difference, r.tenor)
            self.assertIsNotNone(r.nu_difference, r.tenor)
        # A lookback longer than the dynamics window is not shortened to it.
        long = analytics.realized_table(self.book, "USDJPY", self.history["USDJPY"],
                                        lookback_days=500, with_sabr=True)
        self.assertTrue(all(r.dynamics_days == 500.0 for r in long))

    def test_a_tenor_with_no_realized_window_still_carries_its_sabr_shape(self):
        """A one-week window can never hold a week of returns.

        The realized statistics for that row are unavailable and say so; the
        wings as a SABR shape do not depend on them -- the marked half needs
        no history at all -- so losing the column group with them left the
        card's first row permanently blank.
        """
        rows = analytics.realized_table(self.book, "USDJPY", self.history["USDJPY"],
                                        with_sabr=True)
        short = [r for r in rows if not r.observations]
        self.assertTrue(short, "no tenor came up short, so this pins nothing")
        for r in short:
            self.assertIsNotNone(r.implied_rho, r.tenor)
            self.assertIsNotNone(r.realized_rho, r.tenor)
            self.assertIsNotNone(r.rho_difference, r.tenor)


class TestPastTheAtmForwardBoundary(unittest.TestCase):
    """Past a pair's ``atmf beyond`` tenor the smile table labels its
    at-the-money row ``ATMF``, and the triangle, its noise floor and the SABR
    shape all looked it up as ``"ATM"``: ``KeyError: 'ATM'`` took the whole
    Analysis screen down on any cross whose tenors ran past 1Y."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURJPY"])

    def test_the_at_the_money_is_found_by_kind_not_label(self):
        from volkit.surface import smile_points
        s = self.book["EURJPY"]
        t = s.tenor_years("2y")
        self.assertTrue(s.slice_conv(t).atm_is_forward(t))
        table = s.smile_table(self.book.clock.datetime_from_years(t), cut="NY")
        atm = [r for r in table if r["kind"] == "atm"]
        self.assertEqual([r["label"] for r in atm], ["ATMF"])
        self.assertEqual(smile_points(table)["ATM"], atm[0]["vol"])

    def test_the_triangle_reads_a_tenor_past_the_boundary(self):
        rows = analytics.triangle_table(self.book, "EURJPY", cut="NY", tenors=["1y", "2y"])
        for r in rows:
            self.assertEqual([w for w in r.warnings if "could not" in w], [], r.tenor)
            self.assertIn("fly25", r.marked, r.tenor)
            self.assertIn("atm", r.noise, r.tenor)

    def test_the_sabr_shape_reads_a_tenor_past_the_boundary(self):
        s = self.book["EURJPY"]
        t = s.tenor_years("2y")
        expiry = self.book.clock.datetime_from_years(t)
        warn = []
        got = analytics._sabr_shape(s, expiry, t, float(s.atm_vol(expiry, "NY")), 0.25,
                                    None, "NY", warn)
        self.assertIsNotNone(got, warn)


class TestCrossCorrelation(unittest.TestCase):
    """A cross's correlation at each tenor, beside the legs' realized one."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURJPY"])
        cls.history = history.load_history(HISTORY, cls.book.pairs)

    def test_the_realized_correlation_closes_the_variance_triangle(self):
        """Zero-mean, on the days both legs hold: the one estimator for which
        the cross's own realized volatility is the triangle of the legs' at
        this correlation.  The sample's EURJPY is EURUSD x USDJPY, so the
        three sheets agree to their rounding."""
        h = self.history
        rc = history.realized_correlation(h["EURUSD"], h["USDJPY"], 180)
        vols = [history.realized(h[p], 180, basis="spot", annualisation="count").vol
                for p in ("EURUSD", "USDJPY", "EURJPY")]
        va, vb, vc = vols
        self.assertEqual(rc.basis, "spot")
        self.assertAlmostEqual(vc * vc, va * va + vb * vb + 2.0 * rc.rho * va * vb, delta=1e-4 * vc * vc)
        # The legs as they are quoted: the sample is simulated at +0.40
        # between EURUSD and USDJPY, which is the number the book marks.
        whole = history.realized_correlation(h["EURUSD"], h["USDJPY"], 10_000)
        self.assertLess(abs(whole.rho - 0.40), 3.0 * whole.rho_se)

    def test_only_the_days_both_sheets_hold_are_paired(self):
        a, b = self.history["EURUSD"], self.history["USDJPY"]
        full = history.realized_correlation(a, b, 120)
        i, j = b.window(120)
        keep = [k for k in range(len(b.dates)) if k != j - 10]
        thin = history.PairHistory(pair=b.pair, dates=[b.dates[k] for k in keep],
                                   spot=b.spot[keep])
        got = history.realized_correlation(a, thin, 120)
        self.assertEqual(got.observations, full.observations - 1)
        self.assertTrue(any("one sheet and not the other" in w for w in got.warnings))

    def test_auto_is_spot_on_both_legs_when_one_cannot_build_the_forward(self):
        a, b = self.history["EURUSD"], self.history["USDJPY"]
        bare = history.PairHistory(pair=b.pair, dates=list(b.dates), spot=b.spot)
        got = history.realized_correlation(a, bare, 180, basis="auto", basis_tenor="3M")
        self.assertEqual(got.basis, "spot")
        self.assertAlmostEqual(got.rho, history.realized_correlation(a, b, 180).rho, places=12)
        self.assertTrue(any("spot rather than the forward" in w for w in got.warnings))
        with self.assertRaises(history.HistoryError):
            history.realized_correlation(a, bare, 180, basis="forward", basis_tenor="3M")
        fwd = history.realized_correlation(a, b, 180, basis="auto", basis_tenor="3M")
        self.assertEqual(fwd.basis, "forward")

    def test_the_marked_column_is_the_curve_and_needs_no_history(self):
        table = analytics.correlation_table(self.book, "EURJPY", None)
        curve = self.book["EURJPY"].atm
        self.assertEqual(table.legs, ("EURUSD", "USDJPY"))
        self.assertIn("no historical workbook", table.unavailable)
        self.assertEqual([r.tenor for r in table.rows], list(self.book.data.tenor_points))
        for r in table.rows:
            self.assertAlmostEqual(r.marked, float(curve.correlation(r.t)), places=15)
            self.assertIsNone(r.realized)

    def test_the_window_matches_each_tenor_unless_a_number_is_given(self):
        matched = analytics.correlation_table(self.book, "EURJPY", self.history)
        fixed = analytics.correlation_table(self.book, "EURJPY", self.history, lookback_days=90)
        self.assertEqual(matched.unavailable, "")
        by = {r.tenor.upper(): r for r in matched.rows}
        self.assertAlmostEqual(by["1Y"].window_days, by["1Y"].t * 365.2425)
        self.assertGreater(by["1Y"].observations, by["3M"].observations)
        # A one-week window cannot hold enough returns: the row stays, with why.
        self.assertIsNone(by["1W"].realized)
        self.assertIn("at least", by["1W"].error)
        for r in fixed.rows:
            self.assertEqual(r.window_days, 90.0)
            self.assertIsNotNone(r.realized, r.tenor)
            self.assertAlmostEqual(r.difference, r.marked - r.realized, places=15)

    def test_a_pair_that_is_not_a_cross_is_refused(self):
        with self.assertRaises(ValueError):
            analytics.correlation_table(self.book, "EURUSD", self.history)

    def test_the_route_reads_the_lookback_box_and_writes_nothing(self):
        from volkit.webapp import BookService
        service = BookService(str(book_for("EURJPY", "EURUSD", "USDJPY")), ASOF,
                              history_path=str(HISTORY))
        out = service.cross_correlation({"pair": "eurjpy", "lookback_days": "match"})
        self.assertIsNone(out["lookback_days"])
        self.assertEqual(out["legs"], ["EURUSD", "USDJPY"])
        self.assertEqual(out["unavailable"], "")
        self.assertEqual([r["window_days"] for r in service.cross_correlation(
            {"pair": "EURJPY", "lookback_days": "60"})["rows"]][:1], [60.0])
        for bad in ("soon", "0", "-5", "nan"):
            with self.assertRaises(ValueError, msg=bad):
                service.cross_correlation({"pair": "EURJPY", "lookback_days": bad})
        self.assertFalse(service.dirty)
        bare = BookService(str(book_for("EURJPY", "EURUSD", "USDJPY")), ASOF)
        self.assertIn("no historical workbook",
                      bare.cross_correlation({"pair": "EURJPY"})["unavailable"])


class TestAnalysisApi(unittest.TestCase):
    def test_the_payload_carries_no_number_a_browser_cannot_parse(self):
        """Python's json writes NaN, which JSON.parse refuses.

        One unavailable cell would take the whole response down in the
        browser, so non-finite floats become null on the way out.
        """
        import json as _json
        from volkit.webapp import BookService, _finite
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED),
                              history_path=str(HISTORY))
        payload = service.analysis({"pair": "EURUSD", "cut": "NY", "horizon_days": "30",
                                    "lookback_days": "match", "noise": "0"})
        text = _json.dumps(_finite(payload), default=str)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(_json.loads(text)["pair"], "EURUSD")

    def test_a_cross_carries_its_dependence_through_both_routes(self):
        """The triangle's dependence columns and the implied quotes' reach the
        browser finite, and the page's *implied vol-vol* switch reaches the
        triangle."""
        import json as _json
        from volkit.webapp import BookService, _finite
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED))
        payload = service.analysis({"pair": "EURJPY", "cut": "NY", "noise": "0",
                                    "implied": "0"})
        text = _json.dumps(_finite(payload), default=str)
        self.assertNotIn("NaN", text)
        rows = _json.loads(text)["triangle"]
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNone(r["implied_vol_vol"], r["tenor"])
            self.assertIn("copula_rho", r)
            self.assertEqual((r["vol_vol"], r["corr_vol"], r["gaussian"]), (None, 0.0, {}))
        quotes = service.cross_quotes({"pair": "EURJPY", "cut": "NY"})
        for r in quotes["rows"]:
            self.assertEqual((r["vol_vol"], r["corr_vol"]), (None, 0.0), r["tenor"])
            if not r["error"]:
                self.assertAlmostEqual(r["copula_rho"], r["rho"], places=12)

    def test_sections_fail_independently(self):
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)      # no feed, no history
        payload = service.analysis({"pair": "EURUSD", "cut": "NY"})
        self.assertTrue(payload["carry"])               # still rolls, in moneyness
        self.assertIsNone(payload["realized"])
        self.assertIn("history", payload["unavailable"])
        self.assertIn("triangle", payload["unavailable"])


class TestRelativeValue(unittest.TestCase):
    """Scoring the expiry / strike surface.

    The grid is not a new model: every signal is one of the comparisons the
    Analysis screen already makes, read at a strike instead of at the
    at-the-money.  What is pinned here is that it stays that way -- the
    at-the-money column has to reproduce the fair-value table exactly, the
    three additive signals have to add to the richness, and a signal a cell
    does not have has to be renormalised away rather than counted as a zero.
    """

    @classmethod
    def setUpClass(cls):
        from volkit import relvalue
        cls.relvalue = relvalue
        cls.book = Book.from_excel(BOOK, ASOF).load_all()
        cls.book.feed = MarketFeed.load(FEED)
        cls.history = history.load_history(HISTORY, cls.book.pairs)
        cls.grid = relvalue.relative_value(cls.book, "EURUSD", cls.history["EURUSD"],
                                           horizon_days=7, cut="NY")

    def test_the_at_the_money_column_reproduces_the_fair_value_table(self):
        """Two ways of computing one number is how they drift apart.

        The grid extends ``fair_value_table``'s break-even from the
        at-the-money to a strike; at the at-the-money itself it has to be the
        same arithmetic, to the last bit, or the screen is showing a richness
        in one card and a different richness in the next.
        """
        fair = {r.tenor: r for r in analytics.fair_value_table(
            self.book, "EURUSD", self.history["EURUSD"], horizon_days=7, cut="NY")}
        checked = 0
        for row in self.grid.rows:
            cell = [c for c in row.cells if c.column == "atm"]
            got = fair.get(row.tenor)
            if not cell or cell[0].richness is None or got is None or got.richness is None:
                continue
            self.assertAlmostEqual(cell[0].richness, got.richness, places=12, msg=row.tenor)
            checked += 1
        self.assertGreater(checked, 3, "no tenor was actually compared")

    def test_the_three_additive_signals_add_to_the_richness(self):
        """``level + shape + carry`` is ``implied(K) - fair(K)`` and nothing else.

        The other two answer different questions -- where the mark sits in its
        own history, and what the legs of a cross imply -- and adding them in
        would make the volatility-point column mean nothing.
        """
        seen = 0
        for row in self.grid.rows:
            for cell in row.cells:
                by = {s.name: s for s in cell.signals}
                parts = [by[n].value for n in self.relvalue.ADDITIVE]
                if any(p is None for p in parts):
                    self.assertIsNone(cell.richness, f"{row.tenor} {cell.column}")
                    continue
                self.assertAlmostEqual(cell.richness, sum(parts), places=15)
                seen += 1
        self.assertGreater(seen, 10)

    def test_the_at_the_money_carries_no_shape_by_statement(self):
        """Zero because the at-the-money *is* the level, not because two
        near-equal numbers happened to cancel."""
        for row in self.grid.rows:
            for cell in row.cells:
                if cell.column != "atm":
                    continue
                shape = [s for s in cell.signals if s.name == "shape"][0]
                if shape.value is None:
                    continue
                self.assertEqual(shape.value, 0.0, row.tenor)
                self.assertIn("level", shape.message)

    def test_the_at_the_money_shape_is_shown_and_not_averaged_in(self):
        """A statement is not a measurement.

        The at-the-money's shape is zero by construction, and averaging that
        zero into the score pulled every at-the-money cell a fifth of the way
        to the middle -- the same failure the module refuses when a signal is
        *missing*, arriving through the one signal that is present.  It is
        still reported, with its value and its reason, because "zero" and "not
        measured" are different answers.
        """
        checked = 0
        for row in self.grid.rows:
            for cell in row.cells:
                if cell.column != "atm" or cell.score is None:
                    continue
                shape = [s for s in cell.signals if s.name == "shape"][0]
                if shape.value is None:
                    continue
                self.assertEqual(shape.value, 0.0, row.tenor)
                self.assertFalse(shape.used, row.tenor)
                self.assertNotIn("shape", cell.used, row.tenor)
                rest = [s for s in cell.signals if s.used]
                self.assertAlmostEqual(
                    cell.score,
                    sum(s.weight * s.value for s in rest) / sum(s.weight for s in rest),
                    places=12, msg=row.tenor)
                checked += 1
        self.assertGreater(checked, 2)

    def test_the_shape_signal_survives_a_short_realized_lookback(self):
        """The bug: shape zero at the at-the-money and unavailable everywhere else.

        The comparison smile's ``(rho, nu)`` were measured over the realized
        lookback, which needs *more* paired observations than a realized
        volatility needs returns.  Set the lookback to three weeks and the
        level signal went on working while the shape signal was blank at every
        strike of every tenor -- which reads as a signal that does not work
        rather than as a window that is too short.  They come off the history
        window now, for the same reason the scale does.
        """
        grid = self.relvalue.relative_value(
            self.book, "EURUSD", self.history["EURUSD"], horizon_days=7, cut="NY",
            lookback_days=21)
        wings = 0
        for row in grid.rows:
            if row.realized_rho is None:
                continue
            self.assertGreaterEqual(row.dynamics_days, history.DYNAMICS_DAYS, row.tenor)
            for cell in row.cells:
                if cell.column == "atm":
                    continue
                shape = [s for s in cell.signals if s.name == "shape"][0]
                self.assertIsNotNone(shape.value, f"{row.tenor} {cell.column}")
                wings += 1
        self.assertGreater(wings, 10, "no wing was actually scored on its shape")

    def test_the_score_is_the_weighted_mean_of_the_signals_it_used(self):
        """In **volatility points**, and of no others.

        The score was the weighted mean of the *z-scores* until 2026-08-31 and
        the desk asked for the points: how unusual a difference is is a
        statistic about a series, and how much you are being paid is the
        number the mark is moved by.  A missing signal is still renormalised
        away rather than counted as a zero, which would drag every score
        toward the middle.
        """
        scored = 0
        for row in self.grid.rows:
            for cell in row.cells:
                if cell.score is None:
                    continue
                used = [s for s in cell.signals if s.used]
                self.assertEqual(sorted(s.name for s in used), sorted(cell.used))
                total = sum(s.weight for s in used)
                self.assertAlmostEqual(
                    cell.score, sum(s.weight * s.value for s in used) / total, places=12)
                self.assertAlmostEqual(cell.confidence,
                                       total / sum(self.grid.weights.values()), places=12)
                scored += 1
        self.assertGreater(scored, 10)

    def test_the_score_is_on_the_same_footing_as_the_richness_beside_it(self):
        """One unit across the whole card.

        The three additive signals sum to the richness, so a cell scored on
        exactly those three at equal-enough weights lands within their own
        range.  What is pinned is the weaker and more useful thing: the score
        is never outside the span of the values it averaged, which a weighted
        mean cannot be and a weighted mean of z-scores plainly could.
        """
        checked = 0
        for row in self.grid.rows:
            for cell in row.cells:
                if cell.score is None:
                    continue
                vals = [s.value for s in cell.signals if s.used]
                self.assertGreaterEqual(cell.score, min(vals) - 1e-12, row.tenor)
                self.assertLessEqual(cell.score, max(vals) + 1e-12, row.tenor)
                checked += 1
        self.assertGreater(checked, 10)

    def test_a_signal_carries_its_z_wherever_there_is_a_scale_and_says_why_not(self):
        """The z is reported beside the value and scored on by nothing.

        It used to be the score, so it was present exactly where a signal was
        used.  Now it is the reading of *how unusual* the value is, so it
        follows the scale instead: present wherever the history can measure
        one, absent where it cannot, used or not.
        """
        for row in self.grid.rows:
            for cell in row.cells:
                for sig in cell.signals:
                    if sig.value is not None and cell.scale is not None:
                        self.assertIsNotNone(sig.z, f"{row.tenor} {cell.column} {sig.name}")
                        self.assertAlmostEqual(sig.z, sig.value / cell.scale, places=12)
                    else:
                        self.assertIsNone(sig.z)
                    if not sig.used:
                        self.assertTrue(sig.message or cell.message,
                                        f"{row.tenor} {cell.column} {sig.name} is silent")

    def test_the_carry_signal_does_not_carry_the_option_s_own_direction(self):
        """The bug this was written for: the score flipped sign across a row.

        The carry signal used to be built on ``carry_pnl``, the whole
        revaluation of the column's option at the rolled forward.  At a strike
        with any delta on it that is dominated by ``delta * (F2 - F1)`` -- a
        directional number with nothing to say about a volatility -- and by
        put-call parity it is equal and *opposite* for the call columns and
        the put columns.  So one row of the grid was pushed rich on one side
        and cheap on the other, the composite changed sign somewhere between
        them, and the at-the-money column barely showed it because a
        delta-neutral straddle has almost no first-order term.  On the sample
        marks a USDJPY one-year 25 delta put scored ``+13.8`` against the call's
        ``-0.46``, and 30 basis points of forward carry was the whole of it.

        Delta hedged, what is left is the gamma over the move: the same
        positive number whichever side the strike is written as, so the signal
        can no longer split a row by direction.
        """
        h = 7 / 365.2425
        rows = {col.name: {r.tenor: r for r in analytics.carry_table(
            self.book, "USDJPY", horizon_days=7, target=col.target, cut="NY")}
            for col in self.relvalue.COLUMNS}
        split = 0
        for tenor in self.book.data.tenor_points:
            here = [rows[c][tenor] for c in rows if rows[c][tenor].carry_pnl is not None]
            if len(here) < len(self.relvalue.COLUMNS):
                continue
            # The forward's half of the signal, column by column.  Hedged it is
            # a gamma and is one sign right across the row, so it cannot be
            # what separates the wings; unhedged it took the sign of each
            # column's own delta, and did.
            hedged = [r.carry_hedged * (r.t / h) / r.vega for r in here]
            raw = [r.carry_pnl * (r.t / h) / r.vega for r in here]
            self.assertEqual(len({v >= 0 for v in hedged}), 1, msg=tenor)
            if len({v >= 0 for v in raw}) > 1:
                split += 1
                self.assertGreater(max(raw) - min(raw), 5.0 * (max(hedged) - min(hedged)),
                                   msg=tenor)
        self.assertGreater(split, 3, "the old reading split the row at most tenors")

        # And on the grid itself: the whole carry signal, roll included, now
        # spans less than a volatility point across a row.
        grid = self.relvalue.relative_value(self.book, "USDJPY", self.history["USDJPY"],
                                            horizon_days=7, cut="NY")
        checked = 0
        for row in grid.rows:
            values = [c.signal["carry"].value for c in row.cells]
            if any(v is None for v in values):
                continue
            self.assertLess(max(values) - min(values), 0.01, msg=row.tenor)
            checked += 1
        self.assertGreater(checked, 4)

    def test_the_scale_is_measured_over_its_own_window_not_the_realized_lookback(self):
        """The bug this was written for.

        Scoring divided every signal by the standard deviation of the *same*
        window the realized volatility was measured over, which is matched to
        each tenor.  A month of a one-month at-the-money is a handful of
        observations of a smooth series, and an ordinary half point of
        richness came out at thirty standard deviations.  How much a
        volatility moves is a slower measurement and gets its own window.
        """
        short = self.relvalue.relative_value(
            self.book, "EURUSD", self.history["EURUSD"], horizon_days=7, cut="NY",
            history_days=40)
        long = self.relvalue.relative_value(
            self.book, "EURUSD", self.history["EURUSD"], horizon_days=7, cut="NY",
            history_days=500)
        pairs = 0
        for a, b in zip(short.rows, long.rows):
            for ca, cb in zip(a.cells, b.cells):
                if ca.scale is None or cb.scale is None:
                    continue
                self.assertNotAlmostEqual(ca.scale, cb.scale, places=6)
                # Same volatility points either way: only the denominator moved.
                if ca.richness is not None:
                    self.assertAlmostEqual(ca.richness, cb.richness, places=15)
                pairs += 1
        self.assertGreater(pairs, 10)
        self.assertNotEqual(short.history_days, long.history_days)

    def test_a_wing_the_sheet_does_not_quote_borrows_a_scale_and_says_so(self):
        """A substituted denominator is still a substitution.

        A z-score is only as meaningful as what it was divided by, so the cell
        names the series the scale came from rather than quietly using another
        one.
        """
        borrowed = [c for r in self.grid.rows for c in r.cells
                    if c.scale is not None and c.scale_source not in ("", c.column)]
        for cell in borrowed:
            self.assertEqual(cell.scale_source, "atm")
            self.assertIsNone(cell.history_mean, "a borrowed scale is not a history")

    def test_without_a_history_what_can_be_measured_is_still_scored(self):
        """The bug the change to volatility points fixed.

        A score in standard deviations needed a scale, and a scale needed the
        historical sheet -- so a pair the sheet does not quote scored nothing
        at all, in every cell, while its carry had been measured perfectly
        well.  In volatility points the scale is context and not the
        denominator: the ``history`` signal goes (it *is* the history) and so
        does every z, and what is left is scored on its own points.
        """
        grid = self.relvalue.relative_value(self.book, "EURUSD", None, horizon_days=7, cut="NY")
        self.assertIn("history", grid.unavailable)
        self.assertIsNotNone(grid.summary["mean_score"])
        measured = 0
        for row in grid.rows:
            for cell in row.cells:
                self.assertIsNone(cell.scale)
                for sig in cell.signals:
                    self.assertIsNone(sig.z, f"{row.tenor} {cell.column} {sig.name}")
                hist = [s for s in cell.signals if s.name == "history"][0]
                self.assertFalse(hist.used)
                self.assertIsNone(hist.value)
                carry = [s for s in cell.signals if s.name == "carry"][0]
                if carry.value is not None:
                    self.assertTrue(carry.used, f"{row.tenor} {cell.column}")
                    self.assertIn("carry", cell.used)
                    self.assertIsNotNone(cell.score)
                    measured += 1
        self.assertGreater(measured, 10)

    def test_the_row_keeps_the_whole_realized_measurement_not_one_field_of_it(self):
        """The bug this was written for.

        The grid kept ``stats.vol`` and threw away the decomposition
        ``history.realized`` had already measured, so a cell scored rich on
        ``level`` gave no way to say whether the richness was genuine forward
        variance or a level comparison against a thin estimate.
        """
        checked = 0
        for row in self.grid.rows:
            if row.realized is None:
                continue
            stats = history.realized(self.history["EURUSD"], row.window_days,
                                     annualisation="weighted", basis="auto",
                                     basis_tenor=row.tenor)
            self.assertAlmostEqual(row.realized_spot, stats.vol_spot, places=15)
            self.assertAlmostEqual(row.realized_forward, stats.vol_forward, places=15)
            self.assertAlmostEqual(row.points_vol, stats.points_vol, places=15)
            self.assertAlmostEqual(row.points_correlation, stats.points_correlation, places=15)
            self.assertAlmostEqual(row.realized_carry_rate, stats.carry_rate, places=15)
            checked += 1
        self.assertGreater(checked, 3)

    def test_the_vol_support_number_is_the_ratio_and_not_the_level_of_the_points(self):
        """A large carry says nothing on its own about whether the forward is
        more volatile than spot.  What answers that is the ratio of the two
        realized volatilities, so the ratio is what the row carries."""
        rows = [r for r in self.grid.rows if r.forward_vol_ratio is not None]
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(row.forward_vol_ratio,
                                   row.realized_forward / row.realized_spot, places=12)
            # The sample's swap points barely move, so the forward and spot
            # measurements are the same variance -- which is the reading, and
            # it is nothing like the -1.8%/yr level of the carry beside it.
            self.assertAlmostEqual(row.forward_vol_ratio, 1.0, places=2)
            self.assertLess(row.realized_carry_rate, -0.01)

    def test_no_row_carries_a_non_finite_number_out_of_the_decomposition(self):
        """``history.realized`` uses nan for 'not measured on this basis' in
        some fields and None in others; either one reaching JSON would take
        the whole grid down in the browser."""
        for row in self.grid.rows:
            for name in ("realized_spot", "realized_forward", "points_vol",
                         "points_correlation", "realized_carry_rate", "forward_vol_ratio"):
                value = getattr(row, name)
                if value is not None:
                    self.assertTrue(math.isfinite(value), f"{row.tenor} {name}")

    def test_the_triangle_is_read_at_the_strike_the_cross_is_marked_at(self):
        """``triangle_table`` compares an at-the-money, a risk reversal and a
        butterfly; a call at that delta is ``atm + fly + rr/2``, so the same
        combination of the differences is the difference at its strike."""
        rows = {r.tenor: r for r in analytics.triangle_table(
            self.book, "EURJPY", cut="NY")}
        grid = self.relvalue.relative_value(self.book, "EURJPY", self.history["EURJPY"],
                                            horizon_days=7, cut="NY")
        checked = 0
        for row in grid.rows:
            tri = rows.get(row.tenor)
            if tri is None:
                continue
            for cell in row.cells:
                sig = [s for s in cell.signals if s.name == "triangle"][0]
                if sig.value is None:
                    continue
                d = tri.difference
                if cell.column == "atm":
                    want = d["atm"]
                else:
                    tag = f"{int(round(cell.delta * 100))}"
                    want = d["atm"] + d[f"fly{tag}"] + (0.5 if cell.is_call else -0.5) * d[f"rr{tag}"]
                self.assertAlmostEqual(sig.value, want, places=15,
                                       msg=f"{row.tenor} {cell.column}")
                checked += 1
        self.assertGreater(checked, 10)

    def test_a_difference_inside_the_triangle_noise_floor_is_shown_but_not_scored(self):
        """That section's own rule: a difference smaller than what the
        machinery gets wrong on the legs alone is not a difference.  Scoring
        it anyway would put the reconstruction error into the answer."""
        real = analytics.triangle_table(self.book, "EURJPY", cut="NY")
        loud = [analytics.TriangleRow(
            tenor=r.tenor, t=r.t, rho=r.rho, coefficients=r.coefficients, marked=r.marked,
            triangle=r.triangle, difference=r.difference,
            noise={k: 10.0 for k in r.difference}, variance_triangle_atm=r.variance_triangle_atm,
            smile_convexity=r.smile_convexity, leg_atm=r.leg_atm,
            implied_correlation=r.implied_correlation) for r in real]
        old = self.relvalue.triangle_table
        self.relvalue.triangle_table = lambda *a, **k: loud
        try:
            grid = self.relvalue.relative_value(self.book, "EURJPY", self.history["EURJPY"],
                                                horizon_days=7, cut="NY")
        finally:
            self.relvalue.triangle_table = old
        seen = 0
        for row in grid.rows:
            for cell in row.cells:
                sig = [s for s in cell.signals if s.name == "triangle"][0]
                if sig.value is None:
                    continue
                self.assertFalse(sig.used, f"{row.tenor} {cell.column}")
                self.assertIn("noise floor", sig.message)
                self.assertNotIn("triangle", cell.used)
                seen += 1
        self.assertGreater(seen, 10)

    def test_a_pair_that_is_not_a_cross_has_no_triangle_and_is_not_charged_for_it(self):
        for row in self.grid.rows:
            for cell in row.cells:
                sig = [s for s in cell.signals if s.name == "triangle"][0]
                self.assertIsNone(sig.value)
                self.assertIn("not a cross", sig.message)
        self.assertIn("not a cross", self.grid.unavailable["triangle"])

    def test_a_tenor_that_cannot_be_rolled_keeps_its_row_and_carries_the_reason(self):
        """Dropping it makes a short grid look like a complete one."""
        grid = self.relvalue.relative_value(self.book, "EURUSD", self.history["EURUSD"],
                                            horizon_days=90, cut="NY")
        self.assertEqual([r.tenor for r in grid.rows], list(self.book.data.tenor_points))
        blocked = [(r, c) for r in grid.rows for c in r.cells
                   if [s for s in c.signals if s.name == "carry"][0].value is None]
        self.assertTrue(blocked)
        for _, cell in blocked:
            carry = [s for s in cell.signals if s.name == "carry"][0]
            self.assertTrue(carry.message)

    def test_the_carry_horizon_and_the_dominance_threshold_are_one_statement(self):
        """``T = 0.64 * sigma**2 / c**2`` is exactly where ``z`` reaches 0.8.

        The factor is the threshold squared, so it is derived from it rather
        than written down twice: two constants that could drift apart would
        put the annotation and the horizon on opposite sides of the line.
        """
        rv = self.relvalue
        self.assertAlmostEqual(rv.CARRY_HORIZON_FACTOR, rv.CARRY_DOMINANT_Z ** 2, places=15)
        sigma, carry, spot = 0.10, 0.06, 1.25
        at_a_year = rv._regime(spot, spot * math.exp(carry * 1.0), 1.0, sigma, None)
        horizon = at_a_year["carry_horizon_days"] / 365.2425
        at_the_horizon = rv._regime(spot, spot * math.exp(carry * horizon), horizon,
                                    sigma, None)
        self.assertAlmostEqual(at_the_horizon["regime_z"], rv.CARRY_DOMINANT_Z, places=12)
        # The claim is that the horizon is where the two cross, so it is
        # tested either side of it rather than exactly on it: the boundary
        # itself lands within a rounding error of the threshold.
        for scale, dominant in ((1.02, True), (0.98, False)):
            t = horizon * scale
            side = rv._regime(spot, spot * math.exp(carry * t), t, sigma, None)
            self.assertEqual(side["carry_dominant"], dominant, f"at {scale:g} of the horizon")
        # And the sign of the carry does not decide the regime: a discount and
        # a premium of the same size are the same distance travelled.
        mirrored = rv._regime(spot, spot * math.exp(-carry * horizon), horizon, sigma, None)
        self.assertAlmostEqual(mirrored["regime_z"], at_the_horizon["regime_z"], places=12)

    def test_a_carry_to_volatility_ratio_alone_would_flag_a_free_float(self):
        """The bug in the obvious version of this test.

        Read as "carry over volatility" alone, USDJPY on a five point rate
        differential and ten volatility points scores 0.53 -- right beside
        USDCNH's 0.50 -- and USDJPY is not managed in any sense.  What
        separates them is the second condition: a managed float's realized
        volatility is *low in absolute terms*, which is the suppressed
        diffusion itself rather than a consequence of it.
        """
        rv = self.relvalue

        class Row:
            def __init__(self, ratio, vol):
                self.carry_to_vol, self.realized = ratio, vol

        def verdict(ratio, vol):
            return rv.suppressed_diffusion([Row(ratio, vol)] * 3)

        # The ratio alone does not separate these two.
        self.assertGreater(verdict(0.53, 0.095)["carry_to_vol"],
                           verdict(0.50, 0.050)["carry_to_vol"])
        self.assertFalse(verdict(0.53, 0.095)["managed"], "USDJPY is not a managed float")
        self.assertTrue(verdict(0.50, 0.050)["managed"], "USDCNH has the shape")
        # A high-carry, high-volatility pair is deliberately outside it: its
        # diffusion is not suppressed, it is merely expensive.
        self.assertFalse(verdict(1.40, 0.250)["managed"])
        # And an ordinary free float fails the first condition instead.
        self.assertFalse(verdict(0.25, 0.060)["managed"])
        self.assertFalse(verdict(None, None)["managed"])

    def test_a_carry_dominated_tenor_warns_and_marks_the_signal_it_dominates(self):
        """Said where the signal is read, not only in the row's warnings."""
        import tempfile
        from volkit.feed import MarketFeed
        rv = self.relvalue
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steep.csv"
            path.write_text(
                "# a deliberately steep curve: EURUSD at a 7%/yr discount\n"
                "EURUSD,SPOT,1.08000\n"
                "EURUSD,1M,-60.0\nEURUSD,3M,-180.0\n"
                "EURUSD,6M,-360.0\nEURUSD,1Y,-720.0\n", encoding="utf-8")
            old = self.book.feed
            self.book.feed = MarketFeed.load(path)
            self.addCleanup(setattr, self.book, "feed", old)
            grid = rv.relative_value(self.book, "EURUSD", self.history["EURUSD"],
                                     horizon_days=7, cut="NY")
        hot = [r for r in grid.rows if r.carry_dominant]
        self.assertTrue(hot, "a 7%/yr carry against a 6% mark should dominate by one year")
        for row in hot:
            self.assertGreaterEqual(row.regime_z, rv.CARRY_DOMINANT_Z)
            self.assertTrue(any("carry trade" in w for w in row.warnings), row.tenor)
            for cell in row.cells:
                carry = [s for s in cell.signals if s.name == "carry"][0]
                if carry.value is not None:
                    self.assertIn("carry dominated", carry.message)
        # The weight is not tapered on the strength of it: the score is still
        # the weighted mean of what it used, and the weights are the desk's.
        for row in hot:
            for cell in row.cells:
                used = [s for s in cell.signals if s.used]
                if not used:
                    continue
                total = sum(s.weight for s in used)
                self.assertAlmostEqual(
                    cell.score, sum(s.weight * s.value for s in used) / total, places=12)
                for sig in used:
                    self.assertEqual(sig.weight, grid.weights[sig.name])

    def test_the_forward_comes_from_the_feed_before_the_carry_table(self):
        """A tenor the horizon cannot roll still has a forward and a regime.

        Reading it off the carry table alone left every tenor shorter than the
        horizon with no forward, and therefore no absolute strikes, on a pair
        whose forward the feed was quoting perfectly well.
        """
        grid = self.relvalue.relative_value(self.book, "EURUSD", self.history["EURUSD"],
                                            horizon_days=90, cut="NY")
        unrollable = [r for r in grid.rows
                      if all([s for s in c.signals if s.name == "carry"][0].value is None
                             for c in r.cells) and r.cells]
        self.assertTrue(unrollable, "no tenor was blocked by a 90-day horizon")
        for row in unrollable:
            self.assertIsNotNone(row.forward, row.tenor)
            self.assertIsNotNone(row.spot, row.tenor)
            self.assertIsNotNone(row.regime_z, row.tenor)
            for cell in row.cells:
                self.assertIsNotNone(cell.strike, f"{row.tenor} {cell.column}")

    def test_a_shared_signal_is_one_number_for_the_row_and_is_marked_as_one(self):
        """``level`` is a statement about the level, and a level is one number
        per expiry.  Printed in five cells with nothing to tie them together,
        one at-the-money mispricing reads as five confirmations."""
        rv = self.relvalue
        spread = 0
        for row in self.grid.rows:
            if not row.cells:
                continue
            for name in rv.SHARED:
                values = [[s for s in c.signals if s.name == name][0].value
                          for c in row.cells]
                self.assertEqual(len(set(values)), 1, f"{row.tenor} {name} is not shared")
                zs = {[s for s in c.signals if s.name == name][0].z for c in row.cells}
                if len(zs) > 1:
                    spread += 1        # one number, but each cell's own scale
            for cell in row.cells:
                for sig in cell.signals:
                    self.assertEqual(sig.shared, sig.name in rv.SHARED, sig.name)
        self.assertGreater(spread, 2, "a shared value should still take each cell's scale")

    def test_the_state_response_names_the_shared_signal_for_the_page(self):
        """The page marks it without knowing which signal it happens to be."""
        from volkit.webapp import BookService
        from volkit.relvalue import SHARED
        state = BookService(str(BOOK), ASOF).state()
        signals = state["analysis"]["signals"]
        self.assertEqual({s["key"] for s in signals if s["shared"]}, set(SHARED))
        self.assertTrue(all("weight" in s for s in signals))

    def test_a_weight_that_is_not_a_signal_is_refused_not_ignored(self):
        with self.assertRaises(self.relvalue.RelativeValueError) as ctx:
            self.relvalue.resolve_weights({"gut_feel": 1.0})
        self.assertIn("gut_feel", str(ctx.exception))
        with self.assertRaises(self.relvalue.RelativeValueError):
            self.relvalue.resolve_weights({"level": "quite a lot"})
        with self.assertRaises(self.relvalue.RelativeValueError):
            self.relvalue.resolve_weights({"level": -1})
        with self.assertRaises(self.relvalue.RelativeValueError):
            self.relvalue.resolve_weights({k: 0 for k in self.relvalue.WEIGHTS})

    def test_a_reweighting_moves_the_score_and_nothing_else(self):
        heavy = self.relvalue.relative_value(
            self.book, "EURUSD", self.history["EURUSD"], horizon_days=7, cut="NY",
            weights={"carry": 5.0})
        moved = 0
        for a, b in zip(self.grid.rows, heavy.rows):
            for ca, cb in zip(a.cells, b.cells):
                self.assertAlmostEqual(ca.implied, cb.implied, places=15)
                if ca.richness is not None:
                    self.assertAlmostEqual(ca.richness, cb.richness, places=15)
                if ca.score is not None and "carry" in ca.used:
                    if abs(ca.score - cb.score) > 1e-9:
                        moved += 1
        self.assertGreater(moved, 5, "reweighting the carry changed no score")

    def test_the_panel_is_the_only_reader_of_the_request(self):
        panel = self.relvalue.panel_from_request({
            "pair": "EURUSD", "cut": "NY", "method": "SVI", "horizon_days": "7",
            "lookback_days": "match", "history_days": "250", "annualisation": "weighted",
            "realized_basis": "auto", "triangle": "0", "weights": {"carry": "0.4"}})
        self.assertEqual(panel.pair, "EURUSD")
        self.assertIsNone(panel.lookback_days)
        self.assertFalse(panel.with_triangle)
        self.assertEqual(panel.weights["carry"], 0.4)
        for bad in ({"pair": ""}, {"pair": "EURUSD", "history_days": "soon"},
                    {"pair": "EURUSD", "lookback_days": "-3"},
                    {"pair": "EURUSD", "weights": "level=1"}):
            with self.assertRaises(self.relvalue.RelativeValueError):
                self.relvalue.panel_from_request(bad)

    def test_the_command_line_and_the_screen_run_the_same_panel(self):
        """The CLI builds the panel the browser posts, so a cell quoted off
        the screen can be reproduced in a batch job."""
        from volkit.webapp import BookService
        request = {"pair": "EURUSD", "cut": "NY", "method": "SVI", "horizon_days": "7",
                   "lookback_days": "match", "history_days": "250", "triangle": "1"}
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED),
                              history_path=str(HISTORY))
        service.load_history({"path": str(HISTORY)})
        served = service.relative_value(request)
        direct = self.relvalue.panel_from_request(request).run(service.book, service.history)
        self.assertEqual(served["summary"]["headline"], direct.summary["headline"])
        self.assertEqual(served["pair"], "EURUSD")

    def test_the_payload_carries_no_number_a_browser_cannot_parse(self):
        """Python's json writes NaN and JSON.parse refuses it, so one
        unscored cell would take the whole grid down in the browser."""
        import json as _json
        from volkit.webapp import BookService, _finite
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED),
                              history_path=str(HISTORY))
        service.load_history({"path": str(HISTORY)})
        text = _json.dumps(_finite(service.relative_value(
            {"pair": "EURJPY", "cut": "NY", "horizon_days": "7"})), default=str)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(_json.loads(text)["pair"], "EURJPY")

    def test_the_route_belongs_to_the_analysis_screen(self):
        from volkit import screens
        owner = {r: s.name for s in screens.SCREENS for r in s.routes}
        self.assertEqual(owner["/api/relvalue"], "analysis")


class TestHistory(unittest.TestCase):
    """The historical workbook, and what the market actually did."""

    def test_headers_are_read_by_meaning_not_by_position(self):
        cases = {
            "Fwd 1M": ("forward", "1M", None),
            "1M swap points": ("points", "1M", None),
            "3m atm vol": ("atm", "3M", None),
            "RR25 1M": ("rr", "1M", 25),
            "1M 25d rr": ("rr", "1M", 25),
            "10RR 3M": ("rr", "3M", 10),
            "1M 10d fly": ("bf", "1M", 10),
            "implied vol 6m": ("atm", "6M", None),
        }
        for header, (field_name, tenor, delta) in cases.items():
            got = history.parse_header(header)
            self.assertIsNotNone(got, header)
            self.assertEqual((got.field, got.tenor, got.delta), (field_name, tenor, delta), header)

    def test_a_ten_delta_wing_is_not_a_ten_day_tenor(self):
        """'RR 10d 1M' has two tokens that parse as tenors.

        Deciding in token order files the whole column under a 10-day maturity
        that does not exist, and the ten-delta series then goes missing without
        a word.
        """
        got = history.parse_header("RR 10d 1M")
        self.assertEqual((got.field, got.tenor, got.delta), ("rr", "1M", 10))
        got = history.parse_header("BF 10d 6M")
        self.assertEqual((got.field, got.tenor, got.delta), ("bf", "6M", 10))

    def test_a_header_naming_nothing_is_reported_not_guessed(self):
        self.assertIsNone(history.parse_header("Trader note"))
        self.assertIsNone(history.parse_header("3M"))          # a tenor with no field

    def test_the_volatility_unit_is_decided_per_sheet_not_per_column(self):
        """A 25 delta risk reversal of -0.89 vol points is below 1 in magnitude.

        Sniffing each column on its own reads that as a decimal and returns it
        a hundred times too large, while the at-the-money column beside it is
        read correctly -- so the error shows up only in the skew.
        """
        h = history.load_history(HISTORY, ["EURUSD", "USDJPY", "EURJPY", "GBPUSD"])
        for pair in ("EURUSD", "USDJPY", "EURJPY"):
            atm = h[pair].series("atm", "3M")[-1]
            rr = h[pair].series("rr", "3M", 25)[-1]
            self.assertTrue(0.01 < atm < 0.50, f"{pair} ATM came back as {atm}")
            self.assertLess(abs(rr), 0.05, f"{pair} risk reversal came back as {rr}")

    def test_a_low_at_the_money_is_still_read_as_points(self):
        """A pegged pair marks its at-the-money below one volatility point.

        The reader used to call any sheet whose at-the-money sat under 1.0 a
        sheet of decimals, so USDHKD's 0.35 came back as 0.35 *decimal* and
        the monitor showed it at 35 vol points.  What the sheet says is what
        the number is: 0.35 points, and the risk reversal and butterfly beside
        it on the same scale.  A genuinely decimal sheet is something the
        caller says, with ``vol_unit='decimal'``.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pegged.xlsx"
            pd.DataFrame({
                "Date": pd.to_datetime(["2024-05-01", "2024-05-02", "2024-05-03"]),
                "Spot": [7.81, 7.812, 7.815],
                "3M ATM": [0.35, 0.36, 0.34],
                "3M 25d RR": [0.12, 0.13, 0.11],
                "3M 25d BF": [0.08, 0.08, 0.09],
            }).to_excel(path, sheet_name="USDHKD", index=False)
            h = history.load_history(path, ["USDHKD"])
            hist = h["USDHKD"]
            self.assertAlmostEqual(hist.series("atm", "3M")[-1], 0.0034)
            self.assertAlmostEqual(hist.series("rr", "3M", 25)[-1], 0.0011)
            self.assertAlmostEqual(hist.series("bf", "3M", 25)[-1], 0.0009)
            # It is the one reading somebody might have meant the other way,
            # so it is said once rather than guessed at in silence.
            self.assertTrue(any("read as written" in p for p in h.problems), h.problems)
            # And the other reading is still available, by name.
            dec = history.load_history(path, ["USDHKD"], vol_unit="decimal")
            self.assertAlmostEqual(dec["USDHKD"].series("atm", "3M")[-1], 0.34)

    def test_the_monitor_shows_a_low_at_the_money_as_it_is_written(self):
        """The same number, at the edge a person reads it at (§4).

        curves is decimals throughout and the page multiplies by 100, so a
        0.35 point at-the-money read as a decimal reached the monitor tile at
        35.00 -- a hundred times the mark, on the screen a desk opens first.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pegged.xlsx"
            pd.DataFrame({
                "Date": pd.to_datetime(["2024-05-01", "2024-05-02"]),
                "Spot": [7.81, 7.812],
                "3M ATM": [0.35, 0.36],
                "3M 25d RR": [0.12, 0.13],
            }).to_excel(path, sheet_name="USDHKD", index=False)
            h = history.load_history(path, ["USDHKD"])
            from volkit import curves
            curve = curves.history_curve(h["USDHKD"])
            point = curve.at("3M")
            self.assertAlmostEqual(point.values["atm"] * 100.0, 0.36)
            self.assertAlmostEqual(point.values["rr25"] * 100.0, 0.13)

    def test_forcing_the_unit_matches_what_auto_detected(self):
        a = history.load_history(HISTORY, ["EURUSD"])
        b = history.load_history(HISTORY, ["EURUSD"], vol_unit="percent")
        self.assertTrue(np.allclose(a["EURUSD"].series("atm", "3M"),
                                    b["EURUSD"].series("atm", "3M")))
        c = history.load_history(HISTORY, ["EURUSD"], vol_unit="decimal")
        self.assertTrue(np.allclose(c["EURUSD"].series("atm", "3M"),
                                    100.0 * a["EURUSD"].series("atm", "3M")))

    def test_unreadable_columns_are_named(self):
        h = history.load_history(HISTORY, ["EURUSD", "USDJPY", "EURJPY", "GBPUSD"])
        self.assertTrue(any("Trader note" in p for p in h.problems))

    def test_forward_points_are_turned_into_outrights(self):
        """USDJPY's sheet quotes swap points; EURUSD's quotes outrights."""
        h = history.load_history(HISTORY, ["EURUSD", "USDJPY", "EURJPY", "GBPUSD"])
        jpy = h["USDJPY"]
        self.assertIn("3M", jpy.forwards)
        self.assertGreater(jpy.forwards["3M"][-1], 50.0)       # an outright, not points
        self.assertNotEqual(jpy.forwards["3M"][-1], jpy.spot[-1])

    def test_a_sheet_that_is_not_a_pair_is_skipped_with_a_reason(self):
        h = history.load_history(HISTORY, ["EURUSD", "USDJPY", "EURJPY", "GBPUSD"])
        self.assertEqual(sorted(h.pairs), ["EURJPY", "EURUSD", "GBPUSD", "USDJPY"])
        with self.assertRaises(history.HistoryError):
            h["AUDNZD"]

    def test_volatility_time_matches_the_models_own_integration(self):
        """Realized and implied must be measured on the same clock.

        A flat-backbone curve integrates to ``sigma * sqrt(voltime / t)``; if
        the window measure here did not agree with the one inside the model,
        every realized-against-implied number on the screen would be biased by
        the difference.
        """
        from volkit.timeweight import TimeWeighting
        curve = AtmCurve(pair="EURUSD",
                         params=BackboneParams(initial_vol=0.10, long_term_vol=0.10,
                                               mean_reversion=1.0),
                         clock=ASOF, weighting=TimeWeighting("EURUSD"))
        t = 0.5
        vt = history.volatility_time(
            "EURUSD", ASOF.now, ASOF.now + timedelta(days=t * 365.2425))
        self.assertAlmostEqual(curve.integrated_vol(t), 0.10 * math.sqrt(vt / t), places=4)

    def test_a_calendar_year_is_less_than_a_year_of_volatility_time(self):
        vt = history.volatility_time("EURUSD", datetime(2023, 2, 28, tzinfo=UTC),
                                     datetime(2024, 2, 28, tzinfo=UTC))
        self.assertLess(vt, 0.9)          # weekends and holidays are nearly free
        self.assertGreater(vt, 0.6)

    def test_the_three_annualisations_are_all_reported_and_differ(self):
        h = history.load_history(HISTORY, ["EURUSD"])
        r = history.realized(h["EURUSD"], 365)
        self.assertGreater(r.vol, r.vol_calendar)      # weighted time is shorter
        self.assertGreater(r.vol, r.vol_count)
        self.assertEqual(r.annualisation, "weighted")
        # The ratio between two of them is arithmetic, not an estimate.
        self.assertAlmostEqual(r.vol / r.vol_count,
                               math.sqrt((r.observations / 252.0) / r.vol_time), places=9)

    def test_the_count_annualisation_recovers_the_walk_it_was_simulated_at(self):
        """The sample is a lognormal walk with one step per business day."""
        h = history.load_history(HISTORY, ["EURUSD"])
        r = history.realized(h["EURUSD"], 730)
        self.assertAlmostEqual(r.vol_count, 0.0524, delta=0.006)

    def test_too_short_a_window_is_refused_with_the_count(self):
        h = history.load_history(HISTORY, ["EURUSD"])
        with self.assertRaises(history.HistoryError) as cm:
            history.realized(h["EURUSD"], 7)
        self.assertIn("observation", str(cm.exception))

    def test_skew_and_kurtosis_carry_their_standard_errors(self):
        h = history.load_history(HISTORY, ["EURUSD"])
        r = history.realized(h["EURUSD"], 365)
        self.assertAlmostEqual(r.skew_se, math.sqrt(6.0 / r.observations), delta=0.02)
        self.assertAlmostEqual(r.kurtosis_se, math.sqrt(24.0 / r.observations), delta=0.06)

    def test_shape_projects_onto_a_horizon_by_the_independence_rule(self):
        """Skewness falls as 1/sqrt(n) and excess kurtosis as 1/n in the steps."""
        h = history.load_history(HISTORY, ["EURUSD"])
        r = history.realized(h["EURUSD"], 365)
        t = 0.25
        n = t / (r.vol_time / r.observations)
        self.assertAlmostEqual(r.scaled_skew(t), r.skew / math.sqrt(n), places=12)
        self.assertAlmostEqual(r.scaled_excess_kurtosis(t), r.excess_kurtosis / n, places=12)
        self.assertLess(abs(r.scaled_skew(t)), abs(r.skew))

    # -- the swap points are part of what was realized --------------------

    def _synthetic(self, pair="EURUSD", n=400, vol=0.10, carry=0.05, points_vol=0.0, seed=7):
        """A sheet with a known spot walk and a known carry curve.

        ``carry`` is the annualised continuous carry the forward is built at;
        ``points_vol`` is how much that carry itself wobbles day to day.  With
        ``points_vol`` zero the forward's only extra motion is the *decay* of
        the points, which is a known slide and not a risk.
        """
        rng = np.random.default_rng(seed)
        dt = 1.0 / 252.0
        dates = [date(2022, 1, 3) + timedelta(days=int(i)) for i in range(n)]
        steps = rng.normal(0.0, vol * math.sqrt(dt), n - 1)
        spot = 1.10 * np.exp(np.concatenate(([0.0], np.cumsum(steps))))
        c = carry + (rng.normal(0.0, points_vol, n) if points_vol else 0.0)
        h = history.PairHistory(pair=pair, dates=dates, spot=spot)
        for tenor, tau in (("1M", 1.0 / 12.0), ("3M", 0.25), ("1Y", 1.0)):
            h.forwards[tenor] = spot * np.exp(c * tau)
        return h

    def test_a_pure_carry_decay_is_not_counted_as_volatility(self):
        """The points *decaying* by one day of carry is a known slide.

        Leaving it in the sum of squares books the carry itself as
        volatility, which is exactly backwards for the pairs the forward basis
        exists for -- so a forward built on a perfectly constant 5% carry must
        realize what spot realized, to the last digit.
        """
        h = self._synthetic(carry=0.05, points_vol=0.0)
        r = history.realized(h, 365, basis="forward", basis_tenor="1Y")
        self.assertEqual(r.basis, "forward")
        self.assertAlmostEqual(r.vol, r.vol_spot, places=12)
        self.assertAlmostEqual(r.carry_rate, 0.05, places=9)
        self.assertLess(r.points_vol, 1e-12)

    def test_the_swap_points_moving_is_realized_volatility(self):
        """A forward whose carry wobbles realizes more than spot did.

        The tenor multiplies it: a one-year forward carries a whole year of
        the carry's move, a one-month forward a twelfth of it.
        """
        h = self._synthetic(carry=0.05, points_vol=0.01)
        spot_only = history.realized(h, 365, basis="spot", basis_tenor="1Y")
        year = history.realized(h, 365, basis="forward", basis_tenor="1Y")
        month = history.realized(h, 365, basis="forward", basis_tenor="1M")
        self.assertGreater(year.vol, spot_only.vol)
        self.assertGreater(year.points_vol, month.points_vol * 5.0)
        # Independent by construction, so the variances add.
        self.assertAlmostEqual(year.vol ** 2,
                               year.vol_spot ** 2 + year.points_vol ** 2,
                               delta=0.05 * year.vol ** 2)

    def test_a_tenor_the_sheet_does_not_quote_is_interpolated_not_dropped(self):
        """Falling back to spot on the misses put two different measurements
        in one column, so the term structure of realized volatility grew steps
        at whichever tenors the sheet happened to quote."""
        h = self._synthetic(carry=0.05, points_vol=0.01)
        self.assertNotIn("6M", h.forwards)
        r = history.realized(h, 365, basis="auto", basis_tenor="6M")
        self.assertEqual(r.basis, "forward")
        self.assertTrue(any("interpolated" in w for w in r.warnings))
        three, one = (history.realized(h, 365, basis="forward", basis_tenor=t).points_vol
                      for t in ("3M", "1Y"))
        self.assertGreater(r.points_vol, three)
        self.assertLess(r.points_vol, one)

    def test_a_sheet_with_no_points_at_all_falls_back_and_says_so(self):
        h = self._synthetic()
        h.forwards.clear()
        r = history.realized(h, 365, basis="auto", basis_tenor="3M")
        self.assertEqual(r.basis, "spot")
        self.assertTrue(any("realized on spot" in w for w in r.warnings))
        with self.assertRaises(history.HistoryError):
            history.realized(h, 365, basis="forward", basis_tenor="3M")

    # -- what the volatility itself did -----------------------------------

    def test_vol_dynamics_recovers_the_correlation_and_vol_of_vol_it_was_built_with(self):
        """rho and nu are the two numbers a SABR smile is made of, and under
        beta = 1 they are directly measurable off the quoted at-the-money."""
        rng = np.random.default_rng(11)
        n, dt = 1500, 1.0 / 252.0
        rho, nu = -0.55, 0.60
        z1 = rng.normal(size=n - 1)
        z2 = rho * z1 + math.sqrt(1.0 - rho * rho) * rng.normal(size=n - 1)
        spot = 1.10 * np.exp(np.concatenate(([0.0], np.cumsum(0.10 * math.sqrt(dt) * z1))))
        vol = 0.10 * np.exp(np.concatenate(([0.0], np.cumsum(nu * math.sqrt(dt) * z2))))
        h = history.PairHistory(
            pair="EURUSD",
            dates=[date(2020, 1, 1) + timedelta(days=int(i)) for i in range(n)],
            spot=spot, atm={"3M": vol})
        d = history.vol_dynamics(h, 5000, "3M")
        self.assertEqual(d.source, "quoted")
        self.assertAlmostEqual(d.rho, rho, delta=3.0 * d.rho_se)
        # nu is per unit of *volatility time*, not per 252 business days, for
        # the same reason the realized volatility is: it has to be comparable
        # with the nu a marked smile implies.  The series above was built on a
        # flat 1/252 step, so recovering it means converting onto the model's
        # own clock first -- a calendar year holds about 0.78 years of it.
        per_step = d.vol_time / d.observations
        self.assertAlmostEqual(d.nu, nu * math.sqrt(dt / per_step), delta=0.08 * d.nu)
        self.assertLess(per_step, dt)

    def test_vol_dynamics_falls_back_to_a_rolling_volatility_and_warns(self):
        """A rolling average moves less than the thing it averages, so the
        fallback is a floor and has to say so."""
        rng = np.random.default_rng(3)
        n = 400
        spot = 1.10 * np.exp(np.cumsum(rng.normal(0.0, 0.006, n)))
        h = history.PairHistory(
            pair="EURUSD",
            dates=[date(2021, 1, 1) + timedelta(days=int(i)) for i in range(n)],
            spot=spot)
        d = history.vol_dynamics(h, 5000, "3M")
        self.assertEqual(d.source, "rolling")
        self.assertTrue(any("floor" in w for w in d.warnings))

    def test_a_tenor_the_sheet_does_not_quote_uses_the_nearest_one_by_name(self):
        """Interpolating a volatility column would be interpolating something
        whose *changes* are the measurement; the nearest real column is used."""
        h = self._synthetic()
        h.atm["3M"] = np.full(len(h.dates), 0.10) * np.exp(
            np.linspace(0.0, 0.3, len(h.dates)))
        d = history.vol_dynamics(h, 5000, "2M")
        self.assertEqual(d.tenor, "3M")
        self.assertTrue(any("3M column instead" in w for w in d.warnings))

    def test_a_percentile_locates_todays_mark_in_its_own_history(self):
        h = history.load_history(HISTORY, ["EURUSD"])
        series = h["EURUSD"].series("atm", "3M")
        lo, hi = float(np.min(series)), float(np.max(series))
        self.assertEqual(history.implied_stats(h["EURUSD"], 3650, "atm", "3M",
                                               current=lo - 0.01).percentile, 0.0)
        self.assertEqual(history.implied_stats(h["EURUSD"], 3650, "atm", "3M",
                                               current=hi + 0.01).percentile, 100.0)


def _simulated_legs(rho_path, *, vol_vol=0.7, days=750, seed=3):
    """Two dollar legs' daily history with a known correlation path and a known
    correlation between their at-the-money volatilities."""
    from volkit.history import PairHistory
    rng = np.random.default_rng(seed)
    dates, d = [], date(2023, 1, 2)
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rho_path = np.broadcast_to(np.asarray(rho_path, dtype=float), (days,))
    z1 = rng.standard_normal(days)
    z2 = rho_path * z1 + np.sqrt(1.0 - rho_path ** 2) * rng.standard_normal(days)
    e1 = rng.standard_normal(days)
    e2 = vol_vol * e1 + math.sqrt(1.0 - vol_vol ** 2) * rng.standard_normal(days)
    a = PairHistory("EURUSD", dates=list(dates), spot=1.1 * np.exp(np.cumsum(0.006 * z1)),
                    atm={"1M": 8.0 * np.exp(np.cumsum(0.03 * e1)),
                         "3M": 8.0 * np.exp(np.cumsum(0.03 * e1))})
    b = PairHistory("USDJPY", dates=list(dates), spot=140.0 * np.exp(np.cumsum(0.007 * z2)),
                    atm={"1M": 9.0 * np.exp(np.cumsum(0.03 * e2))})
    return a, b


class TestRealizedDependence(unittest.TestCase):
    """What a cross's legs' history says about the dependence CROSS_DEPENDENCE marks."""

    def test_the_vol_vol_correlation_is_the_correlation_of_the_legs_atm_changes(self):
        a, b = _simulated_legs(0.3, vol_vol=0.7)
        got = history.realized_vol_vol(a, b, "1M")
        self.assertAlmostEqual(got.rho, 0.7, delta=3 * got.rho_se)
        self.assertEqual(got.tenors, ("1M", "1M"))
        # A leg without the tenor is read at its nearest quoted one, and named.
        near = history.realized_vol_vol(a, b, "3M")
        self.assertEqual(near.tenors, ("3M", "1M"))
        self.assertTrue(any("USDJPY" in w and "1M" in w for w in near.warnings))
        b.atm.clear()
        with self.assertRaises(history.HistoryError):
            history.realized_vol_vol(a, b, "1M")

    def test_a_constant_correlation_has_no_correlation_vol_however_much_its_windows_scatter(self):
        """Twenty returns at rho 0.3 scatter by about 0.2 when the correlation
        never moves at all. Read as a correlation vol, that sampling noise would
        fatten every short-dated cross fly by a dependence that is not there."""
        a, b = _simulated_legs(0.3)
        got = history.realized_corr_vol(a, b, 30)
        self.assertGreater(got.raw_sd, 0.1)
        self.assertLess(got.corr_vol, 2 * got.corr_vol_se + 1e-12)
        self.assertAlmostEqual(got.raw_sd, got.noise_sd, delta=0.05)

    def test_a_correlation_that_moves_is_measured_net_of_its_noise(self):
        path = 0.3 + 0.3 * np.sign(np.sin(np.arange(750) * 2 * np.pi / 126))
        a, b = _simulated_legs(path)
        got = history.realized_corr_vol(a, b, 30)
        self.assertAlmostEqual(got.corr_vol, 0.3, delta=0.06)
        self.assertLess(got.corr_vol, got.raw_sd)

    def test_too_few_independent_windows_is_refused(self):
        a, b = _simulated_legs(0.3)
        with self.assertRaises(history.HistoryError) as cm:
            history.realized_corr_vol(a, b, 365)
        self.assertIn("independent", str(cm.exception))

    def test_the_realized_correlation_is_unchanged_by_the_shared_pairing(self):
        a, b = _simulated_legs(0.3)
        common, ra, rb, used, _, _ = history._paired_returns(a, b, 365)
        rho = float(np.sum(ra * rb) / math.sqrt(np.sum(ra * ra) * np.sum(rb * rb)))
        self.assertAlmostEqual(history.realized_correlation(a, b, 365).rho, rho, places=15)

    def test_a_measured_band_stays_inside_what_the_model_holds(self):
        m = analytics.MeasuredDependence(vol_vol=0.95, vol_vol_se=0.1, corr_vol=0.05,
                                         corr_vol_se=0.2)
        for d in m.band(moments.Dependence(0.95, 0.05), rho=0.8):
            self.assertLessEqual(d.vol_vol, 1.0)
            self.assertLessEqual(d.corr_vol, 0.2)
            self.assertGreaterEqual(d.corr_vol, 0.0)
        none = analytics.measure_dependence(None, "EURUSD", "USDJPY", "1m", 0.08)
        self.assertIsNone(none.vol_vol)
        self.assertTrue(none.notes)


class TestDependenceFromHistory(unittest.TestCase):
    """The tables on the sample history, and the triangle priced on them."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURJPY", "EURGBP"])
        cls.book.feed = MarketFeed.load(FEED)
        cls.history = history.load_history(HISTORY, cls.book.pairs)
        cls.own = analytics.dependence_table(cls.book, "EURJPY", cls.history,
                                             tenors=["1m", "3m"])

    def test_the_correlation_card_carries_the_measured_dependence(self):
        rows = analytics.correlation_table(self.book, "EURJPY", self.history,
                                           tenors=["1m", "3m"]).rows
        for r in rows:
            self.assertIsNotNone(r.dependence)
            self.assertIsNotNone(r.dependence.vol_vol, r.dependence.notes)
            self.assertIsNotNone(r.dependence.corr_vol, r.dependence.notes)
        plain = analytics.correlation_table(self.book, "EURJPY", None, tenors=["1m"]).rows[0]
        self.assertIsNone(plain.dependence)

    def test_the_own_premium_suggestion_gives_back_the_marked_fly(self):
        """Marked as suggested, the triangle reproduces the cross's own fly --
        at a correlation vol the history supports rather than one picked to fit."""
        r = next(x for x in self.own.rows if x.tenor == "3m")
        self.assertIsNotNone(r.suggested_vol_vol, r.reason)
        self.assertAlmostEqual(r.premium, r.implied_vol_vol - r.measured.vol_vol, places=12)
        self.assertAlmostEqual(r.suggested_vol_vol, r.implied_vol_vol, places=12)
        book = Book.from_excel(BOOK, ASOF, config={"CROSS_DEPENDENCE": [
            {"pair": "EURJPY", "tenor": "3m", "vol vol corr": r.suggested_vol_vol,
             "corr vol": r.suggested_corr_vol}]}).load_all(["EURJPY"])
        book.feed = self.book.feed
        tri = analytics.triangle_table(book, "EURJPY", cut="NY", tenors=["3m"],
                                       with_noise=False, implied_vol_vol=False)[0]
        # The implied search runs on the coarser grid, and answers to 0.005.
        self.assertAlmostEqual(tri.triangle["fly25"], tri.marked["fly25"], delta=5e-5)

    def test_no_premium_is_history_alone_and_a_lender_lends_its_own(self):
        none = analytics.dependence_table(self.book, "EURJPY", self.history, premium="none",
                                          tenors=["3m"]).rows[0]
        self.assertEqual(none.suggested_vol_vol, none.measured.vol_vol)
        lender = analytics.dependence_table(self.book, "EURGBP", self.history,
                                            tenors=["3m"]).rows[0]
        lent = analytics.dependence_table(self.book, "EURJPY", self.history, premium="eurgbp",
                                          tenors=["3m"]).rows[0]
        self.assertEqual(lent.premium_source, "EURGBP")
        if lender.premium is None:
            self.assertIsNone(lent.suggested_vol_vol)
            self.assertIn("EURGBP", lent.reason)
        else:
            self.assertAlmostEqual(lent.premium_used, lender.premium, places=12)
        with self.assertRaises(ValueError):
            analytics.dependence_table(self.book, "EURJPY", self.history, premium="EURUSD")

    def test_the_relative_value_triangle_is_priced_on_the_realized_dependence(self):
        """The Gaussian copula put every cross fly below its legs' history as
        well as the market, so the triangle signal read every cross's wings
        rich for a reason that was the copula. On realized, the triangle is the
        legs tied at what they have shown, with its uncertainty in the floor."""
        from volkit import relvalue
        kw = dict(horizon_days=7, cut="NY", tenors=["1m", "3m"])
        realized = relvalue.relative_value(self.book, "EURJPY", self.history["EURJPY"],
                                           history=self.history, **kw)
        marked = relvalue.relative_value(self.book, "EURJPY", self.history["EURJPY"],
                                         history=self.history, triangle_basis="marked", **kw)
        self.assertEqual(realized.triangle["basis"], "realized")
        self.assertEqual(marked.triangle["basis"], "marked")
        self.assertIn("measured on EURUSD/USDJPY history", realized.triangle["sources"]["3m"])
        self.assertEqual(marked.triangle["sources"]["3m"], "Gaussian copula")

        def triangle(grid, tenor, column):
            row = next(r for r in grid.rows if r.tenor == tenor)
            cell = next(c for c in row.cells if c.column == column)
            return next(s for s in cell.signals if s.name == "triangle")

        # The ATM is held, so its triangle does not care what the legs' wings do.
        self.assertAlmostEqual(triangle(realized, "3m", "atm").value,
                               triangle(marked, "3m", "atm").value, places=5)
        self.assertNotAlmostEqual(triangle(realized, "3m", "10dc").value,
                                  triangle(marked, "3m", "10dc").value, places=4)
        rows = analytics.triangle_table(
            self.book, "EURJPY", cut="NY", tenors=["3m"], implied_vol_vol=False,
            dependence={"3m": moments.Dependence(0.2, 0.05)},
            dependence_band={"3m": (moments.Dependence(0.3, 0.05),)})
        self.assertGreater(rows[0].dependence_noise["fly25"], 0.0)
        value, noise, _ = relvalue._triangle_difference(rows[0], next(
            c for c in relvalue.COLUMNS if c.name == "25dc"))
        self.assertGreater(noise, relvalue._triangle_difference(
            analytics.TriangleRow(**{**rows[0].__dict__, "dependence_noise": {}}),
            next(c for c in relvalue.COLUMNS if c.name == "25dc"))[1])

    def test_the_triangle_cannot_borrow_its_own_premium_and_falls_back_without_history(self):
        from volkit import relvalue
        for own in ("own", "EURJPY"):
            with self.assertRaises(relvalue.RelativeValueError):
                relvalue.relative_value(self.book, "EURJPY", None, history=self.history,
                                        premium=own, tenors=["3m"])
        grid = relvalue.relative_value(self.book, "EURJPY", None, horizon_days=7, cut="NY",
                                       tenors=["3m"])
        self.assertEqual((grid.triangle["asked"], grid.triangle["basis"]), ("realized", "marked"))
        self.assertTrue(any("no historical workbook" in w for w in grid.warnings))
        panel = relvalue.panel_from_request({"pair": "EURJPY", "triangle_basis": "Marked",
                                             "premium": "eurgbp"})
        self.assertEqual((panel.triangle_basis, panel.premium), ("marked", "eurgbp"))

    def test_the_route_suggests_and_the_config_window_offers_it(self):
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF, feed_path=str(FEED), history_path=str(HISTORY))
        out = service.dependence_realized({"pair": "EURJPY", "premium": "none"})
        self.assertEqual((out["sheet"], out["premium"]), ("CROSS_DEPENDENCE", "none"))
        self.assertTrue(out["rows"])
        tabs = {t["sheet"]: t for t in service.config_tabs()["tabs"]}
        self.assertEqual(tabs["CROSS_DEPENDENCE"]["measure"], "dependence")
        with self.assertRaises(ValueError):
            service.dependence_realized({"pair": "EURUSD"})
        with self.assertRaises(ValueError):
            BookService(str(BOOK), ASOF).dependence_realized({"pair": "EURJPY"})


if __name__ == "__main__":
    unittest.main()
