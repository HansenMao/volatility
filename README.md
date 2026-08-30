# volkit

FX volatility surface modelling — ATM term structure, dated events, SABR/SVI
smiles, cross pairs, rolldown, two-way market making, and a local web
interface.

This is a rebuild of the original `vol` tool. The legacy modules
(`vol.py`, `cvol.py`, `ssabr.py`, `vols.py`, `common_functions.py`,
`__main__.py`, `rv.py`) are **left untouched** in the repository root so the
two can be compared. Nothing in `volkit/` imports them.

`MIGRATION.md` lists every behavioural difference, including the three that
change your marks.

## Install

```bash
pip install -r requirements.txt      # numpy, scipy, pandas, openpyxl
pip install -e .                     # optional: puts `volkit` on your PATH
```

`pysabr`, `xlrd`, `tkcalendar` and every web framework are **no longer
required**. The optional `holidays` package adds fuller statutory calendars;
built-in rules are used when it is absent.

## The workbook

`vol_marks.xlsx` is what the tool starts from, and its `CONFIG` sheet is **two
columns**: the pairs to build, and the tenor points.

```
PAIRS    TENORS
USDJPY   1w
EURUSD   2w
EURJPY   1m
EURGBP   3m
```

A pair with the dollar on one side is marked on its own backbone. A pair
without one is a **cross**, and a cross is never marked directly: it is broken
into the two dollar pairs the market quotes — EURJPY into EURUSD and USDJPY,
EURGBP into EURUSD and GBPUSD, EURCNH into EURUSD and USDCNH — and what gets
marked for it is the **correlation** between them, which is what the
`initial` / `long term` / `MR` cells of a cross's `PARAMS` column have always
meant. A leg nothing listed is added, because a cross cannot be built without
both of them.

None of that was ever a decision. EURGBP has exactly one sensible pair of legs,
and the sheet used to spell them out in a column per cross — which is a chance
to write one upside down, and a leg written the wrong way up enters the
triangle with the other sign. `cross.dollar_legs` is now the one place that
convention lives, and it is pinned by a test against the legs the shipped sheet
used to name by hand.

The old `BASE` / `COR` / column-per-cross layout still loads, and **explicitly
named legs win** over the derived ones: a sheet that says something is not
second-guessed by a convention. The legacy tool in the repo root reads only
that layout, so the same marks in it are kept as
`files/vol_marks_legacy_format.xlsx` and the side-by-side comparison still
runs. Anything the reader worked out rather than read
is reported — in the message box at the top of the page, and by
`volkit check`.

`PARAMS` holds one column per pair (`initial`, `long term`, `ratevol`, `addon`,
`MR`, `rate corr`, `short decay`, then one row per event date), and one sheet
per pair holds `expiry, ST 10D, ST 25D, RR 25D, RR 10D`. Everything in the
workbook is in **vol points**. Nothing is ever written back to it.

A pair named in `CONFIG` with no sheet behind it -- a tab deleted, renamed or
overwritten, and the pair left in the list -- is reported by `volkit check`,
which exits non-zero. It used to be skipped in silence: the book loaded
looking complete and the first screen to ask that pair for a smile said
`no smile term structure; run calibrate() first`, naming neither the workbook
nor the missing tab.

An event is weighted **per currency**: a `PARAMS` column headed by a currency
(`USD`, `JPY`) holds that currency's weight on each event row, and a pair's
bump is its two legs' weights added together plus the pair's own cell on that
row -- the adjustment on top. A workbook with no currency columns reads
exactly as it always did, the cell being the whole bump.

## The six panels

The web interface separates the jobs the tool does. The tabs run **Pricing,
Vol marking, Monitor, Exchange traded, Analysis, Market maker** — Monitor sits
behind Vol marking because those two are what a morning starts on.
`screens.SCREENS` is the single declaration of that order; the page's nav and
its panel map are pinned against it by a test, so a build that leaves a screen
out cannot end up showing the rest in a different order.

**Pricing** — a grid where **each column is an option** and each row is a
field. Add columns for as many legs as you like; vol and premium are computed
off the current marks and refresh as you type.

| Input | Accepts |
|---|---|
| Pair | any pair in the book |
| Expiry | a tenor (`1W`, `8d`, `3M`) resolved through spot and delivery on the pair's holiday calendar, or a date in most of the ways one is written (`2026-09-15`, `15Sep26`, `15 Sep 2026`, `September 15, 2026`, `2026/09/15`, `9/15/2026`, `20260915`). Whatever is typed, the box comes back holding the one standard date |
| Strike | a number, `ATM`, or a delta — `25d`, `10dp`, `-25d`. A bare `25d` takes its wing from the option type. Once it has been solved on the marks the box holds that absolute strike and says what it was asked as; type the request in again to solve it afresh |
| Cut | `TK` / `NY` / `LDN` / `HK` |
| Type | `C`, `P`, or `Auto` (call if the strike is above the forward). `Auto` comes back as the `C` or `P` it resolved to |
| Spot, Swap, Forward | three boxes holding one identity, `forward = spot + swap / pip`. All three are filled from the feed at this leg's own expiry and shown greyed while they are still the feed's; type in any of them and the third follows. The **outright** is what is priced. Empty a box to hand it back to the feed |
| Notional, Side | notional in millions of base (payout, for touch products); buy or sell flips the signed amounts *and* which way an overhedge buffer is applied |
| Product | `vanilla`, `digital`, `one_touch`, `no_touch` |
| Barrier | level for touch products; the vol is read at the barrier |
| Digital ramp % | call-spread width replicating the digital, as % of strike |
| Overhedge, Buffer % | barrier shift: `extend`, `bend_front`, `bend_back` |

**The Results rows repeat none of the inputs.** What the pricer resolves is
written back into the box it was asked in and is then what is priced: the
expiry as the one standard date, `ATM` or `25d` as the absolute strike it
solved to on the marks, `Auto` as `C` or `P`. Showing them again underneath
would be one number in two places on one screen — and for spot and the
forward it is worse than that, because the box is the input and the row
beneath it reads like an answer. So a delta strike does not quietly re-solve
under a mark that has moved, exactly as a tenor does not re-read on a later
morning; the box says what it was asked as, and typing the request in again
solves it afresh. Moving the leg to another pair puts the request back rather
than carrying an absolute strike from the old pair onto the new one.

**A row appears only where the product uses it.** A vanilla has no barrier, a
touch has no strike and no ramp, and only a touch takes an overhedge — the
model reads none of those, so the grid does not offer them. A leg whose
product does not use a row that another leg does keeps its place in the
column and shows a dot, because a gap that vanished would make two legs look
like the same instrument. The same rule applies to the result rows: an MC
standard error belongs to a simulated barrier and a smile delta to a vanilla.
Every column is the same fixed width whatever is in it, so a long premium
cannot widen the leg beside it.

**The three market boxes are resolved by the server**, because that is where
both authorities are: the expiry goes through the pair's own holiday calendar,
and the level is `Book.market_level`, the one place a level is read — so a
cross the file quotes only through its legs (EURJPY off EURUSD and USDJPY) is
filled from them, by the same triangle that prices it. Changing the pair or
the expiry refills the level boxes, because the swap points are
interpolated to the expiry and a forward left behind is simply the wrong one.
A box you have typed into is yours and is not refilled. While every box is
still the feed's, the outright is the feed's **own** published outright and
not the two rounded boxes above it added up: the three carry different
precisions, and on a cross the file builds from its legs the sum lands a
digit off what was published.

**Refresh spot** re-reads the feed file from disk and re-prices, so every box
still holding the feed's number picks up whatever has just been published.
**Fill legs** does the same and then writes the feed over the levels you typed
as well — which is what a leg with a hand-marked spot that has gone stale
needs. A leg the feed has no pair for, or whose expiry will not
resolve, is reported and left exactly as it was. **Watch file** polls only for
when the file was last written and lights the status pill; nothing reloads
underneath a price being read.

### Exotics and overhedges

**Digitals** are priced as the call spread that actually replicates them, each
leg on its own smile vol. The benchmark is the smile-consistent fair value
`-dC/dK = N(d2) - vega * dsigma/dK`, not `N(d2)` at a single vol — on a 3M
USDJPY 155 digital the skew term is 12% of the price, so using the flat-vol
number as the baseline would make a narrow ramp look *cheaper* than fair. The
ramp is the overhedge knob: zero is the unhedgeable limit, wider is what you
can run, and it converges back to fair value at first order in the width.

**One-touch / no-touch** are continuously monitored and pay at expiry. A flat
barrier has a closed form (reflection principle with drift). The buffer shifts
the barrier — *toward* spot when you are the seller, away when you are the
buyer:

* `extend` — parallel shift for the whole life.
* `bend_front` — full shift at inception, tapering to nothing at expiry.
* `bend_back` — nothing at inception, full shift by expiry.

A bent barrier is time-dependent and has **no closed form**, so it is priced by
Monte Carlo with a Brownian-bridge touch correction (unbiased for continuous
monitoring) and reports its own standard error. The simulator is validated
against the closed form on flat barriers.

Both price *and* risk move with the buffer — a 0.5% `extend` on a 3M USDJPY
one-touch at 158 takes the price from 0.1008 to 0.1384 and the delta from 577%
to 751% of payout.

### Spot / forward feed

```bash
python3 -m volkit serve --feed files/market_feed.csv
```

A `pair,tenor,value` CSV: `tenor` is `SPOT` for the spot rate, otherwise the
forward points at that pillar. Swap points are interpolated linearly in time
between pillars, scaled to zero at the very front, and held flat beyond the
last pillar with an `extrapolated` flag rather than trended off the end of the
curve. The pip divisor follows the term currency (100 for JPY, 10000 otherwise).

