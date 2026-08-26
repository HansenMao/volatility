# volkit — project context

Daily FX volatility marking and pricing tool. This file is the standing
context: what the project is, what has been decided, and what must not be
broken. Read it before changing anything.

- `README.md` — install, run, architecture, how to extend
- `USER_MANUAL.md` — for the trader using it
- `MIGRATION.md` — every difference from the legacy tool, the convention audit,
  and the managed-currency work. **The authority on anything that moves marks.**

---

## 1. What this is

A rebuild of a legacy tool (`vol.py`, `cvol.py`, `ssabr.py`, `vols.py`,
`common_functions.py`, `__main__.py`, `rv.py`), which is **still present in the
repo root, untouched, for comparison**. Nothing in `volkit/` imports it.

- ~16,800 lines across 36 modules, 431 tests, `unittest` only (no pytest).
- Runtime deps: numpy, scipy, pandas, openpyxl. Plus `tzdata` on Windows.
- Deliberately no `pysabr`, `xlrd`, `tkcalendar`, and no web framework.
- **Six screens**, each with a command-line equivalent, in the order a desk
  reads them: Pricing, Vol marking, Monitor (`volkit monitor`), Exchange traded
  (`volkit listed`), Analysis (`volkit analysis`), Market maker (`volkit mm`).
  Monitor sits behind Vol marking because those are the two a morning starts
  on. `screens.SCREENS` is the one declaration of that order and a test pins
  the page's nav and panel map against it. One HTML file,
  `volkit/web/index.html`, is the whole front end. A build may be made
  **without** some of them, or with some **hidden** until asked for -- see §14.

## 2. Standing decisions

These were the user's explicit choices. Do not quietly reverse them.

| Decision | Consequence |
|---|---|
| **Correctness over parity** | Fix model bugs rather than reproduce them. But *document every change that moves a number*, and give the exact input change that restores the old figure. |
| **Model risk, don't remove it** | Never make a risk vanish by construction to make a model tidy. If a bad outcome is possible, put it in the model and let it be marked. (This came from a direct correction — see §6.) |
| **Implement fixes, don't just flag them** | When something is wrong, fix it and note what moves. Leave it switchable only when there is a real reason to reconcile against old marks. |
| **Web UI, not Tkinter** | Stdlib `http.server` only. No Flask/FastAPI. The desk machine may have nothing installed. |
| **Excel stays primary** | `vol_marks.xlsx` must keep working as-is. Abstract behind a data source; do not migrate to YAML. |
| **Nothing writes to the workbook** | Every mark a screen makes lives on the loaded book. What a session wants to keep goes into `session.py`'s own file *beside* the workbook, never into it -- see §13. |
| **Nothing fails silently** | The legacy `except: pass` returning `0.0000` is the anti-pattern this project exists to remove. Errors surface with the real message. |

## 3. Architecture

```
timeutil   one day-count (365.2425), one injected Clock, tenor parsing
numerics   bracketed solves, damped fixed points, panel integration
calendars  holiday calendars, spot/expiry rolls, CSV overrides
timeweight intraday / weekend / holiday weighting
black      Black-76, its greeks, FX delta conventions, strike-from-delta
sabr       Hagan 2002 + calibration (closed-form alpha, global sweep)
smile      arbitrage-constrained SVI, vanna-volga, cached slices
banded     pegged pairs: Beta-on-band body + hazard-rate jump leg, and the
           marked treatment deciding how much the surface takes notice of it
events     dated vol bumps, joint height calibration
atm        the ATM term structure
cross      cross pairs from two legs and a correlation
surface    ATM + smile, greeks, delta strikes, RR / fly
exotics    digitals, one-touch / no-touch, overhedge buffers
pricing    multi-leg strips, strike/expiry specs, per-leg error isolation
marketdata validated Excel reader
feed       spot / forward points from file, interpolated
econ       scheduled economic events (rules + dated table)
book       all pairs, built in dependency order
listed     exchange traded options: paste parsing, least-squares SABR fit,
           comparison against the marked FX surface, and a position book with
           aggregated greeks both Black-Scholes and on the fitted smile
moments    risk-neutral distribution from a smile; two combined into a cross
history    historical spot / forwards / quotes; realized vol, skew, kurtosis
analytics  carry and roll, fair value, the cross triangle, indication pricing
curves     several vol curves side by side, and the same curve on other dates
monitor    small panels: what has moved between two points in time, per pair
quotes     a broker run, in English or in columns: outrights, RR, fly, spreads,
           timestamps and which of two quotes for one thing is live
knowledge  the per-pair knowledge bank: widths, floors, shifts, notes
marketmaker  fit the curve to a target, fine tune the wings to a market, quote it
webapp     JSON API + stdlib server;  web/index.html is the whole front end
cli        every screen has a command-line equivalent
screens    which screens a build has, shown or hidden; the one reader of the
           build's manifest, and of --enable-tab
session    the marks a session made, saved beside the workbook and put back
config     the startup settings file a double-clicked exe reads
paths      resource vs user-data paths (source and frozen), and the one
           text encoding every file is read and written in
preflight  startup checks (tzdata above all)
```

## 4. Invariants — breaking these is a regression

- **The clock is injected.** Nothing calls `datetime.utcnow()` inside the model.
  One `Clock` per book; same clock ⇒ identical numbers.
