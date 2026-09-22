"""The band as a process: the Jacobi target zone, and touches inside it.

Two claims are pinned here and the rest follows from them.

The **moments are exact**, not an approximation: the conditional mean and
variance of the Jacobi diffusion have closed forms, and a Monte Carlo of the
process itself has to agree with them.  Everything downstream -- the term
structure of the Beta concentration, the estimator, the prediction the panel
makes -- is built on those two formulas.

And a **defended edge is not reachable by diffusion**.  The Jacobi coefficient
``sigma sqrt(x(1-x))`` vanishes at both edges, so a touch of a Convertibility
Undertaking can only happen if the peg breaks.  A lognormal has no idea, which
is why it prices a one-touch on 7.75 at fifteen times the mixture's value; that
factor is the whole reason this module reaches into ``exotics``.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from volkit.banded import Band, JumpSpec
from volkit.book import Book
from volkit.exotics import band_touch
from volkit.feed import MarketFeed
from volkit.targetzone import (
    EDGE_CENTRE,
    MIN_STEPS,
    TargetZone,
    dynamics_panel,
    estimate,
)

from ._support import ASOF, BOOK, FEED

BAND = Band("EURUSD", 0.95, 1.25, "synthetic, for the test only")
HKD = Band("USDHKD", 7.75, 7.85, "HKMA Convertibility Undertakings")


def jacobi_path(x0, m, kappa, sigma, years, per_year=252, sub=10, seed=17):
    """A sampled path of the process the module models, for round trips."""
    rng = np.random.default_rng(seed)
    dt = 1.0 / (per_year * sub)
    n = int(years * per_year)
    out, x = [], x0
    for _ in range(n):
        for _ in range(sub):
            x += kappa * (m - x) * dt + sigma * math.sqrt(
                max(x * (1.0 - x), 0.0) * dt) * rng.standard_normal()
            x = min(max(x, 1e-9), 1.0 - 1e-9)
        out.append(x)
    return np.array(out), np.arange(n) / per_year


class TestMoments(unittest.TestCase):
    """The closed forms everything else stands on."""

    def test_they_agree_with_a_monte_carlo_of_the_process(self):
        """Against the simulation's own standard error, not a decimal place.

        A fixed tolerance here is a test of the step count and the seed: the
        Monte Carlo mean has a standard error of about ``sd/sqrt(n)``, which
        at these sizes is ~7e-4, so ``places=3`` fails on ordinary sampling
        noise roughly a third of the time. Four standard errors is the
        statement actually being made -- the closed form is the process's
        mean, so the simulation must land on it.
        """
        zone = TargetZone("T", kappa=1.2, sigma=0.9, m=0.5)
        rng = np.random.default_rng(5)
        for x0, t in ((0.90, 0.25), (0.19, 1.0)):
            mean, var = zone.moments(x0, t)
            n, steps = 200_000, 800
            dt = t / steps
            x = np.full(n, x0)
            for _ in range(steps):
                x = x + zone.kappa * (zone.m - x) * dt + zone.sigma * np.sqrt(
                    np.clip(x * (1 - x), 0, None) * dt) * rng.standard_normal(n)
                x = np.clip(x, 0.0, 1.0)
            se = float(x.std(ddof=1)) / math.sqrt(n)
            self.assertAlmostEqual(mean, float(x.mean()), delta=4 * se)
            # The variance of a sample variance is the heavier tail of the two,
            # so it is compared in relative terms.
            self.assertAlmostEqual(var / float(x.var()), 1.0, delta=0.02)

    def test_the_limits_are_a_point_mass_now_and_the_stationary_beta_later(self):
        zone = TargetZone("T", kappa=0.6, sigma=1.1, m=0.4)
        self.assertEqual(zone.moments(0.9, 0.0), (0.9, 0.0))
        mean, var = zone.moments(0.9, 500.0)
        a, b = zone.stationary()
        s = a + b
        self.assertAlmostEqual(mean, zone.m, places=9)
        self.assertAlmostEqual(var, zone.m * (1 - zone.m) / (s + 1.0), places=9)

    def test_the_concentration_is_two_kappa_over_sigma_squared(self):
        """The identity that makes this a prior on ``banded``'s own parameter
        rather than a second model: the stationary law is *exactly* the Beta
        family the mixture already prices with."""
        zone = TargetZone("T", kappa=0.55, sigma=1.15, m=0.5)
        self.assertAlmostEqual(zone.concentration, 2 * 0.55 / 1.15 ** 2, places=12)
        a, b = zone.stationary()
        self.assertAlmostEqual(a + b, zone.concentration, places=12)
        self.assertAlmostEqual(a / (a + b), zone.m, places=12)

    def test_the_term_structure_runs_from_a_point_mass_to_the_stationary_body(self):
        """What replaces a free parameter refitted at every expiry."""
        zone = TargetZone("T", kappa=0.6, sigma=1.1, m=0.5)
        concs = [zone.concentration_at(0.90, t) for t in (0.02, 0.25, 1.0, 3.0, 50.0)]
        self.assertTrue(all(x > y for x, y in zip(concs, concs[1:])), concs)
        self.assertGreater(concs[0], 20.0)                       # nearly a point mass
        self.assertAlmostEqual(concs[-1], zone.concentration, places=4)

    def test_u_shaped_is_the_same_test_the_mixture_applies_to_its_body(self):
        self.assertTrue(TargetZone("T", kappa=0.5, sigma=1.2, m=0.5).u_shaped)
        self.assertFalse(TargetZone("T", kappa=2.0, sigma=0.7, m=0.5).u_shaped)

    def test_a_degenerate_input_is_refused_rather_than_returned(self):
        for bad in ({"kappa": -0.1}, {"sigma": 0.0}, {"m": 1.0}, {"m": 0.0}):
            with self.assertRaises(ValueError):
                TargetZone("T", **{"kappa": 1.0, "sigma": 1.0, "m": 0.5, **bad})


class TestEstimate(unittest.TestCase):
    """Fitting the process to a series of band positions."""

    def test_it_recovers_the_diffusion_and_the_shape_from_its_own_process(self):
        """``sigma`` is recovered sharply and the U-shaped / bell call is
        right; ``kappa`` is not pinned down and is not asserted to be, which
        is the honest statement about a mean-reversion speed measured over a
        decade of daily data."""
        for kappa, sigma, m, u in ((0.6, 1.1, 0.5, True), (2.0, 0.7, 0.3, False)):
            x, ts = jacobi_path(m, m, kappa, sigma, 10, seed=31)
            zone = estimate("TEST", x, ts)
            self.assertAlmostEqual(zone.sigma, sigma, delta=0.12)
            self.assertEqual(zone.u_shaped, u)
            self.assertGreater(zone.observations, 2000)

    def test_the_uncertainty_on_kappa_travels_with_the_estimate(self):
        """The concentration is quoted as a range because the point estimate
        of ``kappa`` is loose; a comparison that ignored the width would read
        a disagreement into ordinary sampling noise."""
        x, ts = jacobi_path(0.5, 0.5, 0.6, 1.1, 10, seed=31)
        zone = estimate("TEST", x, ts)
        self.assertGreater(zone.kappa_stderr, 0.0)
        lo, hi = zone.concentration_range()
        self.assertLess(lo, zone.concentration)
        self.assertGreater(hi, zone.concentration)
        self.assertAlmostEqual(hi - lo, 4.0 * zone.kappa_stderr / zone.sigma ** 2, places=9)

    def test_too_short_a_series_is_refused_by_name(self):
        x, ts = jacobi_path(0.5, 0.5, 0.6, 1.1, 0.1, seed=3)
        with self.assertRaises(ValueError) as ctx:
            estimate("TEST", x, ts)
        self.assertIn(str(MIN_STEPS), str(ctx.exception))

    def test_a_constant_series_has_no_diffusion_and_says_so(self):
        n = 400
        with self.assertRaises(ValueError):
            estimate("TEST", np.full(n, 0.5), np.arange(n) / 252.0)


class TestDynamicsPanel(unittest.TestCase):
    """The read-out: the at-the-money the dynamics predict, beside the marked one."""

    def book(self, *, band=True):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        if band:
            surface.band = BAND
            surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        return book

    def history(self):
        from volkit.history import load_history
        from ._support import HISTORY
        return load_history(HISTORY, ["EURUSD"])

    def test_a_pair_with_no_band_is_told_where_bands_live(self):
        out = dynamics_panel(self.book(band=False), "EURUSD", None, ["3M"])
        self.assertFalse(out["has_band"])
        self.assertIn("PEG_BANDS", out["message"])

    def test_no_history_is_said_rather_than_guessed(self):
        out = dynamics_panel(self.book(), "EURUSD", None, ["3M"])
        self.assertIn("spot series", out["message"])

    def test_it_puts_the_measured_concentration_beside_the_marked_one(self):
        out = dynamics_panel(self.book(), "EURUSD", self.history()["EURUSD"], ["3M", "1Y"])
        self.assertEqual(out["message"], "")
        self.assertIsNotNone(out["zone"])
        priced = [r for r in out["rows"] if not r["message"]]
        self.assertTrue(priced)
        for row in priced:
            for key in ("marked_concentration", "dynamics_concentration",
                        "marked_atm", "dynamics_atm", "difference"):
                self.assertIsNotNone(row[key], key)
            self.assertAlmostEqual(row["difference"],
                                   row["marked_atm"] - row["dynamics_atm"], places=12)

    def test_only_the_spread_is_taken_from_the_measurement(self):
        """The mean stays the forward's, through ``_BodyFit.build``'s
        re-centring. A disagreement here is about the *shape* of the band and
        never about its level -- the forward is a price and this module has no
        business overriding it."""
        from volkit.banded import _BodyFit

        book = self.book()
        out = dynamics_panel(book, "EURUSD", self.history()["EURUSD"], ["1Y"])
        row = next(r for r in out["rows"] if not r["message"])
        t = book.tenor_years("EURUSD", "1Y")
        expiry = book["EURUSD"].clock.datetime_from_years(t)
        level = book.market_level_for("EURUSD", expiry)
        fit = _BodyFit(BAND, float(level["forward"]), t, row["marked_atm"],
                       book["EURUSD"].conv, "1Y")
        built = fit.build(row["dynamics_concentration"],
                          book["EURUSD"].band_treatment.jump)
        self.assertAlmostEqual(built.mean, float(level["forward"]), places=9)

    def test_an_unresolvable_pull_is_flagged_rather_than_quoted_as_a_number(self):
        out = dynamics_panel(self.book(), "EURUSD", self.history()["EURUSD"], ["3M"])
        zone = out["zone"]
        if zone["kappa_stderr"] >= zone["kappa"]:
            self.assertTrue(any("not distinguishable" in w for w in out["warnings"]),
                            out["warnings"])
        if min(zone["m"], 1 - zone["m"]) < EDGE_CENTRE:
            self.assertTrue(any("fitted centre" in w for w in out["warnings"]),
                            out["warnings"])


class TestBandTouch(unittest.TestCase):
    """Path-dependent payouts that know the edges are defended."""

    ZONE = TargetZone("USDHKD", kappa=0.55, sigma=1.15, m=0.5)
    SPEC = JumpSpec(hazard=0.08, weak_share=0.6, weak_jump=0.02, strong_jump=0.07,
                    weak_vol=0.02, strong_vol=0.05)
    SPOT, FWD, T = 7.8402, 7.7694, 1.0

    def touch(self, barrier, spec=None, **kw):
        return band_touch(self.SPOT, barrier, HKD, self.T, self.FWD, self.ZONE,
                          spec or self.SPEC, paths=20_000, seed=7, **kw)

    def test_a_defended_edge_is_unreachable_by_diffusion(self):
        """The headline. ``sigma sqrt(x(1-x))`` vanishes at the edge, so the
        peg-intact leg can never touch a Convertibility Undertaking: the only
        route to it is a break, and the price is the break's."""
        r = self.touch(7.75)
        self.assertEqual(r.intact, 0.0)
        self.assertGreater(r.broken, 0.0)
        self.assertLess(r.probability, r.break_probability + 1e-9)
        # what a lognormal would have charged for the same one-touch
        self.assertGreater(r.lognormal_probability, 10 * r.probability)

    def test_outside_the_band_with_no_break_risk_the_touch_is_exactly_zero(self):
        calm = JumpSpec(hazard=0.0, weak_share=0.6, weak_jump=0.02, strong_jump=0.07,
                        weak_vol=0.02, strong_vol=0.05)
        r = self.touch(7.90, calm)
        self.assertFalse(r.reachable_in_band)
        self.assertEqual(r.probability, 0.0)
        self.assertGreater(r.lognormal_probability, 0.3)

    def test_inside_the_band_the_lognormal_overstates_the_chance(self):
        """It has no idea the edges are defended and its diffusion does not
        die as it nears one."""
        r = self.touch(7.80)
        self.assertGreater(r.intact, 0.0)
        self.assertLess(r.probability, r.lognormal_probability)

    def test_a_no_touch_is_one_less_the_touch_on_the_same_paths(self):
        a = self.touch(7.80)
        b = self.touch(7.80, is_no_touch=True)
        self.assertAlmostEqual(a.probability, b.probability, places=12)
        self.assertAlmostEqual(a.price + b.price, 1.0, places=12)

    def test_more_break_risk_buys_more_touches_of_an_unreachable_level(self):
        low = self.touch(7.90, JumpSpec(hazard=0.02, weak_share=0.6, weak_jump=0.02,
                                        strong_jump=0.07, weak_vol=0.02, strong_vol=0.05))
        high = self.touch(7.90, JumpSpec(hazard=0.40, weak_share=0.6, weak_jump=0.02,
                                         strong_jump=0.07, weak_vol=0.02, strong_vol=0.05))
        self.assertGreater(high.probability, low.probability)

    def test_it_reports_its_own_sampling_error(self):
        r = self.touch(7.80)
        self.assertGreater(r.std_error, 0.0)
        self.assertLess(r.std_error, 0.02)
        self.assertIn("monte carlo", r.method)


