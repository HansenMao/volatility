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

    def test_no_table_carries_it_and_the_route_refuses_without_a_study(self):
        # The suggestion is a card of its own, independent of channel: no export table offers it.
        from volkit.webapp import BookService
        service = BookService(str(book_for("EURUSD")), ASOF)
        tabs = {t["sheet"]: t for t in service.config_tabs()["tabs"]}
        self.assertEqual(tabs["MARKET_WIDTHS"]["measure"], "")
        self.assertEqual(tabs["WING_WIDTHS"]["measure"], "")
        with self.assertRaises(ValueError):
            service.export_suggest_widths({"pairs": "EURUSD"})

    def test_the_route_reads_several_pairs_and_refuses_a_bad_one_in_its_place(self):
        from volkit.webapp import BookService
        service = BookService(str(book_for("EURUSD")), ASOF)
        service._bidoffer = lambda: _study()
        r = service.export_suggest_widths({"pairs": "eurusd, EURUSD; BAD", "tenors": "1M, 3M"})
        self.assertEqual([x["pair"] for x in r["results"]], ["EURUSD", "BAD"])
        self.assertEqual([x["tenor"] for x in r["results"][0]["rows"]], ["1M", "3M"])
        self.assertIsNotNone(r["results"][0]["rows"][0]["atm"])
        self.assertIn("six-letter", r["results"][1]["error"])
        self.assertEqual(r["tenors"], ["1M", "3M"])
        d = service.export_suggest_widths({"pair": "EURUSD"})
        self.assertEqual(len(d["results"][0]["rows"]), 10)
        with self.assertRaises(ValueError):
            service.export_suggest_widths({"pairs": " , "})

    def test_the_export_screen_carries_the_card(self):
        html = (Path(__file__).resolve().parents[1] / "volkit" / "web" / "index.html").read_text()
        for needle in ('id="xwpairs"', 'id="xwtenors"', 'id="xwgo"', 'id="xwout"',
                       "function xwSuggest", "cfgwbuild"):
            self.assertIn(needle, html)
        self.assertNotIn("cfgWidthHtml", html)



# -- the desk's own sources: the sdr folder and the historical workbook -----------------------------
_SDR_HEADER = ["Dissemination Identifier", "Original Dissemination Identifier", "Action type",
               "Event type", "Event timestamp", "Execution Timestamp", "Expiration Date",
               "Strike Price", "Option Premium Amount", "Option Premium Currency",
               "Notional amount-Leg 1", "Notional currency-Leg 1", "Call amount", "Call currency",
               "Put amount", "Put currency", "UPI FISN"]


def _sdr_zip(folder, day: str, rows) -> Path:
    """One day's dissemination zip, named the way the DTCC download names it."""
    import csv, io, zipfile
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_SDR_HEADER)
    for r in rows:
        w.writerow([r.get(h, "") for h in _SDR_HEADER])
    stamp = day.replace("-", "_")
    path = Path(folder) / f"CFTC_CUMULATIVE_FOREX_{stamp}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"CFTC_CUMULATIVE_FOREX_{stamp}.csv", buf.getvalue())
    return path


def _eurusd_call(diss, ts, prem, action="NEWT", orig=""):
    return {"Dissemination Identifier": diss, "Original Dissemination Identifier": orig,
            "Action type": action, "Event type": "TRAD", "Event timestamp": ts,
            "Execution Timestamp": ts, "Expiration Date": "2026-12-01", "Strike Price": "1.1",
            "Option Premium Amount": str(prem), "Option Premium Currency": "USD",
            "Notional amount-Leg 1": "10000000", "Notional currency-Leg 1": "EUR",
            "Call amount": "10000000", "Call currency": "EUR", "Put amount": "11000000",
            "Put currency": "USD", "UPI FISN": "NA/O Van Call EUR USD"}


