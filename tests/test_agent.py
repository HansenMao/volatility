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
        # so the quote would fall silently through to the panel fallback.
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

    def test_an_ambiguous_date_is_not_guessed(self):
        # 03/04/2026 is four weeks apart in the two conventions and the tenor
        # a trade lands on is the whole point of keeping it.
        self.assertEqual(sdr._date("03/04/2026"), "")
        self.assertEqual(sdr._date("2026-09-21"), "2026-09-21")


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
class TestAsks(unittest.TestCase):
    """Reading what was asked for."""

    def setUp(self):
        from volkit import agent
        self.agent = agent

    def test_25d_is_a_delta_on_a_risk_reversal_and_an_expiry_otherwise(self):
        # Read as a tenor unconditionally, "3M 25d RR" became a risk reversal
        # with no delta and an expiry nobody asked for.
        asks, _, _ = self.agent.parse_asks("3M 25d RR\n25d ATM\n")
        self.assertEqual(asks[0].tenor, "3M")
        self.assertEqual(asks[0].delta, 0.25)
        self.assertEqual(asks[1].tenor, "25D")
        self.assertIsNone(asks[1].delta)

    def test_25_delta_written_with_a_space_is_one_token(self):
        asks, _, _ = self.agent.parse_asks("1M 25 delta rr\n")
        self.assertEqual(asks[0].delta, 0.25)

    def test_a_price_on_a_request_line_is_refused(self):
        # A line with a two-way on it is a market somebody showed, and taking
        # it here would quote over the top of it without reading it.
        asks, _, skipped = self.agent.parse_asks("1M ATM 8.20/8.60\n")
        self.assertEqual(asks, [])
        self.assertIn("market-maker screen", skipped[0][1])

    def test_a_wing_without_a_delta_is_refused(self):
        _, _, skipped = self.agent.parse_asks("3M RR\n")
        self.assertIn("needs a delta", skipped[0][1])

    def test_an_unqualified_fly_takes_the_convention_and_says_so(self):
        asks, notes, _ = self.agent.parse_asks("2M 25d fly\n")
        self.assertEqual(asks[0].fly_kind, "market")
        self.assertTrue(any("market convention" in n for n in notes), notes)

    def test_a_request_carries_no_price_into_the_evaluator(self):
        # nan rather than zero: a zero bid and offer is a market of zero, and
        # every width and hinge downstream would take it at face value.
        ask = self.agent.parse_asks("1M ATM\n")[0][0]
        quote = ask.as_quote()
        self.assertNotEqual(quote.bid, quote.bid)      # nan
        self.assertNotEqual(quote.ask, quote.ask)


class _FakeEvaluator:
    """A surface that says one number, so the ladder can be tested without one."""

    def __init__(self, vol_points: float = 8.40):
        self.vol = vol_points / 100.0

    def value(self, quote, expiries, forwards):
        return self.vol


