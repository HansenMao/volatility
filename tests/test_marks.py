"""Marking a curve, a smile or a quote, and held fits going stale.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestCurveMarking(unittest.TestCase):
    def setUp(self):
        self.book = Book.from_excel(BOOK, ASOF).build(["USDJPY", "AUDJPY"])

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

    def test_the_events_route_takes_parts_and_returns_the_total(self):
        """The panel posts each leg's weight and the adjustment; the rows
        that come back carry both and their total."""
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        when = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        r = service.set_events({"pair": "USDJPY", "events": [
            {"when": when.strftime("%Y-%m-%dT%H:%M"), "weights": {"USD": 1.5, "JPY": 0.3},
             "adjust": 0.2, "label": "FOMC"}]})
        # A dollar weight reaches every dollar pair, so a warning may come
        # back for one of them; none of them is USDJPY's.
        self.assertEqual([p for p in r["problems"] if p.startswith("USDJPY")], [])
        row = next(e for e in r["events"] if e["label"] == "FOMC")
        self.assertAlmostEqual(row["bump"], 2.0)
        self.assertEqual(set(row["weights"]), {"USD", "JPY"})
        self.assertAlmostEqual(row["weights"]["JPY"], 0.3)
        self.assertAlmostEqual(row["adjust"], 0.2)
        self.assertEqual(service.curve({"pair": "USDJPY"})["legs_ccy"], ["USD", "JPY"])
        with self.assertRaises(ValueError):
            service.set_events({"pair": "USDJPY", "events": [
                {"when": when.strftime("%Y-%m-%dT%H:%M"), "weights": {"USD": 1.5}, "adjust": 0,
                 "bump": 3.0}]})

    def test_a_weight_marked_on_one_pair_reaches_the_others(self):
        """The sheet's currency columns are shared.  A dollar weight typed on
        USDJPY's panel is EURUSD's too, and the reply says which pairs moved
        -- the old arrangement kept a copy per pair, so the two could disagree
        about what one release was worth."""
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        service.book.build(["USDJPY", "EURUSD"])
        when = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        r = service.set_events({"pair": "USDJPY", "events": [
            {"when": when.strftime("%Y-%m-%dT%H:%M"), "weights": {"USD": 1.5, "JPY": 0.3},
             "adjust": 0.2, "label": "FOMC"}]})
        self.assertTrue(any("EURUSD" in n for n in r["notes"]), r["notes"])
        eu = next(e for e in service.curve({"pair": "EURUSD"})["events"]
                  if e["label"] == "FOMC")
        self.assertAlmostEqual(eu["bump"], 1.5)     # the dollar leg, no adjustment
        self.assertAlmostEqual(eu["adjust"], 0.0)

    def test_reload_gives_back_the_workbooks_own_rows(self):
        """The Reload button: the EVENTS sheet as the file has it, after a
        session has marked over it.  It reads and applies nothing."""
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        before = service.workbook_events({"pair": "USDJPY"})["events"]
        when = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        service.set_events({"pair": "USDJPY", "events": [
            {"when": when.strftime("%Y-%m-%dT%H:%M"), "weights": {"USD": 1.5}, "adjust": 0,
             "label": "NEW"}]})
        self.assertEqual(service.workbook_events({"pair": "USDJPY"})["events"], before)
        self.assertNotEqual(service.curve({"pair": "USDJPY"})["events"], before)

    def test_the_weights_card_is_a_whole_table_and_keeps_every_pair_cell(self):
        """The optional card is the currency side of the sheet, posted whole.
        Applying it re-solves every pair with those currencies and leaves each
        pair's own adjustment column alone.  A bad cell leaves it untouched."""
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        service.book.build(["USDJPY", "EURUSD"])
        when = (ASOF.now + timedelta(days=30)).replace(hour=16, minute=0, second=0, microsecond=0)
        service.set_events({"pair": "USDJPY", "events": [
            {"when": when.strftime("%Y-%m-%dT%H:%M"), "weights": {"USD": 1.5}, "adjust": 0.4,
             "label": "X"}]})
        table = service.event_weights()
        self.assertIn("USD", table["currencies"])
        row = next(e for e in table["events"] if e["label"] == "X")
        self.assertAlmostEqual(row["weights"]["USD"], 1.5)
        self.assertIn("USDJPY", row["pairs"])

        rows = [{"when": e["when"] + "Z", "label": e["label"],
                 "weights": dict(e["weights"])} for e in table["events"]]
        next(r for r in rows if r["label"] == "X")["weights"]["USD"] = 2.0
        r = service.set_event_weights({"weights": rows})
        self.assertIn("USD", r["currencies"])
        # USDJPY's own adjustment survived the currency-side replacement.
        uj = next(e for e in service.curve({"pair": "USDJPY"})["events"] if e["label"] == "X")
        self.assertAlmostEqual(uj["adjust"], 0.4)
        self.assertAlmostEqual(uj["bump"], 2.4)
        # And EURUSD, which has no cell of its own, moved with the weight.
        eu = next(e for e in service.curve({"pair": "EURUSD"})["events"] if e["label"] == "X")
        self.assertAlmostEqual(eu["bump"], 2.0)

        next(r for r in rows if r["label"] == "X")["weights"]["USD"] = "much"
        with self.assertRaises(ValueError):
            service.set_event_weights({"weights": rows})
        uj = next(e for e in service.curve({"pair": "USDJPY"})["events"] if e["label"] == "X")
        self.assertAlmostEqual(uj["bump"], 2.4)

    def test_events_can_be_set_and_reprice_their_bump(self):
        atm = self.book["USDJPY"].atm
        base = Book.from_excel(BOOK, ASOF).build(["USDJPY"])["USDJPY"].atm
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

    def test_a_block_of_atm_overwrites_is_written_as_one_edit(self):
        """What a paste into the overwrite column posts.

        The column used to take no block at all -- the browser dropped the
        whole clipboard into one box, the line breaks stripped, and
        ``parseFloat`` read ``8.28.3`` out of two tenors and wrote 8.28 to one
        of them.  A silently wrong mark from a paste nobody was told had
        failed, which is the anti-pattern this project exists to remove.
        """
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        out = service.overwrite({"pair": "USDJPY", "kind": "atm_block", "cells": [
            {"tenor": "1M", "value": 9.5}, {"tenor": "3M", "value": 9.75}]})
        self.assertEqual(out["problems"], [])
        rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDJPY"})["atm"]}
        self.assertAlmostEqual(rows["1M"]["overwrite"] * 100, 9.5)
        self.assertAlmostEqual(rows["3M"]["overwrite"] * 100, 9.75)
        self.assertAlmostEqual(rows["1M"]["marked"], 9.5)
        # A blank cell of the block is that tenor given back to the curve --
        # the same thing emptying its box does.
        service.overwrite({"pair": "USDJPY", "kind": "atm_block",
                           "cells": [{"tenor": "1M", "value": None}]})
        again = {r["tenor"].upper(): r
                 for r in service.marks({"pair": "USDJPY"})["atm"]}["1M"]
        self.assertIsNone(again["overwrite"])
        self.assertAlmostEqual(again["marked"], again["curve"], places=9)

    def test_a_block_of_atm_overwrites_that_is_refused_writes_none_of_it(self):
        """Half a pasted column is not a column anybody typed.

        Two ways in: a cell that is not a number, and a tenor the ATM table
        does not have.  The second is the one only a block can produce -- a
        box is typed into on a row that exists, but a pasted label is whatever
        the spreadsheet called it, and an overwrite at a tenor the curve has
        no pillar for would be read by nobody.
        """
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        service.overwrite({"pair": "USDJPY", "kind": "atm", "tenor": "1M", "value": 9.5})
        for cells, says in (
                ([{"tenor": "3M", "value": 9.75}, {"tenor": "3M", "value": "much"}],
                 "much"),
                ([{"tenor": "3M", "value": 9.75}, {"tenor": "4M", "value": 9.8}],
                 "4M is not a tenor")):
            with self.assertRaises(ValueError) as cm:
                service.overwrite({"pair": "USDJPY", "kind": "atm_block", "cells": cells})
            self.assertIn("nothing was written", str(cm.exception))
            self.assertIn(says, str(cm.exception))
            rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDJPY"})["atm"]}
            # The good cell of the refused block is gone and the mark that was
            # there before it is still there.
            self.assertIsNone(rows["3M"]["overwrite"])
            self.assertAlmostEqual(rows["1M"]["overwrite"] * 100, 9.5)
        with self.assertRaises(ValueError):
            service.overwrite({"pair": "USDJPY", "kind": "atm_block", "cells": []})