- **One year length: 365.2425.** The legacy had six.
- **Daily variances sum to the term variance** (holds to 2e-16). Any change to
  the day grid must preserve this.
- **Integration splits at known breakpoints** (hourly edges, event times), then
  fixed Gauss-Legendre. Order 5 ⇒ order 20 changes nothing. Never reach for
  adaptive `quad` on this integrand.
- **Every solve is bracketed** and raises `ConvergenceError` with a diagnosis.
  No bare `fsolve`, no fixed-iteration loops.
- **Smile slices are cached** per (expiry, method, cut, forward). The legacy
  re-ran a 12-parameter optimisation per strike query.
- **Use `scipy.special` ufuncs** (`ndtr`, `ndtri`, `log_ndtr`), not
  `scipy.stats.norm`, in inner loops. That alone was a 13x calibration speedup.
- **The smile chart's strike axis is a scale, not a model change.** When the
  feed covers the pair, `/api/smile` carries `spot` / `forward` from the one
  lookup the band model uses (`Book.market_level`) and the page multiplies the
  axis, the point table and the density by it; without a feed it stays in K/F
  and says so. The slice itself is always built in moneyness. `volkit smile`
  prints the same two ways, off the same call.
- **The server holds no screen state.** The browser owns the pricing legs, the
  listed panels and the analysis query, and posts each one whole. That is what
  makes `volkit listed` and `volkit analysis` reproduce a screen exactly, and
  it is why every endpoint is a pure function of its request plus the book.
- **No response may carry a non-finite float.** Python's `json` writes `NaN`
  and `Infinity`; `JSON.parse` refuses both, so one unavailable cell would
  take a whole response down in the browser. `webapp._finite` maps them to
  `null` on the way out.
- **Realized and implied share one clock.** Anything compared against a quoted
  volatility must be measured in the model's own volatility time
  (`history.volatility_time`), not calendar days and not a flat 252. A test
  pins it against `AtmCurve.integrated_vol`.
- **Units and signs are decided once per source, never per row.** The
  volatility unit of a historical sheet comes from its ATM column; the unit of
  a pasted listed table comes from the whole table and is *refused* when
  ambiguous. Per-column sniffing gets small risk reversals wrong.
- **A row that cannot be computed keeps its place and carries its reason.**
  Dropping it makes a short table look complete, and makes an all-failed table
  look empty. `carry_table`, `realized_table` and `triangle_table` all emit a
  blank row with the message instead.
- **A shortcut through the model is measured, not assumed.** The market-maker
  fit may read a delta off a SABR wing instead of solving the interpolation,
  but only for expiries where `marketmaker.anchor_gap` has *measured* the two
  to agree, and it re-checks at the answer. They do not always agree: the
  arbitrage-constrained SVI cannot pass through anchors that imply arbitrage,
  and USDCNY misses its own by 0.15 vol points. See §11.
- **A data file is read and closed, never held open.** Every workbook goes
  through `marketdata.open_workbook`, which copies the bytes and hands pandas
  a buffer; the feed CSV is read whole in a `with`. `pd.ExcelFile(path)` keeps
  the file open for as long as the reader lives, and openpyxl's workbook is
  full of parent/child cycles, so the handle outlived the call that made it --
  and on Windows that is enough to stop Excel saving the very sheet the tool
  had just read. These are somebody else's files and the tool only reads them.
- **One place reads a timestamp.** `timeutil.parse_datetime` tries the tabular
  formats first, unchanged, then ISO 8601 -- which is what the tool itself
  writes, in `/api/state`, in a session file, and out of a browser's
  `datetime-local` field. Three callers had each patched that in for
  themselves and had done it differently; the one that handled an offset
  *dropped* it and stamped the result UTC, reading `19:00+09:00` as 19:00Z.
  An offset is converted here, never discarded. Do not re-add a local
  `.replace("T", " ")`.
- **A screen shows a field only where the model reads it.** The pricing grid's
  rows carry the products they belong to: no barrier on a vanilla, no strike
  or ramp on a touch, no overhedge outside one. A box that can be filled in
  and is then ignored is a silent zero with a cursor in it. A row another leg
  needs keeps its place and shows a dot, so two legs never look like the same
  instrument, and the columns are a fixed width so a long premium cannot
  widen the leg beside it.
- **A reload does not move the screen.** `boot()` runs again after every
  workbook reload, so every select is rebuilt with `fillSel`, which keeps what
  was chosen; the marking screen's pair, cut, interpolation and chart expiry
  survive. The marks are discarded -- that is the point of a reload -- but
  putting a marker on a different pair is a change nobody asked for.
- **Every text file is UTF-8, said once, in `paths`.** `read_text`,
  `open_text` and `write_text` are the only spellings; nothing calls
  `Path.read_text()` or `open()` on text and takes Python's default, which is
  the *locale* encoding -- cp1252 on the desk machine. Reading
  `volkit/web/index.html` with that default is what stopped the Windows build,
  at the test suite, with `'charmap' codec can't decode byte 0x81`, and the
  same default sat under `volkit.cfg`, the holiday overrides, `bands.csv` and
  the published feed. Reading strips a byte order mark (`utf-8-sig`, because
  Notepad and Excel both write one and it is not part of the first key or the
  first pair name); writing never adds one. The standard streams are
  reconfigured to UTF-8 at the top of `cli.main` for the same reason: a
  redirected monitor table carries an arrow, and cp1252 has no room for it.
  A test walks the source for the default spellings, so this cannot come back
  one call at a time.
