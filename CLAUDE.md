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
`vols.py` reads the workbook's CONFIG sheet by its old column names, so the
sheet it was written against is kept as `files/vol_marks_legacy_format.xlsx`
-- same marks, old layout -- and the comparison still runs. volkit reads
either (§4).

- ~29,000 lines across 48 modules, 728 tests, `unittest` only (no pytest).
  Tests live in `tests/test_volkit.py`, `tests/test_agent.py` (the desk
  agent, §17) and `tests/test_marking.py` (the marking agent, §18).
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
marketdata validated Excel reader; CONFIG is two columns and a cross
           names its own dollar legs
feed       spot / forward points from file, interpolated
econ       scheduled economic events (rules + dated table)
book       all pairs, built in dependency order
listed     exchange traded options: paste parsing, least-squares SABR fit,
           comparison against the marked FX surface, and a position book with
           aggregated greeks both Black-Scholes and on the fitted smile
moments    risk-neutral distribution from a smile; two combined into a cross
history    historical spot / forwards / quotes; realized vol, skew, kurtosis
analytics  carry and roll, fair value, the cross triangle, indication pricing
relvalue   one score per expiry and strike: implied against realized in level
           and in shape, the roll and the forward carry, the cross triangle,
           and each cell's own z-score in its history
curves     several vol curves side by side, and the same curve on other dates
monitor    small panels: what has moved between two points in time, per pair
quotes     a broker run, in English or in columns: outrights, RR, fly, spreads,
           timestamps and which of two quotes for one thing is live. And the
           same grammar with the price taken out: what is being asked for
knowledge  the per-pair knowledge bank: widths, floors, shifts, notes
marketmaker  two panels: the fit (curve to a target, wings to a market) and the
           quote (a two-way in each instrument asked for). They meet at the
           marks the fit hands back
archive    every observation the desk has kept: quotes shown, trades printed,
           prices we made and what became of them. Append-only, content-addressed
dtcc       fetching the public dissemination files from DTCC: which URL, whether
           a 200 is really a file, and what a 404 on a Saturday means
sdr        the public dissemination file, both layouts, zipped or not, without
           guessing
llm        a local model on a short leash: prose into the house grammar, and the
           finished decision into English. Every number it returns is checked
           against the text it was given
ingest     the watched folders, read once each by content
synthesis  the archive worked out: age-weighted widths, where the market has
           been, what became of our prices, what printed
agent      request in, price out, with the ordered list of ingredients that
           sum to it
remarks    every time somebody moved a mark, and what from: a re-mark is a diff
           of two snapshots, so nothing has to be instrumented
marking    the marking agent: how to run the fit, and what this desk does after
           it -- tendencies with counts on them, never a policy
consult    what the two agents say to each other: a finding, a proposal, and a
           scored critique of what it broke
ask        the third agent: a question in English about what the tool holds,
           answered from the record with every fact sourced. Reads everything,
           writes nothing
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
- **One place reads a level, and a cross it does not hold it builds.**
  `Book.market_level` is that place: spot, the outright forward, the points
  and the pip, for the band model, the strike axis, the carry table, the
  relative-value grid, a pricing leg with a blank spot and the market-maker
  sheet's absolute strikes. When the feed does not quote the pair itself but
  quotes both of its legs, the level is **composed from them** -- EURJPY is
  EURUSD x USDJPY, EURGBP is EURUSD / GBPUSD, by `cross.infer_leg_signs` read
  as quotation rather than as correlation. That is triangular arbitrage and
  not a model, and it is why a loaded feed is no longer invisible to a cross:
  every screen used to ask the feed for the pair *by name*, so the
  market-maker screen refused a strike quote on EURJPY while the pricing
  screen quoted both its legs off the same file. `derived` and `via` travel
  with the level and every screen shows them, because a level that came out
  of an identity and one that was published must not read the same. Half a
  triangle is still a refusal -- no NZDUSD in the file, no GBPNZD forward --
  and the points are the cross's own in the cross's own pips, never the legs'
  points added.
- **The workbook's CONFIG sheet is two columns: the pairs and the tenors.**
  A pair with the dollar on one side is marked on its own backbone; a pair
  without one is a **cross**, is never marked directly, and is broken into the
  two dollar pairs the market quotes by `cross.dollar_legs` -- EURJPY into
  EURUSD and USDJPY, EURGBP into EURUSD and GBPUSD. What is marked for it is
  the **correlation** between those legs, which is what a cross's
  `initial` / `long term` / `MR` cells have always meant. A leg nothing listed
  is added, because a cross cannot be built without both of them. None of this
  was ever a decision -- EURGBP has one sensible pair of legs -- and writing
  them out per cross was a chance to write one upside down, which flips the
  triangle's sign (§5 item 1). The old `BASE` / `COR` / column-per-cross layout
  still loads and **explicitly named legs win**: a sheet that says something is
  not second-guessed by a convention. Every derivation travels in
  `MarketData.notes` and is shown, for the same reason `derived` and `via`
  travel with a market level -- a pair that came out of a convention and one
  that was written down must not read the same.
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
- **A pricing leg's market is spot, the swap and the outright, and they hold
  one identity: `forward = spot + swap / pip`.** Two of the three are free
  and the third is arithmetic; `fwdsrc` says which of the swap and the
  outright the leg is holding, and `syncMarket` in the page is the one place
  the other is worked out. The feed fills all three -- and while it is still
  filling them the outright box takes the feed's **own** published outright
  rather than the sum of the two rounded boxes above it, because the boxes
  carry different precisions (a yen spot shows a tenth of a pip, the swap
  four decimals of one) and adding them up lands a digit off a published
  cross: EURJPY 1M off the sample file reads 162.864 that way and 162.865 as
  published. Only the outright is ever posted, so what is priced is the box
  that is on the screen and the server never has to choose between two
  spellings of one number. Beside them is the expiry, which takes a tenor
  (`1W`, `8d`) or a date in any of the spellings `timeutil.parse_datetime`
  reads and comes back holding the one standard date, so what is priced is
  what can be read on the screen. `pricing.resolve_legs` is that place: the expiry through the pair's own
  calendar, the level through `Book.market_level`, and the *same* reading the
  pricer does -- so what `Fill legs` writes into a row cannot differ from what
  the row is then priced at, and a cross the feed quotes only through its legs
  fills its boxes rather than being refused, which is what asking the feed for
  the pair *by name* used to do here. `/api/legs` answers while somebody is
  typing and re-reads no file; `/api/feed/refresh` is the same reading after
  the file has been read again. A box the feed filled is refilled when the
  pair or the expiry moves -- the points are interpolated to the expiry, so a
  forward left behind is the wrong forward -- and a box somebody typed is not;
  the browser owns that distinction, as it owns the panel. Emptying a box
  hands it back to the feed, which is the only way back and so is the
  documented one, and moving a leg to another pair hands both back on its
  own -- a level somebody marked by hand belongs to the pair they marked it
  for, and 150.25 carried onto EURUSD is a silent zero with a decimal point
  in it. `OptionLeg.forward_points` is the other spelling, for a
  caller holding points rather than an outright, and it defaults to `None` and
  not to zero: nothing else can tell "said nothing about the forward" from
  "the forward is at spot", and those two want opposite things from the feed.
  The screen does not use it -- the swap box is converted where it is typed,
  the way every other edge of this tool converts once -- and a test pins that
  a leg never posts `points`.
