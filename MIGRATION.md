# Migrating from the legacy `vol` tool

The rebuild was done **correctness first**: model-level bugs were fixed rather
than reproduced. Three of those fixes change the numbers the tool produces.
Read this section before trusting a comparison.

---

## 1. Changes that move your marks

### 1.1 Cross-pair triangle sign  — **largest impact**

`CVol_Cor.getBackboneVol` always used

```
sigma_cross^2 = v1^2 + v2^2 - 2 rho v1 v2
```

That is correct only when the common currency sits in the **same** position in
both legs. For `AUDJPY = AUDUSD × USDJPY` the common currency is on opposite
sides, so the log-returns *add* and the cross term must be `+2 rho v1 v2`.
`volkit.cross.infer_leg_signs` derives the sign from the pair names.

At 3M, with the workbook's parameters:

| Cross | Legs | Leg vols | Legacy | volkit | |
|---|---|---|---|---|---|
| AUDJPY | AUDUSD / USDJPY | 9.66 / 6.04 | 9.9087 | **12.8734** | changed |
| EURJPY | EURUSD / USDJPY | 5.87 / 6.04 | 6.4838 | **9.9163** | changed |
| EURCNH | EURUSD / USDCNH | 5.87 / 5.05 | 5.9631 | **9.1643** | changed |
| GBPCNH | GBPUSD / USDCNH | 7.44 / 5.05 | 6.8363 | **10.7616** | changed |
| GBPNZD | GBPUSD / NZDUSD | 7.44 / 9.72 | 7.4739 | 7.4739 | same |
| EURGBP | EURUSD / GBPUSD | 5.87 / 7.44 | 6.3960 | 6.3960 | same |

**To reproduce the legacy numbers exactly: negate the `initial` and
`long term` cells for AUDJPY, EURJPY, EURCNH and GBPCNH in the PARAMS sheet.**
Verified — with `+0.370 → −0.370` etc., volkit matches the legacy output to
1e-12 at every tenor. Nothing else needs to change.

Then decide which is right, because the two disagree about economics:

* Your stored correlations are all **positive** (+0.37 to +0.50). Under the
  legacy `−2 rho` formula they acted with a negative sign.
* `corr(AUDUSD, USDJPY)` is genuinely **positive** — both are risk-on trades.
  With `+2 rho` and `rho ≈ +0.3` the triangle gives ≈12.8%, and AUDJPY does
  trade above AUDUSD (9.66%). The legacy 9.91% sits barely above its own leg.
* `corr(EURUSD, USDJPY)` is genuinely **negative**, so for EURJPY the legacy
  6.48% is the plausible figure and volkit's 9.92% is not — it comes from
  feeding a positive correlation into the corrected formula.

In other words the stored column behaves like a fitted knob rather than a
correlation, and it carries a different effective sign for different crosses.
The formula is now unambiguous; **the correlation marks need revisiting
per cross**, not a blanket flip. `CrossAtmCurve.implied_correlation(t, vol)`
backs out the correlation that reproduces an observed cross vol, which is the
quickest way to re-mark the column against live quotes.

### 1.2 Rate-volatility cross term

```
legacy:  sqrt(sigma^2 + 2 rho sigma nu     + nu^2 t^2)
volkit:  sqrt(sigma^2 + 2 rho sigma nu t   + nu^2 t^2)
```

The legacy middle term is dimensionally inconsistent with the other two.
**Every pair in the sample workbook has `ratevol = 0`, so this changes
nothing today.** It only matters if you start using that parameter.

### 1.3 Smile interpolation

The legacy `solve_svi3` summed three SVI slices — **twelve free parameters
fitted to five points** — with an unconstrained optimiser, no convergence
check and no no-arbitrage conditions. volkit fits a single raw SVI slice
(five parameters, five points) under Gatheral's constraints, and reports
Durrleman's butterfly condition.

Wing vols away from the quoted deltas will differ. The new fit reproduces all
five anchors to ~1e-12 vol points and the resulting risk-neutral density
integrates to 1.000000; the legacy fit had no such guarantee. The `SVI` /
`LOWSVI` pair (which only differed by a `sigma` seed of 0.6 vs 0.4) collapses
to a single `SVI` method; `VV25`, `VV10`, `SABR25` and `SABR10` are unchanged
in spirit.

### 1.4 Date and weighting conventions

* Expiries roll **modified following** (FX standard). The legacy code rolled
  weekends backwards unconditionally. Pass `convention="preceding"` to
  `CalendarSet.expiry_date` for the old behaviour.
* The weekend window is Friday 22:00 → Sunday 22:00 UTC (the real market
  closure) rather than Saturday/Sunday 00:00 UTC. Same 48 hours, shifted 2.
* Holiday weighting is computed from each trading centre's share of the hour
  instead of a hand-tuned 6×24 matrix. The legacy matrix's paired rows were
  not consistent with its single rows, and it had no row for a Chinese or
  Hong Kong holiday.
* One year length everywhere: 365.2425 days. The legacy code mixed `365`,
  `365.2425`, `31536000`, `31556952`, `52.0345` and `12.0079726` — in
  `refreshDailyCumulativeVols`, the loop bound used one and the normalisation
  another.

### 1.5 Risk reversal sign — please check

`test.py` records `solveSabrFromMarket(rr=0.025, ...)` returning
`rho = -0.383`. A **positive** risk reversal means calls over, which must give
a **positive** rho. volkit returns `+0.349` for that input and `-0.388` for
`rr = -0.025` — the latter matching the legacy comment.

Either that comment is stale, or the legacy smile was skewed the wrong way.
I could not settle it because `pysabr` is not installed here. volkit's
convention is `RR = call vol − put vol`, verified by round-trip: calibrate to
an RR, re-extract it from the fitted smile, and the two agree to 1e-8.
**Sanity-check one live smile against a broker quote before going live.**

### 1.6 A tenor is a settlement date — the FX date construction

This is the largest of the date changes and it moves every tenor slightly.

**What a tenor now resolves to.** The market builds an option's dates in one
order and this now follows it:

1. **Spot date** = trade date + the pair's spot lag (T+1 for USDCAD, T+2 for
   the rest), counted on the *two currencies'* own calendars, and then rolled
   forward to a day USD can settle on. US holidays do not stop the count for a
   pair with no dollar in it — they only rule out the value date it lands on.
   Counting them would push EURJPY spot out a day every Thanksgiving, which is
   not what the market does. `CalendarSet.settlement_countries` is the seam.
2. **Settlement (delivery) date** = spot + the tenor, adjusted **modified
   following** on the value-date calendars, with the **end-of-month rule**: off
   a spot date that is the last value date of its month, every month and year
   tenor settles on the last value date of *its* month. Without that rule a 1M
   dealt off a 28-Feb spot settles 28-Mar where the market settles 31-Mar.
3. **Expiry** = the spot lag *back* from the settlement date, on the pair's own
   calendars.

**Day tenors go the other way, because that is what they mean.** `O/N` expires
on the next business day and settles from that day's own spot; `8D` expires on
the eighth business day. Adding calendar days to the spot date and subtracting
the lag at the end — which is what this used to do — collapses the short
tenors onto each other, because the two business days taken off swallow the
weekend the addition just crossed. Dealt on a Wednesday, `1D` and `2D` both
came back Thursday. `calendars.fx_dates` returns all four dates together.

**Short-date codes are read.** `O/N`, `T/N`, `S/N` and `S/W` parse as tenors
(`timeutil.parse_tenor`), spelled canonically by `timeutil.normalise_tenor`.

**Where a tenor sits on the volatility axis moved with it.** A `1M` is no
longer 0.083333 years; it is the years from the valuation clock to the 1M
expiry date on the pair's own calendar. `calendars.expiry_years` is the one
reading of it, reached through `AtmCurve.tenor_years` /
`VolSurface.tenor_years` / `Book.tenor_years`. `timeutil.tenor_to_years`
survives as what it always was — a nominal length and a sort key, needing no
pair and no clock. This is the change that moves marked volatilities: a `1M`
mark is now the volatility a `1M` option actually receives, which it was not
before.

Off `files/vol_marks.xlsx` at a 2026-09-01 12:00Z valuation:

| Tenor | t before | t after | EURUSD ATM % | USDJPY ATM % |
|---|---|---|---|---|
| 1w | 0.019165 | 0.017796 | 5.8338 → 5.7827 | 5.7463 → 5.6955 |
| 1m | 0.083333 | 0.080768 | 5.8985 → 5.8646 | 5.8479 → 5.8093 |
| 2m | 0.166667 | 0.160168 | 5.8583 → 5.8991 | 5.8977 → 5.9338 |
| 3m | 0.250000 | 0.247781 | 5.9098 → 5.8943 | 6.0211 → 6.0020 |
| 1y | 1.000000 | 0.997967 | 6.0742 → 6.0704 | 6.5104 → 6.5058 |

Under five hundredths of a volatility point, largest at the front where a day
is a bigger share of the term. There is **no switch**: this is a date being
computed correctly rather than a modelling choice, and a build that placed a
tenor two ways would be a build with two answers for "where is 1M marked".
To reconcile against an old figure, read the curve at `tenor_to_years(tenor)`
directly — `VolSurface.atm.term_vol(tenor_to_years("1m"))` is exactly the old
number.