- **Volatility points at the edges, decimals in the middle.** Everything a
  human types or reads -- a pasted quote, a knowledge-bank width, a curve
  parameter on screen -- is in volatility points; everything inside a model is
  decimals. Each boundary converts exactly once. A bank width read as a
  decimal turned a 0.28 market into a 28-point one.

## 5. Things that moved marks vs the legacy tool

All documented in `MIGRATION.md`. In rough order of impact:

1. **Cross triangle sign.** `AUDJPY = AUDUSD × USDJPY` needs `+2ρ`, not `−2ρ`.
   Changes AUDJPY, EURJPY, EURCNH, GBPCNH. Negating those four correlation
   cells reproduces the old numbers exactly (verified to 1e-12).
   `Book(legacy_cross_sign=True)` A/Bs it.
2. **Event windows.** A bump is now the 24 hours *after* the release, not the
   NY-cut day containing it. The old reading gave a 12x height and +35 vol
   points of next-day contamination for an event a minute before the roll.
3. **DST-aware cuts and weekly close.** NY cut is 15:00Z in winter, 14:00Z in
   summer. Weekly close follows NY 17:00. `dst_aware_cuts=False` restores.
4. **SVI.** One arbitrage-constrained slice (5 params, 5 points) replaces three
   summed slices (12 params, 5 points, unconstrained).
5. **Joint event calibration**, modified-following expiry rolls, UK holiday
   observation rule.

## 6. Managed / pegged currencies — read this before touching `banded.py`

The user corrected an earlier design that forced out-of-band probability to
zero: *"you can't force out of band probability to 0. The probability is real I
just need a possible adjustment to better model the jump risk."*

So the model is a **regime mixture**, not a bounded distribution:

- Peg-intact body: Beta on the band (U-shaped when a,b < 1, which matches the
  realised edge-seeking distribution).
- Break leg: a **hazard rate** λ, two-sided and asymmetric, with marked jump
  sizes and post-break volatilities.
- Breach probability is a calibrated **output**, and positive.
- Break risk is a **marked input**, never inferred from a butterfly — a wider
  body and a higher hazard both raise the ATM, so a joint fit is degenerate.
  `solve_hazard=True` inverts it deliberately and reports the sensitivity.

Useful finding: the band alone gives a *negative* USDHKD risk reversal against
a quoted positive one. Most of the quoted skew is peg-break premium.

How much notice the surface takes of a band is `banded.BandTreatment`, marked
per pair and living on the surface beside `param_shifts`:

- `mode` — `warn` (default; lognormal prices, out-of-band strikes flagged),
  `off` (a deliberate marking that the range is not defended), `mixture`.
- the jump spec, an override of the band edges, and a `blend` against the
  lognormal smile.
- **The treatment is part of the smile cache key.** Two hazards are two smiles;
  a cache that could not tell them apart would serve the first answer for the
  rest of the session.
- **A blend strictly between 0 and 1 is a weighted average of two implied
  volatilities.** It is a marking convenience, is arbitrage free in neither
  model's sense, and warns.
- Bands load automatically (`Book.from_excel` → `files/bands.csv`), so a
  pegged pair is flagged on every screen rather than on whichever one
  remembered to call `load_bands`.
- **`BAND` is only offered where it can work.** The page filters the
  interpolation list by `STATE.bands.pairs`: a named pair answers for itself,
  a screen spanning several (the monitor's tiles, a listed panel taking its
  pair from the contract) falls back to whether the book has a pegged pair at
  all. A leg or a panel restored from `localStorage` has its method put back
  to a legal one, because the browser owns that state and posts it whole -- a
  select that cannot show `BAND` while the state still says `BAND` is a
  setting the screen never showed and the server still receives. The server's
  refusal stays exactly where it is; this is not it moving.
- **The band warning reads the level the payout depends on**: the barrier for
  a touch, the strike for everything else. It read `leg.barrier` for every
  product, so a vanilla struck outside a band said nothing and a barrier left
  on a leg that was no longer a touch was checked instead of the strike.
- `VolSurface.band_for_slice` is the one place ratio space and absolute price
  space meet: the mixture is scale invariant, so the edges and the forward are
  divided by the same outright. No feed, or a forward the band does not
  contain, is a refusal with the reason.