- **The Results rows repeat no input box, because what was resolved is
  written back into the box it was asked in.** The expiry already worked that
  way; the strike and the option type now do too. `ATM` and `25d` are ways of
  *asking for* a strike, and once one has been solved on the marks the box
  holds that strike and the next price uses it -- so a delta strike does not
  quietly re-solve under a mark that has moved, exactly as a tenor does not
  re-read on a later morning. The box says what it was asked as (`strikeask`,
  shown as its tooltip), typing in it asks again, and moving the leg to
  another pair puts the **request** back rather than carrying 150.446 onto
  EURUSD. Showing the same number as an input above and an answer below was
  two places for one number to disagree, and for spot and the forward it was
  worse: the box was the input and the row read like a result.
- **A screen shows a field only where the model reads it.** The pricing grid's
  rows carry the products they belong to: no barrier on a vanilla, no strike
  or ramp on a touch, no overhedge outside one. A box that can be filled in
  and is then ignored is a silent zero with a cursor in it. A row another leg
  needs keeps its place and shows a dot, so two legs never look like the same
  instrument, and the columns are a fixed width so a long premium cannot
  widen the leg beside it.
- **A panel is read before it is repainted.** Every screen here owns its own
  state and posts it whole (§4, the server holds none), so the fields *are*
  the payload: a spinner written into the div that holds them removes them,
  and the payload function then reads `.value` off `null`. The managed-band
  card did exactly that and no Apply ever reached the server. Build the body
  first, put progress somewhere that is not the form, and report a failure
  **beside** the form rather than over it -- a number with a typo in it is the
  ordinary way to reach the error path, and the box the typo is in has to stay
  on screen to be corrected.
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
- **A fit and a quote are two calls, and the marks travel between them.**
  The market-maker screen's fit hands back `capture_marks` and the browser
  posts it with the request; `applied_marks` puts it on the surface for that
  one call and verifies the restore. The server still holds no screen state --
  the marks are the browser's, like the panel -- and a quote given none prices
  the surface as it stands and says so. See §11.
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
- **The treatment is part of the smile cache key, and so is the level the
  feed puts the band at.** Two hazards are two smiles; a cache that could not
  tell them apart would serve the first answer for the rest of the session.
  The same is true of the forward, because a band is absolute and is placed
  against whatever the feed says *now* -- and the feed is re-read all morning
  (§15). With only the treatment in the key, a republished spot moved the
  forward column on the band card and left every probability beside it
  calibrated against the old one. `VolSurface._band_placement` is that half of
  the key; it is read for every band slice rather than only for the moneyness
  ones `band_for_slice` actually looks the feed up for, because a key that has
  to reproduce a decision made further down is a second place for that
  decision to live.
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
- **The relative-value carry signal stretches the fair-value break-even out
  into the wings.** The gamma against theta is read as `(h/T) * vega *
  (sigma_R - sigma_I)`, which is first order at the at-the-money and rougher
  at a 10 delta strike, where the option's gamma over the horizon is not that
  share of its whole life. Stated in `relvalue.py`, not corrected for. What
  *is* corrected for is the option's own delta: see §9's `carry_hedged`.
- **The relative-value shape signal inherits SABR's lack of mean reversion.**
  The comparison smile is built from the *measured* `(rho, nu)`, and a measured
  `nu` falls away at long tenors because real volatility mean-reverts and SABR
  does not. A long-dated wing can therefore be scored rich against a
  comparison smile that is flatter than the market would ever be. The grid
  warns, the ρ/ν card says the same thing, and neither corrects for it.
- **The managed-float reading is a heuristic on measured numbers, and it is
  not the authority on anything.** A hard, defended band is a *policy fact*
  and is marked in `files/bands.csv` (§6); `relvalue.suppressed_diffusion`
  only raises a hand on a pair whose carry and realized volatility have the
  shape. It deliberately takes **two** conditions, because the obvious
  one-condition version is wrong: read as `|c| / sigma` alone, USDJPY on a
  five point rate differential and ten volatility points scores 0.53, right
  beside USDCNH's 0.50, and USDJPY is not managed in any sense. The second
  condition is a low realized volatility in absolute terms
  (`MANAGED_VOL_CEILING`), which is the suppressed diffusion itself rather
  than a consequence of it. A high-carry, high-volatility pair -- USDTRY at 35
  and 25 -- is outside it on purpose: its diffusion is not suppressed, it is
  merely expensive. Both thresholds are marking judgements and the
  measurements behind them are reported whether they trip or not.
