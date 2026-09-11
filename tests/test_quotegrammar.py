"""Parsing what a trader types: quotes, requests, shorthand and dates.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


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
        # A volatility at an absolute strike is one number whichever side it
        # is quoted from, so the side is dropped and the two lines are one quote.
        self.assertIsNone(run.quotes[3].is_call)

    def test_a_volatility_is_read_as_the_number_it_was_written_as(self):
        """The level is not evidence of the unit (§4).

        A paste whose at-the-money is a third of a point is a managed pair, not
        a paste in decimals; sniffing the magnitude returned it as 35 points.
        A paste really in decimals is loaded by saying so.
        """
        run = self.parse("1M ATM 8.20/8.60\n3M 25d RR 0.35/0.55\n")
        self.assertEqual(run.vol_unit, "percent")
        self.assertAlmostEqual(run.quotes[1].bid, 0.0035)
        # The whole paste below 1.0: still points, and it says so.
        low = self.parse("1M ATM 0.35/0.40\n3M 25d RR 0.02/0.04\n")
        self.assertEqual(low.vol_unit, "percent")
        self.assertAlmostEqual(low.quotes[0].bid, 0.0035)
        self.assertTrue(any("as written" in n for n in low.notes))
        dec = self.parse("1M ATM 0.0820/0.0860\n3M 25d RR 0.0035/0.0055\n",
                         vol_unit="decimal")
        self.assertEqual(dec.vol_unit, "decimal")
        self.assertAlmostEqual(dec.quotes[0].ask, 0.0860)

    def test_a_paste_that_straddles_one_is_read_and_not_refused(self):
        """It used to be refused as 'percent in one place and decimal in
        another'.  There is only one reading now: both lines are points."""
        run = self.parse("1M ATM 8.20/8.60\n3M ATM 0.35/0.45\n")
        self.assertEqual(run.vol_unit, "percent")
        self.assertAlmostEqual(run.quotes[0].bid, 0.0820)
        self.assertAlmostEqual(run.quotes[1].bid, 0.0035)

    def test_a_paste_with_no_level_quote_still_reads_as_written(self):
        run = self.parse("3M 25d RR 0.35/0.55\n6M 25d fly 0.20/0.28\n")
        self.assertEqual(run.vol_unit, "percent")
        self.assertAlmostEqual(run.quotes[0].bid, 0.0035)

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
        # Two different spreads, deliberately: '1M/3M' and '3M-1M' are the
        # same quote written two ways, and one now supersedes the other.
        run = self.parse("1M atm 8.2/8.6\n1M/3M atm spread 0.30/0.55\n6M-2M atm spread 0.30/0.55")
        for q, near, far in ((run.quotes[1], "1M", "3M"), (run.quotes[2], "2M", "6M")):
            self.assertEqual(q.instrument, "spread")
            self.assertEqual(str(q.expiry), near)
            self.assertEqual(str(q.expiry_far), far)
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


class TestRequestParsing(unittest.TestCase):
    """Reading the other box: what is being asked for, with no price on it.

    Same grammar as a broker run with one thing taken out, read by the same
    tokeniser -- and the absence of the price is *enforced*, which is the
    whole difference between the two boxes.
    """

    def parse(self, text, **kw):
        kw.setdefault("pair", "EURUSD")
        return quotes.parse_requests(text, **kw)

    def test_reads_the_instruments_a_desk_is_asked_for(self):
        asked = self.parse(
            "1M ATM in 100mm vega\n"
            "3M 25d RR\n"
            "2M 25d fly\n"
            "6M 1.1000 call\n"
            "1M/3M ATM spread\n")
        self.assertEqual([q.instrument for q in asked.requests],
                         ["atm", "rr", "fly", "outright", "spread"])
        self.assertEqual(asked.skipped, ())
        self.assertEqual(asked.requests[0].size, 100.0)
        self.assertEqual(asked.requests[0].size_basis, "vega")
        self.assertAlmostEqual(asked.requests[1].delta, 0.25)
        self.assertAlmostEqual(asked.requests[3].strike, 1.10)
        self.assertIsNone(asked.requests[3].is_call)   # a strike quote carries no side

    def test_a_price_in_the_request_box_is_refused_not_read_as_a_strike(self):
        """A broker run pasted into the wrong box would otherwise be quoted at
        levels nobody asked about, which is the silent wrong answer this
        project exists to remove."""
        asked = self.parse("1M ATM 8.20/8.60\n3M 25d RR 0.35/0.55\n")
        self.assertEqual(asked.requests, ())
        self.assertEqual(len(asked.skipped), 2)
        for _, _, why in asked.skipped:
            self.assertIn("reads as a price", why)
            self.assertIn("market box", why)

    def test_one_number_on_a_line_that_has_not_said_what_it_is_struck_at(self):
        """'6M 1.1000' is a strike; '1M ATM 8.5' is a price on an instrument
        that already said what it is."""
        got = self.parse("6M 1.1000\n").requests[0]
        self.assertEqual(got.instrument, "outright")
        self.assertAlmostEqual(got.strike, 1.10)
        self.assertEqual(self.parse("1M ATM 8.5\n").requests, ())

    def test_a_direction_word_is_resolved_against_the_pair(self):
        """The sign lives on the request and is applied once, where the row is
        built.  §5's first entry is what a second place for a sign costs."""
        plain = self.parse("3M 25d rr\n", pair="USDJPY").requests[0]
        asked = self.parse("3M 25d rr jpy call over\n", pair="USDJPY").requests[0]
        self.assertEqual(plain.sign, 1.0)
        self.assertEqual(asked.sign, -1.0)
        self.assertEqual(asked.direction, "jpy")
        self.assertIn("JPY call over", asked.describe())
        # And a currency that is not a leg is a refusal, not a guess.
        self.assertIn("not a leg", self.parse("3M 25d rr chf call over\n",
                                              pair="USDJPY").skipped[0][2])

    def test_the_same_instrument_asked_for_twice_is_two_questions(self):
        """Unlike the market box, where a run is a conversation and a later
        quote of one thing replaces the earlier one.  Two sizes of the same
        tenor are two prices to make."""
        asked = self.parse("1M ATM in 50mm\n1M ATM in 500mm\n")
        self.assertEqual(len(asked.requests), 2)
        self.assertEqual([q.size for q in asked.requests], [50.0, 500.0])

    def test_an_instrument_that_cannot_be_read_keeps_its_reason(self):
        asked = self.parse("3M rr\n1M ATM\nnonsense\n")
        self.assertEqual(len(asked.requests), 1)
        self.assertEqual([n for n, _, _ in asked.skipped], [1, 3])
        self.assertIn("needs a delta", asked.skipped[0][2])


