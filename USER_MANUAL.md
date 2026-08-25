# volkit — user manual

FX volatility marking and pricing. An ATM term structure with dated events, a
SABR/SVI smile, cross pairs built from their legs, vanilla and exotic pricing,
two-way market making, and a browser interface.

---

## 1. Starting it

**Packaged (Windows):** double-click `volkit.exe`. A console window opens,
prints a URL, and your browser opens on it. Keep the console open — closing it
stops the tool. Press `Ctrl+C` in the console to stop cleanly.

A double-click gives the tool no options, so it reads **`volkit.cfg`** beside
the executable instead. Open it in Notepad; it ships commented out, one setting
a line:

```
command    = serve
workbook   = vol_marks.xlsx
port       = 8900
enable-tab = analysis
```

Whatever it read is printed at the top of the console, so you can always see
which settings are in force. If you start the tool from a command prompt with
any options of your own the file is ignored entirely — the two never apply half
each. `volkit.exe --no-config` ignores it too, and `volkit.exe --config
other.cfg` reads a different one. A setting name that is not a real option is
reported by name and the tool stops, rather than being quietly skipped.

**From source:**

```
pip install -r requirements.txt
python -m volkit serve
```

Your files live **next to the executable** (or in `files/` when running from
source):

| File | What it is | Required |
|---|---|---|
| `vol_marks.xlsx` | the workbook: parameters, quotes, events | yes |
| `market_feed.csv` | spot and forward points | no |
| `vol_history.xlsx` | past spot / forwards / quotes, for the Analysis tab | no |
| `bands.csv` | managed/pegged trading bands | no |
| `volkit.cfg` | startup settings, read when the exe is double-clicked | no |
| `holiday_overrides.csv` | extra holiday dates | no |
| `mm_knowledge.json` | the market maker's knowledge bank — widths, floors, notes | created when you first save one |

If the tool cannot find the workbook, pass it: `volkit.exe -w C:\path\to\vol_marks.xlsx`

---

## 2. The tabs

A build does not always have all five. One can be made without any of them
(see the packaging notes in `README.md`), and then the tab is simply not
there: nothing is greyed out, and the command line for that screen is gone
too. Whatever the build has is what the tab bar shows.

A build can also be made with a tab **hidden** rather than missing: it is
there, but off until you ask for it. Start the tool with
`volkit.exe --enable-tab analysis`, or put `enable-tab = analysis` in
`volkit.cfg` so a double-click turns it on. If you ask for a screen this build
does not contain at all, it says so and stops rather than starting without it.
Run `volkit.exe --help` to see which, if any, are hidden.

### Pricing

Each **column is one option**; each row is a field. Add columns with
**+ Option**, copy the last with **Duplicate last**, and delete one with the
**✕** at the top of its column. **− Remove last** drops the newest and
**Clear all** starts again. The row of **Remove** buttons that used to sit
between the inputs and the results is gone; the cross in the column header
does the same job out of the way of the fields you are typing into. Columns
are remembered between sessions. Prices refresh as you type unless you untick
**auto-price**.

**Inputs**

| Field | Accepts |
|---|---|
| Product | `vanilla`, `digital`, `one_touch`, `no_touch` |
| Expiry | a date, or a tenor (`1M`, `3M`) resolved through spot and delivery on the pair's holiday calendar |
| Strike | a number, `ATM`, or a delta — `25d`, `10dp`, `-25d`. A bare `25d` takes its wing from the option Type |
| Barrier | level for touch products; the volatility is read *at the barrier* |
| Type | `C`, `P`, or `Auto` (call if the strike is above the forward) |
| Spot / Fwd pts / Pip | forward is `spot + points / pip`. **Leave Spot blank to take both from the feed**, with points interpolated to your exact expiry |
| Notional / Side | millions of base (payout, for touch products). Buy or sell flips the signed amounts *and* which way an overhedge buffer is applied |
| Digital ramp % | width of the call spread replicating a digital, as % of strike |
| Overhedge / Buffer % | barrier shift: `extend`, `bend_front`, `bend_back` |

**Outputs** — forward, resolved strike, volatility, price, fair value before
any buffer, the overhedge cost, delta, smile delta, vega, and notional-scaled
amounts. Totals are bucketed by currency pair. A leg that fails shows its error
in its own column; the rest still price.