- **The carry weight is not tapered by the regime, on purpose.** `regime_z`
  says which side of the carry-dominance line a tenor is on, and the row, the
  carry signal and the CLI all say so, but the weight stays where the desk put
  it. A score that quietly reweighted itself would be a different statistic on
  every row with nothing on the screen to say so -- the same rule as §11's
  knowledge bank, where a width that no rule matches gets no width rather than
  an invented one.
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
- **The contract is free text, and a typed one says so.** A desk trades more
  listed contracts than any table shipped here will hold, so `UNDERLYINGS` is
  a set of *suggestions* offered by the box, not the set of legal answers. A
  code it does not hold is taken as typed and carries `known=False`: its pair,
  strike direction, scale and contract size are the panel's own, nothing is
  inferred from the name, and every screen marks it *typed* — a typo (`6R` for
  `6E`) must not read as a contract that merely has no mapping. What is still
  refused is a code with no shape, so a mis-pasted quote row cannot become the
  name of a contract. The dropdown it replaced is why: every contract missing
  from it had to be entered as `CUSTOM`, two `CUSTOM` panels on one screen
  cannot be told apart, and a position line naming either was refused as
  matching two panels with no field left that could settle it.
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
- **Three aggregates, because three different things add.** *Per panel*,
  every column adds. *Per contract*, every column still adds — the panels
  under one code are the same contract at different expiries, and that is the
  number a desk means when it asks how much `6E` it is running. This is what
  §8 always said and what the code did not do: it aggregated per *panel* and
  jumped straight to money, so a book of one contract over four expiries had a
  futures-equivalent delta nowhere. Two delivery months are not the same
  future, so the row says that total is a net position and not a hedge ratio.
  *Across contracts*, only money adds. `ADDITIVE_GREEKS` is the one
  declaration of which columns those are.
- **Money adds only within one settlement currency.** The premium comes out of
  Black-76 in the currency the *listed strike axis* is quoted in, which
  `ListedUnderlying.premium_ccy` derives from the pair and the inversion —
  the term currency as the pair is written, the base currency when the
  contract is the reciprocal. Every CME contract works out as USD, which is
  why this was a single total for as long as the contract came off a list; a
  typed contract need not, so the totals are struck per currency and the
  all-in row is *blanked* rather than left showing a sum of euros and dollars.
  A panel with no pair has no derivable currency: it joins the total and the
  screen says it was assumed to match.
- **A line that matches no panel, or two, keeps its place and says which.** A
  position priced against the wrong month's curve looks perfectly ordinary,
  which is the one thing that may never be guessed at. The refusal offers the
  **label** as well as the contract and the expiry, because two panels may
  legitimately agree on both of those and the label is the one field that is
  always free to differ. A panel that will not
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
- **The volatility unit of a historical sheet is decided once, from the ATM
  column**, and applied to the RR and fly. Per-column sniffing reads a small
  risk reversal as a decimal and returns it 100x too large.
- **The relative-value grid** (`relvalue.py`) is the screen's first card and
  its summary: one score per expiry and strike, positive when the mark is
  rich. It is **not a new model** -- every signal is one of the comparisons
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
  - **One scale, and it is the cell's own historical standard deviation.**
    Half a volatility point is a great deal on a one-year at-the-money and
    nothing on a one-week wing, and only the history knows which. **The scale
    window is not the realized lookback**: the lookback is matched to each
    tenor because a one-month implied forecasts one month, but how much a
    volatility *moves* is a slower measurement (`HISTORY_DAYS`, a year). Run
    off one window it measured a one-month mark on a month of a smooth series
    and read an ordinary half point of richness as thirty standard
    deviations.
  - **No history means no score**, and the volatility points are still
    reported. Inventing a scale would be inventing the answer. A wing the
    sheet does not quote borrows the at-the-money's scale and `scale_source`
    says so, because a z-score is only as good as its denominator.
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

## 10. Working on this

