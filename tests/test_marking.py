"""The marking agent, the journal it learns from, and the exchange with the
quoting agent.

Structural properties only where a fit is involved: the numbers a least
squares lands on are scipy's business and pinning them would make this a test
of the optimiser.  What is pinned is what the module promises -- the book is
restored, a tendency needs a count behind it, a correction that scatters is
not applied, and a re-mark that breaks a tenor is reported as breaking it.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from volkit import consult, marking, remarks, rules, session
from volkit.book import Book
from volkit.marketmaker import CurveTarget
from volkit.timeutil import Clock, tenor_to_years

UTC = timezone.utc
WORKBOOK = Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx"
ASOF = Clock(datetime(2024, 2, 28, 12, 0, tzinfo=UTC))
NOW = ASOF.now


def _tmp(name: str) -> str:
    return str(Path(tempfile.mkdtemp()) / name)


def _snapshot(**curve) -> dict:
    base = {"initial_vol": 6.25, "long_term_vol": 6.95, "mean_reversion": 6.0,
            "short_addon": 0.6, "short_decay": 50.0}
    base.update(curve)
    return {"curve": base, "param_shifts": {}, "atm_overwrites": {},
            "smile_overwrites": {}, "events": [], "anchor_tenors": False}


# ==========================================================================
class TestDiff(unittest.TestCase):
    """A re-marking instance is a subtraction, not an instrumented control."""

    def test_a_knob_that_moved_is_found_with_what_was_proposed_beside_it(self):
        moved = remarks.diff_snapshots(_snapshot(), _snapshot(long_term_vol=7.30),
                                       _snapshot(long_term_vol=7.45))
        self.assertEqual(len(moved), 1)
        self.assertAlmostEqual(moved[0].move, 0.35)
        self.assertAlmostEqual(moved[0].correction, -0.15)

    def test_a_missing_smile_shift_is_a_zero_and_moving_it_is_a_move(self):
        # A shift that is absent *is* a shift of zero, so putting one on is a
        # knob moving.  Read as "no value" it would not count, and the desks
        # that shift wings would look like desks that never touch them.
        before, after = _snapshot(), _snapshot()
        after["param_shifts"] = {"rho25": 0.04}
        moved = remarks.diff_snapshots(before, after)
        self.assertEqual(len(moved), 1)
        self.assertAlmostEqual(moved[0].move, 0.04)

    def test_a_missing_overwrite_is_not_a_zero(self):
        # An absent tenor overwrite is the absence of an overwrite, not an
        # overwrite of zero: one is "they moved it", the other is "they left
        # the curve to speak", and counting them alike loses the difference.
        before, after = _snapshot(), _snapshot()
        after["atm_overwrites"] = {"1M": 6.15}
        moved = remarks.diff_snapshots(before, after)
        self.assertEqual(len(moved), 1)
        self.assertIsNone(moved[0].move)
        self.assertIsNone(moved[0].before)

    def test_clearing_an_overwrite_is_an_instance(self):
        before, after = _snapshot(), _snapshot()
        before["atm_overwrites"] = {"1M": 6.15}
        moved = remarks.diff_snapshots(before, after)
        self.assertEqual([c.key for c in moved], ["atm_overwrites.1M"])


class TestJournal(unittest.TestCase):

    def _entry(self, **kw):
        body = dict(pair="EURUSD", before=_snapshot(), after=_snapshot(long_term_vol=7.3),
                    proposed=_snapshot(long_term_vol=7.45), verdict="edited", at=NOW)
        body.update(kw)
        return remarks.instance(**body)

    def test_the_same_instance_twice_lands_once(self):
        j = remarks.Journal.load(_tmp("j.jsonl"))
        entry = self._entry()
        self.assertEqual(j.add(entry), (True, ""))
        self.assertEqual(j.add(entry)[0], False)

    def test_it_reads_back_what_it_wrote(self):
        path = _tmp("j.jsonl")
        j = remarks.Journal.load(path)
        j.add(self._entry())
        self.assertEqual(j.flush(), 1)
        back = remarks.Journal.load(path)
        self.assertEqual(len(back), 1)
        self.assertAlmostEqual(back.entries[0].changes()[0].correction, -0.15)

    def test_a_verdict_that_answers_a_proposal_must_have_one(self):
        # "accepted" with nothing proposed records the agent agreeing with
        # itself, which would be the highest-value row in the journal and
        # entirely fictional.
        bad = self._entry(verdict="accepted", proposed=None)
        self.assertTrue(any("answers a proposal" in p for p in bad.problems()))

    def test_answered_is_what_separates_a_verdict_from_a_diff(self):
        self.assertTrue(self._entry(verdict="edited").answered)
        self.assertFalse(self._entry(verdict="unprompted", proposed=None).answered)


# ==========================================================================
class TestLearning(unittest.TestCase):
    """Tendencies: a few statements with counts, or silence."""

    def _journal(self, n: int, *, correction=-0.12, jitter=0.01, move_addon=False):
        j = remarks.Journal.load(_tmp("j.jsonl"))
        for i in range(n):
            proposed = 6.95 + 0.40 + 0.01 * i
            landed = proposed + correction + (jitter if i % 2 else -jitter)
            after = _snapshot(long_term_vol=landed)
            if move_addon:
                after["curve"]["short_addon"] = 0.6 + 0.05 * (1 if i % 2 else -1)
            j.add(remarks.instance("EURUSD", _snapshot(), after,
                                   proposed=_snapshot(long_term_vol=proposed),
                                   verdict="edited", at=NOW - timedelta(days=n - i)))
        return j

    def test_under_the_floor_nothing_is_claimed(self):
        t = marking.learn(self._journal(3), "EURUSD", asof=NOW)
        got = t.get("curve", "long_term_vol")
        self.assertFalse(got.enough)
        self.assertIn("under the floor", got.why_not)
        self.assertFalse(got.reluctant)

    def test_a_knob_never_moved_over_enough_instances_is_reluctant(self):
        t = marking.learn(self._journal(9), "EURUSD", asof=NOW)
        self.assertIn("mean_reversion", t.reluctant_knobs())
        self.assertNotIn("long_term_vol", t.reluctant_knobs())

    def test_a_tight_correction_becomes_a_bias(self):
        t = marking.learn(self._journal(9), "EURUSD", asof=NOW)
        value, why = t.get("curve", "long_term_vol").bias()
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, -0.12, places=1)
        self.assertIn("9 answered proposal", why)

    def test_a_correction_that_scatters_is_not_a_bias(self):
        # The median of six corrections is a number whatever those six were.
        # It is only evidence when they agree, and this is the test that stops
        # the agent learning the desk's noise and quoting it back.
        t = marking.learn(self._journal(9, correction=0.02, jitter=0.30), "EURUSD", asof=NOW)
        value, why = t.get("curve", "long_term_vol").bias()
        self.assertIsNone(value)
        self.assertIn("both sides", why)

    def test_too_few_corrections_is_not_a_bias_however_tight(self):
        t = marking.learn(self._journal(3, jitter=0.0), "EURUSD", asof=NOW,
                          min_instances=1)
        value, why = t.get("curve", "long_term_vol").bias()
        self.assertIsNone(value)
        self.assertIn("floor", why)

    def test_a_knob_available_but_unmoved_still_counts_as_seen(self):
        # Counting only the knobs that moved would make every knob look like
        # one this desk always moves.
        t = marking.learn(self._journal(9), "EURUSD", asof=NOW)
        self.assertEqual(t.get("curve", "short_decay").seen, 9)
        self.assertEqual(t.get("curve", "short_decay").moved, 0)

    def test_an_empty_journal_says_so_rather_than_claiming_nothing_matters(self):
        t = marking.learn(remarks.Journal.load(_tmp("j.jsonl")), "EURUSD", asof=NOW)
        self.assertEqual(t.instances, 0)
        self.assertTrue(any("no re-marking instance" in n for n in t.notes), t.notes)


# ==========================================================================
def _rules(text: str) -> rules.RuleBook:
    path = _tmp("mm_rules.toml")
    Path(path).write_text(text, encoding="utf-8")
    return rules.RuleBook.load(path)


RULE = """
[[nudge_rule]]
section = "curve"
knob    = "long_term_vol"
scope   = { pair = "EURUSD" }
value   = %s
spread  = 0.06
weight  = %d
why     = "back end lags broker moves"
"""


class TestRulesOfThumb(unittest.TestCase):
    """A prior seeded into the sample, and the three guards on it."""

    def _journal(self, n: int, *, correction=-0.12, jitter=0.01):
        j = remarks.Journal.load(_tmp("j.jsonl"))
        for i in range(n):
            proposed = 6.95 + 0.40 + 0.01 * i
            landed = proposed + correction + (jitter if i % 2 else -jitter)
            j.add(remarks.instance("EURUSD", _snapshot(), _snapshot(long_term_vol=landed),
                                   proposed=_snapshot(long_term_vol=proposed),
                                   verdict="edited", at=NOW - timedelta(days=n - i)))
        return j

    def test_seeding_is_exact_at_every_allowed_weight(self):
        # _spread's quartile indices pick different rows at each n, so the
        # placement is pinned against the two functions themselves.
        for w in range(rules.MIN_PRIOR_WEIGHT, rules.MAX_PRIOR_WEIGHT + 1):
            got = rules.seed(0.10, 0.06, w)
            self.assertEqual(len(got), w)
            self.assertAlmostEqual(marking._median(got), 0.10, places=12, msg=f"weight {w}")
            self.assertAlmostEqual(marking._spread(got), 0.06, places=12, msg=f"weight {w}")
        for got, want in zip(rules.seed(0.10, 0.06, 4), [0.04, 0.07, 0.13, 0.16]):
            self.assertAlmostEqual(got, want, places=12)

    def test_a_rules_file_with_a_known_answer_loads(self):
        book = _rules(RULE % ("0.10", 4) + """
