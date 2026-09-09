"""The quoting agent: the archive, the dissemination reader, the model leash, and
the price.

Most of these pin a behaviour that was wrong at some point during the build,
and the comment above each names it.  The ones that need a real surface are at
the bottom and are skipped where the numeric stack is not installed; every
other test here runs on stdlib plus the package.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from volkit import archive as arch
from volkit import flow as flow_mod
from volkit import ingest, llm, quotes, sdr
from volkit import synthesis as syn
from volkit.knowledge import KnowledgeBank, PairKnowledge, Rule

UTC = timezone.utc
MORNING = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)

RUN = """\
09:15 1M ATM 8.20/8.60 in 100mm vega
09:41 1M ATM 8.25/8.65
3M 25d RR 0.35/0.55 eur call over
2M 25d fly 0.20/0.28
"""


def _tmp(name: str) -> str:
    return str(Path(tempfile.mkdtemp()) / name)


def _run(text: str = RUN, pair: str = "EURUSD"):
    return quotes.parse_quotes(text, pair=pair)


def _obs(**kw) -> arch.Observation:
    body = dict(kind="quote", pair="EURUSD", at=arch._iso(MORNING), instrument="atm",
                tenor="1M", bid=8.20, ask=8.60)
    body.update(kw)
    return arch.Observation(**body)


# ==========================================================================
class TestArchiveIdentity(unittest.TestCase):
    """An observation seen twice is one observation."""

    def test_same_run_read_twice_adds_nothing(self):
        # The failure this stops: a watched folder rescanned all day, every
        # width statistic slowly gaining confidence it never earned.
        path = _tmp("arc.jsonl")
        a = arch.Archive.load(path)
        first, _ = a.extend(arch.from_quotes(_run(), pair="EURUSD", origin="chat.txt",
                                             default_time=MORNING))
        a.flush()
        b = arch.Archive.load(path)
        again, _ = b.extend(arch.from_quotes(_run(), pair="EURUSD", origin="a_copy.txt",
                                             default_time=MORNING))
        self.assertEqual(first, 4)
        self.assertEqual(again, 0, "the same quotes under a different file name are not new")

    def test_fallback_time_must_come_from_the_source(self):
        # The id is a hash of the content, so a fallback of "now" would give
        # the same line a new id on every scan and defeat the test above.
        with self.assertRaises(arch.ArchiveError) as caught:
            arch.from_quotes(_run(), pair="EURUSD")
        self.assertIn("default_time", str(caught.exception))

    def test_the_hash_ignores_how_a_line_was_read(self):
        # One quote read by the grammar and again by a language model is one
        # quote.  ``via`` is on the record and out of the id on purpose.
        by_parser = _obs(via="parser")
        by_model = _obs(via="model:llama3.1")
        self.assertEqual(by_parser.id, by_model.id)

    def test_a_level_is_rounded_before_it_is_hashed(self):
        # 8.2 * 100 is not 8.2 in binary.  Unrounded, the same quote reached
        # by two routes hashed two ways and was counted twice.
        one = arch.from_quotes(_run("1M ATM 8.20/8.60"), pair="EURUSD",
                               default_time=MORNING)[0]
        self.assertEqual(one.bid, 8.2)
        self.assertEqual(one.ask, 8.6)


class TestArchiveRefusals(unittest.TestCase):
    """What must never reach the file."""

    def test_an_inverted_market_is_refused_not_repaired(self):
        # Swapping them and clamping them disagree about which number was the
        # mistake, so neither is done: the line was misread.
        problems = _obs(bid=9.0, ask=8.0).problems()
        self.assertTrue(any("misread" in p for p in problems), problems)

    def test_a_delta_in_points_is_refused(self):
        # 25 instead of 0.25 would never match a bank rule written for 0.25,
        # so the quote would fall silently through to the fallback tier.
        problems = _obs(instrument="rr", delta=25.0, bid=0.3, ask=0.5).problems()
        self.assertTrue(any("fraction" in p for p in problems), problems)

    def test_a_quote_with_no_side_is_not_an_observation(self):
        problems = _obs(bid=None, ask=None).problems()
        self.assertTrue(any("neither a bid nor an offer" in p for p in problems), problems)

    def test_an_outcome_must_name_the_price_it_answers(self):
        problems = arch.Observation(kind="outcome", pair="EURUSD",
                                    result="traded_ask").problems()
        self.assertTrue(any("names no price" in p for p in problems), problems)

    def test_a_record_from_a_newer_build_is_named_not_dropped(self):
        with self.assertRaises(arch.ArchiveError) as caught:
            arch.observation_from_dict({"kind": "quote", "pair": "EURUSD",
                                        "vega_notional_v2": 1})
        self.assertIn("vega_notional_v2", str(caught.exception))


class TestArchiveHistory(unittest.TestCase):
    """Append-only, and corrections that name what they correct."""

    def test_a_correction_supersedes_and_both_stay_in_the_file(self):
        path = _tmp("arc.jsonl")
        a = arch.Archive.load(path)
        original = _obs(external_id="101")
        a.add(original)
        a.add(_obs(bid=8.30, ask=8.70, external_id="103", supersedes=original.id))
        a.flush()
        back = arch.Archive.load(path)
        self.assertEqual(len(back.records), 2, "the file keeps what happened")
        self.assertEqual(len(back.live()), 1, "the view shows what is believed")
        self.assertEqual(back.live()[0].external_id, "103")

    def test_a_cancel_for_a_trade_we_never_saw_is_reported_not_applied(self):
        # Publishers cancel prints from before this file existed, and a cancel
        # that silently matched nothing looks exactly like one that worked.
        a = arch.Archive.load(_tmp("arc.jsonl"))
        resolved, notes = a.resolve([_obs(kind="trade", bid=None, ask=None,
                                          action="CANC", supersedes_external="999")])
        self.assertEqual(resolved[0].supersedes, "")
        self.assertTrue(any("999" in n for n in notes), notes)

    def test_a_superseded_quote_is_kept_as_width_evidence(self):
        # One tenor quoted twice in a run is one live price and *two*
        # observations of how wide that tenor is shown.
        run = _run()
        self.assertEqual(len(run.quotes), 3)
        self.assertEqual(len(run.superseded), 1)
        self.assertEqual(len(arch.from_quotes(run, pair="EURUSD", default_time=MORNING)), 4)

    def test_a_flush_after_a_bad_line_appends_the_right_slice(self):
        # Counting the file's lines to find what was already written mis-sliced
        # when the loader had skipped an unreadable line.
        path = _tmp("arc.jsonl")
        Path(path).write_text('{"kind": "quote", "pair": "EURUSD", "bid": 1, "ask": 2}\n'
                              'not json at all\n', encoding="utf-8")
        a = arch.Archive.load(path)
        self.assertEqual(len(a.records), 1)
        self.assertTrue(any("line 2" in p for p in a.problems), a.problems)
        a.add(_obs(tenor="6M"))
        self.assertEqual(a.flush(), 1)
        self.assertEqual(len(arch.Archive.load(path).records), 2)


# ==========================================================================
_OLD_LAYOUT = """\
DISSEMINATION_ID,ORIGINAL_DISSEMINATION_ID,ACTION,EXECUTION_TIMESTAMP,ASSET_CLASS,\
UNDERLYING_ASSET_1,OPTION_STRIKE_PRICE,OPTION_TYPE,OPTION_PREMIUM,OPTION_CURRENCY,\
ROUNDED_NOTIONAL_AMOUNT_1,NOTIONAL_CURRENCY_1,OPTION_EXPIRATION_DATE
101,,NEWT,2026-08-20T09:14:22Z,FX,EUR-USD,1.1000,CALL,1250000,USD,100000000,EUR,2026-09-21
102,,NEWT,2026-08-20T10:02:00Z,FX,EUR-USD,1.0800,PUTO,880000,USD,250000000+,EUR,2026-11-20
103,101,CORR,2026-08-20T11:30:00Z,FX,EUR-USD,1.1000,CALL,1260000,USD,100000000,EUR,2026-09-21
104,,NEWT,2026-08-20T11:31:00Z,COMMODITY,WTI,70,CALL,10,USD,1000,USD,2026-09-21
105,,CANC,2026-08-20T12:00:00Z,FX,EUR-USD,1.0800,PUTO,880000,USD,250000000,EUR,2026-11-20
"""

_CDE_LAYOUT = """\
Dissemination Identifier,Action type,Execution Timestamp,Expiration Date,Strike Price,\
Option Type,Option Premium Amount,Option Premium Currency,Notional amount-Leg 1,\
Underlier ID-Leg 1,Notional amount cap indicator
900,NEWT,2026-08-21T14:05:11Z,2026-10-21,151.50,PUTO,2100000,USD,300000000,USD/JPY,
"""


#: The layout actually in circulation, taken column-for-column off a 2026
#: CFTC FOREX file.  Three things about it are the point: ``Option Type`` is
#: **empty in every row**, the pair is written ``HKD/USD`` for what a desk
#: quotes as USDHKD, and the side and the strike are carried by the two legs.
_LIVE_LAYOUT = """\
Dissemination Identifier,Original Dissemination Identifier,Action type,Event type,\
Event timestamp,Asset Class,Execution Timestamp,Expiration Date,Notional amount-Leg 1,\
Notional amount-Leg 2,Notional currency-Leg 1,Notional currency-Leg 2,Call amount,\
Call currency,Put amount,Put currency,Exchange rate,Exchange rate basis,\
Option Premium Amount,Option Premium Currency,Strike Price,\
Strike price currency/currency pair,Option Type,Unique Product Identifier,UPI FISN,\
UPI Underlier Name
700,,NEWT,TRAD,2026-09-03T07:21:28Z,FX,2026-09-03T07:21:17Z,2028-03-09,"149,840,000",\
"1,176,262,090+",USD,HKD,149840000,USD,1176244000,HKD,7.85,HKD/USD,"106,386.4",USD,7.85,\
HKD/USD,,QZBVMVVKXBSM,NA/O Van Put HKD USD,HKD USD
701,,NEWT,TRAD,2026-09-03T14:59:48Z,FX,2026-09-03T14:59:40Z,2026-09-28,"16,000,000",\
"125,379,984",USD,HKD,,,,,7.836249,HKD/USD,,,,,,QZ7T4G0X6H2P,NA/Fwd NDF HKD USD,HKD USD
702,,NEWT,TRAD,2026-09-02T09:11:03Z,FX,2026-09-02T09:10:55Z,2026-12-02,"50,000,000",\
"390,000,000",USD,HKD,390000000,HKD,50000000,USD,7.8,HKD/USD,"31,000",USD,7.8,HKD/USD,,\
QZBVMVVKXBSM,NA/O Van Call HKD USD,HKD USD
"""


def _csv(text: str) -> str:
    path = _tmp("sdr.csv")
    Path(path).write_text(text, encoding="utf-8")
    return path


class TestSdr(unittest.TestCase):

    def test_both_layouts_are_read_by_the_same_reader(self):
        old = sdr.read_sdr(_csv(_OLD_LAYOUT), known_pairs=["EURUSD"])
        new = sdr.read_sdr(_csv(_CDE_LAYOUT), known_pairs=["USDJPY"])
        self.assertEqual([o.external_id for o in old.records], ["101", "102", "103"])
        self.assertEqual(len(new.records), 1)
        self.assertEqual(new.records[0].pair, "USDJPY")

    def test_commodity_is_not_a_currency_pair(self):
        # Without the boundary look-arounds the pair regex found COM + MOD
        # inside COMMODITY and filed a crude oil trade under the pair COMMOD.
        self.assertEqual(sdr._pair_of("WTI", "", "COMMODITY", "", "", ""), ("", ""))
        read = sdr.read_sdr(_csv(_OLD_LAYOUT), known_pairs=["EURUSD"])
        self.assertTrue(any("no currency pair" in why for _, why, _ in read.skipped),
                        read.skipped)

    def test_a_capped_notional_keeps_the_number_and_says_it_is_a_cap(self):
        # Read as a plain number, a 750 million trade becomes a 250 million
        # one and every size-conditioned statistic downstream is wrong.
        read = sdr.read_sdr(_csv(_OLD_LAYOUT), known_pairs=["EURUSD"])
        capped = [o for o in read.records if o.external_id == "102"][0]
        self.assertTrue(capped.notional_capped)
        self.assertEqual(capped.notional, 250_000_000.0)
        self.assertTrue(any("lower bound" in n for n in capped.notes), capped.notes)

    def test_a_correction_carries_the_id_it_corrects(self):
        read = sdr.read_sdr(_csv(_OLD_LAYOUT), known_pairs=["EURUSD"])
        corr = [o for o in read.records if o.action == "CORR"][0]
        self.assertEqual(corr.supersedes_external, "101")

    def test_a_cancel_with_no_original_id_is_refused(self):
        read = sdr.read_sdr(_csv(_OLD_LAYOUT), known_pairs=["EURUSD"])
        self.assertTrue(any("names no original" in why for _, why, _ in read.skipped),
                        read.skipped)

    def test_an_unplaceable_column_is_reported_rather_than_skipped(self):
        read = sdr.read_sdr(_csv(_OLD_LAYOUT.replace("ASSET_CLASS", "SOMETHING_NEW")),
                            known_pairs=["EURUSD"])
        self.assertIn("SOMETHING_NEW", read.unplaced)

    def test_a_missing_column_names_itself(self):
        with self.assertRaises(sdr.SdrError) as caught:
            sdr.read_sdr(_csv("a,b,c\n1,2,3\n"))
        self.assertIn("executed", str(caught.exception))

    # ---- the layout in circulation ------------------------------------
    def test_the_side_comes_off_the_legs_because_option_type_is_empty(self):
        """``Option Type`` is blank in every row of a 2026 file.  Read only
        from that column no print has a side, and a trade with no side cannot
        be inverted to a volatility -- so the whole tape is unusable.  The
        legs carry it: a USD call against an HKD put is a call on USDHKD."""
        read = sdr.read_sdr(_csv(_LIVE_LAYOUT), known_pairs=["USDHKD"])
        trades = [o for o in read.records if o.kind == "trade"]
        self.assertEqual([o.external_id for o in trades], ["700", "702"])
        self.assertEqual([o.is_call for o in trades], [True, False])
        self.assertTrue(any("read as a call from the legs" in n for n in trades[0].notes),
                        trades[0].notes)

    def test_the_pair_is_oriented_against_the_book_and_not_the_file(self):
        """The file writes USDHKD as ``HKD/USD`` and AUDHKD as ``AUD/HKD``, so
        the order in the file is not a convention anything may be read from.
        Matched either way round, the book's spelling wins -- and without this
        every USDHKD print is dropped as a pair the book does not build."""
        read = sdr.read_sdr(_csv(_LIVE_LAYOUT), known_pairs=["USDHKD"])
        self.assertTrue(read.records)
        self.assertEqual({o.pair for o in read.records}, {"USDHKD"})
        self.assertTrue(any("the book quotes USDHKD" in n for n in read.records[0].notes))
        # And with no book to orient against, the file's own order stands.
        loose = sdr.read_sdr(_csv(_LIVE_LAYOUT))
        self.assertEqual({o.pair for o in loose.records}, {"HKDUSD"})

    def test_the_legs_say_which_way_round_the_strike_is_written(self):
        """A strike of 7.85 and one of 0.127 are the same strike written two
        ways, and the file's own labels do not settle which.  The amounts do:
        the quote-currency leg over the base-currency leg."""
        flipped = _LIVE_LAYOUT.replace(",7.85,HKD/USD,,QZBVMVVKXBSM",
                                       ",0.12738853503,HKD/USD,,QZBVMVVKXBSM")
        read = sdr.read_sdr(_csv(flipped), known_pairs=["USDHKD"])
        trade = [o for o in read.records if o.external_id == "700"][0]
        self.assertAlmostEqual(trade.strike, 7.85, places=6)
        self.assertTrue(any("published in the other direction" in n for n in trade.notes),
                        trade.notes)

    def test_a_forward_print_is_kept_as_a_forward_and_not_as_a_trade(self):
        """Two thirds of every file is outrights, NDFs and swaps.  Filed as
        at-the-money *trades* they are 60,000 rows a day that are not options;
        kept as forwards they are the only forward curve there is on a pair
        the historical workbook has never held."""
        read = sdr.read_sdr(_csv(_LIVE_LAYOUT), known_pairs=["USDHKD"])
        fwd = [o for o in read.records if o.kind == "forward"]
        self.assertEqual([o.external_id for o in fwd], ["701"])
        self.assertAlmostEqual(fwd[0].rate, 7.836249, places=6)
        self.assertEqual(fwd[0].expiry_date, "2026-09-28")
        self.assertEqual(fwd[0].problems(), [])

    def test_the_cap_is_read_off_the_leg_the_premium_is_divided_by(self):
        """The file caps one leg and not the other.  Reading the row's flag
        instead of the base leg's threw away trades whose base leg was
        published in full -- and the base leg is the only one a premium per
        unit is divided by."""
        read = sdr.read_sdr(_csv(_LIVE_LAYOUT), known_pairs=["USDHKD"])
        trade = [o for o in read.records if o.external_id == "700"][0]
        self.assertFalse(trade.notional_capped)      # the HKD leg is capped, the USD leg is not
        self.assertEqual(trade.notional, 149_840_000.0)
        self.assertEqual(trade.notional_ccy, "USD")

    def test_the_execution_timestamp_beats_the_event_one(self):
        """On an exercise or a correction the event timestamp is the moment of
        that event, and reading it as the trade dates a print days late."""
        read = sdr.read_sdr(_csv(_LIVE_LAYOUT), known_pairs=["USDHKD"])
        trade = [o for o in read.records if o.external_id == "700"][0]
        self.assertTrue(trade.at.startswith("2026-09-03T07:21:17"))

    def test_an_ambiguous_date_is_not_guessed(self):
        # 03/04/2026 is four weeks apart in the two conventions and the tenor
        # a trade lands on is the whole point of keeping it.
        self.assertEqual(sdr._date("03/04/2026"), "")
        self.assertEqual(sdr._date("2026-09-21"), "2026-09-21")

    def test_the_fisn_names_the_side_when_the_legs_did_not(self):
        # The pattern was written with literal backspace characters in place
        # of the word boundaries it meant, so it matched nothing and every row
        # that relied on the FISN for its side came back without one.
        self.assertTrue(sdr._fisn_side("NA/O Van Put HKD USD", "USD", "HKD"),
                        "a put on the quote currency is a call on the base")
        self.assertFalse(sdr._fisn_side("NA/O Van Put USD HKD", "USD", "HKD"))
        self.assertTrue(sdr._fisn_side("NA/O Van Call USD HKD", "USD", "HKD"))
        self.assertIsNone(sdr._fisn_side("NA/Fwd NDF HKD USD", "USD", "HKD"))
        self.assertIsNone(sdr._fisn_side("NA/O Van Put JPY USD", "USD", "HKD"),
                          "a currency not in the pair names no side")


# ==========================================================================
class TestModelLeash(unittest.TestCase):
    """The numeric guard, and what happens with no model at all."""

    def test_a_number_not_in_the_source_refuses_the_whole_line(self):
        source = llm.numbers_in("eurusd 1m running 8.2 at 8.6")
        self.assertEqual(llm.invented_numbers("1M ATM 8.20/8.60", source), [])
        self.assertEqual(llm.invented_numbers("1M ATM 8.20/8.65", source), ["8.65"])

    def test_the_guard_compares_values_and_not_spellings(self):
        # 8.60 against a chat that said 8.6 has to pass, or the guard refuses
        # every correctly transcribed line.
        self.assertEqual(llm.invented_numbers("8.60 .350 08.20",
                                              llm.numbers_in("8.6 0.35 8.2")), [])

    def test_no_model_degrades_and_says_so(self):
        model = llm.LocalModel(llm.ModelConfig(base_url="http://127.0.0.1:9"))
        out = llm.extract_quotes(model, "eurusd 1m running 8.2 at 8.6", pair="EURUSD")
        self.assertEqual(out.lines, [])
        self.assertFalse(out.used_model)
        self.assertTrue(any("no local model" in n for n in out.notes), out.notes)

    def test_a_narration_with_an_invented_number_is_refused_whole(self):
        facts = ["width 0.400 vol points, the bank"]
        self.assertEqual(llm.invented_numbers("shown 0.400 wide, 12% of the time",
                                              set().union(llm.numbers_in(facts[0]))),
                         ["12"])

    def test_complete_never_raises_when_nothing_is_listening(self):
        model = llm.LocalModel(llm.ModelConfig(base_url="http://127.0.0.1:9"))
        reply = model.complete("system", "user")
        self.assertFalse(reply.ok)
        self.assertTrue(reply.why)


# ==========================================================================
class TestIngest(unittest.TestCase):

    def _folder(self, files: dict) -> str:
        folder = Path(tempfile.mkdtemp())
        for name, body in files.items():
            (folder / name).write_text(body, encoding="utf-8")
        return str(folder)

    def test_a_folder_scanned_twice_adds_nothing(self):
        folder = self._folder({"EURUSD_run.txt": RUN})
        a = arch.Archive.load(_tmp("arc.jsonl"))
        state = ingest.State.load(_tmp("state.json"))
        first = ingest.scan([(folder, "chat")], archive=a, state=state,
                            known_pairs=["EURUSD"])
        second = ingest.scan([(folder, "chat")], archive=a, state=state,
                             known_pairs=["EURUSD"])
        self.assertEqual(first.added, 4)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.unchanged, 1)

    def test_a_chat_naming_no_pair_is_refused_by_name(self):
        # A risk reversal's direction cannot be resolved without the pair, and
        # a sign error on a number read as a direction looks like a market.
        folder = self._folder({"run.txt": "1M ATM 8.20/8.60\n"})
        a = arch.Archive.load(_tmp("arc.jsonl"))
        state = ingest.State.load(_tmp("state.json"))
        out = ingest.scan([(folder, "chat")], archive=a, state=state)
        self.assertEqual(out.added, 0)
        self.assertIn("no currency pair", out.files[0].error)

    def test_a_file_that_failed_is_not_retried_while_it_is_unchanged(self):
        folder = self._folder({"run.txt": "1M ATM 8.20/8.60\n"})
        a = arch.Archive.load(_tmp("arc.jsonl"))
        state = ingest.State.load(_tmp("state.json"))
        ingest.scan([(folder, "chat")], archive=a, state=state)
        again = ingest.scan([(folder, "chat")], archive=a, state=state)
        self.assertEqual(again.files, [])
        self.assertTrue(any("still unread" in n for n in again.notes), again.notes)

    def test_a_chat_covering_two_pairs_is_split_at_the_headings(self):
        text = "EURUSD\n1M ATM 8.30/8.70\nUSDJPY\n1M ATM 9.10/9.50\n"
        blocks = ingest.split_by_pair(text, known_pairs=["EURUSD", "USDJPY"])
        self.assertEqual([b[0] for b in blocks], ["EURUSD", "USDJPY"])

    def test_a_heading_is_a_line_that_is_only_a_pair(self):
        # Anchored on purpose: "EURUSD 1M ATM 8.2/8.6" is a quote, and reading
        # it as a heading silently drops the first line of every block.
        blocks = ingest.split_by_pair("EURUSD 1M ATM 8.20/8.60\n", default_pair="EURUSD",
                                      known_pairs=["EURUSD"])
        self.assertEqual(len(blocks), 1)
        self.assertIn("8.20/8.60", blocks[0][1])

    def test_a_pair_in_the_file_name_is_never_a_partial_match(self):
        self.assertEqual(ingest.pair_from_name("EURUSD_2026-08-20.txt"), "EURUSD")
        self.assertEqual(ingest.pair_from_name("run.txt"), "")
        self.assertEqual(ingest.pair_from_name("summary.txt", known_pairs=["EURUSD"]), "")


# ==========================================================================
class TestSynthesis(unittest.TestCase):

    def _archive(self, rows) -> arch.Archive:
        a = arch.Archive.load(_tmp("arc.jsonl"))
        for row in rows:
            ok, why = a.add(row)
            self.assertTrue(ok, why)
        return a

    def _quote(self, days_ago: float, width: float, **kw) -> arch.Observation:
        when = MORNING - timedelta(days=days_ago)
        body = dict(kind="quote", pair="EURUSD", at=arch._iso(when), instrument="atm",
                    tenor="1M", bid=8.40 - width / 2, ask=8.40 + width / 2,
                    counterparty=f"broker{days_ago:g}")
        body.update(kw)
        return arch.Observation(**body)

    def test_one_observation_is_not_a_width(self):
        # A width computed from one quote has a false pedigree, and a false
        # pedigree is worse than a blank because a blank gets questioned.
        out = syn.synthesize(self._archive([self._quote(0, 0.40)]), "EURUSD", asof=MORNING)
        width = out.width_for(instrument="atm", days=30)
        self.assertFalse(width.enough)
        self.assertIn("age-weighted count", width.why_not)
        self.assertEqual(out.proposed_rules(), [])

    def test_a_recent_quote_counts_for_more_than_an_old_one(self):
        out = syn.synthesize(self._archive([self._quote(0, 0.40), self._quote(0.5, 0.40),
                                            self._quote(1, 0.40), self._quote(30, 1.00)]),
                             "EURUSD", asof=MORNING, half_life=5.0)
        width = out.width_for(instrument="atm", days=30)
        self.assertTrue(width.enough)
        self.assertEqual(width.observations, 4, "the old quote is weighted, not dropped")
        self.assertLess(width.median, 0.55, "a month-old quote must not set today's width")

    def test_a_choice_price_is_not_a_zero_width(self):
        # Averaging a zero in quietly tightens the whole ladder.
        out = syn.synthesize(self._archive([self._quote(0, 0.40), self._quote(1, 0.40),
                                            self._quote(2, 0.0)]),
                             "EURUSD", asof=MORNING)
        width = out.width_for(instrument="atm", days=30)
        self.assertEqual(width.observations, 2)
        self.assertAlmostEqual(width.median, 0.40, places=6)

    def test_nothing_after_the_valuation_time_is_used(self):
        # A run priced as of a past date must not see what happened next.
        out = syn.synthesize(self._archive([self._quote(0, 0.40), self._quote(-30, 0.40)]),
                             "EURUSD", asof=MORNING)
        self.assertTrue(any("later than the valuation time" in n for n in out.notes),
                        out.notes)

    def test_an_undated_observation_counts_as_half_a_life_old(self):
        # Treating it as current and dropping it are both wrong in a way that
        # shows up later as a width nobody can explain.
        undated = arch.Observation(kind="quote", pair="EURUSD", instrument="atm",
                                   tenor="1M", bid=8.2, ask=8.6)
        self.assertAlmostEqual(syn._weight(undated, MORNING, 5.0), 0.5)

    def test_the_market_level_is_never_the_mid(self):
        out = syn.synthesize(self._archive([self._quote(0, 0.40), self._quote(1, 0.40)]),
                             "EURUSD", asof=MORNING)
        level = out.level_for(instrument="atm", tenor="1M")
        gap, why = level.gap_to(9.90)
        self.assertGreater(gap, 1.0)
        self.assertIn("not applied", why)

    def test_a_wing_width_is_never_borrowed_for_the_level(self):
        rows = [self._quote(0, 0.20, instrument="rr", delta=0.25, bid=0.35, ask=0.55),
                self._quote(1, 0.20, instrument="rr", delta=0.25, bid=0.35, ask=0.55)]
        out = syn.synthesize(self._archive(rows), "EURUSD", asof=MORNING)
        self.assertIsNone(out.width_for(instrument="atm", days=30))

    def test_our_record_is_words_and_moves_nothing(self):
        rows = []
        for i in range(5):
            price = arch.shown("EURUSD", instrument="atm", tenor="1M", bid=8.25, ask=8.55,
                               model_mid=8.40, at=MORNING - timedelta(days=i))
            rows += [price, arch.outcome(price, "traded_ask", at=MORNING - timedelta(days=i))]
        out = syn.synthesize(self._archive(rows), "EURUSD", asof=MORNING)
        record = out.outcome_for(instrument="atm", days=30)
        which, why = record.lean()
        self.assertEqual(which, "lifted")
        self.assertIn("offer", why)
        # A lean is prose.  Nothing in the synthesis produces a shift.
        self.assertFalse(hasattr(record, "shift"))

    def test_a_premium_is_not_inverted_when_the_size_was_capped(self):
        trade = arch.Observation(kind="trade", pair="EURUSD", at=arch._iso(MORNING),
                                 instrument="outright", strike=1.10, is_call=True,
                                 premium=1_250_000, notional=250_000_000,
                                 notional_capped=True)
        vol, why = syn.implied_from_trade(trade, pair="EURUSD", forward=1.09, years=0.25)
        self.assertIsNone(vol)
        self.assertIn("cap", why)


# ==========================================================================
class TestClientEvidence(unittest.TestCase):
    """What one client has done with our prices, and what a quote may do with it.

    The desk-wide hit rate used to be words only ("shown here, and applied to
    nothing").  Per client, per instrument, it is the one part of the record a
    price applies (§17): their side leans the mid, the move against us after
    their trades widens the price.  These pin the counting; the engine tests
    in ``test_marking`` pin what the price does with it.
    """

    def _archive(self):
        return arch.Archive.load(_tmp("arc.jsonl"))

    def _show(self, a, client, result, *, hours_ago=1.0, instrument="atm", tenor="1M",
              bid=8.20, ask=8.60, away=None, delta=None):
        when = MORNING - timedelta(hours=hours_ago)
        price = arch.shown("EURUSD", instrument=instrument, tenor=tenor, bid=bid, ask=ask,
                           delta=delta, counterparty=client, at=when)
        ok, why = a.add(price)
        self.assertTrue(ok, why)
        if result:
            ans = arch.outcome(price, result, away_level=away,
                               at=when + timedelta(minutes=5))
            ok, why = a.add(ans)
            self.assertTrue(ok, why)
        return price

    def test_a_client_who_only_lifts_is_a_buyer_and_the_side_says_so(self):
        a = self._archive()
        for h in (1, 2, 3, 4):
            self._show(a, "Client A", "traded_ask", hours_ago=h)
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("Client A", instrument="atm", days=30)
        self.assertTrue(rec.enough, rec.why_not)
        self.assertEqual((rec.traded_ask, rec.traded_bid, rec.answered), (4, 0, 4))
        self.assertAlmostEqual(rec.side, 1.0)
        self.assertIn("a buyer", rec.reading())

    def test_the_side_is_age_weighted_and_two_way_is_near_zero(self):
        a = self._archive()
        self._show(a, "Client B", "traded_ask", hours_ago=1)
        self._show(a, "Client B", "traded_bid", hours_ago=2)
        self._show(a, "Client B", "traded_ask", hours_ago=3)
        self._show(a, "Client B", "traded_bid", hours_ago=4)
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("Client B", instrument="atm", days=30)
        self.assertTrue(rec.enough)
        self.assertLess(abs(rec.side), 0.05)
        self.assertIn("two-way", rec.reading())
        # A month-old lift counts for less than this morning's hit: the side
        # leans toward the recent trade, the way every other statistic here does.
        b = self._archive()
        self._show(b, "Client C", "traded_bid", hours_ago=1)
        self._show(b, "Client C", "traded_ask", hours_ago=24 * 30)
        self._show(b, "Client C", "passed", hours_ago=2)
        self._show(b, "Client C", "passed", hours_ago=3)
        rec = syn.synthesize(b, "EURUSD", asof=MORNING).client_for(
            "Client C", instrument="atm", days=30)
        self.assertLess(rec.side, -0.9)

    def test_below_the_minimum_the_record_is_shown_and_not_enough(self):
        # Three answered prices are a story; the row must be able to tell it
        # without leaning on it.  The minimum is the caller's, so one synthesis
        # answers a panel that wants four and a shell that wants two.
        a = self._archive()
        for h in (1, 2, 3):
            self._show(a, "Client D", "traded_ask", hours_ago=h)
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("Client D", instrument="atm", days=30)
        self.assertFalse(rec.enough)
        self.assertIn("below the 4", rec.why_not)
        self.assertEqual(rec.traded_ask, 3, "the counts are still there to be shown")
        self.assertTrue(out.client_for("Client D", instrument="atm", days=30,
                                       minimum=2).enough)

    def test_a_pulled_price_was_never_answered(self):
        a = self._archive()
        for h in (1, 2, 3, 4):
            self._show(a, "Client E", "pulled", hours_ago=h)
        rec = syn.synthesize(a, "EURUSD", asof=MORNING).client_for(
            "Client E", instrument="atm", days=30)
        self.assertEqual(rec.answered, 0)
        self.assertEqual(rec.shown, 4)
        self.assertFalse(rec.enough)

    def test_the_tenor_bucket_is_tried_first_and_the_instrument_second(self):
        # Four lifts on the 1Y and a question about the 1M: the 1M bucket is
        # thin, the instrument across every tenor is not, and the answer says
        # which scope it came from.  Never another instrument: a buyer of the
        # at-the-money says nothing about the risk reversal.
        a = self._archive()
        for h in (1, 2, 3, 4):
            self._show(a, "Client F", "traded_ask", hours_ago=h, tenor="1Y")
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("Client F", instrument="atm", days=30)
        self.assertTrue(rec.enough)
        self.assertIsNone(rec.bucket)
        self.assertIn("every tenor", rec.scope)
        self.assertIsNone(out.client_for("Client F", instrument="rr", days=30, minimum=1))
        self.assertIsNone(out.client_for("Nobody", instrument="atm", days=30))

    def test_a_client_name_is_matched_whatever_its_spacing_and_case(self):
        a = self._archive()
        for h, name in enumerate(("Client G", "client g", "CLIENT  G", " Client G "), start=1):
            self._show(a, name, "traded_bid", hours_ago=h)
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("client G", instrument="atm", days=30)
        self.assertTrue(rec.enough)
        self.assertEqual(rec.traded_bid, 4)
        self.assertEqual(out.client_names(), ["Client G"])

    def test_the_market_moving_their_way_after_a_trade_is_the_cost_of_dealing(self):
        # They lifted our offer at 8.60 four times; the market then quoted
        # 8.80/9.20 (mid 9.00).  It went 0.40 against us each time, and that
        # is what widens the next price -- the adverse move, not the hit rate.
        a = self._archive()
        for h in (30, 31, 32, 33):
            self._show(a, "Client H", "traded_ask", hours_ago=h)
        run = quotes.parse_quotes("1M ATM 8.80/9.20", pair="EURUSD")
        a.extend(arch.from_quotes(run, pair="EURUSD", origin="later.txt",
                                  default_time=MORNING - timedelta(hours=2)))
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        rec = out.client_for("Client H", instrument="atm", days=30)
        self.assertEqual(rec.after_count, 4)
        self.assertAlmostEqual(rec.after_move, 0.40, places=6)
        self.assertIn("followed them", rec.reading())
        # A quote *before* the trade is not a move after it, and a quote
        # further out than AFTER_DAYS is somebody else's week.
        b = self._archive()
        for h in (1, 2, 3, 4):
            self._show(b, "Client I", "traded_bid", hours_ago=h)
        b.extend(arch.from_quotes(run, pair="EURUSD", origin="earlier.txt",
                                  default_time=MORNING - timedelta(days=1)))
        rec = syn.synthesize(b, "EURUSD", asof=MORNING).client_for(
            "Client I", instrument="atm", days=30)
        self.assertIsNone(rec.after_move)
        self.assertEqual(rec.after_count, 0)

    def test_hitting_our_bid_and_the_market_falling_is_adverse_too(self):
        a = self._archive()
        for h in (30, 31, 32, 33):
            self._show(a, "Client J", "traded_bid", hours_ago=h)
        run = quotes.parse_quotes("1M ATM 7.60/8.00", pair="EURUSD")
        a.extend(arch.from_quotes(run, pair="EURUSD", origin="later.txt",
                                  default_time=MORNING - timedelta(hours=2)))
        rec = syn.synthesize(a, "EURUSD", asof=MORNING).client_for(
            "Client J", instrument="atm", days=30)
        self.assertAlmostEqual(rec.after_move, 0.40, places=6)
        self.assertAlmostEqual(rec.side, -1.0)

    def test_done_away_inside_our_price_is_a_negative_gap(self):
        a = self._archive()
        for h in (1, 2, 3, 4):
            self._show(a, "Client K", "done_away", hours_ago=h, away=8.50)
        rec = syn.synthesize(a, "EURUSD", asof=MORNING).client_for(
            "Client K", instrument="atm", days=30)
        self.assertEqual(rec.done_away, 4)
        self.assertLess(rec.away_gap, 0)
        self.assertIn("inside our nearer side", rec.describe())

    def test_a_price_shown_to_nobody_is_in_no_client_record(self):
        a = self._archive()
        for h in (1, 2, 3, 4):
            self._show(a, "", "traded_ask", hours_ago=h)
        out = syn.synthesize(a, "EURUSD", asof=MORNING)
        self.assertEqual(out.clients, [])
        # ... but it is still in the desk's own record.
        self.assertEqual(out.outcome_for(instrument="atm", days=30).traded_ask, 4)


class TestDecisionProse(unittest.TestCase):
    """The explanation is generated from the trace, never the other way round."""

    def _row(self, **kw):
        row = {
            "line": 1, "raw": "1M ATM in 100mm", "describe": "1M ATM in 100mm",
            "instrument": "atm", "tenor": "1M", "model": 8.40, "our_mid": 8.35,
            "our_bid": 8.15, "our_ask": 8.55, "width": 0.40, "width_rung": "bank",
            "skew_total": -0.05, "agent_verdict": "wide",
            "agent_note": "the bank would show 0.400, which is 0.100 wider than the 0.300",
            "flags": ["a flag"], "advice": ["some advice"], "warnings": [],
            "trace": [
                {"name": "model mid", "value": 8.40, "unit": "vol points",
                 "source": "the marked surface", "detail": "", "applied": True},
                {"name": "width", "value": 0.40, "unit": "vol points",
                 "source": "the bank: atm 0.40", "detail": "", "applied": True},
                {"name": "shading, fair value", "value": -0.05, "unit": "vol points",
                 "source": "implied against realized", "detail": "", "applied": True},
                {"name": "mid", "value": 8.35, "unit": "vol points",
                 "source": "the mark plus the shading", "detail": "8.400 -0.050",
                 "applied": True},
                {"name": "bid / offer", "value": None, "unit": "vol points",
                 "source": "the mid, 0.400 wide", "detail": "8.150 / 8.550",
                 "applied": True},
            ],
        }
        row.update(kw)
        return row

    def test_the_facts_are_what_the_explanation_may_say(self):
        from volkit import agent
        d = agent.Decision(row=self._row(), pair="EURUSD")
        self.assertTrue(d.priced)
        self.assertEqual(d.quote_text(), "8.150/8.550")
        facts = d.facts()
        allowed = set()
        for line in facts:
            allowed |= llm.numbers_in(line)
        self.assertIn(llm._canonical(f"{d.bid:.3f}"), allowed)
        self.assertIn(llm._canonical(f"{d.offer:.3f}"), allowed)
        self.assertTrue(any("width verdict: wide" in f for f in facts), facts)
        self.assertTrue(any(f.strip().startswith("flag: a flag") for f in facts))
        self.assertTrue(any(f.strip().startswith("advice: some advice") for f in facts))

    def test_a_signed_ingredient_is_printed_with_its_sign(self):
        from volkit import agent
        d = agent.Decision(row=self._row(), pair="EURUSD")
        lines = [i.line() for i in d.trace]
        self.assertTrue(any(x.startswith("shading, fair value: -0.050") for x in lines), lines)
        self.assertTrue(any(x.startswith("model mid: 8.400") for x in lines), lines)
        self.assertTrue(any("bid / offer: 8.150 / 8.550" in x for x in lines), lines)

    def test_an_unpriced_row_says_no_price(self):
        from volkit import agent
        d = agent.Decision(row=self._row(our_bid=None, our_ask=None, width=None,
                                         width_rung="none"), pair="EURUSD")
        self.assertFalse(d.priced)
        self.assertEqual(d.quote_text(), "no price")
        self.assertEqual(d.width_source, "none")
        self.assertEqual(d.to_json()["quote"], "no price")


class TestRecordAndAnswer(unittest.TestCase):
    """A price shown, filed under a client, and what became of it.

    One pair of functions for the sheet's buttons and the command line, so a
    price recorded from a shell and one recorded from the screen are one kind
    of record.
    """

    def _sheet(self, *rows):
        return {"pair": "EURUSD", "client": {"name": "Client A"},
                "sheet": {"rows": list(rows)}}

    def _row(self, **kw):
        row = {"line": 1, "raw": "1M ATM in 100mm", "describe": "1M ATM in 100mm",
               "instrument": "atm", "tenor": "1M", "tenor_far": None, "delta": None,
               "strike": None, "is_call": None, "fly_kind": None, "size": 100.0,
               "size_basis": "unspecified", "sign": 1.0, "direction": None,
               "model": 8.40, "our_mid": 8.40, "our_bid": 8.20, "our_ask": 8.60,
               "width": 0.40, "width_rung": "bank", "skew_total": 0.0, "flags": []}
        row.update(kw)
        return row

    def test_a_price_is_recorded_with_the_mid_it_was_made_from(self):
        # Looked up when the outcome arrives instead, the question "was our
        # market right that morning" is answered by a curve re-marked since.
        from volkit import agent
        a = arch.Archive.load(_tmp("arc.jsonl"))
        written, refused = agent.record_quote(a, self._sheet(self._row()), at=MORNING)
        self.assertEqual(refused, [])
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].kind, "shown")
        self.assertEqual(written[0].model_mid, 8.40)
        self.assertEqual(written[0].counterparty, "Client A")
        self.assertEqual(written[0].size, 100.0)
        answer = agent.answer(a, written[0].id, "traded_ask", at=MORNING)
        self.assertEqual(answer.ref, written[0].id)
        self.assertEqual(answer.tenor, "1M")
        self.assertEqual(answer.counterparty, "Client A")

    def test_an_unpriced_row_is_not_recorded_and_says_so(self):
        from volkit import agent
        a = arch.Archive.load(_tmp("arc.jsonl"))
        written, refused = agent.record_quote(
            a, self._sheet(self._row(our_bid=None, our_ask=None, width=None)), at=MORNING)
        self.assertEqual(written, [])
        self.assertEqual(len(refused), 1)
        self.assertIn("no price", refused[0])

    def test_a_row_asked_the_other_way_round_is_filed_in_the_books_convention(self):
        # `JPY call over` on USDJPY is sign -1: the row shows -0.55/-0.35 and
        # the file holds 0.35/0.55, because a client's record on the risk
        # reversal must be one record however each request was worded.  So
        # "they lifted our offer" on that row is "they hit our bid" in the file.
        from volkit import agent
        a = arch.Archive.load(_tmp("arc.jsonl"))
        sheet = {"pair": "USDJPY", "client": {"name": "Client B"}, "sheet": {"rows": [
            self._row(instrument="rr", delta=0.25, sign=-1.0, direction="JPY call over",
                      model=-0.45, our_mid=-0.45, our_bid=-0.55, our_ask=-0.35,
                      width=0.20)]}}
        written, _ = agent.record_quote(a, sheet, at=MORNING)
        self.assertEqual((written[0].bid, written[0].ask), (0.35, 0.55))
        self.assertEqual(written[0].model_mid, 0.45)
        self.assertTrue(any("book's convention" in n for n in written[0].notes))
        answer = agent.answer(a, written[0].id, "traded_ask", sign=-1.0, away_level=None,
                              at=MORNING)
        self.assertEqual(answer.result, "traded_bid")
        self.assertTrue(any("filed as traded_bid" in n for n in answer.notes))
        away = agent.answer(a, written[0].id, "done_away", sign=-1.0, away_level=-0.30,
                            at=MORNING + timedelta(minutes=1))
        self.assertEqual(away.away_level, 0.30)

    def test_only_the_lines_asked_for_are_recorded(self):
        from volkit import agent
        a = arch.Archive.load(_tmp("arc.jsonl"))
        sheet = self._sheet(self._row(line=1), self._row(line=2, tenor="3M"))
        written, _ = agent.record_quote(a, sheet, at=MORNING, lines=[2])
        self.assertEqual([o.tenor for o in written], ["3M"])

    def test_an_answer_that_names_nothing_or_the_wrong_thing_is_refused(self):
        from volkit import agent
        a = arch.Archive.load(_tmp("arc.jsonl"))
        written, _ = agent.record_quote(a, self._sheet(self._row()), at=MORNING)
        with self.assertRaises(agent.AgentError):
            agent.answer(a, "nosuchid", "traded_ask")
        with self.assertRaises(agent.AgentError):
            agent.answer(a, written[0].id, "lifted")
        # An outcome answers a price we made, never a market somebody showed.
        market = _obs()
        a.add(market)
        with self.assertRaises(agent.AgentError) as caught:
            agent.answer(a, market.id, "traded_ask")
        self.assertIn("not a price we showed", str(caught.exception))

    def test_the_two_routes_read_the_sheet_and_the_button(self):
        from volkit import agent
        from volkit.timeutil import Clock
        a = arch.Archive.load(_tmp("arc.jsonl"))
        clock = Clock(MORNING)
        with self.assertRaises(agent.AgentError):
            agent.record_from_request(a, {"client": "Client A"}, clock=clock)
        out = agent.record_from_request(a, {"sheet": self._sheet(self._row()),
                                            "client": "Client Z"}, clock=clock)
        self.assertEqual(out["client"], "Client Z")
        self.assertEqual(len(out["recorded"]), 1)
        self.assertEqual(a.by_id(out["recorded"][0]["id"]).counterparty, "Client Z",
                         "the client typed on the bar beats the one the sheet was made for")
        done = agent.outcome_from_request(
            a, {"ref": out["recorded"][0]["id"], "result": "passed", "sign": 1},
            clock=clock)
        self.assertEqual(done["result"], "passed")
        self.assertEqual(done["ref"], out["recorded"][0]["id"])
        with self.assertRaises(agent.AgentError):
            agent.outcome_from_request(a, {"ref": out["recorded"][0]["id"],
                                           "result": "done_away", "away_level": "far"},
                                       clock=clock)


class _FakeBook:
    """Only the clock: filing a paste never touches a surface."""

    def __init__(self, now=MORNING):
        from volkit.timeutil import Clock
        self.clock = Clock(now)


class TestPasteReader(unittest.TestCase):

    def test_a_pair_is_required(self):
        from volkit.agent import AgentError, paste_from_request
        with self.assertRaises(AgentError):
            paste_from_request({"text": "1M ATM 8.2/8.6"})
        p = paste_from_request({"pair": "eurusd", "text": "x"})
        self.assertEqual((p.pair, p.fly_convention, p.vol_unit), ("EURUSD", "market", "auto"))


class TestFilingThePaste(unittest.TestCase):
    """The run on the screen, put into the archive."""

    def setUp(self):
        from volkit import agent
        self.agent = agent
        self.book = _FakeBook()
        self.archive = arch.Archive.load(_tmp("arc.jsonl"))
        self.payload = {"pair": "EURUSD", "text": "1M ATM 8.20/8.60",
                        "fly_convention": "market", "vol_unit": "auto"}

    def _file(self, **kw):
        return self.agent.file_paste(self.archive, self.payload, clock=self.book.clock, **kw)

    def test_the_same_run_filed_twice_lands_once(self):
        # The stamp is the start of the valuation day and not the instant the
        # button was pressed: the id is a hash of the content, so "now" would
        # give a double-clicked morning a new id and count it twice in every
        # width it touches.
        self.assertEqual(self._file(counterparty="BrokerA")["added"], 1)
        second = self._file(counterparty="BrokerA")
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["already_held"], 1)

    def test_the_same_run_under_another_broker_is_new_and_says_so(self):
        # Two brokers really showing the same market is stronger evidence than
        # one, so it is a new record -- and it is also the obvious way to
        # double a width by accident, so it is counted out loud.
        self._file(counterparty="BrokerA")
        again = self._file(counterparty="BrokerB")
        self.assertEqual(again["added"], 1)
        self.assertEqual(again["under_another_name"], 1)
        self.assertTrue(any("different broker name" in n for n in again["notes"]),
                        again["notes"])

    def test_filing_nothing_is_refused(self):
        from volkit.agent import AgentError
        self.payload["text"] = "   "
        with self.assertRaises(AgentError):
            self._file()

    def test_what_is_filed_is_marked_as_typed_by_hand(self):
        self._file()
        held = self.archive.query(pair="EURUSD")
        self.assertEqual(held[0].via, "hand")
        self.assertIn("market-maker screen", held[0].origin)

def _zipped(text: str, name: str = "CFTC_CUMULATIVE_FOREX_2026_08_25.csv") -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


_SDR_CSV = (
    "Dissemination Identifier,Action type,Execution Timestamp,Expiration Date,Strike Price,"
    "Option Type,Option Premium Amount,Option Premium Currency,Notional amount-Leg 1,"
    "Notional currency-Leg 1,Underlier ID-Leg 1\n"
    "900,NEWT,2026-08-25T14:05:11Z,2026-10-21,1.1000,CALL,902157,USD,100000000,EUR,EUR/USD\n")


class _Server:
    """A DTCC that answers from memory, so the downloader is testable offline."""

    def __init__(self, body=None, status=200, content_type="application/zip"):
        self.body = _zipped(_SDR_CSV) if body is None else body
        self.status = status
        self.content_type = content_type
        self.asked: list[str] = []
        self.fail_first = 0
        self.direct = False

    def __call__(self, url, *, timeout, proxy, user_agent="", direct=False):
        from volkit import dtcc
        self.direct = direct
        self.asked.append(url)
        if url.endswith(".csv"):
            return dtcc.Response(url=url, status=404, body=b"")
        if self.fail_first > 0:
            self.fail_first -= 1
            return dtcc.Response(url=url, status=503, body=b"busy")
        return dtcc.Response(url=url, status=self.status, body=self.body,
                             content_type=self.content_type)


class TestDtccDownload(unittest.TestCase):
    """Getting the public dissemination files, without a network."""

    def setUp(self):
        from volkit import dtcc
        self.dtcc = dtcc
        self.folder = tempfile.mkdtemp()
        self.today = date(2026, 8, 26)
        self.server = _Server()
        self.down = dtcc.Downloader()
        self.down.opener = self.server
        self.down.sleeper = lambda _s: None

    def test_a_date_outside_what_dtcc_keeps_is_refused_before_any_request(self):
        # "That is older than DTCC keeps" is a sentence.  As a 404 it is a
        # thing the caller has to interpret, and usually interprets as broken.
        out = self.down.fetch([date(2022, 5, 1)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "refused")
        self.assertIn("2023-12-29", out.days[0].why)
        self.assertEqual(self.server.asked, [], "nothing should have been asked for")

    def test_today_is_refused_with_the_reason_and_the_alternative(self):
        out = self.down.fetch([self.today], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "refused")
        self.assertIn("after the session", out.days[0].why)

    def test_a_404_on_a_kept_date_is_nothing_published_not_a_failure(self):
        # Two days in seven have no session.  A run that shouts on every
        # weekend is a run nobody reads.
        self.server.status = 404
        out = self.down.fetch([date(2026, 8, 22)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "nothing published")
        self.assertEqual(out.failed, [])

    def test_a_login_page_with_a_200_is_not_a_file(self):
        # The failure a proxy or a captive portal produces: status 200, HTML
        # body.  Trusting the status writes that HTML into the SDR folder,
        # where the reader meets it tomorrow.
        self.server.body = b"<html><body>Please sign in</body></html>"
        self.server.content_type = "text/html; charset=utf-8"
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "failed")
        self.assertIn("web page", out.days[0].why)
        self.assertFalse(list(Path(self.folder).glob("*.zip")), "nothing should be on disk")

    def test_a_zip_with_no_csv_in_it_is_refused(self):
        self.server.body = _zipped("nope", name="readme.bin")
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "failed")
        self.assertIn("no CSV", out.days[0].why)

    def test_a_file_already_held_is_not_fetched_again(self):
        # The folder is the cache, deliberately: it is also exactly what
        # sdr.py reads and what a person can open.
        first = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        asked = len(self.server.asked)
        second = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(first.written, 1)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.days[0].status, "held")
        self.assertEqual(len(self.server.asked), asked, "it asked again for a file it had")

    def test_a_5xx_is_retried_and_a_404_is_not(self):
        # A 503 means "later"; a 404 means there is no such file, and asking
        # again more slowly does not create one.
        self.server.fail_first = 2
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "written")
        self.server.asked.clear()
        self.server.status = 404
        self.down.fetch([date(2026, 8, 24)], self.folder, today=self.today)
        zips = [u for u in self.server.asked if u.endswith(".zip")]
        self.assertEqual(len(zips), 1, f"a 404 was retried: {self.server.asked}")

    def test_every_url_it_tried_is_named_when_none_answers(self):
        self.server.status = 500
        self.down.retries = 1
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "failed")
        self.assertIn("https://", out.days[0].why)

    def test_the_proxy_is_named_when_it_is_the_thing_that_refused(self):
        def refuse(url, *, timeout, proxy, user_agent="", direct=False):
            raise self.dtcc.DtccError(f"could not reach {url} through the proxy {proxy}: no")
        self.down.opener = refuse
        self.down.proxy = "http://desk-proxy:8080"
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertIn("desk-proxy:8080", out.days[0].why)

    def test_a_system_proxy_nobody_named_is_still_named_on_the_failure(self):
        # The bug: WinError 10061 arrived reading "could not reach
        # https://pddata.dtcc.com/...", as though the attempt had gone
        # straight out, while urllib had in fact dialled the proxy in the
        # Windows registry -- which is what refused.  ``default_proxy`` reads
        # the environment only, so nothing in the tool ever named it.
        import urllib.error
        import urllib.request
        real_get, real_bypass = urllib.request.getproxies, urllib.request.proxy_bypass
        real_build = urllib.request.build_opener

        class _Refuses:
            def open(self, *a, **k):
                raise urllib.error.URLError(
                    ConnectionRefusedError(10061, "no connection could be made because "
                                                  "the target machine actively refused it"))

        urllib.request.getproxies = lambda: {"https": "http://127.0.0.1:8080"}
        urllib.request.proxy_bypass = lambda host: False
        urllib.request.build_opener = lambda *h: _Refuses()
        try:
            with self.assertRaises(self.dtcc.DtccError) as caught:
                self.dtcc.urllib_opener("https://pddata.dtcc.com/x.zip", timeout=1, proxy=None)
        finally:
            urllib.request.getproxies = real_get
            urllib.request.proxy_bypass = real_bypass
            urllib.request.build_opener = real_build
        said = str(caught.exception)
        self.assertIn("127.0.0.1:8080", said)          # the thing that actually refused
        self.assertIn("--no-proxy", said)              # and the way past it

    def test_a_direct_refusal_says_a_proxy_may_be_the_missing_piece(self):
        # The other half of the same diagnosis: no proxy anywhere, the
        # connection refused before it left the building.  urllib does not
        # execute a PAC script, so a desk configured that way goes direct
        # here while its browser does not.
        import urllib.error
        import urllib.request
        real_get, real_build = urllib.request.getproxies, urllib.request.build_opener

        class _Refuses:
            def open(self, *a, **k):
                raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        urllib.request.getproxies = lambda: {}
        urllib.request.build_opener = lambda *h: _Refuses()
        try:
            with self.assertRaises(self.dtcc.DtccError) as caught:
                self.dtcc.urllib_opener("https://pddata.dtcc.com/x.zip", timeout=1,
                                        proxy=None, direct=True)
        finally:
            urllib.request.getproxies = real_get
            urllib.request.build_opener = real_build
        said = str(caught.exception)
        self.assertIn("--proxy", said)
        self.assertIn("Scan folders", said)            # the offline way in

    def test_a_drop_is_not_diagnosed_as_a_refusal(self):
        # A timeout (WinError 10060) is a different fault with a different
        # cure, and offering the refusal's advice for it would be a guess.
        import urllib.error
        import urllib.request
        real_get, real_build = urllib.request.getproxies, urllib.request.build_opener

        class _Times:
            def open(self, *a, **k):
                raise urllib.error.URLError(TimeoutError("timed out"))

        urllib.request.getproxies = lambda: {}
        urllib.request.build_opener = lambda *h: _Times()
        try:
            with self.assertRaises(self.dtcc.DtccError) as caught:
                self.dtcc.urllib_opener("https://pddata.dtcc.com/x.zip", timeout=1, proxy=None)
        finally:
            urllib.request.getproxies = real_get
            urllib.request.build_opener = real_build
        self.assertNotIn("--no-proxy", str(caught.exception))

    def test_direct_ignores_every_proxy_the_environment_names(self):
        import urllib.request
        real_get = urllib.request.getproxies
        urllib.request.getproxies = lambda: {"https": "http://127.0.0.1:8080"}
        try:
            self.assertIsNone(self.dtcc.effective_proxy("https://x/y", None, direct=True))
            self.assertEqual(self.dtcc.effective_proxy("https://x/y", None),
                             "http://127.0.0.1:8080")
            # An explicitly named one still wins over the system's.
            self.assertEqual(self.dtcc.effective_proxy("https://x/y", "http://named:3128"),
                             "http://named:3128")
        finally:
            urllib.request.getproxies = real_get

    def test_a_direct_downloader_says_so_and_asks_for_no_proxy(self):
        self.down.direct = True
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        self.assertEqual(out.days[0].status, "written")
        self.assertTrue(self.server.direct)            # it reached the opener
        self.assertIn("--no-proxy", self.down.route)

    def test_weekends_are_not_asked_for(self):
        days = self.dtcc.business_days(date(2026, 8, 21), date(2026, 8, 25))
        self.assertEqual([d.isoformat() for d in days],
                         ["2026-08-21", "2026-08-24", "2026-08-25"])

    def test_what_it_writes_is_what_the_reader_reads(self):
        # The two modules meet here and nowhere else, so this is the join
        # worth pinning: a file straight off the wire, read without unzipping.
        from volkit import sdr
        out = self.down.fetch([date(2026, 8, 25)], self.folder, today=self.today)
        read = sdr.read_sdr(out.days[0].path, known_pairs=["EURUSD"])
        self.assertEqual(len(read.records), 1)
        self.assertEqual(read.records[0].pair, "EURUSD")
        self.assertEqual(read.records[0].notional_ccy, "EUR")
        self.assertIn(".csv", read.records[0].origin)


class TestPremiumInversion(unittest.TestCase):
    """A printed premium turned into a volatility, or refused by name."""

    def setUp(self):
        from volkit import synthesis
        self.syn = synthesis
        self.archive = arch.Archive.load(_tmp("arc.jsonl"))
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
        self.when = datetime(2026, 8, 25, 14, 5, tzinfo=UTC)

    def _history(self, last=date(2026, 8, 25)):
        import numpy as np
        from volkit.history import PairHistory
        hp = PairHistory(pair="EURUSD")
        hp.dates = [last]
        hp.spot = np.array([1.0850])
        hp.forwards = {"1M": np.array([1.0862]), "3M": np.array([1.0888])}
        return hp

    def _trade(self, **kw):
        body = dict(kind="trade", pair="EURUSD", at=arch._iso(self.when),
                    instrument="outright", strike=1.10, is_call=True, premium=902157.0,
                    premium_ccy="USD", notional=100_000_000.0, notional_ccy="EUR",
                    expiry_date="2026-10-21", action="NEWT", external_id="900", via="sdr")
        body.update(kw)
        obs = arch.Observation(**body)
        ok, why = self.archive.add(obs)
        self.assertTrue(ok, why)
        return obs

    def _invert(self, **kw):
        return self.syn.invert_trades(self.archive, "EURUSD", asof=self.now,
                                      hist_pair=self._history(), **kw)

    def test_a_premium_built_from_a_known_volatility_comes_back_as_that_volatility(self):
        # The whole chain: forward off the sheet, day count, currency
        # convention, inversion.  Built from a vol so the answer is known.
        from volkit import black
        hp = self._history()
        days = self.syn.days_of("2026-10-21", asof=self.when)
        forward, _ = self.syn._forward_for_trade(hp, self.when, days)
        truth = 0.0825
        premium = float(black.price(forward, 1.10, truth, days / self.syn.DAYS_IN_YEAR,
                                    True)) * 100_000_000.0
        self._trade(premium=premium)
        vols, _ = self._invert()
        self.assertEqual(len(vols), 1)
        self.assertAlmostEqual(vols[0].vol, truth * 100.0, places=6)

    def test_a_premium_in_the_base_currency_gives_the_same_volatility(self):
        from volkit import black
        hp = self._history()
        days = self.syn.days_of("2026-10-21", asof=self.when)
        forward, _ = self.syn._forward_for_trade(hp, self.when, days)
        truth = 0.0825
        domestic = float(black.price(forward, 1.10, truth, days / self.syn.DAYS_IN_YEAR, True))
        self._trade(premium=domestic * 100_000_000.0)
        # The same instant, so the day count is identical and the only thing
        # differing is the currency the premium was paid in.
        self._trade(external_id="901", premium=domestic / forward * 100_000_000.0,
                    premium_ccy="EUR")
        vols, _ = self._invert()
        self.assertEqual(len(vols), 2)
        self.assertAlmostEqual(vols[0].vol, vols[1].vol, places=6)

    def test_a_stale_historical_row_is_refused_not_reached_for(self):
        # "Last row on or before" would take a forward from two years ago
        # without a word, which is the silent substitution this refuses.
        self._trade()
        vols, notes = self.syn.invert_trades(
            self.archive, "EURUSD", asof=self.now,
            hist_pair=self._history(last=date(2024, 2, 28)))
        self.assertEqual(vols, [])
        self.assertTrue(any("too old to invert against" in n for n in notes), notes)

    def test_no_history_at_all_refuses_rather_than_using_todays_forward(self):
        self._trade()
        vols, notes = self.syn.invert_trades(self.archive, "EURUSD", asof=self.now)
        self.assertEqual(vols, [])
        self.assertTrue(any("no historical workbook" in n for n in notes), notes)

    def test_a_capped_notional_is_never_inverted(self):
        self._trade(notional_capped=True)
        vols, notes = self._invert()
        self.assertEqual(vols, [])
        self.assertTrue(any("cap" in n for n in notes), notes)

    def test_a_notional_on_the_wrong_leg_is_refused_by_name(self):
        self._trade(notional_ccy="USD")
        vols, notes = self._invert()
        self.assertEqual(vols, [])
        self.assertTrue(any("base is EUR" in n for n in notes), notes)

    def test_a_premium_in_a_third_currency_is_refused(self):
        self._trade(premium_ccy="JPY")
        vols, notes = self._invert()
        self.assertEqual(vols, [])
        self.assertTrue(any("neither leg" in n for n in notes), notes)

    def test_a_cancelled_print_is_not_business_that_got_done(self):
        self._trade(action="CANC", external_id="905")
        vols, _ = self._invert()
        self.assertEqual(vols, [])

    def test_the_undiscounted_reading_is_the_lower_one_and_says_so(self):
        from volkit import black
        hp = self._history()
        days = self.syn.days_of("2026-10-21", asof=self.when)
        forward, _ = self.syn._forward_for_trade(hp, self.when, days)
        self._trade(premium=float(black.price(forward, 1.10, 0.0825,
                                              days / self.syn.DAYS_IN_YEAR, True)) * 1e8)
        plain, _ = self._invert()
        discounted, _ = self._invert(discount_rate=0.04)
        self.assertLess(plain[0].vol, discounted[0].vol)
        self.assertIn("undiscounted", plain[0].why)
        self.assertIn("carries no rate curve", plain[0].why)

    def test_every_inverted_row_names_the_forward_it_used(self):
        from volkit import black
        hp = self._history()
        days = self.syn.days_of("2026-10-21", asof=self.when)
        forward, _ = self.syn._forward_for_trade(hp, self.when, days)
        self._trade(premium=float(black.price(forward, 1.10, 0.0825,
                                              days / self.syn.DAYS_IN_YEAR, True)) * 1e8)
        vols, notes = self._invert()
        self.assertIn(f"{forward:.6g}", vols[0].why)
        self.assertIn("historical sheet", vols[0].source)
        self.assertTrue(any("midnight UTC" in n for n in notes), notes)

# ==========================================================================
class _Model:
    """A local model that answers what the test tells it to."""

    def __init__(self, reply: str):
        self.reply = reply
        self.asked: list[tuple[str, str]] = []
        self.why_not = ""
        self.config = llm.ModelConfig()

    def available(self, *, recheck=False):
        return True

    def complete(self, system, user, *, timeout=None):
        self.asked.append((system, user))
        return llm.Reply(text=self.reply, ok=True, model="fake")


class TestAskGrammar(unittest.TestCase):
    """The third agent's reading of a question."""

    def setUp(self):
        from volkit import ask
        self.ask = ask

    def test_a_question_becomes_a_query(self):
        q = self.ask.parse_question(
            "how wide has the 3M 25d fly been shown this month, and by whom", pair="EURUSD")
        self.assertEqual(q.topics, ["widths"])
        self.assertEqual((q.pair, q.tenor, q.instrument, q.delta), ("EURUSD", "3M", "fly", 0.25))
        self.assertEqual(q.lookback_days, 31.0)
        self.assertTrue(q.who)

    def test_a_delta_is_not_a_tenor(self):
        # ``25d`` read as a twenty-five day tenor: the first thing the grammar
        # got wrong.  The delta is read and taken out before the tenor is.
        q = self.ask.parse_question("where is the 25d rr quoted", pair="EURUSD")
        self.assertEqual(q.delta, 0.25)
        self.assertIsNone(q.tenor)
        q = self.ask.parse_question("where is the 1M 25d rr quoted", pair="EURUSD")
        self.assertEqual((q.tenor, q.delta), ("1M", 0.25))

    def test_an_english_phrase_is_not_a_pair(self):
        # ``THE ATM`` is three capitals, a space and three capitals, and read
        # as a pair every question about the at-the-money was about THEATM.
        q = self.ask.parse_question("how wide is the atm", pair="USDJPY")
        self.assertEqual(q.pair, "USDJPY")
        q = self.ask.parse_question("how wide is the eur/usd atm", pair="USDJPY")
        self.assertEqual(q.pair, "EURUSD", "a pair in the question beats the default")
        q = self.ask.parse_question("how wide is the atm in audusd", known_pairs=["AUDUSD"])
        self.assertEqual(q.pair, "AUDUSD")

    def test_been_shown_is_the_market_and_not_us(self):
        # "how wide has it been shown" is a widths question; "shown" as a
        # topic word made it also a question about our own prices.
        q = self.ask.parse_question("how wide has the 1M been shown", pair="EURUSD")
        self.assertEqual(q.topics, ["widths"])
        q = self.ask.parse_question("what did we show in the 1M", pair="EURUSD")
        self.assertEqual(q.topics, ["shown"])

    def test_a_follow_up_fills_only_its_gaps_and_says_so(self):
        first = self.ask.parse_question("how wide has the 1M atm been shown this week",
                                        pair="EURUSD")
        q = self.ask.parse_question("and the 3M?", previous=first)
        self.assertEqual(q.topics, ["widths"])
        self.assertEqual((q.pair, q.tenor, q.instrument), ("EURUSD", "3M", "atm"))
        self.assertEqual(q.lookback_days, 7.0)
        self.assertIn("topic", q.inherited)
        self.assertTrue(any("taken from the question before" in n for n in q.notes))
        # A question that names its own topic inherits nothing but the pair.
        q = self.ask.parse_question("what printed last month", previous=first)
        self.assertEqual(q.topics, ["trades"])
        self.assertEqual(q.pair, "EURUSD")
        self.assertIsNone(q.tenor)

    def test_a_window_is_not_a_tenor(self):
        q = self.ask.parse_question("what printed in the last 30 days", pair="EURUSD")
        self.assertEqual(q.lookback_days, 30.0)
        self.assertIsNone(q.tenor)
        q = self.ask.parse_question("what printed since 2026-08-01", pair="EURUSD")
        self.assertEqual(q.since, "2026-08-01")

    def test_the_topics_the_grammar_hears_are_the_ones_the_answer_builds(self):
        self.assertEqual(set(self.ask._TOPIC_WORDS), set(self.ask.TOPICS))
        self.assertEqual(set(self.ask._TOPIC_HELP), set(self.ask.TOPICS))


class TestAskAgent(unittest.TestCase):
    """Reads everything, writes nothing, and says where each fact came from."""

    def setUp(self):
        from volkit import ask, remarks
        self.ask = ask
        self.arc_path = _tmp("arc.jsonl")
        self.archive = arch.Archive.load(self.arc_path)
        for i, ago in enumerate((0, 0.5, 1, 2)):
            when = MORNING - timedelta(days=ago)
            ok, why = self.archive.add(arch.Observation(
                kind="quote", pair="EURUSD", at=arch._iso(when), instrument="atm",
                tenor="1M", bid=8.20, ask=8.60, counterparty=f"broker{i % 2}"))
            self.assertTrue(ok, why)
        self.archive.flush()
        self.journal_path = _tmp("j.jsonl")
        self.journal = remarks.Journal.load(self.journal_path)

    def _ask(self, text, **kw):
        kw.setdefault("journal", self.journal)
        return self.ask.ask(text, archive=self.archive, pair="EURUSD", asof=MORNING, **kw)

    def test_a_width_question_is_answered_from_the_archive_with_sources(self):
        out = self._ask("how wide has the 1M atm been shown this week, and by whom")
        self.assertTrue(out.ok, out.refused)
        self.assertTrue(all(f.source == "archive" for f in out.facts), out.facts)
        self.assertTrue(any("shown 0.400 wide" in f.text for f in out.facts), out.fact_lines())
        self.assertTrue(any("broker0 (2), broker1 (2)" in f.text for f in out.facts),
                        out.fact_lines())
        self.assertEqual(out.model_note, "no model")

    def test_a_turn_writes_nothing(self):
        # The whole reason this is a third agent.  The archive and the journal
        # are byte-identical after a question about every topic there is.
        before = (Path(self.arc_path).read_bytes(), Path(self.journal_path).exists())
        records = len(self.archive.records)
        for text in ("how wide is the 1M atm", "where is the 1M atm quoted",
                     "what printed last week", "what became of our prices",
                     "what did we show", "what do you hold", "who moved the mark",
                     "what does this desk do", "what is in the bank"):
            out = self._ask(text)
            self.assertTrue(out.ok, (text, out.refused))
            self.assertTrue(out.facts, text)
        self.assertEqual((Path(self.arc_path).read_bytes(), Path(self.journal_path).exists()),
                         before)
        self.assertEqual(len(self.archive.records), records)
        self.assertEqual(len(self.journal), 0)

    def test_a_clients_record_is_answered_by_name_and_the_names_are_listed(self):
        # The quote applies a client's record; the record agent reads the
        # same evidence out in words, and it never guesses a name: one the
        # archive does not know is answered with the names it does.
        for h in (1, 2, 3, 4):
            when = MORNING - timedelta(hours=h)
            price = arch.shown("EURUSD", instrument="atm", tenor="1M", bid=8.20, ask=8.60,
                               counterparty="Fund A", at=when)
            self.archive.add(price)
            self.archive.add(arch.outcome(price, "traded_ask", at=when + timedelta(minutes=5)))
        out = self._ask("what has client Fund A done in the 1M?")
        self.assertTrue(out.ok, out.refused)
        self.assertEqual(out.question.client, "Fund A")
        self.assertTrue(any("a buyer here" in f.text for f in out.facts), out.fact_lines())
        self.assertTrue(all(f.source == "archive" for f in out.facts), out.facts)
        out = self._ask("is fund a a buyer or a seller of the atm")
        self.assertTrue(any("a buyer here" in f.text for f in out.facts), out.fact_lines())
        out = self._ask("what has client Nobody Ltd done with us")
        self.assertTrue(out.ok, out.refused)
        self.assertTrue(any("Fund A" in f.text and "nothing is held for 'Nobody Ltd'" in f.text
                            for f in out.facts), out.fact_lines())
        out = self._ask("which clients do we have a record on")
        self.assertTrue(any("clients with a record -- Fund A" in f.text for f in out.facts),
                        out.fact_lines())

    def test_the_tape_is_answered_in_words_and_takes_no_side_without_a_surface(self):
        # No trade in this archive: the answer says so and names the fetch.
        out = self._ask("who has been paying in the 1M")
        self.assertTrue(out.ok, out.refused)
        self.assertEqual(out.question.topics, ["flow"])
        self.assertTrue(any("nothing printed" in f.text for f in out.facts), out.fact_lines())
        self.assertTrue(any("no surface is loaded" in f.text for f in out.facts),
                        out.fact_lines())

    def test_doing_is_handed_off_by_name(self):
        for text, where in (("fetch the dtcc files for the last 3 days", "volkit agent fetch"),
                            ("re-mark the 1M atm to 8.4", "marking agent"),
                            ("record that as shown", "volkit agent shown"),
                            ("quote me the 1M atm in 100mm", "volkit agent quote")):
            out = self._ask(text)
            self.assertFalse(out.ok, text)
            self.assertIn(where, out.refused)
            self.assertEqual(out.facts, [])

    def test_a_question_about_nothing_it_knows_is_refused_with_the_list(self):
        out = self._ask("what is the weather in london")
        self.assertFalse(out.ok)
        for topic in self.ask.TOPICS:
            self.assertIn(topic, out.refused)

    def test_a_pair_is_needed_and_the_archive_summary_is_the_exception(self):
        out = self.ask.ask("how wide is the 1M", archive=self.archive, asof=MORNING)
        self.assertFalse(out.ok)
        self.assertIn("needs a currency pair", out.refused)
        out = self.ask.ask("what do you hold", archive=self.archive, asof=MORNING)
        self.assertTrue(out.ok, out.refused)
        self.assertTrue(any("EURUSD: 4 record(s)" in f.text for f in out.facts), out.fact_lines())

    def test_trades_as_volatilities_need_the_history_and_say_so(self):
        when = MORNING - timedelta(days=1)
        ok, why = self.archive.add(arch.Observation(
            kind="trade", pair="EURUSD", at=arch._iso(when), instrument="outright",
            tenor="3M", expiry_date="2026-11-19", strike=1.10, is_call=True,
            premium=12000.0, premium_ccy="USD", notional=1e7, notional_ccy="EUR",
            source="sdr", external_id="T1", action="NEWT"))
        self.assertTrue(ok, why)
        out = self._ask("what printed in the 3M last week and what vol does it imply")
        self.assertTrue(out.ok, out.refused)
        self.assertTrue(any("1.1 call" in f.text and "premium 12,000 USD" in f.text
                            for f in out.facts), out.fact_lines())
        self.assertTrue(any("historical workbook" in f.text and f.source == "note"
                            for f in out.facts), out.fact_lines())
        # A different tenor bucket finds nothing, and does not borrow the 3M.
        out = self._ask("what printed in the 1Y last week")
        self.assertTrue(any("nothing printed" in f.text for f in out.facts), out.fact_lines())

    def test_the_surface_is_optional_and_a_failure_to_load_it_is_a_note(self):
        out = self._ask("where is the surface marked", book=None)
        self.assertTrue(any("no workbook is loaded" in f.text for f in out.facts))

        def broken():
            raise RuntimeError("no workbook at /nowhere")

        out = self._ask("where is the surface marked", book=broken)
        self.assertTrue(any("no workbook at /nowhere" in n for n in out.notes), out.notes)

    def test_the_model_may_rewrite_a_question_but_never_answer_it(self):
        # A question the grammar cannot read goes to the model to be rewritten
        # into the grammar's own words, and the grammar then reads *that*.
        model = _Model("widths 1M ATM this week who")
        out = self._ask("gimme the picture on the 1M atm, who is showing it", model=model,
                        narrate=False)
        self.assertTrue(out.ok, out.refused)
        self.assertEqual(out.question.topics, ["widths"])
        self.assertEqual(out.question.rewritten, "widths 1M ATM this week who")
        self.assertEqual(out.question.text, "gimme the picture on the 1M atm, who is showing it")
        self.assertTrue(out.used_model)
        # A rewrite with a number the question did not have is refused whole:
        # "the front end" is not 1M until a person says so.
        model = _Model("widths 1M ATM this week")
        out = self._ask("gimme the picture on the front end", model=model, narrate=False)
        self.assertFalse(out.ok)
        self.assertTrue(any("contained 1" in n for n in out.notes), out.notes)

    def test_a_narration_with_an_invented_number_is_dropped_and_the_facts_stay(self):
        model = _Model("The 1M has been shown 0.400 wide by two brokers, about 5% of the level.")
        out = self._ask("how wide is the 1M atm", model=model)
        self.assertTrue(out.ok)
        self.assertEqual(out.narration, "")
        self.assertIn("5", out.narration_why)
        self.assertTrue(out.facts)
        model = _Model("The 1M ATM has been shown 0.400 wide over 4 observations.")
        out = self._ask("how wide is the 1M atm", model=model)
        self.assertTrue(out.narration.startswith("The 1M ATM"))
        self.assertTrue(out.used_model)

    def test_a_posted_transcript_is_reparsed_and_never_trusted(self):
        # The browser owns the conversation and posts it whole; the last
        # question is rebuilt from its *text*, so a transcript cannot carry a
        # pair or a topic the grammar would not have read.
        conv = self.ask.Conversation.from_json([
            {"q": "how wide has the eurusd 1M atm been shown", "a": {"ok": True,
                                                                      "pair": "USDJPY"}},
            {"q": "what is the weather", "a": {"ok": False}},
        ])
        self.assertEqual(conv.last.pair, "EURUSD")
        self.assertEqual(conv.last.topics, ["widths"])
        self.assertEqual(len(conv.turns), 1)

    def test_the_cli_reproduces_the_answer_and_writes_nothing(self):
        import contextlib
        from volkit import cli
        before = Path(self.arc_path).read_bytes()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--asof", MORNING.isoformat(), "agent", "ask", "EURUSD",
                             "how", "wide", "is", "the", "1M", "atm", "this", "week",
                             "--archive", self.arc_path, "--journal", self.journal_path,
                             "--knowledge", _tmp("bank.json"), "--no-llm", "--json"])
        self.assertEqual(code, 0, buf.getvalue())
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertTrue(any("shown 0.400 wide" in f["text"] for f in out["facts"]), out)
        self.assertEqual(Path(self.arc_path).read_bytes(), before)
        self.assertFalse(Path(self.journal_path).exists())