```
python -m unittest discover -s tests        # 697 tests, ~7m
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
python -m volkit analysis EURJPY --history files/history_sample.xlsx --horizon 7 \
    --relative-value --weight carry=0.4   # score the whole expiry / strike grid
python -m volkit mm EURUSD --target-source quotes < run.txt   # the fit, on its own
python -m volkit mm EURUSD --file run.txt --request ask.txt --fallback-spread 0.3
python -m volkit mm EURUSD --request ask.txt --target-source none   # the quote, on its own
python -m volkit mm EURUSD --learn < run.txt          # propose widths, --save writes them
python -m volkit mm EURUSD --request ask.txt --archive-width   # the archive on the width ladder
python -m volkit mark propose EURUSD --file run.txt --out p.json   # the marking-agent card's path
python -m volkit agent ask EURUSD "how wide has the 3M fly been shown this month, and by whom"
python -m volkit agent ask EURUSD --journal mm_remarks.jsonl   # interactive: a question a line
python -m volkit serve --journal mm_remarks.jsonl     # where the card's verdicts go
python3 files/make_history_sample.py        # regenerate the example history
python3 build_exe.py --host-check           # validate the packaging (Windows exe: on Windows)
python3 build_exe.py --only-tabs pricing,marking   # a build without the other three
./build_windows_github.sh                   # drive the Windows build on CI, fetch the exe
./build_windows_github.sh --explain         # print a failed run's own log
python3 build_exe.py --hidden-tab mm        # built, off until --enable-tab mm
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 --rho -0.2
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --positions book.txt        # aggregated greeks, BS and smile
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --panels more.json --positions book.txt   # several contracts at once
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

**Those three stages are two panels, two routes and two buttons.**
`marketmaker.Panel` (`/api/mm/fit`) reads the market paste and does stages 1
and 2; `marketmaker.QuotePanel` (`/api/mm/quote`) reads the **request box** and
does stage 3. The fit puts a price on nothing and the quote fits nothing.

- A fit is a morning's decision, taken against a run that has just arrived; a
  quote is answered in seconds, over and over, against whatever was fitted. A
  request does not arrive with a market on it (§17 says the same thing about
  the quoting agent), so tying the two together meant a request could only be
  priced by re-running a fit against a market that had nothing to do with it,
  and a market could not be fitted without also producing prices in
  instruments nobody had asked for.
- **They meet at `capture_marks`, and the browser carries it.** The fit hands
  back the parameters it arrived at -- volatility knobs in points, like every
  other number crossing this boundary -- and the quote posts them back and
  puts them on the surface for the length of one call, under `applied_marks`,
  which restores and *verifies* the restore the way `marking.marked` does. The
  server holds no screen state (§4) and this does not change that: the marks
  are panel state and the browser owns them. A quote given no marks prices the
  surface as it stands, and `sheet.marks.note` says which of the two it was --
  a price made on this morning's fit and one made on last night's marks must
  never read the same. Marks naming another pair are refused: the browser
  holds the fit and the pair selector apart and they can be moved apart.
- **The request box takes no prices** (`quotes.parse_requests`). One number on
  a line that has not already said what it is struck at is a strike; anything
  else is refused with the line. A broker run pasted into the wrong box would
  otherwise be quoted at levels nobody asked about.
- **A request is quoted in the convention it was asked in.** `JPY call over`
  on USDJPY carries `sign = -1` on the request, and the sign is applied
  **once**, where the row is built -- to the model value and to the bank's
  shift, before anything else reads them, so the bid is still the low side of
  what we show and no second place exists for a sign to live. §5's first entry
  is what a second place for a sign costs.
- **A request the market paste also quoted carries that market beside it**,
  matched on `quotes.instrument_key` -- what makes two lines the same quote --
  so "inside their market" survives the split. The paste is read here for that
  and nothing else: it is never fitted to on this route, and its own parse
  notes are not repeated beside a price, because the fit that read it already
  reported them.
- **The fair value is measured inside the marks**, not before them. It is the
  mark against realized volatility, and the mark being shaded is the one being
  quoted; measured outside, a fit that moved the at-the-money half a point
  would have its price shaded by the richness of the level it had just left.
  This moves quote numbers against the single-panel version, and it is the
  only thing that does.

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

---

## 17. The quoting agent (`archive.py`, `sdr.py`, `llm.py`, `ingest.py`, `synthesis.py`, `agent.py`)

The market-maker screen answers "what do I show against *this* market" and
needs a paste to fit to. A request does not arrive with a market on it, so the
agent answers "where are you on the 1 month at-the-money in a hundred million"
out of four sources: the marked surface, the knowledge bank, an archive of what
has been seen, and the same two leans (fair value, position) the market-maker
screen uses with the same caps.

Two ways in. `volkit agent <action>` on the command line, with `quote`,
`ingest`, `watch`, `evidence`, `learn`, `shown`, `outcome` and `archive`; and
a **card inside the market-maker tab** — not a seventh screen.

That is deliberate. The card answers a question *about the market on that
tab*, and a build without the market-maker screen has nothing for it to
answer, so it is three more routes on `mm` (`/api/mm/agent`,
`/api/mm/agent/ingest`, `/api/mm/agent/file`) and `agent` is one of `mm`'s
commands. `screens.SCREENS` keeps six entries, the nav and panel-map tests are
untouched, and excluding the market-maker tab takes the agent with it.

The card is `agent.SuggestPanel` and it **fits nothing**. A width comparison
needs the paste, the bank and the archive and no surface at all, so it answers
without touching the curve, the wings or the marks — which is what lets it sit
on its own button beside a fit that takes a second and a half. It posts its
own payload (`const AF=[…]` in the page) rather than the market maker's, read
by `agent.panel_from_request`, with a test pinning the two lists against each
other exactly as `MF` is pinned against `marketmaker.panel_from_request`.

Things the card decides once:

- **It compares widths and proposes nothing else.** Per quoted row: what the
  market showed, what we would show (the bank rule, or the panel fallback),
  and what the archive says this has actually been shown at. The verdict is
  `agrees`, `tight`, `wide`, `no rule`, `thin` or `not read`, and the quote
  sheet's width does not move until a rule is written in the bank below it.
- **Agreement is the quiet case.** A gap has to clear both a fraction of the
  archived width (`tolerance`, 10% by default) *and* an absolute floor
  (`MIN_GAP`, 0.02) before it is worth saying. Without the floor a 0.08
  butterfly "disagrees" over four thousandths and every wing row carries a
  flag forever, which is a screen nobody reads.
- **Its columns only appear beside the quotes they were computed from.** The
  page keeps the paste the last run saw (`agentFresh()`), and the two extra
  quote-sheet columns are omitted when the textarea has moved on. A stale
  width sitting next to a fresh quote is worse than no width at all.
- **The browser chooses when to scan, never where.** The watched folders come
  from `serve --chats` / `--sdr` and live on the service; the ingest route
  reads no path out of the payload, and a test pins that. A path a page can
  post is a path anything that reaches the page can read.
- **Filing the pasted run stamps it at the start of the valuation day**, not
  at the instant the button was pressed — the id is a hash of the content, so
  "now" would give a double-clicked morning a new id and count it twice in
  every width it touches. The same run under a *different* broker name is a
  genuinely new record (three brokers showing one width is stronger evidence
  than one broker three times) and is also the obvious way to double a width
  by accident, so `under_another_name` counts it and the card says so.

Things decided once, which must not be re-derived per row:

- **The output is a list of ingredients that sums to the price, and the prose
  is generated from the list.** Not the other way round. `Decision.trace` is
  the record, `Decision.facts()` renders it, `explain()` prints it and
  `llm.narrate` writes the paragraph *from it*. A story written first and
  reconciled to the numbers afterwards is a story that stays plausible when
  the numbers are wrong.
- **The archive is never quoted back at the market.** The recent market level
  is computed, shown beside the mark, and applied to nothing. A market maker
  whose mid follows the last thing it was shown is being led by the party it
  is about to trade with. A gap is a *flag*; the answer to a flag is to
  re-mark on the marking screen, deliberately.
- **The width ladder is bank, then archive, then a typed fallback, then no
  price.** Every row names the rung it stood on. A row that reaches the bottom
  shows no bid and no offer — §11's rule, unchanged, and the archive is a new
  rung on that ladder rather than a new default. The quote panel has the same
  ladder behind `QuotePanel.use_archive_width` (§18 says how it is switched
  on), and the order matters there: the bank overlay folds the typed fallback
  in when no rule matches, so the archive is tested against `spread_rule`
  and not against `spread`, or the fallback would beat it.
- **What became of our prices moves nothing.** Hit rate and adverse selection
  are the most interesting thing in the archive and the easiest to over-read:
  a run of lifted offers is sometimes a mid that is too low and sometimes a
  week of being the only one showing. `OutcomeEvidence.lean()` returns
  *words*, and the row says "shown here, and applied to nothing".
- **Thin evidence produces no number.** Weights sum to an effective count and
  below the floor (default 2.0) the answer is "not enough", named, with what
  there is. Same rule as the bank, same reason.
- **Age is a weight, not a cutoff.** `0.5 ** (age / half_life)`, default five
  days. A cutoff would make a width jump the day one observation crossed a
  line, for a reason nobody could point at. An observation with no readable
  time counts as one half-life old — treating it as current and dropping it
  are both wrong in a way that surfaces later as a width nobody can explain.
- **Nothing after the valuation time is used.** `--asof` a past date and the
  archive is read to that instant, and says how many observations it left out.
  Without this every backward-looking check on this tool flatters it.

The model, specifically:

- **It never produces a number that reaches anything.** Extraction returns
  candidate lines that `quotes.parse_quotes` must then accept; a line the
  grammar refuses is refused, reported with its text, and never repaired.
  Narration is generated from an already-computed record.
- **The numeric guard is a set membership test, not a similarity score.**
  Every number in what the model returns must already be in what the model was
  given. `8.60` against a chat that said `8.6` passes because both canonicalise
  the same way; `8.65` does not, and the whole line goes — a line with one
  invented figure was being reasoned about rather than transcribed, and the
  rest of it is not more trustworthy for being arithmetically unremarkable.
  It is strict and it does produce false refusals; a refused line is shown so
  it can be typed by hand, which is the right trade against a fabricated level
  nobody notices for a month.
- **Everything works without it.** No model configured or none running: files
  are still ingested by the grammar, the archive still fills, the statistics
  still compute, the price is still made and the explanation is the itemised
  one. What degrades is that prose the grammar cannot read stays unread. Every
  action prints one line saying which of the two it was — a build that quietly
  used a model and one that quietly did not must never look the same.
- Transport is `urllib` and nothing else, against Ollama's `/api/chat` or any
  OpenAI-compatible `/v1/chat/completions`. No SDK: this ships as one
  executable to a desk machine that may have nothing installed. Configured
  through `VOLKIT_LLM_BACKEND` / `_URL` / `_MODEL` / `_TIMEOUT`, or the
  `--llm-*` flags.

The archive, specifically:

- **Append-only, and content-addressed.** A record is never edited and never
  deleted; a correction *supersedes* one and both stay in the file, exactly as
  `quotes.ParsedRun` keeps a superseded quote. `Archive.live()` is the view,
  `Archive.records` is the file.
- **The id is a hash of the content**, so re-reading yesterday's chat does not
  double the evidence behind a width. That is the likeliest way for this thing
  to lie — a folder rescanned nightly, every width slowly gaining confidence it
  never earned — so the fallback timestamp for a line with no clock on it is
  **the source file's** modification time and never `now`; `from_quotes`
  refuses to run without one. Levels are rounded to 6dp before hashing, because
  `8.2 * 100` is not `8.2` in binary and the same quote reached two ways hashed
  two ways.
- **Four kinds, because they are evidence of different things**: `quote` (where
  the market was shown), `trade` (where business got done, which is one side of
  somebody's market), `shown` (what we made — evidence about us, not the
  market), `outcome` (whether we were right).
- **`delta` is a fraction on the record**, 0.25 for a 25 delta — the spelling
  `quotes.py` parses into and `knowledge.Rule.delta` matches on. Points are a
  display convention and `describe()` is the one converter. Storing points
  would mean a rule lookup silently missing and the quote falling through to
  the fallback.
- Volatility is in **points** throughout the file, like the bank.

The dissemination reader, specifically:

- **Headers are matched by meaning, both layouts**, pre- and post-2022-rewrite;
  a column that cannot be placed is reported with the header that confused it.
- **A capped notional is not a notional.** Kept, flagged, and treated as a
  lower bound; never used as an equality, and `implied_from_trade` refuses to
  invert a premium against one.
- **A cancel is not a trade and a correction is not two.** Each carries the
  dissemination id it names and `Archive.resolve` ties it to the record it
  supersedes, or reports that it could not — publishers cancel prints from
  before the file existed, and a cancel silently matching nothing looks exactly
  like one that worked.
- **A premium is not a volatility.** Inverting one needs a forward, a discount
  factor and a model, all of which can be re-marked, so the economics are
  stored as published and `synthesis.implied_from_trade` does the inversion
  with its inputs named on the result.
- **The pair regex needs both legs to stand alone.** Without the boundary
  look-arounds it found `COM` + `MOD` inside `COMMODITY` and filed a crude oil
  trade under the pair COMMOD.

### Getting the trades (`dtcc.py`)

`volkit agent fetch --sdr sdr/ --since 2025-09-01` downloads DTCC's public
price dissemination files; `serve --sdr DIR` puts a **Fetch from DTCC** button
on the agent card for the last few days. What arrives is read immediately,
because a file downloaded and not ingested is a file somebody has to remember
to come back for.

Verified live from this repo: `https://pddata.dtcc.com/ppd/api/report/`
`cumulative/cftc/CFTC_CUMULATIVE_FOREX_YYYY_MM_DD.zip` answers with a zip.
DTCC keeps **366 days** and publishes nothing before **2023-12-29**.