class TestDecision(unittest.TestCase):
    """The width ladder, the shading and the trace, with no surface involved."""

    def setUp(self):
        from volkit import agent, marketmaker
        self.agent = agent
        self.clock_now = MORNING
        from volkit.timeutil import Clock
        self.clock = Clock(MORNING)
        self.marketmaker = marketmaker

    def _decide(self, text="1M ATM in 100mm vega", *, rules=(), synthesis=None,
                request=None, rich=None, axe=None, model_mid=8.40):
        asks, _, _ = self.agent.parse_asks(text)
        ask = asks[0]
        quote = ask.as_quote()
        expiries = self.marketmaker.resolve_expiries(self.clock, [quote])
        forwards = {k: 1.10 for k in expiries}
        pk = PairKnowledge(rules=list(rules))
        empty = synthesis or syn.Synthesis(pair="EURUSD", asof=MORNING)
        req = request or self.agent.Request(pair="EURUSD")
        return self.agent._decide(
            ask, quote, pair="EURUSD", evaluator=_FakeEvaluator(model_mid),
            expiries=expiries, forwards=forwards, pk=pk, synthesis=empty,
            request=req, rich_at=rich, axe_at=axe, method="SVI")

    def test_no_rule_and_no_evidence_means_no_price(self):
        # There is no built-in default width anywhere in this package.
        out = self._decide()
        self.assertFalse(out.priced)
        self.assertEqual(out.width_source, "none")
        self.assertEqual(out.quote_text(), "no price")
        self.assertTrue(out.warnings)

    def test_a_bank_rule_beats_the_archive(self):
        evidence = syn.Synthesis(pair="EURUSD", asof=MORNING, widths=[
            syn.WidthEvidence(instrument="atm", bucket="out to a month", delta=None,
                              observations=9, effective=6.0, sources=3, median=0.80,
                              low=0.7, high=0.9, tightest=0.7, widest=0.9,
                              newest_days=0.0, oldest_days=3.0, model_read=0, enough=True)])
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm")],
                           synthesis=evidence)
        self.assertEqual(out.width_source, "bank")
        self.assertAlmostEqual(out.width, 0.40)

    def test_the_archive_is_used_when_the_bank_has_nothing(self):
        evidence = syn.Synthesis(pair="EURUSD", asof=MORNING, widths=[
            syn.WidthEvidence(instrument="atm", bucket="out to a month", delta=None,
                              observations=9, effective=6.0, sources=3, median=0.44,
                              low=0.4, high=0.5, tightest=0.4, widest=0.5,
                              newest_days=0.0, oldest_days=3.0, model_read=0, enough=True)])
        out = self._decide(synthesis=evidence)
        self.assertEqual(out.width_source, "archive")
        self.assertAlmostEqual(out.width, 0.44)
        self.assertTrue(any("not from the bank" in a for a in out.advice), out.advice)

    def test_the_fallback_is_the_last_rung_and_says_so(self):
        request = self.agent.Request(pair="EURUSD", fallback_spread=0.5)
        out = self._decide(request=request)
        self.assertEqual(out.width_source, "fallback")
        self.assertTrue(out.priced)

    def test_a_floor_that_did_not_bind_is_shown_as_not_applied(self):
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm"),
                                  Rule(kind="floor", value=0.20)])
        floor = [i for i in out.trace if i.name == "floor"][0]
        self.assertFalse(floor.applied)
        self.assertAlmostEqual(out.width, 0.40)

    def test_a_floor_that_binds_widens_the_quote_and_names_itself(self):
        out = self._decide(rules=[Rule(kind="spread", value=0.10, instrument="atm"),
                                  Rule(kind="floor", value=0.30)])
        self.assertAlmostEqual(out.width, 0.30)
        self.assertAlmostEqual(out.offer - out.bid, 0.30, places=9)

    def test_a_note_is_shown_and_never_applied(self):
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm"),
                                  Rule(kind="note", text="check the ECB date")])
        self.assertIn("check the ECB date", out.advice)
        self.assertAlmostEqual(out.mid, 8.40, places=9)

    def test_the_trace_adds_up_to_the_mid(self):
        # The explanation is generated from the trace, so a trace that does
        # not reconcile is an explanation of a different price.
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm"),
                                  Rule(kind="shift", value=0.05, instrument="atm")],
                           rich=lambda t: 0.002)
        parts = {i.name: i.value for i in out.trace}
        total = (parts["shading, fair value"] + parts["shading, position"]
                 + parts["shift, bank"])
        self.assertAlmostEqual(out.model_mid + total, out.mid, places=9)
        self.assertAlmostEqual((out.bid + out.offer) / 2.0, out.mid, places=9)

    def test_the_archive_level_flags_the_mark_and_does_not_move_it(self):
        evidence = syn.Synthesis(pair="EURUSD", asof=MORNING, levels=[
            syn.LevelEvidence(instrument="atm", tenor="1M", delta=None, observations=6,
                              effective=4.0, typical=9.90, newest=9.90, newest_days=0.0,
                              low=9.8, high=10.0, enough=True)])
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm")],
                           synthesis=evidence)
        self.assertAlmostEqual(out.mid, 8.40, places=9)
        self.assertTrue(any("not applied to it" in f for f in out.flags), out.flags)
        level = [i for i in out.trace if i.name == "market level"][0]
        self.assertFalse(level.applied)

    def test_a_stale_archive_level_says_it_is_stale(self):
        evidence = syn.Synthesis(pair="EURUSD", asof=MORNING, levels=[
            syn.LevelEvidence(instrument="atm", tenor="1M", delta=None, observations=6,
                              effective=4.0, typical=8.41, newest=8.41, newest_days=40.0,
                              low=8.3, high=8.5, enough=True)])
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm")],
                           synthesis=evidence)
        self.assertTrue(any("stale" in f for f in out.flags), out.flags)

    def test_the_facts_are_what_the_explanation_may_say(self):
        out = self._decide(rules=[Rule(kind="spread", value=0.40, instrument="atm")])
        facts = out.facts()
        allowed = set()
        for line in facts:
            allowed |= llm.numbers_in(line)
        self.assertIn(llm._canonical(f"{out.bid:.3f}"), allowed)
        self.assertIn(llm._canonical(f"{out.offer:.3f}"), allowed)