**The forward is now read on the settlement date.** A forward is a price for a
value date, and an option's is its settlement date — two business days past
its expiry, which on a one-week option is a fifth of its swap points. Two
things changed together:

* A **feed pillar is placed on its own delivery date**: a broker's 1M swap
  points are the points to the 1M value date, so the pillar sits at the days
  from the spot date to that date, not at a nominal 30.44-day month. The
  axis was always "years from the spot date" (the `feed` module docstring);
  the tenor pillars were the one thing not actually on it.
* Every screen reads a level through **`Book.market_level_for(pair, expiry)`**,
  which puts the option's settlement date on that same axis.
  `Book.market_level(pair, t)` survives for the callers that genuinely have a
  time and not an expiry. A caller may **state** that settlement date -- the
  pricing screen's Settlement box, for a trade settling on a broken date --
  and it is the date that is stated, never the placement: it still goes onto
  the axis through `Book.settlement_years`, still as an offset. Nothing moves
  unless somebody types one.

One row needed care rather than a substitution. A **carry row's** content is
the difference between two forwards — the option's now, and the same option a
horizon later — so reading one on the settlement date and the other at a year
fraction contaminates that difference with the gap between the two
conventions. A third of a pip, which is the same order as the gamma carry
being measured, and it turned a flat carry profile into a ragged one. Both are
now read on the settlement axis, the rolled one a horizon back along it:
`ts = Book.settlement_years(pair, expiry)`, then `ts` and `ts - h`. The rolled
leg is this same option a week later, not another option.

The consequence worth having: **at every quoted pillar the forward is now
exactly the published swap points.** It used to interpolate between two of
them, because the pillar was placed nominally and the query was made at the
expiry.

| | 1w | 1m | 2m | 3m | 6m | 1y |
|---|---|---|---|---|---|---|
| EURUSD, pips | +0.09 | +0.18 | +0.48 | +0.17 | +0.45 | +0.16 |
| USDJPY, pips | −0.19 | −0.35 | −0.91 | −0.32 | −0.85 | −0.31 |

A feed loaded without a valuation date has no spot date to place a pillar
against and keeps the nominal placement, which is the only thing there is to
fall back on. `feed.load_for` always passes the book's clock, so that is the
degenerate case and not the working one.

**The pricing screen shows the settlement date** on its own Results row, with
the spot date and the rule that produced it on hover; the marking screen's vol
query and `volkit vol --verbose` say it too, and `volkit tenors` prints the
expiry and settlement date beside every pillar.

---

## 2. Bugs fixed that did not change correct results

Things that were broken outright:

| Legacy | Problem |
|---|---|
| `Vol.getDensity` | `NameError` on undefined `S`; divided by `step**2` instead of `(K*step)**2` |
| `Vol.pnl_opt` | `NameError` on undefined `t` |
| `test.py` | did not parse (indentation error) |
| `run_indication` | undefined `FILE_PATH`; `xlrd.open_workbook` on `.xlsx` (dropped in xlrd 2.0); `writer.book=` / `writer.save()` (removed in pandas 2.0) |
| `Vol.fit_sabrs`, `RV.calc` | chained `df[col].iloc[i] = v` — a no-op under copy-on-write, so the rolldown matrix stayed empty |
| `get_neighbor_tenors` | `argmax(tenors > t)` returns 0 when nothing exceeds `t`, silently giving `(last, first)` past the end and index `-1` below the start |
| `five_point_interpolate_straight` | divides by zero when `l1 == l4` |
| `get_years_time` | `"1D"` returned 1.0 — one *year* |
| `getVV` | unguarded `sqrt` of a possibly negative discriminant; divides by `d1*d2`, which vanishes at two strikes |
| `Vols.__init__` | `tenor_points` set inside the cross loop, so a workbook with no crosses raised `AttributeError` |
| `config` | hard-coded `/home/colin/Desktop/vol/files/` |
| GUI `calc()` | bare `except:` turning every failure into a silent `0.0000` |

## 3. Numerical robustness

* **Root finding.** Every `fsolve(f, 1.0)` — unbracketed, result never
  inspected — is now a bracketed Brent solve that raises `ConvergenceError`
  with a diagnostic if the target is unattainable.
* **Fixed points.** `while i < 10: v = f(v)` returned whatever it held after
  ten steps. Now iterated to tolerance with damping on oscillation.
* **Premium-adjusted call delta is not monotone** in strike — it rises, peaks,
  then decays to zero. The legacy solver could land on the branch below the
  forward. The peak is now located explicitly (verified against brute force to
  6 decimals) and the solve is confined to the correct branch. Asking for a
  delta above the peak raises instead of returning a wrong strike.
* **Closed forms replace solvers.** Unadjusted strike-from-delta and both
  delta-neutral-straddle strikes are analytic.
* **`z/x(z)`** in Hagan is 0/0 at the money; a Taylor expansion is used near
  zero instead of the raw ratio.
* **Integration.** The integrand jumps at every hour boundary and kinks at
  every event, so adaptive `quad(..., limit=500)` was the wrong tool. The
  integral is now split at those known breakpoints with a fixed Gauss-Legendre
  rule per smooth panel: raising the order from 5 to 20 changes the answer by
  0 in double precision.
* **Reproducibility.** Nothing calls `datetime.utcnow()` inside the model. A
  `Clock` is injected once, so a whole surface is built against one instant.

## 4. Performance

| Operation | Legacy | volkit |
|---|---|---|
| One strike vol query | full 12-parameter SVI optimisation, every call | cached slice |
| 50,000 strike vols | ~hours | 24 ms slice build + 0.2 ms |
| Full smile calibration, 9 tenors | — | 0.66 s |
| Event height calibration | nested `fsolve` over `quad`, per event | 4 ms |
| Whole 13-pair book | — | 5.2 s |
| Re-marking a curve and reading 9 tenors | — | 3.5 ms (was 60 ms, see below) |

The three structural causes were: rebuilding the smile per query, `scipy.stats.
norm`'s generic dispatch in the inner loops (`scipy.special.ndtr` is ~50×
faster), and adaptive quadrature on a discontinuous integrand.

**A fourth, found while building the market-maker fit.** `AtmCurve.invalidate`
dropped the intraday/holiday weight profile along with the cached integrals.
That profile is a pure function of the pair, the clock and the horizon — none
of which a backbone or event change can touch — and rebuilding it costs about
20 ms against 2 ms for every integral that genuinely had to go. Worse, each
tenor asks for a longer horizon than the last, so a nine-tenor read after a
parameter change rebuilt it several times over: 60 ms where 3.5 ms was needed.

`invalidate` no longer clears it. **No number changes** — the profile it was
discarding was identical to the one it rebuilt — but every re-mark got 17×
faster, which the Vol marking tab feels as well. A caller that mutates the
weighting or its calendars in place owns calling `weighting.clear_cache()`;
nothing in the package does. A test pins that the cache survives a parameter
change *and* that the numbers still move.

## 4b. Exchange traded options — new, and moves nothing

The listed-options tab is new functionality; the legacy tool had no equivalent
and it changes no existing mark. It is recorded here only because it reads the
same marked surface and could otherwise look like a second source of truth.

It is not. The panel **displays** the marked surface alongside a curve fitted
to an exchange's own settlement volatilities; it never writes to the book, and
no calibration, overwrite or event height is affected by anything done in it.

Two conventions in it are worth knowing before reading a difference as a
mispricing:

* **Inversion.** A CME yen future is quoted in USD per JPY. Its strikes are
  the reciprocal of USDJPY's, so the wings swap sides. Lognormal implied
  volatility is invariant under that inversion, so volatilities compare
  directly once strikes are mapped — but *deltas do not*, which is why every
  comparison is made at matched physical strikes and the reported risk
  reversals are the book's own delta strikes read off both curves. Negating a
  risk reversal to make an inverted contract line up would be §1.1 again.
* **Three things not corrected for.** Exchange settlement volatilities on
  American-style options are not European volatilities; a future is not a
  forward once rates move with the underlying; and the exchange settlement
  time is rarely an FX cut. Each is reported rather than absorbed.
* **A parameter may be given rather than fitted.** Alpha, rho and nu each have
  a box; blank means the fit decides, which is the default and reproduces the
  fit exactly as it was before the boxes existed. A number holds that
  parameter there and the rest are fitted around it, so the residuals are the
  best the free parameters can do *at* that value — the panel marks every held
  parameter, because one that was typed is otherwise indistinguishable from
  one the market implied. Nothing about the marked surface is affected either
  way.
* **Positions and aggregated risk.** A position book underneath the panels,
  also new and also moving nothing: it reads the curve each panel fitted and
  nothing else, and never touches the marked surface. Two things to know
  before reading its two columns as a disagreement. **Black-Scholes** is the
  closed-form Black-76 sensitivity at the option's own volatility with that
  volatility held fixed as the future moves — that is what a Black-Scholes
  greek is, and it is what an exchange's own risk file computes. **Smile**
  revalues the same position on the fitted SABR curve, with the forward
  bumped inside the parameters so the curve moves with the future. Both read
  one volatility at one strike, so the premium is the same in both and the
  entire difference is in the sensitivities. Money is totalled across
  contracts because every CME FX option settles in US dollars; a
  futures-equivalent delta is **not** totalled, because a euro future is not a
  yen future, and the total row is left blank there rather than showing a sum
  of unlike things.

## 4b-ii. The data files are no longer held open, and can be watched