- **Fetching and reading are two modules.** `sdr.py` must keep working on a
  desk with no route out, which is most desks this is built for, and a reader
  that could not be exercised without a network is a reader nobody can test.
- **The network is injected** (`Downloader.opener`), like the clock is
  everywhere else. That is what lets all of `dtcc.py` be tested offline -- and
  it had better be, because the machine this is developed on has no route to
  DTCC either.
- **A 200 is not a file until it has been checked.** A proxy, a captive portal
  or an outage answers every request with HTML and status 200; trusting the
  status writes that HTML into the SDR folder, where the reader meets it
  tomorrow and reports a header it cannot place. Every body is checked for
  being a zip holding a CSV, and one that is not is refused with the first
  line of whatever came back -- usually the whole diagnosis.
- **Nothing published is not a failure.** Two days in seven have no session. A
  404 on a date DTCC keeps is reported as "nothing published"; a run that
  shouts every weekend is a run nobody reads. But "nothing published" needs
  *every* candidate URL to have said 404 -- reporting the last status instead
  called a 500 followed by a 404 on an older spelling a quiet weekend, which
  is a server falling over in disguise.
- **A date outside the window is refused before any request**, by name, rather
  than becoming a 404 the caller has to interpret.
- **The folder is the cache.** A date already on disk is not fetched again.
  One mechanism, and it is the same folder `sdr.py` reads and a person can
  open; two would be two places for it to go stale.