**A cross the file does not quote is built from its legs.** A feed carrying
EURUSD and USDJPY is carrying EURJPY, so `Book.market_level` — the one lookup
behind every level on every screen — composes the two outrights rather than
refusing the pair by name. It is the triangle and not a model: EURJPY is
EURUSD × USDJPY, EURGBP is EURUSD ÷ GBPUSD, and the points that come back are
the cross's own, in the cross's own pips, never the legs' points added. Every
screen that shows a derived level says which legs it came from, because a
level that came out of an identity and one that was published must not read
the same. Half a triangle is still a refusal: GBPNZD with no NZDUSD in the
file has no forward, and says so.

Outputs per column: forward, resolved strike, vol, ATM vol, premium (term
currency per unit of base, and % of base), delta, smile delta, vega, and the
notional-scaled premium / vega / delta. Totals are bucketed by currency pair.
A leg that fails shows its error in its own column — the rest still price.

Premiums are **undiscounted forward values**; the model carries no rate curve
(neither did the original). Columns persist in the browser between sessions.

### Auto-loading the market feed

```bash
python3 -m volkit serve --auto-reload        # every 5s
python3 -m volkit serve --auto-reload 30     # or every 30
```

or the **auto-load** checkbox on the pricing toolbar, which turns the same
thing on and off while the tool is running.

**Only the feed is watched.** The workbook is the book of record and this
session's marks are not in it -- nothing writes to the workbook -- so
re-reading it is exactly what discards a morning's marking, and it stays on
its own button. The historical workbook is a record of what happened, not a
market. The feed is a publication, republished all morning, and a price quoted
off a stale spot is simply wrong.

Off unless asked for. Every re-read is recorded, counted and shown on the
page: the browser polls one integer (`/api/auto`) and rebuilds the screens
only when something actually happened.

The feed is read once its write time has stopped moving -- the same stamp on
two passes -- rather than after a fixed number of seconds. A file is written
in pieces and half a feed is not a market, and a file stamped by another
machine can be seconds ahead of this one's clock, which a wall-clock settle
would hold back for as long as the two disagreed. **Check the feed now** does
not wait: somebody who pressed it knows they have saved.

None of the three files is held open while it is read. `pd.ExcelFile(path)`
keeps the file open for as long as the reader lives and openpyxl's workbook is
full of reference cycles, so the handle used to outlive the call -- and on
Windows that stopped Excel saving the very sheet the tool had just read.
`marketdata.open_workbook` copies the bytes and hands pandas a buffer; the
feed CSV is read whole and closed the same way.

**Vol marking** — the surface itself, editable at four levels, all of which
feed the pricing panel immediately:

1. **Curve parameters** — the backbone itself: initial vol, long-term vol,
   mean reversion, short add-on and decay, rate vol and correlation. For a
   cross the same card marks the *correlation* term structure (initial, final,
   decay) and shows which legs and triangle signs it is built from. A rejected
   value leaves the curve untouched and says why.
2. **Events** — a table of dated volatility bumps. **Auto-load** pulls the
   scheduled economic releases for the pair's currencies over a chosen horizon;
   every row is then editable, and rows can be added or deleted by hand. A row
   holds each **leg's weight** (the event is weighted per currency) and the
   pair's **adjustment**; the bump the curve is calibrated to is the two
   weights added plus the adjustment, shown beside them and never typed over.
   Applying re-solves each event height so that bump is reproduced exactly. The
   *vol day* column shows which volatility day each bump actually prices into —
   the day rolls at 14:00 UTC, so a late release lands on the next one and is
   flagged.
3. **ATM term structure** — per-tenor overwrites of the marked vol.
4. **Smile parameters** — per-tenor `slog25`, `slog10`, `rho25`, `rho10`, and
   the **anchor** switch, which lives here rather than over the ATM table
   because what it anchors is the smile.

The **smile chart** puts its strikes in the levels a trader would name
whenever the feed covers the pair: the axis, the point table and the density
are scaled by the outright forward at that expiry, with the spot and the
forward printed beside them. Without a feed there is no honest level to name,
so it stays in `K/F` and the column heading says so. The smile itself is
fitted in moneyness either way — this is a scale on the way out and moves no
number. `volkit smile` prints the same two ways for the same reason.

An **implied vs quoted** table reads the risk reversals and butterflies back
off the fitted smile and shows them against the quotes that went in. Because
the smile is fitted per tenor and then given a parameter term structure of its
own, the two differ — on the sample workbook by up to 0.07 vol points at 2Y —
until **anchor** is on, which collapses the differences to under 0.003. That
table is the marking check the legacy tool had no way to display.

Type into a shaded cell and press Enter; clear it to revert to the model.
Overwrites are held in memory and are lost on reload — but **the pair, cut,
interpolation and chart expiry a reload finds selected are the ones it leaves
selected**. A reload that quietly moved the screen back to the first pair in
the book would take a marker off the pair they were marking, which is the
same silent change the feed pill exists to prevent.

### Managed and pegged currencies

For a pair whose spot is held inside a defended band — USDHKD under the HKMA's
Convertibility Undertakings is the clear case — a lognormal smile gets two
things structurally wrong. It puts mass everywhere, so it pays real premium for
a strike the peg forbids; and the realised distribution is not merely bounded
but *U-shaped*, because intervention at the edges is what keeps the rate near
them.

The model is a **regime mixture, not a bounded distribution**. With probability
`exp(-λT)` the peg holds and the rate is Beta-distributed on the band, which is
U-shaped whenever `a, b < 1`; otherwise it breaks, two-sided and asymmetric,
with marked jump sizes and post-break volatilities. Out-of-band probability is
therefore an **output**, and a real one — the peg breaking is a risk to be
marked, not one to be assumed away.

Bands are policy, so they are data: `files/bands.csv`, one `pair,lower,upper`
row each, loaded automatically and attached to the surface. A **Managed band**
card appears on the marking screen for a pair that has one, and nothing at all
for a pair that does not. What is marked on it:

| Setting | Means |
|---|---|
| Treatment | `flag strikes outside it` (default), `ignore the band`, or `price the regime mixture` |
| Hazard %/yr | the break intensity `λ`. A rate, not a per-tenor probability, so one number is consistent at every expiry |
| Weak side %, jumps, post-break vols | how a break goes, and where it lands |
| Lower / upper edge | override the policy band for this session |
| Band weight % | 100 is the pure mixture; anything between blends it with the lognormal smile |
| Wing delta | which wing the model is reported against |

Choosing **BAND** as the interpolation method prices the mixture instead of the
lognormal smile — anywhere the method is chosen: the pricing grid, the marking
screen, the analysis panel, `volkit vol --method BAND`. A band weight strictly
between 0 and 100 averages two implied volatilities, which is a marking
convenience and not a model; it says so.

**The method is only offered where it can work.** A free floater has no band
to place and `VolSurface.band_for_slice` refuses by name, so `BAND` is left
out of the interpolation list for a pair that has none — and out of it
entirely for a book with no pegged pair at all, which is every book that does
not carry one of the pairs in `bands.csv`. A leg or a panel saved against a
pegged pair and then pointed at a free floater has its method put back to a
legal one rather than left holding a choice the screen no longer shows. The
server's refusal stays exactly where it was; this only stops the screen
putting a choice in front of somebody that it will not honour.

A **strike** outside the band is flagged wherever it is priced, not only a
barrier. The check reads the level the payout actually depends on — the
barrier for a touch, the strike for a vanilla or a digital — so the product a
band matters most for is no longer the one product nothing was said about.

Two refusals are deliberate. **Break risk is a marked input, never inferred
from a butterfly alone**: a wider Beta body and a higher hazard both raise the
at-the-money, so the at-the-money cannot separate them. And a band is an
*absolute* price range while the surface works in strike over forward, so
placing one needs an outright forward from the feed; without one the BAND
method refuses and names the feed rather than guessing a level.

What the wings can say about the break regime is a **proposal**, and the
calibration is two stages kept apart because they are identified by different
quotes. Stage A is exact: the forward pins where the peg-intact Beta sits and
the at-the-money sets how concentrated it is, one monotone solve. Stage B
reads the break regime off the wings — the 10 and 25 delta risk reversals and
butterflies — by least squares, holding what it was not asked to free (the
jump sizes by default: where a peg would go is a policy view, how likely it is
to go is what the market prices). It runs per tenor
(`banded.calibrate_band_wings`) or over the whole curve at once
(`calibrate_band_term_structure`): the hazard and the share of breaks are
properties of the regime, not of a tenor, and the body is bounded by the band
while break variance grows with time, so the term structure separates the two
in a way no single expiry can. Every quote reports its residual, the Jacobian
at the answer says which parameters these quotes informed and names a
near-degenerate pair, and each tenor's own implied hazard is printed beside the
shared one — a flat row says the regime is consistent across the curve, a
sloping one points at the jump sizes or the band. `--solve-hazard` is the
one-parameter, one-instrument case of the same machinery (the hazard against
the strangle premium at one delta, bracketed) and gives the number it always
gave. Nothing is marked by any of it: the card's **Fit from the wings** fills
the boxes and Apply is the same Apply.

```bash
volkit band USDHKD --feed market_feed.csv --hazard 3 --weak-jump 8
volkit band USDHKD --feed market_feed.csv --fit                   # hazard and weak_share
volkit band USDHKD --feed market_feed.csv --fit hazard,weak_share,weak_vol
volkit vol USDHKD 2026-03-16 --strike 7.90 --method BAND --feed market_feed.csv
```

A useful finding from the sample marks: the band alone gives USDHKD a
*negative* risk reversal against a quoted positive one. Most of the quoted skew
is peg-break premium.

### Analysis

A fourth panel asks five questions of the whole tenor grid at once. Each
section is built independently and reports its own reason for being empty, so a
missing forward feed does not take the realized statistics down with it.

**Relative value.** The first card, and the summary of the rest: a grid
of expiry against strike — 10 delta put, 25 delta put, ATM, 25 delta call, 10
delta call — with one score per cell, positive when the mark is rich and that
is the side to sell. Nothing in it is a new model. Every signal is one of the
comparisons below, read at a strike instead of at the at-the-money:

| Signal | What it compares | Adds up? |
|---|---|---|
| `level` | the marked ATM against the realized volatility | yes |
| `shape` | the marked smile's shape at this strike against the shape a SABR smile built from the *measured* `(ρ, ν)` would show | yes |
| `carry` | minus the roll and the forward carry, as the volatility they are worth | yes |
| `history` | where this cell's own volatility sits in its own recent history | no |
| `triangle` | for a cross: the cell's mark against what its two legs imply | no |

The first three are the fair-value break-even extended from the at-the-money to
a strike, so they **add**: `level + shape + carry` is exactly `implied(K) −
fair(K)`, and at the ATM column it is exactly the fair-value table's own
`richness` (a test pins that to 1e-12). The other two answer different
questions and are deliberately not summed with them.

Combining them needs one scale, and the scale is **the cell's own historical
standard deviation** — half a volatility point is a great deal on a one-year
ATM and nothing on a one-week 10 delta wing, and only the history knows which.
So each signal in volatility points becomes a z-score by dividing by that, and
the composite is the weighted mean of whichever z-scores are available,
**renormalised over them**: a missing signal is dropped, never counted as a
zero, which would drag every score toward the middle. Every cell reports which
signals it used and a **confidence** — the share of the declared weight the
score rests on — so a cell scored on one signal and a cell scored on four are
not read alike.

The same rule catches a zero that is *not* missing. At the at-the-money column
`shape` is zero by statement — the at-the-money **is** the level, so there is
no shape there to be rich or cheap in — and a statement is not a measurement:
it is shown with its value and its reason and kept out of the average, because
averaging it in pulled every at-the-money cell a fifth of the way to the middle
through the one signal that was present. An ATM cell is therefore scored on
less of the declared weight than the wings beside it, and its confidence says
so.

Two windows, and neither is the realized lookback. The lookback is matched to
each tenor because a one-month implied volatility forecasts one month. How much
a volatility *moves* — the **scale** every score is divided by — is a slower
measurement and gets its own window (`HISTORY_DAYS`, a year); run off one
window, a month of a smooth one-month series read an ordinary half point of
richness as thirty standard deviations. The `(ρ, ν)` the `shape` signal's
comparison smile is built from get theirs (`history.DYNAMICS_DAYS`), for the
same reason and never shorter than the lookback: they are properties of the
process rather than forecasts over a horizon, and they need *more* paired
observations than a realized volatility needs returns. Measured on the
lookback, `shape` was blank at every short tenor, and blank at every tenor at
once whenever the lookback was set under about a month — which reads as a
signal that does not work rather than as a window that is too short. The scale
window keeps its own constant so that changing it moves the denominator only:
the volatility-point column is not a function of how the z-scores are scaled,
and a test pins that.

Three things the grid says out loud rather than leaving to be inferred. The
row carries **what the realized number is made of** — the spot leg, the
forward leg, the swap-point volatility and its correlation with spot — and the
one number that answers "does the carry support the volatility" is
`forward_vol_ratio`, realized vol of the forward over realized vol of spot,
*never* the level of the swap points: a large carry says nothing on its own
about whether the forward is more volatile than spot. Every row also carries
its **regime**, `z = |ln(F/S)| / (σ√T)`, which puts the forward's own drift
and the option's diffusion in the same units; past 0.8 the position is mostly
a carry trade in an option's clothes and the row, the carry signal and the CLI
all say so. The same z against *realized* volatility is the managed-float
evidence, read at a one-year reference because whether a pair is managed is a
property of the pair. And `level` is marked as a **row** signal: it is the
same number in all five cells by construction, so it is printed once in its
own column rather than five times where it would read as five independent
confirmations.

What none of that does is move a weight. `regime_z` says which side of the
line a tenor is on; the weight stays where the desk put it, because a score
that quietly reweighted itself would be a different statistic on every row
with nothing on the screen to say so.

The managed-float test takes **two** conditions, and the one-condition version
is wrong: read as `|c|/σ` alone, USDJPY on a five point rate differential and
ten volatility points scores 0.53, right beside USDCNH's 0.50. The second
condition is a low realized volatility in absolute terms — the suppressed
diffusion itself rather than a consequence of it. It is a heuristic on
measured numbers and not an authority: a hard defended band is a policy fact
and is marked in `bands.csv`.

No historical sheet means no scale, and then there is no score — the
volatility-point columns are still reported, because they are still true.
Inventing a scale would be inventing the answer. A wing the sheet does not
quote borrows the at-the-money's scale and says so. A triangle difference
inside its own noise floor is shown and not scored, which is that section's own
rule. The weights are a marking judgement rather than a result: they are
declared once in `relvalue.WEIGHTS`, sent to the page so a box cannot offer a
signal the scorer never heard of, and editable on the panel or with
`--weight NAME=VALUE`; one that is not a signal, or is not a number, is refused
rather than ignored.

```
python3 -m volkit analysis EURJPY --history vol_history.xlsx --horizon 7 \
    --relative-value --history-days 250 --weight carry=0.4
```

**Carry and rolldown.** Every tenor is revalued after a horizon at a *fixed
absolute strike* — the option you own keeps its strike while both the maturity
and the forward move under it. The roll splits into the slide along the term
structure (same moneyness, shorter maturity) and the slide across the smile
(same maturity, forward moved), so the forward curve's contribution is
separable rather than buried in one number. The target can be the ATM, either
25 or 10 delta wing, or a risk reversal or butterfly; `roll / atm` is the roll
per year as a fraction of the ATM level.

The forward curve pays **twice**, and the two are reported separately because
they are different risks:

| Through | Column | What it is |
|---|---|---|
| the mark | `smile` | the forward slides out from under a fixed strike, so the option's moneyness — and the volatility it is marked at — changes. In volatility points. |
| the price | `carry` | the option is worth `V(F, K, σ, τ)` and `F` itself has rolled from `forward` to `rolled`. In basis points of the forward, and `in vols` divided by the position's own vega so it can be read beside the roll. |

**The delta is the smile delta.** This whole table is a fixed strike sliding
under a moving forward, so the volatility that strike is marked at moves with
it and `dV/dF` carries `vega × dσ/dF` along; a Black-Scholes delta holds that
volatility still and is short of the position by the skew. Three columns:
`Δ smile` (what you are running), `Δ BS` (the Black-Scholes reading) and
`Skew` (the difference, which is the whole of what the smile contributes). On
the sample marks a USDJPY 25 delta put runs at 0.168 rather than 0.239, and
the at-the-money straddle is delta neutral in the Black-Scholes column *only*
— it is long vega, the volatility moves with the forward, and that is also
why the at-the-money row shows any carry. The check is that
`Δ BS × ΔF` reproduces `carry` and `Δ smile × ΔF` reproduces
`carry + vega × smile`, the whole of the forward's effect, both to within a
percent over a one-day horizon.

Both columns are `dV/dF` in the term currency and deliberately *not* the
quoted delta: a premium-adjusted delta is a hedge ratio in the other currency,
and multiplying it by a move in the forward does not give money.

The second one depends on how the delta is hedged, and the convention is worth
stating because it is the whole of the number. Hedged in the **outright forward
to the option's own expiry**, the hedge rolls down the curve exactly as the
option does and the two cancel. Hedged in **spot**, which is what an FX options
desk actually does, nothing rolls on the hedge side and the position keeps it.
So `carry` is the carry of a spot-hedged book, and the same number is the cost
of *not* hedging in the forward. It is a full revaluation, not `Δ·(F₂−F₁)`, so
the gamma over the move is in it; `Δ` is shown beside it as the first-order
reading, and `fwd/yr` is the annualised proportional roll-down of the forward —
the rate differential the swap points are quoting.

The at-the-money row shows `Δ = 0` and almost no carry, and that is correct
rather than missing: the at-the-money is a **delta-neutral straddle**, so the
forward moving under it earns only the gamma over the move. A 25 delta call
shows a quarter of the forward's roll-down. Without a forward feed there is no
curve to roll down and every carry figure is left unavailable rather than
reported as an exact zero.

**Fair value.** Hold the `T` at-the-money option for the horizon `h` and delta
hedge it. The mark slides by `roll`, worth `vega(T−h)·roll`; the gamma against
theta earns roughly `h/T` of the option's life at `σ_realized − σ_implied`.
Setting the two to cancel:

```
fair = realized + (T/h) · [ roll · vega(T−h) + carry ] / vega(T)
richness = implied − fair
```

— the `roll value` and `carry value` columns. The multiplier uses the actual
vegas rather than the `sqrt(T)` proxy, because the strike is not the same
distance from the two forwards once the forward curve has any slope. The roll
here is always the **at-the-money** roll, taken from a table the function
builds itself — feeding it a risk-reversal roll and an at-the-money implied
would mix two different positions into one break-even.

The forward curve reaches the break-even twice, and the two columns are kept
apart. Through the *mark* it is `of which fwd`, the part of the roll value the
smile slide caused, and it is first order in the shape of the curve. Through
the *price* it is `carry value`, and for a delta-neutral straddle it is second
order — computed rather than assumed to be zero, because it is not zero for a
steep curve or a long horizon, and because a number reported as an exact zero
should have been measured.

**Realized against implied.** Load a historical workbook — one sheet per pair,
one row per date, columns for spot, forwards and the quoted surface. Columns
are matched by reading their headers, not by position, and the volatility unit
is decided **once per sheet from the at-the-money column**: a 25 delta risk
reversal of −0.89 vol points is below 1 in magnitude, so sniffing each column
on its own reads it as a decimal and returns it a hundred times too large.

Realized volatility is measured on the **forward** to each tenor wherever the
sheet quotes the swap points, and it says which it used in the `on` column. A
quoted volatility is the volatility of the forward the option is struck
against, not of spot; for most of G10 the two are within a few hundredths of a
point, but they are not the same number anywhere the rate differential is large
or unstable. Writing the outright as `F = S·exp(c·τ)`,

