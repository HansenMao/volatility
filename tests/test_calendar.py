"""Dates, calendars, events and settlement conventions.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestEvents(unittest.TestCase):
    def test_calibrated_height_reproduces_the_quoted_bump(self):
        """The bump is quoted over the event's own 24 hours, so that is where
        it must be delivered exactly."""
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        when = datetime(2024, 3, 20, 18, 0, tzinfo=UTC)
        ev = curve.events.add(when, 0.030, "FOMC")
        # 20 Mar 2024 really was Vernal Equinox Day, and the March FOMC really
        # did land on it: the calendar says so, and the bump is still delivered
        self.assertEqual(curve.calibrate_events(),
                         ["FOMC at 2024-03-20 falls on a JP holiday for USDJPY"])
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

    def test_two_legs_weights_add_and_the_adjustment_sits_on_top(self):
        """An event is weighted per currency; a pair's bump is its two legs
        superposed plus the pair's own adjustment.  They add: a bump is a
        variance increment over twice the volatility, so two bumps add to
        first order, and a root-sum-square would be the rule for two event
        volatilities, which a bump is not."""
        from volkit.events import EventEntry, leg_weights, pair_bump, superpose
        self.assertEqual(superpose(0.015, 0.003), 0.018)
        self.assertAlmostEqual(pair_bump({"USD": 0.015, "JPY": 0.003}, "USDJPY", 0.002), 0.020)
        # A leg the table does not name weighs nothing; EURUSD sees only USD.
        self.assertAlmostEqual(pair_bump({"USD": 0.015, "JPY": 0.003}, "EURUSD"), 0.015)
        # CNH and CNY are one market for this purpose, unless CNY was named.
        self.assertEqual(leg_weights({"CNH": 0.02}, "USDCNY"), {"USD": 0.0, "CNY": 0.02})
        self.assertEqual(leg_weights({"CNH": 0.02, "CNY": 0.01}, "USDCNY"), {"USD": 0.0, "CNY": 0.01})
        # An entry typed as one number is that number, carried as the
        # adjustment, so ``bump == legs + adjust`` holds for every event.
        one = EventEntry(datetime(2026, 9, 1, tzinfo=UTC), 0.02, "x").resolve("EURUSD")
        self.assertEqual((one.bump, one.adjust, one.weights), (0.02, 0.02, {"EUR": 0.0, "USD": 0.0}))
        parts = EventEntry(datetime(2026, 9, 1, tzinfo=UTC), None, "x",
                           {"USD": 0.015}, 0.002).resolve("USDJPY")
        self.assertAlmostEqual(parts.bump, 0.017)
        self.assertEqual(parts.weights, {"USD": 0.015, "JPY": 0.0})
        # A total that disagrees with its parts is refused, not averaged.
        with self.assertRaises(ValueError):
            EventEntry(datetime(2026, 9, 1, tzinfo=UTC), 0.05, "x", {"USD": 0.015}, 0.002).resolve("USDJPY")

    def test_weighted_event_calibrates_to_the_superposed_bump(self):
        from volkit.events import EventEntry
        curve = AtmCurve("USDJPY", BackboneParams(0.0605, 0.0765, 5.0, 0.007, 50.0), ASOF,
                         events=EventSchedule())
        when = datetime(2024, 3, 21, 18, 0, tzinfo=UTC)   # the 20th is a JP holiday
        problems = curve.set_events([EventEntry(when, None, "FOMC", {"USD": 0.015, "JPY": 0.003}, 0.002)])
        self.assertEqual(problems, [])
        ev = curve.events.events[0]
        self.assertAlmostEqual(ev.bump, 0.020)
        self.assertAlmostEqual(ev.adjust, 0.002)
        self.assertAlmostEqual(curve.achieved_bump(ev), 0.020, places=9)
        # The old three-tuple spelling still works and reads as an adjustment.
        curve.set_events([(when, 0.01, "T")])
        self.assertEqual(curve.events.events[0].adjust, 0.01)

    def test_event_rows_in_points_are_read_once(self):
        """The panel and the session file post the parts; a row may carry the
        total too when it agrees, and a bad row is named rather than fatal."""
        from volkit.events import event_entries
        rows = [{"when": "2026-09-16T18:00", "weights": {"USD": 1.5, "JPY": 0.3}, "adjust": 0.2,
                 "label": "FOMC"},
                {"when": "2026-09-16T18:00", "weights": {"USD": 1.5}, "adjust": 0.0, "bump": 1.5},
                {"when": "2026-10-01T12:00", "bump": 0.75},
                {"when": "2026-10-01T12:00", "weights": {"USD": 1.5}, "adjust": 0.0, "bump": 2.0},
                {"when": "", "bump": 1.0}]
        entries, problems = event_entries(rows)
        self.assertEqual(len(entries), 3)
        self.assertAlmostEqual(entries[0].resolve("USDJPY").bump, 0.020)
        self.assertAlmostEqual(entries[2].resolve("USDJPY").adjust, 0.0075)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("not the weights" in p for p in problems), problems)
        self.assertTrue(any("no date/time" in p for p in problems), problems)

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


class TestFxDateConventions(unittest.TestCase):
    """The market's own construction: settlement first, expiry back from it.

    Pinned in detail because every one of these was arrived at by getting it
    wrong first, and because a date that is a day out is a whole day of
    volatility and two business days of swap points.
    """

    def setUp(self):
        self.c = CalendarSet()

    def test_the_settlement_date_is_the_anchor_and_the_expiry_comes_from_it(self):
        d = self.c.fx_dates("EURUSD", "1M", date(2026, 9, 1))
        self.assertEqual(d.spot, date(2026, 9, 3))       # T+2
        self.assertEqual(d.delivery, date(2026, 10, 5))  # 3 Oct is a Saturday
        self.assertEqual(d.expiry, date(2026, 10, 1))    # the spot lag back
        # and the two are consistent both ways round
        self.assertEqual(self.c.delivery_from_expiry("EURUSD", d.expiry), d.delivery)
        self.assertEqual(self.c.expiry_from_delivery("EURUSD", d.delivery), d.expiry)

    def test_a_us_holiday_rules_out_a_value_date_but_does_not_stop_the_count(self):
        """The half of the spot convention that is easiest to conflate.

        Counting US holidays as non-business days for a pair with no dollar in
        it would push EURJPY spot out a day every Thanksgiving, which is not
        what the market does.  The date the count lands on must still be one
        USD can settle on, because every FX trade settles through New York.
        """
        thanksgiving = date(2026, 11, 26)
        self.assertTrue(self.c.is_holiday("USD", thanksgiving))
        self.assertFalse(self.c.is_holiday("EURJPY", thanksgiving))
        # EUR and JPY are both open on the 26th, so it is the second of the two
        # counted days -- and is then rolled off because USD is shut.
        self.assertTrue(self.c.is_business_day("EURJPY", thanksgiving))
        self.assertFalse(self.c.is_settlement_day("EURJPY", thanksgiving))
        self.assertEqual(self.c.spot_date("EURJPY", date(2026, 11, 24)),
                         date(2026, 11, 27))

    def test_a_us_holiday_on_t_plus_one_does_not_delay_spot_for_a_dollar_pair(self):
        """The dollar needs one clear US working day before spot, not two.

        19 Jan 2026 is Martin Luther King Day.  EURUSD dealt on Friday the
        16th settles Tuesday the 20th: EUR counts Monday and Tuesday, USD needs
        only one open day and Tuesday is it.  Counting the holiday against the
        pair -- the old construction -- came back Wednesday the 21st, a day
        late for every dollar pair after every Monday US holiday.
        """
        mlk = date(2026, 1, 19)
        self.assertTrue(self.c.is_holiday("USD", mlk))
        self.assertEqual(self.c.spot_date("EURUSD", date(2026, 1, 16)), date(2026, 1, 20))
        self.assertEqual(self.c.spot_date("USDJPY", date(2026, 1, 16)), date(2026, 1, 20))
        # ...while a US holiday on T+2 does delay it, for every pair: dealt
        # Thursday the 15th the count lands on the Monday and rolls off it.
        self.assertEqual(self.c.spot_date("EURUSD", date(2026, 1, 15)), date(2026, 1, 20))
        self.assertEqual(self.c.spot_date("EURJPY", date(2026, 1, 15)), date(2026, 1, 20))

    def test_the_other_currency_s_holiday_on_t_plus_one_does_delay_spot(self):
        """21-23 Sep 2026 is Japan's Silver Week (the 22nd is the sandwiched day)."""
        for day in (21, 22, 23):
            self.assertTrue(self.c.is_holiday("JPY", date(2026, 9, day)), day)
        self.assertEqual(self.c.spot_date("USDJPY", date(2026, 9, 17)), date(2026, 9, 24))
        self.assertEqual(self.c.spot_date("USDJPY", date(2026, 9, 18)), date(2026, 9, 25))
        # and it is the 1W's whole shape: dealt on the 14th it settles on the
        # 24th and can only expire on the 17th, the last day that settles then
        d = self.c.fx_dates("USDJPY", "1W", date(2026, 9, 14))
        self.assertEqual((d.spot, d.expiry, d.delivery),
                         (date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 24)))

    def test_the_expiry_is_the_inverse_spot_and_takes_the_later_of_a_shared_pair(self):
        """Thursday 15 and Friday 16 Jan 2026 both settle on Tuesday the 20th.

        An option settling on the 20th expires on the Friday -- the latest day
        whose spot date is the delivery -- not the Thursday that stepping two
        business days back over the holiday produced.
        """
        self.assertEqual(self.c.expiry_from_delivery("EURUSD", date(2026, 1, 20)),
                         date(2026, 1, 16))
        self.assertEqual(self.c.delivery_from_expiry("EURUSD", date(2026, 1, 16)),
                         date(2026, 1, 20))
        d = self.c.fx_dates("EURUSD", "1M", date(2025, 12, 16))
        self.assertEqual((d.spot, d.delivery, d.expiry),
                         (date(2025, 12, 18), date(2026, 1, 20), date(2026, 1, 16)))

    def test_when_no_expirable_day_settles_on_the_delivery_the_rule_says_so(self):
        """1M from 27 Oct 2026 settles Monday 30 Nov, two days after Thanksgiving.

        Only Thanksgiving itself settles on the 30th, and an option does not
        expire on a pair holiday, so the expiry is the last day that settles
        *by* the 30th -- Wednesday the 25th -- and the delivery stays put.
        """
        d = self.c.fx_dates("EURUSD", "1M", date(2026, 10, 27))
        self.assertEqual(d.delivery, date(2026, 11, 30))
        self.assertEqual(d.expiry, date(2026, 11, 25))
        self.assertEqual(self.c.delivery_from_expiry("EURUSD", d.expiry), date(2026, 11, 27))
        self.assertIn("no expirable day settles exactly on 2026-11-30", d.rule)

    def test_the_fed_does_not_observe_saturday_holidays_but_the_commonwealth_moves_forward(self):
        # 4 July 2026 is a Saturday: federal offices close the Friday, Fedwire
        # does not, so the 3rd is a USD value date.
        self.assertFalse(self.c.is_holiday("USD", date(2026, 7, 3)))
        self.assertEqual(self.c.spot_date("EURUSD", date(2026, 7, 1)), date(2026, 7, 3))
        # Columbus Day and Veterans Day shut Fedwire and so cannot be value dates
        self.assertEqual(self.c.spot_date("EURUSD", date(2026, 10, 8)), date(2026, 10, 13))
        # Canada Day 2028 is a Saturday and is taken on Monday the 3rd, not Friday
        self.assertFalse(self.c.is_holiday("CAD", date(2028, 6, 30)))
        self.assertTrue(self.c.is_holiday("CAD", date(2028, 7, 3)))

    def test_japanese_substitute_and_citizens_holidays(self):
        # 3 May 2026 is a Sunday; the 4th and 5th are holidays already, so the
        # substitute is Wednesday the 6th
        self.assertTrue(self.c.is_holiday("JPY", date(2026, 5, 6)))
        self.assertFalse(self.c.is_holiday("JPY", date(2026, 5, 7)))
        # the equinoxes, and the year-end bank holiday
        self.assertTrue(self.c.is_holiday("JPY", date(2026, 3, 20)))
        self.assertTrue(self.c.is_holiday("JPY", date(2027, 9, 23)))
        self.assertTrue(self.c.is_holiday("JPY", date(2026, 12, 31)))

    def test_the_end_of_month_rule(self):
        """Off a month-end spot, a month tenor settles on a month end.

        Without it a 1M dealt off a 28-Feb spot settles 28-Mar where the
        market settles 31-Mar, and the expiry is then a business day early.
        """
        d = self.c.fx_dates("EURUSD", "1M", date(2026, 2, 25))
        self.assertEqual(d.spot, date(2026, 2, 27))      # the last value date of Feb
        self.assertTrue(self.c.is_month_end("EURUSD", d.spot))
        self.assertEqual(d.delivery, date(2026, 3, 31))  # not 27 Mar
        self.assertIn("end-of-month", d.rule)
        # and it carries down the curve, not just to the first month
        self.assertEqual(self.c.delivery_date("EURUSD", "3M", date(2026, 2, 25)),
                         date(2026, 5, 29))

    def test_a_spot_that_is_not_a_month_end_takes_the_ordinary_roll(self):
        d = self.c.fx_dates("EURUSD", "1M", date(2026, 1, 29))
        self.assertFalse(self.c.is_month_end("EURUSD", d.spot))
        self.assertNotIn("end-of-month", d.rule)
        self.assertEqual(d.delivery, date(2026, 3, 2))

    def test_a_day_tenor_is_business_days_from_the_trade_date(self):
        """And so the short tenors stay distinct.

        Adding calendar days to the spot date and taking the spot lag off the
        end -- the old construction -- collapses them: the two days subtracted
        swallow the weekend the addition just crossed.  Dealt on a Wednesday,
        "1D" and "2D" both came back Thursday.
        """
        wed = date(2026, 9, 2)
        got = [self.c.expiry_date("EURUSD", f"{n}D", wed) for n in (1, 2, 3, 4)]
        # Thu, Fri, then over the weekend *and* over US Labor Day on the 7th
        self.assertTrue(self.c.is_holiday("EURUSD", date(2026, 9, 7)))
        self.assertEqual(got, [date(2026, 9, 3), date(2026, 9, 4),
                               date(2026, 9, 8), date(2026, 9, 9)])
        self.assertEqual(len(set(got)), 4)

    def test_overnight_is_one_business_day_settling_from_its_own_spot(self):
        d = self.c.fx_dates("EURUSD", "O/N", date(2026, 9, 1))
        self.assertEqual(d.expiry, date(2026, 9, 2))
        self.assertEqual(d.delivery, date(2026, 9, 4))
        # "1D" is the same dates under a different name
        one = self.c.fx_dates("EURUSD", "1D", date(2026, 9, 1))
        self.assertEqual((one.expiry, one.delivery), (d.expiry, d.delivery))

    def test_usdcad_settles_t_plus_one_all_the_way_through(self):
        d = self.c.fx_dates("USDCAD", "1M", date(2026, 9, 1))
        self.assertEqual(self.c.spot_lag("USDCAD"), 1)
        self.assertEqual(d.spot, date(2026, 9, 2))
        self.assertEqual(self.c.add_business_days("USDCAD", d.expiry, 1), d.delivery)

    def test_a_pair_s_own_holiday_moves_its_settlement_and_not_another_pair_s(self):
        """3 Nov 2026 is Culture Day in Japan and an ordinary Tuesday elsewhere."""
        self.assertEqual(self.c.delivery_date("EURUSD", "2M", date(2026, 9, 1)),
                         date(2026, 11, 3))
        self.assertEqual(self.c.delivery_date("USDJPY", "2M", date(2026, 9, 1)),
                         date(2026, 11, 4))

    def test_the_short_date_codes_parse(self):
        for text, expect in (("O/N", (1.0, "d")), ("on", (1.0, "d")),
                             ("T/N", (2.0, "d")), ("s/n", (3.0, "d")),
                             ("S/W", (1.0, "w"))):
            self.assertEqual(parse_tenor(text), expect, text)
        self.assertEqual(normalise_tenor("o/n"), "O/N")
        self.assertEqual(normalise_tenor("3m"), "3M")
        self.assertEqual(normalise_tenor("1D"), "1D")   # not folded into O/N

    def test_expiry_years_is_the_calendar_and_not_the_nominal_length(self):
        """The reading that moved every mark; see MIGRATION.md 1.6."""
        clock = Clock(datetime(2026, 9, 1, 12, tzinfo=UTC))
        t = self.c.expiry_years("EURUSD", "1M", clock)
        expected = clock.years_to(datetime(2026, 10, 1, tzinfo=UTC))
        self.assertAlmostEqual(t, expected, places=12)
        self.assertNotAlmostEqual(t, tenor_to_years("1M"), places=4)


