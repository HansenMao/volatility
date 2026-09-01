# volkit §9 — Analysis (`analytics.py`, `moments.py`, `history.py`)

Extracted verbatim from `CLAUDE.md` §9. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

A fourth UI tab. Five sections, each built and reported independently so one
missing input does not empty the others. The first of them, the relative-value
grid, is the summary of the other four and is described last here because it
is built out of them.

- **The delta on the carry table is the smile delta**, not Black-Scholes'.
  The whole table is a fixed strike sliding under a moving forward, so the
  volatility that strike is marked at moves with it: `dV/dF` carries
  `vega * dsigma/dF` along, and a delta that held the volatility still is
  short of the position by the skew. Both are reported -- `smile_delta` leads,
  `delta` is the Black-Scholes reading beside it, `skew_delta` is the
  difference -- for the same reason §8's listed panel reports two sets of
  greeks. On the sample marks a USDJPY 25 delta put runs at **0.168**, not
  0.239, and the at-the-money straddle is delta neutral in the Black-Scholes
  column *only*: it is long vega, the volatility moves with the forward, and
  the skew leaves it several delta. That is also why the at-the-money row
  shows any carry at all.
  - **The reconciliation is the test.** `delta * (F2 - F1)` is `carry_pnl` and
    `smile_delta * (F2 - F1)` is `carry_pnl + vega * roll_smile` -- the whole
    of what the forward move does -- both to within a percent over a one-day
    horizon, while the Black-Scholes delta misreads the whole by 20% to 65%.
  - **Both are `dV/dF` in the term currency, deliberately not the quoted
    convention.** A premium-adjusted delta is a hedge ratio in the *other*
    currency; multiplying it by a move in the forward does not give money, and
    money is what this table reports. `VolSurface.smile_delta` takes a `conv`
    override for this one caller and nothing else uses it -- the pricing
    screen's own smile delta stays in the surface's convention, beside a
    Black-Scholes delta in the same one. So on a premium-adjusted pair the 25
    delta strike does not read 0.25 in this column, and that is correct.
  - The decomposition itself did **not** change. The smile's reaction to the
    forward was always in the P&L, through `vega * roll_smile`; what was wrong
    was only the delta reported beside it. `carry_pnl`, `roll`, `pnl` and
    `total_pnl` are untouched, so nothing in fair value or the relative-value
    grid moves.
- **Carry and roll** revalues at a **fixed absolute strike** and splits the
  result into the term-structure slide and the smile slide, so the forward
  curve's contribution is separable. Without a feed the strike is held in
  moneyness, the smile slide is zero *by construction*, and the row says so
  rather than showing a plausible zero.
- **The forward curve pays twice and the two are reported apart.** Through the
  *mark* it is `roll_smile`, in volatility points. Through the *price* it is
  `carry_pnl` -- the option is worth `V(F, K, sigma, tau)` and `F` itself has
  rolled -- in premium, with `carry_vols` the same number over the position's
  own vega so it can be read beside the roll. The convention is the whole of
  that number: hedged in the **outright forward to the option's own expiry**
  the hedge rolls down exactly as the option does and the two cancel; hedged in
  **spot**, as a desk is, nothing rolls on the hedge side and the position
  keeps it. Full revaluation, not `delta*(F2-F1)`, so the gamma over the move
  is in it. An at-the-money leg is read as **half a straddle**, so its delta is
  zero and its vega is still one option's -- reading it as the call alone gave
  the at-the-money row half a unit of forward carry nobody runs. A risk
  reversal has almost no net vega, so it states its carry in premium and
  declines to divide by that vega. No feed means every carry figure is `None`,
  not zero.
- **Fair value** is
  `fair = realized + (T/h)*[roll*vega(T-h) + carry_hedged]/vega(T)`, derived in
  the docstring. The roll is **always the ATM roll**, built inside the
  function -- an earlier cut took it from whatever target the carry screen was
  showing, which mixed a risk-reversal roll into an ATM break-even. The
  at-the-money is a delta-neutral straddle, so `carry_value` is second order
  there and `forward_value` carries the curve's first-order effect; it is
  computed anyway, because a number reported as an exact zero should have been
  measured.
