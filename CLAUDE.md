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

- ~14,300 lines across 34 modules, 347 tests, `unittest` only (no pytest).
- Runtime deps: numpy, scipy, pandas, openpyxl. Plus `tzdata` on Windows.
- Deliberately no `pysabr`, `xlrd`, `tkcalendar`, and no web framework.
- **Five screens**, each with a command-line equivalent: Pricing, Vol marking,
  Exchange traded (`volkit listed`), Analysis (`volkit analysis`), Market maker
  (`volkit mm`). One HTML file, `volkit/web/index.html`, is the whole front
  end. A build may be made **without** some of them, or with some **hidden**
  until asked for -- see §12.

## 2. Standing decisions

These were the user's explicit choices. Do not quietly reverse them.

| Decision | Consequence |
|---|---|
| **Correctness over parity** | Fix model bugs rather than reproduce them. But *document every change that moves a number*, and give the exact input change that restores the old figure. |
| **Model risk, don't remove it** | Never make a risk vanish by construction to make a model tidy. If a bad outcome is possible, put it in the model and let it be marked. (This came from a direct correction — see §6.) |
| **Implement fixes, don't just flag them** | When something is wrong, fix it and note what moves. Leave it switchable only when there is a real reason to reconcile against old marks. |
| **Web UI, not Tkinter** | Stdlib `http.server` only. No Flask/FastAPI. The desk machine may have nothing installed. |
| **Excel stays primary** | `vol_marks.xlsx` must keep working as-is. Abstract behind a data source; do not migrate to YAML. |
| **Nothing fails silently** | The legacy `except: pass` returning `0.0000` is the anti-pattern this project exists to remove. Errors surface with the real message. |

## 3. Architecture

```
timeutil   one day-count (365.2425), one injected Clock, tenor parsing
numerics   bracketed solves, damped fixed points, panel integration
calendars  holiday calendars, spot/expiry rolls, CSV overrides
timeweight intraday / weekend / holiday weighting
black      Black-76, FX delta conventions, strike-from-delta
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
           comparison against the marked FX surface
moments    risk-neutral distribution from a smile; two combined into a cross
history    historical spot / forwards / quotes; realized vol, skew, kurtosis
analytics  carry and roll, fair value, the cross triangle, indication pricing
curves     several vol curves side by side, and the same curve on other dates
quotes     a broker run written in English: outrights, RR, fly, spreads
knowledge  the per-pair knowledge bank: widths, floors, shifts, notes
marketmaker  fit the curve to a target, fine tune the wings to a market, quote it
webapp     JSON API + stdlib server;  web/index.html is the whole front end
cli        every screen has a command-line equivalent
screens    which screens a build has, shown or hidden; the one reader of the
           build's manifest, and of --enable-tab
config     the startup settings file a double-clicked exe reads
paths      resource vs user-data paths (source and frozen)
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
- The parser reports every inference and every rejected line. A table
  straddling 1.0 is refused, not guessed.
- The arbitrage check runs in **moneyness, per unit of forward**. In raw
  strike units the second difference of a yen future's prices is small enough
  that rounding reads as arbitrage; the test pins this at four forwards.

## 9. Analysis (`analytics.py`, `moments.py`, `history.py`)

A fourth UI tab. Four sections, each built and reported independently so one
missing input does not empty the others.

- **Carry and roll** revalues at a **fixed absolute strike** and splits the
  result into the term-structure slide and the smile slide, so the forward
  curve's contribution is separable. Without a feed the strike is held in
  moneyness, the smile slide is zero *by construction*, and the row says so
  rather than showing a plausible zero.
- **Fair value** is `fair = realized + roll*(T/h)*vega(T-h)/vega(T)`, derived
  in the docstring. The roll is **always the ATM roll**, built inside the
  function -- an earlier cut took it from whatever target the carry screen was
  showing, which mixed a risk-reversal roll into an ATM break-even.
- **Realized** is annualised on the model's own **volatility time**
  (`history.volatility_time`), which is pinned against `AtmCurve.integrated_vol`
  by a test. Calendar and 252-day annualisations are reported beside it, not
  instead of it. Daily skew/kurtosis are projected onto each tenor before being
  compared with the smile's, and both are shown with standard errors.
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
- **The curve comparison panel** (`curves.py`) is a fifth section, owned by
  the browser and posted whole like the listed and market-maker panels, so
  `volkit analysis --compare` reproduces a screen exactly. Four sources:
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
python -m unittest discover -s tests        # 347 tests, ~2.5m
pip install esprima                         # enables the front-end JS syntax test
python -m volkit check                      # validate the workbook
python -m volkit serve --feed files/market_feed.csv --history vol_history.xlsx
python -m volkit analysis EURJPY --history files/history_sample.xlsx --horizon 7
python -m volkit mm EURUSD --target-source quotes --fallback-spread 0.3 < run.txt
python -m volkit mm EURUSD --learn < run.txt          # propose widths, --save writes them
python3 files/make_history_sample.py        # regenerate the example history
python3 build_exe.py --host-check           # validate the packaging (Windows exe: on Windows)
python3 build_exe.py --only-tabs pricing,marking   # a build without the other three
./build_windows_github.sh                   # drive the Windows build on CI, fetch the exe
./build_windows_github.sh --explain         # print a failed run's own log
python3 build_exe.py --hidden-tab mm        # built, off until --enable-tab mm
python -m volkit band USDHKD --feed files/market_feed.csv --hazard 3
python -m volkit analysis EURUSD --history files/history_sample.xlsx \
    --compare surface --compare history:-30d --field rr25
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

---

## 12. Building without some of the screens (`screens.py`)

`build_exe.py --exclude-tab` / `--only-tabs` chooses which of the five a build
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

## 13. Starting a build nobody types at (`config.py`)

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