```
dlog F = dlog S + τ·dc − c·dt
```

The first two terms are what moved — spot, and the swap points *moving*. The
third is the points *decaying* by one day of carry, which is a known slide and
not a risk; leaving it in the sum of squares would book the carry itself as
volatility. It is removed and reported as the carry rate instead. The
swap-point part alone is the `pts` column and the spot-only figure stays beside
it as `spot`, so the difference is visible rather than assumed. A tenor the
sheet does not quote has its carry *interpolated* between the pillars it does,
rather than dropping that row back to spot — falling back on the misses put two
different measurements in one column and grew steps in the term structure at
whichever tenors the sheet happened to quote. `--realized-basis spot` restores
the older reading.

Realized volatility is annualised by the same **volatility time** the model
integrates variance with — weekends near zero, holidays reduced, the intraday
profile not flat. A calendar year holds about 0.78 years of it, so a calendar
or 252-day annualisation lands roughly a tenth low; both are shown beside the
weighted one because they answer a different question. Skew and kurtosis need
the same care in the other direction: the realized figures are daily and the
implied ones are for the whole return to expiry, so the daily numbers are
projected onto each tenor (`skew/√n`, `kurtosis/n`) before being compared, and
both are shown with standard errors.

The implied side comes from the marked smile's own density, by
Breeden-Litzenberger, not from a risk-reversal proxy.

**Wings as a SABR shape.** The moments above cannot answer the same question
about the risk reversal and the butterfly: a quoted spread is not a moment, and
a realized third moment is not a risk reversal. What both sides *do* share is
the pair of numbers a SABR smile is built from — the spot/volatility
correlation `ρ` a risk reversal is paid for, and the volatility of volatility
`ν` a butterfly is paid for. So the panel offers to say both in those terms:

* **marked** — the `(ρ, ν)` a SABR smile would need to show the quoted ATM,
  risk reversal and *smile* butterfly at the chosen delta
  (`sabr.fit_smile_shape`). Not a calibration of the book — the book's smile is
  SVI — but a reading of it, and it reports its own residual so a smile SABR
  cannot reach says so instead of quietly returning the nearest thing.
* **measured** — the same two taken from the history (`history.vol_dynamics`):
  under SABR with `β = 1` the at-the-money volatility *is* the state variable,
  so `ρ` is the correlation of daily spot returns with daily log changes in the
  quoted at-the-money, and `ν` is the annualised size of those changes — on the
  same volatility time as everything else here, so the two are comparable.
  Where the sheet quotes no at-the-money for a tenor it uses the nearest one it
  does, by name; where it quotes none at all it falls back to a rolling
  realized volatility and warns that an average moves less than the thing it
  averages, so that `ν` is a floor. The **window** it is measured over is not
  the realized lookback beside it (`history.DYNAMICS_DAYS`, 250 days, and never
  shorter than the lookback), and each row names it. A `ρ` and a `ν` are
  properties of the process rather than forecasts over a horizon, and they need
  more paired observations than a realized volatility needs returns: read off
  the lookback, the whole measured half of the table — both **diff** columns
  with it — was blank at every tenor whenever the lookback was set under about
  a month, and blank at the short tenors always. The marked half needs no
  history at all, so a tenor whose realized window is too short to measure
  anything still shows its own `(ρ, ν)` and the difference.

SABR has no mean reversion, so `ν` rises at short tenors on both sides and must
not be blended across them; `ν√t` is the scale-free number that actually sets
the shape and is shown beside it. Off by default (`--sabr`, or the *wings as
ρ / ν* box), because it is a fit per tenor.

**Cross triangle.** For a cross, the at-the-money row has an exact answer and
gets one: the variance triangle the book is built on. The risk reversal and
butterfly have no exact answer from two marginals and a correlation, so each
leg's whole marked density is read off its smile, the two are tied together
with a Gaussian copula at the marked correlation, and the cross's smile is
integrated out of the result on a deterministic tensor grid — nothing fitted,
nothing simulated.

That assumes a dependence structure the market does not quote, and it ignores
the change of measure between the legs' domestic currencies. Both are stated
rather than hidden, and both are bounded from below by the **noise floor**: the
same machinery run on each leg alone, where it must reproduce the input
exactly. A difference smaller than the noise is shown as `~0`.

The two at-the-money triangles do not agree and should not. The variance
triangle uses each leg's at-the-money volatility; the distribution triangle
uses each leg's whole density, whose variance is larger by the convexity of its
own smile. That gap is reported by name so it is not read as a marking error.

**Vega split.** One more column differentiates the same variance triangle
rather than integrating it, so unlike the risk reversal and the butterfly it is
exact. With `x = ca·cb·ρ` the signed correlation the triangle carries,

```
σ_c² = σ_a² + σ_b² + 2·x·σ_a·σ_b
∂σ_c/∂σ_a = (σ_a + x·σ_b) / σ_c        ∂σ_c/∂σ_b = (σ_b + x·σ_a) / σ_c
∂σ_c/∂ρ   = ca·cb·σ_a·σ_b / σ_c
```

so one unit of at-the-money vega on the cross behaves like that many units in
each leg — the two hedges. They are **not shares and do not add to one**;
what is exact is Euler's identity, `σ_a·∂σ_c/∂σ_a + σ_b·∂σ_c/∂σ_b = σ_c`,
because the triangle is homogeneous of degree one in the leg volatilities. The
correlation is homogeneous of degree zero, appears nowhere in that identity,
and is therefore reported on its own: the **per ρ** column is what a whole unit
of correlation is worth in cross volatility points, and no amount of leg vega
hedges it.

**Curve comparison.** This moved to the Monitor panel, below — it answers
"what has changed", which is that panel's question. `volkit analysis --compare`
is kept only to say where it went. It puts any number of curves side by side
and differences them against whichever one is marked *base*. A curve is one of
four things:

| Source | What it is |
|---|---|
| `surface` | the fitted surface, at the cut and interpolation chosen above |
| `marks` | the workbook's own quotes: the marked ATM curve, the quoted risk reversals and market strangles |
| `history` | one dated row of the historical workbook |
| `paste` | a curve typed or pasted in — a broker run, another system, last night's close |

Several `history` rows is the same curve on different days, which is what the
panel is mostly for. A date takes `latest`, a date, or an offset back from the
last row (`-30d`, `-3m`), and the row used is the last one **on or before**
what was asked for: a historical workbook has no rows at the weekend, and
snapping forward would compare a Friday mark against the following Monday's.
Each curve says which day it actually landed on.

A pasted curve is `tenor atm [rr25 bf25 rr10 bf10]`, one line per tenor,
everything after the at-the-money optional. Its unit is decided **once, from
the at-the-money column**, and a paste whose levels straddle 1.0 is refused
rather than guessed — the same rule the historical sheet and the broker run
follow, and for the same reason.

Two things the panel is careful about. A tenor a source does not quote is
**blank, not absent**, so a short curve cannot read as agreement; and a curve
that could not be built at all keeps its place in the table and carries the
reason. One caution it repeats in words: a historical sheet's butterfly column
is whatever that desk quoted, while the book's is a market strangle.

```bash
volkit analysis EURJPY --history vol_history.xlsx --horizon 7 --target rr25
volkit analysis USDJPY --history vol_history.xlsx --sabr --sabr-delta 0.25
volkit analysis USDTRY --history vol_history.xlsx --realized-basis forward
volkit analysis EURJPY --history vol_history.xlsx --relative-value --horizon 7
volkit monitor EURUSD --history vol_history.xlsx \
    --compare surface --compare marks --compare history:-30d --field rr25
python3 files/make_history_sample.py     # regenerate the example workbook
```

`files/history_sample.xlsx` is a synthetic example of the layout, deliberately
inconsistent between sheets to show what the header reader tolerates. It is
never loaded automatically — made-up realized volatility appearing on the
screen without anyone asking for it is the one thing this panel must not do.

### Market maker

The other panels answer *what is this worth*. This one answers *what do I
show*, in three stages that report separately so one that cannot run leaves
the others alone — and behind **two buttons**, because fitting and quoting are
two jobs asked at two different moments.

**Fit** reads the market box, moves the curve and the wings, and puts a price
on nothing. **Quote** reads a second box, where you write what you are being
*asked* for, and makes a two-way in each line of it — and fits nothing, which
is why it answers instantly and can be pressed as often as the phone rings. A
request does not arrive with a broker run attached to it, and tying the two
together meant you could only price one by re-fitting to a market that had
nothing to do with it.

They meet at the marks. With **quote on the fit** ticked, the parameters the
last fit arrived at travel back with the request and go on the surface for the
length of that one call; untick it, or fit nothing, and the price stands on the
marks as they are. The sheet says which of the two it was every time. Nothing
is left on the book either way — that is **keep the marks**, on the fit, and it
is a separate decision. (The server keeps no screen state, here as everywhere:
the browser holds the fit's answer and posts it whole, which is what makes
`volkit mm --request` reproduce a screen exactly.)

**1. Fit the at-the-money curve to a target.** The target is one of:

| Source | What it uses |
|---|---|
| `overwrites` | the tenors you pinned on the Vol marking tab — mark the levels you want, then fit a smooth curve through them |
| `quotes` | the mid of the at-the-money quotes in the pasted market |
| `paste` | a `tenor level` list you type in |
| `current` | the curve as it stands, as a no-op check on the fit itself |

It is a cold fit: the level parameters come off the targets, the shape
parameters are swept before anything is polished, and a parameter you pin is
never moved. A fit cannot have more free parameters than the target has
points. For a **cross** the level belongs to its legs, so what gets fitted is
the correlation term structure instead.

**2. Fine tune the wings to the quoted market.** Paste a broker run in the
shorthand it arrives in:

```
1M ATM 8.20/8.60 in 100mm vega
3M 25d RR 0.35/0.55 eur call over
2M 25d fly 0.20/0.28
6M 1.1000 call 7.90/8.40
1M/3M ATM spread 0.30/0.55
1Y 10d strangle 0.55/0.70
```

or as the columns a chat window or a spreadsheet gives you — `expiry, strike,
bid/offer`, with a timestamp in front of it if there is one:

```
09:15, 1M, ATM,    8.20/8.60
09:15, 3M, 1.0900, 8.10/8.50
09:20, 2M, 25d,    8.00/8.40
09:41, 1M, ATM,    8.25/8.65
```

One parser reads both, because a run mixes them. The strike column takes the
pricing screen's own vocabulary: `ATM`, an absolute strike, or a delta. A bare
`25d` names two strikes, one on each wing, so it takes the call and says so on
the row; `25dp` and `-25d` are the put. An *absolute* strike needs no side at
all — the volatility at a strike is one number either way — but it does need a
forward feed, because the surface works in strike over forward.

**A comma is a column boundary and a price never straddles one.** That is the
whole difference between `3M, 7.75, 8.30` — a choice price at the 7.75 strike —
and `3M 7.75 8.30`, which is the two-way at-the-money it has always been.
Thousands separators are stripped first, so `1,000mm` is a size and not a
column.

**A later timestamp wins.** A run is a conversation and the same tenor is
requoted as the market moves; the older quote would otherwise go into the fit
beside the newer and pull it backwards. The rule holds whatever order the lines
were pasted in, so a stale line at the bottom of a run cannot become the live
market. Two quotes that cannot be compared on time fall back to the later
*line*, which is the only ordering an untimed line carries. The quote that lost
is kept and reported with the line that beat it — and it is still evidence when
the knowledge bank learns widths, because one tenor quoted twice is one live
price and two observations of how wide it is shown.

Outrights, risk reversals, butterflies, calendar spreads; tabs or spaces;
percent or decimal; a size and a direction word if you write one. The
objective is a **hinge**: no penalty anywhere inside a quoted bid and offer,
and the distance to the nearer side outside it — the brief is that your mid
falls *inside* the market, not on top of somebody's mid. The four smile
parameters move by an additive shift across the whole curve, so a broker run
changes the level of a wing without flattening its term structure. A shift
that cannot reach a tenor says so in its residual rather than bending the
surface to one quote.

Three conventions are decided once and reported, never guessed per line:

* the **volatility unit** comes from the whole paste's at-the-money and
  outright lines, and a paste that straddles 1.0 is refused — a small risk
  reversal read on its own looks exactly like a decimal at-the-money;
* a **risk reversal's direction** (`EUR call over`, `JPY call over`) is
  resolved against the pair; one written without a direction word is read in
  the book's convention and the paste says so;
* an unqualified **`fly`** means whichever butterfly the selector says — the
  market strangle by default, because that is what the workbook marks.

**3. Put a price round the mid.** This is the **Quote** button, and it reads
the request box: the same words as the market box with the price left off.

```
1M ATM in 100mm
3M 25d RR
2M 25d fly
6M 1.1000 call
1M/3M ATM spread
3M 25d RR jpy call over
```

A number that reads as a market is **refused with the line** rather than taken
as a strike — a broker run pasted into the wrong box would otherwise be quoted
at levels nobody asked about. A risk reversal asked for as `JPY call over` is
answered in *that* convention, sign and sides both, and the row says so.

The width comes from the pair's knowledge bank; the mid is shaded by
fair-value richness and by the vega already on your book, both capped as a
fraction of the width so an axe can lean a price inside the market but never
walk it out of one. Both leans point the same way: a rich market and a long
position are both reasons to want to sell, and you attract a seller's trade by
shading down. Neither is applied to a risk reversal or a butterfly — a
break-even against realized volatility and a vega position are statements about
the *level*, and those rows say so.

A request that names something the market box also quoted carries **their
market** beside our price, and the crossing verdict with it, so *inside their
market* survives the split. One nothing quoted is priced just the same, which
is the whole reason for asking separately.

Nothing here touches the workbook. The fit reports and then puts the marks
back, and so does a quote made on them; tick **keep the marks** to leave the
fit on the loaded book, in memory only.

```bash
volkit mm EURUSD --file run.txt                    # fit, and report
volkit mm EURUSD --file run.txt --request ask.txt  # fit, then quote off it
volkit mm EURUSD --request ask.txt --target-source none   # quote off the marks as they are
```

### The knowledge bank

Per pair, in `mm_knowledge.json` beside the workbook. Four kinds of entry:

| Kind | Effect |
|---|---|
| `spread` | sets the bid-offer width, in volatility points |
| `floor` | a minimum width; every matching floor applies and the widest wins |
| `shift` | moves the mid by a signed offset |
| `note` | prose, shown beside the quote and **never** applied |

Conditions are all optional — instrument, exact tenor, a day range, a size
ceiling and its basis, a delta — and the **narrowest matching rule wins**. Every
quoted row names the rule that set its width and the rule that moved its mid,
and lists the rules that matched but lost.

There is no built-in default width. A quote no rule matches gets no bid and no
offer and says so; a visible fallback on the panel is the only alternative, and
the row reports which it was. **Learn widths from this paste** proposes a
ladder measured from the widths the market actually showed, with the evidence
attached — proposing and saving are two steps on purpose.

### Monitor

The panel a desk opens first, and then leaves open: **what has moved**. A
*panel* on it is one pair and two points in time — the five quoted numbers as
they stand now, the same five as they stood then, and the change between them,
tenor by tenor. Add as many as you want; they are remembered between sessions
and laid out side by side.

Either end is any source the curve comparison understands except a paste:

| End | Typical use |
|---|---|
| `surface` | the fitted surface as the book has it now |
| `marks` | the workbook's own quotes — against `surface`, the fit residual |
| `history` | one dated row of the historical workbook |

so a panel is "the surface against last week's close" (the default), "the
surface against the quotes it was fitted to", or two dated rows against each
other. Dates take `latest`, a date, or an offset back from the last row
(`-1w`, `-30d`, `-3m`), and the row used is the last one **on or before** what
was asked for. Each end reports the day it landed on, and two dated ends that
land on the *same* row say so — a column of zeros otherwise reads as a quiet
market rather than as a comparison that never happened.

An end that cannot be built does not empty the panel: the levels that could be
read stay, and the panel carries the reason it has no change. A tenor one end
does not quote is a blank change, not a missing row. **Highlight** picks which
of the five gets the levels column beside the changes; the changes themselves
are always all five.

The **curve comparison** lives here too, under the panels.

```bash
volkit monitor EURUSD --history vol_history.xlsx
volkit monitor --watch EURUSD --watch USDJPY:history@-1m \
    --watch EURJPY:history@-1w:history@latest --history vol_history.xlsx
volkit monitor EURUSD --compare surface --compare history:-30d --field rr25
```

### Saving a session

The workbook is the book of record and is **never written to**. Everything the
Vol marking and Market maker panels do — a re-marked backbone, a correlation
term structure, an event schedule, a tenor overwrite, a smile parameter
overwrite, the market maker's wing shifts, the anchor switch, a band treatment
— lives on the loaded book, and **Reload workbook** discards all of it.

That is the right default for a tool whose primary file is somebody else's
spreadsheet, and the wrong thing to do to a morning's work at five o'clock. So
a session is saved *beside* the workbook, in the tool's own file, the way the
knowledge bank is. **Save marks** on the Vol marking panel writes it; **Load
marks** puts it back; the Market maker panel writes the same file once its fit
has been kept.

The file is JSON and is meant to be readable by the person whose marks are in
it. Volatility numbers are in **volatility points**, exactly as the panel shows
them; shape parameters, decays, correlations and smile parameters are the raw
numbers those fields carry. The screen and the file share one conversion, so
they cannot come to disagree.

Loading **replaces** rather than merges: overwrites and events are cleared
before the saved ones go on, because merging would double every release that
appears in both the workbook and the file. A pair the workbook does not build
is reported rather than skipped, a pair the file never mentions is left exactly
as the workbook has it and that is reported too, and a pair whose marks will
not go on does not take the rest of the file down with it.

```bash
volkit session marks.json                      # save every mark on the book
volkit session marks.json --pair EURUSD        # one pair only
volkit session marks.json --load               # put them back
volkit session marks.json --show               # what the file holds
volkit --session marks.json vol USDJPY 2024-05-28 --strike 155
volkit serve --session marks.json              # start with them on
```

`--session` is global, so every subcommand prices against the same marks the
screen would be showing.

#### Writing a session into the workbook

When a morning's marks should become the book of record, the session file can
be written into the workbook's own cells:

```bash
volkit session marks.json --to-workbook                 # -> vol_marks_marked.xlsx beside it
volkit session marks.json --to-workbook out.xlsx        # a named copy
volkit session marks.json --to-workbook --in-place      # the workbook itself
volkit session marks.json --to-workbook --pair USDJPY   # one pair only
```

**Write to workbook copy** on the Vol marking panel does the first of these
(after saving the marks); only the command can write in place. Curve
parameters and events go into PARAMS -- weights into the currency columns,
the pair's adjustment into its own -- and the marks the workbook had no cell
for go into rows the tool reads back: `atm 1m` (an ATM overwrite, in points),
`slog25 3m` (a smile parameter overwrite), `shift rho25` (a wing shift),
`anchor`, and a `BANDS` sheet for the band treatment. A copy loads as the
session it came from; formulas and their last values are kept, images and
charts are not. A pair is replaced, not merged, and the report says every
cell it touched and anything it could not write.

### Exchange traded options

A third panel fits a SABR curve to a listed market and holds it up against the
OTC mark. Each **panel is one expiry/underlying combination**; create as many
as you want and they are remembered between sessions.