class TestColumnQuotes(unittest.TestCase):
    """A run written as ``expiry, strike, bid/offer`` columns.

    The same parser reads it and the broker-English form, because a run that
    mixes them -- and they do -- must not depend on which line came first.
    """

    def parse(self, text, **kw):
        kw.setdefault("pair", "EURUSD")
        return quotes.parse_quotes(text, **kw)

    def test_the_three_strike_column_spellings_all_read(self):
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n"
                         "09:15, 3M, 1.0900, 8.10/8.50\n"
                         "09:15, 2M, 25d, 8.00/8.40\n"
                         "09:15, 6M, 25dp, 7.90/8.30\n")
        self.assertEqual(len(run.quotes), 4, run.skipped)
        atm, strike, call, put = run.quotes
        self.assertEqual(atm.instrument, "atm")
        self.assertEqual((strike.instrument, strike.strike), ("outright", 1.09))
        self.assertEqual((call.instrument, call.delta, call.is_call), ("outright", 0.25, True))
        self.assertEqual((put.instrument, put.delta, put.is_call), ("outright", 0.10 * 2.5, False))
        self.assertAlmostEqual(atm.bid, 0.0820)
        self.assertAlmostEqual(strike.ask, 0.0850)

    def test_an_absolute_strike_needs_no_side_and_is_not_called_a_put(self):
        """The volatility at a strike is one number whichever side quotes it.

        ``is_call`` defaulted to None and ``describe`` read None as a put, so a
        strike-column quote came back labelled as something it was not.
        """
        q = self.parse("3M, 1.0900, 8.10/8.50").quotes[0]
        self.assertIsNone(q.is_call)
        self.assertEqual(q.describe(), "3M 1.09")
        self.assertNotIn("put", q.describe())

    def test_a_strike_with_the_side_glued_on_is_a_strike(self):
        """'7.77c' is the 7.77 call, and it beats a delta on the same line.

        It used to match nothing -- not a number, not a word -- and was
        reported as ignored, which left a line that had named its strike to be
        quoted off whatever delta was beside it, or off the at-the-money.
        """
        q = self.parse("3M, 7.77c, 8.10/8.30").quotes[0]
        self.assertEqual((q.instrument, q.strike), ("outright", 7.77))
        self.assertFalse(any("ignored" in n for n in q.notes), q.notes)

        # The strike names the option exactly and the delta only through the
        # marks, so the strike wins and the line says the delta was dropped.
        both = self.parse("1M 25d 7.77p 8.10/8.30").quotes[0]
        self.assertEqual(both.strike, 7.77)
        self.assertIsNone(both.delta)
        self.assertTrue(any("delta is dropped" in n for n in both.notes), both.notes)

        # On a premium the side is the whole difference between two prices,
        # so there it survives.
        prem = self.parse("6M 1.1000c 0.0123 prem").quotes[0]
        self.assertEqual((prem.strike, prem.is_call), (1.10, True))

        # The delta spellings above it are untouched.
        self.assertEqual(self.parse("1M 25dc 8.1/8.3").quotes[0].delta, 0.25)
        self.assertEqual(self.parse("1M 100k 8.2/8.3").quotes[0].size, 0.1)

    def test_a_bare_delta_takes_the_call_wing_and_says_so(self):
        """A delta names two strikes, one on each wing, so it has to pick.

        It picks the same one the pricing screen's strike box picks for a bare
        '25d', and reports it rather than letting the choice be invisible.
        """
        q = self.parse("2M, 25d, 8.00/8.40").quotes[0]
        self.assertTrue(q.is_call)
        self.assertTrue(any("bare delta" in n for n in q.notes), q.notes)

    def test_a_comma_is_a_column_boundary_and_a_price_never_straddles_one(self):
        """This is the whole difference between two readings of three numbers.

        ``3M, 7.75, 8.30`` is a choice at the 7.75 strike; ``3M 7.75 8.30``,
        with no columns, is the two-way at-the-money it has always been. With
        the commas thrown away, as they used to be, the two are the same line.
        """
        columned = self.parse("1M atm 8.2/8.6\n3M, 7.75, 8.30").quotes[1]
        self.assertEqual(columned.instrument, "outright")
        self.assertEqual(columned.strike, 7.75)
        self.assertAlmostEqual(columned.bid, 0.0830)
        self.assertAlmostEqual(columned.ask, 0.0830)

        plain = self.parse("1M atm 8.2/8.6\n3M 7.75 8.30").quotes[1]
        self.assertEqual(plain.instrument, "atm")
        self.assertAlmostEqual(plain.bid, 0.0775)
        self.assertAlmostEqual(plain.ask, 0.0830)

    def test_a_thousands_separator_is_not_a_column(self):
        q = self.parse("1M ATM 8.20/8.60 in 1,000mm vega").quotes[0]
        self.assertEqual(q.instrument, "atm")
        self.assertEqual(q.size, 1000.0)
        self.assertAlmostEqual(q.bid, 0.0820)

    def test_two_numbers_before_the_price_column_are_refused(self):
        run = self.parse("1M atm 8.2/8.6\n3M, 7.75, 7.80, 8.10/8.50")
        self.assertEqual(len(run.quotes), 1)
        self.assertIn("strike column holds one strike", run.skipped[0][2])

    def test_a_column_header_is_recognised_rather_than_reported_as_a_bad_line(self):
        """A run pasted out of a spreadsheet brings its header with it.

        Listing it as a line that could not be read is noise on top of a paste
        that worked; it is passed over and said so instead. Two header words at
        least, because one stray word is more likely a quote that failed.
        """
        run = self.parse("time, expiry, strike, bid/offer\n09:15, 1M, ATM, 8.20/8.60\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertEqual(run.skipped, ())
        self.assertTrue(any("column header" in n for n in run.notes))

        # One word is not a header, and a line with numbers in it never is.
        broken = self.parse("1M atm 8.2/8.6\nstrike\n")
        self.assertEqual(len(broken.skipped), 1)

    def test_broker_english_still_reads_the_way_it_did(self):
        """The columnar reading must not have moved the old one."""
        run = self.parse("1M ATM 8.20/8.60 in 100mm vega\n"
                         "3M 25d RR 0.35/0.55 eur call over\n"
                         "2M 25d fly 0.20/0.28\n"
                         "6M 1.1000 call 7.90/8.40\n"
                         "1M/3M ATM spread 0.30/0.55\n")
        self.assertEqual([q.instrument for q in run.quotes],
                         ["atm", "rr", "fly", "outright", "spread"])
        self.assertAlmostEqual(run.quotes[3].strike, 1.1000)
        # A volatility at an absolute strike is one number whichever side it
        # is quoted from, so the side is dropped and the two lines are one quote.
        self.assertIsNone(run.quotes[3].is_call)


class TestQuoteTimestamps(unittest.TestCase):
    """A run is a conversation: the same thing is quoted again as it moves."""

    def parse(self, text, **kw):
        kw.setdefault("pair", "EURUSD")
        return quotes.parse_quotes(text, **kw)

    def test_a_later_timestamp_wins_whatever_order_it_was_pasted_in(self):
        """The point of reading the timestamp at all.

        Line 3 is the newest quote and line 5 is an older one pasted after it;
        without timestamps the last line would win and the screen would show a
        stale market as the live one.
        """
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n"
                         "09:41, 1M, ATM, 8.25/8.65\n"
                         "09:05, 1M, ATM, 8.10/8.50\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertAlmostEqual(run.quotes[0].bid, 0.0825)
        self.assertEqual(run.quotes[0].line, 2)
        self.assertEqual({q.line for q in run.superseded}, {1, 3})
        self.assertTrue(all(q.replaced_by == 2 for q in run.superseded))

    def test_without_timestamps_the_later_line_wins(self):
        """The only ordering an untimed line carries is where it was written."""
        run = self.parse("1M ATM 8.20/8.60\n1M ATM 8.25/8.65\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertAlmostEqual(run.quotes[0].bid, 0.0825)
        self.assertEqual(run.superseded[0].line, 1)

    def test_only_the_same_thing_is_superseded(self):
        """An update replaces its own quote and nothing else."""
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n"
                         "09:41, 1M, ATM, 8.25/8.65\n"
                         "09:41, 3M, ATM, 8.40/8.80\n"
                         "09:41, 1M, 25d, 8.30/8.70\n")
        self.assertEqual(len(run.quotes), 3)
        self.assertEqual(len(run.superseded), 1)
        self.assertEqual({q.describe() for q in run.quotes},
                         {"1M ATM", "3M ATM", "1M 25d call"})

    def test_a_market_strangle_and_a_smile_fly_are_not_the_same_quote(self):
        run = self.parse("1M atm 8.2/8.6\n1M 25d strangle 0.20/0.28\n"
                         "1M 25d smile fly 0.20/0.28\n")
        self.assertEqual(len(run.quotes), 3)
        self.assertEqual(run.superseded, ())

    def test_the_survivor_keeps_the_first_position(self):
        """An updated run reads in the order it was written."""
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n"
                         "09:15, 3M, ATM, 8.40/8.80\n"
                         "09:41, 1M, ATM, 8.25/8.65\n")
        self.assertEqual([q.describe() for q in run.quotes], ["1M ATM", "3M ATM"])

    def test_a_time_only_line_takes_the_last_date_above_it(self):
        run = self.parse("2024-02-28 09:15, 1M, ATM, 8.20/8.60\n"
                         "09:41, 1M, ATM, 8.25/8.65\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertAlmostEqual(run.quotes[0].bid, 0.0825)
        self.assertEqual(run.quotes[0].timestamp.strftime("%Y-%m-%d %H:%M"), "2024-02-28 09:41")
        self.assertTrue(any("took the last date above them" in n for n in run.notes))

    def test_an_undated_run_says_it_is_ordered_as_one_day(self):
        """That ordering is wrong across midnight, so it is stated."""
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n23:50, 3M, ATM, 8.40/8.80\n")
        self.assertTrue(any("one day" in n and "midnight" in n for n in run.notes))
        # And the nominal day is never shown: the text is what was written.
        self.assertEqual([q.timestamp_text for q in run.quotes], ["09:15", "23:50"])

    def test_a_date_alone_is_an_expiry_and_a_date_with_a_time_is_a_stamp(self):
        """Reading one as the other moves a quote to a tenor nobody asked for."""
        expiry = self.parse("2024-05-28 ATM 8.15/8.55").quotes[0]
        self.assertEqual(expiry.timestamp_text, "")
        self.assertEqual(str(expiry.expiry)[:10], "2024-05-28")

        stamped = self.parse("2024-02-28T10:05Z, 1M, ATM, 8.30/8.70").quotes[0]
        self.assertEqual(str(stamped.expiry), "1M")
        self.assertEqual(stamped.timestamp.strftime("%Y-%m-%d %H:%M"), "2024-02-28 10:05")

    def test_a_bracketed_time_is_a_time_and_not_a_label(self):
        run = self.parse("[08:00] 3M ATM 8.00/8.40\n[broker A] 1M ATM 8.20/8.60")
        self.assertEqual(run.quotes[0].timestamp_text, "08:00")
        self.assertEqual(run.quotes[0].label, "")
        self.assertEqual(run.quotes[1].label, "broker a")
        self.assertEqual(run.quotes[1].timestamp_text, "")

    def test_a_superseded_quote_is_kept_rather_than_dropped(self):
        """A line read, understood and then silently discarded is the failure
        this module exists to remove."""
        run = self.parse("09:15, 1M, ATM, 8.20/8.60\n09:41, 1M, ATM, 8.25/8.65\n")
        self.assertEqual(len(run.all_quotes), 2)
        self.assertEqual([q.line for q in run.all_quotes], [1, 2])
        self.assertTrue(any("replaced by a later quote" in n for n in run.notes))

    def test_the_panel_reports_the_time_and_what_it_replaced(self):
        from volkit import marketmaker as mm
        panel = mm.check_panel_from_request({
            "pair": "EURUSD", "cut": "NY", "method": "SVI",
            "text": ("09:15, 1M, ATM, 8.20/8.60\n09:41, 1M, ATM, 8.25/8.65\n"
                     "09:20, 2M, 25d, 8.00/8.40\n"),
        })
        book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD"])
        sheet = panel.run(book)["market"]
        self.assertEqual([r["timestamp"] for r in sheet["rows"]], ["09:41", "09:20"])
        self.assertEqual(len(sheet["superseded"]), 1)
        self.assertEqual(sheet["superseded"][0]["replaced_by"], 2)
        self.assertAlmostEqual(sheet["superseded"][0]["bid"], 8.20)


class TestQuoteGrammar(unittest.TestCase):
    """The looser reading of a run: what wins when a line says two things,
    which lines are somebody else's, and structures of more than two legs.

    Every rule here is a precedence stated once: a strike over a delta, a
    date over a tenor, the side only where the side changes the number.
    """

    def parse(self, text, **kw):
        kw.setdefault("pair", "EURUSD")
        return quotes.parse_quotes(text, **kw)

    def test_a_strike_beats_a_delta_and_says_so(self):
        q = self.parse("6M 25d 1.1200 call 7.8/8.3\n").quotes[0]
        self.assertEqual(q.instrument, "outright")
        self.assertAlmostEqual(q.strike, 1.12)
        self.assertIsNone(q.delta)
        self.assertTrue(any("the strike is used" in n for n in q.notes))
        # And in columns, where this used to be a refusal.
        col = self.parse("3M, 25d 1.0900, 8.10/8.50\n")
        self.assertEqual(col.skipped, ())
        self.assertAlmostEqual(col.quotes[0].strike, 1.09)
        self.assertIsNone(col.quotes[0].delta)

    def test_a_weekday_is_an_intra_week_expiry(self):
        """A day name is the next one of that day from today: a run that wants
        a date this week writes 'Fri', and counting the days by hand is how a
        quote lands a day off the option it was quoting."""
        from datetime import date
        friday = date(2026, 9, 4)                     # a Friday
        run = self.parse("mon atm 8.1/8.5\nwed 1.1000 call 7.9/8.4\n"
                         "thursday 25d fly 0.20/0.28\nfri 25d rr -0.30/-0.10\n",
                         today=friday)
        self.assertEqual(run.skipped, ())
        self.assertEqual([str(q.expiry)[:10] for q in run.quotes],
                         ["2026-09-07", "2026-09-09", "2026-09-10", "2026-09-11"])
        self.assertTrue(any("next Monday from today" in n for n in run.quotes[0].notes))
        # Strictly forward: 'Fri' read on a Friday is the Friday coming, not
        # an expiry this morning, which is no expiry at all.
        self.assertEqual(str(run.quotes[3].expiry)[:10], "2026-09-11")
        # The weakest way to say when: anything else on the line beats it.
        both = self.parse("fri 1M atm 8.2/8.6\n", today=friday)
        self.assertEqual(str(both.quotes[0].expiry), "1M")
        self.assertTrue(any("the weekday is read as the day it was written" in n
                            for n in both.quotes[0].notes))
        # A day name in front of a time is the day the run was written. Read
        # as an expiry it would beat the tenor beside it and move the quote a
        # month, which is the one way a weekday could do real damage.
        stamped = self.parse("mon 09:15 1M atm 8.2/8.6\n", today=friday)
        self.assertEqual(str(stamped.quotes[0].expiry), "1M")
        self.assertEqual(stamped.quotes[0].timestamp_text, "09:15")
        # Both legs of a spread may be weekdays.
        sp = self.parse("mon vs fri atm 0.10/0.20\n", today=friday)
        self.assertEqual([str(sp.quotes[0].expiry)[:10], str(sp.quotes[0].expiry_far)[:10]],
                         ["2026-09-07", "2026-09-11"])
        # And with no clock behind the run there is nothing to count from, so
        # it is said rather than guessed.
        none = self.parse("mon atm 8.1/8.5\n")
        self.assertEqual(none.quotes, ())
        self.assertIn("no tenor or expiry", none.skipped[0][2])

    def test_a_date_beats_a_tenor(self):
        q = self.parse("1M 2026-09-30 atm 8.1/8.5\n").quotes[0]
        self.assertEqual(str(q.expiry)[:10], "2026-09-30")
        self.assertTrue(any("the date is used" in n for n in q.notes))
        # Dates in the spellings a desk types, not only ISO.
        for spelt in ("30sep26", "30-Sep-2026", "2026/09/30", "09/30/2026"):
            got = self.parse(f"{spelt} atm 8.1/8.5\n")
            self.assertEqual(got.skipped, (), spelt)
            self.assertEqual(str(got.quotes[0].expiry)[:10], "2026-09-30", spelt)
        # Two tenors on a line that is not a spread stay a refusal.
        self.assertIn("2 tenors", self.parse("1M 3M atm 8.1/8.5\n").skipped[0][2])

    def test_the_side_matters_only_with_a_delta_or_on_a_premium(self):
        """'6M 1.10 call' and '6M 1.10 put' are one volatility, so they are one
        quote and the later one supersedes the earlier.  '25d call' and
        '25d put' are two strikes.  A premium at a strike needs the side,
        because a call and a put there are two different prices -- unless the
        line said 'live', which is a low-delta option and so names its side by
        which side of the forward the strike sits on."""
        run = self.parse("6M 1.10 call 7.9/8.4\n6M 1.10 put 7.95/8.45\n")
        self.assertEqual(len(run.quotes), 1)
        self.assertIsNone(run.quotes[0].is_call)
        self.assertEqual(len(run.superseded), 1)
        wings = self.parse("1M 25d call 8.9/9.3\n1M -25d 8.9/9.3\n1M 25dp 8.9/9.3\n")
        self.assertEqual([q.is_call for q in wings.quotes], [True, False])
        self.assertEqual(len(wings.superseded), 1)
        live = self.parse("6M 1.10 live 0.0125/0.0135\n")
        self.assertEqual(live.skipped, ())
        self.assertIsNone(live.quotes[0].is_call)
        self.assertTrue(any("moneyness" in n for n in live.quotes[0].notes))
        # A premium that did not say 'live' still needs the side written out.
        prem = self.parse("6M 1.10 prem 0.0125/0.0135\n")
        self.assertEqual(prem.quotes, ())
        self.assertIn("needs the side", prem.skipped[0][2])

    def test_a_premium_is_read_in_its_unit_and_never_scaled(self):
        run = self.parse("1M atm 8.2/8.6\n"
                         "6M 1.10 call 125/135 pips\n"
                         "6M 1.10 put 1.25%/1.35% prem\n"
                         "3M 1.10 call 0.0125/0.0135 usd\n"
                         "3M 1.10 put 0.0125/0.0135 eur\n")
        self.assertEqual(run.skipped, ())
        self.assertEqual(run.vol_unit, "percent")
        kinds = [(q.quote_kind, q.premium_unit) for q in run.quotes]
        self.assertEqual(kinds, [("vol", None), ("premium", "pips"), ("premium", "pct"),
                                 ("premium", "price"), ("premium", "pct")])
        self.assertAlmostEqual(run.quotes[1].bid, 125.0)       # not 1.25
        self.assertAlmostEqual(run.quotes[2].ask, 1.35)
        self.assertAlmostEqual(run.quotes[3].bid, 0.0125)
        self.assertAlmostEqual(run.quotes[4].bid, 1.25)        # a fraction of base, as a per cent
        self.assertTrue(all(q.is_call is not None for q in run.quotes[1:]))
        self.assertIn("premium", run.quotes[1].describe())
        # A premium on something that is not an option is refused.
        self.assertIn("premium", self.parse("1M atm 0.5/0.6 prem\n").skipped[0][2])
        # A currency that is neither leg is refused rather than guessed.
        self.assertIn("not a leg", self.parse("3M 1.10 call 0.01/0.02 chf\n").skipped[0][2])

    def test_basis_points_are_a_hundredth_of_a_per_cent(self):
        """'5bp' is 0.05% of the base notional and never 5 of anything.  It is
        read glued to its number or as the word after it, on one side of a
        two-way or on both -- and because the price named its own unit, the
        number beside it that did not is the strike."""
        run = self.parse("6M 1.2500 live 12/14bp\n"
                         "3M 1.3000 call 8bps prem\n"
                         "1M 1.1000 live 5 bp / 7 bp\n"
                         "2M 1.0500 live 9 bps\n")
        self.assertEqual(run.skipped, ())
        self.assertEqual([(q.premium_unit, round(q.bid, 6), round(q.ask, 6)) for q in run.quotes],
                         [("pct", 0.12, 0.14), ("pct", 0.08, 0.08),
                          ("pct", 0.05, 0.07), ("pct", 0.09, 0.09)])
        self.assertEqual([q.strike for q in run.quotes], [1.25, 1.30, 1.10, 1.05])
        self.assertTrue(all(q.quote_kind == "premium" for q in run.quotes))
        self.assertTrue(any("hundredth of a per cent" in n for n in run.quotes[0].notes))
        # A digit has to come before the unit, so GBP is a currency and a
        # volatility line is not turned into a premium by the letters in it.
        self.assertIsNone(quotes._BPS_NUM.search("gbpusd"))
        self.assertEqual(self.parse("1M atm 8.1/8.5\n").quotes[0].quote_kind, "vol")

    def test_lines_for_another_pair_are_passed_over_not_refused(self):
        run = self.parse("1M atm 8.2/8.6\n"
                         "USDJPY 1M atm 9/9.4\n"
                         "GBPUSD\n"
                         "1M atm 7/7.4\n"
                         "eur/usd:\n"
                         "3M atm 8.1/8.5\n")
        self.assertEqual([q.describe() for q in run.quotes], ["1M ATM", "3M ATM"])
        self.assertEqual(run.skipped, ())
        self.assertEqual([(n, why) for n, _, why in run.ignored],
                         [(2, "quotes USDJPY, not EURUSD"), (4, "quotes GBPUSD, not EURUSD")])
        self.assertTrue(any("passed over" in n for n in run.notes))
        self.assertEqual(run.quotes[1].pair, "EURUSD")
        # With no pair given nothing is filtered and every line carries its pair.
        every = quotes.parse_quotes("USDJPY 1M atm 9/9.4\nGBPUSD\n1M atm 7/7.4\n")
        self.assertEqual([q.pair for q in every.quotes], ["USDJPY", "GBPUSD"])
        # The same on the request box.
        asked = quotes.parse_requests("1M atm\nUSDJPY 1M atm\n", pair="EURUSD")
        self.assertEqual(len(asked.requests), 1)
        self.assertEqual(asked.ignored[0][0], 2)

    def test_two_legs_of_one_instrument_are_the_calendar_spread_as_before(self):
        run = self.parse("1M/3M atm spread 0.30/0.55\n"
                         "1M vs 3M 25d rr 0.10/0.20\n"
                         "1M atm vs 3M atm 0.30/0.55\n"
                         "buy 1M atm sell 3M atm 0.30/0.55\n")
        self.assertEqual(run.skipped, ())
        # Line 3 is the same quote as line 1 and supersedes it; line 4 is a
        # structure because it carries signs.
        self.assertEqual([q.instrument for q in run.quotes], ["spread", "spread", "structure"])
        rr = run.quotes[1]
        self.assertEqual((str(rr.expiry), str(rr.expiry_far), rr.leg, rr.delta),
                         ("1M", "3M", "rr", 0.25))
        self.assertTrue(any("took" in n or "same instrument" in n for n in rr.notes))
        self.assertEqual(quotes.instrument_key(run.superseded[0]),
                         quotes.instrument_key(run.quotes[0]))
        self.assertEqual([leg.weight for leg in run.quotes[2].legs], [1.0, -1.0])

    def test_a_structure_is_the_signed_sum_of_its_legs(self):
        run = self.parse("6M 1.10 call vs 1.15 call 0.35/0.55\n"
                         "+1M atm vs -2x 3M atm vs +6M atm 0.05/0.15\n"
                         "1M vs 3M vs 6M atm 0.1/0.2\n"
                         "sell 3M 25d rr jpy call over buy 6M 25d rr 0.1/0.2\n", pair="USDJPY")
        self.assertEqual([n for n, _, _ in run.skipped], [3])
        self.assertIn("needs a sign on each", run.skipped[0][2])
        cs, fly, rr = run.quotes
        self.assertEqual(cs.instrument, "structure")
        self.assertEqual([(str(l.expiry), l.strike, l.weight) for l in cs.legs],
                         [("6M", 1.10, -1.0), ("6M", 1.15, 1.0)])
        self.assertIsNone(cs.expiry_far)                  # one tenor, so one expiry
        self.assertEqual([str(x) for x in cs.expiries()], ["6M"])
        self.assertEqual([l.weight for l in fly.legs], [1.0, -2.0, 1.0])
        self.assertEqual((str(fly.expiry), str(fly.expiry_far)), ("1M", "6M"))
        self.assertEqual([str(x) for x in fly.expiries()], ["1M", "3M", "6M"])
        # A direction word on a leg is folded into that leg's weight.
        self.assertEqual([l.weight for l in rr.legs], [1.0, 1.0])
        self.assertEqual(rr.describe(), "-3M 25d RR (JPY call over) +6M 25d RR")
        self.assertAlmostEqual(cs.bid, 0.0035)
        # The request box reads the same structures with no price.
        asked = quotes.parse_requests("6M 1.10 call vs 1.15 call\n", pair="EURUSD")
        self.assertEqual(asked.requests[0].instrument, "structure")
        self.assertEqual(len(asked.requests[0].legs), 2)


class TestPairShorthand(unittest.TestCase):
    """One currency names a pair: 'cnh' is USDCNH and 'eur' is EURUSD.

    Which side the dollar sits on is market convention, and the shorthand is
    read **last**, so that everything else a currency word does on a run -- a
    direction, a premium currency, the currency of a size -- keeps its token.
    """

    def test_the_dollar_goes_where_convention_puts_it(self):
        self.assertEqual(quotes._shorthand_pair("cnh"), "USDCNH")
        self.assertEqual(quotes._shorthand_pair("hkd"), "USDHKD")
        self.assertEqual(quotes._shorthand_pair("jpy"), "USDJPY")
        self.assertEqual(quotes._shorthand_pair("eur"), "EURUSD")
        self.assertEqual(quotes._shorthand_pair("gbp"), "GBPUSD")
        self.assertEqual(quotes._shorthand_pair("aud"), "AUDUSD")
        self.assertEqual(quotes._shorthand_pair("xau"), "XAUUSD")
        # The dollar names no pair on its own, and neither does a non-currency.
        self.assertIsNone(quotes._shorthand_pair("usd"))
        self.assertIsNone(quotes._shorthand_pair("atm"))

    def test_a_shorthand_on_the_line_names_the_pair_and_says_so(self):
        q = quotes.parse_quotes("cnh 1M atm 5.0/5.4\n").quotes[0]
        self.assertEqual(q.pair, "USDCNH")
        self.assertTrue(any("read as USDCNH" in n for n in q.notes))

    def test_a_run_of_several_shorthands_carries_a_pair_a_line(self):
        run = quotes.parse_quotes("eur 1M atm 8.0/8.4\n"
                                  "hkd 1M atm 1.0/1.4\n"
                                  "cnh 1M atm 5.0/5.4\n")
        self.assertEqual([q.pair for q in run.quotes], ["EURUSD", "USDHKD", "USDCNH"])

    def test_a_shorthand_heading_is_a_block_heading(self):
        run = quotes.parse_quotes("cnh\n1M atm 5.0/5.4\n3M atm 5.2/5.6\n")
        self.assertEqual([q.pair for q in run.quotes], ["USDCNH", "USDCNH"])
        self.assertTrue(any("USDCNH" in n for n in run.notes))

    def test_a_shorthand_naming_another_pair_is_passed_over_like_a_written_one(self):
        run = quotes.parse_quotes("cnh 1M atm 5.0/5.4\n1M atm 8.2/8.6\n", pair="EURUSD")
        self.assertEqual([q.pair for q in run.quotes], ["EURUSD"])
        self.assertEqual([n for n, _, _ in run.ignored], [1])
        self.assertIn("USDCNH", run.ignored[0][2])

    def test_the_market_maker_reading_takes_it_as_a_named_pair(self):
        """``require_pair`` refuses a line that names none; a shorthand names one."""
        self.assertEqual(quotes.parse_quotes("cnh 1M atm 5.0/5.4\n",
                                             require_pair=True).quotes[0].pair, "USDCNH")
        bare = quotes.parse_quotes("1M atm 5.0/5.4\n", require_pair=True)
        self.assertEqual(bare.quotes, ())
        self.assertIn(quotes.NO_PAIR, bare.skipped[0][2])

    def test_the_request_box_reads_it_too(self):
        self.assertEqual(quotes.parse_requests("cnh 1M atm\n").requests[0].pair, "USDCNH")

    def test_a_direction_word_keeps_its_currency(self):
        """'jpy call over' is a direction, not a line about USDJPY."""
        q = quotes.parse_quotes("3M 25d rr 0.40/0.60 jpy call over", pair="USDJPY").quotes[0]
        self.assertEqual((q.pair, q.direction), ("USDJPY", "jpy"))
        self.assertLess(q.bid, 0.0)
        # And the two-word form, with nothing between the currency and 'over'.
        other = quotes.parse_quotes("3M 25d rr 0.40/0.60 eur over", pair="EURUSD").quotes[0]
        self.assertEqual((other.pair, other.direction), ("EURUSD", "eur"))

    def test_a_premium_currency_keeps_its_currency(self):
        q = quotes.parse_quotes("6M 1.10 call 0.0125/0.0135 usd", pair="EURUSD").quotes[0]
        self.assertEqual((q.quote_kind, q.premium_unit, q.pair),
                         ("premium", "price", "EURUSD"))

    def test_a_size_keeps_its_currency(self):
        """'in 100mm eur' is a hundred million euros, not a line about EURUSD.

        Reading it as the pair would do worse than mislabel the row: under
        another pair the whole line would go to ``ignored`` and disappear.
        """
        for text in ("1M atm 8.2/8.6 in 100mm eur",
                     "1M atm 8.2/8.6 in 100mm vega eur"):
            run = quotes.parse_quotes(text, pair="USDJPY")
            self.assertEqual(run.ignored, (), text)
            self.assertEqual(run.quotes[0].pair, "USDJPY", text)

    def test_a_written_pair_beats_a_shorthand_on_the_same_line(self):
        q = quotes.parse_quotes("usdjpy 1M atm 8.2/8.6 jpy call over").quotes[0]
        self.assertEqual(q.pair, "USDJPY")


class TestSpacedDates(unittest.TestCase):
    """A date written with the spaces in it is one expiry.

    ``timeutil.parse_datetime`` has read '29 Sep' all along; what it never saw
    was the line, which is split on whitespace long before it gets there.
    """

    TODAY = date(2026, 9, 10)

    def parse(self, text, **kw):
        kw.setdefault("pair", "USDCNH")
        kw.setdefault("today", self.TODAY)
        return quotes.parse_quotes(text, **kw)

    def test_every_spelling_lands_on_the_same_day(self):
        for text in ("29 Sep atm 8.2/8.6", "Sep 29 atm 8.2/8.6",
                     "29 September atm 8.2/8.6", "29 Sep 2026 atm 8.2/8.6",
                     "September 29 2026 atm 8.2/8.6", "29 Sep-26 atm 8.2/8.6",
                     "29-Sep-2026 atm 8.2/8.6"):
            run = self.parse(text)
            self.assertEqual(run.skipped, (), text)
            self.assertEqual(str(run.quotes[0].expiry)[:10], "2026-09-29", text)

    def test_a_strike_after_it_is_still_the_strike(self):
        """The line the convention was asked for: 'cnh 29 Sep 6.66'."""
        asked = quotes.parse_requests("cnh 29 Sep 6.66 call", today=self.TODAY).requests[0]
        self.assertEqual(asked.pair, "USDCNH")
        self.assertEqual(str(asked.expiry)[:10], "2026-09-29")
        self.assertAlmostEqual(asked.strike, 6.66)

    def test_a_bare_two_digit_year_is_not_taken(self):
        """'29 Sep 26' is 29 September at the 26 strike as readily as 2026.

        Nothing in the line says which, so it is refused rather than read one
        of the two ways; the year is written in full or glued on.
        """
        self.assertEqual(self.parse("29 Sep 26 atm 8.2/8.6").quotes, ())
        self.assertEqual(str(self.parse("29Sep26 atm 8.2/8.6").quotes[0].expiry)[:10],
                         "2026-09-29")

    def test_a_join_that_could_not_be_a_date_is_not_made(self):
        """A day out of range, and a number that merely ends in one."""
        self.assertNotIn("-sep", quotes._join_spaced_dates("45 sep atm 8.2/8.6"))
        self.assertNotIn("-sep", quotes._join_spaced_dates("1.29 sep atm 8.2/8.6"))

    def test_a_timestamp_and_a_tenor_read_as_they_did(self):
        self.assertEqual(str(self.parse("mon 09:15 1M atm 8.2/8.6").quotes[0].expiry), "1M")
        self.assertEqual(str(self.parse("1M/3M atm spread 0.30/0.55").quotes[0].expiry), "1M")


class TestQuoteGrammarOnTheBook(unittest.TestCase):
    """The new grammar through the fit and the quote."""

    @classmethod
    def setUpClass(cls):
        from volkit.book import Book
        from volkit.feed import MarketFeed
        cls.book = Book.from_excel(BOOK, ASOF).build(["EURUSD"])
        cls.book.feed = MarketFeed.load(FEED)

    def test_a_premium_becomes_the_volatility_that_reprices_it(self):
        from volkit import marketmaker as mm
        run = quotes.parse_quotes("3M 1.0900 call 125/135 pips\n3M 1.0900 put 0.9%/1.0% prem\n",
                                  pair="EURUSD")
        expiries = mm.resolve_expiries(self.book.clock, run.quotes)
        levels = mm._levels_for(self.book, "EURUSD", expiries)
        out, errors = mm.premiums_as_vols(list(run.quotes), expiries, levels, "EURUSD")
        self.assertEqual(errors, ["", ""])
        F, spot, pip = (levels["3M"][k] for k in ("forward", "spot", "pip"))
        _, t = expiries["3M"]
        for q, px, factor in ((out[0], (125.0, 135.0), 1.0 / pip),
                              (out[1], (0.9, 1.0), spot / 100.0)):
            self.assertEqual(q.quote_kind, "vol")
            for v, p in zip((q.bid, q.ask), px):
                self.assertAlmostEqual(float(black.price(F, 1.09, v, t, bool(q.is_call))),
                                       p * factor, places=12)
        # No feed, no conversion -- and a reason rather than a forward of 1.
        bare, why = mm.premiums_as_vols(list(run.quotes), expiries, {}, "EURUSD")
        self.assertEqual(bare[0].quote_kind, "premium")
        self.assertIn("no forward feed", why[0])

    def test_a_live_line_takes_its_side_from_the_moneyness(self):
        """A live option is dealt without its delta hedge, which is what makes
        it a low-delta option: it is the out-of-the-money one, so the side of
        the forward the strike sits on names the call or the put.  The parse
        leaves the side open -- it has no forward -- and the conversion here
        settles it and says so."""
        from volkit import marketmaker as mm
        run = quotes.parse_quotes("3M 1.2500 live 12/14bp\n3M 0.9500 live 8/10bp\n",
                                  pair="EURUSD")
        self.assertEqual(run.skipped, ())
        self.assertEqual([q.is_call for q in run.quotes], [None, None])
        # 12bp is 0.12% of the base notional, not 12 of anything.
        self.assertEqual([(q.premium_unit, q.bid, q.ask) for q in run.quotes],
                         [("pct", 0.12, 0.14), ("pct", 0.08, 0.10)])
        expiries = mm.resolve_expiries(self.book.clock, run.quotes)
        levels = mm._levels_for(self.book, "EURUSD", expiries)
        out, errors = mm.premiums_as_vols(list(run.quotes), expiries, levels, "EURUSD")
        self.assertEqual(errors, ["", ""])
        F = levels["3M"]["forward"]
        self.assertLess(F, 1.25)
        self.assertGreater(F, 0.95)
        # Above the forward the call, below it the put, and both said outright.
        self.assertEqual([q.is_call for q in out], [True, False])
        for q in out:
            self.assertTrue(any("read from the moneyness" in n for n in q.notes), q.notes)
        # And the volatility it lands on reprices the premium it came from.
        _, t = expiries["3M"]
        spot = levels["3M"]["spot"]
        for q, px, k in ((out[0], (0.12, 0.14), 1.25), (out[1], (0.08, 0.10), 0.95)):
            for v, p in zip((q.bid, q.ask), px):
                self.assertAlmostEqual(float(black.price(F, k, v, t, bool(q.is_call))),
                                       p * spot / 100.0, places=12)

    def test_a_side_read_off_a_near_the_money_strike_says_so(self):
        """The moneyness names the side because a live option is far out of
        the money.  Read off a strike that is not, it is a guess, and the row
        carries the warning that says which way to write it out."""
        from volkit import marketmaker as mm
        expiries = mm.resolve_expiries(self.book.clock, [])
        near = quotes.parse_quotes("3M atm 8.0/8.4\n", pair="EURUSD")
        expiries = mm.resolve_expiries(self.book.clock, near.quotes)
        F = mm._levels_for(self.book, "EURUSD", expiries)["3M"]["forward"]
        run = quotes.parse_quotes(f"3M {F * 1.001:.4f} live 2.0/2.4%\n", pair="EURUSD")
        expiries = mm.resolve_expiries(self.book.clock, run.quotes)
        levels = mm._levels_for(self.book, "EURUSD", expiries)
        out, errors = mm.premiums_as_vols(list(run.quotes), expiries, levels, "EURUSD")
        self.assertEqual(errors, [""])
        self.assertIs(out[0].is_call, True)
        self.assertTrue(any("not the low-delta one" in n for n in out[0].notes), out[0].notes)

    def test_a_structure_values_as_the_signed_sum_of_its_legs(self):
        from volkit import marketmaker as mm
        run = quotes.parse_quotes("+1M atm vs -2x 3M atm vs +6M atm 0.05/0.15\n"
                                  "1M atm 8/8.4\n3M atm 8/8.4\n6M atm 8/8.4\n", pair="EURUSD")
        surface = self.book["EURUSD"]
        ev = mm.Evaluator(surface, "SVI", "NY")
        expiries = mm.resolve_expiries(self.book.clock, run.quotes)
        got = ev.value(run.quotes[0], expiries, {})
        legs = [ev.value(q, expiries, {}) for q in run.quotes[1:]]
        self.assertAlmostEqual(got, legs[0] - 2 * legs[1] + legs[2], places=14)
        # Every expiry a structure names is resolved, not only its two ends.
        self.assertEqual(sorted(expiries), ["1M", "3M", "6M"])


if __name__ == "__main__":
    unittest.main()