> Premiums are **undiscounted forward values**. There is no rate curve in this
> model.

**Overhedges.** A zero digital ramp is the unhedgeable limit; widening it gives
you an instrument you can actually run and costs the seller more.
`extend` shifts the barrier in parallel for the whole life; `bend_front` shifts
it fully at inception tapering to nothing at expiry; `bend_back` is the
reverse. A bent barrier has no closed form, so it is simulated and reports its
Monte Carlo standard error in the **MC std error** row.

### Vol marking

Two columns.

**Left** — the fitted surface and the checks on it:

* **Charts** — smile (with the risk-neutral density), term structure, daily
  vols. The expiry box beside the tabs drives the smile chart.
* **Smile parameters** — per-tenor `slog25`, `slog10`, `rho25`, `rho10`. Type
  into a shaded cell and press Enter to overwrite; clear it to revert.
* **Implied vs quoted** — risk reversals and butterflies read back off the
  fitted smile against the quotes that went in. They differ because the smile
  is fitted per tenor and then given a parameter term structure of its own;
  ticking **anchor** collapses the differences. This is your marking check.

**Right** — the curve, the marks it produces, and the events on it:

* **Curve parameters** — the backbone: initial vol, long-term vol, mean
  reversion, short add-on and decay, rate vol and correlation. For a **cross**
  this marks the *correlation* term structure instead and shows which legs it
  is built from. A rejected value leaves the curve untouched and says why.
* **ATM term structure** — per-tenor overwrites of the marked volatility.
* **Managed band** — only for a pegged pair, and only if it is listed in
  `bands.csv`. See below.
* **Events** — dated volatility bumps. **Auto-load** pulls scheduled economic
  releases for the pair's currencies; edit, add or delete rows, then **Apply**
  to re-solve the heights.

Everything here feeds the pricing tab immediately.

#### Managed and pegged pairs (USDHKD and the like)

A pair whose spot is defended inside a band does not behave lognormally. The
band model treats it as two regimes: the peg holds, and the rate sits somewhere
on the band in a U-shape (it spends its life near the edges, because that is
where the central bank steps in); or the peg breaks, one way or the other, and
lands somewhere else entirely.

The band itself comes from `bands.csv` and is not marked on the screen — it is
policy, and if it changes the file changes. What you mark is how much notice to
take of it:

| Setting | What it does |
|---|---|
| **Treatment** | *flag strikes outside it* (the default — prices stay lognormal, you get a warning), *ignore the band*, or *price the regime mixture* |
| **Hazard %/yr** | how likely the peg is to break, per year. One number covers every tenor |
| **Weak side %** | of breaks, the share that go the weak way (USDHKD higher) |
| **Weak / strong jump %** | how far it moves when it goes |
| **Weak / strong vol %** | how it behaves afterwards |
| **Lower / upper edge** | override the band for this session; blank uses the policy file |
| **Band weight %** | 100 is the pure band model, 0 is the plain smile, in between blends them |
| **Wing delta** | which wing the model is reported against |

Press **Apply**. The table underneath shows, for each tenor, the probability
the peg breaks, the probability the pair ends *outside* the band (a real
number, and smaller — a break does not guarantee it clears the edge), how far
the peg-intact distribution has to shift to keep the forward right, and the
risk reversal the band model gives against the lognormal one.

To actually **price** off it, choose **BAND** as the interpolation, here or on
the pricing tab. Two things it will refuse to do:

* It will not price a band without a **spot/forward feed** for the pair. A band
  is an absolute range (7.75–7.85) and the surface works in strike over
  forward, so it needs today's outright to place one. It says so rather than
  guessing.
* It will not work out the break risk for you from the quotes. A wider band
  body and a bigger break risk both raise the at-the-money by the same amount,
  so no quote can separate them. You mark it. (`volkit band PAIR
  --solve-hazard` will invert it from the wings if you want that, and tells you
  what the answer depended on.)

A band weight strictly between 0 and 100 averages two implied volatilities.
That is a marking convenience, not a model, and the screen says so every time.

### Exchange traded

For listed options — CME currency futures options and anything else with a
published strike ladder.

**+ Panel** creates one panel; one panel is **one expiry and one underlying**.
Make as many as you need. They are remembered between sessions, and refit as
you type unless you untick **auto-fit**.

