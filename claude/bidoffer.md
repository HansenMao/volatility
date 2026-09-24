# volkit — the bid-offer study (`tapespread.py`, `bidoffer.py`)

Read this before changing either module, the quote's `model_width` column or its model
rung, or `volkit bidoffer`. It records the owner's principle, what was measured, and
what is still judgement.

## The principle (owner, 2026-09-24)

A two-way is **protection**. Half of it must cover the likely move in the option's own
volatility over the time it takes to get out of the position, judged from how the
surface has actually moved. A size the market cannot absorb at once both lengthens that
time and moves the market. **The desk's typed widths are not benchmarks**:
`MARKET_WIDTHS`, `WING_WIDTHS`, the `SPREADS` tiers and `exportseed` are neither inputs
nor targets. The rule of thumb (a broker two-way of 4–5 bp of premium on about USD
100mm) is the answer only when nothing else is available.

```
width = lay-off cost  +  2 x ( Q_p |move over the hold|  +  impact(size) )
hold  = wait for comparable flow  +  size / (share x flow per hour)
```

## Where each input comes from

All of it is read by `volkit bidoffer study` into `bidoffer_study.pkl` beside the
workbook. The quote reads only that file. The inputs live in the quant repo (`--tape`,
`--store`, `--bars`):

| Input | Source | How |
|---|---|---|
| the pillars' daily moves | Bloomberg store, `DATA.pkl` (ATM 1W–1Y, 25Δ RR/BF where pulled) | a strike's vol is `ATM + w_bf·BF + w_rr·RR` (exactly `ATM + BF25 + RR25/2` at a 25Δ call), so its move on each historical day is that combination of the three days' moves, keeping their correlation and tails. Each day is rescaled to today's regime (filtered historical simulation, EWMA λ 0.94) |
| flow per hour, the wait between comparable prints | DTCC tape | the median daily USD notional per pair × tenor bucket, divided by the intraday clock; crosses are put in dollars through the base currency's leg |
| the intraday clock | DTCC tape | a day's vol move arrives over **11.3 effective hours**: the slope of straddle-pair dispersion against the gap, about 9,400 pairs, in units of each bucket's daily move |
| the cost of crossing the market on one leg | DTCC tape | ATM legs: a straddle's measured spread × one option's vega, about **1 bp** of notional. Wing legs (25Δ, 10Δ): single options at that delta, priced off their own intraday spot. A pair's own cell is used only where it clears three standard errors; otherwise the median across pairs at that tenor and delta (cells clear of two). See *Wings* below |
| size impact | DTCC tape | the extra dispersion of a print against the last comparable one, by ticket size, over the same gaps: **none measurable below USD 150mm**; 150–400mm trade ~0.29 of a daily move further (183 pairs; the tape caps its largest tickets) |

## The measurement (`tapespread.py`)

The tape publishes no side, so a spread is read off **pairs** of comparable prints close
in time:

E[dv²] = s²/2 + b·gap

Both prints are inverted off the **same** spot and forward reference, so an error in the
reference becomes drift that grows with the gap and never enters the intercept. That is
why the study worked before any intraday spot existed. Once the quant repo's five-minute
bars (`data/external/bbg_intraday/`) cover a print, the bar before it (within 15 minutes)
is its spot, and the spot drops out of the difference. **A pair's two prints must stand on
the same kind of spot** (`spot_source`: bar or close); one of each shares no reference.