Paste the exchange's strike and volatility columns straight in — tabs, commas
or spaces, header row or not, percent or decimal, bid/ask or mid. Everything
the parser infers is printed under the table and every line it cannot use is
listed with the reason; nothing is dropped quietly. A table whose volatilities
straddle 1.0 is refused rather than guessed, because 0.95 could be 95% or
0.95% and guessing would move a mark silently.

The fit is a vega-weighted least squares over `(alpha, rho, nu)` at fixed
`beta`. Alpha is *profiled out* at each `(rho, nu)` by a bounded scalar search
inside a bracket taken from the closed-form at-the-forward inversion, so the
outer problem stays two-dimensional and can be swept over its whole admissible
box before anything is polished — the same reasoning as the three-quote
calibration, and for the same reason: the answer must not depend on a starting
guess. With exactly three strikes the fit is an exact interpolation and the
panel says so, because zero residuals from three points are not evidence of
anything.

When the contract maps to a pair in the book, the marked surface is read at
the **same physical strikes**. `6J` is quoted in USD per JPY, so its strikes
are reciprocated onto USDJPY and its wings swap sides; lognormal implied
volatility is unchanged by that inversion, which is why volatilities compare
directly once strikes have been mapped. The delta table is *the book's* delta
strikes with both curves read off them, so the risk reversals under it are a
slope comparison at fixed strikes rather than two quotes in two conventions —
comparing quoted deltas across an inversion is exactly the kind of sign error
this project has already had to fix once.

Any of `alpha`, `rho` and `nu` may be **given rather than fitted** -- a wing a
desk has a view on, or yesterday's curve carried over and nudged. A held
parameter is held everywhere: the sweep visits no other value of it and the
polish does not carry it as a variable, so what comes back is the best curve
through the quotes *at* that value and the residuals say so. Hold all three
and nothing is fitted at all; the quotes are still priced against the curve,
which is the point of doing it. The three-strike rule follows the *free*
parameters, not SABR, so two held parameters leave two quotes sufficient. On
the screen the three boxes are blank by default, **Hold fit** fills them from
the last fit and **Release** empties them again, and every parameter that was
typed is marked `held` where it is shown -- one that was is otherwise
indistinguishable from one the market implied.

```bash
volkit listed 6J --expiry "2026-09-11 19:00" --forward 0.0068 --file quotes.txt
volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 --rho -0.2
```

#### Positions and aggregated risk

Underneath the panels is a position book. One line is one option —
`contract, expiry, strike, C/P, contracts` — and each line is priced against
the **panel above** whose contract and expiry it names. Drop the leading
columns when only one panel can answer (`strike, C/P, contracts`), or put a
header row on it and name the columns in any order. `ATM` is the panel's own
forward, and a quantity is signed.

That is the whole design: **every parameter a greek needs is one the panel
already had to have** in order to fit — the forward, the expiry, the
volatility at the strike. The one thing a fit never needed is the **contract
size**, which is therefore a field on each panel and defaults to the
contract's standard (125,000 for `6E`, 12,500,000 for `6J`).

Two sets of greeks are reported side by side:

* **Black–Scholes** — the closed-form Black-76 sensitivity at the option's own
  volatility, with that volatility *held fixed* as the future moves. This is
  what a Black-Scholes greek is, and what an exchange's own risk file will
  agree with.
* **Smile** — the same position revalued on the fitted SABR curve, with the
  forward bumped *inside* the parameters so the curve travels with the future,
  and volatility bumped by lifting the whole curve (alpha is scaled and the
  resulting at-the-money move is *measured*, so the number is per one point of
  at-the-money volatility). At a strike equal to the forward the two vegas are
  identical by construction.

Both read one volatility at one strike, so the premiums are identical and the
entire difference between the columns is in the sensitivities.

**What adds up.** Money adds across contracts — every CME FX option settles in
US dollars — so premium, vega, theta, the 1% delta and gamma money and volga
are totalled. Futures-equivalent delta and gamma are *not*: a euro future is
not a yen future, so those two are totalled per contract only and the
grand-total row leaves them blank rather than printing a sum of unlike things.
A line that matches no panel, or two, keeps its place in the table and says
which; a position priced against the wrong month's curve looks perfectly
ordinary, which is the one thing that may never be guessed at.

```bash
volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --positions book.txt --theta-days 3
```

Three things the model does **not** correct for, and reports instead of
silently absorbing: exchange settlement volatilities on American-style options
are not European volatilities; a future is not a forward once rates move with
the underlying; and the exchange's settlement time is rarely an FX cut. The
fitted curve is also checked for a negative risk-neutral density — Hagan's
expansion arbitrages in the wings — and the offending strike range is reported
rather than clipped away.

### Economic events

Dates come from two places, neither of which needs a network connection:

* **Rules**, for releases whose timing is defined by one — US non-farm
  payrolls is the first Friday at 08:30 New York (shifting a week when that
  Friday is a holiday). Release times are stored in the releasing body's local
  zone and converted through `zoneinfo`, so NFP is 13:30 UTC in winter and
  12:30 UTC in summer rather than whichever was hard-coded.
* **A dated table** at `volkit/data/econ_events.csv` for central bank
  decisions, which are set by committee and cannot be derived. FOMC, ECB, BoE
  and BoJ dates for 2026–27 ship with it. **Verify them before relying on
  them** — banks publish provisionally — and edit the file to extend.

US CPI has no stable rule; a second-Wednesday generator exists but is off by
default and flags itself as approximate.

**Weights are per currency, not per pair.** `volkit/data/event_weights.csv`
(`EVENT,CCY,WEIGHT`, vol points) says how much each release is worth on each
currency; without a row an event weighs its default on the currency that
releases it and nothing on any other. A pair's bump is its two legs' weights
**added** (`events.superpose`: a bump is a variance increment over twice the
volatility, so two bumps add to first order -- a root-sum-square would be the
rule for two event *volatilities*, which a bump is not) plus whatever the
pair marks on top in its own events table. So `FOMC,JPY,0.3` makes the Fed 1.8
on USDJPY and leaves it 1.5 on EURUSD, and a weight on a currency that does not
release the event puts the event on every pair with that leg. A weight of 0
switches an event off for that currency.

The table can also be marked for a session without touching the file: the
**Weights** button on the marking screen's Events card opens an optional card
holding it (one row per release, one column per currency, **Apply** posts it
whole), `--set EVENT:CCY=POINTS` does the same on the command line, and a saved
session keeps it. Applying changes what **Auto-load** suggests from then on and
nothing already on a pair's events table.

```bash
python3 -m volkit events USDJPY --horizon 1     # what would be auto-loaded, leg by leg
python3 -m volkit events USDJPY --weights        # ... and the whole weight table
python3 -m volkit events USDJPY --set FOMC:JPY=0.3   # the weights card, as a flag
```

## Run

```bash
python3 -m volkit -w files/vol_marks.xlsx serve       # web interface
python3 -m volkit -w files/vol_marks.xlsx check       # validate the workbook
python3 -m volkit validate USDJPY                    # hunt for competing smile fits
python3 -m volkit events   USDJPY --horizon 1        # scheduled economic events
python3 -m volkit tenors USDJPY --cut TK
python3 -m volkit smile  USDJPY 2024-05-28
python3 -m volkit vol    USDJPY 2024-05-28 --strike 1.02 --forward 1.0
python3 -m volkit daily  USDJPY --horizon 1 --cut NY --out USDJPY_daily_vol

python3 -m volkit band   USDHKD --feed files/market_feed.csv --hazard 3

python3 -m volkit listed 6J --expiry "2026-09-11 19:00" --forward 0.0068 --file quotes.txt
python3 -m volkit analysis EURJPY --history vol_history.xlsx --horizon 7 --target rr25
python3 -m volkit analysis USDJPY --history vol_history.xlsx --sabr

python3 -m volkit monitor EURUSD --history vol_history.xlsx \
    --watch EURUSD --watch USDJPY:history@-1m \
    --compare surface --compare history:-30d --compare history:-90d --field rr25

python3 -m volkit session marks.json                 # save every mark on the book
python3 -m volkit session marks.json --show          # what a saved file holds
python3 -m volkit --session marks.json vol USDJPY 2024-05-28 --strike 155

python3 -m volkit mm EURUSD --target-source quotes < run.txt          # fit, and report
python3 -m volkit mm EURUSD --file run.txt --request ask.txt --fallback-spread 0.3
python3 -m volkit mm EURUSD --request ask.txt --target-source none \
    --vega position.txt --axe-scale 500 --history vol_history.xlsx   # quote off the marks
python3 -m volkit mm EURUSD --learn < run.txt        # propose widths; --save writes them

# the quoting agent: keep an archive of what the market has shown, and quote from it
python3 -m volkit agent fetch  --sdr sdr/ --days 5           # get DTCC's public dissemination
python3 -m volkit agent fetch  --sdr sdr/ --since 2025-09-01 # ... or backfill the 366 days it keeps
python3 -m volkit agent trades EURUSD --invert --history vol_history.xlsx
python3 -m volkit agent ingest --chats chats/ --sdr sdr/    # read what is new
python3 -m volkit agent watch  --chats chats/ --every 30    # ... and keep reading
python3 -m volkit agent evidence EURUSD                     # what the archive says
python3 -m volkit agent learn    EURUSD --save              # propose widths from it
python3 -m volkit agent quote    EURUSD --record <<< '1M ATM in 100mm vega'
python3 -m volkit agent archive  EURUSD --kind shown        # the prices we made
python3 -m volkit agent outcome  EURUSD --ref <id> --result traded_ask
python3 -m volkit agent ask      EURUSD "how wide has the 3M fly been shown, and by whom"
python3 -m volkit agent ask      EURUSD                     # ... a question a line; it writes nothing

# the same archive behind the cards in the Market maker tab (the quoting agent, the
# marking agent, and "Ask the record" -- the question box that writes nothing)
python3 -m volkit serve --chats chats/ --sdr sdr/ --archive mm_archive.jsonl
python3 -m volkit mm EURUSD --request ask.txt --archive-width   # the archive on the width ladder

# the marking agent: how to run the fit, and what this desk does after it
python3 -m volkit mark propose EURUSD --file run.txt --out p.json     # the card's own path
python3 -m volkit mark propose EURUSD --target curve.txt --out p.json
python3 -m volkit mark record  EURUSD --proposal p.json --verdict edited
python3 -m volkit mark learn   EURUSD                    # what the journal says
python3 -m volkit mark rules   EURUSD                    # the rules of thumb against the desk
python3 -m volkit mark learn   EURUSD --no-rules         # the desk-only answer
python3 -m volkit mark confer  EURUSD --archive mm_archive.jsonl   # the two agents
```