[[plan_rule]]
free_order = ["long_term_vol", "initial_vol"]
why = "the back end first"
""")
        self.assertEqual(len(book.nudges), 1)
        self.assertEqual(book.nudges[0].key, "curve.long_term_vol")
        self.assertEqual(book.nudges[0].scope, {"pair": "EURUSD"})
        self.assertTrue(book.nudges[0].applies("eurusd"))
        self.assertFalse(book.nudges[0].applies("USDJPY"))
        self.assertEqual(book.free_order("EURUSD")[1], ("long_term_vol", "initial_vol"))
        self.assertEqual(book.nudges_for("USDJPY"), [])

    def test_a_malformed_or_over_weighted_file_fails_loudly(self):
        # A weight of nine silently trimmed to five would be the agent taking
        # an order it was never given; a file that half loads is a set of
        # beliefs nobody can point at.
        with self.assertRaises(rules.RulesError) as cm:
            _rules(RULE % ("0.10", 9))
        self.assertIn("weight 9", str(cm.exception))
        with self.assertRaises(rules.RulesError):
            _rules(RULE % ("0.10", 1))
        with self.assertRaises(rules.RulesError):
            _rules("[[nudge_rule]]\nsection = 'curve'\nknob = 'x'\nvalue = 'ten'\nspread = 0.1")
        with self.assertRaises(rules.RulesError):
            _rules("[[hunch]]\nvalue = 1")
        with self.assertRaises(rules.RulesError):
            _rules("this is = = not toml")
        with self.assertRaises(rules.RulesError):
            _rules(RULE.replace("spread  = 0.06", "spread  = 0.0") % ("0.10", 3))
        missing = rules.RuleBook.load(_tmp("absent.toml"))
        self.assertEqual(len(missing), 0)
        self.assertTrue(any("no rules of thumb" in p for p in missing.problems))

    def test_a_prior_alone_cannot_authorise_a_nudge(self):
        # Zero real corrections: the rule is printed and nothing moves,
        # however confident the rule.  This is also what stops a weight of 4
        # clearing MIN_CORRECTIONS on its own, which it otherwise would.
        book = _rules(RULE % ("0.10", 5))
        empty = remarks.Journal.load(_tmp("j.jsonl"))
        t = marking.learn(empty, "EURUSD", asof=NOW, rules=book)
        self.assertEqual(t.prior_n, 5)
        self.assertEqual(t.instances, 0)
        got = t.get("curve", "long_term_vol")
        self.assertEqual(got.correction_n, 5)
        self.assertEqual(got.correction_real_n, 0)
        self.assertGreaterEqual(got.correction_n, marking.MIN_CORRECTIONS)
        value, why = got.bias()
        self.assertIsNone(value)
        self.assertIn("rule of thumb", why)
        self.assertIn("never authorise", why)
        self.assertIn("rule of thumb", got.describe())
        self.assertIn("plus 5 rule of thumb pseudo-instance(s)", t.learned_from())
        # and a proposal built on it says so, and applies nothing
        out = marking.propose(_Book.get(), "EURUSD", tendencies=t,
                              targets=_targets(("1M", 6.2), ("3M", 6.6), ("6M", 7.0)))
        self.assertEqual(out.corrections, [])
        self.assertTrue(any("pseudo-instance" in n for n in out.notes), out.notes)

    def test_a_prior_shapes_a_nudge_the_desk_supports_and_the_line_decomposes(self):
        book = _rules(RULE % ("-0.10", 4))
        t = marking.learn(self._journal(7), "EURUSD", asof=NOW, rules=book)
        got = t.get("curve", "long_term_vol")
        self.assertEqual(got.correction_real_n, 7)
        self.assertEqual(got.correction_n, 11)
        value, why = got.bias()
        self.assertIsNotNone(value)
        self.assertLess(value, 0)
        line = got.decompose()
        self.assertIn("rule of thumb", line)
        self.assertIn("desk (n=7)", line)
        # two medians and a subtraction, nothing else
        self.assertAlmostEqual(value, got.correction_real + (value - got.correction_real))
        out = marking.propose(_Book.get(), "EURUSD", tendencies=t,
                              targets=_targets(("1M", 6.2), ("3M", 6.6), ("6M", 7.0)))
        for c in out.corrections:
            self.assertEqual(c.source, "learned + rule of thumb")
            self.assertIn("desk (n=7)", c.prior)
        self.assertNotIn("plus 4", " ".join(n for n in out.notes if "rule of thumb" not in n))

    def test_fifteen_real_corrections_overwhelm_a_weight_five_rule(self):
        book = _rules(RULE % ("0.10", 5))
        t = marking.learn(self._journal(15), "EURUSD", asof=NOW, rules=book)
        value, _ = t.get("curve", "long_term_vol").bias()
        self.assertIsNotNone(value)
        self.assertLess(value, 0)                    # the desk's sign, not the rule's
        self.assertAlmostEqual(value, -0.12, places=1)

    def test_a_rule_far_off_the_desk_is_outvoted_at_sixteen(self):
        # The boundary, pinned rather than hidden.  With n real corrections
        # and a weight of 5, _spread reads its upper quartile at
        # ceil(0.75 (n + 4)); at n = 15 that is row 15, the first pseudo-
        # correction, so a rule half a point off the desk widens the spread
        # past the median and nothing is applied.  At sixteen the quartile is
        # the desk's own and the rule is outvoted.
        book = _rules(RULE % ("0.50", 5))
        blocked, why = marking.learn(self._journal(15), "EURUSD", asof=NOW,
                                     rules=book).get("curve", "long_term_vol").bias()
        self.assertIsNone(blocked)
        self.assertIn("both sides", why)
        value, _ = marking.learn(self._journal(16), "EURUSD", asof=NOW,
                                 rules=book).get("curve", "long_term_vol").bias()
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, -0.12, places=1)

    def test_a_rule_the_desk_edits_away_is_contested_and_nothing_is_retired(self):
        book = _rules(RULE % ("0.10", 4))
        t = marking.learn(self._journal(rules.CONTESTED_N), "EURUSD", asof=NOW, rules=book)
        (rep,) = t.rule_reports
        self.assertTrue(rep.contested)
        self.assertEqual(rep.far_side, rules.CONTESTED_N)
        self.assertLess(rep.real_median, 0)
        self.assertIn("CONTESTED", rep.line())
        self.assertTrue(any("contested" in n and "edit the file" in n for n in t.notes), t.notes)
        # still seeded, still weighted as written: flagged, not halved
        self.assertEqual(t.get("curve", "long_term_vol").correction_n, rules.CONTESTED_N + 4)
        under = marking.learn(self._journal(rules.CONTESTED_N - 1), "EURUSD", asof=NOW,
                              rules=book)
        self.assertFalse(under.rule_reports[0].contested)

    def test_without_rules_the_answer_is_the_desk_only_one(self):
        t = marking.learn(self._journal(7), "EURUSD", asof=NOW)
        self.assertEqual(t.prior_n, 0)
        self.assertEqual(t.rule_reports, [])
        self.assertIsNone(t.get("curve", "long_term_vol").prior)
        self.assertNotIn("rule of thumb", " ".join(t.lines()))

    def test_a_plan_rule_orders_the_free_set_and_cannot_widen_it(self):
        # Discrete: the order knobs are freed in.  Four targets still cannot
        # determine five parameters, whatever the file says.
        book = _rules("""
