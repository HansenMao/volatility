"""Black, SABR, smile shape, moments and the numeric kernels.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestTimeUtil(unittest.TestCase):
    def test_tenor_units(self):
        self.assertAlmostEqual(tenor_to_years("2Y"), 2.0)
        self.assertAlmostEqual(tenor_to_years("3M"), 0.25, places=6)
        self.assertAlmostEqual(tenor_to_years("5D"), 5 / 365.2425)

    def test_unknown_unit_raises(self):
        """Legacy get_years_time returned 1.0 for '1D' -- one year."""
        with self.assertRaises(TenorError):
            tenor_to_years("1X")
        self.assertLess(tenor_to_years("1D"), tenor_to_years("1W"))

    def test_month_end_clamping(self):
        self.assertEqual(add_tenor(date(2024, 1, 31), "1M"), date(2024, 2, 29))
        self.assertEqual(add_tenor(date(2023, 1, 31), "1M"), date(2023, 2, 28))
        self.assertEqual(add_tenor(date(2024, 11, 30), "3M"), date(2025, 2, 28))

    def test_clock_roundtrip(self):
        t = ASOF.years_to(datetime(2025, 2, 28, 12, tzinfo=UTC))
        self.assertAlmostEqual(ASOF.years_to(ASOF.datetime_from_years(t)), t, places=12)

    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(parse_datetime("2024-02-02 10:00").tzinfo, UTC)

    def test_a_unit_may_be_spelled_out(self):
        """'1wk' is a week and '3mth' three months.

        The unit used to be a single letter, so a desk that wrote its own
        shorthand into the expiry box was told its tenor could not be parsed.
        """
        for text, want in (("1wk", "1W"), ("1 wk", "1W"), ("1-week", "1W"),
                           ("2weeks", "2W"), ("3mth", "3M"), ("3 months", "3M"),
                           ("1mo", "1M"), ("2yr", "2Y"), ("2 years", "2Y"),
                           ("10days", "10D"), ("8d", "8D"), ("o/n", "O/N")):
            with self.subTest(text):
                self.assertEqual(normalise_tenor(text), want)
        self.assertAlmostEqual(tenor_to_years("1wk"), tenor_to_years("1W"))
        self.assertAlmostEqual(tenor_to_years("3mth"), tenor_to_years("3M"))
        # O/N is one day, which is what the code means.
        self.assertEqual(parse_tenor("o/n"), (1.0, "d"))
        with self.assertRaises(TenorError):
            tenor_to_years("1wkk")

    def test_a_date_with_no_year_is_the_next_one_of_it(self):
        """'06 Nov' is the coming sixth of November, not a parse error.

        The year is obvious to whoever typed it, and the reference date is
        the book's clock rather than the machine's, so the same box read
        twice reads the same way.
        """
        today = date(2026, 9, 1)
        for text in ("06 Nov", "06Nov", "6-Nov", "Nov 6", "November 6"):
            with self.subTest(text):
                self.assertEqual(parse_datetime(text, today=today).date(),
                                 date(2026, 11, 6))
        # Already gone this year, so it is next year's.
        self.assertEqual(parse_datetime("31 Aug", today=today).date(), date(2027, 8, 31))
        # Today itself matches: the horizon starts now.
        self.assertEqual(parse_datetime("01 Sep", today=today).date(), today)
        # A time of day survives.
        self.assertEqual(parse_datetime("06 Nov 15:00", today=today),
                         datetime(2026, 11, 6, 15, 0, tzinfo=UTC))
        # 29 February is the one day whose next occurrence is not within a
        # year; answering it beats refusing it.
        self.assertEqual(parse_datetime("29 Feb", today=today).date(), date(2028, 2, 29))
        # A year that is given still wins, and nothing that parsed moves.
        self.assertEqual(parse_datetime("15Sep26", today=today).date(), date(2026, 9, 15))

    def test_a_year_less_date_with_no_reference_says_what_is_missing(self):
        """It is never the wall clock: the clock is injected (§4)."""
        with self.assertRaises(ValueError) as caught:
            parse_datetime("06 Nov")
        self.assertIn("no year", str(caught.exception))
        # A purely numeric year-less date stays ambiguous and is refused:
        # '06/11' is a day and a month in one country and the reverse in
        # another, and there is nothing in it to say which.
        with self.assertRaises(ValueError):
            parse_datetime("06/11", today=date(2026, 9, 1))

    def test_the_clock_is_what_says_which_year(self):
        from volkit.timeutil import Clock
        clock = Clock(datetime(2026, 9, 1, 12, tzinfo=UTC))
        self.assertEqual(clock.coerce_datetime("06 Nov").date(), date(2026, 11, 6))


class TestNumerics(unittest.TestCase):
    def test_bracketed_solve(self):
        self.assertAlmostEqual(solve_scalar(lambda x: x * x - 2, 1.0, lo_bound=0), math.sqrt(2))

    def test_unattainable_target_raises(self):
        with self.assertRaises(ConvergenceError):
            solve_scalar(lambda x: x * x + 1.0, 1.0)

    def test_fixed_point_detects_divergence(self):
        """Legacy loops ran a fixed 10 iterations and returned the last value."""
        self.assertAlmostEqual(fixed_point(math.cos, 1.0), 0.7390851332151607, places=8)
        with self.assertRaises(ConvergenceError):
            fixed_point(lambda x: 2 * x + 1, 1.0, max_iter=30)

    def test_piecewise_integration_is_exact_on_discontinuities(self):
        f = lambda x: np.where(x < 1.0, 1.0, 3.0)
        self.assertAlmostEqual(integrate_piecewise(f, np.array([0.0, 1.0, 2.0])), 4.0, places=12)
        self.assertAlmostEqual(integrate_piecewise(np.sin, np.linspace(0, math.pi, 9)), 2.0, places=10)


class TestBlack(unittest.TestCase):
    def test_premium_adjustment_follows_the_premium_currency_not_the_first_letters(self):
        """The crosses pay premium in their base currency and are adjusted.

        The legacy flag was ``ccy[0:3] == 'USD'``, which read EURJPY, EURGBP,
        AUDJPY and every other cross as unadjusted -- the wrong delta, and so
        the wrong strike for every wing quote on them.
        """
        from volkit.black import DeltaConvention
        adjusted = {"USDJPY", "USDCNH", "USDCAD", "EURJPY", "EURGBP", "AUDJPY", "GBPNZD",
                    "EURCNH", "CNHHKD", "USDHKD"}
        unadjusted = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"}
        for pair in adjusted | unadjusted:
            self.assertEqual(DeltaConvention.for_pair(pair).premium_adjusted, pair in adjusted, pair)
        self.assertEqual(DeltaConvention.default_premium_currency("EURJPY"), "EUR")
        self.assertEqual(DeltaConvention.default_premium_currency("USDJPY"), "USD")
        self.assertEqual(DeltaConvention.default_premium_currency("EURUSD"), "USD")
        # A desk can say otherwise, in the pair's own currencies only.
        self.assertFalse(DeltaConvention.for_pair("EURJPY", premium_ccy="JPY").premium_adjusted)
        with self.assertRaises(ValueError):
            DeltaConvention.for_pair("EURJPY", premium_ccy="USD")
        # The legacy True/False still coerces, and a convention passes through whole.
        self.assertTrue(DeltaConvention.of(True).premium_adjusted)
        c = DeltaConvention.for_pair("USDJPY", atmf_beyond="2y")
        self.assertIs(DeltaConvention.of(c), c)

    def test_the_atm_is_the_straddle_out_to_the_boundary_and_the_forward_beyond(self):
        from volkit.black import DeltaConvention, atm_strike, dns_strike
        c = DeltaConvention.for_pair("EURUSD")
        self.assertEqual(c.atmf_beyond, "1y")
        # the 1Y pillar itself, even a few days long on the calendar, is DNS
        for t in (0.02, 0.5, 1.0, 1.0 + 0.5 / 365.25):
            self.assertFalse(c.atm_is_forward(t), t)
            self.assertAlmostEqual(atm_strike(1.0, 0.10, t, c), dns_strike(1.0, 0.10, t, c))
            self.assertGreater(atm_strike(1.0, 0.10, t, c), 1.0)   # unadjusted: above F
        for t in (1.5, 2.0, 5.0):
            self.assertTrue(c.atm_is_forward(t), t)
            self.assertEqual(atm_strike(1.0, 0.10, t, c), 1.0)
        pa = DeltaConvention.for_pair("USDJPY")
        self.assertLess(atm_strike(1.0, 0.10, 0.5, pa), 1.0)      # adjusted: below F
        self.assertEqual(atm_strike(1.0, 0.10, 2.0, pa), 1.0)
        # never / always / a different tenor
        self.assertFalse(DeltaConvention.for_pair("USDCNH", atmf_beyond="never").atm_is_forward(30.0))
        self.assertTrue(DeltaConvention.for_pair("USDCNH", atmf_beyond="always").atm_is_forward(0.01))
        two = DeltaConvention.for_pair("USDJPY", atmf_beyond="2y")
        self.assertFalse(two.atm_is_forward(1.5))
        self.assertTrue(two.atm_is_forward(2.1))
        # and the boundary resolves through a calendar: a 1Y that the calendar
        # makes 371 days long keeps a 370-day expiry on the straddle side
        resolved = c.resolved(lambda tenor: 371 / 365.2425)
        self.assertFalse(resolved.atm_is_forward(370 / 365.2425))
        self.assertTrue(resolved.atm_is_forward(380 / 365.2425))
        self.assertIn("1Y", c.describe())
        with self.assertRaises(ValueError):
            DeltaConvention.for_pair("USDJPY", atmf_beyond="sometime")

    def test_spot_delta_is_forward_delta_through_the_foreign_discount_factor(self):
        """A 25 spot delta is a 25/DF forward delta, and the strike moves with it.

        The pair's convention becomes a slice's through ``at``: with a
        discount factor it reads spot delta, without one it reads forward
        delta and says why, and beyond the boundary it reads forward delta
        whatever the feed has.
        """
        from volkit.black import DeltaConvention, delta, dns_strike, strike_from_delta
        c = DeltaConvention.for_pair("USDJPY")
        self.assertTrue(c.spot_delta)
        spot = c.at(1.0, 0.96, "USD")
        fwd = c.at(1.0, None, "USD")
        far = c.at(2.0, 0.92, "USD")
        self.assertEqual((spot.delta_label(), fwd.delta_label(), far.delta_label()),
                         ("spot delta", "forward delta (no USD discount factor from the feed)",
                          "forward delta"))
        self.assertTrue(spot.delta_is_spot)
        self.assertFalse(fwd.delta_is_spot)
        K = strike_from_delta(0.25, 1.0, 0.10, 1.0, True, spot)
        self.assertAlmostEqual(float(delta(1.0, K, 0.10, 1.0, True, spot)), 0.25, places=12)
        self.assertAlmostEqual(float(delta(1.0, K, 0.10, 1.0, True, fwd)), 0.25 / 0.96, places=12)
        self.assertLess(K, strike_from_delta(0.25, 1.0, 0.10, 1.0, True, fwd))
        # the straddle strike is a delta-neutral point and does not move
        self.assertEqual(dns_strike(1.0, 0.10, 1.0, spot), dns_strike(1.0, 0.10, 1.0, fwd))
        # unadjusted put, same arithmetic
        u = DeltaConvention.for_pair("EURUSD").at(0.5, 0.99, "EUR")
        Kp = strike_from_delta(-0.25, 1.0, 0.08, 0.5, False, u)
        self.assertAlmostEqual(float(delta(1.0, Kp, 0.08, 0.5, False, u)), -0.25, places=12)
        # a spot delta above the discount factor names no strike
        with self.assertRaises(ValueError):
            strike_from_delta(0.97, 1.0, 0.10, 1.0, True, spot)
        # a pair that quotes forward delta never asks for the factor
        f = DeltaConvention.for_pair("USDCNH", delta_type="forward")
        self.assertFalse(f.wants_spot_delta(0.5))
        self.assertEqual(f.at(0.5, 0.96, "USD").df_foreign, 1.0)
        with self.assertRaises(ValueError):
            DeltaConvention.for_pair("USDCNH", delta_type="sideways")
        # the legacy True/False is a forward delta
        self.assertEqual(DeltaConvention.of(True).df_foreign, 1.0)

    def test_strike_delta_roundtrip_all_conventions(self):
        n = 0
        for vol in (0.02, 0.05, 0.12, 0.30):
            for t in (1 / 365, 0.02, 0.5, 2.0):
                for pa in (False, True):
                    for d, call in ((0.25, True), (-0.25, False), (0.10, True), (-0.10, False)):
                        k = black.strike_from_delta(d, 1.0, vol, t, call, pa)
                        # 1e-10 is far tighter than any quote resolution; the
                        # residual is the strike solver's xtol, not model error.
                        self.assertAlmostEqual(float(black.delta(1.0, k, vol, t, call, pa)), d, places=10)
                        n += 1
        self.assertGreater(n, 100)

    def test_dns_strike_is_delta_neutral(self):
        for pa in (False, True):
            for vol in (0.05, 0.30):
                k = black.dns_strike(1.0, vol, 0.5, pa)
                total = (float(black.delta(1.0, k, vol, 0.5, True, pa))
                         + float(black.delta(1.0, k, vol, 0.5, False, pa)))
                self.assertAlmostEqual(total, 0.0, places=14)

    def test_premium_adjusted_call_peak_matches_brute_force(self):
        """The pa call delta is non-monotone; the legacy solver could land
        on the wrong branch."""
        ks = np.linspace(0.01, 8.0, 400_000)
        d = black.delta(1.0, ks, 0.30, 10.0, True, True)
        _, peak = black._pa_call_delta_peak(1.0, 0.30, 10.0)
        self.assertAlmostEqual(peak, float(d.max()), places=5)

    def test_unattainable_premium_adjusted_delta_raises(self):
        with self.assertRaises(ConvergenceError):
            black.strike_from_delta(0.45, 1.0, 0.30, 10.0, True, True)

    def test_put_call_parity(self):
        c = float(black.price(1.0, 1.05, 0.11, 0.5, True))
        p = float(black.price(1.0, 1.05, 0.11, 0.5, False))
        self.assertAlmostEqual(c - p, 1.0 - 1.05, places=13)

    def test_implied_vol_inverts_price(self):
        px = float(black.price(1.0, 1.05, 0.11, 0.5, True))
        self.assertAlmostEqual(black.implied_vol(px, 1.0, 1.05, 0.5, True), 0.11, places=11)

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            black.dns_strike(1.0, -0.1, 0.5)
        with self.assertRaises(ValueError):
            black.strike_from_delta(1.5, 1.0, 0.1, 0.5, True)

    def test_theta_vanna_and_volga_match_a_finite_difference(self):
        """Closed forms, pinned against the price they are derivatives of.

        These are what the exchange-traded positions panel reports as the
        Black-Scholes column, so an algebra slip in one of them would be a
        plausible-looking risk number with nothing to contradict it.
        """
        h = 1e-6
        for F, K, v, t in ((1.09, 1.12, 0.085, 0.37), (1.09, 1.09, 0.075, 0.04),
                           (150.0, 141.0, 0.11, 1.4)):
            for call in (True, False):
                # theta is the derivative with respect to *calendar* time, so
                # against t (time to expiry) it comes back with a sign change.
                fd = -(float(black.price(F, K, v, t + h, call))
                       - float(black.price(F, K, v, t - h, call))) / (2 * h)
                self.assertAlmostEqual(float(black.theta(F, K, v, t)) / fd, 1.0, places=6)
                fd = (float(black.delta(F, K, v + h, t, call))
                      - float(black.delta(F, K, v - h, t, call))) / (2 * h)
                self.assertAlmostEqual(float(black.vanna(F, K, v, t)) / fd, 1.0, places=6)
            fd = (float(black.vega(F, K, v + h, t)) - float(black.vega(F, K, v - h, t))) / (2 * h)
            self.assertAlmostEqual(float(black.volga(F, K, v, t)) / fd, 1.0, places=6)


class TestSabr(unittest.TestCase):
    def test_z_over_x_is_continuous_at_the_money(self):
        z = np.array([-1e-3, -1e-8, 0.0, 1e-8, 1e-3])
        vals = sabr._z_over_x(z, -0.4)
        self.assertTrue(np.all(np.isfinite(vals)))
        self.assertAlmostEqual(float(vals[2]), 1.0, places=14)
        self.assertLess(abs(float(vals[1]) - float(vals[3])), 1e-6)

    def test_atm_closed_form(self):
        p = sabr.SabrParams(0.09, -0.3, 0.8, 1.0)
        self.assertAlmostEqual(sabr.atm_vol(p), float(sabr.lognormal_vol(1.0, p)), places=15)

    def test_calibration_reprices_quotes(self):
        for rr in (-0.025, 0.0, 0.025):
            cal = sabr.calibrate(0.07, rr, 0.01, 0.10, 1.0, False)
            self.assertTrue(cal.converged, cal.message)
            _, cv = sabr.smile_strike_and_vol(cal.params, 0.10, 1.0, True, False)
            _, pv = sabr.smile_strike_and_vol(cal.params, -0.10, 1.0, False, False)
            self.assertAlmostEqual(cv - pv, rr, places=8)

    def test_risk_reversal_sign_convention(self):
        """Positive RR means calls over, which must give a positive rho."""
        self.assertGreater(sabr.calibrate(0.07, 0.025, 0.01, 0.25, 1.0, False).params.rho, 0)
        self.assertLess(sabr.calibrate(0.07, -0.025, 0.01, 0.25, 1.0, False).params.rho, 0)

    def test_smile_strike_converges_or_raises(self):
        p = sabr.SabrParams(0.09, -0.3, 0.8, 1.0)
        k, v = sabr.smile_strike_and_vol(p, 0.25, 1.0, True, False)
        self.assertAlmostEqual(float(black.delta(1.0, k, v, 1.0, True, False)), 0.25, places=9)


class TestSmile(unittest.TestCase):
    def setUp(self):
        self.t = 0.25
        self.atm = 0.0765
        self.c25 = sabr.calibrate(self.atm, -0.0024, 0.0024, 0.25, self.t, True).params
        self.c10 = sabr.calibrate(self.atm, -0.0044, 0.0072, 0.10, self.t, True).params
        self.slice = SmileSlice.build(self.t, self.atm, self.c25, self.c10, True)

    def test_svi_reproduces_its_anchors(self):
        got = np.asarray(self.slice.vol(self.slice.strikes), dtype=float)
        np.testing.assert_allclose(got, self.slice.vols, atol=1e-9)

    def test_svi_is_arbitrage_free(self):
        self.assertTrue(self.slice.svi.arbitrage_free, self.slice.svi.warnings)
        self.assertEqual(self.slice.svi.params.violates(), [])
        g = self.slice.svi.params.durrleman(np.linspace(-1.5, 1.5, 500))
        self.assertGreaterEqual(float(np.min(g)), -1e-8)

    def test_svi_uses_five_parameters_not_twelve(self):
        """The legacy fit had 12 free parameters for 5 points."""
        p = self.slice.svi.params
        self.assertEqual(len({"a", "b", "rho", "m", "sigma"} & set(vars(p))), 5)

    def test_all_interpolators_agree_at_the_anchors(self):
        for method in ("SVI", "VV25", "VV10", "SABR25", "SABR10"):
            sl = SmileSlice.build(self.t, self.atm, self.c25, self.c10, True, method=method)
            self.assertAlmostEqual(float(sl.vol(sl.strikes[2])), self.atm, delta=2e-3)

    def test_vanna_volga_survives_degenerate_strike(self):
        """Legacy getVV divided by d1*d2, which vanishes at two strikes."""
        v = smile.vanna_volga_vol(np.array([1.0, 1.02]), 0.25, 0.97, 1.0, 1.03, 0.08, 0.076, 0.079)
        self.assertTrue(np.all(np.isfinite(v)))


class TestSmileShape(unittest.TestCase):
    """Reading a marked smile back as a correlation and a vol of vol.

    ``calibrate`` matches the *market* strangle, because that is what a broker
    quotes.  This one matches the *smile* butterfly, because that is the number
    the analysis screen has off whatever surface is marked -- matching a
    premium condition against a moment would be comparing two different things.
    """

    def test_a_sabr_smile_reads_back_as_the_parameters_it_was_built_from(self):
        for rho, nu, t in ((-0.35, 0.55, 0.25), (0.20, 0.90, 1.0), (-0.60, 1.40, 0.08)):
            with self.subTest(rho=rho, nu=nu, t=t):
                p = sabr.SabrParams(alpha=0.09, rho=rho, volvol=nu, t=t)
                # The at-the-money here is the *delta-neutral straddle*
                # volatility, which is how this book quotes it and what the
                # fit's own at-the-money condition solves alpha against.
                # ``sabr.atm_vol`` is the at-the-forward one, and feeding that
                # in instead moves alpha and with it both parameters.
                # The delta-neutral strike depends on the very volatility it
                # carries, so it is a fixed point; the fit solves alpha at
                # ``dns_strike(f, atm)`` and the test has to hand it an ``atm``
                # consistent with that or the two are a strike apart.
                atm = sabr.atm_vol(p)
                for _ in range(40):
                    nxt = float(sabr.lognormal_vol(
                        black.dns_strike(p.f, atm, t, False), p))
                    if abs(nxt - atm) < 1e-15:
                        atm = nxt
                        break
                    atm = nxt
                _, call = sabr.smile_strike_and_vol(p, 0.25, t, True, False)
                _, put = sabr.smile_strike_and_vol(p, -0.25, t, False, False)
                got = sabr.fit_smile_shape(atm, call - put, 0.5 * (call + put) - atm,
                                           0.25, t, False)
                self.assertTrue(got.converged, got.message)
                self.assertAlmostEqual(got.rho, rho, places=5)
                self.assertAlmostEqual(got.nu, nu, places=5)
                self.assertLess(got.max_error, 1e-7)

    def test_the_correlation_carries_the_sign_of_the_risk_reversal(self):
        """This is the whole reason the number is worth reporting: a risk
        reversal is a price, and rho is what it is a price *of*."""
        t = 0.5
        base = sabr.fit_smile_shape(0.10, 0.0, 0.0030, 0.25, t, False)
        up = sabr.fit_smile_shape(0.10, +0.012, 0.0030, 0.25, t, False)
        down = sabr.fit_smile_shape(0.10, -0.012, 0.0030, 0.25, t, False)
        # Not exactly zero, and it should not be: a zero-correlation SABR smile
        # is symmetric about the *forward*, while the two wings are placed
        # symmetrically about the delta-neutral strike, which is above it.
        self.assertAlmostEqual(base.rho, 0.0, delta=0.05)
        self.assertGreater(up.rho, 0.15)
        self.assertLess(down.rho, -0.15)

    def test_a_wider_butterfly_is_a_higher_vol_of_vol(self):
        t = 0.5
        thin = sabr.fit_smile_shape(0.10, -0.005, 0.0015, 0.25, t, False)
        fat = sabr.fit_smile_shape(0.10, -0.005, 0.0045, 0.25, t, False)
        self.assertGreater(fat.nu, thin.nu * 1.4)

    def test_a_smile_no_sabr_can_reach_says_so_rather_than_returning_the_nearest(self):
        """A steep risk reversal on a flat butterfly is outside the family.

        Returning the nearest parameters in silence would put a number on the
        screen that does not describe the smile it claims to summarise.
        """
        got = sabr.fit_smile_shape(0.10, -0.060, 0.0002, 0.25, 0.5, False)
        self.assertFalse(got.converged)
        self.assertGreater(got.max_error, 1e-6)
        self.assertTrue(got.warnings)
        self.assertIn("nearest", " ".join(got.warnings))

    def test_the_inputs_are_checked(self):
        for bad in ((0.0, 0.0, 0.001, 0.25, 0.5), (0.10, 0.0, 0.001, 0.25, 0.0),
                    (0.10, 0.0, 0.001, 0.0, 0.5), (0.10, 0.0, 0.001, 0.6, 0.5)):
            with self.assertRaises(ValueError):
                sabr.fit_smile_shape(*bad, conv=False)


class TestSabrRobustness(unittest.TestCase):
    def test_alpha_closed_form_matches_hagan(self):
        for rho, nu, t in ((-0.3, 0.8, 1.0), (0.5, 1.5, 2.0), (0.0, 0.5, 0.25)):
            for a in sabr.alpha_roots_at_forward(0.08, rho, nu, t):
                got = float(sabr.lognormal_vol(1.0, sabr.SabrParams(a, rho, nu, t)))
                self.assertAlmostEqual(got, 0.08, places=12)

    def test_multiple_alpha_roots_are_found(self):
        """The at-the-money condition is a cubic and can have two positive roots."""
        roots = sabr.alpha_roots_at_forward(0.08, -0.3, 0.8, 1.0)
        self.assertGreater(len(roots), 1)
        self.assertLess(roots[0], roots[1])

    def test_no_alpha_root_is_reported_not_guessed(self):
        self.assertEqual(sabr.alpha_roots_at_forward(0.08, -0.9, 3.0, 5.0), [])
        K = black.dns_strike(1.0, 0.08, 5.0, False)
        with self.assertRaises(ConvergenceError):
            sabr.alpha_from_atm(0.08, K, -0.9, 3.0, 5.0)

    def test_calibration_is_independent_of_the_starting_point(self):
        """The sweep locates the basin, so no seed can change the answer."""
        base = sabr.calibrate(0.12, -0.055, 0.009, 0.25, 0.25, DeltaConvention(True))
        coarse = sabr.calibrate(0.12, -0.055, 0.009, 0.25, 0.25, DeltaConvention(True),
                                scan=(21, 15))
        self.assertAlmostEqual(base.params.rho, coarse.params.rho, places=6)
        self.assertAlmostEqual(base.params.volvol, coarse.params.volvol, places=6)

    def test_stress_quotes_still_calibrate(self):
        cases = [(0.085, -0.030, 0.0065, 0.25, 1 / 52), (0.085, -0.002, 0.0180, 0.25, 1 / 52),
                 (0.12, -0.055, 0.0090, 0.25, 0.25), (0.28, -0.090, 0.0300, 0.25, 1 / 12),
                 (0.07, -0.010, 0.0004, 0.25, 0.5)]
        for atm, rr, st, d, t in cases:
            cal = sabr.calibrate(atm, rr, st, d, t, DeltaConvention(True))
            self.assertTrue(cal.converged, f"{(atm, rr, st, d, t)}: {cal.message}")
            self.assertLess(cal.max_error, 1e-9)

    def test_extreme_total_vol_raises_rather_than_overflowing(self):
        with self.assertRaises(ValueError):
            black.strike_from_delta(0.25, 1.0, 50.0, 100.0, True, False)

    def test_prior_pulls_the_fit(self):
        free = sabr.calibrate(0.07, -0.02, 0.005, 0.25, 1.0, False)
        prior = sabr.SabrParams(0.07, 0.5, 0.2, 1.0)
        pulled = sabr.calibrate(0.07, -0.02, 0.005, 0.25, 1.0, False,
                                prior=prior, prior_weight=5.0)
        self.assertGreater(pulled.params.rho, free.params.rho)

    def test_book_calibration_is_unique(self):
        """No competing solutions anywhere in the sample workbook."""
        book = Book.from_excel(BOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        for mark in book.data.marks["USDJPY"][:4]:
            t = tenor_to_years(mark.tenor)
            atm = surface.atm.cut_vol(ASOF.datetime_from_years(t), "NY")
            cal = sabr.calibrate(atm, mark.rr_25, mark.st_25, 0.25, t,
                                 surface.conv, max_solutions=3)
            self.assertEqual(cal.alternatives, (), f"{mark.tenor}: {cal.warnings}")


class TestAtmCurve(unittest.TestCase):
    def setUp(self):
        self.curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF)

    def test_integration_is_exact_on_its_panels(self):
        """Splitting at the hourly/event breakpoints makes each panel smooth,
        so a 5-point rule is already exact -- raising the order changes
        nothing.  The legacy code ran adaptive quad with limit=500 on the
        discontinuous integrand instead."""
        for T in (0.02, 0.25, 1.0):
            self.curve._int_cache.clear()
            self.curve.quad_order = 5
            low = self.curve.integrated_variance(T)
            self.curve._int_cache.clear()
            self.curve.quad_order = 20
            high = self.curve.integrated_variance(T)
            self.curve._int_cache.clear()
            self.curve.quad_order = 5
            self.assertLess(abs(low - high) / high, 1e-14)

    def test_integration_matches_dense_reference(self):
        # A uniform trapezoid rule converges only slowly across the hourly
        # jumps, so its own error sets the tolerance here.
        for T in (0.02, 0.25, 1.0):
            fast = self.curve.integrated_variance(T)
            ts = np.linspace(0, T, 8_000_001)
            ref = float(np.trapezoid(self.curve.instantaneous_variance(ts), ts))
            self.assertLess(abs(fast - ref) / ref, 5e-7)

    def test_term_structure_is_monotone_in_the_right_direction(self):
        v_1w = self.curve.term_vol(tenor_to_years("1w"))
        v_1y = self.curve.term_vol(tenor_to_years("1y"))
        self.assertLess(v_1w, v_1y)  # initial 6.05 below long term 7.65

    def test_neighbour_tenors_outside_range(self):
        """Legacy argmax(tenors > t) returned (last, first) past the end."""
        self.assertEqual(self.curve._neighbour_tenors(5.0), (None, None))
        self.assertEqual(self.curve._neighbour_tenors(1e-6), (None, None))
        self.assertEqual(self.curve._neighbour_tenors(0.3), ("3m", "6m"))

    def test_tenor_overwrite_is_honoured(self):
        """And it is anchored where the tenor actually is on the calendar.

        Read at ``tenor_to_years`` the overwrite is a hair off, because that
        is not where 3M is: a month is 30 or 31 days, not a nominal 30.44.
        ``tenor_years`` is the curve's own reading, and it is the one the
        marks and the pricer both use.
        """
        t3 = self.curve.tenor_years("3m")
        self.curve.overwrite_tenor("3m", 0.09)
        self.assertAlmostEqual(self.curve.term_vol(t3), 0.09, places=8)
        self.assertNotAlmostEqual(self.curve.term_vol(tenor_to_years("3m")), 0.09, places=8)
        self.curve.clear_overwrite("3m")
        self.assertNotAlmostEqual(self.curve.term_vol(t3), 0.09, places=4)

    def test_last_pillar_overwrite_reaches_the_cut(self):
        """The 1Y option expires at the cut, hours past the 1Y pillar.

        The marked curve used to drop back onto the raw curve past the last
        pillar, so the cut column -- and every 1Y price -- ignored a 1Y
        overwrite outright.  Past the last anchor it now carries the anchor's
        total variance plus the curve's forward variance.
        """
        t1y = self.curve.tenor_years("1y")
        self.curve.overwrite_tenor("1y", 0.09)
        at_cut = self.curve.cut_vol(self.curve.clock.datetime_from_years(t1y), "TK")
        self.assertAlmostEqual(at_cut, 0.09, delta=2e-4)
        t2y = self.curve.tenor_years("2y")
        expected = (0.09 ** 2 * t1y + self.curve.integrated_variance(t2y)
                    - self.curve.integrated_variance(t1y)) / t2y
        self.assertAlmostEqual(self.curve.term_vol(t2y), math.sqrt(expected), places=12)
        # No jump either side of the pillar.
        eps = 1e-7
        self.assertAlmostEqual(self.curve.term_vol(t1y - eps), self.curve.term_vol(t1y + eps),
                               places=6)

    def test_first_pillar_overwrite_scales_the_short_end(self):
        t1w = self.curve.tenor_years("1w")
        self.curve.overwrite_tenor("1w", 0.09)
        t = t1w / 2
        expected = 0.09 ** 2 * t1w * (self.curve.integrated_variance(t)
                                     / self.curve.integrated_variance(t1w)) / t
        self.assertAlmostEqual(self.curve.term_vol(t), math.sqrt(expected), places=10)
        self.assertAlmostEqual(self.curve.term_vol(t1w - 1e-7), 0.09, places=6)

    def test_overwrite_off_the_pillars_is_an_anchor(self):
        """A 4M or an 18M is not on CONFIG's list and used to move nothing."""
        for tenor in ("4m", "18m"):
            t = self.curve.tenor_years(tenor)
            self.curve.overwrite_tenor(tenor, 0.12)
            self.assertAlmostEqual(self.curve.term_vol(t), 0.12, places=12)
        self.assertEqual(self.curve._neighbour_tenors(self.curve.tenor_years("5m")), ("4m", "6m"))
        # The pillars either side are not overwritten and stay on the curve.
        for tenor in ("3m", "6m"):
            t = self.curve.tenor_years(tenor)
            self.assertAlmostEqual(self.curve.term_vol(t), self.curve.curve_vol(t), places=12)

    def test_two_spellings_of_one_expiry_are_one_anchor(self):
        self.curve.overwrite_tenor("12m", 0.09)
        t = self.curve.tenor_years("1y")
        self.assertAlmostEqual(self.curve.term_vol(t), 0.09, places=12)

    def test_an_overwrite_that_cannot_be_placed_is_refused(self):
        with self.assertRaises(ValueError):
            self.curve.overwrite_tenor("xyz", 0.09)
        self.assertEqual(self.curve.tenor_overwrites, {})

    def test_weekend_volatility_is_damped(self):
        sat = self.curve.daily_vol(datetime(2024, 3, 2, 12, tzinfo=UTC))
        wed = self.curve.daily_vol(datetime(2024, 3, 6, 12, tzinfo=UTC))
        self.assertLess(sat, wed)

    def test_zero_length_window_does_not_divide_by_zero(self):
        self.assertEqual(self.curve.integrated_variance(0.5, 0.5), 0.0)
        with self.assertRaises(ValueError):
            self.curve.integrated_vol(0.5, 0.5)

    def test_backbone_cross_term_carries_time(self):
        """The legacy expression omitted t from the rate-vol cross term."""
        p = BackboneParams(0.06, 0.08, 5.0, rate_vol=0.02, rate_corr=0.5)
        c = AtmCurve("USDJPY", p, ASOF)
        sigma = 0.08 - (0.08 - 0.06) * math.exp(-5.0 * 0.5)
        expected = math.sqrt(sigma**2 + 2 * 0.5 * sigma * 0.02 * 0.5 + (0.02 * 0.5) ** 2)
        self.assertAlmostEqual(float(c.backbone_vol(0.5)), expected, places=14)

    def test_rate_vol_zero_is_unaffected(self):
        p = BackboneParams(0.06, 0.08, 5.0, rate_vol=0.0, rate_corr=0.9)
        c = AtmCurve("USDJPY", p, ASOF)
        sigma = 0.08 - (0.08 - 0.06) * math.exp(-5.0 * 0.5)
        self.assertAlmostEqual(float(c.backbone_vol(0.5)), sigma, places=15)


class TestSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        cls.s = cls.book["USDJPY"]
        cls.expiry = datetime(2024, 5, 28, tzinfo=UTC)

    def test_all_tenors_calibrate(self):
        self.assertTrue(self.s.fits)
        self.assertTrue(all(f.ok for f in self.s.fits), [f.message for f in self.s.fits if not f.ok])

    def test_density_integrates_to_one(self):
        """Legacy getDensity raised NameError and mis-scaled by K^2."""
        g = np.linspace(0.5, 1.8, 601)
        total = float(np.trapezoid([self.s.density(float(k), self.expiry) for k in g], g))
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_density_is_non_negative(self):
        g = np.linspace(0.85, 1.2, 120)
        self.assertGreaterEqual(min(self.s.density(float(k), self.expiry) for k in g), -1e-9)

    def test_delta_strike_matches_its_delta(self):
        for d, call in ((0.25, True), (0.10, False)):
            k, v = self.s.delta_strike(self.expiry, d, call)
            got = float(black.delta(1.0, k, v, self.book.clock.years_to(self.expiry), call, self.s.conv))
            self.assertAlmostEqual(abs(got), d, places=9)

    def test_slice_is_cached(self):
        a = self.s.slice_at(self.expiry)
        b = self.s.slice_at(self.expiry)
        self.assertIs(a, b)

    def test_vectorised_strike_query(self):
        ks = np.linspace(0.95, 1.05, 50)
        self.assertEqual(np.asarray(self.s.vol(ks, self.expiry)).shape, ks.shape)

    def test_past_expiry_raises_rather_than_returning_zero(self):
        """The legacy GUI wrapped this in a bare except and showed 0.0000."""
        with self.assertRaises(ValueError):
            self.s.vol(1.0, datetime(2020, 1, 1, tzinfo=UTC))

    def test_param_term_structure_decay_is_non_negative(self):
        for name, ts in self.s.term.items():
            self.assertGreaterEqual(ts.decay, 0.0, name)

    def test_anchoring_pins_the_quoted_tenors(self):
        self.s.anchor_tenors = True
        self.s._slices.clear()
        try:
            fit = self.s.fits[3]
            self.assertAlmostEqual(self.s.params_at(fit.t)["rho25"], fit.rho25, places=9)
        finally:
            self.s.anchor_tenors = False
            self.s._slices.clear()

    def test_flat_param_curve_does_not_divide_by_zero(self):
        ts = fit_param_term_structure([0.1, 0.2, 0.3], [0.5, 0.5, 0.5])
        self.assertTrue(all(math.isfinite(x) for x in (ts.initial, ts.final, ts.decay)))


