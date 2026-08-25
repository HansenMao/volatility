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

## 4c. Analysis — new, and moves nothing

The analysis tab is new. The legacy tool had `rv.py`, whose `RV.calc` wrote
results through chained indexing and so silently produced an empty matrix on
any current pandas; nothing else in it corresponds. No existing mark changes.

Three conventions in it are worth knowing before reading a number off it.

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
  forward the band does not contain — either the peg has moved and `bands.csv`
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

Bands live in `files/bands.csv` — they are policy, not market data. They are
loaded automatically now (`Book.from_excel` finds the file beside the exe or in
`files/`) rather than only when `Book.load_bands` was called by hand, so a
pegged pair is flagged on every screen instead of on whichever one remembered
to ask. Only USDHKD ships enabled. USDCNY is deliberately excluded: its ±2% limit is around
a *daily fixing*, so it is a moving band, not a fixed range an option can be
priced against. USDCNH is not subject to it at all.

Sources:
[NBER — How Credible Is Hong Kong's Currency Peg?](https://www.nber.org/system/files/working_papers/w34300/w34300.pdf) ·
[Pricing and Hedging on Pegged FX Markets](https://arxiv.org/pdf/1910.08344) ·
[HKMA Linked Exchange Rate System](https://www.hkma.gov.hk/eng/key-functions/money/linked-exchange-rate-system/)