The bars carry the terminal's own clock. On the desk's VDI that is **HKT, +8h**, which
the quant ingest measures every night (newest bar against the workbook's save time) and
keeps as `tz_offset_hours`. `terminal_offset` reads that. Its fallback, matching bars to
each day's 5pm New York close, can only tell 8 from 9 hours to within the quiet hour
after the close, so it is the fallback and nothing more. From the first pull
(2026-09-24), 51% of the tape's prints are inverted off their own bar: every print from
12 March on.

- **Straddles** and **two-strike packages** are spot-neutral. The package premium is a
  sum of amounts, so a risk reversal and a strangle invert the same way. They give the
  ATM and the wings' level. **Singles** are spot-sensitive and are the reason the bars
  are pulled.
- **One trade in pieces is one trade.** The same option at the same price per unit
  notional within 4 hours is merged: 135k of 632k prints were such pieces. Unmerged, they
  put the spread at zero.
- **The sides are assumed independent.** A mixture fit that frees the same-side share
  put it near 85% for USDJPY, USDCAD and AUDUSD 1M straddles, and s at 0.13–0.21 against
  the independent-sides ~0.10. Read the tape's spreads as **lower bounds**.
- What it found at the ATM (straddles, full two-way, vol):

| | 1W | 1M | 3M–1Y |
|---|---|---|---|
| Straddle spread | ≈ 0.26–0.36 | ≈ 0.09–0.16 | ≈ 0.03–0.08 |

  In premium that is 1–2 bp, a quarter to a half of the broker rule of thumb. These are
  **traded** spreads, where business printed. A quoted two-way protects more, so they
  check `p` rather than set it.

## Wings

With bar spots, single options measure the wings. For example, EURUSD 1M: 25Δ call 0.15,
25Δ put 0.18, 10Δ call 0.14, 10Δ put 0.09 vol. Two cautions shape how they are used:

- **They are noisy.** The standard error per cell is about 0.15 vol, so a pair's own cell
  stands only past three standard errors, and the rest read the pooled median.
- **They read short in the tails.** Flow there is one-way: clients keep buying the same
  wing, so consecutive prints sit on one side and the bounce is understated. The tape
  shows 10Δ puts *narrower* in vol than 25Δ puts in every major at 1M. A lower bound must
  not make a wing cheaper, so **a strike's lay-off cost in vol is held at least at the
  next strike inside it on the same side** (10Δ ≥ 25Δ ≥ ATM). The part's detail says when
  that floor bound.

## The tick

The owner's tick (2026-09-24) is the least a quote moves: 0.05–0.1 for a vol and an RR,
0.025–0.05 for a fly, smaller at the long end. `bidoffer.TICKS` holds it as a ladder:

| Tenor | ATM, RR, strike vol | Fly |
|---|---|---|
| Up to 45 days | 0.10 | 0.05 |
| Beyond | 0.05 | 0.025 |

Every width is **rounded up** to whole ticks and is never narrower than one. Rounding down
would quote less protection than the buffer measured. The rounding is its own `tick` step
on the trace, and `raw` keeps the unrounded sum. A cross's leg route adds the legs' raw
widths and rounds once, on the cross's grid.

On the tape: every single-option bucket measured clear of two standard errors trades at
least one tick wide, and ATM straddles print at about one tick (1M ≈ 0.096 against 0.10).
The quote's bid and offer are still the mid ± half the width; putting them on the grid
themselves is the quote engine's business and has not been done.

## Quoting, and suggesting into the export tables

The quote engine takes its width from this study first (`width_policy`, default `study`: study,
bank, archive, rule of thumb, tier, none; `bank` puts a bank rule first; `off` shows it only).
`claude/agent-quoting.md` is the ladder in full.

On the Vol bulk processing screen, `MARKET_WIDTHS` and `WING_WIDTHS` get their widths suggested
by the quoting agent (`agent.suggest_widths`) for one pair at the table's tenors: the ATM into
the pair's column, and the four wings as the pair's rows. It is a suggestion into the boxes,
never a write (`claude/screen-export.md`).

A 10Δ RR or fly is never cheaper a leg to lay off than its 25Δ one, for the same reason a 10Δ
strike is never cheaper than a 25Δ one.

## Validation

`bidoffer.coverage` re-reads the history each month from data before it and scores
every later day against that month's buffer. At `p = 0.68`, 1M, ten pairs, 2022–2026:

| | ATM | RR | fly | 10Δ put |
|---|---|---|---|---|
| Pooled coverage (target 68%) | 66.7% | 66.8% | 67.0% | 66.9% |

That's about 12,300 days each. The one-point shortfall comes from holding a month's
regime fixed. GBPUSD RR, AUDUSD fly, USDCAD RR and NZDUSD ATM run 2–6 points short:
their tails are fatter than their history shows. `volkit bidoffer coverage PAIR` re-runs
it.

## Crosses

`cross_width` prices the ATM two ways and takes the cheaper:

- **direct:** the cross on its own history and flow, where it has a vol history.
- **legs:** vega hedged in the dollar legs at once. In the triangle
  `σc² = σa² + σb² − 2sρσaσb` (where `s` is the product of `cross.infer_leg_signs`: −1 for
  EURJPY, which adds +2ρ), the cross moves by `∂σc/∂σleg` with each leg and by `∂σc/∂ρ`
  with the correlation. So:
  - each leg's own width is paid **at the vega it actually trades**: the leg's notional is
    `|∂σc/∂σleg|` of the cross's, so its hold and impact are the leg's at that size;
  - the **correlation's move**, times `∂σc/∂ρ`, is held over the cross's own hold, because a
    correlation can only be laid off in the cross.

**The correlation is explicit** (2026-09-24), in one of two forms. The row says which (`legs`
or `legs (realized correlation)`):

- **implied:** where the cross has a vol history, ρ is rebuilt day by day from the three ATMs at
  the nearest tenor all three quote. Its daily changes are rescaled to today's regime.

  Checked against the earlier implicit version (the cross's move the legs do not explain,
  `dσc − a·dσa − b·dσb`): the two correlate at 0.82–0.92 on ten crosses (EURGBP 0.61, where the
  linearisation is worst at ρ 0.86). The implicit one also carried quote noise and curvature.
- **realized:** where the cross has none (NZDJPY, GBPAUD, EURAUD, CADJPY, GBPCHF, …), ρ comes
  from the legs' spot, over windows the option's length (a month at the least), across two
  years or eight windows. Sampling noise is taken out, as in `history.realized_corr_vol`.
  - The level is read over at least a quarter.
  - Checked on the 11 crosses that have both: levels agree (0.99 across crosses). The move
    agrees at 1M (median ratio 0.96) and reads short at 3M (0.62).
  - It is floored at the **pooled implied move** (`pooled_corr_move`, 1M ≈ 0.018 a day). A
    correlation measured not to move is sampling noise winning, not a fact.

**A cross's smile.**
- Where the cross has its own RR/BF history, that is the smile's move.
- Where it has none, the smile is made off the legs (`leg_smile_moves`):
  `rr = e_a·a·dRR_a + e_b·b·dRR_b`, where `exposures` gives each leg's orientation in the
  cross, and `bf = |a|·dBF_a + |b|·dBF_b`. This is scaled by how far crosses with a history
  move against their legs' making of them (`cross_smile_scales`: ATM ×1.07, RR ×1.17,
  BF ×0.97).