[[plan_rule]]
free_order = ["long_term_vol", "initial_vol", "short_addon", "mean_reversion", "no_such_knob"]
why = "the back end first"
""")
        t = marking.learn(remarks.Journal.load(_tmp("j.jsonl")), "EURUSD", asof=NOW, rules=book)
        plan = marking.plan_fit(_Book.get(), "EURUSD", tendencies=t, targets=_targets(("1M", 6.2)))
        self.assertEqual(plan.free, ("long_term_vol",))
        self.assertTrue(any(c.source == "rule of thumb" and c.what == "free order"
                            for c in plan.choices), plan.lines())
        self.assertTrue(any("no_such_knob" in n for n in plan.notes), plan.notes)
        plain = marking.plan_fit(_Book.get(), "EURUSD", targets=_targets(("1M", 6.2)))
        self.assertNotEqual(plain.free, plan.free)


# ==========================================================================
class _Book:
    """One loaded book, shared: building it is the slow part of these tests."""

    _book = None

    @classmethod
    def get(cls):
        if cls._book is None:
            cls._book = Book.from_excel(WORKBOOK, ASOF).load_all(["EURUSD"])
        return cls._book


def _targets(*rows):
    return [CurveTarget(tenor=t, t=tenor_to_years(t), vol=v / 100.0, source="test")
            for t, v in rows]


class TestPlan(unittest.TestCase):
    """Which knobs move, and whether a rule or the desk decided."""

    def setUp(self):
        self.book = _Book.get()

    def test_a_rule_and_a_learned_reason_are_labelled_apart(self):
        # A person disagreeing with the second should see immediately that it
        # is the second.
        plan = marking.plan_fit(self.book, "EURUSD", targets=_targets(("1M", 6.2), ("3M", 6.6),
                                                                     ("6M", 7.0), ("1Y", 7.4)))
        self.assertTrue(all(c.source in ("rule", "learned", "caller") for c in plan.choices))
        self.assertTrue(any(c.source == "rule" for c in plan.choices))

    def test_more_knobs_than_targets_pins_the_extras_by_rule(self):
        # Not a preference: freeing more parameters than there are targets
        # leaves a family of fits that all hit them, and the one that comes
        # back is whichever the optimiser wandered into.
        plan = marking.plan_fit(self.book, "EURUSD", targets=_targets(("1M", 6.2)))
        self.assertEqual(len(plan.free), 1)
        self.assertTrue(any("at most 1 parameter" in c.why for c in plan.choices),
                        plan.lines())

    def test_a_knob_this_desk_never_moves_is_pinned_and_says_it_was_learned(self):
        j = remarks.Journal.load(_tmp("j.jsonl"))
        for i in range(8):
            j.add(remarks.instance("EURUSD", _snapshot(),
                                   _snapshot(long_term_vol=7.0 + 0.01 * i),
                                   verdict="unprompted", at=NOW - timedelta(days=8 - i)))
        t = marking.learn(j, "EURUSD", asof=NOW)
        plan = marking.plan_fit(self.book, "EURUSD", tendencies=t,
                                targets=_targets(("1M", 6.2), ("3M", 6.6),
                                                 ("6M", 7.0), ("1Y", 7.4)))
        self.assertIn("mean_reversion", plan.pinned)
        self.assertTrue(any(c.source == "learned" and "mean_reversion" in c.what
                            for c in plan.choices), plan.lines())

    def test_the_caller_overrides_both_the_default_and_the_learned(self):
        plan = marking.plan_fit(self.book, "EURUSD", free=("long_term_vol",),
                                targets=_targets(("1M", 6.2), ("3M", 6.6)))
        self.assertEqual(plan.free, ("long_term_vol",))
        self.assertTrue(any(c.source == "caller" for c in plan.choices))

    def test_nothing_quoted_leaves_the_wings_alone(self):
        plan = marking.plan_fit(self.book, "EURUSD", targets=_targets(("1M", 6.2)))
        self.assertFalse(plan.tune_wings)
        self.assertTrue(any("does not constrain" in c.why or "curve's job" in c.why
                            for c in plan.choices))


class TestProposal(unittest.TestCase):

    def setUp(self):
        self.book = _Book.get()
        self.before = session.capture_pair(self.book, "EURUSD")

    def tearDown(self):
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before,
                         "a test left the book marked")

    def test_a_proposal_leaves_the_book_exactly_as_it_found_it(self):
        # The worst possible outcome of a tool whose whole job is marking is a
        # surface left half-marked by a proposal nobody accepted.
        out = marking.propose(self.book, "EURUSD",
                              targets=_targets(("1M", 6.2), ("3M", 6.6), ("6M", 7.0)))
        self.assertTrue(out.moved)
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def test_marked_puts_back_what_it_found_even_after_a_mess(self):
        # The context manager is what every proposal and every critique runs
        # inside, so this is the promise the whole module rests on: whatever
        # happens in there, the surface that comes out is the one that went in.
        with marking.marked(self.book, "EURUSD", _snapshot()):
            self.book["EURUSD"].atm.overwrite_tenor("2M", 0.09)
            self.book["EURUSD"].param_shifts["rho25"] = 0.5
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def test_a_restore_that_did_not_work_is_caught_and_not_hoped_for(self):
        # Fault injection: make the restore a no-op and check the guard fires.
        # "Report and then restore the book exactly" is the kind of invariant
        # that stays true right up until it does not, and the failure it
        # protects against is silent -- a surface left half-marked by a
        # proposal nobody accepted, priced off all morning.
        calls = []
        real = session.apply_block

        def once(surface, block):
            calls.append(block)
            return real(surface, block) if len(calls) == 1 else []

        with mock.patch.object(marking.session, "apply_block", once):
            with self.assertRaises(marking.MarkingError) as caught:
                with marking.marked(self.book, "EURUSD",
                                    {**self.before,
                                     "curve": {**self.before["curve"],
                                               "long_term_vol": 9.9}}):
                    pass
        self.assertIn("not restored", str(caught.exception))
        session.apply_block(self.book["EURUSD"], self.before)
        self.book["EURUSD"].invalidate()

    def test_every_proposal_says_how_much_it_learned_from(self):
        # A proposal that quietly had nothing to learn from and one built on a
        # year of instances must not read the same.
        out = marking.propose(self.book, "EURUSD", targets=_targets(("1M", 6.2)))
        self.assertTrue(any("nothing here is learned" in n for n in out.notes), out.notes)

    def test_a_learned_correction_is_capped_at_a_share_of_the_fit(self):
        # A nudge that can exceed the fit is a second fit with a smaller
        # sample behind it.
        j = remarks.Journal.load(_tmp("j.jsonl"))
        for i in range(9):
            proposed = 6.951 + 0.001 * i          # the fit barely moves it
            j.add(remarks.instance("EURUSD", _snapshot(),
                                   _snapshot(long_term_vol=proposed - 3.0),
                                   proposed=_snapshot(long_term_vol=proposed),
                                   verdict="edited", at=NOW - timedelta(days=9 - i)))
        t = marking.learn(j, "EURUSD", asof=NOW)
        self.assertIsNotNone(t.get("curve", "long_term_vol").bias()[0])
        out = marking.propose(self.book, "EURUSD", tendencies=t,
                              targets=_targets(("1M", 6.2), ("3M", 6.6), ("6M", 7.0)))
        for correction in out.corrections:
            fit_move = abs(correction.fitted - self.before["curve"][correction.knob])
            self.assertLessEqual(abs(correction.applied - correction.fitted),
                                 marking.CORRECTION_CAP * fit_move + 1e-9)

    def test_a_proposal_that_changes_nothing_says_so(self):
        atm = self.book["EURUSD"].atm
        here = [(t, atm.curve_vol(tenor_to_years(t)) * 100.0) for t in ("1M", "3M", "6M")]
        out = marking.propose(self.book, "EURUSD", targets=_targets(*here))
        self.assertTrue(any("nothing to do" in n for n in out.notes) or out.moved)


# ==========================================================================
class _Level:
    """A stand-in for one row of synthesis evidence."""

    def __init__(self, instrument, tenor, typical, low, high, observations=6,
                 newest_days=0.0, delta=None, enough=True):
        self.instrument, self.tenor, self.typical = instrument, tenor, typical
        self.low, self.high, self.observations = low, high, observations
        self.newest_days, self.delta, self.enough = newest_days, delta, enough


class _Syn:
    def __init__(self, levels):
        self.levels = levels


class TestConsult(unittest.TestCase):
    """What the two agents say to each other."""

    def setUp(self):
        self.book = _Book.get()
        self.before = session.capture_pair(self.book, "EURUSD")

    def tearDown(self):
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before,
                         "a test left the book marked")

    def _syn(self, *rows):
        return _Syn([_Level("atm", t, mid, mid - 0.1, mid + 0.1) for t, mid in rows])

    def test_points_the_surface_already_agrees_with_are_kept(self):
        # They are what damage is measured against; a loop that only saw its
        # own complaints could not notice it had broken something.
        atm = self.book["EURUSD"].atm
        here = atm.curve_vol(tenor_to_years("3M")) * 100.0
        found, _ = consult.findings_from(self.book, "EURUSD",
                                         self._syn(("1M", 9.0), ("3M", here)))
        self.assertEqual(len(found), 2)
        self.assertTrue(any(f.inside for f in found))
        self.assertTrue(any(not f.inside for f in found))

    def test_only_the_at_the_money_becomes_a_curve_target(self):
        # A risk reversal is a statement about shape; feeding one to a fit
        # that can only move the level asks a level to explain a skew.
        syn = _Syn([_Level("atm", "1M", 9.0, 8.9, 9.1),
                    _Level("rr", "3M", 0.3, 0.2, 0.4, delta=0.25)])
        found, _ = consult.findings_from(self.book, "EURUSD", syn)
        targets, weights = consult.targets_from(found)
        self.assertEqual([t.tenor for t in targets], ["1M"])
        self.assertEqual(len(weights), 1)

    def test_a_target_carries_the_evidence_that_produced_it(self):
        found, _ = consult.findings_from(self.book, "EURUSD", self._syn(("1M", 9.0)))
        targets, _ = consult.targets_from(found)
        self.assertIn("observation", targets[0].source)

    def test_the_score_counts_inside_not_distance_to_the_mid(self):
        # Otherwise the loop improves its own score by walking the surface
        # onto the middle of every market it has ever seen.
        rows = [consult.Row(key="atm.1M", describe="", before=6.0, after=6.05,
                            inside_before=True, inside_after=True,
                            gap_before=0.0, gap_after=0.0)]
        near = consult.Critique(rows=rows)
        rows2 = [consult.Row(key="atm.1M", describe="", before=6.0, after=6.00,
                             inside_before=True, inside_after=True,
                             gap_before=0.0, gap_after=0.0)]
        dead_on = consult.Critique(rows=rows2)
        self.assertEqual(near.score, dead_on.score)

    def test_a_proposal_that_breaks_a_point_is_reported_as_breaking_it(self):
        atm = self.book["EURUSD"].atm
        here = {t: atm.curve_vol(tenor_to_years(t)) * 100.0 for t in ("1M", "1Y")}
        # the surface currently sits inside both; move the long end a long way
        syn = self._syn(("1M", here["1M"]), ("1Y", here["1Y"]))
        found, _ = consult.findings_from(self.book, "EURUSD", syn)
        self.assertTrue(all(f.inside for f in found))
        wrecked = {**self.before, "curve": {**self.before["curve"],
                                            "long_term_vol": self.before["curve"]["long_term_vol"] + 4.0}}
        judged = consult.critique(self.book, "EURUSD", found, wrecked)
        self.assertTrue(judged.broke)
        self.assertIn("breaks", judged.verdict)

    def test_a_conference_leaves_the_book_alone_and_names_the_round_it_chose(self):
        conference = consult.confer(self.book, "EURUSD",
                                    self._syn(("1M", 6.2), ("3M", 6.6), ("6M", 7.0)),
                                    rounds=2)
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)
        self.assertTrue(conference.rounds)
        self.assertIsNotNone(conference.best)
        self.assertIn("chosen", "\n".join(conference.lines()))

    def test_a_surface_already_inside_every_market_is_left_alone(self):
        atm = self.book["EURUSD"].atm
        here = [(t, atm.curve_vol(tenor_to_years(t)) * 100.0) for t in ("1M", "3M")]
        conference = consult.confer(self.book, "EURUSD", self._syn(*here), rounds=2)
        self.assertEqual(conference.rounds, [])
        self.assertTrue(any("nothing for the marking agent" in n for n in conference.notes),
                        conference.notes)

    def test_an_empty_archive_produces_no_findings_and_says_why(self):
        conference = consult.confer(self.book, "EURUSD", _Syn([]), rounds=2)
        self.assertEqual(conference.findings, [])
        self.assertTrue(any("nothing to tell the marking agent" in n
                            for n in conference.notes), conference.notes)


class TestAskReadsTheSurfaceInPoints(unittest.TestCase):
    """The third agent against a real surface: points at the edge, and no marks moved."""

    def test_a_surface_fact_is_in_volatility_points_and_the_book_is_untouched(self):
        # ``curves`` is decimals throughout, and the first cut of the agent
        # printed "ATM 0.057" beside an archived "8.400".  One conversion, at
        # the agent's edge, and a test that the surface it read is the surface
        # it left.
        from volkit import archive as arch
        from volkit import ask
        book = _Book.get()
        before = session.capture_pair(book, "EURUSD")
        out = ask.ask("where is the surface marked in 1M", archive=arch.Archive.load(_tmp("a.jsonl")),
                      pair="EURUSD", book=book, asof=ASOF.now)
        self.assertTrue(out.ok, out.refused)
        line = next(f for f in out.facts if f.text.startswith("EURUSD 1m:") or f.text.startswith("EURUSD 1M:"))
        self.assertEqual(line.source, "surface")
        atm = float(line.text.split("ATM ")[1].split(",")[0])
        self.assertGreater(atm, 1.0, line.text)
        self.assertLess(atm, 100.0, line.text)
        self.assertEqual(session.capture_pair(book, "EURUSD"), before)


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
class TestCard(unittest.TestCase):
    """The marking-agent card on the market-maker tab, and its verdicts.

    The card is aimed at the fit panel's own inputs, hands back marks the
    quote panel takes as it takes a fit's, and writes nothing but the
    journal.  Numbers are the optimiser's; what is pinned is the contract.
    """

    RUN = ("1M ATM 6.20/6.60\n3M ATM 6.50/6.90\n1Y ATM 7.10/7.50\n"
           "3M 25d RR 0.35/0.55 eur call over\n2M 25d fly 0.20/0.28\n")

    def setUp(self):
        self.book = _Book.get()
        self.before = session.capture_pair(self.book, "EURUSD")
        self.journal = remarks.Journal.load(_tmp("j.jsonl"))

    def tearDown(self):
        session.apply_block(self.book["EURUSD"], self.before)
        self.book["EURUSD"].invalidate()
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def _run(self, **extra):
        payload = {"pair": "EURUSD", "text": self.RUN, "target_source": "quotes"}
        payload.update(extra)
        return marking.panel_from_request(payload).run(self.book, self.journal)

    def test_a_proposal_leaves_the_book_as_it_found_it_and_hands_back_marks(self):
        out = self._run()
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)
        self.assertTrue(out["proposal"]["moved"])
        # The marks are the fit panel's own shape, so the quote panel takes
        # them without knowing which of the two made them -- and says which.
        from volkit.marketmaker import quote_panel_from_request
        self.assertEqual(out["marks"]["pair"], "EURUSD")
        self.assertIn("marking agent", out["marks"]["what"])
        quoted = quote_panel_from_request({"pair": "EURUSD", "request_text": "1M ATM",
                                           "marks": out["marks"], "fallback_spread": 0.3}
                                          ).run(self.book)
        self.assertIn("marking agent", quoted["marks"]["note"])
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def test_the_agent_chooses_the_knobs_unless_told_not_to(self):
        chosen = self._run()
        self.assertTrue(any(c["source"] == "rule" and c["what"] == "free knobs"
                            for c in chosen["proposal"]["plan"]))
        told = self._run(choose_knobs=False, free=["long_term_vol"], smile_free=["rho25"])
        self.assertEqual(told["plan"]["free"], ["long_term_vol"])
        self.assertEqual(told["plan"]["smile_free"], ["rho25"])
        self.assertTrue(all(c["source"] == "caller" for c in told["proposal"]["plan"]
                            if c["what"] in ("free knobs", "wings")))

    def test_every_field_the_card_posts_is_read(self):
        # The page's list against this reader, the way MF and AF are pinned.
        import re as _re
        from pathlib import Path as _P
        root = _P(__file__).resolve().parents[1] / "volkit"
        js = (root / "web" / "index.html").read_text(encoding="utf-8")
        block = js.split("const MKF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("text", fields)
        self.assertIn("choose_knobs", fields)
        src = (root / "marking.py").read_text(encoding="utf-8")
        reader = src.split("def panel_from_request")[1]
        common = (root / "marketmaker.py").read_text(encoding="utf-8").split("def _common")[1]
        for f in fields | {"free", "smile_free"}:
            self.assertIn(f'"{f}"', reader + common, f"the card reader never reads {f!r}")

    def test_accepted_records_the_proposal_and_rejected_records_the_start(self):
        out = self._run()
        acc = marking.answer_from_request(self.journal, self.book,
                                          {"proposal": out["proposal"], "verdict": "accepted"},
                                          clock=ASOF)
        rej = marking.answer_from_request(self.journal, self.book,
                                          {"proposal": out["proposal"], "verdict": "rejected"},
                                          clock=ASOF)
        self.assertEqual(len(self.journal), 2)
        by_id = {e.id: e for e in self.journal.entries}
        self.assertEqual(by_id[acc["id"]].after, out["proposal"]["after"])
        self.assertEqual(by_id[rej["id"]].after, out["proposal"]["before"])
        self.assertTrue(all(e.answered for e in self.journal.entries))
        # Neither wrote to the book.
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def test_edited_needs_the_marks_the_desk_ended_on(self):
        out = self._run()
        with self.assertRaises(marking.MarkingError):
            marking.answer_from_request(self.journal, self.book,
                                        {"proposal": out["proposal"], "verdict": "edited"},
                                        clock=ASOF)
        marks = {**out["marks"], "knobs": dict(out["marks"]["knobs"])}
        marks["knobs"]["initial_vol"] += 0.05
        rec = marking.answer_from_request(
            self.journal, self.book,
            {"proposal": out["proposal"], "verdict": "edited", "marks": marks},
            clock=ASOF)
        entry = next(e for e in self.journal.entries if e.id == rec["id"])
        moved = {c.key: c for c in entry.changes()}
        self.assertAlmostEqual(moved["curve.initial_vol"].correction, 0.05, places=9)
        # Marks fitted on another pair are somebody else's edit.
        with self.assertRaises(marking.MarkingError):
            marking.answer_from_request(
                self.journal, self.book,
                {"proposal": out["proposal"], "verdict": "edited",
                 "marks": {**marks, "pair": "USDJPY"}}, clock=ASOF)

    def test_apply_is_the_only_way_a_verdict_reaches_the_book(self):
        out = self._run()
        marking.answer_from_request(self.journal, self.book,
                                    {"proposal": out["proposal"], "verdict": "accepted"},
                                    clock=ASOF)
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)
        rec = marking.answer_from_request(
            self.journal, self.book,
            {"proposal": out["proposal"], "verdict": "accepted", "apply": True, "note": "x"},
            clock=ASOF)
        self.assertTrue(rec["applied"])
        now = session.capture_pair(self.book, "EURUSD")
        self.assertEqual(now["curve"], out["proposal"]["after"]["curve"])
        # The same morning answered twice is one instance, not two.
        self.assertEqual(len(self.journal), 1)

    def test_a_verdict_the_screen_cannot_give_is_refused(self):
        out = self._run()
        with self.assertRaises(marking.MarkingError):
            marking.answer_from_request(self.journal, self.book,
                                        {"proposal": out["proposal"], "verdict": "unprompted"},
                                        clock=ASOF)
        with self.assertRaises(marking.MarkingError):
            marking.answer_from_request(self.journal, self.book, {"verdict": "accepted"},
                                        clock=ASOF)


class TestQuoteArchiveRung(unittest.TestCase):
    """The quoting agent on the quote's width ladder: bank, archive, fallback."""

    def setUp(self):
        from volkit import archive as arch
        self.book = _Book.get()
        self.before = session.capture_pair(self.book, "EURUSD")
        self.archive = arch.Archive.load(_tmp("a.jsonl"))
        # Three observations of the 1M at-the-money, shown at 0.40 wide, the
        # day before the valuation.  Nothing about the 3M.
        from volkit import quotes
        run = quotes.parse_quotes("1M ATM 6.20/6.60\n1M ATM 6.25/6.65\n1M ATM 6.30/6.70",
                                  pair="EURUSD")
        self.archive.extend(arch.from_quotes(run, pair="EURUSD", origin="t.txt",
                                             default_time=NOW - timedelta(days=1)))

    def tearDown(self):
        self.assertEqual(session.capture_pair(self.book, "EURUSD"), self.before)

    def _quote(self, **extra):
        from volkit.marketmaker import quote_panel_from_request
        payload = {"pair": "EURUSD", "request_text": "1M ATM\n3M ATM"}
        payload.update(extra)
        return quote_panel_from_request(payload).run(self.book, archive=self.archive)

    def test_off_by_default_the_archive_is_not_a_rung(self):
        out = self._quote()
        self.assertFalse(out["archive"]["used"])
        self.assertTrue(all(r["width"] is None for r in out["sheet"]["rows"]))

    def test_on_a_quote_no_rule_matches_takes_the_archived_width_and_names_it(self):
        out = self._quote(use_archive_width=True)
        one, three = out["sheet"]["rows"]
        self.assertAlmostEqual(one["width"], 0.40, places=6)
        self.assertTrue(one["width_source"].startswith("the archive"), one["width_source"])
        self.assertEqual(one["archive_observations"], 3)
        # The 3M has nothing behind it and gets nothing -- the ladder invents
        # no rung, and the fallback is still below the archive.
        self.assertIsNone(three["width"])
        out = self._quote(use_archive_width=True, fallback_spread=0.3)
        one, three = out["sheet"]["rows"]
        self.assertAlmostEqual(one["width"], 0.40, places=6)
        self.assertAlmostEqual(three["width"], 0.30, places=6)
        self.assertEqual(three["width_source"], "panel fallback")

    def test_the_archived_level_is_a_flag_and_moves_nothing(self):
        with_ = self._quote(use_archive_width=True)["sheet"]["rows"][0]
        without = self._quote()["sheet"]["rows"][0]
        self.assertEqual(with_["model"], without["model"])
        self.assertEqual(with_["our_mid"], without["our_mid"])
        self.assertIsNotNone(with_["archive_level"])
        for flag in with_["flags"]:
            self.assertIn("applied to nothing", flag)