Fill in the header, paste the market into the box on the left, and the fit,
the chart and the tables appear on the right.

| Field | What it is |
|---|---|
| Contract | the listed contract. Picking one sets the FX pair and whether its strikes are the reciprocal of that pair's. `custom` for anything not in the list |
| FX pair | override the pair the contract maps to. Leave blank to take the contract's |
| Strikes | *as quoted* or *reciprocal*. Leave on *(from contract)* unless you know better |
| Strike scale | multiplies pasted strikes before anything else. Use `1e-6` if your yen strikes come through as `6850` rather than `0.006850` |
| Expiry (UTC) | **with a time of day** — the exchange's own cut, not midnight, or you are fitting at the wrong maturity |
| Futures price | the forward the curve is fitted around, in the contract's quoted units. It is not taken from the FX feed: a listed future is its own market |
| Beta | SABR beta, 1 for lognormal. Leave at 1 if you want the comparison to be like for like — the book's smile is lognormal |
| Weighting | `vega` (default) makes a quarter of a vol point at the money count for more than a quarter of a point in a 5-delta wing, which is what the book actually feels. `equal` treats every strike alike; `table` uses a weight column from your paste |
| Book cut / Book interp | which marked surface to compare against |
| Vol unit | leave on `auto` unless the paste is refused as ambiguous |
| K col / Vol col | 1-based column numbers, if the headers are not recognised |

**The paste** takes tabs, commas or spaces, with or without a header row,
percent or decimal, and a bid/ask pair as well as a mid. Underneath the tables
you get a line for every choice the parser made and a line for every row it
threw away, with the reason. If a strike appears twice — the exchange lists a
call and a put at each — the out-of-the-money one is kept, because the
in-the-money one has little time value and its implied vol is noise.

**What you get**

* **Quotes vs fit vs mark** — every pasted strike with its market vol, the
  fitted vol, and the marked vol from the book, with both differences.
* **At the pair's delta strikes** — the book's own ATM, 25d and 10d strikes,
  put on the listed axis, with both curves read off them. The risk reversals
  and flies below are those same numbers differenced: a slope comparison at
  **fixed strikes**, not two quotes in two conventions. Labels follow the FX
  pair, so on an inverted contract a "call" there is a put on the future.
* **The header line** — alpha, rho, nu√t, the weighted RMSE in vol points and
  the worst single miss with the strike it is at.

**Read the warnings.** Three strikes and three parameters is an exact
interpolation, not a fit; the residuals will be zero whatever the quotes say
and the panel tells you so. Strikes that do not straddle the futures price
mean only one wing is being fitted. And if the fitted curve implies a negative
probability density anywhere, the strike range is named — do not price
anything in it off that curve.

### Analysis

Four questions asked of the whole tenor grid at once, for one pair. Each card
fills in on its own, so a missing forward feed empties one and leaves the rest.

Set the **pair**, **cut** and **interpolation** at the top, then:

| Control | What it does |
|---|---|
| Roll target | which point on the smile is rolled: the ATM, a 25 or 10 delta wing, or a risk reversal or butterfly |
| Horizon (days) | how far forward to roll. A tenor shorter than the horizon cannot be rolled and says so — drop to 7 days to see the front |
| Lookback (days) | the realized window. `match` uses each tenor's own length, which is the only like-for-like comparison there is |
| Annualise realized | `weighted` divides by the same volatility time the model uses. `calendar` and `count` are the naive alternatives, shown in the table anyway |
| Triangle noise floor | on by default; untick it for a faster cross triangle |
| Historical workbook | your own file of past spot / forwards / quotes. **Load history** reads it |

**Carry and rolldown.** Each tenor is revalued after the horizon at a **fixed
absolute strike** — the option you own keeps its strike while both the maturity
and the forward move under it. The roll splits into **term** (the slide along
the term structure at the same moneyness) and **smile** (the extra from the
forward moving under the strike). **Per yr** annualises the roll; **/atm** is
that as a fraction of the ATM level, which is the carry-to-vol ratio. Without a
forward feed for the pair the strike can only be held in moneyness, the smile
slide is zero by construction, and the row says so.

**Fair value.** What the implied would have to be to break even: buy the ATM
option, hold it for the horizon, delta hedge. You earn the realized volatility
through gamma and you take the roll on the mark. So