class TestSmileTermStructureMarks(unittest.TestCase):
    """The three coefficients behind each smile parameter, marked by hand.

    Every smile parameter already had a term structure -- ``final - (final -
    initial) * exp(-decay t)``, fitted across the quoted tenors -- but it was
    computed, shipped on ``/api/term``, and shown nowhere, so the only handles
    on a wing's *shape* across expiries were a per-tenor overwrite and the
    market maker's curve-wide shift.  A marked curve is kept beside the fitted
    one rather than written into it, which is what these pin.
    """

    def surface(self):
        return Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])["EURUSD"]

    def test_a_marked_curve_replaces_the_fitted_one_and_clearing_gives_it_back(self):
        s = self.surface()
        t = s.tenor_years("3M")
        before = s.params_at(t)
        self.assertEqual(s.set_param_term("rho10", -0.2, -0.05, 1.5), [])
        after = s.params_at(t)
        self.assertAlmostEqual(after["rho10"], -0.05 - (-0.05 - -0.2) * math.exp(-1.5 * t))
        # ...and only that parameter moved.
        for name in ("slog10", "slog25", "rho25"):
            self.assertAlmostEqual(after[name], before[name], places=12)
        s.clear_param_terms("rho10")
        self.assertAlmostEqual(s.params_at(t)["rho10"], before["rho10"], places=12)

    def test_a_refit_does_not_discard_a_marked_curve(self):
        """Why ``term_marks`` is a second dict and not an assignment into
        ``term``: the cross triangle recalibrates a leg in place, and a
        workbook export recalibrates every pair, so a mark written into the
        fitted dict would vanish somewhere no screen would ever show.
        """
        s = self.surface()
        s.set_param_term("rho10", -0.2, -0.05, 1.5)
        t = s.tenor_years("3M")
        marked = s.params_at(t)["rho10"]
        s.calibrate()
        self.assertAlmostEqual(s.params_at(t)["rho10"], marked, places=12)
        self.assertIsNotNone(s.term.get("rho10"), "the fit is still underneath it")

    def test_a_bad_coefficient_is_refused_whole(self):
        """Half a curve is not a curve.  A rejected set leaves the parameter
        exactly as it was, and the message quotes the number that was typed.
        """
        s = self.surface()
        t = s.tenor_years("3M")
        before = s.params_at(t)["rho10"]
        for args, wanted in ((("rho10", -1.0, -0.05, 1.5), "strictly inside"),
                             (("rho10", -0.2, -0.05, -1.0), "must not be negative"),
                             (("slog25", 0.0, 0.6, 1.0), "must be positive"),
                             (("slog25", float("nan"), 0.6, 1.0), "finite")):
            problems = s.set_param_term(*args)
            self.assertTrue(problems, args)
            self.assertIn(wanted, problems[0])
            self.assertEqual(s.term_marks, {}, args)
        self.assertAlmostEqual(s.params_at(t)["rho10"], before, places=12)

    def test_the_shift_and_the_marked_curve_compose(self):
        """A wing shift is additive on whatever curve is in force, so it has
        to read the marked one -- a shift measured against a curve the surface
        is no longer using is a shift the market maker cannot reason about.
        """
        s = self.surface()
        t = s.tenor_years("3M")
        s.set_param_term("rho10", -0.2, -0.05, 1.5)
        plain = s.params_at(t)["rho10"]
        s.set_param_shifts({"rho10": 0.03})
        self.assertAlmostEqual(s.params_at(t)["rho10"], plain + 0.03, places=12)
        # And a shift that would push the marked curve out of the domain is
        # reported rather than silently clamped.
        s.set_param_shifts({"rho10": 1.5})
        self.assertTrue([w for w in s.shift_warnings() if "rho10" in w])

    def test_the_route_marks_a_curve_and_the_grid_reports_it(self):
        from volkit.webapp import BookService
        svc = BookService(str(BOOK), clock=ASOF)
        svc.reload()
        rows = {r["param"]: r for r in svc.marks({"pair": "EURUSD", "cut": "NY"})["term"]}
        self.assertEqual(sorted(rows), ["rho10", "rho25", "slog10", "slog25"])
        self.assertIsNone(rows["rho10"]["marked"])
        self.assertIn("decay", rows["rho10"]["fitted"])
        svc.overwrite({"pair": "EURUSD", "kind": "smile_term", "param": "rho10",
                       "initial": "-0.2", "final": "-0.05", "decay": "1.5"})
        rows = {r["param"]: r for r in svc.marks({"pair": "EURUSD", "cut": "NY"})["term"]}
        self.assertEqual(rows["rho10"]["marked"],
                         {"initial": -0.2, "final": -0.05, "decay": 1.5})
        # An empty box is named rather than read as a zero.
        with self.assertRaises(ValueError) as caught:
            svc.overwrite({"pair": "EURUSD", "kind": "smile_term", "param": "rho10",
                           "initial": "", "final": "-0.05", "decay": "1.5"})
        self.assertIn("initial", str(caught.exception))
        svc.overwrite({"pair": "EURUSD", "kind": "clear_smile_term"})
        self.assertTrue(all(r["marked"] is None
                            for r in svc.marks({"pair": "EURUSD", "cut": "NY"})["term"]))

    def test_the_field_is_the_surface_read_across_expiry_and_wing(self):
        """The contour panel's grid: the surface's own columns, and a straight
        line between the two wings it was calibrated at.
        """
        from volkit.webapp import BookService
        svc = BookService(str(BOOK), clock=ASOF)
        g = svc.param_grid({"pair": "EURUSD", "points": 41, "deltas": 9})
        self.assertEqual(g["families"], ["rho", "slog"])
        self.assertEqual(len(g["t"]), 41)
        self.assertEqual(len(g["deltas"]), 9)
        self.assertAlmostEqual(g["deltas"][0], 0.10)
        self.assertAlmostEqual(g["deltas"][-1], 0.25)
        # The expiry axis runs between the quoted tenors, evenly in sqrt(t) --
        # the axis a term structure is read on.
        fits = svc.book["EURUSD"].fits
        self.assertAlmostEqual(g["t"][0], fits[0].t, places=12)
        self.assertAlmostEqual(g["t"][-1], fits[-1].t, places=12)
        roots = [math.sqrt(t) for t in g["t"]]
        steps = [b - a for a, b in zip(roots, roots[1:])]
        self.assertAlmostEqual(max(steps), min(steps), places=12)
        self.assertEqual([x["tenor"] for x in g["tenors"]],
                         [f.tenor for f in fits])
        for family in ("rho", "slog"):
            m = g["maps"][family]
            self.assertEqual(len(m["z"]), 9)
            self.assertEqual(len(m["z"][0]), 41)
            # The two edge rows are the surface's own numbers at every column,
            # and the middle row is halfway between them -- which is all the
            # band between them claims to be.
            for i, t in enumerate(g["t"]):
                at = svc.book["EURUSD"].params_at(t)
                self.assertAlmostEqual(m["z"][0][i], at[f"{family}10"], places=12)
                self.assertAlmostEqual(m["z"][-1][i], at[f"{family}25"], places=12)
                self.assertAlmostEqual(m["z"][4][i],
                                       0.5 * (at[f"{family}10"] + at[f"{family}25"]),
                                       places=12)
        # rho is signed and reads about zero; slog is a positive magnitude.
        self.assertTrue(g["maps"]["rho"]["diverging"])
        self.assertFalse(g["maps"]["slog"]["diverging"])
        self.assertIn("nowhere in between", g["interpolated"])

    def test_the_field_moves_when_the_surface_does(self):
        """The map cannot show a curve the surface is not on."""
        from volkit.webapp import BookService
        svc = BookService(str(BOOK), clock=ASOF)
        before = svc.param_grid({"pair": "EURUSD", "points": 21, "deltas": 5})
        svc.overwrite({"pair": "EURUSD", "kind": "smile_term", "param": "rho10",
                       "initial": "-0.2", "final": "-0.05", "decay": "1.5"})
        after = svc.param_grid({"pair": "EURUSD", "points": 21, "deltas": 5})
        self.assertNotEqual(after["maps"]["rho"]["z"][0], before["maps"]["rho"]["z"][0])
        for i, t in enumerate(after["t"]):
            self.assertAlmostEqual(after["maps"]["rho"]["z"][0][i],
                                   -0.05 - (-0.05 - -0.2) * math.exp(-1.5 * t), places=12)
        # The 25-delta row and the other family are where they were: a marked
        # curve is one parameter and the map must not smear it across four.
        # (Marking re-reads the book, so the untouched rows come back through
        # the fit again and agree to the last few bits rather than exactly.)
        for a, b in zip(after["maps"]["rho"]["z"][-1], before["maps"]["rho"]["z"][-1]):
            self.assertAlmostEqual(a, b, places=12)
        for ra, rb in zip(after["maps"]["slog"]["z"], before["maps"]["slog"]["z"]):
            for a, b in zip(ra, rb):
                self.assertAlmostEqual(a, b, places=12)
        self.assertTrue(any("anchor" in n for n in after["notes"]))
        svc.overwrite({"pair": "EURUSD", "kind": "clear_smile_term"})

    def test_a_pair_with_nothing_fitted_is_refused_by_name(self):
        from volkit.webapp import BookService
        svc = BookService(str(BOOK), clock=ASOF)
        surface = svc.book["EURUSD"]
        surface.fits = []
        with self.assertRaises(ValueError) as caught:
            svc.param_grid({"pair": "EURUSD"})
        self.assertIn("no fitted tenors", str(caught.exception))

    def test_the_field_route_belongs_to_the_marking_screen(self):
        from volkit import screens
        owner = {r: sc.name for sc in screens.SCREENS for r in sc.routes}
        self.assertEqual(owner["/api/params/grid"], "marking")
        # And the page reaches it from the smile card, which is the card the
        # parameters are on.
        html = _source("volkit", "web", "index.html")
        self.assertIn("/api/params/grid", html)
        self.assertIn('id="mctropen"', html)

    def test_the_workbook_row_grammar_reads_what_the_export_writes(self):
        from volkit.marketdata import overlay_label
        self.assertEqual(overlay_label("term rho10 decay"), ("term", "rho10", "decay"))
        self.assertEqual(overlay_label("TERM  Rho25  Initial"), ("term", "rho25", "initial"))
        # A row the tool would not read must not sit there looking read.
        for bad in ("term rho10", "term rho10 slope", "term bogus final"):
            self.assertIsNone(overlay_label(bad), bad)