class TestCross(unittest.TestCase):
    def test_leg_signs(self):
        self.assertEqual(infer_leg_signs("AUDJPY", "AUDUSD", "USDJPY"), (1, -1))
        self.assertEqual(infer_leg_signs("EURGBP", "EURUSD", "GBPUSD"), (1, 1))

    def test_unrelated_legs_raise(self):
        with self.assertRaises(ValueError):
            infer_leg_signs("AUDJPY", "EURUSD", "GBPCHF")

    def test_correlation_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            CorrelationCurve(1.4, 0.2)

    def test_cross_vol_exceeds_legs_when_usd_positions_oppose(self):
        """AUDJPY = AUDUSD x USDJPY, so the variances add rather than cancel."""
        book = Book.from_excel(BOOK, ASOF).build(["AUDJPY"])
        t = tenor_to_years("3m")
        cross = book["AUDJPY"].atm.term_vol(t)
        self.assertGreater(cross, book["AUDUSD"].atm.term_vol(t))
        self.assertGreater(cross, book["USDJPY"].atm.term_vol(t))


class TestTimeWeighting(unittest.TestCase):
    def test_session_shares_sum_to_one(self):
        total = sum(session_shares(DEFAULT_SESSION_HOURS).values())
        np.testing.assert_allclose(total, np.ones(24))

    def test_us_holiday_only_damps_us_hours(self):
        """The legacy 6x24 matrix could not express new calendar combinations."""
        tw = TimeWeighting("USDJPY")
        july4_ny = datetime(2024, 7, 4, 14, tzinfo=UTC)
        july4_tok = datetime(2024, 7, 4, 2, tzinfo=UTC)
        normal_ny = datetime(2024, 7, 2, 14, tzinfo=UTC)
        self.assertLess(tw.weight_at_datetime(july4_ny), tw.weight_at_datetime(normal_ny))
        self.assertAlmostEqual(tw.weight_at_datetime(july4_tok), tw.hourly_weight[2], places=12)

    def test_weekend_uses_the_real_market_close(self):
        tw = TimeWeighting("USDJPY")
        self.assertTrue(tw.is_closed(datetime(2024, 7, 6, 12, tzinfo=UTC)))     # Saturday
        self.assertTrue(tw.is_closed(datetime(2024, 7, 5, 23, tzinfo=UTC)))     # Fri after close
        self.assertFalse(tw.is_closed(datetime(2024, 7, 5, 20, tzinfo=UTC)))    # Fri before close
        self.assertFalse(tw.is_closed(datetime(2024, 7, 7, 23, tzinfo=UTC)))    # Sun after open