- **A break-even reads the forward's carry delta hedged; a position reads it
  whole.** `carry_pnl` is the entire revaluation at the rolled forward and is
  the right number for the carry table, where a spot-hedged book keeps all of
  it. It is the wrong one for anything asking what the forward's roll is worth
  to the *mark*, because a break-even volatility is a property of the strike
  and put-call parity puts the whole difference between writing that strike as
  a call and as a put into `delta * (F2 - F1)` -- a direction, not a
  volatility. `CarryRow.carry_hedged` is `carry_pnl` with that term removed,
  which leaves the gamma over the move: **non-negative** whichever side the
  option is written as, by convexity, and a test pins that across every single
  option column. Fair value and the relative-value carry signal both read it.
  Unhedged, the relative-value score changed sign across the strike axis with
  the option's own delta -- a USDJPY one-year 25 delta put at `+13.8` against
  the call's `-0.46` on the sample marks, 30 basis points of forward carry
  being the whole of it -- and the at-the-money column barely showed it,
  because a delta-neutral straddle has almost no first-order term. That is why
  the flip looked like it belonged to the wings.
- **Realized is measured on the forward, not on spot** (`basis="auto"`,
  `history.realized`). A quoted volatility is the volatility of the forward the
  option is struck against. With `F = S exp(c tau)`,
  `dlog F = dlog S + tau*dc - c*dt`: the swap points **moving** is realized
  volatility, the points **decaying** by one day of carry is a known slide and
  is removed and reported as `carry_rate` instead of being booked as risk. The
  spot-only figure and the swap-point part alone stay beside it. A tenor the
  sheet does not quote has its carry *interpolated* between the pillars it
  does -- falling back to spot on the misses put two different measurements in
  one column and grew steps in the term structure at whichever tenors the sheet
  happened to quote.
- **Realized** is annualised on the model's own **volatility time**
  (`history.volatility_time`), which is pinned against `AtmCurve.integrated_vol`
  by a test. Calendar and 252-day annualisations are reported beside it, not
  instead of it. Daily skew/kurtosis are projected onto each tenor before being
  compared with the smile's, and both are shown with standard errors.
- **The RR and the fly are compared as `(rho, nu)`, not as moments.** A quoted
  spread is not a moment and a realized third moment is not a risk reversal;
  what both sides share is the two numbers a SABR smile is built from.
  `sabr.fit_smile_shape` reads the marked ATM, RR and **smile** butterfly as
  the `(rho, nu)` that would show them -- the smile butterfly and not the
  market strangle `sabr.calibrate` matches, because matching a premium
  condition against a moment compares two different things -- and
  `history.vol_dynamics` measures the same two from the sheet: under SABR with
  `beta = 1` the ATM *is* the state variable, so `rho` is corr(spot return,
  dlog ATM) and `nu` is those changes annualised on the same volatility time.
  SABR has no mean reversion, so `nu` rises at short tenors on both sides and
  is never blended across them; `nu*sqrt(t)` is the scale-free number. The fit
  reports its own residual, because a smile SABR cannot reach must say so
  rather than return the nearest thing. Off by default (`--sabr`).
  **The measured half has its own window** (`history.DYNAMICS_DAYS`, never
  shorter than the realized lookback) and every row names it: `rho` and `nu`
  are properties of the process rather than forecasts over a horizon, and they
  need more paired observations than a realized volatility needs returns. Read
  off the lookback, the whole measured half of the card -- and with it both
  `diff` columns, which are the only reason the card exists -- was blank at
  every tenor whenever the lookback was under about a month, and blank at the
  short tenors always. **The marked half needs no history at all**, so it is
  built before the realized statistics rather than after them: a one-week row
  can never hold a week of returns in a seven-day window, and it was losing
  the whole column group to a failure that had nothing to do with it.
- **The cross vega split** differentiates the same variance triangle instead
  of integrating it, so it is exact where the RR and fly are not: one unit of
  at-the-money vega on the cross is `(sigma_a + x*sigma_b)/sigma_c` units in
  the first leg and the mirror of it in the second, with `x = ca*cb*rho`.
  They are **hedges, not shares**, and do not add to one; what is exact is
  Euler's identity, `sigma_a*d_a + sigma_b*d_b == sigma_c`, and a test pins
  it. The correlation is homogeneous of degree zero, appears nowhere in that
  identity, and is reported on its own (`rho_vega`) precisely because no
  amount of leg vega hedges it.
- **The cross triangle** does the ATM exactly (the variance triangle) and the
  RR/fly by combining the legs' Breeden-Litzenberger densities under a Gaussian
  copula on a deterministic tensor grid. `moments.triangle_coefficients` gives
  the signs **one at a time**, which `cross.infer_leg_signs` does not -- it only
  ever needs their product, and getting the product right while getting the
  individual signs wrong leaves the ATM correct and flips the RR. A test pins
  both against each other.