class TestTheDesksOwnSources(unittest.TestCase):
    """``volkit.cfg``'s ``sdr`` and ``history`` in place of the quant repo's extract and store.

    The desk's exe has no quant repo, so the study used to be buildable on one machine only.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_sdr_folder_becomes_the_tape_the_study_reads(self):
        from volkit import tapeextract
        sdr = self.dir / "sdr"
        sdr.mkdir()
        _sdr_zip(sdr, "2026-09-01", [
            _eurusd_call("1", "2026-09-01T10:00:00Z", 40000),
            _eurusd_call("2", "2026-09-01T11:00:00Z", 41000),
            _eurusd_call("3", "2026-09-01T12:00:00Z", 42000),
            # a correction replaces print 2: one trade, at the corrected premium
            _eurusd_call("4", "2026-09-01T11:00:00Z", 41500, action="CORR", orig="2"),
        ])
        # a cancel of print 3 arrives in the next day's file, as they do
        _sdr_zip(sdr, "2026-09-02", [
            _eurusd_call("5", "2026-09-01T12:00:00Z", 42000, action="CANC", orig="3")])
        (sdr / "notes.zip").write_bytes(b"")          # no date in the name: said, not read
        cache = self.dir / "extract"
        r = tapeextract.build([sdr], cache, log=lambda *a: None)
        self.assertEqual((r["days"], r["parsed"]), (2, 2))
        self.assertTrue(any("notes.zip" in n for n in r["notes"]))
        live = tsp.load_prints(cache)
        self.assertEqual(sorted(live.prem.tolist()), [40000.0, 41500.0])
        self.assertTrue((live.pair == "EURUSD").all())
        # a second build reads nothing it has already read
        again = tapeextract.build([sdr], cache, log=lambda *a: None)
        self.assertEqual(again["parsed"], 0)
        self.assertEqual(len(tsp.load_prints(cache)), 2)

    def test_a_folder_with_no_zips_is_refused_by_name(self):
        from volkit import tapeextract
        with self.assertRaises(tapeextract.ExtractError) as cm:
            tapeextract.build([self.dir], self.dir / "x", log=lambda *a: None)
        self.assertIn(str(self.dir), str(cm.exception))

    def test_the_history_workbook_reads_exactly_as_the_store_would(self):
        """One set of numbers, laid out once as volkit's history sheet and once as the Bloomberg
        store's tabs: every reading the study makes is the same.  The vols are the trap -- the
        workbook holds them as decimals and the store as points."""
        from volkit.history import load_history
        days = pd.bdate_range("2026-06-01", periods=60)
        rng = np.random.default_rng(3)
        spot = 1.10 + np.cumsum(rng.normal(0, 0.004, len(days)))
        pts1m, pts1y = 25 + rng.normal(0, 1, len(days)), 180 + rng.normal(0, 3, len(days))
        atm1m, atm3m = 7.5 + rng.normal(0, 0.2, len(days)), 7.9 + rng.normal(0, 0.1, len(days))
        rr1m, bf1m = -0.4 + rng.normal(0, 0.05, len(days)), 0.2 + rng.normal(0, 0.01, len(days))
        sheet = pd.DataFrame({"Date": days, "Spot": spot, "1M swap points": pts1m,
                              "12M swap points": pts1y, "ATM 1M": atm1m, "ATM 3M": atm3m,
                              "RR25 1M": rr1m, "BF25 1M": bf1m})
        xl = self.dir / "hist.xlsx"
        with pd.ExcelWriter(xl) as xw:
            sheet.to_excel(xw, sheet_name="EURUSD", index=False)
        tabs = {name: pd.DataFrame({"Date": days, "PX_LAST": v}) for name, v in {
            "EURUSD Curncy": spot, "EUR1M Curncy": pts1m, "EUR12M Curncy": pts1y,
            "EURUSDV1M Curncy": atm1m, "EURUSDV3M Curncy": atm3m,
            "EURUSD25R1M Curncy": rr1m, "EURUSD25B1M Curncy": bf1m}.items()}
        pkl = self.dir / "store.pkl"
        pkl.write_bytes(pickle.dumps(tabs))
        want, got = tsp.Store(pkl), tsp.HistoryStore(load_history(xl))

        def same(a, b):
            self.assertEqual(sorted(a), sorted(b))
            for k in a:
                np.testing.assert_allclose(b[k].values, a[k].values, rtol=1e-12)
                self.assertTrue(b[k].index.equals(a[k].index))

        np.testing.assert_allclose(got.spot("EURUSD").values, want.spot("EURUSD").values)
        same(want.atm("EURUSD"), got.atm("EURUSD"))
        same(want.smile("EURUSD", "rr"), got.smile("EURUSD", "rr"))
        same(want.smile("EURUSD", "bf"), got.smile("EURUSD", "bf"))
        same(want.forward_points("EURUSD"), got.forward_points("EURUSD"))
        self.assertAlmostEqual(float(got.atm("EURUSD")[1 / 12].iloc[0]), atm1m[0] / 100.0)

    def test_the_build_refuses_a_missing_source_by_the_setting_that_names_it(self):
        empty = self.dir / "empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError) as cm:
            bo.build_study(self.dir / "s.pkl", tape=empty, store=self.dir / "none.pkl",
                           log=lambda *a: None)
        self.assertIn("sdr =", str(cm.exception))
        sdr = self.dir / "sdr"
        sdr.mkdir()
        _sdr_zip(sdr, "2026-09-01", [_eurusd_call("1", "2026-09-01T10:00:00Z", 40000)])
        with self.assertRaises(FileNotFoundError) as cm:
            bo.build_study(self.dir / "s.pkl", sdr=[sdr], store=self.dir / "none.pkl",
                           log=lambda *a: None)
        self.assertIn("history =", str(cm.exception))

    def test_the_screen_builds_from_the_servers_own_sources_in_the_background(self):
        """The page names no path: the build reads the ``sdr`` and ``history`` the server was
        started with, runs on its own thread, and refuses a second one while it runs."""
        import threading
        from unittest import mock
        from volkit.webapp import BookService
        service = BookService(str(book_for("EURUSD")), ASOF, agent_sdr=[str(self.dir)])
        seen, release = {}, threading.Event()

        def fake(out, **kw):
            seen.update(kw, out=str(out))
            kw["log"]("read the tape")
            release.wait(10)
            return {"built": "now", "sources": {}, "tape_atm": {"big": 1}}

        with mock.patch.object(bo, "build_study", fake):
            started = service.bidoffer_build_study({})
            self.assertEqual(started["state"], "running")
            self.assertEqual(started["sources"]["sdr"], [str(self.dir)])
            with self.assertRaises(ValueError):
                service.bidoffer_build_study({})
            release.set()
            for t in threading.enumerate():
                if t.name == "bidoffer-study":
                    t.join(10)
        state = service.bidoffer_study_state()
        self.assertEqual(state["state"], "done")
        self.assertEqual(seen["sdr"], [str(self.dir)])
        self.assertIsNone(seen["history"])
        self.assertTrue(seen["out"].endswith(bo.STUDY_FILENAME))
        self.assertIn("read the tape", state["log"])
        self.assertNotIn("tape_atm", state["result"])


if __name__ == "__main__":
    unittest.main()
