"""The bid-offer study: the spread measured off the tape, and the width built from it.

``volkit/tapespread.py`` and ``volkit/bidoffer.py``.  Everything here is synthetic: the study
reads the quant repo's tape and Bloomberg store in real use, and a test that needed them would
pass on one machine only.  What is pinned is the arithmetic each measurement rests on -- that the
estimator gets back a spread it was given, that one trade in pieces is one trade, that the parts
of a width are the width -- because a buffer nobody can take apart is a number nobody can argue
with.
"""

from ._support import *  # noqa: F401,F403

import pickle

from volkit import bidoffer as bo
from volkit import tapespread as tsp



def _prints(rows):
    d = pd.DataFrame(rows)
    d["ts"] = pd.to_datetime(d.ts, utc=True)
    d["capped"] = False
    d["pccy"] = d.get("pccy", "USD")
    return d


class TestTheTape(unittest.TestCase):

    def test_one_trade_reported_in_pieces_is_one_trade(self):
        base = {"pair": "EURUSD", "side": "call", "K": 1.10, "expiry": "2026-10-30", "T": 0.1,
                "pccy": "USD"}
        d = _prints([base | {"ts": "2026-09-01 10:00", "N": 50e6, "prem": 50e6 * 0.004},
                     base | {"ts": "2026-09-01 11:30", "N": 25e6, "prem": 25e6 * 0.004},
                     # the same option at another price is another trade
                     base | {"ts": "2026-09-01 11:40", "N": 25e6, "prem": 25e6 * 0.0041}])
        out, merged = tsp.merge_pieces(d)
        self.assertEqual(merged, 1)
        self.assertEqual(len(out), 2)
        self.assertEqual(out.N.max(), 75e6)

    def test_packages_are_told_apart(self):
        leg = {"pair": "EURUSD", "expiry": "2026-10-30", "T": 0.1, "pccy": "USD", "N": 50e6,
               "prem": 1e5}
        d = _prints([
            leg | {"ts": "2026-09-01 10:00", "side": "call", "K": 1.10},
            leg | {"ts": "2026-09-01 10:00", "side": "put", "K": 1.10},     # straddle
            leg | {"ts": "2026-09-01 11:00", "side": "call", "K": 1.13},
            leg | {"ts": "2026-09-01 11:00", "side": "put", "K": 1.07},     # strangle / RR
            leg | {"ts": "2026-09-01 12:00", "side": "call", "K": 1.12},    # single
        ])
        kinds = tsp.packages(d).kind.value_counts().to_dict()
        self.assertEqual(kinds, {"straddle": 1, "strangle": 1, "single": 1})

    def test_a_premium_inverts_to_the_volatility_that_made_it(self):
        F, T = np.array([1.1, 1.1]), np.array([0.25, 0.25])
        K = np.array([1.1, 1.15])
        v = np.array([0.08, 0.095])
        single = tsp._black(F, K, v, T, np.array([True, True]))
        back = tsp.implied(single, F, [(K, np.array([True, True]))], T)
        np.testing.assert_allclose(back, v, atol=1e-6)
        straddle = tsp._black(F, K, v, T, np.ones(2, bool)) + tsp._black(F, K, v, T,
                                                                           np.zeros(2, bool))
        back = tsp.implied(straddle, F, [(K, np.ones(2, bool)), (K, np.zeros(2, bool))], T)
        np.testing.assert_allclose(back, v, atol=1e-6)
        # a price below intrinsic has no volatility, and is NaN rather than the bracket's edge
        self.assertTrue(np.isnan(tsp.implied(np.array([0.0]), F[:1], [(K[:1] * 0.5,
                                                                       np.array([True]))],
                                             T[:1])[0]))

    def test_the_estimator_gets_back_the_spread_it_was_given(self):
        """Bounce of +-s/2 on independent sides plus drift growing with the gap: s comes back."""
        rng = np.random.default_rng(3)
        s, n = 0.20, 6000
        gap_h = rng.uniform(0.01, 1.5, n)
        sides = rng.choice([-1, 1], size=(n, 2))
        dv = s / 2 * (sides[:, 1] - sides[:, 0]) + rng.normal(0, np.sqrt(0.02 * gap_h))
        days = pd.to_datetime("2026-01-01") + pd.to_timedelta(rng.integers(0, 200, n), "D")
        p = pd.DataFrame({"gap": gap_h * 3600, "dv": dv, "day": days})
        e = tsp.estimate(p, boot=40)
        self.assertAlmostEqual(e.spread, s, delta=3 * e.se + 0.01)
        self.assertAlmostEqual(e.slope_per_hour, 0.02, delta=0.006)

    def test_too_few_pairs_measure_nothing(self):
        p = pd.DataFrame({"gap": [60.0] * 10, "dv": [0.1] * 10,
                          "day": pd.to_datetime(["2026-01-01"] * 10)})
        self.assertIsNone(tsp.estimate(p).spread)