- Every combined distribution is renormalised onto its own forward. The shift
  is compared against the triangle's known convexity (the legs' MGFs at the
  coefficients plus `rho*sd_a*sd_b`); only the unexplained remainder warns.
- **A historical sheet is read in volatility points as written**, one scale
  for the whole sheet, applied to the ATM, the RR and the fly alike.
  Per-column sniffing reads a small risk reversal as a decimal and returns it
  100x too large, so the scale is still decided once; what no longer decides
  it is the *level*, because a low at-the-money is an ordinary quote on a
  managed pair and reading 0.35 as a decimal showed it as 35 on the monitor.
  A decimal sheet is loaded by name, with `vol_unit='decimal'` (`--vol-unit`,
  or the box on the history loader).
- **The relative-value grid** (`relvalue.py`) is the screen's first card and
  its summary: one score per expiry and strike, **in volatility points** and
  positive when the mark is rich. It is **not a new model** -- every signal is one of the comparisons
  above, read at a strike instead of at the at-the-money -- and it is its own
  route (`/api/relvalue`) because it is the most expensive thing on the screen
  and the tables must not wait for it. Owned by the browser and posted whole,
  like every other panel here; `volkit analysis --relative-value` reproduces
  it through the same `panel_from_request`.
  - **Three signals add and two do not.** `level` (marked ATM less realized),
    `shape` (the marked smile's shape at this strike less the shape a SABR
    smile built from the *measured* `(rho, nu)` would show) and `carry`
    (`-(roll_value + carry_value)`, minus because an option that rolls down
    must be cheaper to break even) sum to exactly `implied(K) - fair(K)`, and
    at the at-the-money column to `fair_value_table`'s own `richness` -- a
    test pins it to 1e-12, because two ways of computing one number is how
    they drift apart. `history` (the cell's own z-score) and `triangle` (a
    cross against its legs) answer different questions and are kept out of
    that sum.
  - **The at-the-money carries no shape by statement**, not by two near-equal
    numbers cancelling: the at-the-money *is* the level. And a statement is
    not a measurement, so it is **shown and not scored** (`Signal.scorable`).
    Averaged in, that structural zero pulled every at-the-money cell a fifth
    of the way to the middle -- the very thing the renormalisation rule below
    exists to prevent, arriving through the one signal that was present rather
    than through a missing one. The value stays `0.0` so the additive identity
    is untouched; the at-the-money is simply scored on less of the declared
    weight than the wings beside it, and `confidence` says so.
  - **The comparison smile's `(rho, nu)` are measured over their own window**
    (`history.DYNAMICS_DAYS`), never over the realized lookback, and never
    shorter than it. Same argument as the scale below and a worse failure: a
    spot/volatility correlation and a vol of vol need *more* paired
    observations than a realized volatility needs returns, so on the lookback
    the shape signal was blank at every short tenor and blank at **every**
    tenor at once whenever the lookback was set under about a month -- an
    at-the-money reading `0.000` beside four wings reading nothing, which is a
    signal that looks broken rather than a window that was too short. It is a
    separate constant from `HISTORY_DAYS` on purpose: that one is the
    denominator, and a knob that also moved the numerator would change the
    volatility-point column as a side effect of rescaling the z-scores.
  - **The score is in volatility points, and so is every signal in it.** The
    composite is the weighted mean of the signals' *values*, renormalised
    over the ones a cell has, and it reads as the number of volatility points
    the mark is rich by, and not a z-score: *how unusual is this* is a
    statistic about a series and *how much am I being paid* is what gets
    traded on. So **a cell with no history still scores** -- only `history`
    needs the series -- and the score, the richness under it and the signals
    inside it are one unit, so `level + shape + carry` is exactly the
    richness.
  - **The standardisation is kept beside the value, not removed.** Half a
    volatility point is a great deal on a one-year at-the-money and nothing
    on a one-week wing, and only the history knows which -- so every signal
    still carries its `z` wherever a scale can be measured, and the cell
    carries the `scale` and `scale_source`. It follows the **scale** and not
    the score now: present where the history can measure one, absent where it
    cannot, whether or not the value was counted. **The scale window is not
    the realized lookback**: the lookback is matched to each tenor because a
    one-month implied forecasts one month, but how much a volatility *moves*
    is a slower measurement (`HISTORY_DAYS`, a year). Run off one window it
    measured a one-month mark on a month of a smooth series and read an
    ordinary half point of richness as thirty standard deviations. A wing the
    sheet does not quote borrows the at-the-money's scale and `scale_source`
    says so, because a z is only as good as its denominator.
  - **`EXTREME_SCORE` travels on the response.** Half a volatility point is
    where the summary starts counting a score as an outlier, and the page
    tints to saturation there -- read off `RelativeValue.extreme_score`
    rather than held as a second copy in the page, the same arrangement as
    the signal weights in `/api/state`.
  - **A missing signal is renormalised away, never counted as a zero**, which
    would drag every score toward the middle. Each cell reports `used` and a
    `confidence` -- the share of the declared weight the score rests on -- so
    a cell scored on one signal and a cell scored on four are not read alike.
  - **A triangle difference inside its own noise floor is shown and not
    scored**, which is that section's own rule; the wing mapping is
    `atm + fly + rr/2`, the same arithmetic the marked wing is read with.
  - **The weights are a marking judgement, not a result.** Declared once in
    `relvalue.WEIGHTS`, sent to the page in `/api/state` so a box cannot offer
    a signal the scorer never heard of, and editable on the panel and by
    `--weight NAME=VALUE`. A weight that is not a signal, or is not a number,
    is refused rather than ignored.
  - **The whole realized measurement travels with the row, not one field of
    it.** `history.realized` already measures the spot leg, the forward leg,
    the swap-point volatility and its correlation with spot; the grid kept
    `vol` and threw the rest away, which left a cell scored rich on `level`
    with no way to say whether the richness was genuine forward variance or a
    level comparison against a thin estimate. The number that answers it is
    `forward_vol_ratio` -- realized vol of the forward over realized vol of
    spot -- and **never the level of the swap points**, which says nothing on
    its own about whether the forward is more volatile than spot. Near one,
    the points moved with spot and the level comparison rests on the same
    variance either way.
  - **Every row says which regime it is in before the carry signal is read.**
    `regime_z = |ln(F/S)| / (sigma sqrt(T))` puts the forward's own drift and
    the option's diffusion in the same units; past `CARRY_DOMINANT_Z` the
    position is mostly a carry trade in an option's clothes, and the row, the
    carry signal's own message and the CLI all say so. `carry_horizon_days` is
    the same statement as a maturity, `T = 0.64 sigma^2 / c^2`, and the 0.64
    is **derived** as the threshold squared rather than written down twice.
    The same z against *realized* volatility is the managed-float evidence,
    read at a one-year reference because whether a pair is managed is a
    property of the pair and not of a tenor. What it does **not** do is change
    a weight -- see §7.
  - **The forward comes from the feed before the carry table.** Reading it off
    the carry table alone left every tenor shorter than the horizon with no
    forward, and so no absolute strikes, on a pair the feed was quoting
    perfectly well. They are the same number where both exist:
    `analytics._forward_at` asks the same feed.
  - **A signal that is a property of the tenor is marked as one.** `level` is
    the same number in all five cells of a row by construction, and shown five
    times with nothing tying them together, one at-the-money mispricing reads
    on a heat map as five independent confirmations that a whole tenor is
    rich. `relvalue.SHARED` declares which signals those are, the grid prints
    them once in their own column beside the tenor, and the CLI marks the
    column with a star. The *z* still differs by cell, because each cell
    divides by its own scale.
- **The curve comparison panel** (`curves.py`) lives on the Monitor screen
  (§12), not here: it answers "what has changed", which is that screen's
  question. `volkit monitor --compare` reproduces it; `volkit analysis
  --compare` is kept only to say where it went, because argparse's
  "unrecognised argument" would not. It is owned by the browser and posted
  whole like the listed and market-maker panels. Four sources:
  `surface`, `marks`, `history` (one dated row) and `paste`. A dated request
  snaps **backwards** to the last row on or before it -- a workbook has no
  weekend rows and snapping forward compares Friday against Monday -- and each
  curve reports the day it landed on. A pasted curve's unit is decided once
  from its at-the-money column and refused when the levels straddle 1.0. A
  tenor a source does not quote is blank, not absent; a curve that could not be
  built keeps its place and carries the reason.
- `_finite` in `webapp.py` turns NaN/Infinity into null on every response --
  Python's `json` writes them and `JSON.parse` refuses them.