> **fair = realized + roll × multiplier**, and **rich = implied − fair**

Positive **rich** means the market is charging more than realized volatility and
the carry together justify. **of which fwd** is the part of the roll value the
forward curve caused rather than the term structure. The multiplier turns a
horizon-sized roll into a whole-life one; at long tenors and short horizons it
gets large, and the row warns you when it passes 20 because it multiplies any
interpolation error by the same factor.

**Realized against implied.** For each tenor, over the lookback:

* **Real %** on volatility time, **Cal %** on calendar days, **252 %** on a
  business-day count. They are not the same number and the first is the one
  that compares with implied.
* **Prem** is implied less realized — the volatility risk premium.
* **Skew d** and **Kurt d** are of the daily returns, **→T** is the same figure
  projected onto that tenor, and **Implied** is what the marked smile's own
  density says. Compare **→T** against **Implied**, never the daily one.
* **±se** is the standard error. A skew inside one standard error of zero is
  not a skew, and the panel says so in the messages.
* **ATM %ile** is where today's mark sits in its own history over the window.

**Cross triangle.** Only for crosses. Every quantity is shown as the cross's own
**mark**, what the two legs imply (**tri**), and the difference. The
at-the-money row also has an exact closed-form answer, printed underneath as
the variance triangle — the same expression the book itself is built on. The
risk reversal and butterfly have no exact answer from two marginals and a
correlation, so the legs' whole densities are combined under a Gaussian copula
at the marked correlation.

A difference shown as `~0` is inside the **noise floor**: the same machinery run
on each leg alone, where it should return exactly what it was given. Do not read
anything into a difference smaller than that. The line underneath also names the
gap between the two at-the-money triangles — that is the convexity of the legs'
own smiles, which the variance triangle leaves out, not a marking error.

**Curve comparison.** Any number of volatility curves side by side, with every
one differenced against whichever is marked **base**. Press **+ Curve** and
pick where each comes from:

| Source | What it is |
|---|---|
| *fitted surface* | the curve as the book has it now, at the cut and interpolation above |
| *workbook quotes* | what the sheet says: the marked ATM curve and the quoted risk reversals and market strangles |
| *historical workbook* | one dated row of the history file |
| *pasted curve* | anything you type in |

Load the history file first (the box at the top right of the tab) and then add
several *historical workbook* rows with different dates: that is the same curve
on different days, which is what this is mostly for. A **Date** takes `latest`,
a date like `2024-01-15`, or an offset back from the last row — `-30d`, `-3m`.
Weekends and holidays have no rows, so it uses the last row **on or before**
the date you asked for and tells you which day it landed on.

A **pasted curve** is one line per tenor, in the order of the **Show** menu and
everything after the first optional:

```
1M 8.20 -0.35 0.22 -0.60 0.75
3M 8.45 -0.40 0.24
6M 8.60
```

The unit is decided once from the at-the-money column, and a paste with some
lines in points and some in decimals is refused rather than guessed.

**Show** picks which of the five numbers the chart and the table display. A
blank cell means that source does not quote that tenor — it is not a zero — and
a curve that could not be built at all stays in the list with the reason
against it. One thing to watch: a historical sheet's butterfly column is
whatever that desk quoted, while the book's is a market strangle. The panel
reminds you; it cannot check it for you.

The historical workbook is **one sheet per pair, one row per date**. Column
headers are read for meaning rather than by position, so `ATM 1M`, `1m atm vol`,
`RR25 3M`, `3M 25d rr` and `1M 10d fly` all land in the right place. Forward
**points** and forward **outrights** are both accepted; points are turned into
outrights using the pair's own pip divisor. Anything that cannot be understood is
listed under the status line rather than dropped. `files/history_sample.xlsx` is
a synthetic example of the layout — it is never loaded for you.

### Market maker

The other tabs tell you what something is worth. This one tells you what to
show. It runs in three stages and each reports separately, so one that cannot
run leaves the others alone.

**Paste the market** into the left-hand box, one quote a line, in the shorthand
it arrives in:

```
1M ATM 8.20/8.60 in 100mm vega
3M 25d RR 0.35/0.55 eur call over
2M 25d fly 0.20/0.28
1Y 10d strangle 0.55/0.70
6M 1.1000 call 7.90/8.40
1M/3M ATM spread 0.30/0.55
2M atm 8.35 mid
```