class TestPricingUsesTheZone(unittest.TestCase):
    """A touch priced under BAND reads the measured process, or says it did not."""

    def book(self, *, zone=True):
        from volkit.banded import BandTreatment

        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = BAND
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        surface.set_band_treatment(BandTreatment(mode="mixture"))
        if zone:
            surface.target_zone = TargetZone("EURUSD", kappa=0.6, sigma=1.1, m=0.5)
        return book

    def leg(self, **kw):
        from volkit.pricing import OptionLeg
        return OptionLeg(pair="EURUSD", expiry="3M", product="one_touch",
                         barrier="1.20", cut="NY", **kw)

    def price(self, book, **kw):
        from volkit.pricing import price_leg
        out = price_leg(book, self.leg(**kw))
        self.assertTrue(out.ok, out.error)
        return out

    def test_band_uses_the_mixture_and_svi_does_not(self):
        book = self.book()
        self.assertIn("mixture", self.price(book, method="BAND").pricing_method)
        self.assertNotIn("mixture", self.price(book, method="SVI").pricing_method)

    def test_without_a_measured_zone_the_touch_stays_lognormal(self):
        """A pair with a band but no history to measure its process is the
        ordinary case, and it must not silently price as though it had one."""
        out = self.price(self.book(zone=False), method="BAND")
        self.assertNotIn("mixture", out.pricing_method)

    def test_an_overhedge_falls_back_because_the_mixture_has_no_bend(self):
        """The buffers shift or bend the barrier, which the mixture path does
        not implement; the leg gets the engine that does."""
        out = self.price(self.book(), method="BAND", overhedge="extend", buffer_pct="0.5")
        self.assertNotIn("mixture", out.pricing_method)

    def test_attaching_reports_what_it_did_and_why_not(self):
        from volkit.history import load_history
        from ._support import HISTORY

        book = self.book(zone=False)
        out = book.attach_target_zones(load_history(HISTORY, ["EURUSD"]))
        self.assertIn("EURUSD", out)
        self.assertTrue(out["EURUSD"]["attached"], out["EURUSD"]["message"])
        self.assertIsNotNone(book["EURUSD"].target_zone)

    def test_a_pair_with_no_sheet_keeps_no_zone_and_carries_the_reason(self):
        from volkit.history import History

        book = self.book(zone=False)
        out = book.attach_target_zones(History())
        self.assertFalse(out["EURUSD"]["attached"])
        self.assertIn("no sheet", out["EURUSD"]["message"])
        self.assertIsNone(book["EURUSD"].target_zone)


if __name__ == "__main__":
    unittest.main()