Two changes to how files are read. Neither touches a number.

* **Nothing is held open.** `pd.ExcelFile(path)` keeps the file open for as
  long as the reader lives, and openpyxl's workbook is full of parent/child
  cycles, so the handle outlived the call that made it. On Windows that was
  enough to stop Excel saving the very sheet the tool had just read — a loaded
  historical workbook could not be saved. Every workbook now goes through
  `marketdata.open_workbook`, which copies the bytes and hands pandas a
  buffer, and the feed CSV is read whole and closed the same way. The legacy
  tool had the same lock, via `xlrd.open_workbook`.
* **`serve --auto-reload [SECONDS]`**, and the **auto-load** checkbox on the
  pricing toolbar, re-read the **market feed** when it is written. Only the
  feed: the workbook is the book of record and this session's marks are not in
  it, so reloading it is exactly what discards a morning's marking and it
  stays on its own button; the historical sheet is a record of what happened
  rather than a market. It is **off** unless asked for, and every re-read it
  performs is reported on the page. With it off, nothing about the tool's
  behaviour differs from before.

## 4b-iii. The CONFIG sheet is two columns, and moves nothing

The workbook's `CONFIG` sheet used to carry a `BASE` column of dollar pairs, a
`COR` column naming the crosses, and one further column per cross naming its
two legs — nine columns to describe thirteen pairs. It is now **two**: `PAIRS`
and `TENORS`.

A pair with the dollar on one side is marked on its own backbone. A pair
without one is a cross, and its two dollar legs are worked out from the name by
`cross.dollar_legs` — `AUDJPY` into `AUDUSD` and `USDJPY`, `EURGBP` into
`EURUSD` and `GBPUSD`, `EURCNH` into `EURUSD` and `USDCNH`. What is marked for
a cross is, as before, the **correlation** between its legs: `CrossAtmCurve`,
and the `initial` / `long term` / `MR` cells of its `PARAMS` column read as
correlation initial / final / decay. A leg nothing listed is added, because a
cross cannot be built without both of them.

Nothing about this was ever a decision. `EURGBP` has exactly one sensible pair
of legs, and writing them down thirteen times was thirteen chances to write one
upside down — which is not cosmetic, because a leg written the wrong way up
enters the triangle with the other sign, and that is §1.1 above. The derivation
is pinned by a test against the legs the shipped sheet used to name by hand,
and the whole book was rebuilt off both layouts at the same valuation time:
every ATM and every smile point agrees to **0.0**.

The old layout still loads, unchanged. A `COR` column is read as more pairs,
and a column named after a cross still names that cross's legs and **wins** over
the derived ones — a sheet that says something explicitly is not second-guessed
by a convention, and a desk that goes through sterling for `EURJPY` keeps doing
so. Anything the reader worked out rather than read is reported: `data.notes`,
shown in the page's message box and by `volkit check`.

One check was tightened on the way through. `cross.infer_leg_signs` asked only
that two legs share a third currency, so a mistyped column naming `EURUSD`
twice built `EURJPY` out of nothing and said so nowhere. Each side of the pair
now has to come from a different leg, and a column that cannot build its cross
is reported by name with the rest of the workbook's problems rather than
raising on the first bad cell.

## 4c. Analysis — new; two of its columns moved when the forward curve went in

The analysis tab is new. The legacy tool had `rv.py`, whose `RV.calc` wrote
results through chained indexing and so silently produced an empty matrix on
any current pandas; nothing else in it corresponds. No pricing or marking
number changes; two numbers *on this screen* did, and both are called out
below -- fair value, which gained the forward curve's price-side carry, and the
realized column, which is now measured on the forward rather than on spot.

The conventions in it worth knowing before reading a number off it:

* **Realized volatility is annualised on volatility time.** The weighting in
  this model multiplies the *instantaneous* volatility, so variance time is the
  integral of the squared weight -- weekends near zero, holidays reduced, the
  intraday profile not flat. A calendar year holds about 0.78 years of it.
  Annualising realized returns by calendar days, or by a flat 252, measures
  them on a different clock from the implied volatility they are being compared
  with, and lands roughly a tenth low. All three are reported; only the
  weighted one is like for like. `history.volatility_time` is pinned against
  `AtmCurve.integrated_vol` by a test, so the two cannot drift apart.

* **Skew and kurtosis are horizon-dependent.** The realized figures are of
  daily returns; the ones a smile implies are of the whole return to expiry.
  Under independence skewness falls as `1/sqrt(n)` and excess kurtosis as
  `1/n`, so the daily numbers are projected onto each tenor before being
  compared. Real returns are not independent, which is why the raw daily
  figures are shown beside the projected ones rather than being replaced by
  them.

* **The cross triangle for RR and fly is a choice, not an identity.** Two
  marginals and a correlation do not determine a joint distribution. The legs'
  densities are combined under a Gaussian copula, which assumes a tail
  dependence the market does not quote, and the change of measure between the
  legs' domestic currencies is not corrected for. The noise floor printed
  beside it -- the same machinery run on each leg alone -- is the floor below
  which a difference means nothing. The at-the-money row *is* an identity and
  is computed as one.

  The two at-the-money triangles differ by design: the variance triangle uses
  each leg's at-the-money volatility, the distribution triangle uses each leg's
  whole density. The gap is the convexity of the legs' smiles, typically a
  fifth of a vol point, and is what the book's own construction leaves out.

* **The forward curve's carry is reported twice, on purpose.** An option is
  worth `V(F, K, sigma, tau)` and the strike is held fixed across the horizon,
  so the forward rolling down its own curve reaches the number twice: through
  the *mark*, because the moneyness changes and the volatility marked at it
  changes with it (the `smile` column, in volatility points), and through the
  *price*, because `F` itself has moved (the `carry` column, in premium).
  Reporting only the first would leave a spot-hedged book's largest
  deterministic P&L off a screen headed "carry".

  Which of the two you actually earn is the hedging convention, and it is the
  whole of the number rather than a footnote. Hedged in the **outright forward
  to the option's own expiry**, the hedge rolls down the curve exactly as the
  option does and the two cancel exactly. Hedged in **spot**, which is what an
  FX options desk does, nothing rolls on the hedge side and the position keeps
  it. The panel takes the spot-hedged reading and says so; the same number is
  the cost of not hedging in the forward.

  Two details decided once. The carry is a **full revaluation** at the rolled
  forward, not `delta * (F2 - F1)`, so the gamma over the move is in it and
  `delta` is shown beside it as the first-order reading. And an at-the-money
  leg is read as **half a straddle**: the at-the-money is quoted as a straddle
  on this desk and a straddle at the delta-neutral strike has no delta, so the
  at-the-money row's carry is only the gamma over the move. Reading that leg as
  the call alone -- which is how the target legs are marked, because they only
  ever needed a *strike* -- handed the at-the-money row half a unit of forward
  carry nobody is running. Half a straddle rather than a whole one so that the
  vega column is still a single option's and no existing number moved.

  **This moves fair value.** The break-even gained a third term:
  `fair = realized + (T/h)*[roll*vega(T-h) + carry]/vega(T)`. The at-the-money
  is a delta-neutral straddle, so the new term is second order in the forward's
  move and lands around 0.003 volatility points on EURUSD at a 30-day horizon
  -- visible in the fourth decimal, not in a mark. Set `carry_value` aside (it
  is reported on its own column) to recover the old figure exactly.

  **And a break-even reads that carry delta hedged.** The carry column stays
  the whole revaluation -- that is the position's carry and the panel's
  subject. A break-even is a different question: a fair volatility belongs to
  the *strike*, and put-call parity puts the entire difference between writing
  that strike as a call and as a put into `delta * (F2 - F1)`, which is a
  direction rather than a volatility. `CarryRow.carry_hedged` is the carry
  with that term removed and is what fair value and the relative-value grid
  read; what is left is the gamma over the move, non-negative on either side
  by convexity.

  This moves two things and leaves the carry table alone.

  * *Fair value*, on a **premium-adjusted** pair only. The at-the-money
    straddle is delta neutral in the pair's own quoted convention; on an
    unadjusted one its `dV/dF` is exactly zero and no number moves at all
    (EURUSD is unchanged to the last bit). On USDJPY the delta-neutral strike
    is neutral in the *premium-adjusted* sense and carries a small `dV/dF`, so
    the old `carry_value` held a residual first-order term. The tell is that
    it grew with the tenor -- a delta term is linear in the forward move --
    from 0.0035 volatility points at 2w to 0.068 at 1y, where the gamma it was
    standing in for is flat at 0.0014. Richness moves by up to 0.067
    volatility points at the long end. Add `delta * (F2 - F1) * (T/h) /
    (2*vega)` back to `carry_value` to recover the old figure.
  * *The relative-value grid's carry signal*, on every pair with a forward
    feed, and this one was a real defect rather than a refinement. Read
    unhedged at a 25 delta strike the signal carried a quarter of the forward
    move with the option's own sign, so the put columns and the call columns of
    one row were pushed in opposite directions: on the sample marks a USDJPY
    one-year 25 delta put scored `+13.8` against the call's `-0.46`, and the
    grid's score changed sign across the strike axis for a reason that was not
    a mark. The at-the-money column showed almost none of it, being
    delta-neutral, which is what made the flip look like a property of the
    wings. Tests pin the call/put symmetry at one strike, the sign of the
    hedged term across every column, and the ATM column against
    `fair_value_table` as before.