It reads at-the-monies, risk reversals, butterflies, strangles, outrights by
strike or by delta, and calendar spreads of any of them. Sizes (`in 100mm
vega`, `x 50mm`) and direction words (`EUR call over`, `JPY put over`) are
picked up. Anything it cannot use is listed underneath with the reason —
nothing is dropped quietly, and nothing ambiguous is guessed:

* the **volatility unit** is decided once from the whole paste's at-the-money
  and outright lines. A paste that has some in points and some in decimals is
  refused rather than read line by line — `0.35` is an ordinary risk reversal
  in points and an ordinary at-the-money in decimals, and letting the risk
  reversal decide returns it a hundred times too large;
* a **risk reversal** written with a direction word is resolved against the
  pair (`JPY call over` on USDJPY is a dollar *put* over, so it is negative
  here). One written without a direction word is read in the book's own
  convention — base-currency call over is positive — and a note says so;
* an unqualified **`fly`** means whichever butterfly the selector at the top
  says. The default is the **market strangle**, which is what the workbook
  marks. Write `strangle` or `smile fly` to pin it on the line itself.

**Stage 1 — the at-the-money curve** is fitted to a target term structure. Pick
where the target comes from:

| Target ATM curve | What it uses |
|---|---|
| `overwrites` | the tenors you pinned on the **Vol marking** tab. Mark the levels you want, then fit a smooth curve through them |
| `quotes` | the mid of the at-the-money quotes in the paste |
| `paste` | a `tenor level` list you type into the box that appears |
| `current` | the curve as it stands — a check on the fit itself, which should change nothing |
| `none` | leave the level alone |

Tick the parameters the fit may move under **What the fit may move**. Anything
unticked is left exactly where it is. A curve cannot be fitted with more free
parameters than the target has points, and it will say so rather than produce
an arbitrary answer. For a **cross** the level comes from its legs, so what is
fitted is the correlation term structure — the tick boxes change to match.

**Stage 2 — the wings.** The four smile parameters are moved until the quoted
risk reversals, butterflies and outrights sit inside their markets. There is
**no penalty anywhere inside a bid and offer** — the point is that your mid
falls inside the market, not on top of somebody's mid — and the distance to the
nearer side outside it. The shift applies across the whole curve, so a broker
run changes the level of a wing without flattening its term structure; where
one shift cannot satisfy two tenors that disagree, it says which quotes it
could not reach instead of bending the surface to one of them.

Only parameters the paste can actually inform are moved: a 25-delta quote reads
the 25-delta anchor, and freeing the 10-delta pair as well would not inform
them. The panel says which it left alone and why.

**Stage 3 — the quote sheet.** For every line:

| Column | What it is |
|---|---|
| Their bid / ask | what you pasted |
| Model | where the surface is after the fit |
| Moved | how far the fit moved it |
| Fair / Axe / Bank | the three things shading the mid, each separately |
| Skew | their total, capped; a `*` means the cap bound |
| Our bid / ask | the mid plus the shading, with the bank's width round it |
| Width | the width, and underneath, the rule that set it |
| Verdict | in line, our mid above or below theirs, or through their price |

**Shading the mid.** Two things lean it, and both lean the same way. A market
that is **rich** against the fair-value screen and a **long vega position** are
both reasons to want to sell, and you attract a seller's trade by shading down.
Load a historical workbook on the **Analysis** tab to turn the fair-value lean
on; paste a `tenor vega` profile and set an **axe scale** — the position that
counts as a full axe — to turn the position lean on. Both are capped at
**skew cap** times the half width, so an axe can lean a price inside the market
but never walk it out of one.

Neither is applied to a risk reversal or a butterfly. A break-even against
realized volatility and a vega position are both statements about the *level*;
neither says where the skew should be marked, and those rows say so.

**Nothing here touches the workbook.** The fit reports and then puts the marks
back. Tick **keep the marks** to leave them on the loaded book — still in
memory only, and **Reload workbook** discards them.

### The knowledge bank

The bottom card is the pair's own file of desk knowledge, kept in
`mm_knowledge.json` beside the workbook. Four kinds of entry:

| Kind | Effect |
|---|---|
| `spread` | sets the bid-offer width, in volatility points |
| `floor` | a minimum width. Every matching floor applies and the widest wins |
| `shift` | moves the mid by a signed offset |
| `note` | prose. Shown beside the quote and **never** applied |

Every condition is optional — instrument, exact tenor, a day range, a size
ceiling and its basis, a delta — and the **narrowest matching rule wins**, with
later rules breaking ties. So `spread 0.25 on ATM under a month` and
`spread 0.45 on ATM under a month in up to 200mm` coexist, and the second one
wins in size. Each quoted row names the rule that priced it and lists the ones
that matched but lost.

**There is no default width.** A quote no rule matches gets no bid and no offer
and says so. Type a **fallback width** at the top if you want one anyway; the
row will say it came from the fallback rather than from a rule.

**Learn widths from this paste** proposes a ladder measured from the widths the
market actually showed — the median of what was on the screen, bucketed by
tenor, with the count and the range in its note. Quotes written as a single mid
have no width and are left out. It only *proposes*: look at them, edit them,
then **Save bank**.

---

## 3. Events

A bump is quoted in **vol points over the 24 hours following the release** —
what you mean by "FOMC adds two vols". The **Vol day** column shows which
volatility day the effect mostly lands in; the day rolls at the NY cut, so a
late release straddles two.

Auto-loaded dates come from two places, neither needing a network:

* **Rules** — US non-farm payrolls is the first Friday at 08:30 New York,
  shifting a week when that Friday is a holiday.
* **A published table** — `econ_events.csv` carries FOMC, ECB, BoE and BoJ
  dates. **Verify these**: central banks publish provisionally, and BoJ's
  calendar is only partly filled. Edit the file to extend it.

Release times are stored in the releasing body's local zone, so NFP is 13:30Z
in winter and 12:30Z in summer automatically.

Warnings you may see, and what they mean:

| Warning | Meaning |
|---|---|
| *before the valuation time … skipped* | the event has already happened |
| *falls inside the weekly market closure* | it is dated on a weekend — almost always a typo |
| *falls on a … holiday* | the market is shut that day for this pair |
| *event heights did not settle* | events are too close together to separate |

---

## 4. Data files

**`vol_marks.xlsx`** — the workbook, unchanged from before.
`CONFIG` lists base pairs, crosses and their legs, and the tenor points.
`PARAMS` holds one column per pair: `initial`, `long term`, `ratevol`,
`addon`, `MR`, `rate corr`, `short decay`, then one row per event date.
One sheet per pair holds `expiry, ST 10D, ST 25D, RR 25D, RR 10D`.
Everything is in **vol points**. For a **cross**, the `initial` / `long term` /
`MR` cells mean correlation initial / final / decay.

**`market_feed.csv`** — `pair,tenor,value`, where tenor `SPOT` is the spot rate
and anything else is the forward points at that pillar:

```
USDJPY,SPOT,150.25
USDJPY,1M,-11.4
USDJPY,3M,-35.0
```

Points are interpolated linearly in time between pillars, scaled to zero at the
very front, and held flat beyond the last pillar with an `extrapolated` flag.

**`vol_history.xlsx`** — what the market *did*, for the Analysis tab. One sheet
per pair (the sheet name is matched to a pair, so `EURUSD`, `EUR/USD` and
`GBPUSD Curncy` all work), one row per past date, and a date column first or
named `Date`.

Column headers are read for **meaning, not position**, so all of these land in
the right place:

```
Date  Spot  Fwd 1M  3M swap points  ATM 1M  1m atm vol  RR25 3M  3M 25d rr  BF 10d 6M
```

* `spot` / `px` / `close` / `last` / `fix` → the spot rate
* `fwd` / `forward` / `outright` → an outright forward at that tenor
* `pts` / `points` / `swap` → forward **points**, turned into outrights using
  the pair's own pip divisor
* `atm` / `vol` / `iv` / `sigma` → the at-the-money volatility
* `rr` → risk reversal, `bf` / `fly` / `str` / `strangle` → butterfly

A number in the header is read as a **delta** when it is one a desk quotes
(5, 10, 15, 20, 25, 30, 35, 40, 45) and as a tenor otherwise — so `RR 10d 1M` is
a ten-delta risk reversal at one month, not a ten-*day* one. A risk reversal or
butterfly with no delta in its header defaults to 25 and says so.