`serve` takes `--feed` for the spot / forward file, `--history` for the
historical workbook and `--knowledge` for the market-maker knowledge bank; all
three are optional and each panel says what it is missing without them.
`--session PATH` is global rather than `serve`'s own: it puts a saved set of
marks on the book before anything is priced, so every subcommand and the web
interface see the same ones.

Add `--asof "2024-02-28 12:00"` to price against a fixed valuation time.
Without it the current UTC time is used, once, at startup.

The quoting agent is both a command and a **card inside the Market maker tab**,
not a screen of its own -- it answers a question about the market pasted on
that tab, so it is three more routes on `mm` and it leaves with that tab if a
build excludes it. `serve --chats DIR --sdr DIR` names the folders the card
may scan; `--archive` names the file. The card compares the width you would
show against the width the archive says this thing is actually shown at, per
quoted row, and changes nothing.

There are **two agents**, and on the Market maker tab each is tied to one of
its two buttons. The quoting agent above answers *what do I show*; its link to
**Quote** is the *widths from the archive* switch, which puts the archive on
the quote's width ladder between the bank and the typed fallback. The
**marking agent** (`volkit mark`, and the *Marking agent* card beside the fit)
answers *where should the surface be*: it is aimed at the **Fit** button's own
inputs and decides how to run that fit -- which knobs to free,
what the targets can actually determine, whether anything constrains the wings
-- proposes the result, and learns from what you do to its proposals. It
learns tendencies with counts on them, never a policy: a desk re-marks a curve
a few times a day, so a correction is applied only when the desk's answers
agree with each other, and it is capped at half of what the fit itself moved.
On the card, **Accept** hands the proposal to the quote as the marks it stands
on, **Take the plan onto the fit** puts the agent's knob choices on the fit
panel and runs it, and each answer is a line in the journal (`serve
--journal`; `mm_remarks.jsonl` beside the workbook by default).

Before the journal holds anything the agent can be given **rules of thumb**
(`mm_rules.toml` beside the workbook, TOML, hand-edited; `serve --rules`;
`files/mm_rules_sample.toml` is the shape). A rule is a belief about where
this desk lands relative to the fit on one knob, with a weight of two to five
answered proposals, seeded into the learned sample so the journal can outvote
it. A rule can shape the size of a nudge and never authorise one -- three
real corrections on its side are needed before anything is applied -- and
every rule-shaped number decomposes into the rule's share and the desk's.
`volkit mark rules PAIR` prints each rule against the real corrections and
flags one the desk contests; `--no-rules` (and the checkbox on the card) is
the desk-only answer beside it.

`volkit mark confer` is the two of them talking. The quoting agent's flag
about where the market has been becomes a target for the marking agent; the
marking agent proposes; the quoting agent scores what the proposal fixed and
**what it broke** at every archived point, including the ones the fit was not
aimed at. It re-weights what it broke, tries again a bounded number of times,
and puts the best round in front of you. All numbers, no language model.

`agent fetch` downloads DTCC's public price dissemination files -- the
anonymised FX option trade reports the CFTC requires to be published -- into
the SDR folder, where the reader picks them up. DTCC keeps 366 days. It runs
wherever there is a network, so a desk with no route out can have them fetched
elsewhere and dropped in the folder; `--proxy`, or `https_proxy` in the
environment, covers a desk behind one, and `--no-proxy` gets past a system
proxy that is named and not running. Whichever of those carried the request
is named on a failure, because urllib reads the Windows registry's proxy on
its own and a connection refused by one nothing named reads exactly like no
network at all. `agent trades PAIR --invert` then turns
each printed premium into the volatility it implies, using the forward on the
trade's own date out of the historical workbook -- and refusing, by name, any
trade whose forward it cannot find rather than reaching for today's.

`agent` keeps
`mm_archive.jsonl` beside the workbook -- broker quotes, SDR trades, prices the
desk made and what became of them -- ages that evidence, and makes a two-way
out of the marked surface, the knowledge bank and the archive together. Every
price comes back as an ordered list of ingredients that sums to it. A local
model (Ollama, or anything with an OpenAI-compatible endpoint) is optional and
does two jobs on a leash: it turns chat prose into lines the quote grammar must
then accept, and it writes the explanation from the already-computed decision.
Every number it returns is checked against the text it was given, and the whole
output is refused if it contains one that was not. With no model running,
everything else still works and each command says so.

`--enable-tab NAME` turns on a screen a build was made with but hid, and
`--config PATH` / `--no-config` choose or refuse the startup settings file --
all three are read before the parser exists, because they decide what the
parser contains.

## Library

```python
from volkit import Book, Clock

book = Book.from_excel("files/vol_marks.xlsx").load_all()
jpy  = book["USDJPY"]

jpy.atm_vol("2024-05-28", "TK")          # ATM at the Tokyo cut
jpy.vol(1.02, "2024-05-28")              # strike/forward ratio -> implied vol
jpy.vol([0.98, 1.00, 1.02], "2024-05-28")# vectorised over strikes
jpy.delta_strike("2024-05-28", 0.25, is_call=True)
jpy.risk_reversal("2024-05-28", 0.25)
jpy.strangle("2024-05-28", 0.25)
jpy.density(1.0, "2024-05-28")
jpy.atm.daily_series(1.0, "NY")

from volkit.pricing import OptionLeg, price_strip
price_strip(book, [
    OptionLeg("USDJPY", "1M", "25d", "C", spot=150.25, forward_points=-45, pip=100, notional=10),
    OptionLeg("USDJPY", "1M", "25d", "P", spot=150.25, forward_points=-45, pip=100,
              notional=10, direction=-1),
])

for w in book.all_problems():
    print(w)                             # nothing fails silently
```

Pass `Clock(datetime(...))` to `from_excel` to pin the valuation instant —
the same clock always gives the same numbers.

## Layout

| Module | Responsibility |
|---|---|
| `timeutil` | one day-count, one clock, tenor parsing |
| `numerics` | bracketed solves, damped fixed points, panel integration |
| `calendars` | holiday calendars, spot/expiry rolls, CSV overrides |
| `timeweight` | intraday / weekend / holiday weighting |
| `black` | Black-76, FX delta conventions, strike-from-delta |
| `sabr` | Hagan 2002 lognormal SABR and its calibration |
| `smile` | arbitrage-constrained SVI, vanna-volga, cached slices |
| `events` | dated volatility bumps and height calibration |
| `atm` | the ATM term structure |
| `cross` | cross pairs from two legs and a correlation, and which two dollar pairs a cross name means |
| `surface` | ATM + smile, greeks, delta strikes, RR / fly |
| `marketdata` | validated Excel reader: CONFIG is two columns, and a cross names its own dollar legs |
| `book` | all pairs, built in dependency order |
| `pricing` | multi-leg option strips, strike/expiry specs, per-leg error isolation |
| `econ` | scheduled economic events: rules plus a dated central-bank table |
| `exotics` | digitals, one-touch / no-touch, and the overhedge buffers |
| `feed` | spot and forward points from a file, interpolated between pillars |
| `banded` | managed / pegged pairs: Beta-on-band body with a hazard-rate jump leg, and how much notice the surface takes of it |
| `curves` | several volatility curves side by side, and the same curve on other dates |
| `monitor` | small panels: what has moved between two points in time, one pair each |
| `session` | the marks a session made, saved beside the workbook and put back |
| `listed` | exchange traded options: paste parsing, least-squares SABR, comparison against the marked surface |
| `moments` | risk-neutral distributions read off a smile; two of them combined into a cross |
| `history` | historical spot / forwards / quotes, and realized volatility, skew and kurtosis |
| `analytics` | carry and roll, fair value, the cross triangle, and indication pricing |
| `quotes` | a broker run, in English or in columns: outrights, risk reversals, butterflies, spreads, timestamps and which of two quotes for one thing is live |
| `knowledge` | the per-pair knowledge bank: widths, floors, mid shifts and advisory notes |
| `marketmaker` | fit the curve to a target, fine tune the wings to a market, and quote it |
| `webapp`, `web/` | the local web interface |
| `screens` | which screens a build has, shown or hidden; the one reader of the build's manifest |
| `config` | the startup settings file a double-clicked executable reads |
| `cli` | command line |

## Extending

* **A new interpolator** — add it to `INTERPOLATORS` and to `SmileSlice.vol`.
  One that needs more than the five anchors — as `BAND` needs the band and its
  treatment — takes it as a keyword on `SmileSlice.build` and joins the cache
  key in `VolSurface.slice_at`, or two settings will share one cached smile.
* **A new curve source for the comparison panel** — add it to `CURVE_KINDS`
  and give it a builder dispatched from `curves.build_curve`; the comparison
  panel, the monitor panels, the CLI and the page all read the same list and
  the same dispatch. Decide whether a *monitor* end may use it: a tile is
  rebuilt on every refresh, which is why `paste` is refused there.
* **A new data source** — produce a `MarketData` object; nothing below
  `marketdata` knows about Excel.
* **A new holiday** — add a row to `files/holiday_overrides.csv`.
* **A new economic event** — add a row to `volkit/data/econ_events.csv`, or a
  generator to `RULE_GENERATORS` in `econ.py` for a rule-based release. New
  releasing bodies need one line in `RELEASE_TIMES` giving currency, time zone
  and local release time.