* **Realized volatility is measured on the forward, not on spot.** A quoted
  volatility is the volatility of the forward the option is struck against.
  Writing the outright as `F = S exp(c tau)`,

  ```
  dlog F = dlog S + tau * dc - c * dt
  ```

  The first two terms are what moved -- spot, and the swap points *moving*.
  The third is the points *decaying* by one day of carry, which is a known
  slide and not a risk; leaving it in the sum of squares books the carry itself
  as volatility, which is exactly backwards for the pairs this matters for. It
  is removed and reported as a carry rate instead.

  **This moves the realized column**, by a few hundredths of a volatility point
  on G10 and by considerably more on a high-carry or managed pair. The
  spot-only figure is reported beside it and `--realized-basis spot` (or
  *Realized on: spot*) restores the old reading exactly. A tenor the sheet does
  not quote has its carry **interpolated** between the pillars it does, the way
  `feed.py` interpolates a live curve; falling back to spot on the misses --
  which is what a first cut did -- put two different measurements in one column
  and grew steps in the term structure of realized volatility at whichever
  tenors the sheet happened to quote.

* **The risk reversal and the butterfly are compared as `(rho, nu)`, not as
  moments.** A quoted spread is not a moment and a realized third moment is not
  a risk reversal, so the skew and kurtosis columns cannot answer for the
  wings. What both sides share is the two parameters a SABR smile is built
  from. `sabr.fit_smile_shape` reads the marked at-the-money, risk reversal and
  **smile butterfly** as the `(rho, nu)` that would show them -- the smile
  butterfly and not the market strangle `sabr.calibrate` matches, because
  matching a premium condition against a moment compares two different things.
  `history.vol_dynamics` measures the same two out of the sheet: under SABR
  with `beta = 1` the at-the-money volatility *is* the state variable, so `rho`
  is the correlation of daily spot returns with daily log changes in the quoted
  at-the-money and `nu` is those changes annualised on the same volatility time
  as everything else here.

  Three caveats travel with it, all reported rather than absorbed. The marked
  surface is SVI and not SABR, so the fit reports its own residual and a smile
  SABR cannot reach says so instead of returning the nearest thing in silence.
  SABR has no mean reversion and real volatility does, so `nu` rises at short
  tenors on both sides and must not be blended across them -- `nu*sqrt(t)` is
  the scale-free number that sets the shape. And where the sheet quotes no
  at-the-money column the dynamics fall back to a rolling realized volatility,
  whose changes are damped by the averaging, so that `nu` is a floor and is
  labelled one. The whole section is off unless asked for (`--sabr`) and moves
  nothing.

* **The curve comparison panel compares what it says it compares.** A curve
  from the workbook carries the *marked* at-the-money term structure and the
  *quoted* risk reversals and market strangles; one from the fitted surface
  carries the surface's own, read at a cut. The difference between the two is
  the fit residual and nothing else -- the same quantity the marking screen's
  implied-vs-quoted table shows, extended across the whole grid. A historical
  sheet's butterfly column, though, is whatever that desk quoted, and no
  header tells you which convention that was; the panel says so rather than
  differencing a market strangle against a smile fly in silence. Dated rows
  snap **backwards** to the last row on or before the date asked for, because
  a workbook has no weekend rows and snapping forward would compare a Friday
  mark against the following Monday's; each curve reports the day it landed on.

## 4d. Market making — new, and moves nothing by itself

The market-maker tab is new; the legacy tool had no equivalent. It is recorded
here because, unlike the listed and analysis tabs, it *can* write to the loaded
book — so the boundary needs stating precisely.

**By default it writes nothing.** `Panel.run` fits, reports, and then restores
the backbone parameters and the smile shifts exactly as it found them; a test
pins the whole ATM tenor table and the shift dictionary across a run. Ticking
**keep the marks** leaves the fitted values on the loaded book *in memory
only* — the workbook on disk is never touched by this screen, and a reload
discards them. The panel says so on every applied run.

Three things in it are worth knowing before reading a number off it.

* **The objective is a hinge, not a least squares.** There is no penalty
  anywhere inside a quoted bid and offer. That is deliberate and it is the
  whole brief: a market maker needs their mid *inside* the market, not sitting
  on somebody else's mid. A least squares through the quoted mids would satisfy
  none of a dozen quotes while a hinge satisfies all of them.

  A hinge has a flat bottom, so a small pull toward the quoted mids picks one
  answer out of the many that work. That pull, and the pull back toward the
  marked shifts, have to be **scaled to the market they compete with** — a
  parameter shift is O(0.1) and a volatility violation is O(0.001), so a raw
  weight of 0.02 on the shift is not a tie-breaker, it is twenty times the
  violation it is meant to defer to. Unscaled, the fit stopped short of a
  market it could reach and reported that it had converged. Both weights are
  shown on the panel so a fit being driven by them rather than by the market is
  visible.

* **The wing adjustment is curve-wide, and that is a modelling choice.**
  `VolSurface.param_shifts` moves a smile parameter's *level* across the whole
  term structure and leaves its shape alone. That is what re-marking a wing
  against a broker run means; an overwrite, which the marking screen already
  offers, would replace the parameter and flatten the term structure instead. A
  handful of quotes does not determine a shape, so where one shift cannot
  satisfy two tenors that disagree, the panel names the quotes it could not
  reach rather than bending the surface to whichever one the optimiser
  weighted most. Re-marking those tenors individually remains the marking
  screen's job.

* **The interpolation does not always pass through its own anchors.** The fit
  can read a 25- or 10-delta quote straight off the SABR wing instead of
  solving the interpolation, which avoids a 19 ms SVI solve per expiry per
  evaluation. That shortcut is exact *when the interpolation reproduces the
  anchors it was built through* — and the arbitrage-constrained SVI cannot do
  so when the marked anchors imply a butterfly arbitrage. On this workbook nine
  slices in fifty-two miss, USDCNY by 0.15 vol points at a week; those three
  are already flagged by the smile's own warning, the crosses miss by about
  0.01 and are not.

  So the shortcut is **measured, never assumed**: `anchor_gap` checks each
  expiry before the fit uses it, the answer is re-read through the full
  interpolation afterwards, and if the fitted shifts have moved the smile
  somewhere the interpolation can no longer follow, the whole fit is run again
  on the exact path. The quote sheet is always read off the interpolation,
  because that is what the rest of the tool prices on.

The knowledge bank (`mm_knowledge.json`) is new user data, created beside the
workbook the first time one is saved. It is never bundled into the executable
and its absence is not an error — an empty bank simply means no quote gets a
width until a rule exists or a fallback is typed, which the panel says outright
rather than filling in a plausible default.

## 4e. Events live on their own sheet, weighted per currency

An event used to be a bump on a pair: one dated row on `PARAMS`, one cell per
pair. It is now weighted **per currency**, and a pair's bump is its two legs'
weights **added** plus an adjustment the pair marks on top:
`bump = w[leg1] + w[leg2] + adjust`. The rule is one function,
`events.superpose`, and it adds because a quoted bump is a variance increment
over twice the day's volatility, so two of them add to first order; the exact
variance rule reaches the sum for bumps small against the volatility, which is
every real event on every real pair. Root-sum-square would be the rule for two
event *volatilities*, and a bump is not one.

### The EVENTS sheet

All of it lives on one sheet of the workbook, **`EVENTS`**: one row per
release, timed in the workbook's own Hong Kong clock, with a column headed by
each **currency** and a column headed by each **pair**.

| | `USD` | `JPY` | `USDJPY` | `EURUSD` |
|---|---|---|---|---|
| `2026-09-17 06:00` | 1.5 | 0.3 | 0.2 | |

A currency column is **shared** — every pair with that currency takes it — and
a pair column is that pair's **adjustment**, its alone. So that row is 2.0 on
USDJPY and 1.5 on EURUSD. `events.EventBook` is the sheet in memory and is the
one place an event is read from; a pair's schedule is *derived* from it
(`EventBook.for_pair`) rather than kept beside it, so a weight cannot come to
mean one thing on USDJPY and another on EURUSD.

**A dated row left on `PARAMS` is reported, not read.** Two homes for one bump
is how a weight comes to mean two things, so the reader names the row and tells
you to move it. The three shipped workbooks were migrated: their two event rows
moved from `PARAMS` to `EVENTS` unchanged.

### What moved

* Nothing, for a sheet whose events have no currency weights: the pair's cell
  is the whole bump (carried as the adjustment, with both legs at zero). That
  is what `vol_marks_legacy_format.xlsx` holds and it is pinned.
* `files/vol_marks.xlsx` now carries currency weights on both of its event
  rows (USD 1.0, EUR 0.9, GBP 2.0, CHF 1.0, AUD 3.0, NZD 4.0, CAD 2.0, CNH
  0.2), which the pair cells sit on top of. Blanking those columns restores
  the old bumps exactly.

### What was removed

The shipped economic calendar and its weight file are gone: `volkit/econ.py`,
`volkit/data/econ_events.csv` and `volkit/data/event_weights.csv`, with the
rule generators (`NFP` on the first Friday, a second-Wednesday `US CPI` proxy),
the `RELEASE_TIMES` table and the whole notion of a *suggested* event. Nothing
guesses a date any more and nothing ships one. The desk's own sheet is the
calendar, and it is the only thing that can be wrong about a date.

