"""The swap points' reading on peg-break risk.

The module's whole claim is that a closed form derived by hand reproduces the
constraint ``banded`` enforces through a solver, so that is what most of this
pins: the identity against ``required_in_band_mean``, and the ceiling landing
exactly where ``_BodyFit.build`` stops building.  If those two hold the rest
is presentation.

The other half is the sign discipline.  A forward gap that runs opposite to
the marked break direction produces a *negative* break probability, and that
number is the module's most useful output rather than an error -- it is how a
carry-driven forward is told apart from a fear-driven one.  There are tests
here that exist only to stop somebody clamping it to zero.
"""

from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from volkit.banded import Band, BandTreatment, BetaBandSmile, JumpSpec, _BodyFit
from volkit.book import Book
from volkit.feed import MarketFeed
from volkit.pegcarry import (
    AGGREGATE_BALANCE,
    BALANCE_DAYS,
    BALANCE_STALE_DAYS,
    BREAK_SHARE_FLOOR,
    aggregate_balance,
    rate_legs,
    break_probability_for_anchor,
    break_share_of_gap,
    expected_break_multiple,
    forward_hazard_bound,
    hazard_from_probability,
    peg_carry_panel,
)

from ._support import ASOF, BOOK, FEED

UTC = timezone.utc

HKD = Band("USDHKD", 7.75, 7.85, "HKMA Convertibility Undertakings")
#: The live shape of the USDHKD curve: HKD rates below USD rates, so the
#: forward runs *down* through the band as tenor extends.  7.7694 is the
#: one-year outright off a spot of 7.8402 and -708 points.
HKD_1Y = 7.7694
HKD_SPOT = 7.8402

DEVALUE = JumpSpec()                                      # weak-side biased, the default
REVALUE = JumpSpec(weak_share=0.2, weak_jump=0.03, strong_jump=0.08)


def _respec(spec: JumpSpec, hazard: float) -> JumpSpec:
    return JumpSpec(hazard=hazard, weak_share=spec.weak_share, weak_jump=spec.weak_jump,
                    weak_vol=spec.weak_vol, strong_jump=spec.strong_jump,
                    strong_vol=spec.strong_vol)


class TestClosedForm(unittest.TestCase):
    """The algebra behind the module, against the model it claims to describe."""

    def test_the_anchor_solve_inverts_required_in_band_mean(self):
        """``b = (m - F)/(m - F J)`` is the exact inverse of the forward constraint.

        Everything else here rests on this, so it is checked at both jump
        orientations and across a wide range of hazards rather than at one
        convenient point.
        """
        for spec in (DEVALUE, REVALUE):
            for t in (0.25, 1.0, 2.0):
                for hazard in (0.01, 0.05, 0.2, 0.75):
                    s = _respec(spec, hazard)
                    mean = BetaBandSmile(HKD, 1.0, 1.0, t, 7.80, s).required_in_band_mean()
                    got = break_probability_for_anchor(7.80, mean, s)
                    self.assertAlmostEqual(got, 1.0 - math.exp(-hazard * t), places=12)

    def test_a_break_with_no_expected_direction_places_no_bound(self):
        """Offsetting jumps leave the forward independent of the hazard."""
        # An equal-sized jump either way does *not* offset at weak_share 0.5:
        # e^x and e^-x average above 1.  Solve for the share that does.
        share = (1.0 - math.exp(-0.05)) / (math.exp(0.05) - math.exp(-0.05))
        flat = JumpSpec(weak_share=share, weak_jump=0.05, strong_jump=0.05)
        self.assertAlmostEqual(expected_break_multiple(flat), 1.0, places=12)
        self.assertIsNone(break_probability_for_anchor(7.80, 7.78, flat))
        out = forward_hazard_bound(HKD, 7.80, 1.0, flat)
        self.assertIsNone(out["hazard"])
        self.assertIn("no expected direction", out["message"])

    def test_a_probability_that_is_not_one_has_no_hazard_behind_it(self):
        self.assertIsNone(hazard_from_probability(-0.24, 1.0))
        self.assertIsNone(hazard_from_probability(1.0, 1.0))
        self.assertIsNone(hazard_from_probability(0.5, 0.0))
        self.assertAlmostEqual(hazard_from_probability(1 - math.exp(-0.3), 1.0), 0.3, places=12)