class TestDeterminism(unittest.TestCase):
    def test_same_clock_gives_identical_results(self):
        """Legacy read datetime.utcnow() inside the model on every call."""
        a = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        b = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        e = datetime(2024, 5, 28, tzinfo=UTC)
        self.assertEqual(float(a["USDJPY"].vol(1.02, e)), float(b["USDJPY"].vol(1.02, e)))

    def test_different_clocks_give_different_results(self):
        other = Clock(datetime(2024, 3, 15, 12, tzinfo=UTC))
        a = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        b = Book.from_excel(BOOK, other).load_all(["USDJPY"])
        e = datetime(2024, 5, 28, tzinfo=UTC)
        self.assertNotEqual(float(a["USDJPY"].vol(1.02, e)), float(b["USDJPY"].vol(1.02, e)))


class TestExotics(unittest.TestCase):
    def setUp(self):
        self.S, self.vol, self.t, self.F = 100.0, 0.10, 1.0, 100.0
        self.drift = exotics.implied_drift(self.S, self.F, self.t)

    def test_touch_probability_matches_monte_carlo(self):
        """The closed form is the reference; the simulator must agree with it,
        because the bent-barrier case has only the simulator."""
        for B in (110.0, 90.0, 120.0):
            a = exotics.touch_probability(self.S, B, self.vol, self.t, self.drift)
            m, se = exotics._touch_mc(self.S, B, self.vol, self.t, self.drift,
                                      B > self.S, "extend", 0.0, True, 120_000, 192, 3)
            self.assertLess(abs(m - a), 4.0 * se, f"barrier {B}: analytic {a}, mc {m}+/-{se}")

    def test_barrier_at_spot_touches_immediately(self):
        self.assertEqual(exotics.touch_probability(100.0, 100.0, 0.1, 1.0, 0.0), 1.0)

    def test_zero_drift_is_log_symmetric(self):
        up = exotics.touch_probability(100, 110, 0.1, 1, 0.005)
        down = exotics.touch_probability(100, 100 * 100 / 110, 0.1, 1, 0.005)
        self.assertAlmostEqual(up, down, places=12)

    def test_one_touch_plus_no_touch_is_one(self):
        a = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5)
        b = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, is_no_touch=True)
        self.assertAlmostEqual(a.price + b.price, 1.0, places=12)

    def test_extend_costs_more_than_either_bend(self):
        kw = dict(buffer_pct=1.0, conservative=True, paths=40_000, steps=128)
        flat = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="none").price
        ext = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="extend", **kw).price
        bf = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="bend_front", **kw).price
        bb = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="bend_back", **kw).price
        for bent in (bf, bb):
            self.assertGreater(bent, flat)
            self.assertLess(bent, ext)

    def test_overhedge_side_flips_the_shift(self):
        sell = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="extend",
                                 buffer_pct=1.0, conservative=True)
        buy = exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="extend",
                                buffer_pct=1.0, conservative=False)
        self.assertLess(sell.barrier_used, 160.0)     # toward spot: easier to touch
        self.assertGreater(buy.barrier_used, 160.0)
        self.assertGreater(sell.price, buy.price)

    def test_unknown_overhedge_mode_raises(self):
        with self.assertRaises(ValueError):
            exotics.one_touch(150.0, 160.0, 0.09, 0.5, 149.5, mode="wobble")

    def test_digital_fair_value_includes_the_skew_term(self):
        """The digital is -dC/dK through the smile, so a sloping smile moves it
        away from N(d2); using N(d2) as the benchmark made the overhedge cost
        come out negative."""
        skewed = lambda K: 0.06 + 0.02 * (K - 100.0) / 100.0
        r = exotics.european_digital(100.0, 105.0, 1.0, 100.0, skewed, ramp_pct=0.0)
        self.assertNotAlmostEqual(r.fair_value, r.flat_vol_price, places=4)
        self.assertLess(r.fair_value, r.flat_vol_price)   # upward skew lowers a call digital

    def test_digital_ramp_is_monotone_and_never_cheaper(self):
        flat = lambda K: 0.08
        prev = -1.0
        for ramp in (0.0, 0.1, 0.25, 0.5, 1.0, 2.0):
            r = exotics.european_digital(100.0, 105.0, 1.0, 100.0, flat,
                                         ramp_pct=ramp, conservative=True)
            self.assertGreaterEqual(r.overhedge_cost, prev - 1e-12)
            prev = r.overhedge_cost
        self.assertGreater(prev, 0.0)

    def test_digital_ramp_converges_to_fair_value(self):
        """A one-sided ramp approximates the derivative at the ramp midpoint,
        so the gap closes linearly in the ramp width rather than instantly."""
        flat = lambda K: 0.08
        gaps = []
        for ramp in (0.4, 0.2, 0.1, 0.05, 0.025):
            r = exotics.european_digital(100.0, 105.0, 1.0, 100.0, flat, ramp_pct=ramp)
            gaps.append(abs(r.price - r.fair_value))
        for a, b in zip(gaps, gaps[1:]):
            self.assertLess(b, 0.6 * a)      # roughly halves each time
        self.assertLess(gaps[-1], 6e-4)

    def test_call_and_put_digitals_sum_to_one(self):
        flat = lambda K: 0.08
        c = exotics.european_digital(100.0, 105.0, 1.0, 100.0, flat, is_call=True).fair_value
        p = exotics.european_digital(100.0, 105.0, 1.0, 100.0, flat, is_call=False).fair_value
        self.assertAlmostEqual(c + p, 1.0, places=12)