class TestQuoteMarking(unittest.TestCase):
    """Typing a quote over the one the pair's sheet holds.

    The sheet is where quotes come from, not where they have to come from:
    the marking screen edits the four numbers a tenor is fitted from, the
    session carries them, and writing the session back puts them in the
    sheet's own cell.  What is pinned here is the layering -- ``marks`` stays
    the workbook's, the edit sits beside it, and the fit uses the two
    together -- because an edit written into ``marks`` would be silently lost
    the next time the book handed the surface its quotes.
    """

    def surface(self):
        return Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])["USDJPY"]

    def test_a_typed_quote_is_fitted_and_the_sheets_number_is_kept(self):
        s = self.surface()
        was = {m.tenor.upper(): m.rr_25 for m in s.marks}["3M"]
        before = [f.rho25 for f in s.fits]
        s.overwrite_quote("3M", "rr_25", -0.009)
        s.calibrate()
        self.assertNotEqual(before, [f.rho25 for f in s.fits])
        # The sheet's own quote is untouched, so clearing the box gives it back
        # without a reload -- and the screen can show it as the placeholder.
        self.assertEqual({m.tenor.upper(): m.rr_25 for m in s.marks}["3M"], was)
        row = {r["tenor"]: r for r in s.quote_rows()}["3M"]
        self.assertAlmostEqual(row["rr_25"], -0.009)
        self.assertAlmostEqual(row["rr_25_sheet"], was)
        self.assertTrue(row["marked"])
        s.clear_quote_overwrite("3M", "rr_25")
        s.calibrate()
        self.assertEqual(before, [f.rho25 for f in s.fits])
        self.assertFalse({r["tenor"]: r for r in s.quote_rows()}["3M"]["marked"])

    def test_a_quote_typed_back_onto_the_sheets_own_number_is_not_a_mark(self):
        """A dot that says "changed" on a row nobody changed is noise, and
        the screen reads ``marked`` rather than "is there an entry"."""
        s = self.surface()
        was = {m.tenor.upper(): m.rr_25 for m in s.marks}["3M"]
        s.overwrite_quote("3M", "rr_25", was)
        self.assertFalse({r["tenor"]: r for r in s.quote_rows()}["3M"]["marked"])

    def test_a_tenor_the_sheet_does_not_quote_needs_all_four(self):
        """With no smile to read the missing three off, half a smile is not one.

        The pair is deliberately taken *before* it is calibrated: once there
        is a fit, a CONFIG tenor the sheet does not quote carries four implied
        numbers and typing one materialises the rest
        (``test_typing_into_an_implied_row_marks_the_whole_row``).  This is the
        other case -- a pair with nothing fitted yet -- and it is the one that
        still has to refuse.
        """
        s = Book.from_excel(BOOK, ASOF).build(["USDCNH"])["USDCNH"]
        self.assertEqual(s.fits, [])
        self.assertNotIn("3W", {m.tenor.upper() for m in s.marks})
        s.overwrite_quote("3W", "rr_25", 0.005)
        s.warnings.clear()
        s.calibrate()
        self.assertNotIn("3W", {f.tenor.upper() for f in s.fits})
        self.assertTrue(any("all four" in w for w in s.warnings), s.warnings)
        # The row still exists on the screen that is creating it, saying what
        # it is: quoted by nobody, fitted by nothing.
        row = {r["tenor"]: r for r in s.quote_rows()}["3W"]
        self.assertFalse(row["quoted"])
        self.assertFalse(row["fitted"])
        for name, v in (("st_25", 0.002), ("rr_10", 0.009), ("st_10", 0.0065)):
            s.overwrite_quote("3W", name, v)
        s.calibrate()
        self.assertIn("3W", {f.tenor.upper() for f in s.fits})
        self.assertTrue({r["tenor"]: r for r in s.quote_rows()}["3W"]["fitted"])

    def test_a_strangle_is_refused_where_the_reader_would_refuse_it(self):
        """The same two checks the workbook reader makes on the cell this
        replaces, so a bad number is caught in the box it was typed in and
        not as a convergence failure three calls later."""
        s = self.surface()
        with self.assertRaises(ValueError) as cm:
            s.overwrite_quote("1M", "st_25", -0.01)
        self.assertIn("positive", str(cm.exception))
        with self.assertRaises(ValueError):
            s.overwrite_quote("1M", "fly_25", 0.01)

    def test_the_screen_shows_the_quotes_on_the_tenor_row(self):
        from volkit.webapp import BookService
        from volkit.surface import QUOTE_FIELDS
        service = BookService(str(BOOK), ASOF)
        rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDJPY"})["atm"]}
        self.assertEqual(sorted(rows["3M"]["quotes"]), sorted(QUOTE_FIELDS))
        self.assertTrue(rows["3M"]["quoted"])
        # In points, like every volatility the screen shows.
        self.assertAlmostEqual(rows["3M"]["quotes"]["rr_25"],
                               rows["3M"]["quotes_sheet"]["rr_25"])
        self.assertGreater(abs(rows["3M"]["quotes"]["rr_25"]), 0.05)
        out = service.overwrite({"pair": "USDJPY", "kind": "quote", "tenor": "3M",
                                 "field": "rr_25", "value": -0.9})
        self.assertEqual(out["problems"], [])
        again = {r["tenor"].upper(): r
                 for r in service.marks({"pair": "USDJPY"})["atm"]}["3M"]
        self.assertAlmostEqual(again["quotes"]["rr_25"], -0.9)
        self.assertTrue(again["quotes_marked"])
        # The table is CONFIG's list, so a tenor CONFIG does not name cannot
        # be typed into at all: the mark would be one nothing then shows.
        with self.assertRaises(ValueError) as cm:
            service.overwrite({"pair": "USDJPY", "kind": "quote", "tenor": "4M",
                               "field": "rr_25", "value": -0.5})
        self.assertIn("TENORS", str(cm.exception))
        self.assertNotIn("4M", {r["tenor"].upper()
                                for r in service.marks({"pair": "USDJPY"})["atm"]})

    def test_a_block_of_quotes_is_written_and_fitted_as_one_edit(self):
        """What a paste out of a spreadsheet posts.

        One request rather than the single-quote route in a loop: the screen
        can hand over four columns and a dozen tenors at once, and a refit per
        cell would fit the pair fifty times to answer one paste.
        """
        from volkit.webapp import BookService
        from volkit.surface import QUOTE_FIELDS
        service = BookService(str(BOOK), ASOF)
        block = [{"tenor": t, "field": f, "value": v}
                 for t, vals in (("3M", (-0.80, -1.40, 0.22, 0.75)),
                                 ("6M", (-0.85, -1.50, 0.24, 0.80)))
                 for f, v in zip(QUOTE_FIELDS, vals)]
        out = service.overwrite({"pair": "USDJPY", "kind": "quotes", "cells": block})
        self.assertEqual(out["problems"], [])
        rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDJPY"})["atm"]}
        for tenor, vals in (("3M", (-0.80, -1.40, 0.22, 0.75)),
                            ("6M", (-0.85, -1.50, 0.24, 0.80))):
            self.assertTrue(rows[tenor]["quotes_marked"], tenor)
            self.assertTrue(rows[tenor]["fitted"], tenor)
            for f, v in zip(QUOTE_FIELDS, vals):
                self.assertAlmostEqual(rows[tenor]["quotes"][f], v, places=6)
        # A blank cell of the block is that quote given back to the sheet.
        service.overwrite({"pair": "USDJPY", "kind": "quotes",
                           "cells": [{"tenor": "3M", "field": "rr_25", "value": None}]})
        again = {r["tenor"].upper(): r
                 for r in service.marks({"pair": "USDJPY"})["atm"]}["3M"]
        self.assertAlmostEqual(again["quotes"]["rr_25"], again["quotes_sheet"]["rr_25"])

    def test_a_block_of_quotes_that_is_refused_writes_none_of_it(self):
        """Half a pasted table is not a table anybody typed.

        The single-quote route refuses a negative strangle in the box it was
        typed into, where there is nothing else to undo.  A block refused
        halfway would leave the pair holding the rows before the bad cell and
        no way back to what was there, so the whole block is put back.
        """
        from volkit.webapp import BookService
        service = BookService(str(BOOK), ASOF)
        before = {r["tenor"].upper(): dict(r["quotes"])
                  for r in service.marks({"pair": "USDJPY"})["atm"]}
        with self.assertRaises(ValueError) as cm:
            service.overwrite({"pair": "USDJPY", "kind": "quotes", "cells": [
                {"tenor": "1M", "field": "rr_25", "value": -0.50},
                {"tenor": "1M", "field": "st_25", "value": -0.20}]})
        self.assertIn("nothing was written", str(cm.exception))
        self.assertIn("positive", str(cm.exception))
        rows = {r["tenor"].upper(): r for r in service.marks({"pair": "USDJPY"})["atm"]}
        self.assertFalse(rows["1M"]["quotes_marked"])
        self.assertEqual({t: dict(r["quotes"]) for t, r in rows.items()}, before)

    def test_a_session_carries_the_quotes_in_points_and_refits_on_the_way_back(self):
        from volkit import session
        book = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        s = book["USDJPY"]
        s.overwrite_quote("3M", "rr_25", -0.009)
        s.calibrate()
        block = session.capture(book, ["USDJPY"])["pairs"]["USDJPY"]
        self.assertAlmostEqual(block["quote_overwrites"]["3M"]["rr_25"], -0.9)
        fresh = Book.from_excel(BOOK, ASOF).load_all(["USDJPY"])
        out = session.apply_document(fresh, session.capture(book, ["USDJPY"]))
        self.assertEqual(out["problems"], [])
        # Restored *and refitted*: a quote is an input to the fit, so a
        # surface that took one back without refitting would hold the typed
        # number and the old smile at the same time.
        self.assertEqual([f.rho25 for f in s.fits], [f.rho25 for f in fresh["USDJPY"].fits])