class TestCeiling(unittest.TestCase):
    """The bound, against the refusal it is meant to predict."""

    def test_the_ceiling_is_exactly_where_the_body_stops_building(self):
        """The claim that makes this worth having: no solver, same answer.

        ``_BodyFit.build`` raises once the peg-intact mean leaves the band.
        The closed-form ceiling has to sit on that boundary from both sides,
        at both jump orientations, or it is bounding something else.
        """
        for spec in (DEVALUE, REVALUE):
            for forward, t in ((HKD_1Y, 1.0), (7.818, 0.25), (7.80, 0.5)):
                bound = forward_hazard_bound(HKD, forward, t, spec)
                hazard = bound["hazard"]
                self.assertIsNotNone(hazard)
                fit = _BodyFit(HKD, forward, t, 0.005)
                fit.build(2.0, _respec(spec, hazard * 0.999))       # builds
                with self.assertRaises(ValueError):
                    fit.build(2.0, _respec(spec, hazard * 1.001))   # does not

    def test_which_edge_binds_follows_the_marked_break_direction(self):
        """A devaluation drags the body down to pay for itself, so 7.75 binds."""
        self.assertGreater(expected_break_multiple(DEVALUE), 1.0)
        self.assertLess(expected_break_multiple(REVALUE), 1.0)
        self.assertEqual(forward_hazard_bound(HKD, 7.80, 1.0, DEVALUE)["edge"], "lower")
        self.assertEqual(forward_hazard_bound(HKD, 7.80, 1.0, REVALUE)["edge"], "upper")

    def test_the_bound_tightens_with_tenor_on_a_curve_that_walks_to_an_edge(self):
        """Why the long end is the one worth arguing about.

        The front forward sits near spot and permits an enormous hazard; the
        one-year sits 194 pips off the strong edge and pins it. A reading that
        quoted the 1M ceiling as "the" constraint would be quoting the loosest
        number on the curve.
        """
        near = forward_hazard_bound(HKD, 7.8328, 1 / 12, DEVALUE)["hazard"]
        far = forward_hazard_bound(HKD, HKD_1Y, 1.0, DEVALUE)["hazard"]
        self.assertGreater(near, far)
        self.assertLess(far, 0.06)          # ~5.2%/yr on the live curve
        self.assertGreater(far, 0.04)

    def test_a_forward_outside_the_band_is_a_refusal_with_the_reason(self):
        out = forward_hazard_bound(HKD, 7.90, 1.0, DEVALUE)
        self.assertIsNone(out["hazard"])
        self.assertFalse(out["binding"])
        self.assertIn("outside the band", out["message"])