The **volatility unit is decided once per sheet from the ATM column** and then
applied to the risk reversals and butterflies. Sniffing each column on its own
gets a −0.89 vol point risk reversal wrong, because that looks exactly like a
decimal. Override it if a sheet has no ATM column.

Any column that cannot be understood is listed under the status line rather
than dropped, so a series that has gone missing is visible.
`files/history_sample.xlsx` is a synthetic example of the layout — regenerate it
with `python3 files/make_history_sample.py`. It is never loaded for you.

**`bands.csv`** — `pair,lower,upper,note` for defended pegs. Only put a pair
here if the range is genuinely defended.

**`holiday_overrides.csv`** — `country,date[,remove]`. Lunar-calendar holidays
(Chinese New Year and similar) must be listed here; they cannot be derived.

---

## 5. Command line

Every screen has a command-line equivalent. Options work before or after the
subcommand.

```
volkit check                              validate the workbook, list every problem
volkit serve --feed market_feed.csv       run the interface
volkit tenors USDJPY --cut TK             ATM term structure
volkit smile  USDJPY 2026-11-23           the smile at one expiry
volkit vol    USDJPY 2026-11-23 --strike 152 --forward 149.9
volkit daily  USDJPY --horizon 1 --out USDJPY_daily_vol
volkit events USDJPY --horizon 1          what auto-load would pull in
volkit validate USDJPY                    hunt for competing smile calibrations
volkit listed 6J --expiry "2026-09-11 19:00" --forward 0.0068 --file quotes.txt
                                          fit a listed strike/vol table and compare it
volkit analysis EURJPY --history vol_history.xlsx --horizon 7
                                          carry and roll, realized vs implied, fair value, triangle
volkit analysis EURUSD --history vol_history.xlsx --compare surface --compare history:-30d
                                          the curve comparison panel, as a table
volkit band USDHKD --feed market_feed.csv --hazard 3
                                          the managed-band read-out for a pegged pair
volkit mm EURUSD --target-source quotes --fallback-spread 0.3 < run.txt
                                          fit the curve, tune the wings, print the quote sheet
volkit mm EURUSD --vega position.txt --axe-scale 500 --history vol_history.xlsx < run.txt
                                          the same, with both leans on
volkit mm EURUSD --learn < run.txt        propose bank widths from the paste; --save writes them
```

Add `--asof "2026-08-23 12:00"` to price against a fixed valuation time.
Without it, the current UTC time is taken once at startup and held.

Three options are read before anything else, because they decide what the rest
of the command line may contain:

```
volkit --enable-tab mm ...      turn on a screen this build hides
volkit --config other.cfg ...   read a named settings file
volkit --no-config ...          read none
```