def _ladder(rng, n=900, sd=0.3, start="2022-01-03"):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(8.0 + np.cumsum(rng.normal(0, sd, n)) * 0.2 + 2.0, index=idx)


def _study(rng=None, *, pairs=("EURUSD", "USDJPY"), cross=None):
    """A study with made-up histories and tape facts, for pairs the fixture book builds."""
    rng = rng or np.random.default_rng(11)
    ladders = {}
    for pair in pairs:
        ladders[(pair, "atm")] = {1 / 12: _ladder(rng), 0.25: _ladder(rng, sd=0.2)}
        ladders[(pair, "rr")] = {1 / 12: _ladder(rng, sd=0.1) - 8}
        ladders[(pair, "bf")] = {1 / 12: _ladder(rng, sd=0.03) - 9.8}
    if cross:
        c, (a, b), s = cross
        la, lb = ladders[(a, "atm")][1 / 12], ladders[(b, "atm")][1 / 12]
        rho = -0.4
        lc = np.sqrt(la ** 2 + lb ** 2 - 2 * s * rho * la * lb)
        ladders[(c, "atm")] = {1 / 12: lc}
        ladders[(c, "rr")] = {1 / 12: _ladder(rng, sd=0.1) - 8}
        ladders[(c, "bf")] = {1 / 12: _ladder(rng, sd=0.03) - 9.8}
    facts = bo.TapeFacts(
        flow_usd_mm_per_hour={(p, "1M"): 500.0 for p in pairs} | ({(cross[0], "1M"): 20.0}
                                                                 if cross else {}),
        wait_hours={(p, "1M"): 0.25 for p in pairs}, hours_per_day=10.0,
        impact={"150-400": {"excess_z2": 0.09, "n": 200, "lower_mm": 150, "upper_mm": 400}},
        leg_cost_bp={(p, "1M"): 1.2 for p in pairs}, pair_volume={p: 5000.0 for p in pairs})
    meta = {"median_ticket_usd_mm": 30.0, "wing10": 1.8, "tape_atm": {"USDMXN 1M": 15.0}}
    return bo.Study(facts, ladders, pd.DataFrame(), meta)