class TestWingRatios(unittest.TestCase):
    """The 10-delta wings as a multiple of the 25-delta ones.

    They were formulas in the pair sheets -- ``ST 10D = ST 25D * 3.25`` -- so
    the tool could not see them and a quote written beside one left the wing
    holding a number computed from the cell that had just been replaced.  The
    multiples are data now.  What is pinned is that the derivation is the last
    word on a wing it governs, that typing that wing takes it off the ratio
    rather than fighting it, and that the migration off the formulas does not
    move a single mark.

    One of the few classes that must read ``WORKBOOK`` rather than the fixture:
    the thing being migrated *is* the spreadsheet's array formulas, and
    openpyxl cannot write a formula with its cached value, so a rebuilt
    workbook has the numbers and none of the formulas -- which is a migration
    with nothing to find.
    """

    def workbook(self, migrate=True) -> Path:
        import shutil
        import tempfile
        from volkit import session
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        wb = d / "vol_marks.xlsx"
        shutil.copy(WORKBOOK, wb)
        if migrate:
            session.migrate_wing_ratios(wb, in_place=True)
        return wb

    def test_the_migration_reads_every_multiple_and_moves_no_mark(self):
        from volkit import session
        from volkit.surface import load_wing_ratios
        wb = self.workbook(migrate=False)
        out = session.migrate_wing_ratios(wb, in_place=True)
        self.assertEqual(out["problems"], [])
        self.assertGreater(out["ratios"], 100)
        ratios = load_wing_ratios(wb)
        # Per pair *and* per tenor: one number for the pair would have been a
        # different workbook from the one the desk has.
        self.assertAlmostEqual(ratios["USDJPY"]["3M"].rr, 1.85)
        self.assertAlmostEqual(ratios["USDJPY"]["1Y"].rr, 1.875)
        self.assertAlmostEqual(ratios["USDHKD"]["3M"].st, 3.4)

        pairs = ["USDJPY", "EURUSD", "USDHKD", "EURJPY"]
        was = Book.from_excel(WORKBOOK, ASOF).load_all(pairs)
        now = Book.from_excel(wb, ASOF).load_all(pairs)
        self.assertEqual(now.data.problems, [])
        for pair in pairs:
            for t in (0.02, 0.25, 1.0, 2.0):
                expiry = ASOF.datetime_from_years(t)
                for k in (0.9, 1.0, 1.1):
                    self.assertAlmostEqual(float(was[pair].vol(k, expiry)),
                                           float(now[pair].vol(k, expiry)), places=8,
                                           msg=(pair, t, k))

    def test_a_quoted_25_delta_moves_the_wing_it_derives(self):
        """The whole point. Before the ratios were data, writing the 25-delta
        left the 10-delta at a value taken from the number just replaced."""
        s = Book.from_excel(self.workbook(), ASOF).load_all(["USDJPY"])["USDJPY"]
        before = {m.tenor.upper(): m for m in s.quoted_marks()}["3M"]
        self.assertAlmostEqual(before.rr_10 / before.rr_25, 1.85)
        s.overwrite_quote("3M", "rr_25", -0.009)
        after = {m.tenor.upper(): m for m in s.quoted_marks()}["3M"]
        self.assertAlmostEqual(after.rr_25, -0.009)
        self.assertAlmostEqual(after.rr_10, -0.009 * 1.85)

    def test_typing_the_wing_takes_it_off_the_ratio_and_clearing_puts_it_back(self):
        """Otherwise the ratio, applied last, wins over the box just typed in
        and the number goes back to what it was on the way out of the field."""
        s = Book.from_excel(self.workbook(), ASOF).load_all(["USDJPY"])["USDJPY"]
        s.overwrite_quote("3M", "rr_10", -0.02)
        self.assertIsNone(s.effective_ratio("3M").rr)
        self.assertAlmostEqual({m.tenor.upper(): m for m in s.quoted_marks()}["3M"].rr_10,
                               -0.02)
        # ...and the strangle beside it is untouched: one wing, one decision.
        self.assertAlmostEqual(s.effective_ratio("3M").st, 3.0)
        s.clear_quote_overwrite("3M", "rr_10")
        self.assertAlmostEqual(s.effective_ratio("3M").rr, 1.85)

    def test_a_ratio_and_the_quotes_it_derives_survive_the_session(self):
        from volkit import session
        wb = self.workbook()
        book = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        s = book["USDJPY"]
        s.overwrite_ratio("3M", "st", 4.0)
        s.overwrite_quote("6M", "rr_10", -0.02)      # takes 6M off its rr ratio
        s.calibrate()
        block = session.capture(book, ["USDJPY"])["pairs"]["USDJPY"]
        self.assertAlmostEqual(block["wing_ratios"]["3M"]["st"], 4.0)
        # A wing taken off its ratio is written as a null and *kept*: "no
        # multiple here" and "no opinion" are different answers.
        self.assertIsNone(block["wing_ratios"]["6M"]["rr"])
        fresh = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        out = session.apply_document(fresh, session.capture(book, ["USDJPY"]))
        self.assertEqual(out["problems"], [])
        self.assertAlmostEqual(fresh["USDJPY"].effective_ratio("3M").st, 4.0)
        self.assertIsNone(fresh["USDJPY"].effective_ratio("6M").rr)
        self.assertEqual([f.rho25 for f in s.fits], [f.rho25 for f in fresh["USDJPY"].fits])

    def test_writing_a_quote_leaves_the_sheet_all_numbers_and_consistent(self):
        """A sheet half formula and half number is a workbook that changes
        itself the next time Excel opens it."""
        from volkit import session
        wb = self.workbook(migrate=False)
        book = Book.from_excel(wb, ASOF).load_all(["USDJPY"])
        book["USDJPY"].overwrite_quote("3M", "rr_25", -0.009)
        book["USDJPY"].calibrate()
        session.export_workbook(session.capture(book, ["USDJPY"]), wb, in_place=True)
        marks = {m.tenor.upper(): m
                 for m in Book.from_excel(wb, ASOF).load_all(["USDJPY"])["USDJPY"].marks}
        self.assertAlmostEqual(marks["3M"].rr_25, -0.009)
        import openpyxl
        ws = openpyxl.load_workbook(wb)["USDJPY"]
        self.assertFalse([c.coordinate for row in ws.iter_rows() for c in row
                          if session._is_formula(c.value)])

    def test_a_new_tenor_needs_only_the_25_delta_where_a_ratio_derives_the_wing(self):
        """USDCNH's 3W: CONFIG lists it and the sheet is quoted 2W then 1M.

        A tenor CONFIG does not list cannot be marked at all now
        (``TestConfigTenorsGovern``), so a "new tenor" is one of these.
        """
        s = Book.from_excel(self.workbook(), ASOF).load_all(["USDCNH"])["USDCNH"]
        s.overwrite_ratio("3W", "st", 3.0)
        s.overwrite_ratio("3W", "rr", 1.85)
        s.overwrite_quote("3W", "st_25", 0.0025)
        s.overwrite_quote("3W", "rr_25", 0.006)
        # The two wings the ratios govern are never seeded off the fitted
        # smile: the ratio is the last word on them.
        self.assertEqual(set(s.quote_overwrites["3W"]), {"st_25", "rr_25"})
        s.warnings.clear()
        s.calibrate()
        self.assertIn("3W", {f.tenor.upper() for f in s.fits})
        self.assertEqual(s.warnings, [])