The **Auto-load** button on the Events card became **Reload**: this pair's rows
as the workbook has them, replacing what the session marked. `volkit events`
prints the sheet — whole, or through one pair's legs — instead of what
auto-load would have pulled in; `--horizon`, `--weights` and
`--set EVENT:CCY=POINTS` are gone with the calendar they addressed.

### Marking it, and getting it back

The Events card shows the sheet through one pair's eyes: its two legs' weight
columns and its own **Adj**, and the total beside them. Because a weight is
shared, typing one moves every pair with that currency — that is the point of
the sheet, and it is never hidden: the message under the table names them. The
**Weights** button opens the currency side whole; applying it re-solves every
pair those currencies reach and leaves each pair's adjustment column alone.

The session file carries the table once, at the top level as `event_table`
(replacing `event_weights`), and each pair block keeps its resolved `events`
for the record — that is what a re-mark of an event is diffed against. A file
written before the table existed has one **rebuilt from the union of its
pairs' schedules**, since a currency weight was shared even then; two pairs
disagreeing about one are reported rather than averaged.

`volkit session --to-workbook` writes the `EVENTS` sheet **whole**, from that
one table, rather than pair by pair. The old export wrote one pair's view of a
shared weight and then had to go back and cancel it in every other pair's
column; a table written whole has nothing to cancel.

## 5. Things I did **not** change

* The backbone model itself: mean-reverting instantaneous vol, short-end
  add-on, decaying event spikes.
* Your calibrated intraday hourly weight profile (carried over verbatim).
* The weekend (0.35) and holiday (0.5) weights.
* The event decay constant (5000/yr).
* The whole-day quoting normalisation in `getCutVol` (`× sqrt(t/t0)`).
* The per-tenor-then-interpolate structure of the smile parameters.

## 6. SABR calibration: what was checked and what changed

The question "is there a more robust way to solve SABR from market risk
reversals and flies" was tested rather than assumed.

**Finding: the previous approach was already sound.** With alpha eliminated
analytically the problem is only two-dimensional in `(rho, nu)`, and it turns
out to be very well conditioned. Deliberately bad starting points — `rho=+0.9`
with high vol-of-vol, `rho=-0.99` with low — all converge to the same answer on
every realistic quote tried, including 1W steep skews, EM-style 28% vol quotes
and near-zero flies. No seed-sensitivity failure could be produced.

Four things did change:

1. **Alpha is now closed form.** Hagan's at-the-money condition is a cubic in
   alpha (a quadratic for the lognormal beta=1 case used in FX); the market
   convention is the smallest positive root. Solving it exactly is faster, but
   the real gain is diagnostic: it shows immediately when *no* positive root
   exists for a `(rho, nu)` pair, and when *two* do. Both occur —
   `rho=-0.3, nu=0.8, t=1` has roots at 0.0768 and 17.36; `rho=-0.9, nu=3, t=5`
   has none. The old numeric search reported the second case as an opaque
   bracketing failure.
2. **The admissible box is swept before polishing.** A 13x9 grid over
   `rho` and `nu*sqrt(t)` locates the global basin before any local fit. Because
   each node uses the closed-form alpha it is pure algebra and costs almost
   nothing — the full 13-pair book went from 5.2s to 6.1s. This does not change
   any answer on the sample workbook; it changes "worked on everything tried"
   into "swept the whole domain".
3. **Non-uniqueness is detected and reported.** Healy (2025) shows the three FX
   quotes need not determine a smile uniquely and that nearby quotes can jump
   between solutions. `SabrCalibration.alternatives` carries any competing
   parameter set, and `volkit validate` sweeps for them.
   **On your workbook the result is clean: all 254 tenor/wing calibrations have
   exactly one solution, and no at-the-money condition has multiple alpha roots.**
4. **Regularisation is available.** Tenors are fitted short to long, and
   `fit_smiles(prior_weight=...)` pulls each tenor toward the one before it.
   This is off by default; turn it on if a parameter term structure ever looks
   like it is jumping between equally good fits.

A real crash was found while stress-testing: a sweep node implying an absurd
smile made `strike_from_delta` raise `OverflowError` from inside `math.exp`.
Black-76 now rejects a total volatility above 20 with a clear message.

**On external libraries.** `pysabr` — the old dependency — is a thin
unmaintained Hagan wrapper and offers nothing the in-house implementation
lacks. `QuantLib` is the industrial option and has genuinely useful FX
machinery (`BlackDeltaCalculator` handles premium-adjusted deltas and the
market-strangle convention), but it is a large dependency for a desk tool that
otherwise needs only numpy/scipy/pandas, and it would not have changed any
number here. `pyfeng` is research-grade and worth a look only if you ever want
the exact or Antonov SABR rather than Hagan's expansion. The recommendation is
to stay self-contained; the calibration is now bounded, swept and diagnosed,
which is what the literature actually asks for.