- A delta strike on a band smile does not always come out of the fixed point:
  `v -> vol(K(v))` contracts only while the smile is gentle, and a band's
  wings fall away where the peg's support runs out. `SmileSlice
  ._delta_strike_bracketed` is the fallback -- delta is still monotone in
  strike -- and it runs *only* after the fixed point has failed, so no
  existing number moves. A delta the smile genuinely never reaches (hazard
  marked at zero, so compact support) says that, rather than "did not
  converge".

## 7. Known limitations (flagged, not fixed)

- **Same-day expiries cannot be priced.** `cut_vol` normalises by whole
  volatility days; today's expiry has zero, so vol is zero and Black rejects it.
- **No discount curve.** All premiums are undiscounted forward values.
- **Half-day holidays** (Christmas Eve, day after Thanksgiving) are full days.
- **The band model needs a forward feed.** It is now a UI interpolation method
  (`BAND`, §6), but a band is absolute and the surface works in strike/forward
  ratio, so placing one needs the outright forward at the expiry. Without a
  feed for the pair it refuses and names the feed rather than guessing a level.
- **Central bank dates in `econ_events.csv` are provisional** and BoJ 2026 is
  partly filled.
- **The cross triangle for RR and fly assumes a Gaussian copula** between the
  two legs and ignores the change of measure between their domestic
  currencies. Both are stated in `moments.py` and bounded by the reported
  noise floor; neither is corrected for.
- **Fair value is a first-order break-even**, not a valuation: it ignores the
  convexity of the gamma P&L in realized volatility and assumes the surface
  does not move.
- **Realized moments are projected onto a tenor assuming independence**
  (`skew/sqrt(n)`, `kurtosis/n`). Real returns are not independent; the raw
  daily figures are reported beside the projected ones for that reason.
- **Listed-option comparisons are not adjusted** for American exercise on
  exchange settlement volatilities, for futures-vs-forward convexity, or for
  the exchange settlement time not being an FX cut. All three are reported in
  the panel and in the docs rather than silently absorbed.
- **The RR-sign question from `test.py`** (legacy comment says rho = −0.383 for
  a positive RR) was never settled; `pysabr` is not installed. volkit's own
  convention is verified by round-trip.

## 8. Exchange traded options (`listed.py`)

Added as a third UI tab. Panels are owned by the browser and posted whole, so
the server keeps no panel state and `volkit listed` reproduces a screen exactly.

- The fit is a **least squares over N strikes**, not the three-quote solve in
  `sabr.py`. Alpha is profiled out at each `(rho, nu)` by a bounded scalar
  search inside a bracket from `alpha_roots_at_forward`, keeping the outer
  problem two-dimensional so the whole box can be swept before polishing. Same
  no-starting-guess discipline as `sabr.calibrate`.
- **Inversion is the trap.** `6J` is USD per JPY. Strikes reciprocate onto
  USDJPY and the wings swap sides; lognormal vol itself is invariant. All
  comparison is done at **matched physical strikes** — never by matching
  deltas or negating a risk reversal, which is how §5 item 1 happened. The
  reported RRs are the book's own delta strikes read off both curves.
- **Any of `alpha`, `rho` and `nu` may be given rather than fitted**, and a
  given one is held everywhere: the sweep visits no other value of it and the
  polish does not carry it as a variable, so what comes back is the best curve
  through the quotes *at* that value. It is substituted from the pin and not
  read back out of the optimiser's vector -- alpha travels through a logarithm
  there and `exp(log(a))` is not always `a`, and a number somebody typed must
  come back as the number they typed. The count rules follow the **free**
  parameters, not SABR: two held parameters leave two quotes sufficient, and
  "an exact interpolation" is `n == len(free)`. Hold all three and nothing is
  fitted; that says so rather than reporting a convergence it never attempted.
  Every held parameter is marked where it is shown, on the screen and in the
  CLI, because one that was typed is otherwise indistinguishable from one the
  market implied.
- The parser reports every inference and every rejected line. A table
  straddling 1.0 is refused, not guessed.
- The arbitrage check runs in **moneyness, per unit of forward**. In raw
  strike units the second difference of a yen future's prices is small enough
  that rounding reads as arbitrage; the test pins this at four forwards.

### Positions and aggregated risk

A second panel on the same tab: what is owned, against the panels above. A
position line is `contract, expiry, strike, C/P, contracts` (or the short
layouts, or a header row), and `PositionPanel.run` prices each one against the
fit panel it names. Posted whole like everything else here, so
`volkit listed --positions` reproduces it.

- **A position names a panel, and everything a greek needs is that panel's** --
  the forward, the expiry, the volatility at the strike. The one parameter a
  fit never needed is the **contract size**, which is why it was added as a
  field on the *fit* panel (defaulting to the contract's standard: 125,000 for
  `6E`, 12,500,000 for `6J`) rather than invented in the positions panel.
- **Two sets of greeks, and the premium is the same in both.** Black-Scholes
  is the closed-form Black-76 sensitivity at the option's own volatility with
  that volatility *held fixed* as the future moves -- what a Black-Scholes
  greek is, and what an exchange's own risk file will agree with. Smile is the
  same position revalued on the fitted SABR curve, with the forward bumped
  *inside* the parameters so the curve travels with the future. Both read one
  volatility at one strike, so the entire difference is in the sensitivities.
  `black.theta`, `black.vanna` and `black.volga` were added for the first
  column and each is pinned against a finite difference of `black.price`.
- **Vega is a lift of the whole curve, measured at the forward.** Alpha is
  scaled, the resulting at-the-money move is *measured*, and the revaluation
  is divided by it -- no solve, and the number is per one point of
  at-the-money volatility however alpha happens to map onto it. At `K = F` it
  is therefore exactly the Black-Scholes vega, and a test pins that.
- **Money adds across contracts; a futures equivalent does not.** Every CME FX
  option settles in US dollars, so premium, vega, theta, the 1% delta and
  gamma money and volga are totalled. A euro future is not a yen future, so
  futures-equivalent delta and gamma are totalled *per contract only* and the
  grand-total row says a dash there rather than printing a sum of unlike
  things. `ADDITIVE_GREEKS` is the one declaration of which is which.
- **A line that matches no panel, or two, keeps its place and says which.** A
  position priced against the wrong month's curve looks perfectly ordinary,
  which is the one thing that may never be guessed at. A panel that will not
  fit reports its own message on the lines that name it and takes none of the
  others down.
- **A comma is a column boundary**, as in a broker run (§11), so
  `_detect_position_delimiter` believes any comma at all -- unlike the quote
  table's rule, which needs every row to agree because `1,425.00` is a strike.
  A size written `1,000` in a comma paste is then two columns and its row is
  refused with the reason. The **layout is decided once from the whole table**
  (the modal field count); a row of a different width is skipped rather than
  read on its own width, which would move a quantity into a strike the first
  time somebody left a cell blank.
- **A theta window past the expiry blanks the smile theta with the reason.**
  There is no revaluation to take the decay from, and the closed-form column
  beside it is still reported.

## 9. Analysis (`analytics.py`, `moments.py`, `history.py`)

A fourth UI tab. Four sections, each built and reported independently so one
missing input does not empty the others.

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
  `fair = realized + (T/h)*[roll*vega(T-h) + carry_pnl]/vega(T)`, derived in
  the docstring. The roll is **always the ATM roll**, built inside the
  function -- an earlier cut took it from whatever target the carry screen was
  showing, which mixed a risk-reversal roll into an ATM break-even. The
  at-the-money is a delta-neutral straddle, so `carry_value` is second order
  there and `forward_value` carries the curve's first-order effect; it is
  computed anyway, because a number reported as an exact zero should have been
  measured.
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
- **The volatility unit of a historical sheet is decided once, from the ATM
  column**, and applied to the RR and fly. Per-column sniffing reads a small
  risk reversal as a decimal and returns it 100x too large.
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

## 10. Working on this

```
python -m unittest discover -s tests        # 431 tests, ~4.3m
PYTHONUTF8=0 LC_ALL=C python -m unittest discover -s tests   # as a cp1252 Windows
                                           # box sees it: an ASCII locale is the
                                           # only way to catch an encoding bug
                                           # from a Mac before CI does