- **The URL is probed, not assumed.** The path has changed once. An ordered
  list of candidates is tried, the one that answered is reused for the rest of
  the run, and when none answers the error names every URL it tried -- which
  is a thing a person can paste into a browser.
- **The page chooses when, never where.** The folder and the proxy come from
  the command line; the browser sends only how many days back, capped at 30. A
  year's backfill is a command with somebody watching it, not a button that can
  be leaned on.
- One request at a time, a pause between them, a named `User-Agent`, and a
  retry with backoff on a 429 or 5xx -- a 404 is never retried, because asking
  again more slowly does not create a file. A desk that gets itself blocked
  from a public utility has broken something it cannot fix from here.

### What a printed trade teaches (`synthesis.invert_trades`)

Off by default; `agent trades PAIR --invert --history vol_history.xlsx` turns
each premium into the volatility it implies. Verified: a premium built from a
known volatility comes back as that volatility to 1e-11.

- **The forward comes from the trade's own date**, out of the historical
  workbook, interpolated across pillars by `history.forward_series`. The live
  feed is deliberately *not* a fallback for a trade that printed three weeks
  ago: inverting last month's premium against this morning's forward is wrong
  by the whole of the carry since, and wrong silently.
- **"Last row on or before" is bounded** (`MAX_STALE_DAYS`, 7). Friday's row
  is a fine forward for a Monday trade; the rule without a bound reached back
  two years for a trade the sheet did not cover, and said nothing.
- **The currency the size is in decides whether the arithmetic is well posed.**
  `premium / notional` is a price per unit of the *notional* currency; Black-76
  here wants domestic per unit of base. Notional in the base with premium in
  the quote is the straightforward case; a premium in the base is multiplied by
  the forward first; a notional on the quote leg is **refused**, because
  recovering the base amount needs the convention the trade was struck under
  and that is not published.
- **There is no discount curve, here or anywhere in this package** (§ `pricing`
  says so). Undiscounted, the volatility reads *low* by roughly the discount
  over the option's life -- about 4% of the volatility on a one-year option at
  4% rates, negligible inside a month. It is said on every row, and
  `--discount-rate` removes it.
- **Expiries are taken at midnight UTC**, because the file publishes a date and
  no cut, so a short-dated reading is a touch high. Said once per run.
- A capped notional is never inverted, and a cancelled print is not business
  that got done.
- Refusals are counted **by reason**, not listed one by one: a day of
  dissemination is thousands of rows and "1,180 had a capped notional" is the
  useful shape of that.

Files, all beside the workbook like the bank: `mm_archive.jsonl` (the
observations), `mm_ingest.json` (what has been read). Tests are in
`tests/test_agent.py` — the one test module outside `tests/test_volkit.py`,
because most of it needs no numeric stack and runs on stdlib plus the package.

---

## 18. The marking agent (`remarks.py`, `marking.py`, `consult.py`)

A second agent, and a different problem from §17's. The quoting agent answers
*what do I show*; this one answers *where should the surface be*, which is the
question the marking screen's two fitters leave to a person.

**The fitters do not change.** `fit_atm_curve` stays a cold fit,
`tune_smile_shifts` stays a curve-wide additive shift under a hinge, and every
rule in §11 stands. What a marker actually agonises over is the judgement
*around* the fit -- which knobs to free this morning, whether four targets can
determine four parameters, whether to touch the wings when only the
at-the-money was quoted -- and then whether to take what came out. That
judgement is what this agent makes, and the last part of it is what it learns.

Two ways in, like the quoting agent. `volkit mark propose|confer|learn|journal|
record` on the command line, and a **card inside the market-maker tab**,
beside the fit it plans. Both belong to the **mm** screen: the fit this agent
runs is `marketmaker.fit_atm_curve` and `tune_smile_shifts`, the fit panel's
own, so a build without that tab has nothing for it to plan -- and the command
moved there with the card (it was listed under marking, which was the wrong
screen for the same reason). Excluding the market-maker tab takes both agents.

