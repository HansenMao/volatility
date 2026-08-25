"""Test suite for volkit.

Uses ``unittest`` rather than pytest so it runs on a bare Python install --
the same reason the web interface is stdlib-only.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volkit import black, sabr, smile
from volkit.atm import AtmCurve, BackboneParams
from volkit.black import DeltaConvention
from volkit.book import Book
from volkit.calendars import CalendarSet, easter
from volkit.cross import CorrelationCurve, CrossAtmCurve, infer_leg_signs
from volkit import exotics
from volkit.banded import Band, BetaBandSmile, JumpSpec, calibrate_band_smile, load_bands
from volkit.econ import EconCalendar, generate_nfp, generate_us_cpi, nth_weekday
from volkit.feed import FeedError, MarketFeed, pip_divisor
from volkit import analytics, history, listed, marketmaker, moments, quotes
from volkit.events import EventSchedule
from volkit.knowledge import KnowledgeBank, PairKnowledge, Rule, suggest_rules
from volkit.marketdata import ExcelSource, MarketDataError
from volkit.numerics import ConvergenceError, fixed_point, integrate_piecewise, solve_scalar
from volkit.pricing import OptionLeg, StrikeSpec, parse_strike, price_strip, resolve_expiry
from volkit.smile import SmileSlice, fit_svi
from volkit.surface import PARAM_NAMES, SmileMark, VolSurface, fit_param_term_structure
from volkit.timeutil import Clock, TenorError, UTC, add_tenor, parse_datetime, tenor_to_years
from volkit.timeweight import DEFAULT_SESSION_HOURS, TimeWeighting, session_shares

WORKBOOK = Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx"
ASOF = Clock(datetime(2024, 2, 28, 12, 0, tzinfo=UTC))


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
        self.curve.overwrite_tenor("3m", 0.09)
        self.assertAlmostEqual(self.curve.term_vol(tenor_to_years("3m")), 0.09, places=8)
        self.curve.clear_overwrite("3m")
        self.assertNotAlmostEqual(self.curve.term_vol(tenor_to_years("3m")), 0.09, places=4)

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


class TestEvents(unittest.TestCase):
    def test_calibrated_height_reproduces_the_quoted_bump(self):
        """The bump is quoted over the event's own 24 hours, so that is where
        it must be delivered exactly."""
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        when = datetime(2024, 3, 20, 18, 0, tzinfo=UTC)
        ev = curve.events.add(when, 0.030, "FOMC")
        self.assertEqual(curve.calibrate_events(), [])
        self.assertAlmostEqual(curve.achieved_bump(ev), 0.030, places=9)

    def test_event_height_is_stable_across_the_day_roll(self):
        """Under the legacy vol-day reading an event a minute before the 14:00
        roll needed a 12x height and dumped the overflow on the next day."""
        heights = {}
        for minute, mode in ((59, "forward24h"), (1, "forward24h")):
            curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0),
                             ASOF, events=EventSchedule(window_mode=mode))
            hour = 13 if minute == 59 else 14
            curve.events.add(datetime(2024, 3, 20, hour, minute, tzinfo=UTC), 0.02, "E")
            curve.calibrate_events()
            heights[minute] = curve.events.events[0].height
        self.assertLess(abs(heights[59] / heights[1] - 1.0), 0.10)

    def test_clustered_events_each_deliver_their_own_bump(self):
        """Solved independently, two nearby events each stop delivering their
        quote once the other is switched on."""
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        a = curve.events.add(datetime(2024, 3, 20, 16, 0, tzinfo=UTC), 0.02, "A")
        b = curve.events.add(datetime(2024, 3, 20, 18, 0, tzinfo=UTC), 0.03, "B")
        curve.calibrate_events()
        self.assertAlmostEqual(curve.achieved_bump(a), 0.02, places=8)
        self.assertAlmostEqual(curve.achieved_bump(b), 0.03, places=8)

    def test_weekend_and_holiday_events_are_flagged(self):
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        curve.events.add(datetime(2024, 3, 23, 16, 0, tzinfo=UTC), 0.02, "SAT")
        problems = curve.calibrate_events()
        self.assertTrue(any("weekly market closure" in p for p in problems), problems)

    def test_event_does_not_leak_into_unrelated_days(self):
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        base = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF)
        curve.events.add(datetime(2024, 3, 20, 18, 0, tzinfo=UTC), 0.030, "FOMC")
        curve.calibrate_events()
        far = datetime(2024, 3, 25, 12, tzinfo=UTC)
        self.assertAlmostEqual(curve.daily_vol(far), base.daily_vol(far), places=12)

    def test_addon_is_vectorised(self):
        sched = EventSchedule()
        ev = sched.add(datetime(2024, 3, 1, 12, tzinfo=UTC), 0.02, "x")
        ev.height = 0.5
        sched.refresh(ASOF)
        t = np.linspace(0, 0.05, 100)
        self.assertEqual(sched.addon(t).shape, t.shape)


class TestCalendars(unittest.TestCase):
    def test_easter(self):
        self.assertEqual(easter(2024), date(2024, 3, 31))
        self.assertEqual(easter(2025), date(2025, 4, 20))
        self.assertEqual(easter(2027), date(2027, 3, 28))

    def test_holiday_lookup(self):
        c = CalendarSet()
        self.assertTrue(c.is_holiday("USDJPY", date(2024, 7, 4)))
        self.assertIn("JP", c.holiday_countries("USDJPY", date(2024, 4, 29)))
        self.assertFalse(c.is_holiday("EURGBP", date(2024, 7, 4)))

    def test_overrides_add_dates_without_code_changes(self):
        """Legacy left '# manually add Chinese holidays' as a TODO."""
        c = CalendarSet()
        self.assertFalse(c.is_holiday("USDCNH", date(2025, 1, 29)))
        c.add_overrides("CN", ["2025-01-29"])
        self.assertTrue(c.is_holiday("USDCNH", date(2025, 1, 29)))

    def test_spot_and_expiry(self):
        c = CalendarSet()
        self.assertEqual(c.spot_lag("USDCAD"), 1)
        self.assertEqual(c.spot_lag("USDJPY"), 2)
        exp = c.expiry_date("USDJPY", "1M", date(2024, 2, 28))
        self.assertTrue(c.is_business_day("USDJPY", exp))


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
        book = Book.from_excel(WORKBOOK, ASOF).build(["AUDJPY"])
        t = tenor_to_years("3m")
        cross = book["AUDJPY"].atm.term_vol(t)
        self.assertGreater(cross, book["AUDUSD"].atm.term_vol(t))
        self.assertGreater(cross, book["USDJPY"].atm.term_vol(t))


class TestSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
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


class TestMarketData(unittest.TestCase):
    def test_real_workbook_loads_without_problems(self):
        data = ExcelSource(WORKBOOK).load()
        self.assertEqual(data.problems, [], data.problems)
        self.assertIn("USDJPY", data.pairs)
        self.assertTrue(data.pairs["AUDJPY"].is_cross)
        self.assertEqual(data.pairs["AUDJPY"].legs, ("AUDUSD", "USDJPY"))

    def test_units_are_converted_once(self):
        data = ExcelSource(WORKBOOK).load()
        self.assertAlmostEqual(data.params["USDJPY"].initial, 0.0605)
        self.assertAlmostEqual(data.marks["USDJPY"][0].st_25, 0.002175)

    def test_cross_correlations_are_not_rescaled(self):
        data = ExcelSource(WORKBOOK).load()
        self.assertAlmostEqual(data.params["AUDJPY"].initial, 0.37)

    def test_missing_file_raises_clearly(self):
        with self.assertRaises(MarketDataError):
            ExcelSource("/nonexistent/nope.xlsx")

    def test_premium_adjustment_follows_the_pair(self):
        data = ExcelSource(WORKBOOK).load()
        self.assertTrue(data.pairs["USDJPY"].resolved_premium_adjusted())
        self.assertFalse(data.pairs["EURUSD"].resolved_premium_adjusted())


class TestBook(unittest.TestCase):
    def test_build_order_puts_legs_first(self):
        book = Book.from_excel(WORKBOOK, ASOF)
        order = book.build_order()
        self.assertLess(order.index("AUDUSD"), order.index("AUDJPY"))
        self.assertLess(order.index("USDJPY"), order.index("AUDJPY"))

    def test_requesting_a_cross_pulls_in_its_legs(self):
        book = Book.from_excel(WORKBOOK, ASOF).build(["AUDJPY"])
        self.assertIn("AUDUSD", book.surfaces)
        self.assertIn("USDJPY", book.surfaces)

    def test_past_events_are_reported_not_silently_used(self):
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
        self.assertTrue(any("before the valuation time" in w for w in book.warnings))

    def test_tenor_points_available_without_crosses(self):
        """Legacy set self.tenor_points inside the cross loop only."""
        book = Book.from_excel(WORKBOOK, ASOF)
        self.assertTrue(book.data.tenor_points)

    def test_unknown_pair_raises_with_a_useful_message(self):
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
        with self.assertRaises(KeyError) as ctx:
            book["NOPE"]
        self.assertIn("available", str(ctx.exception))


class TestPricing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY", "EURUSD"])

    def test_strike_spec_forms(self):
        self.assertEqual(parse_strike("ATM").kind, "atm")
        self.assertEqual(parse_strike("").kind, "atm")
        self.assertEqual(parse_strike("1.0234").value, 1.0234)
        self.assertEqual((parse_strike("25d").value, parse_strike("25d").is_call), (0.25, True))
        self.assertFalse(parse_strike("10dp").is_call)
        self.assertFalse(parse_strike("-25d").is_call)
        self.assertTrue(parse_strike("25dp").side_explicit)
        self.assertFalse(parse_strike("25d").side_explicit)

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
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        for mark in book.data.marks["USDJPY"][:4]:
            t = tenor_to_years(mark.tenor)
            atm = surface.atm.cut_vol(ASOF.datetime_from_years(t), "NY")
            cal = sabr.calibrate(atm, mark.rr_25, mark.st_25, 0.25, t,
                                 surface.conv, max_solutions=3)
            self.assertEqual(cal.alternatives, (), f"{mark.tenor}: {cal.warnings}")


class TestEconCalendar(unittest.TestCase):
    def setUp(self):
        self.cal = EconCalendar.load()

    def test_shipped_calendar_loads(self):
        self.assertGreater(len(self.cal.dated), 20)

    def test_nfp_is_the_first_friday(self):
        for e in generate_nfp(2026):
            self.assertEqual(e.when.astimezone(ZoneInfo("America/New_York")).weekday(), 4)

    def test_nfp_release_time_follows_us_dst(self):
        """08:30 New York is 13:30 UTC in winter and 12:30 UTC in summer.
        A hand-kept UTC list gets this wrong twice a year."""
        by_month = {e.when.month: e.when.hour for e in generate_nfp(2026)}
        self.assertEqual(by_month[1], 13)
        self.assertEqual(by_month[7], 12)
        self.assertEqual(by_month[12], 13)

    def test_nfp_shifts_off_a_holiday(self):
        jan = generate_nfp(2027)[0]
        self.assertEqual(jan.when.date(), date(2027, 1, 8))
        self.assertIn("shifted", jan.source)

    def test_events_are_filtered_by_pair_currency(self):
        start = datetime(2026, 8, 23, tzinfo=UTC)
        end = datetime(2026, 12, 31, tzinfo=UTC)
        names = {e.name for e in self.cal.for_pair("USDJPY", start, end)}
        self.assertIn("FOMC", names)
        self.assertNotIn("ECB", names)
        self.assertIn("ECB", {e.name for e in self.cal.for_pair("EURUSD", start, end)})

    def test_approximate_events_are_flagged_and_off_by_default(self):
        self.assertTrue(all(e.approximate for e in generate_us_cpi(2026)))
        self.assertNotIn("US CPI", self.cal.rules)

    def test_unknown_event_name_in_csv_raises(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("NOTATHING,2026-01-01\n")
            path = fh.name
        with self.assertRaises(ValueError):
            EconCalendar.load(path)


class TestCurveMarking(unittest.TestCase):
    def setUp(self):
        self.book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY", "AUDJPY"])

    def test_backbone_parameters_can_be_remarked(self):
        atm = self.book["USDJPY"].atm
        before = atm.term_vol(1.0)
        self.assertEqual(atm.set_params(long_term_vol=0.09), [])
        self.assertGreater(atm.term_vol(1.0), before)

    def test_bad_parameter_is_rejected_and_curve_unchanged(self):
        atm = self.book["USDJPY"].atm
        before = atm.term_vol(1.0)
        self.assertTrue(atm.set_params(long_term_vol=-1.0))
        self.assertAlmostEqual(atm.term_vol(1.0), before, places=14)

    def test_cross_correlation_can_be_remarked(self):
        atm = self.book["AUDJPY"].atm
        self.assertIsInstance(atm, CrossAtmCurve)
        before = atm.term_vol(1.0)
        self.assertEqual(atm.set_correlation(0.1, 0.1, 1.0), [])
        self.assertNotAlmostEqual(atm.term_vol(1.0), before, places=6)
        self.assertTrue(atm.set_correlation(1.5, 0.1, 1.0))

    def test_events_can_be_set_and_reprice_their_bump(self):
        atm = self.book["USDJPY"].atm
        base = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])["USDJPY"].atm
        # 16:00 UTC sits just after the 14:00 roll, so the spike has its whole
        # volatility day ahead of it and no leakage warning is expected.
        when = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        self.assertEqual(atm.set_events([(when, 0.02, "TEST")]), [])
        self.assertAlmostEqual(atm.achieved_bump(atm.events.events[0]), 0.02, places=9)
        # It still lifts the calendar volatility day, just not by the same amount.
        self.assertGreater(atm.daily_vol(when) - base.daily_vol(when), 0.01)

    def test_event_near_the_day_roll_no_longer_distorts(self):
        """Under the legacy vol-day reading, a release shortly before 14:00 UTC
        was calibrated against a sliver of its own day: the height exploded and
        the overflow landed on the next day.  Forward-24h windows remove it."""
        atm = self.book["USDJPY"].atm
        early = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        late = (ASOF.now + timedelta(days=37)).replace(hour=13, minute=45, second=0, microsecond=0)
        problems = atm.set_events([(early, 0.015, "EARLY"), (late, 0.015, "LATE")])
        self.assertFalse(any("volatility-day roll" in p for p in problems), problems)
        heights = {e.label: e.height for e in atm.events.events}
        self.assertLess(abs(heights["LATE"] / heights["EARLY"] - 1.0), 0.35)
        for ev in atm.events.events:
            self.assertAlmostEqual(atm.achieved_bump(ev), 0.015, places=9)

    def test_legacy_vol_day_mode_still_available(self):
        from volkit.events import EventSchedule as ES
        atm = self.book["USDJPY"].atm
        atm.events = ES(window_mode="vol_day")
        late = (ASOF.now + timedelta(days=30)).replace(hour=13, minute=45, second=0, microsecond=0)
        problems = atm.set_events([(late, 0.015, "LATE")])
        self.assertTrue(any("volatility-day roll" in p for p in problems), problems)

    def test_setting_events_clears_the_previous_schedule(self):
        atm = self.book["USDJPY"].atm
        a = ASOF.now + timedelta(days=20)
        b = ASOF.now + timedelta(days=40)
        atm.set_events([(a, 0.01, "A"), (b, 0.01, "B")])
        self.assertEqual(len(atm.events.events), 2)
        atm.set_events([(b, 0.02, "B")])
        self.assertEqual([e.label for e in atm.events.events], ["B"])


FEED = Path(__file__).resolve().parents[1] / "files" / "market_feed.csv"


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


class TestFeed(unittest.TestCase):
    def setUp(self):
        self.feed = MarketFeed.load(FEED)

    def test_sample_feed_loads_clean(self):
        self.assertEqual(self.feed.problems, [], self.feed.problems)
        self.assertIn("USDJPY", self.feed)

    def test_pip_divisor_by_term_currency(self):
        self.assertEqual(pip_divisor("USDJPY"), 100.0)
        self.assertEqual(pip_divisor("EURUSD"), 10000.0)

    def test_interpolation_is_exact_at_the_pillars(self):
        pf = self.feed.pairs["USDJPY"]
        for tenor, pts in zip(pf.tenors, pf.points):
            got, _ = pf.forward_points(tenor_to_years(tenor))
            self.assertAlmostEqual(got, pts, places=10)

    def test_interpolation_between_pillars_is_bracketed(self):
        pf = self.feed.pairs["USDJPY"]
        lo, _ = pf.forward_points(tenor_to_years("1m"))
        hi, _ = pf.forward_points(tenor_to_years("2m"))
        mid, extrap = pf.forward_points(0.5 * (tenor_to_years("1m") + tenor_to_years("2m")))
        self.assertFalse(extrap)
        self.assertTrue(min(lo, hi) < mid < max(lo, hi))

    def test_points_go_to_zero_at_the_very_front(self):
        pts, _ = self.feed.pairs["USDJPY"].forward_points(0.0)
        self.assertAlmostEqual(pts, 0.0, places=12)

    def test_beyond_the_last_pillar_is_flagged_not_trended(self):
        pf = self.feed.pairs["USDJPY"]
        far, extrap = pf.forward_points(5.0)
        last, _ = pf.forward_points(tenor_to_years("1y"))
        self.assertTrue(extrap)
        self.assertAlmostEqual(far, last, places=10)

    def test_quote_is_json_safe(self):
        import json
        q = self.feed.quote("USDJPY", 0.2)
        self.assertIsInstance(q["extrapolated"], bool)
        json.dumps(q)   # must not need a default= coercion

    def test_missing_file_and_unknown_pair_raise(self):
        with self.assertRaises(FeedError):
            MarketFeed.load("/nonexistent/feed.csv")
        with self.assertRaises(FeedError):
            self.feed.quote("XXXYYY", 0.2)


class TestExoticLegs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
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
        """The marking check the implied-vs-quoted table displays."""
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        surface = book["USDJPY"]
        mark = {m.tenor.upper(): m for m in book.data.marks["USDJPY"]}["3M"]
        expiry = ASOF.datetime_from_years(tenor_to_years("3m"))
        loose = abs(surface.risk_reversal(expiry, 0.25) - mark.rr_25)
        surface.anchor_tenors = True
        surface._slices.clear()
        tight = abs(surface.risk_reversal(expiry, 0.25) - mark.rr_25)
        self.assertLess(tight, loose)
        self.assertLess(tight, 1e-4)


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

    def test_bands_file_loads_and_rejects_degenerate_rows(self):
        bands = load_bands(Path(__file__).resolve().parents[1] / "files" / "bands.csv")
        self.assertIn("USDHKD", bands)
        self.assertEqual(bands["USDHKD"].lower, 7.75)


class TestBandGuard(unittest.TestCase):
    def test_surface_flags_a_strike_outside_the_band(self):
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
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
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
        surface = book["USDJPY"]
        surface.band = Band("USDHKD", 7.75, 7.85)
        self.assertTrue(surface.band_check(7.90, 7.80))
        surface.set_band_treatment(BandTreatment(mode="off"))
        self.assertEqual(surface.band_check(7.90, 7.80), [])
        surface.set_band_treatment(BandTreatment(mode="mixture"))
        self.assertEqual(surface.band_check(7.90, 7.80, method="BAND"), [])
        # Marked as a mixture but priced lognormally: that is worth saying.
        self.assertIn("switch the method to BAND", surface.band_check(7.90, 7.80, "SVI")[0])

    def test_the_band_edges_can_be_overridden_on_the_screen(self):
        from volkit.banded import BandTreatment
        book = Book.from_excel(WORKBOOK, ASOF).build(["USDJPY"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        with self.assertRaises(ValueError) as ctx:
            book["EURUSD"].vol(1.0, self.EXPIRY, "BAND", "NY")
        self.assertIn("bands.csv", str(ctx.exception))

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

    def test_decimal_and_percent_are_both_understood(self):
        dec = listed.parse_quote_table("1.05 0.0820\n1.08 0.0750\n1.10 0.0765\n")
        pct = listed.parse_quote_table("1.05 8.20\n1.08 7.50\n1.10 7.65\n")
        self.assertEqual(dec.vol_unit, "decimal")
        self.assertEqual(pct.vol_unit, "percent")
        for a, b in zip(dec.quotes, pct.quotes):
            self.assertAlmostEqual(a.vol, b.vol, places=12)

    def test_a_table_straddling_one_is_refused_rather_than_guessed(self):
        """0.95 could be 95% or 0.95%.  Guessing would move a mark silently."""
        with self.assertRaises(ValueError) as cm:
            listed.parse_quote_table("1.05\t0.95\n1.08\t8.20\n1.10\t7.70\n")
        self.assertIn("percent or decimals", str(cm.exception))

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
        with self.assertRaises(ValueError):
            listed.resolve_underlying("NOT_A_CONTRACT")

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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        out = self._panel_from_the_book("6J", "USDJPY", 150.0, book).run(book)
        for row in out["rows"]:
            self.assertAlmostEqual(row["book_vol"], row["market_vol"], places=6)
        self.assertEqual(out["comparison"]["pair"], "USDJPY")
        self.assertAlmostEqual(out["comparison"]["forward_fx"], 150.0, places=9)

    def test_an_upright_table_maps_back_onto_the_same_marks(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        out = self._panel_from_the_book("6E", "EURUSD", 1.0850, book).run(book)
        for row in out["rows"]:
            self.assertAlmostEqual(row["book_vol"], row["market_vol"], places=6)

    def test_the_book_delta_strikes_come_back_on_the_listed_axis(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        out = self._panel_from_the_book("6J", "USDJPY", 150.0, book).run(book)
        self.assertAlmostEqual(out["years"], ASOF.years_to(self.EXPIRY), places=12)
        self.assertEqual(out["valuation"][:16], ASOF.now.isoformat()[:16])

    def test_a_past_expiry_is_rejected_with_both_times(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        panel = self._panel_from_the_book("6J", "USDJPY", 150.0, book)
        panel.expiry = "2023-01-01 19:00"
        with self.assertRaises(ValueError) as cm:
            panel.run(book)
        self.assertIn("not in the future", str(cm.exception))

    def test_an_unmapped_contract_fits_but_does_not_compare(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        out = listed.panel_from_request({
            "underlying": "CUSTOM", "expiry": "2024-06-14 19:00", "forward": 4200.0,
            "text": "Strike\tIV\n3800\t18.4\n4000\t16.9\n4200\t15.8\n4400\t15.4\n4600\t15.6\n",
        }).run(book, clock=ASOF)
        self.assertIsNone(out["comparison"])
        self.assertTrue(out["fit"]["converged"])

    def test_a_pair_outside_the_book_is_named_in_the_error(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        panel = listed.panel_from_request({
            "underlying": "CUSTOM", "pair": "USDNOK", "expiry": "2024-06-14 19:00",
            "forward": 10.5, "text": "K\tIV\n10.0\t9.4\n10.5\t9.0\n11.0\t9.2\n"})
        with self.assertRaises(ValueError) as cm:
            panel.run(book)
        self.assertIn("USDNOK", str(cm.exception))

    def test_a_browser_datetime_is_accepted(self):
        """An HTML datetime-local field emits a 'T'; parse_datetime wants a space."""
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        panel = self._panel_from_the_book("6J", "USDJPY", 150.0, book)
        panel.expiry = "2024-06-14T19:00"
        self.assertAlmostEqual(panel.run(book)["years"], ASOF.years_to(self.EXPIRY), places=12)


HISTORY = Path(__file__).resolve().parents[1] / "files" / "history_sample.xlsx"
FEED = Path(__file__).resolve().parents[1] / "files" / "market_feed.csv"


def _flat_distribution(vol, t, n=1601, span=6.0):
    """A lognormal with no smile, built the same way a real one is."""
    sd = vol * math.sqrt(t)
    x = np.linspace(-span * sd, span * sd, n)
    k = np.exp(x)
    c = np.asarray(black.price(1.0, k, vol, t, True), dtype=float)
    cdf = 1.0 + np.gradient(c, x, edge_order=2) / k
    return moments.Distribution(x=x, pdf=np.gradient(cdf, x, edge_order=2), cdf=cdf, t=t)


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
        book = Book.from_excel(WORKBOOK, ASOF).load_all()
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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

    def test_a_percentile_locates_todays_mark_in_its_own_history(self):
        h = history.load_history(HISTORY, ["EURUSD"])
        series = h["EURUSD"].series("atm", "3M")
        lo, hi = float(np.min(series)), float(np.max(series))
        self.assertEqual(history.implied_stats(h["EURUSD"], 3650, "atm", "3M",
                                               current=lo - 0.01).percentile, 0.0)
        self.assertEqual(history.implied_stats(h["EURUSD"], 3650, "atm", "3M",
                                               current=hi + 0.01).percentile, 100.0)


class TestAnalysis(unittest.TestCase):
    """Carry and roll, fair value, and the cross triangle."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all()
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
        """EURJPY has marks but no feed, so the strike can only be held in moneyness."""
        rows = [r for r in analytics.carry_table(self.book, "EURJPY", horizon_days=30, cut="NY")
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

    def test_richness_is_implied_less_realized_less_the_roll_value(self):
        rows = analytics.fair_value_table(self.book, "EURUSD", self.history["EURUSD"],
                                          horizon_days=30, cut="NY")
        priced = [r for r in rows if r.fair is not None]
        self.assertTrue(priced)
        for r in priced:
            self.assertAlmostEqual(r.richness, r.implied - r.realized - r.roll_value, places=15)
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURGBP"])
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

    def test_a_pair_that_is_not_a_cross_has_no_triangle(self):
        with self.assertRaises(ValueError) as cm:
            analytics.triangle_table(self.book, "EURUSD", cut="NY")
        self.assertIn("not a cross", str(cm.exception))


class TestAnalysisApi(unittest.TestCase):
    def test_the_payload_carries_no_number_a_browser_cannot_parse(self):
        """Python's json writes NaN, which JSON.parse refuses.

        One unavailable cell would take the whole response down in the
        browser, so non-finite floats become null on the way out.
        """
        import json as _json
        from volkit.webapp import BookService, _finite
        service = BookService(str(WORKBOOK), ASOF, feed_path=str(FEED),
                              history_path=str(HISTORY))
        payload = service.analysis({"pair": "EURUSD", "cut": "NY", "horizon_days": "30",
                                    "lookback_days": "match", "noise": "0"})
        text = _json.dumps(_finite(payload), default=str)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(_json.loads(text)["pair"], "EURUSD")

    def test_sections_fail_independently(self):
        from volkit.webapp import BookService
        service = BookService(str(WORKBOOK), ASOF)      # no feed, no history
        payload = service.analysis({"pair": "EURUSD", "cut": "NY"})
        self.assertTrue(payload["carry"])               # still rolls, in moneyness
        self.assertIsNone(payload["realized"])
        self.assertIn("history", payload["unavailable"])
        self.assertIn("triangle", payload["unavailable"])


class TestCurveComparison(unittest.TestCase):
    """Several curves side by side, and the same curve on other dates."""

    def book(self, pairs=("EURUSD",)):
        return Book.from_excel(WORKBOOK, ASOF).load_all(list(pairs))

    def history(self, book):
        return history.load_history(HISTORY, book.pairs)

    def test_the_surface_and_the_quotes_differ_by_the_fit_residual(self):
        """The same comparison the marking screen's implied-vs-quoted table
        makes, extended across curves: small, and not zero."""
        from volkit import curves
        book = self.book()
        panel = curves.ComparePanel(curves=(curves.CurveRequest("surface", "EURUSD"),
                                            curves.CurveRequest("marks", "EURUSD")))
        r = panel.run(book)
        self.assertEqual(r["base"], 0)
        self.assertTrue(r["curves"][0]["is_base"])
        diffs = [p["diffs"]["atm"] for p in r["curves"][1]["points"]
                 if p["diffs"]["atm"] is not None]
        self.assertTrue(diffs)
        self.assertLess(max(abs(d) for d in diffs), 0.01)
        self.assertTrue(any(abs(d) > 1e-9 for d in diffs))

    def test_the_tenor_axis_is_the_union_and_a_gap_is_a_gap(self):
        """A curve that does not quote a tenor leaves a blank, which is not the
        same thing as the row being missing."""
        from volkit import curves
        book = self.book()
        r = curves.ComparePanel(curves=(
            curves.CurveRequest("marks", "EURUSD"),
            curves.CurveRequest("history", "EURUSD", "latest"))).run(book, self.history(book))
        self.assertIn("2Y", r["tenors"])            # in the workbook, not in the sheet
        self.assertIn("1M", r["tenors"])
        hist = r["curves"][1]
        self.assertIsNone(hist_point(hist, "2Y"))
        self.assertIsNotNone(hist_point(hist, "1M"))
        self.assertTrue(any("not every curve quotes every tenor" in n for n in r["notes"]))

    def test_a_date_snaps_back_to_the_last_row_on_or_before_it(self):
        """Forward-snapping would compare a Friday mark against Monday's."""
        from volkit import curves
        book = self.book()
        hist = self.history(book)["EURUSD"]
        when, note = curves.resolve_history_date(hist, "2024-02-25")   # a Sunday
        self.assertLessEqual(when, date(2024, 2, 25))
        self.assertIn("nearest row on or before", note)
        self.assertEqual(curves.resolve_history_date(hist, "")[0], hist.dates[-1])
        back, _ = curves.resolve_history_date(hist, "-30d")
        self.assertLess(back, hist.dates[-1])
        with self.assertRaises(curves.CurveError):
            curves.resolve_history_date(hist, "the other day")

    def test_a_curve_that_cannot_be_built_keeps_its_place(self):
        """Dropping it makes a short comparison look complete."""
        from volkit import curves
        book = self.book()
        r = curves.ComparePanel(curves=(
            curves.CurveRequest("surface", "EURUSD"),
            curves.CurveRequest("history", "USDHKD"))).run(book, self.history(book))
        self.assertEqual(len(r["curves"]), 2)
        self.assertFalse(r["curves"][1]["ok"])
        self.assertIn("no sheet for USDHKD", r["curves"][1]["message"])

    def test_the_base_falls_back_when_the_one_chosen_could_not_be_built(self):
        from volkit import curves
        book = self.book()
        r = curves.ComparePanel(curves=(curves.CurveRequest("history", "USDHKD"),
                                        curves.CurveRequest("surface", "EURUSD")),
                                base=0).run(book, self.history(book))
        self.assertEqual(r["base"], 1)
        self.assertTrue(r["curves"][1]["is_base"])

    def test_a_pasted_curve_decides_its_unit_once_from_the_level_column(self):
        """0.35 is an ordinary risk reversal in points and an ordinary
        at-the-money in decimals; letting a wing vote returns it 100x."""
        from volkit import curves
        c = curves.parse_pasted_curve("1M 8.20 -0.35 0.22\n3M 8.45")
        self.assertTrue(c.ok)
        self.assertAlmostEqual(c.at("1M").values["atm"], 0.0820)
        self.assertAlmostEqual(c.at("1M").values["rr25"], -0.0035)
        self.assertIsNone(c.at("3M").values["rr25"])
        dec = curves.parse_pasted_curve("1M 0.0820 -0.0035")
        self.assertAlmostEqual(dec.at("1M").values["atm"], 0.0820)

    def test_a_pasted_curve_that_straddles_one_is_refused_not_guessed(self):
        from volkit import curves
        c = curves.parse_pasted_curve("1M 8.20\n3M 0.0845")
        self.assertFalse(c.ok)
        self.assertIn("straddle", c.message)
        bad = curves.parse_pasted_curve("1M\n3M 8.4")
        self.assertFalse(bad.ok)
        self.assertIn("line 1", bad.message)

    def test_a_command_line_spec_reads_the_same_panel(self):
        from volkit import curves
        self.assertEqual(curves.parse_spec("history:-30d:eurusd", "USDJPY"),
                         curves.CurveRequest("history", "EURUSD", "-30d"))
        self.assertEqual(curves.parse_spec("marks", "USDJPY").pair, "USDJPY")
        # A date on a source that has none would silently be ignored.
        with self.assertRaises(curves.CurveError):
            curves.parse_spec("surface:2024-01-15", "EURUSD")
        with self.assertRaises(curves.CurveError):
            curves.parse_spec("yesterday", "EURUSD")

    def test_the_endpoint_is_a_pure_function_of_its_request(self):
        from volkit.webapp import BookService
        service = BookService(str(WORKBOOK), ASOF, history_path=str(HISTORY))
        payload = {"curves": [{"kind": "surface", "pair": "EURUSD"},
                              {"kind": "history", "pair": "EURUSD", "date": "-60d"}],
                   "cut": "NY", "method": "SVI", "field": "rr25", "base": 0}
        first = service.compare_curves(payload)
        self.assertEqual(first, service.compare_curves(payload))
        self.assertEqual(first["field"], "rr25")


def hist_point(curve, tenor):
    return next((p for p in curve["points"] if p["tenor"].upper() == tenor.upper()), None)


class TestMarketMakerApi(unittest.TestCase):
    """The endpoints, and the one piece of server state on the whole tool."""

    RUN = ("1M ATM 6.05/6.35 in 100mm vega\n"
           "3M ATM 6.25/6.55\n"
           "6M ATM 6.60/6.90\n"
           "1Y atm 7.00/7.30\n"
           "1M 25d rr -0.30/-0.10\n")

    def service(self, tmp):
        from volkit.webapp import BookService
        return BookService(str(WORKBOOK), ASOF, bank_path=str(Path(tmp) / "bank.json"))

    def test_the_payload_carries_no_number_a_browser_cannot_parse(self):
        """Python's json writes NaN, which JSON.parse refuses."""
        import json as _json, tempfile
        from volkit.webapp import _finite
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.service(tmp).mm_fit(
                {"pair": "EURUSD", "text": self.RUN, "target_source": "quotes",
                 "fallback_spread": "0.3", "tune_wings": False})
        text = _json.dumps(_finite(payload), default=str)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(_json.loads(text)["pair"], "EURUSD")

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
            out = service.mm_fit({"pair": "EURUSD", "text": self.RUN,
                                  "target_source": "quotes", "tune_wings": False})
            atm = next(r for r in out["sheet"]["rows"] if r["instrument"] == "atm")
            self.assertAlmostEqual(atm["width"], 0.28)
            # A note is advice, kept apart from the reader's own notes so it
            # cannot get buried: it exists to be read.
            self.assertIn("wider into the ECB", atm["advice"])
            # And it survives a fresh service reading the same file.
            self.assertEqual(len(self.service(tmp).bank.for_pair("EURUSD").rules), 2)

    def test_a_bad_rule_set_is_rejected_without_touching_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            service.mm_save_bank({"pair": "EURUSD",
                                  "rules": [{"kind": "spread", "value": "0.28"}]})
            bad = service.mm_save_bank({"pair": "EURUSD",
                                        "rules": [{"kind": "spread", "value": "-3"}]})
            self.assertFalse(bad["ok"])
            self.assertTrue(bad["problems"])
            self.assertEqual(len(self.service(tmp).bank.for_pair("EURUSD").rules), 1)

    def test_learning_proposes_and_does_not_save(self):
        """A paste that happens to hold one wide quote must not be able to
        rewrite the desk's ladder without somebody looking at it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            got = service.mm_learn({"pair": "EURUSD", "text": self.RUN,
                                    "target_source": "quotes"})
            self.assertTrue(got["rules"])
            self.assertTrue(all(r["kind"] == "spread" for r in got["rules"]))
            self.assertEqual(service.bank.for_pair("EURUSD").rules, [])

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


class TestWebAssets(unittest.TestCase):
    def test_front_end_javascript_parses(self):
        """Guards against shipping a page that dies on load."""
        try:
            import esprima
        except ImportError:
            self.skipTest("esprima not installed")
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        # esprima tops out at ES2017; downlevel the two newer operators used.
        probe = _re.sub(r"\?\.", ".", js.replace("??", " || "))
        esprima.parseScript(probe)

    def test_the_panel_roots_are_siblings_and_the_markup_closes(self):
        """A missing </div> put one panel inside another.

        The marking panel was never closed, so anything added after it landed
        *inside* it and was hidden whenever the marking tab was not showing --
        a whole tab that silently renders nothing. Browsers repair the markup
        on their own, which is exactly why it went unnoticed, so the balance
        is checked here instead.
        """
        from html.parser import HTMLParser
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        body = _re.sub(r"<script>.*?</script>", "", html, flags=_re.S)
        body = _re.sub(r"<style>.*?</style>", "", body, flags=_re.S)
        void = {"meta", "input", "br", "hr", "img", "link", "source", "col",
                "area", "base", "embed", "param", "track", "wbr"}

        class Scan(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad, self.depth = [], [], {}

            def handle_starttag(self, tag, attrs):
                if tag in void:
                    return
                got = dict(attrs).get("id")
                if got:
                    self.depth[got] = len(self.stack)
                self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack:
                    self.bad.append(f"stray </{tag}>")
                elif self.stack[-1] != tag:
                    self.bad.append(f"</{tag}> closes <{self.stack[-1]}>")
                    self.stack.pop()
                else:
                    self.stack.pop()

        scan = Scan()
        scan.feed(body)
        self.assertEqual(scan.bad, [])
        self.assertEqual(scan.stack, [])
        roots = [scan.depth[p] for p in ("p-pricing", "p-marking", "p-listed", "p-analysis",
                                         "p-mm")]
        self.assertEqual(len(set(roots)), 1, "the panel roots are not siblings")

    def test_every_class_the_script_looks_up_is_one_it_emits(self):
        """The panel shell and the painter that fills it are different functions.

        Nothing else would catch a renamed class between the two -- the page
        would simply render a panel with no chart and no error.
        """
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        for name in set(_re.findall(r"querySelector\('\.([A-Za-z0-9_-]+)'\)", js)):
            self.assertIn(f'class="{name}"', js, f".{name} is looked up but never emitted")

    def test_the_listed_panel_fields_are_all_understood_by_the_server(self):
        """A field the browser sends and the server ignores is a setting that
        silently does nothing, which is the failure mode this project exists
        to remove."""
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const EF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("forward", fields)
        src = (Path(__file__).resolve().parents[1] / "volkit" / "listed.py").read_text()
        handler = src.split("def panel_from_request")[1]
        for f in fields:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_the_market_maker_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed panel, for the same reason.

        A field the browser sends and the server ignores is a setting that
        silently does nothing, which is the failure mode this project exists
        to remove.
        """
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const MF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("text", fields)
        self.assertIn("fallback_spread", fields)
        src = (Path(__file__).resolve().parents[1] / "volkit" / "marketmaker.py").read_text()
        handler = src.split("def panel_from_request")[1]
        for f in fields | {"free", "smile_free", "fit_curve", "tune_wings"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_the_comparison_panel_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed and market-maker panels.

        A field the browser sends and the server ignores is a setting that
        silently does nothing.
        """
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const CF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("kind", fields)
        self.assertIn("date", fields)
        src = (Path(__file__).resolve().parents[1] / "volkit" / "curves.py").read_text()
        handler = src.split("def panel_from_request")[1]
        for f in fields | {"cut", "method", "field", "base"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_the_band_card_fields_are_all_understood_by_the_server(self):
        """The band treatment is marked on the screen and read in one place."""
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const BFIELDS=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("hazard", fields)
        self.assertIn("blend", fields)
        src = (Path(__file__).resolve().parents[1] / "volkit" / "banded.py").read_text()
        handler = src.split("def from_request")[1]
        for f in fields | {"mode", "solve_hazard"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_every_element_id_referenced_by_the_script_exists(self):
        import re as _re
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        ids = set(_re.findall(r'id="([^"]+)"', html))
        refs = set(_re.findall(r"\$\('#([a-zA-Z0-9_-]+)'\)", js))
        self.assertEqual(refs - ids - {"c1"}, set())


class TestDeterminism(unittest.TestCase):
    def test_same_clock_gives_identical_results(self):
        """Legacy read datetime.utcnow() inside the model on every call."""
        a = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        b = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        e = datetime(2024, 5, 28, tzinfo=UTC)
        self.assertEqual(float(a["USDJPY"].vol(1.02, e)), float(b["USDJPY"].vol(1.02, e)))

    def test_different_clocks_give_different_results(self):
        other = Clock(datetime(2024, 3, 15, 12, tzinfo=UTC))
        a = Book.from_excel(WORKBOOK, ASOF).load_all(["USDJPY"])
        b = Book.from_excel(WORKBOOK, other).load_all(["USDJPY"])
        e = datetime(2024, 5, 28, tzinfo=UTC)
        self.assertNotEqual(float(a["USDJPY"].vol(1.02, e)), float(b["USDJPY"].vol(1.02, e)))


class TestScreens(unittest.TestCase):
    """Building without a screen.

    A build can be made without some of the five tabs (build_exe.py
    --exclude-tab).  What is pinned here is that an excluded screen is really
    gone and says so: the tab is not offered, its routes are refused by name,
    and its subcommands are not registered.  A screen that merely disappeared
    from the page while its routes kept answering would be the same silent
    half-measure as a swallowed error.
    """

    def setUp(self):
        from volkit import screens
        self.screens = screens
        screens.enabled.cache_clear()
        self.addCleanup(screens.enabled.cache_clear)

    def _select(self, names):
        """Run the rest of the test as a build with only *names*."""
        import os
        old = os.environ.get(self.screens.ENV_VAR)
        os.environ[self.screens.ENV_VAR] = ",".join(names)
        self.screens.enabled.cache_clear()

        def restore():
            if old is None:
                os.environ.pop(self.screens.ENV_VAR, None)
            else:
                os.environ[self.screens.ENV_VAR] = old
            self.screens.enabled.cache_clear()

        self.addCleanup(restore)

    def test_a_source_tree_has_every_screen(self):
        self.assertEqual(self.screens.enabled(), self.screens.ALL)
        self.assertEqual(self.screens.excluded(), ())
        self.assertEqual(self.screens.summary(), "")

    def test_the_manifest_in_the_bundle_beats_the_environment(self):
        """The manifest is the build's own decision.

        An environment variable that could put a screen back would make the
        exclusion a suggestion; it is meant to be a property of the build.
        """
        import os
        import tempfile
        from volkit import paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.screens.write_manifest(root / "volkit" / "data", ["pricing", "mm"])
            os.environ[self.screens.ENV_VAR] = "analysis"
            self.addCleanup(os.environ.pop, self.screens.ENV_VAR, None)
            real = paths.resource_dir
            paths.resource_dir = lambda: root
            self.addCleanup(setattr, paths, "resource_dir", real)
            self.screens.enabled.cache_clear()
            self.assertEqual(self.screens.enabled(), ("pricing", "mm"))

    def test_a_misspelled_screen_is_refused_not_ignored(self):
        with self.assertRaises(self.screens.ScreenError) as ctx:
            self.screens.parse_names("pricing, markign")
        self.assertIn("markign", str(ctx.exception))
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_names("   ")

    def test_the_manifest_round_trips_and_keeps_tab_order(self):
        text = self.screens.manifest_text(["mm", "pricing"])
        self.assertEqual(self.screens.parse_names(text, source="t"), ("pricing", "mm"))
        self.assertIn("Excluded:", text)

    def test_every_route_belongs_to_at_most_one_screen(self):
        seen = set()
        for screen in self.screens.SCREENS:
            for route in screen.routes:
                self.assertNotIn(route, seen, f"{route} is claimed twice")
                seen.add(route)

    def test_an_excluded_screen_refuses_its_routes_by_name(self):
        self._select(["pricing", "marking"])
        msg = self.screens.route_refusal("/api/mm/fit")
        self.assertIsNotNone(msg)
        self.assertIn("Market maker", msg)
        # The shell and the screens that stayed are untouched.
        self.assertIsNone(self.screens.route_refusal("/api/price"))
        self.assertIsNone(self.screens.route_refusal("/api/state"))
        self.assertIsNone(self.screens.route_refusal("/api/reload"))

    def test_the_server_turns_an_excluded_route_away_with_404(self):
        from volkit import webapp
        self._select(["pricing"])

        class FakeHandler(webapp.Handler):
            def __init__(self):  # no socket, no request
                self.sent = None

            def _json(self, payload, code=200):
                self.sent = (code, payload)

        h = FakeHandler()
        h.path = "/api/analysis?pair=EURJPY"
        h.do_GET()
        self.assertEqual(h.sent[0], 404)
        self.assertIn("Analysis", h.sent[1]["error"])

    def test_the_state_response_tells_the_page_which_screens_it_has(self):
        from volkit.webapp import BookService
        self._select(["pricing", "marking"])
        state = BookService(str(WORKBOOK), ASOF).state()
        self.assertEqual(state["screens"], ["pricing", "marking"])

    def test_an_excluded_screen_loses_its_subcommands(self):
        from volkit.cli import build_parser
        import argparse as _argparse
        self._select(["pricing", "marking"])
        names = set()
        for action in build_parser()._actions:
            if isinstance(action, _argparse._SubParsersAction):
                names.update(action.choices)
        self.assertNotIn("mm", names)
        self.assertNotIn("analysis", names)
        self.assertNotIn("listed", names)
        # The shell commands and the screens that stayed are untouched.
        for kept in ("check", "serve", "tenors", "vol"):
            self.assertIn(kept, names)

    def test_an_excluded_subcommand_says_why_rather_than_invalid_choice(self):
        """argparse would answer 'invalid choice', which is not what happened."""
        from volkit import cli
        self._select(["pricing"])
        self.assertEqual(cli._excluded_request(["mm", "EURUSD"]), "mm")
        # The global options may come first, and take a value.
        self.assertEqual(cli._excluded_request(["-w", "book.xlsx", "listed"]), "listed")
        self.assertEqual(cli._excluded_request(["--asof", "2024-01-01", "vol"]), None)
        # A subcommand's own arguments are never read as a subcommand: only
        # the first positional is inspected.
        self.assertEqual(cli._excluded_request(["vol", "mm", "1M", "1.0"]), None)

    def _hidden_build(self, visible, shy):
        """Run the rest of the test as a build with *shy* hidden."""
        import os
        old = os.environ.get(self.screens.ENV_VAR)
        os.environ[self.screens.ENV_VAR] = ", ".join(
            list(visible) + [f"{n} hidden" for n in shy])
        self.screens.deactivate_all()

        def restore():
            if old is None:
                os.environ.pop(self.screens.ENV_VAR, None)
            else:
                os.environ[self.screens.ENV_VAR] = old
            self.screens.deactivate_all()

        self.addCleanup(restore)

    def test_a_hidden_screen_is_off_until_it_is_asked_for(self):
        """The third state.  Off, it is turned away exactly like an excluded
        screen; the only difference is the sentence, and the sentence is the
        whole point -- one of them can be had by starting the tool
        differently."""
        self._hidden_build(["pricing", "marking"], ["analysis"])
        self.assertEqual(self.screens.built(),
                         ("pricing", "marking", "analysis"))
        self.assertEqual(self.screens.hidden(), ("analysis",))
        self.assertEqual(self.screens.enabled(), ("pricing", "marking"))
        msg = self.screens.route_refusal("/api/analysis")
        self.assertIn("--enable-tab analysis", msg)
        self.assertIn("volkit.cfg", msg)

        self.assertEqual(self.screens.activate(["analysis"]), ("analysis",))
        self.assertEqual(self.screens.enabled(), ("pricing", "marking", "analysis"))
        self.assertIsNone(self.screens.route_refusal("/api/analysis"))
        # Asking twice, or for a screen already showing, is not an error.
        self.assertEqual(self.screens.activate(["analysis", "pricing"]), ())

    def test_an_excluded_screen_cannot_be_switched_on(self):
        """Otherwise a build could be talked out of its own decision."""
        self._hidden_build(["pricing"], ["marking"])
        with self.assertRaises(self.screens.ScreenError) as ctx:
            self.screens.activate(["mm"])
        self.assertIn("excluded from this build", str(ctx.exception))
        self.assertEqual(self.screens.enabled(), ("pricing",))

    def test_a_hidden_screens_subcommand_is_registered_only_once_it_is_on(self):
        from volkit.cli import build_parser
        import argparse as _argparse

        def names():
            got = set()
            for action in build_parser()._actions:
                if isinstance(action, _argparse._SubParsersAction):
                    got.update(action.choices)
            return got

        self._hidden_build(["pricing", "marking"], ["mm"])
        self.assertNotIn("mm", names())
        self.screens.activate(["mm"])
        self.assertIn("mm", names())

    def test_the_command_line_switch_is_read_before_the_parser_is_built(self):
        """The flag has to change the parser that would otherwise reject it."""
        from volkit import cli
        self._hidden_build(["pricing", "marking"], ["analysis"])
        self.assertEqual(cli._requested_screens(["--enable-tab", "analysis", "analysis",
                                                 "EURUSD"]), ["analysis"])
        self.assertEqual(cli._requested_screens(["--enable-tab=mm"]), ["mm"])
        self.assertEqual(cli._excluded_request(["analysis", "EURUSD"]), "analysis")
        self.screens.activate(["analysis"])
        self.assertIsNone(cli._excluded_request(["analysis", "EURUSD"]))

    def test_a_selection_that_hides_everything_is_refused(self):
        """A build needs at least one tab that shows without a switch."""
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing hidden, mm hidden", source="t")
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing, pricing hidden", source="t")
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing invisible", source="t")

    def test_the_page_hides_the_screens_it_was_not_given(self):
        """The tab, the panel and the boot work all key off the same list."""
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        self.assertIn("STATE.screens", js)
        for name in self.screens.ALL:
            self.assertIn(f"{name}:'#{self.screens.BY_NAME[name].panel}'", js)

    def test_a_pricing_leg_is_removed_from_its_column_header_only(self):
        """One delete per column, in the header; the Remove row is gone.

        The row of Remove buttons sat between the inputs and the results, so
        the click that removed a leg was one row away from the fields being
        typed into.  The header cross does the same job out of the way of the
        grid.  Pinned in both directions because the header and the rows are
        painted by the same function, and either could come back as a one-line
        change nobody would notice.
        """
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        js = html.split("<script>")[1].split("</script>")[0]
        grid = js.split("function renderGrid(")[1].split("\nfunction ")[0]
        head, body = grid.split("<tr class=\"sec\">", 1)
        self.assertIn("data-del", head)                  # the header cross
        self.assertNotIn("data-del", body)               # and nothing in the grid
        self.assertNotIn("rmcol", js)
        self.assertIn("class=\"x\"", head)
        self.assertIn(".colhead .x", html)               # it is still styled
        self.assertIn('id="delleg"', html)               # - Remove last
        self.assertIn('id="clearlegs"', html)            # Clear all


class TestPackaging(unittest.TestCase):
    """The Windows build.

    None of this builds anything -- PyInstaller takes minutes and cannot
    cross-compile anyway.  What it pins is the part of the build that can go
    wrong silently in the source tree: an input the spec never picks up, or a
    launcher that stops recognising a subcommand.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_launcher_knows_every_subcommand(self):
        """A hardcoded list here went stale when 'analysis' and 'listed' landed.

        The launcher appends 'serve' when it sees no subcommand, so an
        unrecognised one did not fail loudly -- it turned
        'volkit.exe analysis EURJPY' into 'analysis EURJPY serve' and argparse
        rejected the pair.  The names are read off the parser now; this holds
        that.
        """
        import launcher
        from volkit.cli import build_parser
        import argparse as _argparse

        expected = set()
        for action in build_parser()._actions:
            if isinstance(action, _argparse._SubParsersAction):
                expected.update(action.choices)
        self.assertTrue(expected)
        self.assertEqual(launcher._subcommands(), frozenset(expected))

    def test_launcher_defaults_to_serve_only_without_a_subcommand(self):
        import launcher
        known = launcher._subcommands()
        self.assertIn("analysis", known)
        self.assertIn("listed", known)
        self.assertNotIn("--help", known)

    def test_build_inputs_exist(self):
        """Every file the build reads, checked here rather than on a Windows box."""
        import build_exe
        for rel in build_exe.REQUIRED_SOURCES:
            with self.subTest(rel):
                self.assertTrue((build_exe.ROOT / rel).exists(), f"{rel} is missing")

    def test_the_spec_bundles_the_resources_the_code_reads(self):
        """The page and the calendar travel inside the bundle; user data does not.

        paths.resource_dir() and paths.app_dir() are different places, and
        putting a file in the wrong one produces an exe that starts and then
        serves an empty page.
        """
        spec = (self.ROOT / "volkit.spec").read_text()
        self.assertIn("volkit/web", spec)
        self.assertIn("volkit/data", spec)
        # tzdata is not optional on Windows: there is no system IANA database.
        self.assertIn("tzdata", spec)

        import build_exe
        # The user's own files must be staged beside the exe, never bundled.
        for rel in build_exe.USER_DATA:
            with self.subTest(rel):
                self.assertNotIn(rel, spec)

    def test_the_screen_selection_is_checked_before_anything_is_built(self):
        import build_exe
        from volkit import screens
        self.assertEqual(build_exe.choose_screens(None, []), (screens.ALL, ()))
        self.assertEqual(build_exe.choose_screens("pricing,marking", []),
                         (("pricing", "marking"), ()))
        # --only-tabs sets the starting set, --exclude-tab takes further ones away.
        self.assertEqual(build_exe.choose_screens("pricing,marking", ["marking"]),
                         (("pricing",), ()))
        # One flag may carry a list, and an unknown name is an error rather
        # than a screen that quietly stayed in the build.
        self.assertEqual(build_exe.choose_screens(None, ["mm,listed"]),
                         (("pricing", "marking", "analysis"), ()))
        with self.assertRaises(screens.ScreenError):
            build_exe.choose_screens(None, ["markign"])
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens(None, list(screens.ALL))

    def test_a_hidden_screen_is_built_but_not_shown(self):
        """The third state: in the build, off until --enable-tab.

        Hiding a screen that was also excluded is refused: the switch could
        never work, and a build whose documented flag does nothing is the
        silent failure this project exists to remove.
        """
        import build_exe
        from volkit import screens
        shown, shy = build_exe.choose_screens(None, [], ["mm"])
        self.assertNotIn("mm", shown)
        self.assertEqual(shy, ("mm",))
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens(None, ["mm"], ["mm"])          # excluded and hidden
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens("pricing", [], ["pricing"])    # nothing left showing
        text = screens.manifest_text(shown, shy)
        self.assertEqual(screens.parse_selection(text, source="t"), (shown, shy))
        self.assertIn("--enable-tab mm", text)

    def test_the_screens_manifest_is_bundled_and_never_left_in_the_source_tree(self):
        """Written under build/, picked up by the spec, read from the bundle.

        Writing it into volkit/data would leave the source tree in a state
        where running from source silently lost a screen.
        """
        import build_exe
        from volkit import screens
        self.assertIn("build", build_exe.SCREENS_BUILD_DIR.parts)
        self.assertNotIn("data", build_exe.SCREENS_BUILD_DIR.parts)
        self.assertFalse((self.ROOT / screens.MANIFEST).exists())
        spec = (self.ROOT / "volkit.spec").read_text()
        self.assertIn("VOLKIT_SCREENS_FILE", spec)
        self.assertIn("volkit/data", spec)
        for rel in build_exe.USER_DATA:
            self.assertNotIn("screens.txt", rel)

    def test_the_handover_zip_keeps_samples_out_of_the_exes_own_folder(self):
        """The one-file zip flattened everything to the top level.

        That put the synthetic history workbook exactly where
        ``find_data_file()`` looks, which is the failure staging it into
        samples/ exists to prevent -- the folder build got this right and the
        one-file build quietly did not.
        """
        import build_exe
        entries = {str(src): arc for src, arc in
                   build_exe.zip_entries(self.ROOT / "dist" / "volkit.exe", onefile=True)}
        self.assertTrue(entries)
        for rel in build_exe.SAMPLE_DATA:
            arc = entries.get(str(self.ROOT / rel))
            if arc is not None:
                self.assertTrue(arc.startswith("samples/"), f"{rel} lands at {arc!r}")
        for rel in build_exe.USER_DATA:
            arc = entries.get(str(self.ROOT / rel))
            if arc is not None:
                self.assertNotIn("/", arc)      # beside the exe, where app_dir() looks

    def test_samples_are_not_staged_beside_the_exe(self):
        """Synthetic data must not sit where find_data_file() would pick it up.

        Made-up numbers appearing on a screen nobody asked for is the same
        failure as a silent zero, so the sample history goes in samples/.
        """
        import build_exe
        self.assertTrue(build_exe.SAMPLE_DATA)
        for rel in build_exe.SAMPLE_DATA:
            with self.subTest(rel):
                self.assertNotIn(rel, build_exe.USER_DATA)


class TestStartupConfig(unittest.TestCase):
    """The settings file a double-clicked executable reads.

    A packaged app has no command line, so this is the only place a desk can
    fix the port, the workbook or a hidden screen without a rebuild.  What is
    pinned here is that it never applies silently and never applies twice.
    """

    def parse(self, text):
        from volkit import config
        return config.parse(text, source="test.cfg")

    def test_a_settings_file_becomes_a_command_line(self):
        cfg = self.parse("# a note\n"
                         "command = serve\n"
                         "port = 8900\n"
                         "workbook = C:\\Marks and data\\vol_marks.xlsx\n"
                         "no-browser = true\n"
                         "zip = false\n"
                         "enable-tab = analysis\n"
                         "enable-tab = mm\n")
        self.assertEqual(cfg.argv, [
            "serve", "--port", "8900",
            "--workbook", "C:\\Marks and data\\vol_marks.xlsx",
            "--no-browser", "--enable-tab", "analysis", "--enable-tab", "mm"])
        # A value is the rest of the line, so a path with spaces needs no
        # quoting; a false switch is left out entirely but still reported.
        self.assertTrue(any("--zip off" in n for n in cfg.notes))

    def test_the_command_may_carry_its_own_arguments(self):
        cfg = self.parse("command = analysis EURJPY --horizon 7\ncut = NY\n")
        self.assertEqual(cfg.argv, ["analysis", "EURJPY", "--horizon", "7", "--cut", "NY"])

    def test_a_line_that_is_not_a_setting_is_refused_by_line_number(self):
        """'port 8900' with no '=' would otherwise vanish silently."""
        from volkit import config
        with self.assertRaises(config.ConfigError) as ctx:
            self.parse("command = serve\nport 8900\n")
        self.assertIn("line 2", str(ctx.exception))
        with self.assertRaises(config.ConfigError):
            self.parse("command = serve\ncommand = check\n")

    def test_the_file_is_read_only_when_nothing_was_typed(self):
        import tempfile
        from volkit import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("command = serve\nport = 8900\n")
            argv, cfg = config.startup_argv(["--config", str(path)], {"serve", "check"})
            self.assertEqual(argv, ["serve", "--port", "8900"])
            self.assertEqual(cfg.path, path)
            # Something typed, and no --config: the file stays shut.
            argv, cfg = config.startup_argv(["check"], {"serve", "check"})
            self.assertEqual(argv, ["check"])
            self.assertIsNone(cfg.path)
            # Explicitly refused.
            argv, cfg = config.startup_argv(["--no-config"], {"serve", "check"})
            self.assertEqual(argv, [])
            self.assertIsNone(cfg.path)
            with self.assertRaises(config.ConfigError):
                config.startup_argv(["--config", str(path), "--no-config"])
            with self.assertRaises(config.ConfigError):
                config.startup_argv(["--config", str(Path(tmp) / "nope.cfg")])

    def test_two_different_commands_are_refused_rather_than_resolved(self):
        import tempfile
        from volkit import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("command = serve\n")
            with self.assertRaises(config.ConfigError) as ctx:
                config.startup_argv(["--config", str(path), "check"], {"serve", "check"})
            self.assertIn("serve", str(ctx.exception))
            # The same command in both places is somebody typing what the file
            # already says, and its own arguments still count.
            argv, _ = config.startup_argv(["--config", str(path), "serve", "--port", "1"],
                                          {"serve", "check"})
            self.assertEqual(argv, ["serve", "--port", "1"])

    def test_the_launcher_puts_serve_in_front_of_a_file_of_options(self):
        """Appending it left the options in front of the subcommand, where
        argparse cannot place them."""
        import tempfile
        import launcher
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("port = 8900\nno-browser = true\n")
            argv, cfg = launcher.resolve(["--config", str(path)])
            self.assertEqual(argv[0], "serve")
            self.assertEqual(argv, ["serve", "--port", "8900", "--no-browser"])
            self.assertEqual(cfg.path, path)
        # A real command line is untouched and reads no file.
        argv, cfg = launcher.resolve(["tenors", "USDJPY"])
        self.assertEqual(argv, ["tenors", "USDJPY"])
        self.assertIsNone(cfg.path)

    def test_the_sample_settings_file_runs(self):
        """It is staged beside the exe, so a double-click reads it as it ships."""
        from volkit import config
        import build_exe
        self.assertIn("files/volkit.cfg", build_exe.USER_DATA)
        cfg = config.load(Path(__file__).resolve().parents[1] / "files" / "volkit.cfg")
        self.assertEqual(cfg.argv, ["serve"])


# ===========================================================================
# market making
# ===========================================================================


class TestQuoteParsing(unittest.TestCase):
    """Reading a broker run written in English."""

    def parse(self, text, **kw):
        kw.setdefault("pair", "EURUSD")
        return quotes.parse_quotes(text, **kw)

    def test_reads_the_instruments_a_desk_actually_writes(self):
        run = self.parse(
            "1M ATM 8.20/8.60 in 100mm vega\n"
            "3M 25d RR 0.35/0.55 eur call over\n"
            "2M 25d fly 0.20/0.28\n"
            "6M 1.1000 call 7.90/8.40\n"
            "1M/3M ATM spread 0.30/0.55\n")
        self.assertEqual([q.instrument for q in run.quotes],
                         ["atm", "rr", "fly", "outright", "spread"])
        self.assertEqual(run.skipped, ())
        atm = run.quotes[0]
        self.assertAlmostEqual(atm.bid, 0.0820)
        self.assertAlmostEqual(atm.ask, 0.0860)
        self.assertEqual(atm.size, 100.0)
        self.assertEqual(atm.size_basis, "vega")
        self.assertAlmostEqual(run.quotes[1].delta, 0.25)
        self.assertAlmostEqual(run.quotes[3].strike, 1.10)
        self.assertTrue(run.quotes[3].is_call)

    def test_the_unit_is_decided_once_from_the_level_quotes(self):
        """Per-line sniffing reads a small risk reversal as a decimal.

        0.35 is an ordinary risk reversal in points and an ordinary
        at-the-money in decimals.  Letting the risk reversal vote would return
        it a hundred times too large -- the same failure §9 pins for a
        historical sheet.
        """
        run = self.parse("1M ATM 8.20/8.60\n3M 25d RR 0.35/0.55\n")
        self.assertEqual(run.vol_unit, "percent")
        self.assertAlmostEqual(run.quotes[1].bid, 0.0035)
        dec = self.parse("1M ATM 0.0820/0.0860\n3M 25d RR 0.0035/0.0055\n")
        self.assertEqual(dec.vol_unit, "decimal")
        self.assertAlmostEqual(dec.quotes[0].ask, 0.0860)

    def test_a_paste_that_straddles_one_is_refused_not_guessed(self):
        with self.assertRaises(quotes.QuoteError) as ctx:
            self.parse("1M ATM 8.20/8.60\n3M ATM 0.0825/0.0865\n")
        self.assertIn("straddle", str(ctx.exception))

    def test_a_paste_with_no_level_quote_does_not_pretend_to_decide(self):
        run = self.parse("3M 25d RR 0.35/0.55\n6M 25d fly 0.20/0.28\n")
        self.assertEqual(run.vol_unit, "percent")
        self.assertTrue(any("could not decide its own unit" in n for n in run.notes))

    def test_a_direction_word_is_resolved_against_the_pair(self):
        """JPY call over on USDJPY is a dollar put over, so it is negative.

        This is the same class of mistake as the cross-triangle sign in §5:
        the magnitude is right and the sign is not, which nothing downstream
        catches.
        """
        base = quotes.parse_quotes("1M atm 9.0/9.4\n3M 25d rr 0.40/0.60 jpy call over",
                                   pair="USDJPY")
        rr = base.quotes[1]
        self.assertLess(rr.ask, 0.0)
        self.assertAlmostEqual(rr.bid, -0.0060)
        self.assertAlmostEqual(rr.ask, -0.0040)
        other = quotes.parse_quotes("1M atm 9.0/9.4\n3M 25d rr 0.40/0.60 usd call over",
                                    pair="USDJPY")
        self.assertAlmostEqual(other.quotes[1].bid, 0.0040)

    def test_a_currency_that_is_not_a_leg_is_refused(self):
        run = self.parse("1M atm 8.2/8.6\n3M 25d rr 0.4/0.6 jpy call over")
        self.assertEqual(len(run.skipped), 1)
        self.assertIn("not a leg of EURUSD", run.skipped[0][2])

    def test_a_truncated_offer_is_refused_rather_than_repaired(self):
        """'8.2/6' means 8.20/8.60 to a human and repairing it means inventing
        the digits, so it is refused with the reason instead."""
        run = self.parse("1M atm 8.2/8.6\n2M atm 8.2/6")
        self.assertEqual(len(run.skipped), 1)
        self.assertIn("offers below its own bid", run.skipped[0][2])

    def test_calendar_and_literal_spread_orientation(self):
        """'1M/3M' is the calendar convention; '3M-1M' is read literally.

        Both end up as the same difference here, and both say which reading
        they used, because a spread quoted the other way round is a sign error
        nothing downstream would catch.
        """
        run = self.parse("1M atm 8.2/8.6\n1M/3M atm spread 0.30/0.55\n3M-1M atm spread 0.30/0.55")
        for q in run.quotes[1:]:
            self.assertEqual(q.instrument, "spread")
            self.assertEqual(str(q.expiry), "1M")
            self.assertEqual(str(q.expiry_far), "3M")
        self.assertTrue(any("calendar convention" in n for n in run.quotes[1].notes))
        self.assertTrue(any("read literally" in n for n in run.quotes[2].notes))

    def test_strangle_and_smile_fly_pin_their_own_convention(self):
        run = self.parse("1M atm 8.2/8.6\n1M 25d strangle 0.20/0.28\n"
                         "2M 25d smile fly 0.20/0.28\n3M 25d fly 0.20/0.28",
                         fly_convention="smile")
        self.assertEqual(run.quotes[1].fly_kind, "market")
        self.assertEqual(run.quotes[2].fly_kind, "smile")
        self.assertEqual(run.quotes[3].fly_kind, "smile")

    def test_nothing_unreadable_is_dropped_quietly(self):
        run = self.parse("1M atm 8.2/8.6\nrumour has it\n1M 25d rr\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertEqual([n for n, _, _ in run.skipped], [2, 3])
        for _, _, why in run.skipped:
            self.assertTrue(why)

    def test_a_vega_profile_reads_tenors_and_reports_the_rest(self):
        profile, notes, skipped = quotes.parse_vega_profile("1M 250\n3M -120\nnope 4\n1M 50")
        self.assertEqual(profile, {"1M": 300.0, "3M": -120.0})
        self.assertEqual(skipped[0][0], 3)
        self.assertTrue(any("more than once" in n for n in notes))


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
        self.assertIn("panel fallback", fell_back.reason)

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
        would quietly tighten the whole ladder."""
        run = quotes.parse_quotes(
            "1M atm 8.20/8.60\n1M atm 8.30/8.70\n2M atm 9.00\n", pair="EURUSD")
        rules, notes = suggest_rules(run.quotes, days_of=lambda q: 30.0)
        self.assertEqual(len(rules), 1)
        self.assertAlmostEqual(rules[0].value, 0.0040)
        self.assertIn("median of 2", rules[0].text)


class TestMarketMakerModel(unittest.TestCase):
    """The three stages: the curve, the wings, and the quote."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD", "USDJPY"])

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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["USDCNY"])
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

    def test_a_cross_fits_its_correlation_not_a_level_it_does_not_own(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["AUDUSD", "USDJPY", "AUDJPY"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
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
    """The screen as a whole: what it reports and what it leaves behind.

    Most of these switch the wing fine tune off.  It is exercised properly in
    ``TestMarketMakerModel`` and in the two tests here that need it; running a
    full one in every case would spend a minute of the suite re-proving it.
    """

    TEXT = ("1M ATM 6.05/6.35 in 100mm vega\n"
            "3M ATM 6.25/6.55\n"
            "6M ATM 6.60/6.90\n"
            "1Y atm 7.00/7.30\n"
            "1M 25d rr -0.30/-0.10 eur call over\n"
            "3M 25d fly 0.12/0.20\n")

    @classmethod
    def setUpClass(cls):
        # Shared by everything that only reports: Panel.run puts the book back,
        # which is itself one of the tests below.
        cls.book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])

    def panel(self, **kw):
        payload = {"pair": "EURUSD", "text": self.TEXT, "target_source": "quotes",
                   "fallback_spread": "0.30", "tune_wings": False}
        payload.update(kw)
        return marketmaker.panel_from_request(payload)

    def bank(self):
        bank = KnowledgeBank()
        bank.set_pair("EURUSD", [Rule("spread", 0.28, "atm"), Rule("spread", 0.20, "rr"),
                                 Rule("spread", 0.12, "fly")], ASOF.now)
        return bank

    def test_reporting_puts_the_book_back_exactly(self):
        """The default is to report.  A screen that quietly re-marked the book
        every time somebody typed in it would be unusable.  This runs the wing
        tune as well, because the shifts have to come back too."""
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = list(surface.atm.tenor_table())
        shifts = dict(surface.param_shifts)
        out = self.panel(tune_wings=True).run(book, bank=self.bank())
        self.assertFalse(out["applied"])
        self.assertIsNotNone(out["wings"])
        self.assertNotEqual(out["wings"]["after"], out["wings"]["before"])
        self.assertEqual(list(surface.atm.tenor_table()), before)
        self.assertEqual(dict(surface.param_shifts), shifts)

    def test_keeping_the_marks_writes_them_and_says_it_did(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = surface.atm.term_vol(tenor_to_years("1m"))
        out = self.panel(apply=True).run(book, bank=self.bank())
        self.assertTrue(out["applied"])
        self.assertAlmostEqual(surface.atm.term_vol(tenor_to_years("1m")), 0.062, places=5)
        self.assertNotAlmostEqual(surface.atm.term_vol(tenor_to_years("1m")), before)
        self.assertTrue(any("in memory only" in w for w in out["warnings"]))

    def test_the_width_a_rule_states_is_the_width_that_comes_out(self):
        """The bank is written in volatility points and the model in decimals.
        Reading a 0.28 rule as a decimal produced a 28 vol point market."""
        out = self.panel().run(self.book, bank=self.bank())
        atm = next(r for r in out["sheet"]["rows"] if r["instrument"] == "atm")
        self.assertAlmostEqual(atm["width"], 0.28)
        self.assertAlmostEqual(atm["our_ask"] - atm["our_bid"], 0.28, places=9)
        self.assertIn("ATM", atm["width_source"])

    def test_a_section_that_cannot_run_empties_only_itself(self):
        """No pinned tenor means no target curve.  The sheet is still built,
        the same way the analysis screen keeps its sections apart."""
        out = self.panel(target_source="overwrites").run(self.book, bank=self.bank())
        self.assertIsNone(out["curve"])
        self.assertIn("no tenor is pinned", out["unavailable"]["curve"])
        self.assertIsNotNone(out["sheet"])
        self.assertEqual(out["sheet"]["n_quotes"], 6)

    def test_a_quote_with_no_rule_and_no_fallback_gets_no_price(self):
        out = self.panel(fallback_spread="").run(self.book, bank=KnowledgeBank())
        for row in out["sheet"]["rows"]:
            self.assertIsNone(row["our_bid"])
            self.assertEqual(row["verdict"], "no width")
            self.assertTrue(any("no width rule" in w for w in row["warnings"]))

    def test_an_absolute_strike_without_a_feed_is_reported_not_priced_at_one(self):
        """Without a forward there is no moneyness, and pricing it at a forward
        of 1 would be a silent, badly wrong answer."""
        out = self.panel(text=self.TEXT + "6M 1.1000 call 7.90/8.40\n").run(
            self.book, bank=self.bank())
        row = out["sheet"]["rows"][-1]
        self.assertEqual(row["verdict"], "not priced")
        self.assertIsNone(row["model_after"])
        self.assertTrue(any("forward feed" in w for w in row["warnings"]))

    def test_the_sheet_reports_where_our_price_sits_against_theirs(self):
        out = self.panel().run(self.book, bank=self.bank())
        rows = {r["describe"]: r for r in out["sheet"]["rows"]}
        for row in rows.values():
            self.assertIn(row["position"], ("inside", "below", "above"))
            if row["position"] == "inside":
                self.assertEqual(row["edge"], 0.0)
            else:
                self.assertNotEqual(row["edge"], 0.0)
        self.assertEqual(out["sheet"]["n_quotes"], 6)
        self.assertEqual(out["sheet"]["priced"], 6)

    def test_a_paste_the_reader_cannot_use_is_listed_not_silently_shortened(self):
        out = self.panel(text=self.TEXT + "3M 25d rr 0.4/0.6 jpy call over\n").run(
            self.book, bank=self.bank())
        self.assertEqual(out["sheet"]["n_quotes"], 6)
        self.assertEqual(len(out["sheet"]["skipped"]), 1)
        self.assertIn("not a leg of EURUSD", out["sheet"]["skipped"][0]["why"])

    def test_the_panel_and_the_command_line_share_one_entry_point(self):
        """A panel set up in the browser and the same panel run from a shell
        must produce the same numbers, which is only guaranteed if there is one
        function."""
        import inspect
        from volkit import cli
        source = inspect.getsource(cli.cmd_mm)
        self.assertIn("panel_from_request", source)


class TestCurveInvalidation(unittest.TestCase):
    def test_a_parameter_change_keeps_the_weight_profile_it_cannot_have_changed(self):
        """The intraday weight profile is a pure function of the pair, the
        clock and the horizon.  Dropping it on every backbone change cost about
        20ms against 2ms of integrals that genuinely had to go -- a 17x tax on
        every re-mark, which the market-maker fit pays thousands of times.
        """
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        atm = book["EURUSD"].atm
        atm.term_vol(1.0)
        cached = len(atm.weighting._cache)
        self.assertGreater(cached, 0)
        before = atm.term_vol(0.25)
        atm.set_params(initial_vol=atm.params.initial_vol * 1.10)
        self.assertEqual(len(atm.weighting._cache), cached)
        self.assertNotAlmostEqual(atm.term_vol(0.25), before)


class TestSmileParameterShifts(unittest.TestCase):
    def test_a_shift_moves_the_level_and_keeps_the_term_structure(self):
        """An overwrite replaces a parameter and flattens its term structure;
        re-marking a wing against a broker run should move its level and leave
        the shape alone."""
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        before = [surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)]
        self.assertEqual(surface.set_param_shifts({"rho25": 0.03}), [])
        after = [surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)]
        for a, b in zip(after, before):
            self.assertAlmostEqual(a - b, 0.03, places=12)
        surface.clear_param_shifts()
        self.assertEqual([surface.params_at(t)["rho25"] for t in (0.05, 0.25, 1.0)], before)

    def test_an_unknown_parameter_is_refused(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        problems = book["EURUSD"].set_param_shifts({"vega": 0.1})
        self.assertTrue(problems)
        self.assertIn("unknown smile parameter", problems[0])

    def test_a_clamped_shift_is_reported_rather_than_absorbed(self):
        book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        surface = book["EURUSD"]
        surface.set_param_shifts({"rho25": 1.4})
        self.assertLessEqual(abs(surface.params_at(0.25)["rho25"]), 0.999)
        self.assertTrue(any("clamped" in w for w in surface.shift_warnings()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