* **An event's weight on another currency** — a row in
  `volkit/data/event_weights.csv`; or, for one workbook, a currency column in
  `PARAMS`.
* **A different intraday profile** — pass `hourly_weight` / `session_hours`
  to `TimeWeighting`; holiday combinations are computed, not enumerated.
* **A new output in the UI** — add a branch to `BookService.calc` and a name
  to the quick-query list in `web/index.html`.
* **A new pricing-panel row** — add a field to `LegResult` and an entry to the
  `IN` (input) or `OUT` (result) table in `web/index.html`.
* **A new historical column spelling** — add the word to `_FIELD_WORDS` in
  `history.py`. Tokens are classified before they are assigned, so word order
  in the header does not matter.
* **A new roll target** — add it to `TARGETS` in `analytics.py` and a branch to
  `_target_legs`; the Analysis screen picks it up from `/api/state`.
* **A new listed contract** — nothing has to be added at all: the panel's
  **Contract** box is free text, and a code that is not in the table is taken
  as typed, with its pair, inversion, strike scale and contract size set on
  the panel. Adding a `_u(...)` line to `UNDERLYINGS` in `listed.py` is what
  makes the code bring those four with it and stop being marked *typed*; the
  panel offers the known ones as suggestions from `/api/state`.
* **A new column layout in a pasted table** — add the header spelling to
  `_HEAD_STRIKE` / `_HEAD_VOL` / `_HEAD_BID` / `_HEAD_ASK` in `listed.py`.
  Explicit `strike_column` / `vol_column` overrides always win.
* **A new product** — add it to `PRODUCTS` in `pricing.py` and a branch in
  `_price_leg`; the panel picks it up from `/api/state`.
* **A new overhedge profile** — add a shape to `_barrier_profile` in
  `exotics.py` and a name to `TOUCH_MODES`. Flat profiles use the closed form
  automatically; anything time-dependent routes to the simulator.

## Packaging a Windows executable

`build_exe.py` is the whole build. It runs the same steps whether it is invoked
by hand, by `build_windows.bat`, or by the `build-windows` GitHub Actions
workflow, so a local build and a CI build are the same build.

```bash
python build_exe.py                 # dist/volkit/volkit.exe beside its libraries
python build_exe.py --onefile       # dist/volkit.exe, one self-extracting file
python build_exe.py --zip           # also dist/volkit-windows.zip, to hand over
python build_exe.py --host-check    # build for THIS platform, to validate the spec

python build_exe.py --exclude-tab mm --exclude-tab listed   # build without them
python build_exe.py --only-tabs pricing,marking             # the same, said other way
python build_exe.py --hidden-tab mm      # built, but off until --enable-tab mm
```

It checks the source tree, installs the dependencies, **runs the full test
suite**, builds through `volkit.spec`, copies the trader's data files beside
the exe, then runs the executable it just produced and makes it price
something. Any step that fails stops the build with the real message.

The interpreter, numpy/scipy/pandas/openpyxl and the IANA time zone database
travel inside the bundle, so the target machine needs no Python and no pip.
`tzdata` is not optional: Windows has no system IANA database, and the NY cut,
the weekly close and every economic event resolve through `zoneinfo`. The
workbook, the feed and the band and holiday overrides are *not* bundled --
they are copied next to the exe where `paths.app_dir()` finds them and where
they can be edited without rebuilding. Sample data goes in `samples/`, never
beside the exe, so nothing synthetic is ever picked up by default.

One folder is the default: it starts faster, because `--onefile` unpacks
numpy and scipy to a temporary directory on every launch.

### Building without some of the screens

A desk that only marks the surface has no use for a market-maker tab, and a
tab that is present but unwanted is a tab that gets clicked by accident.
`--exclude-tab` (repeatable, and it accepts a comma-separated list) and
`--only-tabs` choose the set; the workflow takes the same choice as its
`exclude_tabs` input. The chosen names are written into the bundle as
`volkit/data/screens.txt`, and `volkit/screens.py` is the only thing that
reads it. An excluded screen is then gone three ways at once:

* the tab is not in the tab bar and its panel is never booted -- `/api/state`
  lists the screens the build has and the page keys off that;
* its routes answer 404 with *the Market maker screen was excluded from this
  build*, naming the screens that are there;
* its subcommands are not registered, and typing one gets that same sentence
  rather than argparse's *invalid choice*.

Two things it is not. It does not remove code from the build -- numpy and
scipy are the size of a build, not `analytics.py`, and an import that vanished
would turn a wrong build into a stack trace instead of a sentence. And it is
not a permission system: anyone who can run the exe can run a build that has
the tab. It keeps a screen off a desk that did not ask for it, nothing more.

**Hidden, the third state.** `--hidden-tab NAME` builds a screen and leaves it
off. It is turned away by exactly the same route check and the same subcommand
check as an excluded one, and says the other sentence:

```
the Market maker screen is hidden in this build. Start volkit with
--enable-tab mm to turn it on, or add 'enable-tab = mm' to volkit.cfg
beside the executable
```

`volkit.exe --enable-tab mm` switches it on for that run, before the parser is
built -- which it has to be, since the flag changes which subcommands the
parser has. Asking for a screen the build does not contain at all is an error,
not a shrug, so a switch that could never work says so. A build cannot hide
everything: at least one tab must show without a switch. The smoke test checks
both halves of a hidden screen -- that its subcommand is off by default, and
that `--enable-tab` really turns it on -- because a hidden screen whose switch
did not work would be indistinguishable from an excluded one.

### Startup settings, for a double-click

A double-clicked executable gets no command line at all, so `volkit.cfg`
beside it is one:

```ini
command    = serve
workbook   = vol_marks.xlsx
feed       = market_feed.csv
port       = 8900
no-browser = false
enable-tab = analysis
```

`key = value` becomes `--key value`; the value is the rest of the line, so a
Windows path with spaces needs no quoting. `command` is the subcommand and any
positional arguments after it. A `true`/`yes`/`on` value becomes a bare switch
and a `false`/`no`/`off` one is left out. A key may repeat. `#` starts a
comment.

It is read **only when nothing was typed** -- anything on the command line
means the file stays shut, because a settings file that partly overrode what
somebody just typed would be the most confusing possible arrangement.
`--no-config` skips it and `--config PATH` reads a named one whatever else was
typed. What it read is printed on startup: a packaged app taking silent orders
from a file nobody remembers writing is the same failure as a swallowed error.
Option *names* are not validated here -- a misspelled key becomes an option
argparse has never heard of and argparse says so by name, which is a better
error than this file could invent and one that cannot drift out of step with
the real options.

A commented sample ships as `files/volkit.cfg` and is staged beside the exe.

The smoke test follows the selection: `tenors` belongs to the marking screen,
so a build made without marking is smoke-tested with what it does have, and
each excluded screen's subcommands are checked to be genuinely gone. The test
suite always runs with every screen -- what is being checked is the code about
to be bundled, and a `VOLKIT_SCREENS` left in the shell would otherwise turn a
trimmed run into a green build.

**PyInstaller cannot cross-compile.** It bundles the host interpreter and
host-compiled extension modules, so a Windows exe can only be built on
Windows. Run this on macOS or Linux and it refuses rather than handing back
something unusable, and prints the two routes that work: a Windows machine, or
the hosted Windows runner. `--host-check` builds the identical spec for the
host instead, which catches a missing hidden import or an unbundled resource
without leaving the desk.

### Driving the Windows build from here

`build_windows_github.sh` is the second route, wrapped: it dispatches the
workflow, waits, and unwraps the artifact into `dist-windows/`.

```bash
./build_windows_github.sh                    # standalone exe; Analysis, Market
                                             # maker and Monitor hidden
./build_windows_github.sh --folder --no-hidden
./build_windows_github.sh --hidden-tab mm --exclude-tab listed
./build_windows_github.sh --explain          # why did the last run fail?
```

It needs a credential — `brew install gh && gh auth login`, or a
`$GITHUB_TOKEN` with Actions read/write.

**It commits and pushes first.** The runner builds what is on GitHub, so
anything left in the working tree would silently not be in the exe. Rather
than refuse, the script prints what it is about to commit, commits it, pushes,
and confirms `origin/BRANCH` is at your HEAD before dispatching. Two things it
will not do on its own: it will not commit an untracked file larger than 10 MB
— that is build output, not source, and an unguarded `git add -A` is how a
`dist/` folder ends up in a repository — and it will not resolve a diverged
branch, because a merge or a rebase is a decision about someone else's work
and not a build step. `-m` sets the commit message, `--no-commit` refuses
instead of committing, and `--allow-dirty` builds the pushed commit and leaves
local changes alone.

It then checks that the workflow file on that branch actually declares the
inputs about to be sent. An input the workflow does not declare is dropped in
silence, and the build would come back with every tab showing.

`--explain` is the reason it exists. A failed run reports *process completed
with exit code 1* and buries the cause several clicks away; this prints the
failing step's own log, where `build_exe.py` says which of its steps died
and why.

## Tests

```bash
python3 -m unittest discover -s tests -v      # 431 tests, no pytest needed
PYTHONUTF8=0 LC_ALL=C python3 -m unittest discover -s tests   # as Windows sees it
```

`pip install esprima` additionally enables a syntax check on the front-end
JavaScript; that test skips if it is absent.

The second line runs the same suite in an ASCII locale, which is the only way
to catch a text-encoding bug from a Mac before the Windows runner does. Python
reads and writes text in the *locale's* encoding unless told otherwise, and on
the desk machine that is cp1252: reading `volkit/web/index.html` with it once
ended the whole Windows build with `'charmap' codec can't decode byte 0x81`.
Every text file goes through `paths.read_text` / `open_text` / `write_text`
instead, and a test walks the source for anything that does not.