class TestExoticLegs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        cls.book.feed = MarketFeed.load(FEED)

    def _one(self, **kw):
        return price_strip(self.book, [OptionLeg("USDJPY", "3M", **kw)])["legs"][0]

    def test_feed_fills_spot_and_forward(self):
        r = self._one(strike="ATM")
        self.assertTrue(r["feed_used"])
        self.assertAlmostEqual(r["spot"], 150.25, places=10)
        self.assertNotAlmostEqual(r["forward"], r["spot"], places=6)

    def test_explicit_spot_overrides_the_feed(self):
        r = self._one(strike="ATM", spot=160.0, forward_points=0.0, pip=100.0)
        self.assertFalse(r["feed_used"])
        self.assertAlmostEqual(r["spot"], 160.0, places=10)

    def test_touch_leg_needs_a_barrier(self):
        r = self._one(product="one_touch")
        self.assertFalse(r["ok"])
        self.assertIn("barrier", r["error"])

    def test_one_touch_and_no_touch_sum_to_one(self):
        a = self._one(product="one_touch", barrier="158")
        b = self._one(product="no_touch", barrier="158")
        self.assertAlmostEqual(a["premium_dom"] + b["premium_dom"], 1.0, places=10)
        self.assertAlmostEqual(a["delta_pct"], -b["delta_pct"], places=8)

    def test_overhedge_raises_both_price_and_risk(self):
        flat = self._one(product="one_touch", barrier="158")
        buff = self._one(product="one_touch", barrier="158", overhedge="extend",
                         buffer_pct=0.5, conservative=True)
        self.assertGreater(buff["premium_dom"], flat["premium_dom"])
        self.assertGreater(abs(buff["delta_pct"]), abs(flat["delta_pct"]))
        self.assertGreater(buff["overhedge_cost"], 0.0)

    def test_bent_barrier_reports_monte_carlo_error(self):
        r = self._one(product="one_touch", barrier="158", overhedge="bend_front",
                      buffer_pct=0.5)
        self.assertIn("monte carlo", r["pricing_method"])
        self.assertGreater(r["mc_error"], 0.0)

    def test_digital_ramp_raises_price_and_delta(self):
        flat = self._one(product="digital", strike="155", option_type="C")
        ramp = self._one(product="digital", strike="155", option_type="C", ramp_pct=1.0)
        self.assertGreater(ramp["premium_dom"], flat["premium_dom"])
        self.assertGreater(ramp["delta_pct"], flat["delta_pct"])

    def test_unknown_product_is_reported_per_leg(self):
        r = self._one(product="rainbow")
        self.assertFalse(r["ok"])
        self.assertIn("rainbow", r["error"])