class TestHeldFitGoesStale(unittest.TestCase):
    """Held marks are only good for the book they came from.

    The fit, the check and the quote are three routes and the marks travel
    between them in the browser (§4), which means a trip to the marking screen
    can happen in the middle.  ``applied_marks`` then put the fit's backbone knobs and smile
    shifts back over whatever was marked there -- silently, and only over
    *those two*, so a re-marked curve was thrown away while a pinned tenor or
    a re-quoted wing went through.  A price half of this morning's marks and
    half of a fit of the curve they replaced is a wrong answer that reads
    perfectly well, which is the failure this class pins shut.
    """

    def service(self):
        from volkit.webapp import BookService
        return BookService(str(BOOK), ASOF)

    def fit(self, svc, pair="USDJPY", **kw):
        payload = {"pair": pair, "cut": "TK", "target_source": "current",
                   "free": ["initial_vol", "long_term_vol"],
                   "fit_curve": True, "tune_wings": False, "apply": False}
        payload.update(kw)
        return svc.mm_mark_fit(payload)

    def check(self, svc, marks=None, pair="USDJPY"):
        payload = {"cut": "TK", "text": f"{pair} 1M ATM 8.0/8.6\n"}
        if marks is not None:
            payload["marks"] = marks
        return svc.mm_check(payload)

    def quote(self, svc, marks=None, pair="USDJPY"):
        payload = {"request_text": f"{pair} 1M atm", "cut": "TK",
                   "fallback_tier": "default"}
        if marks is not None:
            payload["marks"] = marks
        return svc.mm_quote(payload)

    def remark_curve(self, svc, pair="USDJPY", by=2.0):
        params = dict(svc.curve({"pair": pair})["params"])
        params["initial_vol"] += by
        params["long_term_vol"] += by
        out = svc.set_curve({"pair": pair, "params": params})
        self.assertTrue(out["ok"], out["problems"])

    def test_a_fit_is_stamped_with_the_book_it_was_fitted_on(self):
        svc = self.service()
        marks = self.fit(svc)["marks"]
        self.assertIn("book", marks)
        # Every part of the pair's marked state, hashed on its own, so what
        # moved can be named rather than only that something did.
        self.assertIn("curve", marks["book"])
        self.assertIn("atm_overwrites", marks["book"])
        self.assertIn("quote_overwrites", marks["book"])

    def test_a_held_fit_prices_on_the_book_it_was_made_on(self):
        svc = self.service()
        marks = self.fit(svc)["marks"]
        out = self.quote(svc, marks)
        self.assertTrue(out["marks"]["on_the_marks"])
        self.assertEqual(out["marks"]["stale"], [])

    def test_a_curve_re_marked_after_the_fit_drops_the_fit_and_says_so(self):
        svc = self.service()
        marks = self.fit(svc)["marks"]
        was = self.quote(svc, marks)["sheet"]["rows"][0]["model"]
        self.remark_curve(svc)
        # The book alone, for comparison: this is the number the desk marked.
        book_only = self.quote(svc)["sheet"]["rows"][0]["model"]
        self.assertNotAlmostEqual(book_only, was, places=6)
        out = self.quote(svc, marks)
        self.assertFalse(out["marks"]["on_the_marks"])
        # The sheet's own line names the pair, because a sheet may hold
        # several and only one of them was fitted; the pair's own block says
        # it plainly.
        self.assertEqual(out["marks"]["stale"], ["USDJPY: the curve parameters"])
        self.assertEqual(out["by_pair"]["USDJPY"]["marks"]["stale"],
                         ["the curve parameters"])
        # ...and it is the marked number that is priced, not the fit's.
        self.assertAlmostEqual(out["sheet"]["rows"][0]["model"], book_only, places=12)
        self.assertTrue(any("re-marked since these marks were made" in w
                            for w in out["warnings"]), out["warnings"])

    def test_every_kind_of_re_mark_is_noticed_and_named(self):
        """Not only the two ``capture_marks`` holds.

        A pinned tenor and a re-quoted wing went through ``applied_marks``
        untouched, so the old behaviour was not merely wrong but *selectively*
        wrong -- which is worse, because half a screen agreed with itself.
        """
        for kind, mark, name in (
            ("pin", lambda s: s.overwrite({"pair": "USDJPY", "kind": "atm",
                                           "tenor": "1m", "value": 9.5}),
             "the pinned at-the-money tenors"),
            ("re-quote", lambda s: s.overwrite({"pair": "USDJPY", "kind": "quote",
                                                "tenor": "1M", "field": "rr_25",
                                                "value": -0.9}),
             "the re-quoted wings"),
            ("curve", lambda s: self.remark_curve(s), "the curve parameters"),
        ):
            with self.subTest(kind):
                svc = self.service()
                marks = self.fit(svc)["marks"]
                mark(svc)
                out = self.quote(svc, marks)
                self.assertEqual(out["by_pair"]["USDJPY"]["marks"]["stale"], [name])
                self.assertFalse(out["marks"]["on_the_marks"])

    def test_a_fit_that_kept_its_marks_is_not_stale_against_its_own_write(self):
        """``keep the marks`` writes the fit onto the book, and the stamp is
        taken after that write -- otherwise the fit would arrive already out
        of date with the book it had just made."""
        svc = self.service()
        marks = self.fit(svc, apply=True)["marks"]
        out = self.quote(svc, marks)
        self.assertTrue(out["marks"]["on_the_marks"])
        self.assertEqual(out["marks"]["stale"], [])

    def test_a_fresh_fit_is_good_again(self):
        svc = self.service()
        stale = self.fit(svc)["marks"]
        self.remark_curve(svc)
        self.assertTrue(self.quote(svc, stale)["marks"]["stale"])
        again = self.fit(svc)["marks"]
        self.assertEqual(self.quote(svc, again)["marks"]["stale"], [])

    def test_the_check_reads_a_held_set_of_marks_the_same_way_the_quote_does(self):
        """One stamp, one reading of it.  The check used to *be* the fit and had
        no marks to read at all; it reads them now, because checking the book
        while an unanswered proposal sits on the screen would be checking a
        curve nobody is quoting off -- and a check that took a stale set at face
        value would be worse than one that took none."""
        svc = self.service()
        marks = self.fit(svc)["marks"]
        fresh = self.check(svc, marks)
        self.assertTrue(fresh["marks"]["on_the_marks"])
        self.assertEqual(fresh["marks"]["stale"], [])
        on_marks = fresh["market"]["rows"][0]["model"]
        self.remark_curve(svc)
        stale = self.check(svc, marks)
        self.assertFalse(stale["marks"]["on_the_marks"])
        self.assertEqual(stale["by_pair"]["USDJPY"]["marks"]["stale"],
                         ["the curve parameters"])
        # The marked number, not the dropped fit's.
        self.assertNotAlmostEqual(stale["market"]["rows"][0]["model"], on_marks, places=6)
        self.assertAlmostEqual(stale["market"]["rows"][0]["model"],
                               self.check(svc)["market"]["rows"][0]["model"], places=12)

    def test_marks_with_no_stamp_are_quoted_off_as_they_always_were(self):
        """A payload from a client that predates the stamp, or a hand-written
        one: refusing on a *missing* field would break every saved panel the
        day it shipped."""
        svc = self.service()
        marks = self.fit(svc)["marks"]
        marks.pop("book")
        self.remark_curve(svc)
        out = self.quote(svc, marks)
        self.assertTrue(out["marks"]["on_the_marks"])
        self.assertEqual(out["marks"]["stale"], [])

    def test_quoting_leaves_the_book_where_it_found_it(self):
        """Whether the marks were used or dropped."""
        svc = self.service()
        marks = self.fit(svc)["marks"]
        self.remark_curve(svc)
        before = marketmaker.mark_fingerprint(svc.book, "USDJPY")
        self.quote(svc, marks)
        self.assertEqual(marketmaker.mark_fingerprint(svc.book, "USDJPY"), before)

    def test_the_fingerprint_moves_only_when_a_mark_does(self):
        svc = self.service()
        was = marketmaker.mark_fingerprint(svc.book, "USDJPY")
        # Reading the book is not marking it.
        svc.marks({"pair": "USDJPY"})
        self.quote(svc)
        self.fit(svc)
        self.assertEqual(marketmaker.mark_fingerprint(svc.book, "USDJPY"), was)
        svc.overwrite({"pair": "USDJPY", "kind": "atm", "tenor": "1m", "value": 9.5})
        self.assertNotEqual(marketmaker.mark_fingerprint(svc.book, "USDJPY"), was)

    def test_the_page_hooks_every_route_that_can_move_the_marks(self):
        """The browser's own half of the sync.

        Every marking route used to end with ``schedulePrice()`` -- the
        *pricing* screen -- so the market-maker tab kept showing the last
        run's numbers however much was re-marked.  The hook is in ``post``
        rather than in each route, and this pins its list against the routes
        the server actually marks on: a route added later that touches
        ``self.dirty`` and is not listed here fails this test rather than
        going quietly out of sync.
        """
        import re as _re
        from volkit import webapp as _webapp
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        listed = set(_re.findall(r"'(/api/[a-z/]+)'",
                                 js.split("const MARKING_ROUTES=[")[1].split("];")[0]))
        self.assertIn("/api/overwrite", listed)
        handler = _inspect.getsource(_webapp.Handler.do_POST) \
            if hasattr(_webapp, "Handler") else \
            _inspect.getsource(_webapp).split("def do_POST")[1]
        routed = _re.findall(r'url\.path == "(/api/[^"]+)":\s*\n\s*(?:self\._json\()?'
                             r'self\.service\.(\w+)\(', handler)
        self.assertTrue(routed)
        # What "moves the marks" is: puts a mark on the loaded book, or
        # replaces the book under it.  Merely *reading* ``dirty`` is not --
        # saving a session, exporting one and asking the marking agent for a
        # proposal all do that and leave the book exactly as they found it.
        # ``_rebuild`` is the third way of replacing it: read the workbook
        # again on a configuration this session holds and put the marks back.
        moves = _re.compile(r"self\.dirty = True|self\.dirty = self\.dirty or"
                            r"|self\.reload\(|self\.book = |self\._rebuild\(")
        checked = 0
        for path, name in routed:
            method = getattr(_webapp.BookService, name, None)
            if method is None or not moves.search(_inspect.getsource(method)):
                continue
            checked += 1
            with self.subTest(path):
                self.assertIn(path, listed,
                              f"{path} can move the marks and the page does not "
                              f"tell the market-maker screen about it")
        self.assertGreater(checked, 8, "the route scan found almost nothing; it has broken")
        # And nothing is listed that cannot move anything: a route that fires
        # the hook for no reason trains a desk to ignore the flag.
        marking = {p for p, n in routed
                   if getattr(_webapp.BookService, n, None) is not None
                   and moves.search(_inspect.getsource(getattr(_webapp.BookService, n)))}
        self.assertEqual(listed - marking, set())
        # And the hook is reached from one place: nothing may post around it.
        self.assertEqual(js.count("method:'POST'"), 1)
        self.assertIn("bookMoved()", js.split("const post=")[1][:400])


