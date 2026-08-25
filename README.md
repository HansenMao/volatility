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

## The five panels

The web interface separates the jobs the tool does.

**Pricing** — a grid where **each column is an option** and each row is a
field. Add columns for as many legs as you like; vol and premium are computed
off the current marks and refresh as you type.

| Input | Accepts |
|---|---|
| Pair | any pair in the book |
| Expiry | a date, or a tenor (`1M`, `3M`) resolved through spot and delivery on the pair's holiday calendar |
| Strike | a number, `ATM`, or a delta — `25d`, `10dp`, `-25d`. A bare `25d` takes its wing from the option type |
| Cut | `TK` / `NY` / `LDN` / `HK` |
| Type | `C`, `P`, or `Auto` (call if the strike is above the forward) |
| Spot, Fwd pts, Pip | forward is `spot + points / pip`; the pip divisor defaults to 100 for JPY crosses, 10000 otherwise |
| Notional, Side | notional in millions of base (payout, for touch products); buy or sell flips the signed amounts *and* which way an overhedge buffer is applied |
| Product | `vanilla`, `digital`, `one_touch`, `no_touch` |
| Barrier | level for touch products; the vol is read at the barrier |
| Digital ramp % | call-spread width replicating the digital, as % of strike |
| Overhedge, Buffer % | barrier shift: `extend`, `bend_front`, `bend_back` |

Leave **Spot** blank to take spot and interpolated forward points from the feed.

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

Outputs per column: forward, resolved strike, vol, ATM vol, premium (term
currency per unit of base, and % of base), delta, smile delta, vega, and the
notional-scaled premium / vega / delta. Totals are bucketed by currency pair.
A leg that fails shows its error in its own column — the rest still price.

Premiums are **undiscounted forward values**; the model carries no rate curve
(neither did the original). Columns persist in the browser between sessions.

**Vol marking** — the surface itself, editable at four levels, all of which
feed the pricing panel immediately:

1. **Curve parameters** — the backbone itself: initial vol, long-term vol,
   mean reversion, short add-on and decay, rate vol and correlation. For a
   cross the same card marks the *correlation* term structure (initial, final,
   decay) and shows which legs and triangle signs it is built from. A rejected
   value leaves the curve untouched and says why.
2. **Events** — a table of dated volatility bumps. **Auto-load** pulls the
   scheduled economic releases for the pair's currencies over a chosen horizon;
   every row is then editable, and rows can be added or deleted by hand. Applying
   re-solves each event height so the quoted bump is reproduced exactly. The
   *vol day* column shows which volatility day each bump actually prices into —
   the day rolls at 14:00 UTC, so a late release lands on the next one and is
   flagged.
3. **ATM term structure** — per-tenor overwrites of the marked vol.
4. **Smile parameters** — per-tenor `slog25`, `slog10`, `rho25`, `rho10`.

An **implied vs quoted** table reads the risk reversals and butterflies back
off the fitted smile and shows them against the quotes that went in. Because
the smile is fitted per tenor and then given a parameter term structure of its
own, the two differ — on the sample workbook by up to 0.07 vol points at 2Y —
until **anchor** is on, which collapses the differences to under 0.003. That
table is the marking check the legacy tool had no way to display.

Type into a shaded cell and press Enter; clear it to revert to the model.
Overwrites are held in memory and are lost on reload.

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

Two refusals are deliberate. **Break risk is a marked input, never inferred
from a butterfly**: a wider Beta body and a higher hazard both raise the
at-the-money, so a joint fit is degenerate. `--solve-hazard` inverts it anyway,
deliberately, and reports what the answer depended on. And a band is an
*absolute* price range while the surface works in strike over forward, so
placing one needs an outright forward from the feed; without one the BAND
method refuses and names the feed rather than guessing a level.

```bash
volkit band USDHKD --feed market_feed.csv --hazard 3 --weak-jump 8
volkit vol USDHKD 2026-03-16 --strike 7.90 --method BAND --feed market_feed.csv
```

A useful finding from the sample marks: the band alone gives USDHKD a
*negative* risk reversal against a quoted positive one. Most of the quoted skew
is peg-break premium.

### Analysis

A fourth panel asks four questions of the whole tenor grid at once. Each
section is built independently and reports its own reason for being empty, so a
missing forward feed does not take the realized statistics down with it.

**Carry and rolldown.** Every tenor is revalued after a horizon at a *fixed
absolute strike* — the option you own keeps its strike while both the maturity
and the forward move under it. The roll splits into the slide along the term
structure (same moneyness, shorter maturity) and the slide across the smile
(same maturity, forward moved), so the forward curve's contribution is
separable rather than buried in one number. The target can be the ATM, either
25 or 10 delta wing, or a risk reversal or butterfly; `roll / atm` is the roll
per year as a fraction of the ATM level.

**Fair value.** Hold the `T` at-the-money option for the horizon `h` and delta
hedge it. The mark slides by `roll`, worth `vega(T−h)·roll`; the gamma against
theta earns roughly `h/T` of the option's life at `σ_realized − σ_implied`.
Setting the two to cancel:

```
fair = realized + roll · (T/h) · vega(T−h)/vega(T)
richness = implied − fair
```

The multiplier uses the actual vegas rather than the `sqrt(T)` proxy, because
the strike is not the same distance from the two forwards once the forward
curve has any slope. The roll here is always the **at-the-money** roll, taken
from a table the function builds itself — feeding it a risk-reversal roll and
an at-the-money implied would mix two different positions into one break-even.

**Realized against implied.** Load a historical workbook — one sheet per pair,
one row per date, columns for spot, forwards and the quoted surface. Columns
are matched by reading their headers, not by position, and the volatility unit
is decided **once per sheet from the at-the-money column**: a 25 delta risk
reversal of −0.89 vol points is below 1 in magnitude, so sniffing each column
on its own reads it as a decimal and returns it a hundred times too large.

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

**Curve comparison.** A fifth section on the same panel puts any number of
curves side by side and differences them against whichever one is marked
*base*. A curve is one of four things:

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
volkit analysis EURUSD --history vol_history.xlsx \
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
the others alone.

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

**3. Put a price round the mid.** The width comes from the pair's knowledge
bank; the mid is shaded by fair-value richness and by the vega already on your
book, both capped as a fraction of the width so an axe can lean a price inside
the market but never walk it out of one. Both leans point the same way: a rich
market and a long position are both reasons to want to sell, and you attract a
seller's trade by shading down. Neither is applied to a risk reversal or a
butterfly — a break-even against realized volatility and a vega position are
statements about the *level*, and those rows say so.

Nothing here touches the workbook. The fit reports and then puts the marks
back; tick **keep the marks** to leave them on the loaded book, in memory only.

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

```bash
volkit listed 6J --expiry "2026-09-11 19:00" --forward 0.0068 --file quotes.txt
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

```bash
python3 -m volkit events USDJPY --horizon 1     # what would be auto-loaded
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
python3 -m volkit analysis EURUSD --history vol_history.xlsx \
    --compare surface --compare history:-30d --compare history:-90d --field rr25

python3 -m volkit mm EURUSD --target-source quotes --fallback-spread 0.3 < run.txt
python3 -m volkit mm EURUSD --vega position.txt --axe-scale 500 --history vol_history.xlsx < run.txt
python3 -m volkit mm EURUSD --learn < run.txt        # propose widths; --save writes them
```

`serve` takes `--feed` for the spot / forward file, `--history` for the
historical workbook and `--knowledge` for the market-maker knowledge bank; all
three are optional and each panel says what it is missing without them.

Add `--asof "2024-02-28 12:00"` to price against a fixed valuation time.
Without it the current UTC time is used, once, at startup.

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
| `cross` | cross pairs from two legs and a correlation |
| `surface` | ATM + smile, greeks, delta strikes, RR / fly |
| `marketdata` | validated Excel reader |
| `book` | all pairs, built in dependency order |
| `pricing` | multi-leg option strips, strike/expiry specs, per-leg error isolation |
| `econ` | scheduled economic events: rules plus a dated central-bank table |
| `exotics` | digitals, one-touch / no-touch, and the overhedge buffers |
| `feed` | spot and forward points from a file, interpolated between pillars |
| `banded` | managed / pegged pairs: Beta-on-band body with a hazard-rate jump leg, and how much notice the surface takes of it |
| `curves` | several volatility curves side by side, and the same curve on other dates |
| `listed` | exchange traded options: paste parsing, least-squares SABR, comparison against the marked surface |
| `moments` | risk-neutral distributions read off a smile; two of them combined into a cross |
| `history` | historical spot / forwards / quotes, and realized volatility, skew and kurtosis |
| `analytics` | carry and roll, fair value, the cross triangle, and indication pricing |
| `quotes` | a broker run written in English: outrights, risk reversals, butterflies, spreads |
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
  and give it a builder in `curves.py`; the panel, the CLI and the page all
  read the same list.
* **A new data source** — produce a `MarketData` object; nothing below
  `marketdata` knows about Excel.
* **A new holiday** — add a row to `files/holiday_overrides.csv`.
* **A new economic event** — add a row to `volkit/data/econ_events.csv`, or a
  generator to `RULE_GENERATORS` in `econ.py` for a rule-based release. New
  releasing bodies need one line in `RELEASE_TIMES` giving currency, time zone
  and local release time.
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
* **A new listed contract** — add a `_u(...)` line to `UNDERLYINGS` in `listed.py`
  giving its code, the FX pair it maps to and whether its strikes are the
  reciprocal of that pair's. The panel picks it up from `/api/state`; a
  contract that is not in the table can still be used as `CUSTOM` with the
  pair, inversion and strike scale set by hand.
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
./build_windows_github.sh                    # standalone exe, Analysis hidden
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
failing step's own log, where `build_exe.py` says which of its six steps died
and why.

## Tests

```bash
python3 -m unittest discover -s tests -v      # 347 tests, no pytest needed
```

`pip install esprima` additionally enables a syntax check on the front-end
JavaScript; that test skips if it is absent.