class TestImpliedRRFly(unittest.TestCase):
    def test_anchoring_makes_the_surface_reproduce_its_quotes(self):
        """The marking check the implied-vs-quoted table displays.

        Read at each pillar's **calendar expiry**, which is where a quoted
        tenor sits on the volatility axis, and across every quoted tenor
        rather than at one. Anchoring replaces the smoothed parameter term
        structure with the tenor's own fit, so it tightens the surface against
        its quotes *as a whole*; at any single tenor the smoothed curve can
        happen to pass closer, which is what the 3M does here.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        surface = book["USDJPY"]
        marks = {m.tenor.upper(): m for m in book.data.marks["USDJPY"]}

        def errors():
            out = {}
            for tenor, mark in marks.items():
                expiry = ASOF.datetime_from_years(surface.tenor_years(tenor))
                out[tenor] = abs(surface.risk_reversal(expiry, 0.25) - mark.rr_25)
            return out

        loose = errors()
        surface.anchor_tenors = True
        surface._slices.clear()
        tight = errors()
        self.assertLess(sum(tight.values()), sum(loose.values()))
        # And anchored, every pillar reproduces its own quote to within the
        # SABR fit's own residual there -- not just on average.
        for tenor, err in tight.items():
            self.assertLess(err, 1e-4, tenor)


class TestMoments(unittest.TestCase):
    """Reading a smile as a distribution, and combining two of them."""

    def test_a_flat_smile_is_read_back_as_a_lognormal(self):
        d = _flat_distribution(0.10, 0.5)
        m = d.moments()
        self.assertAlmostEqual(m.annualised_vol(0.5), 0.10, places=5)
        # Two numerical differentiations of a call curve leave a little grid
        # dust; a few times 1e-5 of skew is three orders below anything a
        # butterfly would show.
        self.assertAlmostEqual(m.skew, 0.0, places=4)
        self.assertAlmostEqual(m.excess_kurtosis, 0.0, places=3)
        self.assertAlmostEqual(d.mgf(1.0), 1.0, places=6)      # it is a martingale

    def test_the_copula_reproduces_the_exact_variance_triangle(self):
        """Two smile-less legs have an answer in closed form; the grid must find it."""
        t = 0.5
        for va, vb, rho, ca, cb in ((0.10, 0.08, 0.30, 1, 1), (0.10, 0.08, -0.40, 1, -1),
                                    (0.12, 0.12, 0.60, 1, -1), (0.20, 0.15, 0.80, 1, 1)):
            comb = moments.combine(_flat_distribution(va, t), _flat_distribution(vb, t),
                                   (ca, cb), rho, DeltaConvention(False))
            table = comb.table()
            exact = math.sqrt(va * va + vb * vb + 2 * ca * cb * rho * va * vb)
            # 2e-5 in volatility is two ten-thousandths of a vol point.
            self.assertAlmostEqual(table["atm"], exact, delta=2e-5,
                                   msg=f"rho={rho} coefficients=({ca},{cb})")
            self.assertAlmostEqual(table["rr25"], 0.0, places=4)
            self.assertAlmostEqual(table["fly25"], 0.0, places=4)

    def test_the_forward_shift_is_the_triangles_own_convexity(self):
        """The product of two martingales is not one, and an inverted leg is not
        one at all.  Both are known from the legs, so what is left over after
        subtracting them is grid error and must be tiny."""
        t = 0.5
        for va, vb, rho, ca, cb in ((0.10, 0.08, 0.30, 1, 1), (0.12, 0.12, -0.90, 1, -1),
                                    (0.20, 0.15, 0.80, 1, 1)):
            comb = moments.combine(_flat_distribution(va, t), _flat_distribution(vb, t),
                                   (ca, cb), rho, DeltaConvention(False))
            self.assertLess(abs(comb.shift - comb.convexity), 1e-4,
                            msg=f"rho={rho} coefficients=({ca},{cb})")
            self.assertEqual(comb.warnings, ())

    def test_triangle_coefficients_are_not_just_the_variance_signs(self):
        """The variance triangle only ever needs the *product* of the two signs.

        An odd cumulant needs them one at a time, and getting the product right
        while getting the individual signs wrong leaves the at-the-money
        correct and flips the risk reversal -- which is the cross-pair error
        this project already had to fix once (MIGRATION.md 1.1).
        """
        self.assertEqual(moments.triangle_coefficients("AUDJPY", "AUDUSD", "USDJPY"), (1, 1))
        self.assertEqual(moments.triangle_coefficients("EURGBP", "EURUSD", "GBPUSD"), (1, -1))
        self.assertEqual(moments.triangle_coefficients("GBPNZD", "GBPUSD", "NZDUSD"), (1, -1))

    def test_the_coefficients_agree_with_the_curve_builders_signs(self):
        book = Book.from_excel(BOOK, ASOF).load_all()
        crosses = [(n, s.legs) for n, s in book.data.pairs.items() if s.is_cross]
        self.assertTrue(crosses)
        for name, legs in crosses:
            ca, cb = moments.triangle_coefficients(name, *legs)
            sa, sb = infer_leg_signs(name, *legs)
            self.assertEqual(ca * cb, -sa * sb, msg=name)

    def test_legs_that_share_no_currency_are_refused(self):
        with self.assertRaises(ValueError):
            moments.triangle_coefficients("EURJPY", "AUDUSD", "GBPCAD")

    def test_a_marked_smile_reads_back_as_a_usable_distribution(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        expiry = ASOF.datetime_from_years(0.5)
        d = moments.distribution_from_surface(book["EURUSD"], expiry, method="SVI", cut="NY")
        self.assertGreater(d.captured, 0.99)
        self.assertLess(abs(d.forward_error), 1e-3)
        self.assertEqual(d.warnings, ())
        # A smile with a positive butterfly is fat-tailed: that is what a
        # butterfly *is*, so the density must say so.
        self.assertGreater(d.moments().excess_kurtosis, 0.0)

    def test_the_machinery_reproduces_a_leg_it_is_given_alone(self):
        """The noise floor.  A cross difference smaller than this means nothing."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        expiry = ASOF.datetime_from_years(0.25)
        d = moments.distribution_from_surface(surface, expiry, method="SVI", cut="NY")
        table = surface.smile_table(expiry, method="SVI", cut="NY")
        by = {r["label"]: r["vol"] for r in table}
        ref = {"atm": by["ATM"], "rr25": by["25d call"] - by["25d put"],
               "fly25": 0.5 * (by["25d call"] + by["25d put"]) - by["ATM"]}
        err = moments.reconstruction_error(d, surface.conv, ref)
        for key, value in err.items():
            self.assertLess(abs(value), 2e-4, msg=f"{key} came back {value * 100:+.4f} vol points")

    def test_two_different_expiries_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            moments.combine(_flat_distribution(0.1, 0.5), _flat_distribution(0.1, 0.25),
                            (1, 1), 0.3, DeltaConvention(False))