class TestRecordShown(unittest.TestCase):

    def test_a_price_is_recorded_with_the_mid_it_was_made_from(self):
        # Looked up when the outcome arrives instead, the question "was our
        # market right that morning" is answered by a curve re-marked since.
        from volkit import agent
        run = agent.AgentRun(pair="EURUSD")
        ask = agent.parse_asks("1M ATM in 100mm vega")[0][0]
        run.decisions.append(agent.Decision(ask=ask, pair="EURUSD", model_mid=8.40,
                                            mid=8.40, bid=8.20, offer=8.60, width=0.40,
                                            width_source="bank"))
        a = arch.Archive.load(_tmp("arc.jsonl"))
        written = agent.record_shown(a, run, counterparty="CptyX", at=MORNING)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].kind, "shown")
        self.assertEqual(written[0].model_mid, 8.40)
        answer = arch.outcome(written[0], "traded_ask", at=MORNING)
        self.assertEqual(answer.ref, written[0].id)
        self.assertEqual(answer.tenor, "1M")

    def test_an_unpriced_row_is_not_recorded(self):
        from volkit import agent
        run = agent.AgentRun(pair="EURUSD")
        ask = agent.parse_asks("1M ATM")[0][0]
        run.decisions.append(agent.Decision(ask=ask, pair="EURUSD", model_mid=8.40))
        a = arch.Archive.load(_tmp("arc.jsonl"))
        self.assertEqual(agent.record_shown(a, run), [])


class _FakeBook:
    """Only the clock: the width comparison never touches a surface."""

    def __init__(self, now=MORNING):
        from volkit.timeutil import Clock
        self.clock = Clock(now)