class TestCarryAgainstBreak(unittest.TestCase):
    """Telling a carry-driven forward apart from a fear-driven one."""

    def test_a_forward_running_against_the_break_gives_a_negative_probability(self):
        """**Not** an error, and must never be clamped.

        USDHKD trades at a discount because HKD rates sit below USD rates. If
        the peg-intact mean were spot, matching that discount would need a
        *negative* weight on a devaluation break. The negative number is the
        module saying the gap is carry; a clamp to zero would report the same
        thing as a pair with no carry and no fear at all.
        """
        got = break_probability_for_anchor(HKD_1Y, HKD_SPOT, DEVALUE)
        self.assertLess(got, 0.0)

    def test_the_share_reproduces_the_mixtures_own_forward(self):
        """The attribution is the model's arithmetic, not a rule of thumb.

        Holding the peg-intact mean at spot, the share is the gap the mixture
        itself would show over the gap the market shows, so rebuilding that
        forward from the share has to give the number back.
        """
        for spec in (DEVALUE, REVALUE):
            for t in (0.25, 1.0):
                share = break_share_of_gap(HKD_SPOT, HKD_1Y, t, spec)
                modelled = share * (HKD_1Y - HKD_SPOT)
                b = 1.0 - math.exp(-spec.hazard * t)
                direct = HKD_SPOT * b * (expected_break_multiple(spec) - 1.0)
                self.assertAlmostEqual(modelled, direct, places=12)

    def test_the_same_forward_is_consistent_with_a_revaluation_break(self):
        """The sign against the band is the whole diagnostic.

        A forward below spot is exactly what a strong-side break would
        produce, so the identical number reads as a real probability once the
        marked break direction is flipped.
        """
        got = break_probability_for_anchor(HKD_1Y, HKD_SPOT, REVALUE)
        self.assertGreater(got, 0.0)
        self.assertLess(got, 1.0)