### The card, and how the two agents are tied to the two buttons

The tab has two buttons because it has two jobs (§11), and each agent is
wired to one of them:

- **The marking agent is on the fit.** `marking.MarkPanel` (`/api/mm/mark`)
  reads the *fit panel's own fields* -- the paste, the target source and
  text, the conventions -- through the same `marketmaker.panel_from_request`
  the Fit button uses, and answers how it would run that fit and what came
  out. It has no market of its own on purpose: a proposal about some other
  fit is not an answer to the question on the screen. What it adds is the
  marker's judgement -- `choose_knobs` lets it pick the free set (a rule from
  the target count and what the quotes inform, a learned pin from the
  journal), or it takes the panel's ticks as the caller's -- and the learned
  nudge. Its answer carries `marks` in exactly the shape `Panel.run` hands
  back, so **Accept** puts the proposal where a fit's answer goes: the
  browser's one holder for the marks the quote stands on (`HELD`, filled by
  the fit or by an accepted proposal, never both at once), and the quote
  sheet's note names which. **Take the plan onto the fit** writes the plan
  into the fit panel's knob boxes and runs Fit, so the desk can adjust and
  press Fit again; **Record my fit as the edit** then journals the fit's marks
  beside the proposal, which is the row §18 says is worth the most. The
  verdict buttons vanish when the paste has moved on since the proposal,
  for the same reason the quoting agent's columns do.