Sources: [Healy, *Counterexamples for FX Options Interpolations II*](https://arxiv.org/pdf/2512.19625) ·
[Xiong, *Calibration of SABR*](https://modelmania.github.io/main/Files/Docs/Changwei_Xiong_SABR_Calibration.pdf) ·
[QuantLib-Python volatility docs](https://quantlib-python-docs.readthedocs.io/en/latest/termstructures/volatility.html) ·
[pyfeng SABR](https://pyfeng.readthedocs.io/en/latest/sabr.html) ·
[pysabr](https://github.com/ynouri/pysabr)

## 7. Known issue worth a decision

**Events near the volatility-day roll.** The spike has a ~1.2 hour half-life
but the volatility day rolls at 14:00 UTC. An event timed shortly before the
roll has most of its variance land in the *following* day, so its height is
solved against only the sliver that falls inside its own day. The legacy model
had exactly the same behaviour with no way to observe it; volkit now warns:

```
event 03Mar 13:30 falls 0.5h before the 14:00 UTC volatility-day roll,
so only 21% of its spike prices into the day it was quoted for
```

Options are to shift the day boundary, raise the decay, or anchor the event
window to the event itself. This is a convention choice, so I left it alone
and made it visible.

Related, and now visible in the events table: the volatility day rolls at
**14:00 UTC**, a fixed hour inherited from the legacy `NYCut = 14`. The New
York cut is really 10:00 *New York*, which is 14:00 UTC only in winter. So a
2pm-ET FOMC decision lands in the volatility day *after* the one a marker
probably has in mind, and in summer the roll itself is an hour out. The events
table shows the volatility day each bump actually prices into and flags the
ones that roll over. Making the cuts DST-aware would move marks, so it is
flagged rather than changed.

---

# Convention audit (vol day, events, weekends, holidays, cuts)

Every convention was tested against what it should be rather than read.
Findings below; **fixed** items are done, **flagged** items move marks and are
left switchable.

## Fixed

### F1. Event bumps were anchored to the wrong window — *serious*

A quoted bump was applied to the NY-cut volatility day containing the event.
An event shortly before the 14:00 UTC roll therefore had only minutes of its
own day left, so the solved height exploded and the overflow landed on the
following day:

| event time | height | own day | **next** day | next-day bump |
|---|---|---|---|---|
| 13:59 | **2.0374** | 8.71% | **42.31%** | **+35.59** |
| 13:45 | 0.5032 | 8.71% | 13.05% | +6.33 |
| 13:00 | 0.2639 | 8.71% | 8.50% | +1.78 |
| 14:00 | 0.1645 | 8.72% | 6.73% | +0.00 |

A one-minute change in an event's timestamp moved the 3m term vol from 6.03%
to 7.47%. This was live: the shipped NFP calendar sits at 13:30 UTC in winter,
squarely in the danger zone.

The bump now applies to the **24 hours following the event**, which is what
"FOMC adds two vols" means and is independent of an arbitrary boundary.
Heights are stable (0.1645–0.2116 across the same timestamps) and each event
delivers its quote exactly. `EventSchedule(window_mode="vol_day")` restores
the old reading for comparison.

### F2. Clustered events were calibrated independently

Each height was solved as if its event were the only one. Two events quoted at
+2.00 each produced a +4.01 day and *neither* delivered +2.00 marginally once
both were on. Heights are now solved **jointly** by Gauss-Seidel; each event
delivers its own quoted marginal bump (verified at 2h, 6h and 24h spacing).
Events more than a few hours apart do not interact, so this converges in one
sweep and costs nothing in the normal case.

### F3. UK holidays used the US observation rule

`_observed` moves a weekend holiday *backwards* to Friday, which is the US
federal rule. The UK moves *forward*. With Christmas on a Saturday the model
gave 24 and 27 December; the actual UK substitute days are 27 and 28. Verified
against 2021, when the substitute days were indeed Mon 27 and Tue 28.

### F4. Events on weekends and holidays passed silently

An event timestamped on a Saturday was calibrated against a nearly dead window
and accepted. Almost always a wrong date; now flagged, along with events on a
holiday for that pair.

### F5. SGD had no calendar at all

`CURRENCY_CALENDARS` mapped SGD to "SG", for which there were no built-in
rules — so without the optional `holidays` package every Singapore holiday
silently did not exist. Solar-fixed Singapore dates added; the lunar ones still
need overrides.

### F6. The first daily-vol bucket is partial

It runs from the valuation instant to the next cut, so it is usually shorter
than a day but was annualised over its own span and read as a day vol. Rows now
carry `hours`, `partial` and `cumulative_defined`.

### F9. The managed-band warning only ever looked at a barrier

`pricing._price_leg` checked a strike against the managed band by reading
`leg.barrier`. Two things followed from that. A **vanilla or a digital struck
outside the band went through with no warning at all** — the one product a
band matters most for was the one product nothing was said about. And a
barrier left behind on a leg whose product had since been changed to a
vanilla was checked *instead of* the strike, so the warning, when it came,
was about a level the price did not depend on.

The check now reads whichever level the payout actually depends on: the
barrier for a touch, the strike for everything else. It is also given the
leg's own interpolation, so a leg already priced with `BAND` is not told to
switch to `BAND`. **No price moves** — `band_check` only ever produced text —
but a pegged pair will now say more than it used to.

### F10. ISO 8601 was written everywhere and read nowhere

`timeutil.parse_datetime` had a list of tabular formats and no ISO 8601 in it,
while the tool writes ISO 8601 all over: the valuation stamp in `/api/state`,
the timestamps in a session file, the value a browser's `datetime-local` field
carries. Three callers had each patched around it locally, and they had
patched it *differently* — `listed._normalise_expiry` understood a trailing
`Z` and an offset, the events route and the session loader understood neither
— so one string parsed on one screen and failed on the next, and `--asof` on
the command line refused a timestamp copied off the tool's own header.

Worse than the inconsistency: the one caller that did handle an offset
**dropped** it and then stamped the result UTC, so `2026-09-11T19:00+09:00`
was read as 19:00Z. A nine-hour error in a listed expiry, arrived at in
silence.

ISO 8601 is now a fallback inside `parse_datetime` itself, tried only after
every existing format has been tried, so nothing a workbook or a paste already
parses moves. An offset is **converted**, not discarded. The three local
workarounds are gone.

### F11. `available: []` said the workbook was empty

`Book.__getitem__` reported the pairs it had *built*. `build` and `load_all`
can be narrowed -- `volkit band USDHKD` narrows them to the one pair asked
for -- so when that pair is not in the workbook nothing is built at all and
the message read `'USDHKD' is not built; available: []`, which says the
workbook is empty when what is actually wrong is that it does not carry this
pair. A pair the workbook has never heard of is now told what the workbook
holds; a pair it has, but which this book was not asked to build, is told
that instead.

### F12. A loaded feed was invisible to every cross

Every level lookup asked the feed for the pair **by name**. A feed carrying
EURUSD and USDJPY therefore had no EURJPY in it, and the market-maker screen
answered a quote written against an absolute strike with *"there is no forward
feed for EURJPY"* while the pricing screen was quoting both of its legs off
the same file. The same silence sat under the carry table (no forward carry,
no smile slide), the relative-value grid (no absolute strikes, no regime z),
the strike axis of the marking screen's chart, and a pricing leg left with a
blank spot.

`Book.market_level` now composes a cross from its legs when the feed does not
quote it directly, and every other lookup goes through it -- `analytics
._forward_at`, `relvalue._spot_and_forward`, `pricing._resolve_market` and the
`has_feed` flags on the analysis and band routes each had their own copy of
the by-name test. The composition is the triangle and not a model: EURJPY is
EURUSD x USDJPY, EURGBP is EURUSD / GBPUSD, with the orientation taken from
`cross.infer_leg_signs` -- the same signs the variance triangle uses, read as
quotation rather than as correlation. The points that come back are the
cross's own, in the cross's own pips, and never the legs' points added: a
point of EURUSD and a point of USDJPY are different amounts of money.

**What moves.** Nothing on a pair the feed quotes itself, and nothing at all
without a feed. On a cross whose legs the feed carries, figures that were
previously unavailable now have values: the forward carry columns, the smile
slide, the relative-value carry signal and its `regime_z`, the chart's strike
axis, and an absolute-strike row on the market-maker sheet. A leg with a blank
spot is now filled from the triangle instead of falling back to a spot of 1.0.
Every one of them says which two legs it was built from; `derived` and `via`
travel with the level. Half a triangle is still a refusal -- no NZDUSD in the
file, no GBPNZD forward -- and a pair with no legs at all (USDCNY) is
unchanged. To get the old behaviour back, remove the legs' rows from the feed
file: the tool never invents a level it was not given one way or the other.

### F13. A low at-the-money made a historical sheet read as decimals

The historical workbook's volatility unit was decided per sheet from the
at-the-money column, on the argument that a quoted at-the-money is somewhere
between 2 and 60 in points and between 0.02 and 0.60 in decimals, so nothing
sensible sits near 1. That is true of a free-floating pair and false of a
managed one: USDHKD marks its 3M at about a third of a volatility point. A
sheet holding it was read as a sheet of **decimals**, so 0.35 came back as
0.35 in the model's own units and every screen that prints points showed it
as **35.00** -- a hundred times the mark, on the monitor, which is the screen
a desk opens first, and with the risk reversal and butterfly beside it scaled
the same way.

The level is no longer read as evidence of a unit. On `auto` a historical
sheet is read **as written, in volatility points**, one scale for the whole
sheet as before -- that part was never the problem, and per-column sniffing
still returns a -0.89 risk reversal a hundred times too large. A sheet that
really is quoted in decimals is loaded by name: `--vol-unit decimal` on the
command line, `vol_unit` on `/api/history`, `vol_unit='decimal'` in
`load_history`. A sub-1 at-the-money is reported once per sheet, in
`History.problems`, saying it was read as points and how to say otherwise --
it is the one reading somebody might have meant the other way.

**What moves.** Nothing on any sheet whose at-the-money is above 1.0, which
is every G10 sheet and the shipped sample. On a sheet quoting a pegged or
otherwise low-volatility pair, everything read off it moves by a factor of
100 and into the right place: the monitor's tiles and curve comparison, the
realized columns, the relative-value score, its scale and its z's, and the
historical percentile. To get the old numbers back, load that sheet with
`--vol-unit decimal`.

### F14. `volkit vol --strike` is a strike, not a moneyness

The marking screen gained a **vol query** card -- an expiry, a strike, and the
volatility there -- and it is the same function as the `vol` subcommand, for
the reason every screen here has a command-line equivalent: two readings of
one strike is two ways for one strike to be read.

That made `vol` take the pricing screen's strike box rather than a float:
`ATM`, an absolute level, or a delta (`25d`, `10dp`, `-25d`). An absolute
strike needs a forward to be placed against marks that live in strike/forward,
and that forward now comes from the **feed** at the expiry asked for
(`Book.market_level`, so a cross the file quotes only through its legs is
placed from them) instead of from `--forward`, which defaulted to `1.0`.

**What moves.** `--strike X` with no `--forward`. It used to mean the
moneyness `X`; it now means the strike `X`, placed against the feed's outright
-- which is what the number was always written as in the documented examples,
`volkit vol USDHKD ... --strike 7.90` among them, and what the pricing screen
has always meant by it. On the sample feed `vol USDJPY 2024-05-28 --strike
1.02` goes from 6.203 to 48.121 volatility points: 1.02 against a forward of 1 is
a hair above the at-the-money, and against a USDJPY forward of 150 it is the
far downside. With no feed for the pair an absolute strike is now **refused by
name** rather than silently read as a ratio; `ATM` and a delta still answer, in
`K/F`, exactly as the smile chart's axis does. Nothing changes where
`--forward` was given, which is every example in the README and the manual.
**To get the old figure back, pass `--forward 1`.**

### F15. The relative-value score is in volatility points, not standard deviations

The Analysis screen's relative-value grid scored each cell as a **z**: every
signal divided by that cell's own historical standard deviation, and the
composite the weighted mean of those. The reasoning was sound and is still
written down -- half a volatility point is a great deal on a one-year
at-the-money and nothing on a one-week 10 delta wing, and only the history
knows which. What it answered, though, was *how unusual is this*, which is a
statistic about a series; what a marker asks the grid is *how much am I being
paid*, which is the number the mark is moved by and the number the price is
made in. A headline figure that has to be translated before it can be traded
on is the wrong headline figure.

So the score is now the weighted mean of the signals' **values**, in
volatility points, renormalised over the ones each cell has exactly as before.
Nothing else about the grid changed: the same five signals, the same weights,
the same renormalisation, the same structural-zero rule at the at-the-money,
and `level + shape + carry` is still exactly the richness.

**What moves.** Every number in the *score* row of `volkit analysis
--relative-value`, the big figure in every cell of the screen, `summary
.mean_score`, `summary.headline` and the tint -- all of them by the cell's own
scale, so a one-week wing moves by a different factor from a one-year
at-the-money and the *ordering* of the grid changes with them. On the sample
marks USDJPY 1Y 10d put reads `+0.767` volatility points where it read `+17.12`
standard deviations. `EXTREME_SCORE`, which the summary counts outliers past
and the screen saturates its tint at, is **0.005** -- half a volatility point
-- where it was `2.0` standard deviations.

**What is gained, and it is the reason this is not a pure unit change.** A
scale needs the historical sheet, so a cell without one used to score
*nothing at all* -- a dash in every column of every tenor -- while `level`,
`shape` and `carry` had each been measured perfectly well. Only `history`
needs the series now, and everything else is scored on its own points. On a
pair the sheet does not quote the grid goes from empty to scored on whatever
was measurable, with `confidence` saying how much of the declared weight that
was.

**What is kept.** The standardisation is context rather than the composite: a
signal still carries its `z` wherever a scale can be measured, the cell still
carries `scale` and `scale_source`, and both are shown -- in the detail card's
own column, and in the `z` columns of the command line's attribution block.
The `z` now follows the **scale** rather than the score, so it is present
wherever the history can measure one and absent where it cannot, whether or
not that signal was counted.

**To read the old figure**, take any signal's `z` off the attribution block or
the cell detail and combine them at the same weights: the arithmetic is
unchanged and the grid still reports every part of it.

### F16. The same unit sniffing was still in every pasted market

F13 took the level out of the unit decision for a historical *sheet*, and
left it in four readers that take a paste: a broker run (`quotes.parse_quotes`
on `auto`), the listed-option quote table (`listed.parse_quote_table` on
`auto`), the pasted curve on the monitor's comparison panel
(`curves.parse_pasted_curve`) and the market maker's pasted target curve.
Each read a paste whose levels were all below 1.0 as **decimals**, and each
refused a paste whose levels straddled 1.0 as ambiguous. Both are the same
mistake F13 fixed: a managed pair marks its at-the-money at a third of a
point, so its whole broker run, its whole listed table and its whole curve sit
below 1.0, and a run showing a 0.35 at-the-money beside a G10 line at 8.20 was
refused outright rather than read.

A volatility is now **the number it was written as** in all four, on the one
rule that a number is a number: 8.20 is 8.20 volatility points and 0.35 is
0.35 of a point. A paste that really is in decimals is read by saying so --
`vol_unit='decimal'`, which is the caller's word and never an inference. Where
every level in a paste sits below 1.0 the reader says once, in its notes, that
it read the paste as written and how to say otherwise. Nothing straddling 1.0
is refused any more, because there is nothing left to be ambiguous about.

**What moves.** Nothing in any paste whose levels are above 1.0, which is
every G10 run and every shipped sample. A paste of a pegged or otherwise
low-volatility pair moves by a factor of 100, into the right place: the
market-maker fit and quote, the marking agent's target, the desk agent's
widths and everything filed into the archive from a paste, the listed
comparison, and the monitor's pasted curve. To get the old numbers back on a
paste that really was in decimals, set the volatility unit to `decimal`; the
pasted curve and the market maker's target curve have no such switch, because
they are read in points and there is now nothing else they can be in.

### F17. The expiry box refused a tenor spelled out, and a date without a year

Two things a desk writes were refused by the parser rather than by anything
about the market. A tenor's unit had to be a single letter, so `1wk`, `3mth`,
`2yr` and `10 days` all came back as "cannot parse tenor" while `1W`, `3M`,
`2Y` and `10D` worked; and a date had to carry a year, so `06 Nov` -- the way
a date is written on a run sheet in the week you are trading it -- was not a
date at all.

Both are read now, everywhere a tenor or an expiry is read, because both go
through the one reader each (`timeutil.parse_tenor`, `timeutil.parse_datetime`):
the pricing screen's expiry box, the marking screen's vol query, a broker run,
a request list, a chat file being ingested, and the indication rows in the
analysis screen. A spelled-out unit is the tenor it names -- `1wk` *is* `1W`,
and `normalise_tenor` still gives one spelling per pillar, so nothing acquires
a second column. A year-less date is the **first** matching day on or after
the reference date: on 1-Sep-2026 `06 Nov` is 6-Nov-2026 and `31 Aug` is
31-Aug-2027. `29 Feb` is the one date whose next occurrence is not within a
year, and it is answered rather than refused.

The reference date is the **book's clock**, passed in (§4) and never the
machine's, so the same box read twice reads the same way and a saved session
reads back as it was written. With no clock behind the call a year-less string
is refused by name, saying that is what is missing. Purely numeric year-less
forms (`06/11`) stay refused: day-then-month in one country and
month-then-day in another, with nothing in the string to say which.

**What moves.** Nothing that already parsed: every spelling that worked before
resolves to the same date, and the year-less and spelled-out forms were errors
rather than numbers. What moves is that they are now expiries. One reading did
change shape: `analytics` decided tenor-versus-date by counting characters
(four or fewer was a tenor), which called `1week` a date; it now asks
`parse_tenor`, like everywhere else.

## Verified correct

* **Weekly close window** — Friday 22:00Z to Sunday 22:00Z, exactly 48 hours,
  correct at every boundary hour.
* **Daily variances sum to the term variance** to 2.4e-16, so the daily series
  and the term structure cannot disagree.
* **Event window vs decay** — the spike is down to 1e-6 of its height after 24
  hours, so the one-day lookup window never binds.
* **The day-count roll at 22:00Z** — the quoted basis drops a whole day, so the
  same expiry reprices across it. That is the convention, not a bug.

## Fixed on request: daylight saving

### F7. Cut times now resolve through their local time zone

A cut is a local time in a city, so its UTC hour moves twice a year. The fixed
table was an hour out for the New York cut every winter and two hours out for
London. `dst_aware_cuts` now defaults to **True**:

| cut | old fixed | local definition | winter | summer |
|---|---|---|---|---|
| TK | 06:00Z | 15:00 Tokyo | 06:00Z | 06:00Z |
| NY | 14:00Z | 10:00 New York | **15:00Z** | 14:00Z |
| LDN | 13:00Z | 15:00 London | **15:00Z** | **14:00Z** |
| HK | 03:00Z | 15:00 Hong Kong | **07:00Z** | **07:00Z** |

**This moves marks.** The legacy `HK = 03:00Z` was 11:00 Hong Kong, not a 3pm
cut at all — check what your desk means by it, and set
`AtmCurve(dst_aware_cuts=False)` to restore the old fixed hours.

### F8. The weekly close now follows New York, not UTC

The FX week closes Friday 17:00 New York and reopens Sunday 17:00 New York —
22:00Z in winter but 21:00Z in summer. The model previously used 22:00Z all
year, so it treated the market as open for an hour after it had shut every
summer Friday. Still exactly 48 closed hours in both seasons.

## Still flagged: not changed

### G1. (superseded — see F7)

| cut | model | actual local definition | winter | summer |
|---|---|---|---|---|
| TK | 06:00Z | 15:00 Tokyo | 06:00Z ✓ | 06:00Z ✓ |
| NY | 14:00Z | 10:00 New York | **15:00Z** | 14:00Z ✓ |
| LDN | 13:00Z | 15:00 London | **15:00Z** | **14:00Z** |
| HK | 03:00Z | 15:00 Hong Kong | **07:00Z** | **07:00Z** |

The NY cut is an hour out every winter. The legacy "HK" hour of 03:00Z is
11:00 Hong Kong, not a 3pm cut at all — worth confirming what your desk means
by it. `AtmCurve(dst_aware_cuts=True)` resolves all four through their local
time zone; `CUT_LOCAL` holds the definitions.

### G2. The weekly close is 22:00Z year-round

New York 17:00 is 22:00Z in winter but 21:00Z in summer, so the model treats
the market as open for an hour after it has shut, every summer Friday.

### G3. A same-day expiry cannot be priced

`cut_vol` normalises by whole volatility days, so an expiry on the current
quoting day has a zero day-count and returns zero volatility — which then
fails as an input to Black. Inherited from the legacy `if t0 == 0`. Overnight
and same-day options need a different quoting basis.

### G4. Half-day holidays are not modelled

Christmas Eve, the day after Thanksgiving and similar early closes are treated
as full trading days.

---

# Managed and pegged currencies (USDHKD)

## Correction to the earlier write-up

An earlier version of this section treated **zero out-of-band probability as
the goal**, and the model defaulted to it. That was wrong. The peg can break,
that probability is real, and it belongs in the price. What a lognormal smile
gets wrong is not that the probability is positive — it is that the probability
is enormous and has the wrong shape. The model below was rebuilt accordingly:
breach probability is now a calibrated **output**, and the modelling effort
goes into the jump, not into suppressing it.

## What is wrong with a lognormal here, measured

USDHKD is held inside 7.75–7.85 by the HKMA's Convertibility Undertakings.
Fitted to plausible USDHKD quotes, the lognormal SVI surface in this package
puts **6.53%** of the three-month distribution outside the band and implies a
**4.43%** chance of trading above 7.85 in three months. Published work is
starker: naive Black-Scholes on HKD options implies an average 90-day
probability of about **67%** of breaching 7.85. The realised distribution is
also **U-shaped** — because the authority defends the edges, the rate spends
most of its life near 7.75 or 7.85 rather than near 7.80.

So a lognormal is wrong twice: the tail is far too fat, and the body is the
wrong shape.

## The model

`volkit/banded.py` is a regime mixture:

    with probability exp(-lambda T)   the peg holds:
        x = (S_T - L) / (U - L) ~ Beta(a, b)
    otherwise it has broken, into one of two regimes:
        S_T ~ lognormal(F e^{+j_w}, sigma_w)    weak-side  (USDHKD higher)
        S_T ~ lognormal(F e^{-j_s}, sigma_s)    strong-side

Beta carries the peg-intact regime because its support is exactly the band and
it is **U-shaped whenever a < 1 and b < 1**, which a lognormal or logit-normal
cannot be. Its partial moments are closed form, so prices need no quadrature.

Three things matter about the jump leg:

* **It is a hazard rate, not a per-tenor probability.** `P(break by T) =
  1 - exp(-lambda T)` composes across the term structure, so one marked lambda
  gives a consistent breach probability at every expiry. A probability marked
  per tenor does not.
* **It is two-sided and asymmetric.** Jump size and post-break volatility are
  marked separately for each side, with a share splitting them.
* **It feeds back into the body.** The risk-neutral forward constraint fixes
  where the peg-intact mean must sit, so expected devaluation pushes the
  in-band distribution to the *strong* side. At a 2%/yr hazard with a +6%
  weak-side jump the 3M in-band mean sits 0.0018 below the forward.

That constraint also collapses the calibration to a one-dimensional monotone
solve: it pins `a / (a + b)`, and the Beta concentration is then set by
repricing the at-the-money option. Forward and ATM are matched to 1e-9.

## Breach probability as an output

With a 2%/yr hazard and +6%/−4% jumps:

| tenor | P(broken) | P(outside band) | P(> 7.85) | P(< 7.75) |
|---|---|---|---|---|
| 1M | 0.167% | 0.162% | 0.138% | 0.025% |
| 3M | 0.499% | 0.471% | 0.371% | 0.101% |
| 6M | 0.995% | 0.941% | 0.678% | 0.263% |
| 1Y | 1.980% | 1.890% | 1.237% | 0.653% |

Breaking does not guarantee leaving the band — a small jump can land inside —
so the two columns differ. The analytic figures match Monte Carlo to four
decimals.

## Two diagnoses the model will give you

**An ATM floor.** Break risk alone produces volatility, so a given jump
specification implies a minimum ATM vol. A 2%/yr hazard with +6%/−4% jumps
already implies more than a 0.44% two-year ATM — so that hazard and that quote
cannot both be right. The model says so rather than failing to converge:

    USDHKD at t=2.0000y: a hazard of 2.0000%/yr with +6.0%/-4.0% jumps already
    implies an at-the-money volatility of at least X%, above the quoted 0.4424%.

Dropping the hazard to 1%/yr fits the two-year fine. This tension *is* the
peg-credibility question, made arithmetic.

**A forward-constraint failure.** With enough expected devaluation the
peg-intact mean would have to sit outside the band for the forward to match at
all; that is reported separately.

## Inverting the wings into a hazard

`solve_hazard=True` backs the break intensity out of the quoted wings — the
question the peg-credibility literature asks. It now responds properly to the
assumption (an earlier version silently returned its own input, because an
infeasible bracket end was swallowed by a bare `except`):

| assumed weak-side jump / post-break vol | implied hazard | P(outside band, 3M) |
|---|---|---|
| +3% / 6% | 9.93%/yr | 2.18% |
| +6% / 10% | 4.99%/yr | 1.17% |
| +12% / 18% | 2.44%/yr | 0.59% |
| +25% / 30% | 1.13%/yr | 0.28% |

The implied hazard is only as good as the assumed jump size, which is why the
table is reported alongside it rather than a single number.

## Fitting the break regime from both wings at both deltas

`solve_hazard` inverted one number from one instrument. The calibration is now
two stages with the second generalised: stage A (`_BodyFit`) is the exact
forward-and-ATM solve above, unchanged; stage B (`_fit_break`) reads any
subset of the break parameters — `hazard`, `weak_share`, `weak_jump`,
`strong_jump`, `weak_vol`, `strong_vol` — off the 10 and 25 delta risk
reversals and butterflies by least squares, sweep then polish, with the body
profiled out exactly at every point visited. `calibrate_band_wings` does one
tenor; `calibrate_band_term_structure` does the whole curve under one shared
regime, one body per tenor, and reports each tenor's own implied hazard beside
the shared one. `fit_band_treatment` is the surface-level entry behind the
band card's **Fit from the wings** and `volkit band --fit`; it proposes and
marks nothing.

**Nothing moves.** `solve_hazard=True` is now the one-parameter, one-instrument
case of `_fit_break` — the hazard against the strangle premium at one delta,
bracketed — and agrees with the old solver to 5e-15 across the table above
and the "cannot reach" note. The fly residual is the strangle *premium* gap
over the strangle's vega for exactly that reason: an average of the two legs'
own vega-normalised gaps has a different root whenever the two vegas differ,
and it did, by 5e-5 in the hazard, before the residual was changed.

Residuals are in volatility (price gap over vega), so a one-week fly and a
one-year risk reversal weigh the same. The identifiability is measured at the
answer by a finite-difference Jacobian: per parameter, how far the residual
vector moves over the parameter's whole admissible range (below 1e-4 vol it is
reported *not informed*), and across parameters the condition number with the
near-degenerate pair named above 1e3. Measured, this contradicts the argument
§6 of `CLAUDE.md` used to make: the hazard against the jump size, from strikes
all inside the band, conditions at about 15 on USDHKD quotes — the forward
constraint moves the body with the jump and that is visible from inside. What
is degenerate is freeing the post-break volatilities beside the hazard and the
share on a low-volatility tenor. The jump sizes are held by default because
they are a policy view, not because the quotes could not see them.

On the synthetic marks a planted regime (3%/yr, 70% weak side) is recovered
from one tenor's four quotes to 1e-6 and from four tenors' sixteen to the same,
with every per-tenor hazard flat at 3%; a wrong given jump size shows as a
0.014 vol-point residual and a per-tenor hazard that slopes. A tenor no hazard
can fit inside its band sits out with its reason rather than zeroing the shared
hazard ceiling and taking the curve down with it.

## What the band alone does and does not explain

Calibrated with no break risk the model produces a **negative** risk reversal
of −0.018% against a quoted +0.220%. Add a 2%/yr hazard with asymmetric jumps
and the model risk reversal rises to +0.142%. **Most of the quoted USDHKD skew
is a peg-break premium, not a band-shape effect** — which is exactly why the
jump leg, not the boundary, is where the modelling effort belongs.

## Putting it on the surface

Until now this model was library and CLI only. The obstacle was not the model:
`VolSurface` works in **strike over forward** and a band is an **absolute**
price range, and there was no honest way to place 7.75–7.85 against a moneyness
of 1.02. That plumbing is now in place, and the rule it follows is worth
stating because it is the only place the two spaces meet.

The regime mixture is **scale invariant** — jump sizes are logarithmic and the
post-break volatilities are relative — so dividing the band edges and the
forward by the same number moves the whole model into moneyness exactly. The
number is the outright forward at the expiry, taken from the spot/forward feed.
`VolSurface.band_for_slice` does it, and does three things rather than two:

* a slice built at an outright forward that the band contains is already in the
  band's space and is left alone;
* a slice in moneyness divides the band by the feed's outright;
* with **no feed**, it refuses and names the feed. A guessed level would put a
  hard barrier in the wrong place, which is worse than no answer. So does a
  forward the band does not contain — either the peg has moved and `PEG_BANDS`
  is stale, or the feed is wrong, and both are worth knowing.

`BAND` is then an interpolation method like `SVI` or `VV25`, available anywhere
a method is chosen. It moves no existing mark: it is a new method, and every
pair without a band refuses it by name.

What is marked alongside it is a `BandTreatment` — the hazard, the jump sizes
and post-break volatilities, an override of the band edges, and a blend weight
against the lognormal smile. Three points of discipline in it:

* **The treatment is part of the smile cache key.** Two hazards are two smiles.
  A cache that could not tell them apart would serve the first answer for the
  rest of the session, which is precisely the class of silent staleness this
  rebuild exists to remove.
* **A blend strictly between 0 and 100% is a weighted average of two implied
  volatilities.** It is arbitrage free in neither model's sense, it exists
  because a regime is not switched on overnight, and it warns every time.
* **Percentages at the edge, decimals in the middle.** A hazard is typed as
  `3` and held as `0.03`, converted once in `BandTreatment.from_request` — the
  same rule the pasted broker run and the knowledge bank follow.

One diagnosis changed while this was being wired. `calibrate_band_smile` failed
with a single message naming the break-risk **floor** — the at-the-money the
jump legs alone impose — whichever bound had actually been missed. But the
concentration has two ends: a Beta collapsed to a point gives the floor, and a
Beta sitting entirely on the band's edges gives a **ceiling**, and a quote
above *that* means the band is too narrow to be the whole story. Reporting the
floor for both sent a marker to lower a hazard that was not the problem. Both
bounds are now computed and the message names the one that was missed.

## Using it

Bands live on the workbook's `PEG_BANDS` tab — they are policy, not market
data. They started as `files/bands.csv` and moved into the workbook with the
other settings (see `volkit/configsheets.py`), so a desk copies one file and
gets the marks and the policy that goes with them. They are loaded
automatically (`Book.from_excel` reads the tab of the workbook it was given)
rather than only when `Book.load_bands` was called by hand, so a
pegged pair is flagged on every screen instead of on whichever one remembered
to ask. Only USDHKD ships enabled. USDCNY is deliberately excluded: its ±2% limit is around
a *daily fixing*, so it is a moving band, not a fixed range an option can be
priced against. USDCNH is not subject to it at all.

Sources:
[NBER — How Credible Is Hong Kong's Currency Peg?](https://www.nber.org/system/files/working_papers/w34300/w34300.pdf) ·
[Pricing and Hedging on Pegged FX Markets](https://arxiv.org/pdf/1910.08344) ·
[HKMA Linked Exchange Rate System](https://www.hkma.gov.hk/eng/key-functions/money/linked-exchange-rate-system/)