class TestEventTable(unittest.TestCase):
    """The EVENTS sheet in memory: one row per release, weights per currency
    shared across pairs, an adjustment per pair."""

    def setUp(self):
        from volkit.events import EventBook, EventRow
        self.EventBook, self.EventRow = EventBook, EventRow
        self.when = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)
        self.table = EventBook([EventRow(self.when, "FOMC",
                                         weights={"USD": 0.015, "JPY": 0.003},
                                         adjust={"USDJPY": 0.002})])

    def test_the_shipped_workbook_has_its_events_on_the_events_sheet(self):
        data = ExcelSource(BOOK).load()
        self.assertEqual(data.problems, [], data.problems)
        self.assertTrue(data.events.rows)
        self.assertTrue(data.events.currencies())

    def test_a_weight_is_shared_and_an_adjustment_is_not(self):
        """One row, three pairs.  The dollar's weight reaches every pair with
        a dollar leg; only USDJPY's own cell is USDJPY's."""
        uj = self.table.for_pair("USDJPY")[0]
        self.assertAlmostEqual(uj.bump, 0.020)
        self.assertEqual(uj.weights, {"USD": 0.015, "JPY": 0.003})
        self.assertAlmostEqual(uj.adjust, 0.002)
        eu = self.table.for_pair("EURUSD")[0]
        self.assertAlmostEqual(eu.bump, 0.015)
        self.assertAlmostEqual(eu.adjust, 0.0)
        eg = self.table.for_pair("EURGBP")[0]
        self.assertAlmostEqual(eg.bump, 0.0)
        # It is still a row of the sheet: that blank cell is where EURGBP's
        # adjustment would be typed, so the panel sees it and the curve does not.
        self.assertEqual(self.table.for_pair("EURGBP", touching_only=True), [])

    def test_an_alias_leg_takes_the_weight(self):
        """A weight on CNY is the CNH leg's too (``events.CURRENCY_ALIASES``)."""
        self.table.rows[0].weights = {"CNY": 0.004}
        self.assertAlmostEqual(self.table.for_pair("USDCNH")[0].bump, 0.004)

    def test_marking_a_leg_weight_on_one_pair_moves_the_others_and_says_so(self):
        """The panel shows the sheet through one pair's eyes, so a weight
        typed there is the sheet's.  That is the point, and it is never
        hidden: the note names every pair it reached."""
        from volkit.events import EventEntry
        entry = EventEntry(self.when, None, "FOMC", {"USD": 0.02, "JPY": 0.003}, 0.002)
        bad, notes = self.table.set_pair("USDJPY", [entry],
                                         pairs=["USDJPY", "EURUSD", "EURGBP"])
        self.assertEqual(bad, [])
        self.assertTrue(any("EURUSD" in n and "USD" in n for n in notes), notes)
        self.assertNotIn("EURGBP", " ".join(notes))     # no dollar leg
        self.assertAlmostEqual(self.table.for_pair("EURUSD")[0].bump, 0.02)

    def test_the_other_currencies_of_a_row_are_not_cleared_by_a_pair_that_cannot_see_them(self):
        """EURUSD's panel shows EUR and USD.  Applying it must not wipe the
        JPY column -- a screen that never held a number must not zero it."""
        from volkit.events import EventEntry
        entry = EventEntry(self.when, None, "FOMC", {"EUR": 0.0, "USD": 0.015}, 0.0)
        self.table.set_pair("EURUSD", [entry], pairs=["USDJPY", "EURUSD"])
        self.assertAlmostEqual(self.table.rows[0].weights["JPY"], 0.003)
        self.assertAlmostEqual(self.table.rows[0].adjust["USDJPY"], 0.002)

    def test_two_rows_at_one_time_are_refused(self):
        """A row is identified by its time; a second at the same minute would
        overwrite the first rather than add to it."""
        from volkit.events import EventEntry
        bad, _ = self.table.set_pair("USDJPY", [
            EventEntry(self.when, None, "a", {"USD": 0.01}, 0.0),
            EventEntry(self.when, None, "b", {"USD": 0.02}, 0.0)])
        self.assertTrue(bad and "two events" in bad[0], bad)

    def test_set_weights_replaces_the_currencies_and_keeps_every_pair_cell(self):
        problems = self.table.set_weights([
            {"when": self.when, "label": "FOMC", "weights": {"USD": 0.02}}])
        self.assertEqual(problems, [])
        self.assertEqual(self.table.rows[0].weights, {"USD": 0.02})
        self.assertAlmostEqual(self.table.rows[0].adjust["USDJPY"], 0.002)
        self.assertAlmostEqual(self.table.for_pair("USDJPY")[0].bump, 0.022)