class TestTheWidth(unittest.TestCase):

    def test_a_25_delta_call_is_atm_plus_the_fly_plus_half_the_risk_reversal(self):
        self.assertEqual(dict(zip(bo.PILLARS, bo.weights("outright", 0.25))),
                         {"atm": 1.0, "rr25": 0.5, "bf25": 1.0, "rr10": 0.0, "bf10": 0.0})
        self.assertEqual(bo.weights("outright", 0.75)[1], -0.5)

    def test_a_move_inside_a_day_scales_with_the_share_of_the_day(self):
        rng = np.random.default_rng(5)
        daily = rng.normal(0, 1.0, 20000)
        full = bo.move_quantile(daily, 10.0, 0.68, 10.0)
        self.assertAlmostEqual(full, 1.0, delta=0.03)               # one sd at 68%
        self.assertAlmostEqual(bo.move_quantile(daily, 2.5, 0.68, 10.0), full * 0.5, delta=1e-9)
        self.assertAlmostEqual(bo.move_quantile(daily, 40.0, 0.68, 10.0), 2.0, delta=0.08)

    def test_history_is_read_in_todays_regime(self):
        """A calm year does not set the width of a stressed week."""
        rng = np.random.default_rng(9)
        moves = pd.Series(np.r_[rng.normal(0, 0.1, 500), rng.normal(0, 1.0, 200)])
        scaled = bo._fhs(moves)
        self.assertGreater(scaled.iloc[:400].std(), 0.6)            # the calm year, rescaled up

    def test_the_parts_are_the_width(self):
        study = _study()
        r = bo.quote_width(study, "EURUSD", 1 / 12, "outright", call_delta=0.25,
                           size_usd_mm=300.0)
        vp = {p["name"]: p["value"] for p in r.parts if p["unit"] == "vol points"}
        raw = vp["lay-off cost"] + 2 * (vp["move over the hold"] + vp["size impact"])
        self.assertAlmostEqual(r.raw, raw, places=12)
        # and the tick is the last part: the rest rounded up to the grid, never down
        self.assertAlmostEqual(r.width, raw + vp.get("tick", 0.0), places=12)
        self.assertGreaterEqual(r.width, raw)
        self.assertAlmostEqual(r.width / r.tick, round(r.width / r.tick), places=9)
        self.assertEqual(r.rung, "history + tape")

    def test_size_lengthens_the_hold_and_carries_its_impact(self):
        study = _study()
        small = bo.quote_width(study, "EURUSD", 1 / 12, "atm", size_usd_mm=20.0)
        big = bo.quote_width(study, "EURUSD", 1 / 12, "atm", size_usd_mm=300.0)
        self.assertGreater(big.hold_hours, small.hold_hours)
        self.assertEqual(small.impact, 0.0)
        self.assertGreater(big.impact, 0.0)
        self.assertGreater(big.width, small.width)
        self.assertEqual(big.layoff, small.layoff)                  # crossing costs the same

    def test_a_wing_costs_more_than_the_money(self):
        study = _study()
        atm = bo.quote_width(study, "EURUSD", 1 / 12, "outright", call_delta=0.5)
        wing = bo.quote_width(study, "EURUSD", 1 / 12, "outright", call_delta=0.1)
        self.assertGreater(wing.width, atm.width)

    def test_a_pair_with_no_history_takes_the_rule_of_thumb_at_the_tapes_own_level(self):
        study = _study()
        r = bo.quote_width(study, "USDMXN", 30 / 365, "atm")
        self.assertEqual(r.rung, "rule of thumb")
        vega = 2 * bo.vega_bp(30 / 365, 15.0, 0.5)
        self.assertAlmostEqual(r.raw, bo.RULE_OF_THUMB_BP / vega, places=12)
        self.assertAlmostEqual(r.width, bo.on_ticks(r.raw, 0.10), places=12)
        # and no history and no tape level is no width, said
        self.assertIsNone(bo.quote_width(study, "USDTRY", 30 / 365, "atm").width)

    def test_a_point_is_read_the_way_a_grid_writes_it(self):
        self.assertEqual(bo.parse_point("25c"), ("outright", {"call_delta": 0.25}))
        self.assertEqual(bo.parse_point("10p"), ("outright", {"call_delta": 0.9}))
        self.assertEqual(bo.parse_point("rr10"), ("rr", {"wing": 10}))
        with self.assertRaises(ValueError):
            bo.parse_point("rr15")