---

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *time zone database is unavailable* | Windows has no IANA database. `pip install tzdata`, or use the packaged exe which bundles it |
| *workbook not found* | pass `-w path\to\vol_marks.xlsx` |
| Console flashes and closes | run it from a terminal to read the error, or check the message before the "Press Enter" prompt |
| *ATM volatility is zero* | the expiry is today or in the past. Same-day expiries have no whole volatility days and cannot be quoted on this basis |
| *lies outside the managed band* | you priced a pegged pair outside its band; the lognormal smile is not valid there. Price it with the **BAND** interpolation |
| *the band is the absolute range … and this smile is being read in moneyness* | the BAND model needs an outright forward to place the band. Load a feed (`--feed market_feed.csv`) |
| *the band … is N% of the forward wide, so even a Beta on its edges reaches only …* | at that maturity the quoted ATM needs the peg to break. Raise the hazard or the jump sizes, or re-check the quote |
| *a hazard of …/yr … already implies an at-the-money of at least …* | the opposite: the marked break risk alone is worth more than the quote. Lower the hazard or the jumps |
| *has no managed band* | the BAND method only applies to pairs in `bands.csv` |
| *is hidden in this build* | the screen is there but off. Start with `--enable-tab NAME`, or add `enable-tab = NAME` to `volkit.cfg` |
| *was excluded from this build* | the screen is not there at all; nothing switches it on. You need a build that has it |
| *volkit.cfg could not be read: line N* | a settings line with no `=`. One setting a line, `name = value` |
| *unrecognized arguments: --frobnicate* | a misspelled setting name in `volkit.cfg`. The options are those of the command it names |
| *N distinct parameter sets reprice these quotes* | the smile is not uniquely determined. Run `volkit validate` |
| *only N% of its spike prices into…* | an event sits near the day roll (legacy event mode only) |
| A leg shows a red error | read it — errors are never replaced by a zero |
| *cannot tell whether these volatilities are percent or decimals* | the pasted table straddles 1.0. Set **Vol unit** rather than let it guess |
| *the level quotes straddle 1.0* | the same, on a pasted broker run. Fix the paste or set **Vol unit** |
| *the at-the-money levels straddle 1.0* | the same again, on a curve pasted into the comparison panel |
| *the historical workbook has no sheet for X* | that pair is not in the history file, so no dated curve can be read for it |
| *no width rule in the bank matches this quote* | add a rule in the knowledge bank, or type a **fallback width**. The tool will not invent one |
| *N wing quote(s) cannot determine M free smile parameter(s)* | untick smile parameters, or quote more of the smile |
| *is not a leg of EURUSD* | a direction word named a currency that is not in the pair |
| *offers below its own bid* | a truncated offer like `8.2/6`. Write it in full — repairing it means inventing the digits |
| *still outside their market after the fine tune* | one curve-wide shift cannot satisfy tenors that disagree. Re-mark those tenors on the **Vol marking** tab |
| *no usable strike/volatility rows* | the parser looked in the wrong columns. Set **K col** and **Vol col** |
| *three quotes and three parameters* | not an error: the listed fit is an exact interpolation, so its residuals mean nothing |
| *implies a negative risk-neutral density* | Hagan's formula arbitrages in the wings. Do not price the named strike range off that curve |
| *the horizon is longer than the tenor* | not an error: a 1W option cannot be rolled 30 days. Shorten the horizon |
| *no at-the-money column, so the volatility unit could not be determined* | the historical sheet has risk reversals but no ATM. Vol points were assumed; load with a different unit if that is wrong |
| *the roll is multiplied by N* | a long tenor from a short horizon. Lengthen the horizon to measure that tenor robustly |
| Realized volatility looks a tenth low | you are reading the **Cal %** or **252 %** column. Implied compares with **Real %** |
| Triangle differences all show `~0` | they are inside the noise floor, which means there is nothing there |
| Port already in use | `--port 8766` |

---

## 7. Things to know before trusting a number

* Premiums are undiscounted; there is no rate curve.
* Cut times resolve through their local time zone, so the NY cut is 15:00Z in
  winter and 14:00Z in summer. `dst_aware_cuts=False` restores the old fixed
  hours if you need to reconcile against historic marks.
* Cross pairs use the correct triangle sign, which differs from the previous
  tool for AUDJPY, EURJPY, EURCNH and GBPCNH. See `MIGRATION.md`.
* Auto-loaded central bank dates are provisional. Check them.
* For pegged pairs the probability of the peg breaking is a **marked input**,
  not something inferred from a butterfly. The probability of ending *outside*
  the band is an output, and it is never zero: the peg can break, and pretending
  otherwise is not a safer assumption, it is a wrong one. A band weight between
  0 and 100 blends two implied volatilities and is a marking convenience, not a
  model.
* A curve in the comparison panel is only as comparable as its source. The
  book's butterfly is a market strangle; a historical sheet's is whatever that
  desk quoted, and a pasted one is whatever you pasted.
* Realized volatility is annualised on **volatility time**, the same clock the
  model quotes implied on. A calendar year holds about 0.78 years of it, so the
  calendar and 252-day columns beside it are roughly a tenth lower by
  construction and are not the ones to compare against implied.
* The cross triangle for the risk reversal and butterfly assumes a Gaussian
  copula between the two legs and ignores the change of measure between their
  domestic currencies. Read it against the noise floor printed under it.
* Fair value is a first-order break-even, not a valuation. It assumes the
  surface does not move and inherits every assumption in the realized number.
* An exchange settlement volatility on an American-style option is not a
  European volatility, a future is not a forward once rates move with the
  underlying, and the exchange's settlement time is rarely an FX cut. The
  exchange-traded panel corrects for none of the three — a difference against
  the OTC mark is not by itself a mispricing.