pip install esprima                         # enables the front-end JS syntax test
python -m volkit check                      # validate the workbook
python -m volkit serve --feed files/market_feed.csv --history vol_history.xlsx
python -m volkit serve --auto-reload 30     # re-read the market feed when it changes
                                           # (the pricing tab has the same switch)
python -m volkit analysis EURJPY --history files/history_sample.xlsx --horizon 7
python -m volkit analysis USDJPY --history files/history_sample.xlsx --sabr \
    --realized-basis forward          # wings as (rho, nu), realized on the forward
python -m volkit mm EURUSD --target-source quotes --fallback-spread 0.3 < run.txt
python -m volkit mm EURUSD --learn < run.txt          # propose widths, --save writes them
python3 files/make_history_sample.py        # regenerate the example history
python3 build_exe.py --host-check           # validate the packaging (Windows exe: on Windows)
python3 build_exe.py --only-tabs pricing,marking   # a build without the other three
./build_windows_github.sh                   # drive the Windows build on CI, fetch the exe
./build_windows_github.sh --explain         # print a failed run's own log
python3 build_exe.py --hidden-tab mm        # built, off until --enable-tab mm
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 --rho -0.2
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --positions book.txt        # aggregated greeks, BS and smile
python -m volkit band USDHKD --feed files/market_feed.csv --hazard 3
python -m volkit monitor EURUSD --history files/history_sample.xlsx \
    --watch EURUSD --watch USDJPY:history@-1m \
    --compare surface --compare history:-30d --field rr25