class TestTheSettlementDateBox(unittest.TestCase):
    """The settlement date is an input, and the calendar is only its default.

    A tenor names a settlement date first and the expiry comes back from it,
    so the settlement date is the anchor of the whole construction -- but it
    is also the one date on a leg the calendar cannot always answer, because
    a broken date is a thing two counterparties agree and not a thing a
    holiday table knows.  So the box fills itself from the calendar, says so,
    and takes a date typed over it; emptying it hands it back.
    """

    @classmethod
    def setUpClass(cls):
        from volkit.webapp import BookService
        cls.service = BookService(str(BOOK), ASOF, feed_path=str(FEED))
        # The service's own book, so the leg rows and the level checked
        # against them are read off one feed: `feed.load_for` places a tenor
        # pillar on its real delivery date, which is the placement the whole
        # screen is built on.
        cls.book = cls.service.book

    def rows(self, *legs):
        return self.service.legs({"legs": list(legs)})["legs"]

    def test_an_untyped_box_is_the_calendar_s_and_says_what_the_default_is(self):
        row = self.rows({"pair": "USDJPY", "expiry": "1M"})[0]
        cal = self.service.book.calendars
        expiry = date.fromisoformat(row["expiry"])
        want = cal.delivery_from_expiry("USDJPY", expiry).isoformat()
        self.assertEqual(row["settle"], want)
        # The default travels with it so the screen has somewhere to put the
        # box back to the moment it is emptied, without rebuilding a
        # construction that lives on the pair's own holidays.
        self.assertEqual(row["settle_default"], want)
        self.assertFalse(row["settle_stated"])
        self.assertEqual(row["settle_note"], "")

    def test_a_date_in_the_box_is_not_evidence_anybody_typed_it(self):
        """The screen fills this box the way it fills spot from the feed.

        It then posts what is in it, so a date arriving here means nothing on
        its own; ``settlesrc`` is the screen saying whether somebody chose
        it.  Read the other way, every leg would have claimed a broken date.
        """
        default = self.rows({"pair": "USDJPY", "expiry": "1M"})[0]["settle_default"]
        row = self.rows({"pair": "USDJPY", "expiry": "1M",
                         "settle": "2024-12-31", "settlesrc": "calc"})[0]
        self.assertEqual(row["settle"], default)
        self.assertFalse(row["settle_stated"])
        # A caller that sends a date and no flag -- the API, a script -- means
        # it, which is the reading that needs no screen to be present.
        row = self.rows({"pair": "USDJPY", "expiry": "1M", "settle": "2024-12-31"})[0]
        self.assertEqual(row["settle"], "2024-12-31")
        self.assertTrue(row["settle_stated"])

    def test_the_forward_is_read_on_the_date_in_the_box_and_the_expiry_stays(self):
        """The one thing a stated settlement date moves.

        It is the date a forward is a price for, so a forward read there is a
        different forward; the expiry is what the option is worth time on and
        it does not move, so the volatility and the time to expiry are
        untouched.
        """
        base = self.rows({"pair": "USDJPY", "expiry": "1M"})[0]
        far = self.rows({"pair": "USDJPY", "expiry": "1M", "settle": "2024-06-28"})[0]
        self.assertEqual(far["expiry"], base["expiry"])
        self.assertEqual(far["days"], base["days"])
        self.assertEqual(far["years"], base["years"])
        self.assertEqual(far["settle"], "2024-06-28")
        self.assertAlmostEqual(far["spot"], base["spot"], places=12)
        self.assertNotAlmostEqual(far["points"], base["points"], places=4)
        # And it is the *level* lookup that moved, not a second copy of it:
        # the same date asked of the one place a level is read.
        level = self.book.market_level_for("USDJPY", date.fromisoformat(base["expiry"]),
                                           date(2024, 6, 28))
        self.assertAlmostEqual(far["forward"], level["forward"], places=12)
        self.assertEqual(level["settle"], "2024-06-28")

    def test_the_priced_leg_reads_its_forward_on_the_same_date_it_shows(self):
        """One construction, so the date shown and the date read are one date."""
        from volkit.pricing import OptionLeg, price_strip
        base, moved = price_strip(self.book, [
            OptionLeg("USDJPY", "1M", "ATM"),
            OptionLeg("USDJPY", "1M", "ATM", settle="2024-06-28")])["legs"]
        self.assertEqual(moved["settle"], "2024-06-28")
        self.assertEqual(moved["expiry"], base["expiry"])
        self.assertAlmostEqual(moved["t"], base["t"], places=15)
        self.assertAlmostEqual(moved["atm_vol"], base["atm_vol"], places=12)
        self.assertNotAlmostEqual(moved["forward"], base["forward"], places=5)
        self.assertIn("as typed", moved["settle_rule"])

    def test_a_settlement_date_before_the_expiry_is_refused(self):
        """An option cannot settle before it is exercised.

        Nothing fails silently: the row keeps its place and carries the
        reason, and the message says how to get the box back.
        """
        row = self.rows({"pair": "USDJPY", "expiry": "1M", "settle": "2024-03-01"})[0]
        self.assertIn("before the expiry", row["error"])
        self.assertIn("Empty the box", row["error"])
        from volkit.pricing import OptionLeg, price_strip
        r = price_strip(self.book,
                        [OptionLeg("USDJPY", "1M", "ATM", settle="2024-03-01")])["legs"][0]
        self.assertFalse(r["ok"])
        self.assertIn("before the expiry", r["error"])

    def test_a_broken_date_is_taken_and_said_out_loud(self):
        """The case the box exists for is not the case it refuses.

        A settlement date that is not a value date for the pair is
        deliverable only by agreement -- which is exactly what somebody typing
        one is telling the screen -- so it is priced and reported, never
        rejected and never silently rolled to the next good day.
        """
        saturday = date(2024, 6, 29)
        self.assertFalse(self.service.book.calendars.is_settlement_day("USDJPY", saturday))
        row = self.rows({"pair": "USDJPY", "expiry": "1M", "settle": saturday.isoformat()})[0]
        self.assertEqual(row["error"], "")
        self.assertEqual(row["settle"], saturday.isoformat())
        self.assertIn("not a value date", row["settle_note"])
        from volkit.pricing import OptionLeg, price_strip
        r = price_strip(self.book, [OptionLeg("USDJPY", "1M", "ATM",
                                              settle=saturday.isoformat())])["legs"][0]
        self.assertTrue(r["ok"], r["error"])
        self.assertTrue(any("not a value date" in w for w in r["warnings"]))
        # A date the calendar produced says nothing: a note on every leg is a
        # note nobody reads.
        self.assertEqual(self.rows({"pair": "USDJPY", "expiry": "1M"})[0]["settle_note"], "")

    def test_the_box_takes_every_spelling_the_expiry_box_takes(self):
        """One timestamp reader, so a screen never grows a second date parser."""
        for text in ("2024-06-28", "28Jun24", "28 Jun 2024", "June 28, 2024",
                     "2024/06/28", "6/28/2024", "20240628"):
            with self.subTest(text):
                row = self.rows({"pair": "USDJPY", "expiry": "1M", "settle": text})[0]
                self.assertEqual(row["error"], "")
                self.assertEqual(row["settle"], "2024-06-28")


if __name__ == "__main__":
    unittest.main()