class TestCrossDependence(unittest.TestCase):
    """The marked dependence the smile triangle ties a cross's legs together with.

    The Gaussian copula put every cross butterfly below the market: it ties
    the size of the legs' moves together only as the correlation's square, and
    it gives the correlation no volatility.  ``moments.Dependence`` marks both.
    """

    @classmethod
    def setUpClass(cls):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURJPY"])
        book.feed = MarketFeed.load(FEED)
        surface, curve, (leg_a, leg_b), cls.co = analytics._cross_legs(book, "EURJPY")
        cls.t = surface.tenor_years("6m")
        expiry = ASOF.datetime_from_years(cls.t)
        cls.rho = float(curve.correlation(cls.t))
        cls.conv = surface.slice_conv(cls.t)
        cls.da = moments.distribution_from_surface(book[leg_a], expiry, method="SVI", cut="NY")
        cls.db = moments.distribution_from_surface(book[leg_b], expiry, method="SVI", cut="NY")
        cls.gauss = moments.combine(cls.da, cls.db, cls.co, cls.rho, cls.conv)
        cls.gauss_table = cls.gauss.table()

    def held(self, **dependence):
        return moments.combine_holding_atm(self.da, self.db, self.co, self.rho, self.conv,
                                           moments.Dependence(**dependence),
                                           target_atm=self.gauss_table["atm"])

    def test_nothing_marked_is_the_gaussian_copula_to_the_last_digit(self):
        for dependence in (None, moments.Dependence()):
            law = moments.combine(self.da, self.db, self.co, self.rho, self.conv,
                                  dependence=dependence)
            np.testing.assert_array_equal(law.xc, self.gauss.xc)
            np.testing.assert_array_equal(law.weight, self.gauss.weight)
        law = moments.combine_holding_atm(self.da, self.db, self.co, self.rho, self.conv, None)
        self.assertEqual(law.table(), self.gauss_table)

    def test_the_regime_grid_with_no_regimes_reproduces_the_gaussian_copula(self):
        """The coarser score grid and the binning the dependent law runs on are
        pinned against the Gaussian copula they reduce to: a correlation vol
        too small to move anything, and no regimes."""
        law = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                         moments.Dependence(corr_vol=1e-12))
        got = law.table()
        for key in ("atm", "rr25", "fly25", "rr10", "fly10"):
            self.assertLess(abs(got[key] - self.gauss_table[key]), 1e-5,
                            msg=f"{key} {got[key] * 100:.4f} against {self.gauss_table[key] * 100:.4f}")

    def test_a_marked_dependence_moves_the_wings_and_holds_the_atm(self):
        flies = []
        for vol_vol in (0.0, 0.5, 1.0):
            law = self.held(vol_vol=vol_vol)
            got = law.table()
            self.assertLess(abs(got["atm"] - self.gauss_table["atm"]), 2e-6, msg=vol_vol)
            flies.append((got["fly25"], got["fly10"]))
            self.assertEqual(law.vol_vol, vol_vol)
            self.assertGreater(min(law.leg_kurtosis), 0.0)
        # The more the legs' variance regimes coincide, the fatter the cross.
        self.assertLess(flies[0][0], flies[1][0])
        self.assertLess(flies[1][0], flies[2][0])
        self.assertLess(flies[0][1], flies[2][1])
        # Coinciding always, the legs give a fatter cross than the Gaussian
        # copula does; never coinciding, a thinner one -- which is why zero is
        # not where a desk starts marking, and the class says so.
        self.assertGreater(flies[2][0], self.gauss_table["fly25"])
        self.assertLess(flies[0][0], self.gauss_table["fly25"])

    def test_a_correlation_vol_fattens_the_cross_and_holds_the_atm(self):
        got = self.held(corr_vol=0.2).table()
        self.assertLess(abs(got["atm"] - self.gauss_table["atm"]), 2e-6)
        self.assertGreater(got["fly25"], self.gauss_table["fly25"])
        self.assertGreater(got["fly10"], self.gauss_table["fly10"])

    def test_the_held_atm_is_held_by_the_copula_correlation(self):
        law = self.held(vol_vol=1.0)
        self.assertNotAlmostEqual(law.rho, self.rho, places=3)
        # Rebuilt at that correlation without the solve, it is the same law.
        again = moments.combine(self.da, self.db, self.co, law.rho, self.conv,
                                dependence=moments.Dependence(vol_vol=1.0))
        self.assertAlmostEqual(again.table()["fly25"], law.table()["fly25"], places=12)

    def test_the_implied_vol_vol_recovers_the_one_a_butterfly_was_built_from(self):
        target = self.held(vol_vol=0.6).table()["fly25"]
        got, note = moments.implied_vol_vol(self.da, self.db, self.co, self.rho, self.conv,
                                            target, target_atm=self.gauss_table["atm"])
        self.assertEqual(note, "")
        self.assertAlmostEqual(got, 0.6, delta=0.03)

    def test_a_butterfly_no_vol_vol_reaches_says_how_far_the_legs_go(self):
        high, why = moments.implied_vol_vol(self.da, self.db, self.co, self.rho, self.conv,
                                            5.0 * self.gauss_table["fly25"])
        self.assertIsNone(high)
        self.assertIn("correlation vol", why)
        low, why = moments.implied_vol_vol(self.da, self.db, self.co, self.rho, self.conv, 0.0)
        self.assertIsNone(low)
        self.assertIn("thinner than any variance regimes", why)

    def test_a_lean_moves_the_risk_reversal_and_holds_the_atm_and_the_fly(self):
        """The vol-vol correlation and the correlation vol are radially
        symmetric and leave the cross's risk reversal at its legs' skews.  The
        correlation-spot correlation is what moves it: the high correlation
        leaning onto the falling cross (negative) widens a product cross's
        downside, and the fly is left where the other two put it."""
        base = self.held(vol_vol=0.5, corr_vol=0.15).table()
        moved = {}
        for zeta in (-0.7, -0.3, 0.3, 0.7):
            law = self.held(vol_vol=0.5, corr_vol=0.15, corr_spot=zeta)
            got = law.table()
            self.assertEqual(law.corr_spot, zeta)
            self.assertLess(abs(got["atm"] - self.gauss_table["atm"]), 2e-6, msg=zeta)
            for key in ("fly25", "fly10"):
                self.assertLess(abs(got[key] - base[key]), 5e-4, msg=f"{zeta} {key}")
            moved[zeta] = (got["rr25"] - base["rr25"], got["rr10"] - base["rr10"])
        self.assertEqual(self.co[0] * self.co[1], 1)
        self.assertLess(moved[-0.7][0], moved[-0.3][0])
        self.assertLess(moved[-0.3][0], -1e-3)
        self.assertGreater(moved[0.3][0], 1e-3)
        self.assertLess(moved[0.3][0], moved[0.7][0])
        self.assertLess(moved[-0.7][1], moved[-0.7][0])          # more in the wing
        # Nearly odd in the lean: the legs' own skews sit underneath both.
        self.assertAlmostEqual(moved[0.7][0], -moved[-0.7][0], delta=5e-4)

    def test_no_lean_is_the_regime_copula_the_leaning_tables_reduce_to(self):
        """A lean too small to move anything runs the leaning score tables,
        which integrate each leg's score law numerically; they are pinned
        against the closed-form mixture law the unleaned copula reads."""
        for vol_vol in (None, 0.5):
            plain = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                               moments.Dependence(vol_vol, 0.15)).table()
            lean = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                              moments.Dependence(vol_vol, 0.15, 1e-9)).table()
            for key in ("atm", "rr25", "fly25", "rr10", "fly10"):
                self.assertLess(abs(lean[key] - plain[key]), 5e-6, msg=f"{vol_vol} {key}")

    def test_a_lean_keeps_each_legs_own_marginal(self):
        """Tied to a partner that never moves, a leg must come back as itself
        however hard the correlation leans -- the lean reweights the joint law
        and the score tables undo what that does to each leg on its own."""
        flat = moments.Distribution(x=np.array([-1e-9, 0.0, 1e-9]),
                                    pdf=np.array([0.0, 1e9, 0.0]),
                                    cdf=np.array([0.0, 0.5, 1.0]), t=self.da.t)
        conv = DeltaConvention(False)
        # Against the same leg on the same grid with no lean, so what is
        # measured is the lean and not the two grids' difference.  A partner
        # that never moves is the hardest case for the lean's quadrature --
        # the whole reweighting falls on a direction the payoff ignores -- so
        # the grid is refined until what is left is the marginal: on the
        # default grid this case is off by a hundredth of a vol point and
        # converging, and a real cross is within a thousandth there.
        def law(zeta):
            return moments._combine_dependent(self.da, flat, (1, 1), 0.3, conv,
                                              moments.Dependence(None, 0.3, zeta),
                                              nodes=481).table()
        alone = law(0.0)
        for zeta in (-0.7, moments.MAX_CORR_SPOT):
            got = law(zeta)
            for key in ("atm", "rr25", "fly25", "rr10", "fly10"):
                self.assertLess(abs(got[key] - alone[key]), 3e-5,
                                msg=f"{zeta} {key} {got[key] * 100:.4f} against {alone[key] * 100:.4f}")

    def test_the_marked_number_is_the_correlation_the_lean_carries(self):
        """``corr_spot`` is marked as a correlation -- the one history measures --
        and the two-state law carries it by a lean whose correlation with the
        cross's score is exactly ``zeta sqrt(2/pi)``.  Checked on the law itself:
        the state's sign against the standardised score, over a fine grid."""
        from scipy.special import ndtr
        z = np.linspace(-9.0, 9.0, 200001)
        pdf = np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        for c in (-0.5, 0.3, moments.MAX_CORR_SPOT):
            zeta = moments.Dependence(corr_vol=0.1, corr_spot=c).lean
            p_high = 0.5 * np.asarray(moments._lean(zeta, z))       # P(high state | D)
            got = float(np.trapezoid(pdf * z * (2.0 * p_high - 1.0), z))
            self.assertAlmostEqual(got, c, places=6, msg=c)
        self.assertEqual(moments.Dependence(corr_spot=0.95).lean, 1.0)
        # More than two states can carry is priced at a full lean, and said.
        full = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                          moments.Dependence(0.5, 0.15, moments.MAX_CORR_SPOT))
        over = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                          moments.Dependence(0.5, 0.15, 0.95))
        np.testing.assert_array_equal(over.weight, full.weight)
        self.assertTrue(any("more than two correlation states" in w for w in over.warnings))
        self.assertEqual(over.corr_spot, 0.95)

    def test_a_lean_with_no_correlation_vol_moves_nothing_and_says_so(self):
        plain = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                           moments.Dependence(0.5))
        lean = moments._combine_dependent(self.da, self.db, self.co, self.rho, self.conv,
                                          moments.Dependence(0.5, 0.0, -0.8))
        np.testing.assert_array_equal(lean.xc, plain.xc)
        np.testing.assert_array_equal(lean.weight, plain.weight)
        self.assertTrue(any("moves nothing" in w for w in lean.warnings), lean.warnings)
        self.assertTrue(moments.Dependence(corr_spot=-0.8).active)

    def test_the_implied_corr_spot_recovers_the_one_a_risk_reversal_was_built_from(self):
        target = self.held(vol_vol=0.5, corr_vol=0.15, corr_spot=-0.4).table()["rr25"]
        got, note = moments.implied_corr_spot(self.da, self.db, self.co, self.rho, self.conv,
                                              target, vol_vol=0.5, corr_vol=0.15,
                                              target_atm=self.gauss_table["atm"])
        self.assertEqual(note, "")
        self.assertAlmostEqual(got, -0.4, delta=0.03)
        far, why = moments.implied_corr_spot(self.da, self.db, self.co, self.rho, self.conv,
                                             target - 0.05, vol_vol=0.5, corr_vol=0.15,
                                             target_atm=self.gauss_table["atm"])
        self.assertIsNone(far)
        self.assertIn("more correlation vol", why)
        none, why = moments.implied_corr_spot(self.da, self.db, self.co, self.rho, self.conv,
                                              target, vol_vol=0.5)
        self.assertIsNone(none)
        self.assertIn("none is marked", why)

    def test_a_dependence_outside_its_range_is_refused(self):
        for bad in ({"vol_vol": 1.2}, {"vol_vol": float("nan")}, {"corr_vol": 1.0},
                    {"corr_vol": -0.1}, {"corr_spot": 1.2}, {"corr_spot": float("nan")}):
            with self.assertRaises(ValueError, msg=bad):
                moments.Dependence(**bad)
        with self.assertRaises(ValueError):
            moments.combine(self.da, self.db, self.co, 0.95, self.conv,
                            dependence=moments.Dependence(corr_vol=0.1))

    def test_a_smile_with_no_kurtosis_has_no_regimes(self):
        self.assertEqual(moments.regime_dispersion(0.0), 0.0)
        self.assertEqual(moments.regime_dispersion(-0.4), 0.0)
        self.assertAlmostEqual(moments.regime_dispersion(3.0), math.log(2.0), places=15)

        class Lognormal(moments.Distribution):
            # A flat smile's grid carries a trace of kurtosis from its own
            # differencing; this is the lognormal it stands for, exactly.
            def moments(self):
                m = super().moments()
                return moments.Moments(m.mean, m.variance, 0.0, 0.0)

        flat_a, flat_b = (Lognormal(x=d.x, pdf=d.pdf, cdf=d.cdf, t=d.t)
                          for d in (_flat_distribution(v, 0.5) for v in (0.10, 0.12)))
        law = moments.combine(flat_a, flat_b, (1, 1), 0.3, DeltaConvention(False),
                              dependence=moments.Dependence(vol_vol=0.5))
        self.assertTrue(any("no variance regimes" in w for w in law.warnings), law.warnings)