class TestTheTick(unittest.TestCase):
    """The market's tick: 0.05-0.1 for vol and RR, 0.025-0.05 for a fly, the long end smaller."""

    def test_the_tick_is_finer_at_the_long_end_and_for_a_fly(self):
        self.assertEqual(bo.tick("atm", 7 / 365), 0.10)
        self.assertEqual(bo.tick("rr", 30 / 365), 0.10)
        self.assertEqual(bo.tick("fly", 30 / 365), 0.05)
        self.assertEqual(bo.tick("atm", 0.5), 0.05)
        self.assertEqual(bo.tick("fly", 1.0), 0.025)

    def test_a_width_rounds_up_and_is_never_narrower_than_one_tick(self):
        self.assertAlmostEqual(bo.on_ticks(0.101, 0.05), 0.15)
        self.assertAlmostEqual(bo.on_ticks(0.10, 0.05), 0.10)
        self.assertAlmostEqual(bo.on_ticks(0.004, 0.025), 0.025)
        study = _study()
        fly = bo.quote_width(study, "EURUSD", 1.0, "fly", wing=25)
        self.assertGreaterEqual(fly.width, 0.025)
        self.assertAlmostEqual(fly.width / 0.025, round(fly.width / 0.025), places=9)


class TestTheWings(unittest.TestCase):

    def test_a_wing_leg_reads_its_own_cell_then_the_pool_then_the_money(self):
        f = bo.TapeFacts({}, {}, 10.0, leg_cost_bp={("EURUSD", "1M"): 1.0,
                                                    ("EURUSD", "1M", "25c"): 1.5,
                                                    ("*", "1M", "10p"): 0.7})
        self.assertEqual(f.leg_cost("EURUSD", "1M", "25c")[0], 1.5)
        self.assertEqual(f.leg_cost("USDJPY", "1M", "10p")[0], 0.7)      # the pool
        self.assertEqual(f.leg_cost("EURUSD", "1M", "10c")[0], 1.0)      # the money's legs

    def test_a_wing_is_never_cheaper_to_get_out_of_than_the_strike_inside_it(self):
        """The tape reads one-way tail flow short; a lower bound cannot narrow a 10-delta."""
        study = _study()
        study.facts.leg_cost_bp[("*", "1M", "25p")] = 2.0
        study.facts.leg_cost_bp[("*", "1M", "10p")] = 0.2           # a short read in the tail
        ten = bo.quote_width(study, "EURUSD", 30 / 365, "outright", call_delta=0.9)
        tw5 = bo.quote_width(study, "EURUSD", 30 / 365, "outright", call_delta=0.75)
        self.assertGreaterEqual(ten.layoff, tw5.layoff)
        lay = next(p for p in ten.parts if p["name"] == "lay-off cost")
        self.assertIn("never cheaper than", lay["detail"])