# ==========================================================================
class TestFlow(unittest.TestCase):
    """The tape, and the one inference in the package.

    The dissemination file publishes no buyer and no seller.  Everything here
    turns on that: a side is decided against our own mark, a print near the
    mark is not evidence, and size is vega rather than notional.
    """

    NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    def archive(self, rows=()):
        a = arch.Archive(path=_tmp("flow.jsonl"))
        a.extend(list(rows))
        return a

    def _trade(self, *, days, strike, is_call, premium, notional=100_000_000.0,
               ident="1", at=None):
        when = at or (self.NOW - timedelta(days=1))
        expiry = (when + timedelta(days=days)).date().isoformat()
        return arch.Observation(
            kind="trade", pair="EURUSD", at=when.isoformat(timespec="seconds"),
            instrument="outright", tenor=expiry, expiry_date=expiry,
            strike=strike, is_call=is_call, premium=premium, premium_ccy="USD",
            notional=notional, notional_ccy="EUR", action="NEWT", event="TRAD",
            external_id=ident, via="sdr", source="sdr")

    def _forward(self, *, days, rate=1.10, ident="f1", at=None):
        when = at or (self.NOW - timedelta(days=1))
        expiry = (when + timedelta(days=days)).date().isoformat()
        return arch.Observation(
            kind="forward", pair="EURUSD", at=when.isoformat(timespec="seconds"),
            instrument="atm", tenor=expiry, expiry_date=expiry, rate=rate,
            action="NEWT", event="TRAD", external_id=ident, via="sdr", source="sdr")

    def _priced(self, vol, *, days=30, strike=1.10, is_call=True, **kw):
        """A trade whose premium is exactly what ``vol`` implies, so the test
        knows what the inversion must give back."""
        from volkit import black
        years = days / 365.2425
        px = float(black.price(1.10, strike, vol / 100.0, years, is_call))
        return self._trade(days=days, strike=strike, is_call=is_call,
                           premium=px * kw.pop("notional", 100_000_000.0), **kw)

    def test_a_print_above_the_mark_is_paid_and_one_below_is_given(self):
        a = self.archive([self._forward(days=30), self._forward(days=90, ident="f2"),
                          self._priced(9.0, ident="1"),
                          self._priced(5.0, ident="2", strike=1.1001)])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        sides = {p.side for p in read.prints}
        self.assertEqual(sides, {"paid", "given"})
        ev = read.for_days(30)
        self.assertEqual((ev.paid, ev.given), (1, 1))
        # Equal and opposite vega at the same strike and tenor nets to nothing.
        self.assertAlmostEqual(ev.net_vega, 0.0, delta=abs(ev.gross_vega) * 0.02)

    def test_a_print_near_the_mark_takes_no_side(self):
        """Every market has a mid somebody disagrees with.  A print two
        hundredths over ours is not evidence of demand."""
        a = self.archive([self._forward(days=30), self._priced(7.02, ident="1")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        self.assertEqual([p.side for p in read.prints], ["unclear"])
        self.assertEqual(read.for_days(30).unclear, 1)

    def test_size_is_vega_and_not_notional(self):
        """A hundred million of a one-week option and a hundred million of a
        one-year one are not the same amount of buying."""
        a = self.archive([self._forward(days=7), self._forward(days=400, ident="f2"),
                          self._priced(9.0, days=7, ident="1"),
                          self._priced(9.0, days=360, ident="2")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        by = {p.bucket: p for p in read.prints}
        self.assertLess(by["out to a week"].vega, by["out to a year"].vega / 3.0)

    def test_thin_evidence_leans_nothing(self):
        a = self.archive([self._forward(days=30), self._priced(9.0, ident="1")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW, min_effective=2.0,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        ev = read.for_days(30)
        self.assertFalse(ev.enough)
        self.assertIsNone(ev.net(5_000_000.0))
        self.assertIn("floor", ev.why_not)

    def test_with_no_mark_nothing_takes_a_side(self):
        """A pair whose surface is not built still gets a census of what
        printed; what it does not get is a direction."""
        a = self.archive([self._forward(days=30), self._priced(9.0, ident="1")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW, mark_vol=None,
                                  lookback_days=30)
        self.assertEqual([p.side for p in read.prints], ["unmarked"])
        self.assertEqual(read.for_days(30).paid, 0)

    def test_the_forward_comes_off_the_tape_when_no_sheet_covers_the_pair(self):
        """The inversion needs the forward of the trade's own date.  On a pair
        the historical workbook has never held, the outrights printed in the
        same file are the only place that forward exists."""
        a = self.archive([self._forward(days=30), self._priced(8.0, ident="1")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        self.assertEqual(len(read.prints), 1)
        # Not to the last decimal: the file publishes an expiry *date* and no
        # cut, so the life is measured to midnight UTC and comes out a few
        # hours short of the one the premium was built with.  That is the
        # approximation the read says it makes, and this is its size.
        self.assertAlmostEqual(read.prints[0].vol, 8.0, delta=0.15)
        self.assertIn("tape", read.prints[0].forward)
        self.assertTrue(any("midnight UTC" in n for n in read.notes), read.notes)

    def test_a_forward_is_not_extended_beyond_what_printed(self):
        """Holding the last rate flat would price a two-year option off a
        two-week forward, and on a pegged pair the carry is the trade."""
        a = self.archive([self._forward(days=20), self._forward(days=30, ident="f2"),
                          self._priced(8.0, days=700, ident="1")])
        read = flow_mod.read_flow(a, "EURUSD", asof=self.NOW,
                                  mark_vol=lambda days, k, c, f: 7.0, lookback_days=30)
        self.assertEqual(read.prints, [])
        self.assertTrue(any("nobody quoted" in n for n in read.notes), read.notes)


if __name__ == "__main__":
    unittest.main()