python -m volkit session marks.json                    # save every mark on the book
python -m volkit session marks.json --show             # what a file holds
python -m volkit --session marks.json vol USDJPY 2024-05-28   # price against them
```

- **There is no browser tooling in this environment.** Layout must be confirmed
  by the user. What *is* checked, as tests in `TestWebAssets`:
  - the JS parses under `esprima` (it tops out at ES2017, so `??` and `?.` are
    downlevelled before parsing — do not add newer syntax);
  - every `$('#id')` resolves to an id in the markup;
  - every class the script looks up with `querySelector('.x')` is one it also
    emits — the panel shell and the painter that fills it are separate
    functions, and nothing else would catch a rename between them;
  - every field the listed panel sends is one `panel_from_request` reads;
  - every field the market-maker panel sends is one `marketmaker
    .panel_from_request` reads, and the same for the curve-comparison panel
    (`curves.panel_from_request`) and the band card
    (`banded.BandTreatment.from_request`);
  - the markup balances and the five panel roots are **siblings**. A missing
    `</div>` once nested one panel inside another, which browsers repair
    silently while the tab renders nothing.
- **`volkit/__init__.py` binds its public names lazily** (PEP 562). Not a
  startup optimisation: `build_exe.py` reads `volkit.screens` to decide what
  to build, and it does that *before* its own dependency-install step -- it is
  what installs numpy. An eager `from .atm import ...` there dragged the
  numeric stack in behind `from volkit import screens` and killed the Windows
  build at its first line. Nothing in `screens`, `paths` or `config` may import
  numpy, scipy or pandas, directly or otherwise; a test pins it.
- **PyInstaller cannot cross-compile.** A Windows exe must be built on Windows
  or by the GitHub Actions workflow. `build_exe.py` is the single build entry
  point -- preflight, deps, the full test suite, `volkit.spec`, staging the
  user's data beside the exe, then a smoke test of the executable it just
  built. `build_windows.bat` and the workflow are both thin wrappers around
  it, which is what keeps a desk build and a CI build identical. Off Windows
  it refuses instead of producing something unusable; `--host-check` builds
  the same spec for the host, which is how the spec is validated from here.
  Bundled vs staged is the thing to get right: the page and the calendar go
  inside (`paths.resource_dir()`), the workbook, feed and overrides go beside
  the exe (`paths.app_dir()`), and synthetic samples go in `samples/` so
  `find_data_file` cannot pick them up.
- Prefer adding a test that pins the *behaviour that was wrong*, with a comment
  naming the old bug. Most of the suite is written that way.
- A new screen is four pieces, in this order: the model in its own module, a
  `BookService` method plus a route, a CLI subcommand that calls the *same*
  function, and the panel in `index.html`. Doing the CLI from the same entry
  point is what keeps the two honest.
- Sample data files live in `files/` with the script that generates them
  beside them, seeded so they regenerate identically. Synthetic samples are
  never loaded by default — made-up numbers appearing on a screen nobody asked
  for is the same failure as a silent zero.

## 11. Market making (`quotes.py`, `knowledge.py`, `marketmaker.py`)

A fifth UI tab. The other screens answer "what is this worth"; this one answers
"what do I show", which has three stages, kept apart because they fail for
different reasons and the screen has to say which one broke.

- **The curve.** `fit_atm_curve` puts the backbone through a target term
  structure -- the tenors pinned on the marking screen, a pasted curve, or the
  mid of the at-the-money quotes. It is a **cold** fit: the level parameters
  are read off the targets, the shape parameters are swept, and the sweep may
  only move a parameter the caller left *free* (sweeping a pinned one and then
  keeping the best node's value was a real bug -- it silently un-pinned
  `short_decay`). It runs on a `deepcopy`, so a fit the user does not keep
  cannot leave a half-marked curve behind. For a **cross** the level belongs to
  the legs, so what is fitted is the correlation term structure instead;
  `_Knobs` hides which kind of curve it is from everything above it.
- **The wings.** `tune_smile_shifts` moves the four smile parameters by an
  additive `VolSurface.param_shifts`, and is deliberately *not* a cold fit: it
  starts from the marked surface because that is the thing being adjusted. A
  **shift** rather than an overwrite, because a broker run should move the
  level of a wing and not flatten its term structure. **Curve-wide** rather
  than per tenor, because a handful of quotes does not determine a shape -- a
  shift that cannot reach a tenor says so in its residual instead of bending
  the surface to one quote.
- **The quote.** Width from the knowledge bank, mid shaded by fair-value
  richness and by the vega already on the book, both capped as a fraction of
  the width.

Things that are decided once and must not be re-derived per row:

- **The objective is a hinge**, not a least squares: zero anywhere inside the
  quoted bid and offer, distance to the nearer side outside it. That is the
  brief -- our mid inside the market, not on top of somebody's mid -- and it is
  what lets a dozen quotes be satisfied at once when a fit through their mids
  would satisfy none.
- **The tie-breakers are scaled to the market.** A hinge has a flat bottom, so
  a small pull toward the quoted mids picks one answer out of the many that
  work. The pull toward the marked shifts has to be multiplied by the market's
  own half width first: a shift is O(0.1) and a hinge is O(0.001), so a raw
  weight of 0.02 is not a tie-breaker, it is twenty times the violation it is
  meant to defer to. Unscaled, the fit stopped short of a market it could reach
  and reported that it had converged.
- **Neither lean applies to a risk reversal or a butterfly.** A break-even
  against realized volatility and a pasted vega position are statements about
  the *level*. Those rows carry the bank's own shift and say why there is
  nothing else.
- **Both leans point the same way.** Rich market, long position: both are
  reasons to want to sell, and you attract a seller's trade by shading the
  price *down*. Capped at a multiple of the half width so an axe can lean a
  price inside the market but never walk it out of one.
- **The bank invents nothing.** There is no built-in default width. A quote no
  rule matches gets no bid and no offer and says so; a visible panel fallback
  is the only alternative, and the row reports which it was. `suggest_rules`
  proposes a ladder measured from a pasted market, with the evidence attached,
  and proposing and saving are two steps.
- **A `note` rule is prose, is shown, and is never applied.** A note that reads
  like an instruction the tool silently ignores is a silent zero with better
  grammar.
- **Nothing touches the workbook.** `Panel.run` reports and then restores the
  book exactly; `apply` leaves the marks on the loaded book, in memory only,
  and says so.

The paste (`quotes.py`) follows §8's discipline: the volatility unit is decided
once from the whole run's level quotes and refused when they straddle 1.0; a
risk reversal's direction word is resolved against the pair, and one without a
direction word is read in the book's convention and reported once; an
unqualified `fly` inherits the panel's convention and records that it did; a
truncated offer (`8.2/6`) is refused rather than repaired.

The same parser reads a run written as **columns** -- `[time,] expiry, strike,
bid/offer` -- because a run mixes the two shapes and must not depend on which
line came first.

- **A comma is a column boundary and a price never straddles one.** That is
  the whole difference between `3M, 7.75, 8.30` (a choice at the 7.75 strike)
  and `3M 7.75 8.30` (the two-way at-the-money it has always been); with the
  commas thrown away, as `_norm` used to, they are the same line. Thousands
  separators are stripped first, so `1,000mm` is a size and not a column.
- **The strike column is the pricing screen's own vocabulary**: `ATM`, an
  absolute strike, or a delta. A bare `25d` names two strikes, one per wing, so
  it takes the call -- what `pricing.parse_strike` does with a bare `25d` --
  and says so on the row. An *absolute* strike needs no side at all: the
  volatility there is one number, `Evaluator.leg_value` never reads `is_call`
  for it, and `describe` must not call it a put.
- **A later timestamp wins.** A run is a conversation and the same tenor is
  requoted as it moves; left alone the older quote goes into the fit beside the
  newer and pulls it back. When two quotes cannot be compared on time the later
  *line* wins, which is the only ordering an untimed line carries.
  `_conflict_key` is what "the same thing" means -- a market strangle and a
  smile butterfly at one delta are deliberately two instruments.
- **A superseded quote is kept, not dropped** (`ParsedRun.superseded`), and it
  is still **width evidence**: one tenor quoted twice is one live price and two
  observations of how wide it is shown, so `learn_from_panel` reads
  `all_quotes` and the fit reads `quotes`. A line read, understood and then
  silently discarded is a silent zero with better manners.
- A **column header** is recognised (no digits, two or more header words) and
  reported as passed over rather than as a line that could not be read: a
  spreadsheet paste brings one, and it is not an error.
- A date alone is an expiry; a date **followed by a time** is a timestamp.
  Reading one as the other moves a quote to a tenor nobody asked for. A
  time-only line takes the last date above it; a run with no date anywhere is
  ordered on a nominal day, says so, and never shows that day back.

---

## 12. Monitor (`monitor.py`, `curves.py`)

A sixth UI tab, and the one a desk opens first: *what has moved*. A **tile** is
one pair and two points in time, showing all five quoted numbers and the change
between them, tenor by tenor. Either end is any source `curves.py` builds
except a paste, so a tile is "the surface against last week's close", "the
surface against the quotes it was fitted to", or two dated rows against each
other.

- **Nothing here builds a curve.** `curves.build_curve` is the one dispatch,
  shared with the comparison panel; a second copy would be a second place for
  a source to be added to only one screen.
- **A paste cannot be a tile end.** A tile is rebuilt on every refresh and a
  paste cannot be rebuilt, so it is refused rather than silently frozen.
- **A broken end does not empty the tile**: the levels that could be read stay,
  and the tile carries the reason it has no change. A tenor one end does not
  quote is a blank change, not a missing row.
- **Two dated ends that land on the same row say so.** A column of zeros
  otherwise reads as a quiet market rather than as a comparison that never
  happened. Only checked for two *dated* sources -- the surface and the
  workbook quotes are both stamped with the valuation time and comparing them
  is a perfectly good thing to do.
- **The curve comparison panel moved here** from Analysis, and its command
  moved with it (`volkit monitor --compare`). `/api/history` moved the other
  way, out of the Analysis screen's routes and into the shared set: two screens
  read the historical workbook now, so loading it is a shell job like
  `/api/reload`.

## 13. Saving a session (`session.py`)

The workbook is the book of record and is **never written to**. Everything the
marking and market-maker screens do lives on the loaded book, and a reload
discards it -- the right default for a tool whose primary file is somebody
else's spreadsheet, and the wrong thing to do to a morning's work at 5pm. So a
session is saved *beside* the workbook, in the tool's own JSON file, the way
the knowledge bank is.

- **The file holds what the screen shows.** Volatility numbers in volatility
  points, shape parameters and smile parameters raw. `session.curve_params` /
  `set_curve_params` are the one conversion **and the marking screen's own**
  (`webapp.curve` calls them), so the file cannot drift out of step with the
  panel that wrote it. §4's edge rule, with the boundary named.
- **Loading replaces, and says what it replaced.** Overwrites and events are
  cleared before the saved ones go on: merging would double every release that
  appears in both the workbook and the file. A pair the workbook does not
  build is reported, not skipped; a pair the file never mentions is left alone
  and that is reported too.
- **A pair that will not take its marks does not take the rest down with it.**
  Each is applied in its own guard and every failure carries the pair's name.
- **Pairs are applied in `book.build_order()`**, so a cross recalibrates
  against legs that already have their saved marks.
- The routes (`/api/session`, `/api/session/save`, `/api/session/load`) and the
  `session` subcommand belong to **no screen**: marking and market making both
  write this file, and a route belongs to exactly one screen or to none.
- `--session PATH` is a global option applied in `cli._book`, so every
  subcommand prices against the same marks the screen would show, and
  `volkit serve --session PATH` starts with them on.

## 14. Building without some of the screens (`screens.py`)

`build_exe.py --exclude-tab` / `--only-tabs` chooses which of them a build
contains. The names are written into the bundle as `volkit/data/screens.txt`,
and `screens.py` is the **only** thing that reads it; everything else asks
there. No manifest means every screen, which is what running from source and a
plain `pyinstaller volkit.spec` both give. `VOLKIT_SCREENS` selects a subset
where there is no manifest, which is how the excluded case is tested; a
manifest beats it, because the manifest is the build's own decision and an
environment variable must not quietly put back a screen somebody left out.

A screen has **three** states, not two. `--hidden-tab` builds one and leaves it
off until the exe is started with `--enable-tab NAME` (or an `enable-tab` line
in `volkit.cfg`); the manifest writes it as `name hidden`. Off, it is turned
away by the same route and subcommand checks as an excluded screen and says the
*other* sentence — how to switch it on, which is the whole difference. Asking
for a screen the build does not contain is an error rather than a no-op, a
build may not hide every screen, and the smoke test checks both halves of a
hidden one: off by default, and really on with the switch. `screens.activate`
is read off argv before the parser is built, because the flag changes which
subcommands the parser has; `enabled.cache_clear()` drops the manifest cache
with it, since a stale half is worse than no cache.

- **An excluded screen is gone three ways**: no tab and no boot work (the page
  keys off `screens` in `/api/state`), routes refused **by name** with a 404
  that also says what the build does have, and subcommands not registered --
  with `cli._excluded_request` answering *"the Market maker screen was excluded
  from this build"* rather than argparse's *invalid choice*, which in a trimmed
  build is a lie.
- **Ownership is declared once**, in `SCREENS`. A route or a subcommand belongs
  to exactly one screen (claimed twice ⇒ an assertion at import); anything
  shared -- `/api/state`, `/api/reload`, `check`, `serve` -- belongs to none and
  always works.
- **No code is removed.** numpy and scipy are the size of a build, not
  `analytics.py`, and an import that vanished would turn a wrong build into a
  stack trace instead of a sentence. It is also **not a permission system**:
  anyone who can run the exe can run a build that has the tab.
- **The build's own steps follow the selection.** The smoke test runs what the
  build has -- `tenors` belongs to marking -- and checks that each excluded
  subcommand really fails. The test suite always runs with every screen: a
  `VOLKIT_SCREENS` left in the shell would otherwise turn a trimmed run into a
  green build.
- **The manifest is written under `build/`**, never into `volkit/data/`: a build
  must not leave the source tree quietly missing a screen.

## 15. Auto-loading the market feed (`--auto-reload`, and the pricing checkbox)

**Only the feed is watched.** Three files are read and they have three
different lives, and only one of them is worth chasing:

- The **workbook** is the book of record, and this session's marks are *not*
  in it -- nothing writes to the workbook (§2, §13). Re-reading it is exactly
  what throws a morning's marking away, so it stays on `Reload workbook`,
  where somebody has to mean it.
- The **historical sheet** is a record of what happened, not a market. It does
  not move during a session in any way a screen needs to chase.
- The **feed** is a publication. It is republished all morning and a price
  quoted off a stale spot is simply wrong.

Off unless asked for, because a number that reloads underneath somebody
reading it is its own kind of silent change. Two ways on, one setting:
`serve --auto-reload [SECONDS]` (or `auto-reload = 30` in `volkit.cfg`) at
startup, and the **auto-load** checkbox on the pricing toolbar
(`POST /api/auto`, `BookService.set_auto`) at any time. The switch is the
server's, not a browser's: one watcher, one interval, whatever is open.

- **A changed feed is read once its write time has stopped moving**, the same
  stamp on two passes, rather than after so many seconds of quiet. A feed is
  written in pieces and half a feed is not a market; and a file stamped by
  another machine can be seconds *ahead* of this one's clock, which a
  wall-clock settle would hold back for as long as the two disagreed. It costs
  one tick. `auto_check(settle=False)` is the by-hand check -- the *Check the
  feed now* button -- because somebody who pressed it knows they have saved.
- **A feed is read into a local before it goes on the book**, so a half
  written one does not leave the screen with no market at all.
- **The same message about the same file is said once.** `_auto_record`
  suppresses the repeat -- but never the retry, and a failed read does not
  advance the remembered write time, so a file caught half written is tried
  again.
- **No feed file means the switch says so.** `auto_state().available` is what
  greys it out; a checkbox that can be turned on and then quietly does nothing
  is the same failure as a box that is filled in and ignored (§4).
- **The page polls one integer**, `auto.seq`, and rebuilds only when something
  actually happened. It moves per event, so a watcher that did nothing cannot
  make the screen flicker. `/api/auto` belongs to **no** screen: the feed is
  read by several of them, and the switch sits on the pricing tab only because
  that is where a stale spot does damage.

## 16. Starting a build nobody types at (`config.py`)

A double-clicked exe gets no command line, so `volkit.cfg` beside it is one:
`key = value` becomes `--key value`, `command =` is the subcommand, a boolean
becomes a bare flag or nothing, keys may repeat, `#` comments.

- **Read only when nothing was typed.** Anything on the command line means the
  file stays shut; a file that partly overrode what somebody just typed would
  be the most confusing possible arrangement. `--config PATH` reads a named one
  whatever else was typed and appends what was typed after it; `--no-config`
  reads none. The same subcommand in both places is a merge, two different ones
  a refusal.
- **What it read is printed.** A packaged app taking silent orders from a file
  nobody remembers writing is a swallowed error with better manners.
- **Option names are not validated here.** A misspelled key becomes an option
  argparse has never heard of, and argparse names it and stops — a better error
  than this module could invent, and one that cannot drift out of step with the
  real options. Line *shape* is validated, because `port 8900` with no `=`
  would otherwise vanish.
- **The launcher puts `serve` in front**, not at the end. Every option here is
  either global or a subcommand's, and both parse after the command name; a
  settings file of nothing but options would otherwise leave them in front of
  it, where argparse cannot place them.
- The value is the rest of the line, so a Windows path with spaces needs no
  quoting. Only the `command` line is split, on shell rules.