class TestTheBars(unittest.TestCase):

    def test_prints_at_one_stamp_take_the_bar_before_them_on_the_terminals_clock(self):
        """Two prints at one stamp once broke the lookup; a stale bar is not the spot."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            times = pd.date_range("2026-09-01 16:00", periods=12, freq="5min")   # HKT, +8
            pd.DataFrame({"time": times, "bid": 1.1000 + np.arange(12) * 1e-4,
                          "ask": 1.1002 + np.arange(12) * 1e-4}).to_csv(
                d / "EURUSD.csv.gz", index=False, compression="gzip")
            (d / "MANIFEST.json").write_text('{"pairs": {}, "tz_offset_hours": 8.0}')
            idx = pd.bdate_range("2026-08-01", "2026-09-02")
            tabs = {"EURUSD Curncy": pd.DataFrame({"Date": idx, "PX_LAST": 1.1})}
            with open(d / "store.pkl", "wb") as f:
                pickle.dump(tabs, f)
            st = tsp.Store(d / "store.pkl")
            self.assertEqual(tsp.terminal_offset(st, d), pd.Timedelta(hours=8))
            leg = {"pair": "EURUSD", "expiry": "2026-10-01", "T": 30 / 365, "N": 1e7,
                   "pccy": "USD", "side": "call", "K": 1.11, "prem": 1e7 * 0.004}
            obs = _prints([leg | {"ts": "2026-09-01 08:17"},       # 16:17 HKT: the 16:15 bar
                           leg | {"ts": "2026-09-01 08:17"},       # the same stamp again
                           leg | {"ts": "2026-09-01 12:00"}])      # hours after the last bar
            obs["kind"] = "single"
            obs["K1"] = obs["K2"] = obs.K
            out = tsp.invert(obs, st, bars=d)
            self.assertEqual(list(out.spot_source), ["bar", "bar", "close"])
            self.assertAlmostEqual(out.S.iloc[0], (1.1003 + 1.1005) / 2, places=9)


class TestTheCross(unittest.TestCase):

    def test_a_cross_made_exactly_of_its_legs_is_cheaper_off_them(self):
        """EURJPY = EURUSD x USDJPY: the dollar in opposite places, so the triangle adds +2 rho.

        Built from its legs at a fixed correlation, the cross's move is all legs: the residual the
        leg route holds is nothing, and it must win against a cross that barely trades.
        """
        study = _study(cross=("EURJPY", ("EURUSD", "USDJPY"), -1))
        r, info = bo.cross_width("EURJPY", 1 / 12, history=study.history(), facts=study.facts)
        self.assertAlmostEqual(info["rho"], -0.4, places=6)
        # what the legs leave is the triangle's curvature at today's levels, not a correlation:
        # the correlation never moved, and measures as not moving
        self.assertLess(info["resid_share"], 0.05)
        self.assertLess(info["corr_daily_sd"], 1e-9)
        self.assertEqual(r.rung, "legs")
        self.assertLess(r.width, info["direct"])


class TestTheCrossInDetail(unittest.TestCase):

    def test_a_legs_risk_reversal_enters_with_its_exposure(self):
        self.assertEqual(bo.exposures("EURJPY", "EURUSD", "USDJPY"), (1, 1))
        self.assertEqual(bo.exposures("EURGBP", "EURUSD", "GBPUSD"), (1, -1))
        self.assertEqual(bo.exposures("CHFJPY", "USDCHF", "USDJPY"), (-1, 1))
        self.assertEqual(bo.exposures("AUDNZD", "AUDUSD", "NZDUSD"), (1, -1))

    def test_a_cross_spot_is_its_legs(self):
        idx = pd.bdate_range("2026-01-01", periods=5)
        tabs = {"EURUSD Curncy": pd.DataFrame({"Date": idx, "PX_LAST": 1.10}),
                "AUDUSD Curncy": pd.DataFrame({"Date": idx, "PX_LAST": 0.66})}
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d) / "s.pkl", "wb") as f:
                pickle.dump(tabs, f)
            st = tsp.Store(Path(d) / "s.pkl")
            self.assertAlmostEqual(st.spot("EURAUD").iloc[0], 1.10 / 0.66, places=12)

    def _spot_history(self, rho, n=1500, seed=4):
        rng = np.random.default_rng(seed)
        z = rng.multivariate_normal([0, 0], [[1, rho], [rho, 1]], size=n) * 0.006
        idx = pd.bdate_range("2020-01-01", periods=n)
        return bo.StoredHistory({("AUDUSD", "spot"): {0.0: pd.Series(np.exp(np.cumsum(z[:, 0])),
                                                                    index=idx)},
                                 ("USDJPY", "spot"): {0.0: pd.Series(np.exp(np.cumsum(z[:, 1])),
                                                                    index=idx)}})

    def test_realized_correlation_reads_the_level_and_takes_the_sampling_noise_out(self):
        """A correlation that never moves measures as (nearly) not moving, not as its noise."""
        rho, sd, _ = bo.realized_correlation(self._spot_history(-0.4), "AUDUSD", "USDJPY", 1 / 12)
        self.assertAlmostEqual(rho, -0.4, delta=0.12)
        self.assertLess(sd, 0.01)

    def test_a_correlation_measured_not_to_move_is_held_at_the_pooled_implied_move(self):
        study = _study(cross=("EURJPY", ("EURUSD", "USDJPY"), -1))
        hist = study.history()
        # the pool is the implied correlations' move across crosses with a history
        pool = bo.pooled_corr_move(hist, 1 / 12)
        self.assertIsNotNone(pool)

    def test_nothing_is_held_past_its_expiry(self):
        study = _study()
        study.facts.flow_usd_mm_per_hour[("EURUSD", "1W")] = 0.01     # a market that barely trades
        r = bo.quote_width(study, "EURUSD", 7 / 365, "atm", size_usd_mm=100.0)
        life = 7 / 365 * 252 * study.facts.hours_per_day
        self.assertLessEqual(r.hold_hours, life + 1e-9)
        self.assertTrue(any("longer than the option's life" in n for n in r.notes))

    def test_the_legs_trade_the_vega_the_cross_moves_with(self):
        study = _study(cross=("EURJPY", ("EURUSD", "USDJPY"), -1))
        r, info = bo.cross_width("EURJPY", 1 / 12, history=study.history(), facts=study.facts,
                                 size_usd_mm=100.0)
        a, b = info["d_sigma_d_legs"]
        leg = next(p for p in r.parts if p["name"] == "leg EURUSD")
        self.assertIn(f"USD {abs(a) * 100:.0f}mm", leg["detail"])
        self.assertEqual(info["corr_source"], "implied")

    def test_a_cross_with_no_history_is_made_of_its_legs_smile_and_all(self):
        study = _study()
        rng = np.random.default_rng(8)
        idx = next(iter(study.ladders[("EURUSD", "atm")].values())).index
        for leg, px in (("EURUSD", 1.1), ("USDJPY", 150.0)):
            study.ladders[(leg, "spot")] = {0.0: pd.Series(
                px * np.exp(np.cumsum(rng.normal(0, 0.006, len(idx)))), index=idx)}
        study.meta["cross_smile"] = {"scale": {"atm": 1.0, "rr25": 1.2, "bf25": 1.0}}
        atm = bo.quote_width(study, "EURJPY", 1 / 12, "atm")
        self.assertEqual(atm.rung, "legs (realized correlation)")
        rr = bo.quote_width(study, "EURJPY", 1 / 12, "rr", wing=25)
        self.assertEqual(rr.rung, "legs (realized correlation)")
        self.assertIsNotNone(rr.width)
        self.assertTrue(any("legs', scaled" in n for n in rr.notes))


class TestCoverage(unittest.TestCase):

    def test_iid_moves_are_covered_as_often_as_promised(self):
        rng = np.random.default_rng(21)
        idx = pd.bdate_range("2016-01-01", "2024-06-28")
        lvl = 10 + np.cumsum(rng.normal(0, 0.3, len(idx))) * 0.05
        tabs = {"EURUSDV1M Curncy": pd.DataFrame({"Date": idx, "PX_LAST": lvl})}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "store.pkl"
            with open(path, "wb") as f:
                pickle.dump(tabs, f)
            r = bo.coverage(tsp.Store(path), "EURUSD", 1 / 12, "atm", p=0.68,
                            start="2022-01-01")
        self.assertGreater(r["n"], 500)
        self.assertLess(abs(r["z"]), 3.0, r)


class TestTheQuoteCarriesIt(unittest.TestCase):
    """The quoting agent's width is the study's (owner, 2026-09-24), under a stated policy."""

    @classmethod
    def setUpClass(cls):
        cls.book = Book.from_excel(BOOK, ASOF).load_all(["EURUSD", "USDJPY"])
        cls.study = _study()

    def quote(self, text, bank=None, **kw):
        return marketmaker.quote_panel_from_request(
            {"pair": "EURUSD", "request_text": text} | kw).run(
            self.book, bank=bank or KnowledgeBank(), widths=self.study)

    def bank(self):
        bank = KnowledgeBank()
        bank.set_pair("EURUSD", [Rule("spread", 0.33, "atm")], ASOF.now)
        return bank

    def test_by_default_the_study_prices_every_single_instrument_row(self):
        out = self.quote("EURUSD 1M ATM\nEURUSD 1M 25d RR\nEURUSD 1M 25d call\n")
        self.assertEqual(out["sheet"]["width_policy"], "study")
        for row in out["sheet"]["rows"]:
            self.assertEqual(row["width_rung"], "model", row["notes"])
            self.assertAlmostEqual(row["width"], row["model_width"], places=12)
            self.assertAlmostEqual(row["our_ask"] - row["our_bid"], row["width"], places=9)
            self.assertFalse(any("no bid or offer" in n for n in row["notes"] + row["warnings"]))

    def test_the_study_stands_before_a_bank_rule_and_says_so(self):
        row = self.quote("EURUSD 1M ATM\n", bank=self.bank())["sheet"]["rows"][0]
        self.assertEqual(row["width_rung"], "model")
        self.assertTrue(any("stands before the bank's" in n for n in row["notes"]), row["notes"])

    def test_bank_first_keeps_the_desks_rule(self):
        row = self.quote("EURUSD 1M ATM\n", bank=self.bank(),
                         width_policy="bank")["sheet"]["rows"][0]
        self.assertEqual(row["width_rung"], "bank")
        self.assertAlmostEqual(row["width"], 0.33)
        self.assertAlmostEqual(row["model_gap"], 0.33 - row["model_width"], places=12)
        # and with no rule, bank-first still falls to the study
        row = self.quote("EURUSD 1M ATM\n", width_policy="bank")["sheet"]["rows"][0]
        self.assertEqual(row["width_rung"], "model")

    def test_off_shows_the_study_and_applies_it_to_nothing(self):
        row = self.quote("EURUSD 1M ATM\n", width_policy="off")["sheet"]["rows"][0]
        self.assertIsNotNone(row["model_width"])
        self.assertNotEqual(row["width_rung"], "model")
        step = next(i for i in row["trace"] if i["name"] == "bid-offer study")
        self.assertFalse(step["applied"])

    def test_a_floor_still_holds_under_the_study(self):
        bank = KnowledgeBank()
        bank.set_pair("EURUSD", [Rule("floor", 5.0, "atm")], ASOF.now)
        row = self.quote("EURUSD 1M ATM\n", bank=bank)["sheet"]["rows"][0]
        self.assertAlmostEqual(row["width"], 5.0)

    def test_an_unknown_policy_is_refused_by_name(self):
        with self.assertRaises(ValueError):
            marketmaker.quote_panel_from_request({"pair": "EURUSD", "request_text": "x",
                                                  "width_policy": "tier"})

    def test_no_study_loaded_is_the_old_ladder_and_says_so(self):
        out = marketmaker.quote_panel_from_request(
            {"pair": "EURUSD", "request_text": "EURUSD 1M ATM\n"}).run(
            self.book, bank=KnowledgeBank())
        row = out["sheet"]["rows"][0]
        self.assertEqual(out["sheet"]["width_policy"], "off")
        self.assertIsNone(row["model_width"])
        self.assertIn("volkit bidoffer study", row["model_note"])


class TestSuggestWidths(unittest.TestCase):
    """The quoting agent's widths, suggested into the export tables' boxes -- never written."""

    def test_the_agent_suggests_every_wing_at_every_tenor_asked(self):
        from volkit import agent
        study = _study()
        r = agent.suggest_widths(study, "EURUSD", ["1M", "3M", "BAD"])
        self.assertEqual([x["tenor"] for x in r["rows"]], ["1M", "3M", "BAD"])
        m = r["rows"][0]
        for k in ("atm", "rr25", "rr10", "bf25", "bf10"):
            self.assertIsNotNone(m[k], k)
        self.assertGreaterEqual(m["rr10"], m["rr25"])
        self.assertGreaterEqual(m["bf10"], m["bf25"])
        self.assertIn("not a tenor", r["rows"][2]["notes"][0])

    def test_the_export_tables_offer_it_and_the_route_refuses_without_a_study(self):
        from volkit.webapp import BookService
        service = BookService(str(book_for("EURUSD")), ASOF)
        tabs = {t["sheet"]: t for t in service.config_tabs()["tabs"]}
        self.assertEqual(tabs["MARKET_WIDTHS"]["measure"], "widths")
        self.assertEqual(tabs["WING_WIDTHS"]["measure"], "widths")
        with self.assertRaises(ValueError):
            service.export_suggest_widths({"pair": "EURUSD"})


if __name__ == "__main__":
    unittest.main()