class TestSuggestCard(unittest.TestCase):
    """The card inside the market-maker tab: the bank against the archive."""

    def setUp(self):
        from volkit import agent
        self.agent = agent
        self.book = _FakeBook()
        self.archive = arch.Archive.load(_tmp("arc.jsonl"))

    def _seen(self, width: float, days=(0, 0.5, 1, 2), instrument="atm",
              tenor="1M", **kw):
        for i, ago in enumerate(days):
            when = MORNING - timedelta(days=ago)
            body = dict(kind="quote", pair="EURUSD", at=arch._iso(when),
                        instrument=instrument, tenor=tenor,
                        bid=8.40 - width / 2, ask=8.40 + width / 2,
                        counterparty=f"broker{i}")
            body.update(kw)
            ok, why = self.archive.add(arch.Observation(**body))
            self.assertTrue(ok, why)

    def _run(self, text="1M ATM 8.20/8.60", rules=(), **kw):
        bank = KnowledgeBank()
        if rules:
            bank.set_pair("EURUSD", list(rules), MORNING, "test")
        payload = dict(pair="EURUSD", text=text, fly_convention="market",
                       vol_unit="auto", half_life=5, min_effective=2,
                       lookback_days=90, include_model_read=True, tolerance=0.1)
        payload.update(kw)
        return self.agent.panel_from_request(payload).run(self.book, self.archive, bank)

    # ----------------------------------------------------------------------
    def test_a_bank_width_the_archive_supports_is_left_alone(self):
        # A screen with an opinion about every row is a screen nobody reads.
        self._seen(0.40)
        out = self._run(rules=[Rule(kind="spread", value=0.41, instrument="atm")])
        self.assertEqual(out["rows"][0]["verdict"], "agrees")

    def test_a_bank_width_tighter_than_the_market_is_flagged(self):
        self._seen(0.44)
        out = self._run(rules=[Rule(kind="spread", value=0.30, instrument="atm")])
        row = out["rows"][0]
        self.assertEqual(row["verdict"], "tight")
        self.assertLess(row["gap"], 0)
        self.assertIn("tighter", row["note"])

    def test_a_bank_width_wider_than_the_market_is_flagged_too(self):
        self._seen(0.30)
        out = self._run(rules=[Rule(kind="spread", value=0.60, instrument="atm")])
        self.assertEqual(out["rows"][0]["verdict"], "wide")
        self.assertIn("wider", out["rows"][0]["note"])

    def test_a_small_gap_on_a_narrow_instrument_is_not_a_disagreement(self):
        # Without the floor a 0.08 butterfly width "disagrees" over four
        # thousandths, which is a flag on every wing row forever.
        self._seen(0.08, instrument="fly", tenor="2M", delta=0.25, bid=0.20, ask=0.28)
        out = self._run(text="2M 25d fly 0.20/0.28",
                        rules=[Rule(kind="spread", value=0.09, instrument="fly")])
        self.assertEqual(out["rows"][0]["verdict"], "agrees")

    def test_no_rule_says_what_the_archive_would_support(self):
        self._seen(0.44)
        out = self._run()
        row = out["rows"][0]
        self.assertEqual(row["verdict"], "no rule")
        self.assertIsNone(row["bank_width"])
        self.assertAlmostEqual(row["archive_width"], row["archive_width"])
        self.assertIn("no bank rule", row["note"])

    def test_thin_evidence_produces_no_comparison(self):
        self._seen(0.44, days=(0,))
        out = self._run(rules=[Rule(kind="spread", value=0.30, instrument="atm")])
        row = out["rows"][0]
        self.assertEqual(row["verdict"], "thin")
        self.assertIsNone(row["archive_width"])
        self.assertEqual(row["archive_observations"], 1)

    def test_an_empty_archive_flags_nothing_and_still_answers(self):
        out = self._run(rules=[Rule(kind="spread", value=0.30, instrument="atm")])
        self.assertEqual(out["rows"][0]["verdict"], "thin")
        self.assertEqual(out["widths"], [])
        self.assertEqual(out["archive"]["records"], 0)

    def test_a_superseded_quote_is_still_a_row(self):
        # One tenor quoted twice is two observations of how wide it is shown,
        # and the card is about width.
        self._seen(0.40)
        out = self._run(text="09:15 1M ATM 8.20/8.60\n09:41 1M ATM 8.25/8.65")
        self.assertEqual(len(out["rows"]), 2)
        self.assertTrue(any(r["superseded"] for r in out["rows"]))

    def test_the_rows_are_the_request_box_when_it_holds_anything(self):
        # The card sits beside the quote button and the desk read it as
        # answering about the market paste instead: with a request typed it
        # still compared the paste's rows.  The request box is what is being
        # asked for, so those are the rows; the paste supplies "their width"
        # beside a request it also quoted, matched the quote panel's way.
        self._seen(0.40)
        out = self._run(text="1M ATM 8.20/8.60\n3M ATM 8.00/8.50",
                        request_text="1M ATM\n2M 25d RR",
                        rules=[Rule(kind="spread", value=0.41, instrument="atm")])
        self.assertEqual(out["source"], "request")
        self.assertEqual([r["line"] for r in out["rows"]], [1, 2])
        atm, rr = out["rows"]
        self.assertEqual(atm["instrument"], "atm")
        self.assertAlmostEqual(atm["market_width"], 0.40)      # the paste's 1M
        self.assertEqual(atm["verdict"], "agrees")
        self.assertEqual(rr["instrument"], "rr")
        self.assertIsNone(rr["market_width"])                  # not in the paste
        self.assertEqual(rr["verdict"], "thin")

    def test_a_request_needs_no_market_paste(self):
        self._seen(0.40)
        out = self._run(text="", request_text="1M ATM",
                        rules=[Rule(kind="spread", value=0.30, instrument="atm")])
        self.assertEqual(out["rows"][0]["verdict"], "tight")
        self.assertIsNone(out["rows"][0]["market_width"])
        self.assertTrue(any("no market is pasted" in n for n in out["notes"]))

    def test_a_price_in_the_request_box_is_refused_not_quoted(self):
        self._seen(0.40)
        out = self._run(text="", request_text="1M ATM 8.20/8.60")
        self.assertEqual(out["rows"], [])
        self.assertTrue(out["skipped"], "the priced line should be reported")

    def test_the_paste_is_compared_only_when_nothing_is_asked_for(self):
        self._seen(0.40)
        out = self._run(text="1M ATM 8.20/8.60", request_text="")
        self.assertEqual(out["source"], "market")
        self.assertEqual(len(out["rows"]), 1)
        self.assertAlmostEqual(out["rows"][0]["market_width"], 0.40)

    def test_the_card_answers_with_nothing_pasted(self):
        self._seen(0.40)
        out = self._run(text="")
        self.assertEqual(out["rows"], [])
        self.assertTrue(out["widths"])
        self.assertTrue(any("nothing is pasted" in n for n in out["notes"]))

    def test_the_size_a_quote_carries_picks_the_rule(self):
        # The bank's ladder is size-conditioned and the comparison has to use
        # the rung the quote would really stand on.
        self._seen(0.44)
        out = self._run(text="1M ATM 8.20/8.60 in 100mm vega", rules=[
            Rule(kind="spread", value=0.30, instrument="atm"),
            Rule(kind="spread", value=0.45, instrument="atm", max_size=150.0,
                 size_basis="vega")])
        self.assertAlmostEqual(out["rows"][0]["bank_width"], 0.45)
        self.assertEqual(out["rows"][0]["verdict"], "agrees")

    def test_a_pair_is_required(self):
        from volkit.agent import AgentError
        with self.assertRaises(AgentError):
            self.agent.panel_from_request({"text": "1M ATM 8.2/8.6"})

    def test_a_non_numeric_setting_is_named(self):
        from volkit.agent import AgentError
        with self.assertRaises(AgentError) as caught:
            self.agent.panel_from_request({"pair": "EURUSD", "half_life": "soon"})
        self.assertIn("half_life", str(caught.exception))


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


if __name__ == "__main__":
    unittest.main()