- How well that stand-in tracks crosses that do have a history:
  - ATM: 0.67–0.92.
  - RR: 0.7–0.88 for JPY and dollar-bloc crosses; poor for EURCHF, EURSEK, NZDCAD; nothing
    for AUDNZD.
  - BF: rough, 0.02–0.62.
  - A single cross's scale runs 0.4–2.6.
- A cross **outright** swaps the ATM share for the routed ATM and keeps the smile.

The leg route wins for the JPY crosses (AUDJPY 1M 1.2 → 0.6, CHFJPY 1.1 → 0.5). The direct
route wins where the legs share one large driver (EURGBP, EURCHF, AUDNZD).

**Crosses the store has no spot for** (EURAUD, …) are composed from their legs'
(`Store.spot`), so their tape prints count toward flow.

**Nothing is held past its expiry.** A thin market's hold is capped at the option's
remaining trading hours, for crosses and dollar pairs alike.

## Rungs, and what is judgement

A width names its rung:

- `history + tape`: the pair's own moves and its own flow.
- `history`: flow borrowed from the median pair at the tenor, scaled by the pair's
  volume.
- `legs`: a cross made off its legs.
- `rule of thumb`: no vol history in the store. 4.5 bp over the quote's vega, taken at
  the tape's own traded ATM level. It is shown in the column and **never used as a
  rung**, because a rung is a measurement.

Judgement, each one argument on `volkit bidoffer grid`:

| Setting | Default | Meaning |
|---|---|---|
| `p` | 0.68 | how much of the likely move the half-width covers |
| `share` | 0.20 | how much of the passing flow a position can be laid off into |
| `DEFAULT_WING10` | 1.8 | the 10Δ RR and BF move as 1.8× the 25Δ; the quant store has no 10Δ series |

## Known gaps

- **Follow-up, deferred by the owner (2026-09-24): EM vol history.** MXN, ZAR, TRY, HUF
  and PLN have no vol history in the quant store, so they sit on the rule-of-thumb rung.
  Pulling `<PAIR>V1W…V1Y` and `25R/25B` for them (`pull_manifest.json`; logged in the
  quant repo's `docs/OUTSTANDING.md` §14c) would put them on `history + tape`.
- **Follow-up, confirmed as a working setting by the owner (2026-09-24), to be validated:
  the 10Δ/25Δ ratio.** `DEFAULT_WING10 = 1.8` has the 10Δ RR and BF moving 1.8× the 25Δ
  ones, because the quant store has no 10Δ series. Measure it off volkit's own history
  sheets (they carry 10Δ columns) or a pull of `<PAIR>10R*` / `<PAIR>10B*`. `p = 0.68` and
  `share = 0.20` are confirmed.
- **Follow-up, left as measured by the owner (2026-09-24): thin crosses' hold.** A cross's
  hold comes from its flow on the DTCC tape, and the tape holds only CFTC-reportable trades,
  so it understates non-USD cross flow. For thin crosses the hold then runs to the option's
  expiry, e.g. NZDJPY 1M ATM 1.8 and GBPCHF 1M 1.4. Options when it is taken up: cap the cross
  hold (for instance five trading days), or scale the tape's cross flow up by a stated factor.
- **The intraday bars start 2026-03-12.** Wing measurements rest on the six months since.
  The store grows nightly, and the study should be re-run as it does.
- **The tape holds one year**, so how spreads widen in stress is identified across
  pairs, not through time.