class TestCurveComparison(unittest.TestCase):
    """Several curves side by side, and the same curve on other dates."""

    def book(self, pairs=("EURUSD",)):
        return Book.from_excel(BOOK, ASOF).load_all(list(pairs))

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
        # 2W is on the workbook's marks curve and not on the history sheet,
        # which quotes 1W, 1M, 3M, 6M and 1Y.  (It used to be 2Y; the pair
        # sheets still quote one, but CONFIG's TENORS column does not list it
        # so nothing reads it -- see TestConfigTenorsGovern.)
        self.assertIn("2W", r["tenors"])            # in the workbook, not in the sheet
        self.assertIn("1M", r["tenors"])
        hist = r["curves"][1]
        self.assertIsNone(hist_point(hist, "2W"))
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

    def test_a_pasted_curve_is_read_in_points_as_written(self):
        """The level is not evidence of the unit (§4): a managed pair's whole
        curve sits below 1.0, and reading that as decimals put it on the
        monitor at a hundred times its mark."""
        from volkit import curves
        c = curves.parse_pasted_curve("1M 8.20 -0.35 0.22\n3M 8.45")
        self.assertTrue(c.ok)
        self.assertAlmostEqual(c.at("1M").values["atm"], 0.0820)
        self.assertAlmostEqual(c.at("1M").values["rr25"], -0.0035)
        self.assertIsNone(c.at("3M").values["rr25"])
        low = curves.parse_pasted_curve("1M 0.35 -0.02")
        self.assertTrue(low.ok)
        self.assertAlmostEqual(low.at("1M").values["atm"], 0.0035)
        self.assertIn("volatility points", low.source)

    def test_a_pasted_curve_that_straddles_one_is_read_and_not_refused(self):
        from volkit import curves
        c = curves.parse_pasted_curve("1M 8.20\n3M 0.35")
        self.assertTrue(c.ok)
        self.assertAlmostEqual(c.at("1M").values["atm"], 0.0820)
        self.assertAlmostEqual(c.at("3M").values["atm"], 0.0035)
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
        service = BookService(str(BOOK), ASOF, history_path=str(HISTORY))
        payload = {"curves": [{"kind": "surface", "pair": "EURUSD"},
                              {"kind": "history", "pair": "EURUSD", "date": "-60d"}],
                   "cut": "NY", "method": "SVI", "field": "rr25", "base": 0}
        first = service.compare_curves(payload)
        self.assertEqual(first, service.compare_curves(payload))
        self.assertEqual(first["field"], "rr25")


if __name__ == "__main__":
    unittest.main()