class TestSmileParameterShifts(unittest.TestCase):
    def test_a_shift_moves_the_level_and_keeps_the_term_structure(self):
        """An overwrite replaces a parameter and flattens its term structure;
        re-marking a wing against a broker run should move its level and leave
        the shape alone."""
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = [surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)]
        self.assertEqual(surface.set_param_shifts({"rho25": 0.03}), [])
        after = [surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)]
        for a, b in zip(after, before):
            self.assertAlmostEqual(a - b, 0.03, places=12)
        surface.clear_param_shifts()
        self.assertEqual([surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)], before)

    def test_an_unknown_parameter_is_refused(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        problems = book["EURUSD"].set_param_shifts({"vega": 0.1})
        self.assertTrue(problems)
        self.assertIn("unknown smile parameter", problems[0])

    def test_a_clamped_shift_is_reported_rather_than_absorbed(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        surface.set_param_shifts({"rho25": 1.4})
        self.assertLessEqual(abs(surface.params_at(0.25)["rho25"]), 0.999)
        self.assertTrue(any("clamped" in w for w in surface.shift_warnings()))


class TestCurveInvalidation(unittest.TestCase):
    def test_a_parameter_change_keeps_the_weight_profile_it_cannot_have_changed(self):
        """The intraday weight profile is a pure function of the pair, the
        clock and the horizon.  Dropping it on every backbone change cost about
        20ms against 2ms of integrals that genuinely had to go -- a 17x tax on
        every re-mark, which the market-maker fit pays thousands of times.
        """
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        atm = book["EURUSD"].atm
        atm.term_vol(1.0)
        cached = len(atm.weighting._cache)
        self.assertGreater(cached, 0)
        before = atm.term_vol(0.25)
        atm.set_params(initial_vol=atm.params.initial_vol * 1.10)
        self.assertEqual(len(atm.weighting._cache), cached)
        self.assertNotAlmostEqual(atm.term_vol(0.25), before)


if __name__ == "__main__":
    unittest.main()