class TestPanel(unittest.TestCase):
    """The read-out, on a book with a band and a feed."""

    BAND = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")

    def book(self, *, feed=True, treatment=None):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        if feed:
            book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = self.BAND
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        if treatment is not None:
            surface.set_band_treatment(treatment)
        return book

    def test_a_pair_with_no_band_is_told_where_bands_live(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        out = peg_carry_panel(book, "EURUSD", ["3M"])
        self.assertFalse(out["has_band"])
        self.assertIn("PEG_BANDS", out["message"])

    def test_it_reports_a_bound_and_names_the_binding_tenor(self):
        out = peg_carry_panel(self.book(), "EURUSD", ["1M", "3M", "1Y"])
        self.assertTrue(out["has_band"])
        self.assertEqual(len(out["rows"]), 3)
        priced = [r for r in out["rows"] if r["ceiling_hazard"] is not None]
        self.assertTrue(priced)
        tightest = min(priced, key=lambda r: r["ceiling_hazard"])
        self.assertEqual(out["summary"]["binding_tenor"], tightest["tenor"])
        self.assertEqual(out["summary"]["binding_hazard"], tightest["ceiling_hazard"])

    def test_the_differential_is_named_by_its_currencies_not_by_a_sign(self):
        """A swap point's sign is a quoting convention; ``r_quote - r_base`` is not."""
        out = peg_carry_panel(self.book(), "EURUSD", ["1Y"])
        self.assertEqual((out["base"], out["quote"]), ("EUR", "USD"))
        row = out["rows"][0]
        # EURUSD trades at a premium in the sample feed, so USD rates sit above EUR.
        self.assertGreater(row["forward"], row["spot"])
        self.assertGreater(row["differential"], 0.0)

    def test_a_marked_hazard_above_the_ceiling_is_called_inconsistent(self):
        """The cross-check the module exists for, in the direction that bites."""
        out = peg_carry_panel(self.book(), "EURUSD", ["1Y"])
        ceiling = out["rows"][0]["ceiling_hazard"]
        self.assertIsNotNone(ceiling)
        book = self.book(treatment=BandTreatment(jump=JumpSpec(hazard=ceiling * 2.0)))
        worse = peg_carry_panel(book, "EURUSD", ["1Y"])
        self.assertFalse(worse["rows"][0]["consistent"])
        self.assertLess(worse["rows"][0]["headroom"], 0.0)
        self.assertFalse(worse["summary"]["consistent"])
        self.assertIn("above the", worse["summary"]["verdict"])

    def test_without_a_feed_it_refuses_rather_than_assuming_a_level(self):
        """The band model's own rule: a guessed forward places the band wrongly."""
        out = peg_carry_panel(self.book(feed=False), "EURUSD", ["3M"])
        row = out["rows"][0]
        self.assertIsNone(row["forward"])
        self.assertIn("feed", row["message"])
        self.assertIn("no tenor", out["summary"]["verdict"])

    def test_a_forward_near_an_edge_is_warned_about_before_it_leaves(self):
        """A tenor can stop calibrating on carry alone, with nobody's view moving."""
        book = self.book(treatment=BandTreatment(upper=1.0860))
        out = peg_carry_panel(book, "EURUSD", ["1Y"])
        self.assertTrue(any("pips from the" in w for w in out["warnings"]))

    def test_one_tenor_failing_does_not_take_the_panel_with_it(self):
        out = peg_carry_panel(self.book(), "EURUSD", ["3M", "NOPE"])
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["rows"][0]["message"], "")
        self.assertTrue(out["rows"][1]["message"])

    def test_an_unbuilt_pair_is_a_refusal_not_an_empty_panel(self):
        with self.assertRaises(ValueError) as ctx:
            peg_carry_panel(self.book(), "USDZZZ", ["3M"])
        self.assertIn("not built", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class TestAttributionWarnsInBothOrientations(unittest.TestCase):
    """The bug this class exists for.

    ``carry_dominated`` was keyed on the *sign* of the spot-anchored break
    probability. That works against a devaluation marking, where a carry-driven
    forward produces an obviously impossible negative -- and stays completely
    silent against a revaluation marking, where the identical forward produces
    a quietly plausible large probability instead. On the desk book USDHKD is
    marked with a net revaluation break, so the orientation that failed was the
    live one: reading the whole 1-year discount as break premium gave 38%,
    nobody believed it, and nothing said so. The share is orientation-free and
    is what the flag is keyed on now.
    """

    BAND = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")

    def panel(self, spec):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = self.BAND
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        surface.set_band_treatment(BandTreatment(jump=spec))
        return peg_carry_panel(book, "EURUSD", ["1Y"])

    def test_a_plausible_looking_probability_is_still_flagged_as_carry(self):
        """The orientation the sign test passed. EURUSD trades at a premium in
        the sample feed, so against a devaluation marking the spot-anchored
        probability is *positive* and unremarkable -- and the marked regime
        still explains well under half the gap."""
        row = self.panel(DEVALUE)["rows"][0]
        self.assertGreater(row["spot_anchored_probability"], 0.0)   # a sign test sees nothing
        self.assertLess(row["break_share_of_gap"], BREAK_SHARE_FLOOR)
        self.assertTrue(row["carry_dominated"])

    def test_it_says_so_in_the_warnings_rather_than_only_in_a_column(self):
        out = self.panel(DEVALUE)
        self.assertTrue(any("under half the forward" in w for w in out["warnings"]),
                        out["warnings"])
        self.assertTrue(out["summary"]["carry_dominated"])

    def test_the_opposite_orientation_keeps_its_own_message(self):
        """A share below zero is a different statement from a share below a
        half, and collapsing them would lose the stronger one."""
        out = self.panel(REVALUE)
        self.assertLess(out["rows"][0]["break_share_of_gap"], 0.0)
        self.assertTrue(any("opposite to the marked" in w for w in out["warnings"]),
                        out["warnings"])

    def test_the_summary_carries_the_attribution_at_the_binding_tenor(self):
        out = self.panel(DEVALUE)
        self.assertEqual(out["summary"]["break_share"],
                         out["rows"][0]["break_share_of_gap"])


def _feed_file(directory, rows):
    """A feed CSV the test owns, so the arithmetic is pinned to known rates."""
    path = Path(directory) / "feed.csv"
    path.write_text("\n".join(",".join(str(x) for x in r) for r in rows) + "\n",
                    encoding="utf-8")
    return MarketFeed.load(path)


class TestRateLegs(unittest.TestCase):
    """Splitting the net differential into the two money-market legs.

    The net is read off the traded outright and is right for pricing; it
    cannot say *which* leg moved the forward, which is the only thing that
    separates peg stress from the anchor currency repricing.
    """

    def curves(self, book):
        return book.discount

    def test_the_implied_rate_reproduces_the_forward_it_came_from(self):
        """The identity, not a pinned number: ``F = S x DF_base / DF_term``.

        Rearranging that the wrong way up gives a discount factor that looks
        plausible and is reciprocal (``discount._fx_ratio`` says so in as many
        words), so the round trip is what is checked rather than a figure.
        """
        from volkit.discount import discount_factor

        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        book.feed = MarketFeed.load(FEED)
        t = 1.0
        level = book.market_level("USDJPY", t)
        legs = rate_legs(book.discount, "USD", "JPY", level["spot"], level["forward"], t)
        self.assertIsNotNone(legs["quote_rate_implied"])
        df_base = book.discount.ois["USD"].df(t)
        df_quote = discount_factor(legs["quote_rate_implied"], t)
        self.assertAlmostEqual(level["spot"] * df_base / df_quote, level["forward"], places=9)

    def test_a_cip_consistent_stated_curve_shows_no_basis(self):
        """The basis is the stated curve less the implied one, so a stated
        curve that *is* the implied one has to come back at zero. Anything
        else means the two are not being compared on one compounding."""
        from volkit.discount import discount_factor

        spot, t, r_base = 7.8402, 1.0, 0.045
        forward = 7.7694
        implied = (spot * discount_factor(r_base, t) / forward) ** (-1.0 / t) - 1.0
        with tempfile.TemporaryDirectory() as d:
            feed = _feed_file(d, [
                ("USDHKD", "SPOT", spot),
                ("USDHKD", "1Y", round((forward - spot) * 10000, 6)),
                ("USDOIS", "1Y", r_base * 100),
                ("HKDOIS", "1Y", implied * 100),
            ])
            book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
            book.feed = feed
            legs = rate_legs(book.discount, "USD", "HKD", spot, forward, t)
        self.assertAlmostEqual(legs["basis"], 0.0, places=9)
        self.assertAlmostEqual(legs["base_rate"], r_base, places=12)

    def test_a_missing_leg_is_named_rather_than_guessed(self):
        """A differential assembled from one curve and a guess is the silent
        default this project exists to remove."""
        with tempfile.TemporaryDirectory() as d:
            feed = _feed_file(d, [("USDHKD", "SPOT", 7.84), ("USDHKD", "1Y", -708),
                                  ("USDOIS", "1Y", 4.5)])
            book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
            book.feed = feed
            legs = rate_legs(book.discount, "USD", "HKD", 7.84, 7.7692, 1.0)
        self.assertIsNone(legs["quote_rate"])
        self.assertIsNone(legs["basis"])
        self.assertIn("HKDOIS", legs["source"])
        # The leg that *is* stated still answers; a missing one is not
        # contagious.
        self.assertIsNotNone(legs["base_rate"])


def _history_file(directory, *, sheet="USDHKD", header=AGGREGATE_BALANCE, rows=120,
                  balance=None, also_pair_sheet=False, blank_last=0, blank_every=0):
    """A history workbook with the balance column on ``sheet``.

    ``also_pair_sheet`` adds a readable USDHKD sheet *without* the column,
    which is what makes a currency-named sheet reachable as a skipped one:
    a workbook whose only sheet cannot be placed does not load at all.
    """
    import numpy as np
    import pandas as pd

    path = Path(directory) / "hist.xlsx"
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    if balance is None:
        balance = np.linspace(180000.0, 50000.0, rows)
    balance = np.asarray(balance, dtype=float).copy()
    if blank_every:
        balance[::blank_every] = np.nan
    if blank_last:
        balance[-blank_last:] = np.nan
    base = {"Date": days, "Spot": np.full(rows, 7.84), "ATM 1M": np.full(rows, 0.35)}
    with pd.ExcelWriter(path) as writer:
        if also_pair_sheet:
            pd.DataFrame(base).to_excel(writer, sheet_name="USDHKD", index=False)
        pd.DataFrame(dict(base, **{header: balance})).to_excel(
            writer, sheet_name=sheet, index=False)
    return path


class TestAggregateBalance(unittest.TestCase):
    """The state variable the defence runs on, off the history sheet."""

    def load(self, path):
        from volkit.history import load_history
        return load_history(path, ["USDHKD"])

    def test_an_unrecognised_column_is_kept_by_name_rather_than_dropped(self):
        """The old behaviour: any header this module had no reading of was
        reported as "not understood and is unused" and discarded. A sheet is
        somebody's file and a column on it was put there on purpose."""
        with tempfile.TemporaryDirectory() as d:
            hist = self.load(_history_file(d))
        pair = hist["USDHKD"]
        self.assertIsNotNone(pair.extra(AGGREGATE_BALANCE))
        # asked for however it is spelled
        for spelling in ("bal clos index", "BAL_CLOS_INDEX", "BALCLOSIndex"):
            self.assertIsNotNone(pair.extra(spelling), spelling)
        self.assertFalse(any("unused" in p for p in pair.problems), pair.problems)

    def test_it_reports_the_level_its_place_in_its_own_range_and_the_move(self):
        with tempfile.TemporaryDirectory() as d:
            out = aggregate_balance(self.load(_history_file(d)), "USDHKD")
        self.assertTrue(out["found"])
        self.assertEqual(out["sheet"], "USDHKD")
        self.assertAlmostEqual(out["latest"], 50000.0, places=6)
        self.assertAlmostEqual(out["low"], 50000.0, places=6)
        self.assertAlmostEqual(out["high"], 180000.0, places=6)
        self.assertLess(out["percentile"], 0.02)      # sitting at its own floor
        self.assertLess(out["change"], 0.0)           # and draining
        self.assertEqual(out["days"], BALANCE_DAYS)

    def test_the_level_alone_is_not_the_signal_so_a_percentile_comes_with_it(self):
        """The same absolute figure reads opposite ways depending on the range
        it sits in, which is why the number on its own is not reported."""
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            high = aggregate_balance(self.load(_history_file(
                d, balance=np.linspace(20000.0, 50000.0, 120))), "USDHKD")
        self.assertAlmostEqual(high["latest"], 50000.0, places=6)
        self.assertGreater(high["percentile"], 0.98)   # the same 50,000, at its top

    def test_no_history_is_said_rather_than_left_blank(self):
        out = aggregate_balance(None, "USDHKD")
        self.assertFalse(out["found"])
        self.assertIn("no history workbook", out["message"])

    def test_a_sheet_named_for_a_currency_is_skipped_and_the_message_says_so(self):
        """``history._sheet_to_pair`` needs six letters, so a tab called "HKD"
        never reaches the extras at all. An empty answer would send somebody
        looking for the column; the skipped sheet is what they need told."""
        with tempfile.TemporaryDirectory() as d:
            hist = self.load(_history_file(d, sheet="HKD", also_pair_sheet=True))
        self.assertTrue(any("HKD" in x for x in hist.skipped_sheets), hist.skipped_sheets)
        out = aggregate_balance(hist, "USDHKD")
        self.assertFalse(out["found"])
        self.assertIn("skipped", out["message"])
        self.assertIn("HKD", out["message"])

    def test_a_workbook_with_no_readable_sheet_at_all_refuses_by_name(self):
        """Not this module's message: ``load_history`` never returns for a
        workbook whose only sheet cannot be placed, and says which sheet."""
        from volkit.history import HistoryError
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(HistoryError) as ctx:
                self.load(_history_file(d, sheet="HKD"))
        self.assertIn("HKD", str(ctx.exception))

    def test_the_panel_carries_both_new_blocks(self):
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        book.feed = MarketFeed.load(FEED)
        surface = book["EURUSD"]
        surface.band = Band("EURUSD", 1.05, 1.12, "synthetic, for the test only")
        surface.forward_lookup = lambda t: book.forward_at("EURUSD", t)
        with tempfile.TemporaryDirectory() as d:
            out = peg_carry_panel(book, "EURUSD", ["1Y"],
                                  history=self.load(_history_file(d)))
        self.assertIn("rates", out)
        self.assertTrue(out["aggregate_balance"]["found"])
        # EURUSD: the feed states USDOIS but no EUROIS, so the legs refuse by name.
        self.assertIn("EUROIS", out["rates"])


class TestAggregateBalanceGaps(unittest.TestCase):
    """A balance column that runs behind the price columns beside it.

    The HKMA publishes the balance every business day and a history sheet
    commonly carries it a few days late, so the reading that matters is the
    **last one published**, not whatever is in today's cell. That fallback was
    already the behaviour; what these pin is that the fallback is *visible* --
    a forty-day-old number presented as current, on the one series whose whole
    value is that it leads the rate, is the failure worth testing for.
    """

    def load(self, path):
        from volkit.history import load_history
        return load_history(path, ["USDHKD"])

    def read(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            return aggregate_balance(self.load(_history_file(d, rows=200, **kw)), "USDHKD")

    def test_a_blank_today_falls_back_to_the_last_published_reading(self):
        full, gapped = self.read(), self.read(blank_last=5)
        self.assertTrue(gapped["found"])
        self.assertNotEqual(gapped["date"], full["date"])
        self.assertLess(gapped["date"], full["date"])          # an earlier day
        self.assertEqual(gapped["observations"], full["observations"] - 5)
        # and the number really is that row's, not the last row's
        self.assertNotAlmostEqual(gapped["latest"], full["latest"], places=6)

    def test_the_sheets_own_last_date_travels_with_the_reading(self):
        """Both dates, so the gap is on the screen rather than inferred."""
        out = self.read(blank_last=5)
        self.assertEqual(out["as_of"], self.read()["date"])
        self.assertEqual(out["stale_days"], 5)

    def test_a_short_gap_is_not_called_stale_and_a_long_one_is(self):
        self.assertFalse(self.read(blank_last=BALANCE_STALE_DAYS)["stale"])
        stale = self.read(blank_last=BALANCE_STALE_DAYS + 30)
        self.assertTrue(stale["stale"])
        self.assertIn("behind the sheet", stale["message"])
        self.assertIn(stale["as_of"], stale["message"])
        self.assertIn(stale["date"], stale["message"])

    def test_the_move_is_measured_over_days_and_not_over_rows(self):
        """A column with holes in it would otherwise make the window mean a
        different length on every pair, while the label kept saying one thing."""
        for kw in ({}, {"blank_last": 5}, {"blank_every": 3}, {"blank_every": 4,
                                                               "blank_last": 9}):
            out = self.read(**kw)
            self.assertIsNotNone(out["change"], kw)
            self.assertGreaterEqual(out["change_days"], BALANCE_DAYS, kw)
            # the comparison is the first published reading at or before the
            # cutoff, so it cannot overshoot by more than the gap it skipped
            self.assertLess(out["change_days"], BALANCE_DAYS + 12, kw)
            self.assertTrue(out["previous_date"] < out["date"], kw)

    def test_a_column_too_short_to_reach_back_says_so_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as d:
            out = aggregate_balance(self.load(_history_file(d, rows=70, blank_last=50)),
                                    "USDHKD")
        self.assertTrue(out["found"])
        self.assertIsNone(out["change"])
        self.assertIn("does not reach back", out["message"])

    def test_an_entirely_blank_column_is_not_a_reading_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            hist = self.load(_history_file(d, rows=200, blank_last=200))
        out = aggregate_balance(hist, "USDHKD")
        self.assertFalse(out["found"])