- **The quoting agent is on the quote.** Its card compares widths and proposes
  nothing, as before; the link is the **widths from the archive** switch on
  the toolbar, which puts the archive on the quote panel's width ladder --
  **bank, then archive, then the typed fallback, then no price**, the same
  ladder and order as `agent.run` -- and every row names the rung. Off by
  default: the archive is evidence about the market, and a desk that has not
  yet convinced itself of it should not find it under its prices. The
  evidence settings are the quoting agent card's own boxes (half-life, minimum
  evidence, lookback) read by both, so the card and the quote never disagree
  about what the archive holds. The archive's *level* rides on the row as a
  flag and is applied to nothing (§17's rule). `volkit mm --archive-width`
  is the same switch.
- **`/api/mm/mark/record` is the only route on the card that writes**, and it
  writes to the journal. `accepted` records the proposal as the outcome,
  `rejected` records the start, `edited` needs the marks the desk ended on
  and refuses without them -- an edit recorded as the proposal would be the
  agent agreeing with itself. `apply` puts the recorded marks on the loaded
  book, the fit panel's *keep the marks* decision made here, and it reads
  that same checkbox. Answering the same proposal twice is one instance,
  said rather than raised: the journal is content-addressed.
- **Wing parameters are freed only where a quote reaches them.** The plan
  runs `marketmaker.informative_params` over the wing quotes before it counts
  them: a single 25-delta risk reversal frees `slog25`, not `slog10`, because
  the ten-delta parameters do not enter the 25-delta anchor and the tune
  refuses a parameter nothing informs. Found by the CLI smoke of the card,
  which had freed the first name on the list.

### What it learns from (`remarks.py`)

- **An instance is a diff of two snapshots, not an instrumented control.**
  `session.capture_pair` already photographs every knob, so a re-marking
  instance is a before, an after and a subtraction. Nothing in the marking
  screen reports anything, nothing is forgotten when a control is added, and a
  session file from last month can be turned into instances retroactively.
- **A verdict is worth more than a diff.** `unprompted` is somebody marking;
  `accepted` / `edited` / `rejected` answer a proposal and carry the one thing
  a diff cannot -- what the tool would have done, beside what the desk did, on
  the same morning. An **edited** proposal is the most valuable row in the
  file, and it is the whole reason the agent asks rather than only watching.
- **An absent smile shift is a zero; an absent tenor overwrite is not.** One is
  "they moved it", the other is "they left the curve to speak", and counting
  them alike loses exactly the decisions a marker thinks hardest about.
- Append-only and content-addressed, like the quote archive: a morning
  re-marked twice must not become two instances of a desk that likes moving
  that knob.

### What "learned" means (`marking.py`)

A desk re-marks a curve a few times a day. Over a month that is a few dozen
instances -- enough for a handful of scalars with error bars, nowhere near
enough for a function. So:

- **Tendencies, not a policy.** Per knob: has this desk been given the chance
  to move it and declined every time (`MIN_INSTANCES`, 5); how far does it
  typically move it; how often is a proposal taken as it stood; and does the
  desk land systematically off the fit. Every one carries its count and
  refuses to say anything below the floor.
- **A correction must be a tendency and not a scatter.** The median of six
  corrections is a number whatever those six were; it is evidence only when
  they agree. So a bias is applied only when `|median| > BIAS_SIGNAL x spread`
  (spread being half the interquartile range, which one outlier cannot set),
  and otherwise the row says *this desk lands on both sides of the fit here*
  and nothing moves. That single test is what stops the agent learning the
  desk's noise and quoting it back with confidence.
- **A correction is capped at `CORRECTION_CAP` of what the fit itself moved**
  (half). A nudge on a fitted number is a nudge; a nudge that can exceed the
  fit is a second, unexamined fit with a smaller sample behind it. Where the
  fit barely moved a knob, the correction is skipped and says so.
- **Age is not a weight here**, unlike the quote archive. A width is a fact
  about a market that moves; how a desk marks is a fact about the desk, and a
  habit from three months ago is still that desk's habit. What ages out is the
  window (a year).
- **A rule and a learned reason are labelled apart** in the trace. A rule is
  true of the model -- four targets cannot determine five parameters. A
  learned reason is true of this desk and always carries the instance count.
  Somebody disagreeing with the second must see immediately that it is the
  second.
- **Every proposal says how much it learned from, including none.** A proposal
  that quietly had nothing behind it and one built on a year of instances must
  not read the same.
- **`marked()` is the context manager everything runs inside**, and the
  restore is *verified* rather than assumed -- a surface left half-marked by a
  proposal nobody accepted, priced off all morning, is the worst possible
  outcome of a tool whose whole job is marking. A fault-injection test pins
  the guard.

### What the two agents exchange (`consult.py`)

The quoting agent's most interesting output is a flag it is forbidden to apply
(*the mark is 0.45 below where this has been quoted*); the marking agent's
hardest input is what that flag contains. So they confer, in numbers:

1. **A finding** goes quote-side to mark-side: this instrument at this tenor is
   marked here and has been quoted there, over this many observations from
   this many brokers, this recently.
2. The mark side turns findings into what the existing fitters already take --
   a `CurveTarget` for the at-the-money, a two-way `MarketQuote` built from the
   *observed range* for a wing -- and proposes. Only the at-the-money becomes a
   curve target: a risk reversal is a statement about shape, and feeding one to
   a fit that can only move the level asks a level to explain a skew.
3. **A critique** comes back: with that proposal on the book, how many observed
   markets does the surface sit inside, what improved, and **what it broke**.
4. The mark side weights what it broke by `REWEIGHT` and tries again, at most
   `MAX_ROUNDS` times, and the best round goes to a person.

Two things stop this being circular -- fit to the archive, score against the
archive, of course it improved:

- **The score counts *inside the observed two-way*, not distance to its mid.**
  Anywhere sensible scores the same, so the loop cannot improve its score by
  walking the surface onto the middle of every market it has ever seen. Only
  leaving a market scores worse.
- **Every finding is scored, including the ones no target was built from.**
  The tenors the fit was not aimed at are exactly where a re-mark gets caught
  doing damage.

**No language model is anywhere near this.** Both sides produce numbers; a
model between them could only paraphrase, and `llm.py`'s numeric guard cannot
check a negotiation. What a model may do, at the very end, is describe the
round that won.

A worked consequence worth knowing: with learned pins in force the fit has
fewer free parameters, so its RMSE gets *worse* while matching what the desk
actually does. The critique reports that numerically rather than hiding it,
and adjudicating it is the person's job.

Files, beside the workbook: `mm_remarks.jsonl` (the journal).

---

## 19. The third agent: questions (`ask.py`)

The quoting agent answers *what do I show* and hands back a price; the marking
agent answers *where should the surface be* and hands back a proposal. Each
has one output shape and a test pinning it. "How wide has the 3M fly been
shown this month, and by whom" has neither shape, so it is a **third agent**
rather than a conversation bolted onto one of the first two -- and it is
built on one rule that decides everything else about it:

- **It writes nothing.** It reads the archive, the journal, the knowledge bank
  and the surface and answers. It never prices, proposes, files a quote,
  journals a verdict or touches the book. The other two agents each have one
  writing route (§17 `file`, §18 `record`); this one has none, so a chat box
  can never be the way a width or a mark changed. A test asks about every
  topic and checks the archive and the journal byte-for-byte afterwards.
- **A question is parsed into a query, and volkit runs the query.**
  `ask.parse_question` reads the pair, tenor, instrument, delta, window and
  topic; `ask.TOPICS` is the one declaration of what can be asked (`widths`,
  `levels`, `trades`, `outcomes`, `shown`, `archive`, `journal`,
  `tendencies`, `marks`, `rules`), and every fact comes from `synthesis`,
  `marking.learn`, `curves.surface_curve`, the bank or the archive itself,
  tagged with its source. The surface is read in decimals and converted
  **once**, at this edge, to the points the archive beside it is in (§4).
- **The model may rewrite a question it cannot read; it may never answer
  one.** A question the grammar does not recognise is sent to the local model
  to be put into the grammar's own vocabulary, under `llm.invented_numbers`
  -- "the front end" may not come back as `1M`, because 1 is not in the
  question -- and the grammar then reads the rewrite. The paragraph at the
  end is `llm.narrate` over the fact list, refused whole if it holds a number
  the facts (or the question) do not. Without a model the answer is the fact
  list and `model_note` says so.
- **A question it cannot answer is refused with the list of what it can.**
  "What printed in the 3M" answered with what was *quoted* in the 3M would
  look exactly like the dissemination file. Asked to do something -- fetch,
  re-mark, record, quote -- it names the command or button that does it and
  does not.
- **A follow-up fills only its gaps, and says which.** "And the 3M?" after a
  widths question inherits the topic, pair and instrument and lists them in
  `Question.inherited`. `Conversation.from_json` rebuilds the previous
  question from its *text*, never from the posted structure, so a transcript
  cannot carry a pair the grammar would not have read.
- **The book is lazy.** `ask()` takes a `Book` or a callable; a question about
  the archive never pays for a workbook it does not read. The CLI caches one
  load per session, failures included.

Two ways in, like the other agents. `volkit agent ask PAIR "question"` on the
command line -- without a question it reads a line at a time -- and the **Ask
the record** card in the market-maker tab, under the marking agent, on
`/api/mm/ask`. Both belong to `mm`: a build without that tab has no archive
card to ask about, and excluding it takes all three agents.

- **The browser owns the transcript and posts it whole** (`const AK` in the
  page, pinned against `ask.panel_from_request`), kept in `localStorage`
  because it is a per-browser convenience and nothing else. The server keeps
  no turn; `AskPanel.run` rebuilds the previous question from the transcript's
  *text* and answers, so a turn on the card and the same turn in a shell are
  one function.
- **The evidence settings are the quoting-agent card's own boxes** (half-life,
  minimum evidence, lookback, whether model-read observations count), read
  by both, so the chat and the widths beside it never disagree about what
  the archive holds.
- **A workbook is optional to the route.** A server whose book failed to load
  still answers about the archive; a question about the surface says the
  surface is not there. The model is looked up per request, like the other
  agent routes, so starting Ollama mid-morning is enough.
