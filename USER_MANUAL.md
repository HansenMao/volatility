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

**None of these files is held open.** Each one is copied into memory and
closed before it is read, so a workbook loaded here can still be edited and
saved in Excel. (An earlier build kept the reader alive after the load, and
Windows then refused to let Excel save the very sheet the tool had read.)

**Picking up a new feed on its own.** Tick **auto-load** on the pricing
toolbar and the tool re-reads the feed file whenever it is written. The same
switch can be set at startup:

```
volkit.exe serve --auto-reload          # look every 5 seconds
volkit.exe serve --auto-reload 30       # or every 30
```

or put `auto-reload = 30` in `volkit.cfg` for a double-click. It is **off**
unless you ask for it.

**Only the feed.** The workbook and the historical sheet are deliberately not
watched. Reloading the workbook throws away every mark you have made this
session — that is what a reload is for — so it stays on **Reload workbook**,
where you have to mean it. The historical sheet is a record of what happened,
not a market. The feed is the one that is republished all morning, and a price
quoted off a stale spot is simply wrong.

When auto-load is on, a pill beside **Reload workbook** says so, and
everything it does is written in a line under the header — nothing reloads
silently. The feed is read only once its write time has stopped moving, so one
caught half saved is picked up on the next look rather than read in pieces.
**Check the feed now** in that line does not wait: if you pressed it, you know
you have saved.

One thing it will not do on its own: if you have marked anything on the
**Vol marking** or **Market maker** tabs, a changed workbook is *held*. Those
marks live in memory and a reload throws them away, so it says the workbook
has changed and waits. Save the session, or press **Reload workbook now** in
that line to take the new workbook and lose them.

---

## 2. The tabs

They run **Pricing, Vol marking, Monitor, Exchange traded, Analysis, Market
maker**. Monitor sits second from the left, right behind Vol marking, because
those two are what a morning starts on: what has moved, and then what to do
about it.

A build does not always have all six. One can be made without any of them
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
| Expiry | a tenor — `1W`, `8d`, `3M` — resolved through spot and delivery on the pair's holiday calendar, or a date written any of the usual ways: `2026-09-15`, `15Sep26`, `15 Sep 2026`, `September 15, 2026`, `2026/09/15`, `9/15/2026`, `20260915`. However you type it, the box comes back holding the one standard date, so what is priced is what you can read |
| Strike | a number, `ATM`, or a delta — `25d`, `10dp`, `-25d`. A bare `25d` takes its wing from the option Type. Once the marks have solved it the box holds that absolute strike and its tooltip says what you asked for; type the request in again to solve it afresh |
| Barrier | level for touch products; the volatility is read *at the barrier* |
| Type | `C`, `P`, or `Auto` (call if the strike is above the forward). `Auto` comes back as the `C` or `P` it resolved to |
| Spot / Swap / Forward | three boxes holding one identity: `forward = spot + swap / pip`. All three are filled from the feed at this leg's own expiry, all three are yours to type over, and typing in any of them moves the third. The **outright** is what is priced |
| Notional / Side | millions of base (payout, for touch products). Buy or sell flips the signed amounts *and* which way an overhedge buffer is applied |
| Digital ramp % | width of the call spread replicating a digital, as % of strike |
| Overhedge / Buffer % | barrier shift: `extend`, `bend_front`, `bend_back` |

**You only see the rows your products use.** A vanilla has no barrier, no
ramp and no overhedge; a touch has no strike and no Type, because which side
it is on is decided by where its barrier sits against spot. The model reads
none of those fields for those products, so the grid does not offer them — a
box you can fill in that is then ignored is the same silent nothing as a
price that quietly comes back zero. A row that another column needs stays put
and shows a dot in the columns that do not, so two legs never look like the
same instrument. The result rows follow the same rule: **MC std error**
belongs to a simulated barrier, **Smile delta %** to a vanilla.

Every column is the **same fixed width** whatever is in it, so a long premium
in one leg cannot widen the leg beside it and shift the whole grid under your
cursor.

**The market boxes fill themselves.** Spot, the swap points and the outright
forward all come from the feed at the leg's own expiry, and they are shown
*greyed* while they are still the feed's numbers. Change the pair or the
expiry and they are re-read — the swap points are interpolated to the expiry,
so a forward left behind from the old one is simply the wrong forward. Type
over any of them and it turns black: it is yours from then on, it is priced
exactly as it stands, and nothing refills it except **Fill legs**. Empty the
box to hand it back to the feed — and moving the leg to another pair hands
all of them back on its own, because a level you marked by hand belongs to
the pair you marked it for.

The three are one identity, `forward = spot + swap / pip`, so two of them are
yours and the third follows: type a swap and the forward moves, type an
outright and the swap moves to match. While every box is still the feed's,
the forward shown is the feed's *own* published outright rather than the two
rounded boxes above it added up — they carry different precisions, and on a
cross the file quotes only through its legs the sum lands a digit off what
was published.

**The Results rows repeat none of the inputs.** What the pricer works out
goes back into the box you asked in, and it is then what is priced: the
expiry as the one standard date, `ATM` or `25d` as the absolute strike it
solved to on the marks, `Auto` as the `C` or `P` it turned out to be. There
is no second copy underneath, because one number in two places on one screen
is two places for it to disagree — and for spot and the forward it is worse,
since the box is the input and a row beneath it reads like an answer. The
consequence worth knowing: a delta strike does not quietly re-solve under a
mark that has moved, exactly as a tenor does not re-read on a later morning.
The strike box's tooltip says what it was asked as; type `25d` in again to
solve it against the marks as they now stand, and moving the leg to another
pair puts the request back for you.

A cross the file quotes only through its legs is filled from them — EURJPY
off EURUSD and USDJPY — by the same triangle that prices it, so a pair the
feed covers indirectly is no longer a pair you have to type a level into.

**Keeping spot current.** The feed is read from a file, and the file is
rewritten during the day. Four controls in the toolbar:

| Control | What it does |
|---|---|
| **Refresh spot** | re-reads the feed file and re-prices. Every box still holding the feed's number picks up whatever has just been published |
| **Fill legs** | the same, and then writes the feed over the levels you typed as well, at each leg's own expiry — this is what a leg with a hand-marked spot that has gone stale needs |
| **watch file** | checks every fifteen seconds whether the file has been rewritten, and marks the feed pill when it has. It only *tells* you; nothing is re-read |
| **auto-load** | re-reads the feed for you whenever it changes. Only the feed — the workbook stays on its button, because reloading it discards this session's marks (§1). Greyed out when no feed file is loaded |

Watching only *tells* you; **auto-load** is the opt-in that actually re-reads,
and **Fill legs** is the only thing that overwrites a number you typed. A leg
the feed has no pair for, or whose expiry will not resolve, is named in the
status line and left exactly as it was.

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

  When the **feed covers the pair**, the smile chart is drawn in real strikes:
  the axis, the point table and the density are scaled by that expiry's
  outright forward, and the spot and forward are printed at the top with a
  *feed* pill. Without a feed there is nothing honest to scale by, so it stays
  in `K/F` and the column says `Strike / fwd` instead of `Strike`. The smile
  is fitted in moneyness either way — this changes the axis, never a
  volatility.
* **Smile parameters** — per-tenor `slog25`, `slog10`, `rho25`, `rho10`. Type
  into a shaded cell and press Enter to overwrite; clear it to revert. The
  **anchor** tick box lives here, with the smile it anchors, rather than over
  the ATM table.
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

**Reload workbook** throws away every mark you have made and goes back to the
quotes on disk — but it leaves the **pair, cut, interpolation and chart
expiry** exactly as you had them. A reload that also moved you back to the
first pair in the book would be the tool changing something you did not ask it
to change.

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
the pricing tab. **BAND only appears for a pair that has a band** — there is
nothing for it to price on a free floater, and on a book with no pegged pair
in it at all the option is not in the list anywhere. If you point a leg that
was on a pegged pair at a free floater, its interpolation is put back to a
legal one rather than left showing one thing and sending another.

A **strike** outside the band is flagged too, not only a barrier: the warning
reads whichever level the payout depends on.

Two things it will refuse to do:

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
| Contract | the listed contract, **typed**. The codes volkit knows are offered as you type and bring their FX pair, their strike direction and their standard contract size with them; anything else is accepted as typed, is marked *typed* wherever it is shown, and takes those three from the boxes beside it. Nothing is guessed from the name. Two panels may share a code — the same contract at two expiries — and a position line then names the expiry too |
| FX pair | override the pair the contract maps to. Leave blank to take the contract's |
| Strikes | *as quoted* or *reciprocal*. Leave on *(from contract)* unless you know better |
| Strike scale | multiplies pasted strikes before anything else. Use `1e-6` if your yen strikes come through as `6850` rather than `0.006850` |
| Expiry (UTC) | **with a time of day** — the exchange's own cut, not midnight, or you are fitting at the wrong maturity |
| Futures price | the forward the curve is fitted around, in the contract's quoted units. It is not taken from the FX feed: a listed future is its own market |
| Beta | SABR beta, 1 for lognormal. Leave at 1 if you want the comparison to be like for like — the book's smile is lognormal |
| Contract size | how many units of the base currency one option covers. Blank takes the contract's standard — 125,000 for `6E`, 12,500,000 for `6J`. Only **Positions and risk** below uses it; the fit has no notion of size |
| Alpha / Rho / Nu (hold) | leave **blank** to fit them. Type a number and that parameter is held there and the others are fitted around it |
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
* **The header line** — alpha, rho, nu, nu√t, the weighted RMSE in vol points
  and the worst single miss with the strike it is at. A parameter you typed
  rather than fitted is marked **held** where it is shown.

**Holding a parameter.** The three boxes *Alpha*, *Rho* and *Nu* are blank by
default and the fit decides all three. Type into one and it is held exactly
there: the sweep never visits another value of it and the remaining
parameters are fitted around it. That is the difference the residuals then
report — the best the free parameters can do **at** that value, not the best
SABR through the quotes.

* **Hold fit** puts the fitted alpha, rho and nu into the boxes, where you can
  nudge them. What is held is what the boxes show, to the digits they show.
* **Release** empties all three and fits everything again.
* Hold all three and nothing is fitted at all. The panel says so rather than
  reporting a convergence it never attempted: the curve is the one you typed
  and the table shows what it costs against the quotes. Useful for carrying
  yesterday's curve onto today's market, or for pricing a strike ladder off a
  wing you have a view on.
* The three-strike rule follows the *free* parameters, not SABR: with two
  parameters held, two quotes are enough.

**Read the warnings.** Three strikes and three parameters is an exact
interpolation, not a fit; the residuals will be zero whatever the quotes say
and the panel tells you so. Strikes that do not straddle the futures price
mean only one wing is being fitted. And if the fitted curve implies a negative
probability density anywhere, the strike range is named — do not price
anything in it off that curve.

#### Positions and risk

Underneath the panels is a box for what you own. One line is one option:

```
6E, 2026-09-11 19:00, 1.0900, C, 25
6E, 2026-09-11 19:00, 1.0600, P, -40
```

**contract, expiry, strike, C/P, contracts.** Drop the leading columns when
only one panel can answer the line (`1.0900, C, 25`), or put a header row on
it and name the columns in any order. `ATM` as the strike is that panel's own
forward. A quantity is signed — negative is short. Press **Aggregate**;
**Example** fills the box from the panels you already have.

A comma is a column boundary, so a size written `1,000` in a comma-separated
paste is two columns and the line is refused rather than read as 1. Use tabs
or spaces if you write sizes that way.

**Each line is priced against the panel above that it names**, and everything
the greeks need — the forward, the expiry, the volatility at that strike — is
that panel's. The one thing a fit never needed is the **contract size**, which
is why it is a field on the panel rather than here. A line that matches no
panel, or matches two, keeps its place in the table and says which; nothing is
guessed, because a position priced against the wrong month's curve looks
perfectly ordinary.

**Two sets of greeks**, one under the other:

* **Black–Scholes** — the textbook Black-76 sensitivity at the option's own
  volatility, with that volatility **held fixed** as the future moves. This is
  the number the exchange's own risk file will agree with.
* **Smile** — the same position revalued on the fitted curve, so the curve
  travels with the future, and volatility bumped by lifting the whole curve.

Both read the same volatility at the same strike, so the **premiums are
identical** and the whole difference between the two blocks is in the
sensitivities. An option struck at the futures price has the same vega in
both, by construction.

| Column | What it is |
|---|---|
| Δ fut | how many futures the position is equivalent to. Sell this many to be flat |
| Δ 1% $ | money the position makes on a +1% move in the future |
| Γ fut | how much *Δ fut* changes per 1.00 of the future |
| Γ 1% $ | the curvature over that same 1% move, in money |
| vega $ | money per **vol bump** — one volatility point unless you change the box |
| theta $ | money over the **theta window** — one day unless you change the box |
| vanna $ | how much *Δ 1% $* changes per vol bump |
| volga $ | how much *vega $* changes per vol bump |

**What adds up**, and it is three rows because three different things add.

* **Per panel** — every column adds. One row per panel that has a position on
  it.
* **Per contract** — every column still adds, including *Δ fut* and *Γ fut*:
  the panels under one code are the same contract at different expiries. Shown
  only where a contract has more than one panel, so it never repeats the row
  above it. Two delivery months are not literally the same future, so that
  futures total is a **net position, not a hedge ratio** — September euro
  against December euro is a calendar spread.
* **Across contracts** — only money adds: premium, vega, theta and the 1%
  columns, and only across contracts settling in the **same currency**. Every
  CME FX option settles in US dollars, so on an ordinary screen that is one
  *all · USD* row. A contract you typed need not, and its settlement currency
  is derived from its pair and its strike direction; when two differ there is
  no all-in row, only one row per currency, and the screen says so. *Δ fut*
  and *Γ fut* are never totalled across contracts — a euro future is not a yen
  future — so the *all* row leaves them blank.

Premiums and theta are undiscounted forward values, as everywhere else here.

```
volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --positions book.txt --theta-days 3
```

The command has one panel, which is enough for a book on one contract. To
reproduce a screen holding several, save the other panels as a JSON list in
the shape the screen posts and pass `--panels`; the panel built from the
arguments is the first of them.

```
volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --panels other_panels.json --positions book.txt
```

### Analysis

Six questions asked of the whole tenor grid at once, for one pair. Each card
fills in on its own, so a missing forward feed empties one and leaves the rest.

Set the **pair**, **cut** and **interpolation** at the top, then:

| Control | What it does |
|---|---|
| Roll target | which point on the smile is rolled: the ATM, a 25 or 10 delta wing, or a risk reversal or butterfly |
| Horizon (days) | how far forward to roll. A tenor shorter than the horizon cannot be rolled and says so — drop to 7 days to see the front |
| Lookback (days) | the realized window. `match` uses each tenor's own length, which is the only like-for-like comparison there is |
| Annualise realized | `weighted` divides by the same volatility time the model uses. `calendar` and `count` are the naive alternatives, shown in the table anyway |
| Realized on | `forward` (the default) measures what the option's own underlying did, swap-point moves included, wherever the sheet quotes them. `spot` measures spot alone |
| Triangle noise floor | on by default; untick it for a faster cross triangle |
| Wings as ρ / ν | reads the marked risk reversal and butterfly as a spot/volatility correlation and a vol of vol, against the same two measured from the history. Off by default — it is a fit per tenor |
| ρ/ν wings | which wings to read that shape off: 25 or 10 delta |
| History window (days) | how far back the relative-value grid measures each cell's own mean and how much it moves. Not the realized lookback: a month of a one-month volatility is far too short to say how much it moves |
| Cross triangle | include the legs' view of a cross in the relative-value score. Untick it for a faster grid |
| Weights | what each relative-value signal is worth before renormalisation. **Score** re-runs, **Reset weights** puts the defaults back |
| Historical workbook | your own file of past spot / forwards / quotes. **Load history** reads it |

**Relative value.** The top card, and the short answer the cards below it are
the working for: every expiry against every strike — 10 delta put, 25 delta put,
ATM, 25 delta call, 10 delta call — with one number per cell. **Positive is
rich**: the mark is above what the comparison says it should be, and that is
the side to sell. The colour is the same reading as the number and never the
only one; it saturates at two standard deviations.

The big figure in each cell is the **score**, in standard deviations of *that
cell's own* volatility. The small figure under it is the same reading in
**volatility points**. Click any cell and the panel beside the grid takes it
apart: what each signal said, in volatility points and as a z, what weight it
carried, and — for any signal that did not count — why not.

Five signals go into it:

| Signal | What it compares |
|---|---|
| level | the marked ATM against what actually realized |
| shape | your smile's shape at this strike against the shape the *measured* ρ and ν imply — the wings against what the volatility actually did, not against another quote |
| carry | minus the roll and the forward carry, as the volatility they are worth: an option that rolls down has to be cheaper to break even, so a mark that did not fall is rich by the difference |
| history | where this cell's volatility sits in its own recent history |
| triangle | on a cross only: your mark against what the two legs imply |

The first three are the fair-value break-even you already know, moved from the
at-the-money out to a strike, so they **add up** — `level + shape + carry` is
the volatility-point figure in the cell, and on the ATM column it is exactly
the **rich** column of the fair-value card below. The other two answer a
different question and are not added in; they are averaged with the first three
as z-scores instead.

Everything is standardised by **how much that cell's volatility usually
moves**, because half a volatility point is a great deal on a one-year ATM and
nothing on a one-week 10 delta wing. That measurement has its own window
(**History window**, a year by default) rather than the realized lookback,
which is matched to each tenor: a month of a smooth one-month series is far too
short to say how much anything moves.

The ρ and ν behind **shape** are measured on their own window too, and for the
same reason — they are properties of the market's behaviour, not a forecast
over your horizon, and they need more paired days than a realized volatility
needs returns. Set the lookback to three weeks and they used to have nothing to
say at any tenor at all: the ATM showed a shape of `0.000` and all four wings
showed a dash, which looks like a signal that does not work rather than a
window that is too short. It no longer moves with the lookback.

**Shape at the at-the-money is `0.000` and does not count.** The at-the-money
*is* the level, so there is no shape there to be rich or cheap in. That zero is
a statement, not a measurement, so it is shown with its reason and left out of
the average — averaging it in dragged every ATM cell a fifth of the way toward
zero. It is why an ATM cell reads a lower **confidence** than the wings beside
it: it has one fewer signal available, and the number says so.

Three columns and a tag you will notice on the grid:

- The column between **Tenor** and the strikes is **level**, printed once
  because it is one number for the whole expiry — the marked at-the-money
  against what realized. It goes into all five cells, so it is one
  observation, not five agreeing ones. Anything marked `row` in the detail
  panel works the same way.
- A tenor tagged **carry** has a forward that has drifted more than 0.8
  standard deviations of its own volatility. Past that line you are mostly in
  a carry trade wearing an option's clothes, and the carry signal is most of
  what that row is telling you. Hover the tenor for the numbers, or read the
  `regime` block in `volkit analysis --relative-value`. Nothing is reweighted
  for you — the weights are yours, and a score that quietly changed its own
  recipe row by row would be unreadable.
- Click a cell and the detail panel now breaks the realized number apart:
  what **spot** did, what the **forward** did, and **fwd/spot**, the ratio of
  the two. That ratio is the only honest answer to "is the carry supporting
  this volatility". Near one, the swap points moved with spot and it makes no
  difference which you measure. Well above one, the points are carrying
  variance of their own and the level signal is worth a second look. The
  *level* of the carry — the `+4.2%/yr` beside it — tells you nothing about
  that on its own, which is exactly why it is not the column.

If a pair's carry is large against a realized volatility that is *also* low in
absolute terms, the grid says so once at the top: that is the shape of a
managed float, where the carry is paying for jump and devaluation risk rather
than for diffusion, and the level, shape and history signals all read a
volatility as the width of an ordinary lognormal. It takes both conditions on
purpose — a big rate differential alone describes USDJPY perfectly well, and
USDJPY is not managed. It is a reading of the numbers, not a ruling: a hard
defended band is policy and lives in `bands.csv`.

Read the **faded** cells with care — they were scored on less than half the
declared weight, which the detail panel spells out. A cell with no history at
all shows its volatility points and no score; that is the honest answer, not a
gap. A wing your sheet does not quote borrows the at-the-money's scale and says
so in the detail. On a cross, a triangle difference smaller than the triangle's
own noise floor is shown and not scored — the same rule the cross triangle card
follows.

The **weights** boxes are yours. They are renormalised over whatever a cell
actually has, so raising `carry` to 0.4 changes how much the roll matters
everywhere it was measured and changes nothing where it was not. **Reset
weights** puts them back. A weight that is not one of the five, or is not a
number, is refused rather than quietly ignored.

**Carry and rolldown.** Each tenor is revalued after the horizon at a **fixed
absolute strike** — the option you own keeps its strike while both the maturity
and the forward move under it. The roll splits into **term** (the slide along
the term structure at the same moneyness) and **smile** (the extra from the
forward moving under the strike). **Per yr** annualises the roll; **/atm** is
that as a fraction of the ATM level, which is the carry-to-vol ratio. Without a
forward feed for the pair the strike can only be held in moneyness, the smile
slide is zero by construction, and the row says so. A cross whose **legs** the
feed quotes has a forward either way — EURJPY out of EURUSD and USDJPY — and
the row names the two it was built from.

The forward curve pays twice, and the last five columns are the second time.
**Smile** above is the curve reaching your *mark*. **Carry** is the curve
reaching your *price*: the option is worth something at the forward it is
struck against, and that forward has rolled down its own curve. It is shown in
basis points of the forward, **in vols** is the same number over the position's
vega so you can read it beside the roll, **Δ** is the position's delta at the
fixed strike, and **fwd/yr** is the annualised roll-down of the forward — the
rate differential the swap points are quoting.

Whether you earn it depends on how you hedge, and this is the number itself,
not a detail. Hedge in the **outright forward to the option's own expiry** and
the hedge rolls down exactly as the option does: you earn the carry and pay it
away, net nothing. Hedge in **spot**, as a desk does, and nothing rolls on the
hedge side — you keep it. So read **carry** as what a spot-hedged book earns,
and equally as the cost of not hedging in the forward.

**The delta here is the smile delta, and that matters.** The whole table is a
fixed strike with the forward sliding under it, so the volatility that strike
is marked at moves too — and `dV/dF` carries `vega × dσ/dF` along with it. A
Black-Scholes delta holds the volatility still, which is not what your position
does. So there are three columns: **Δ smile** is what you are running, **Δ BS**
is the Black-Scholes reading, and **Skew** is the difference — the whole of
what the smile contributes.

On a skewed pair the gap is not small. A USDJPY 25 delta put runs at about
**0.17**, not 0.24. And the at-the-money straddle is delta neutral in the
**Δ BS** column *only*: it is long vega, the volatility moves with the forward,
and the skew leaves it several delta of real exposure. That is also the reason
the at-the-money row shows any carry at all — it is not a rounding artefact.
If you hedge the at-the-money row on its Black-Scholes zero you are running the
skew delta unhedged.

Both columns are `dV/dF` in the term currency, so **Δ BS × the forward move**
is the carry column and **Δ smile × the forward move** is the whole of what the
forward did to you. They are deliberately not the quoted premium-adjusted
delta, which is a hedge ratio in the other currency and does not turn a move in
the forward into money — so on USDJPY the 25 delta strike reads a little away
from 0.25 here, and that is correct.

A risk reversal shows its carry in basis points and leaves **in vols** blank,
because it has almost no net vega to divide by. Without a feed every carry
figure is blank rather than zero.

**Fair value.** What the implied would have to be to break even: buy the ATM
option, hold it for the horizon, delta hedge. You earn the realized volatility
through gamma and you take the roll on the mark. So

> **fair = realized + roll value + carry value**, and **rich = implied − fair**

Positive **rich** means the market is charging more than realized volatility and
the carry together justify. **of which fwd** is the part of the roll value the
forward curve caused rather than the term structure — the curve reaching your
mark. **Carry value** is the curve reaching your price, as the volatility it
takes to pay for it; the at-the-money is a delta-neutral straddle, so it is
small there and **of which fwd** carries the curve's real contribution. It is
computed anyway rather than left out, because a zero you can see was measured
is worth more than one you cannot. **On** says whether the realized number was
measured on the forward or on spot. The multiplier turns a
horizon-sized roll into a whole-life one; at long tenors and short horizons it
gets large, and the row warns you when it passes 20 because it multiplies any
interpolation error by the same factor.

**Realized against implied.** For each tenor, over the lookback:

* **Real %** on volatility time, **Cal %** on calendar days, **252 %** on a
  business-day count. They are not the same number and the first is the one
  that compares with implied.
* **On** is what the return series was: the *forward* to that tenor wherever
  the sheet quotes swap points, spot where it does not. A quoted volatility is
  the volatility of the forward you are struck against, so the swap points
  moving is realized volatility too — **Pts %** is that part alone and
  **Spot %** is the same window without it. The points *decaying* by a day of
  carry is not counted: that is a known slide, not a risk. On most of G10 the
  difference is a few hundredths of a point; on a high-carry or managed pair it
  is not.
* **Prem** is implied less realized — the volatility risk premium.
* **Skew d** and **Kurt d** are of the daily returns, **→T** is the same figure
  projected onto that tenor, and **Implied** is what the marked smile's own
  density says. Compare **→T** against **Implied**, never the daily one.
* **±se** is the standard error. A skew inside one standard error of zero is
  not a skew, and the panel says so in the messages.
* **ATM %ile** is where today's mark sits in its own history over the window.

**Wings as SABR shape.** The card above compares *moments*, and the risk
reversal and the butterfly cannot be compared that way: a quoted spread is not
a moment, and a realized third moment is not a risk reversal. What both sides
do share is the two numbers a SABR smile is built from — **ρ**, the
spot/volatility correlation a risk reversal is paid for, and **ν**, the vol of
vol a butterfly is paid for. Tick *wings as ρ / ν* and the card fills in:

* **ρ marked / ν marked** — the pair a SABR smile would need to show your
  quoted ATM, risk reversal and butterfly at that delta. Your surface is not
  SABR, so **fit err** says how far it is from any SABR smile at all, in
  volatility points. A large one means these two numbers describe your smile
  only loosely and the comparison is correspondingly loose.
* **ρ measured / ν measured** — the same two out of your history. ρ is the
  correlation of daily spot returns with daily moves in the quoted
  at-the-money; ν is how big those moves were, annualised on the same
  volatility time as everything else here. **±se** is the standard error, and a
  ρ inside one of them is not a ρ. **Measured on** says `quoted` when it used
  your at-the-money column and `rolling` when the sheet had none and it fell
  back to a rolling realized volatility — an average moves less than what it
  averages, so that ν is a floor. Beside it is the **window** it was measured
  over, which is deliberately not the realized lookback above: ρ and ν are
  properties of the market's behaviour, not a forecast over your horizon, and
  they need more paired days than a realized volatility needs returns. Read off
  a three-week lookback, this whole half of the card — and both **diff**
  columns with it — was blank at every tenor.
* **diff** is marked less measured: what you are charging for the wing against
  what the market delivered.

The marked half needs no history at all, so a tenor whose realized window is too
short to measure anything — the one week row always is, on a lookback matched
to the tenor — still shows its own ρ, ν and both differences. Its realized
columns above stay blank, with the reason.

SABR has no mean reversion and real volatility does, so **ν rises at short
tenors on both sides** — do not read the term structure of ν as a signal, and
do not average it across tenors. **ν√t** is the scale-free number that actually
sets the shape of the smile at that expiry, and it is the one that compares
across the curve.

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

**Vega split** is the one column here that is exact. It differentiates the
variance triangle rather than integrating anything, and reads *how much of each
leg one unit of at-the-money vega on the cross behaves like* — the two hedges.
Long 1 vega of AUDJPY at a split of `0.62 / 0.51` is long about 0.62 of AUDUSD
vega and 0.51 of USDJPY vega, and that is what to trade against it.

They are **not shares and do not add up to one**, and nothing is missing when
they do not: weighted by each leg's own volatility they account for the whole of
the cross's exactly. What the two legs cannot hedge is the correlation, and that
is the **per ρ** column — how many volatility points of the cross a *whole* unit
of correlation is worth, so a hundredth of it for a move of 0.01. If that number
is large, the cross's mark is a correlation view whatever you do in the legs.

The historical workbook is **one sheet per pair, one row per date**. Column
headers are read for meaning rather than by position, so `ATM 1M`, `1m atm vol`,
`RR25 3M`, `3M 25d rr` and `1M 10d fly` all land in the right place. Forward
**points** and forward **outrights** are both accepted; points are turned into
outrights using the pair's own pip divisor. Anything that cannot be understood is
listed under the status line rather than dropped. `files/history_sample.xlsx` is
a synthetic example of the layout — it is never loaded for you.

### Monitor

The tab to leave open: **what has moved**. Each small panel is one pair and two
points in time — the five quoted numbers as they are now, as they were then, and
the change between them, tenor by tenor. **+ Panel** adds one, **Copy** on a
panel duplicates it, and they are all remembered between sessions.

Each panel has two ends, **Was** and **Now**, and either can be:

| Source | What it is |
|---|---|
| *fitted surface* | the curve as the book has it now, at the cut and interpolation at the top of the tab |
| *workbook quotes* | what the sheet says — against the surface, this is the fit residual |
| *historical workbook* | one dated row of the history file |

The default is the surface against the historical row a week ago. Load the
history file first (the box at the top right) or the dated end has nothing to
read. A date takes `latest`, a date like `2024-01-15`, or an offset back from
the last row — `-1w`, `-30d`, `-3m`. Weekends and holidays have no rows, so it
uses the last row **on or before** the one you asked for and each end says which
day it landed on.

**Highlight** picks which of the five gets the *was* and *now* levels printed
beside the changes. The changes themselves are always all five, so a panel set
to at-the-money still shows you a wing that has moved.

Three things worth knowing:

* If both ends land on the **same row** — which is what happens when the history
  file has not been updated — the panel says so. Every change is then zero by
  construction, and a column of zeros otherwise reads as a quiet market.
* If one end cannot be built at all, the panel still shows what it could read
  and carries the reason it has no change. It does not go blank.
* A tenor one end does not quote is a **blank** change, not a missing row.

#### Curve comparison

Underneath the panels, on the same tab. Any number of volatility curves side by
side, with every one differenced against whichever is marked **base**. Press
**+ Curve** and pick where each comes from:

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

### Market maker

The other tabs tell you what something is worth. This one tells you what to
show. It runs in three stages and each reports separately, so one that cannot
run leaves the others alone — and behind **two buttons**, because fitting and
quoting are two different jobs.

**Fit** reads the market box, moves the curve and the wings to it, and prices
nothing. **Quote** reads the box below it, where you write what you are being
*asked* for, and makes a two-way in each line — and fits nothing, so it comes
back instantly and you can press it as often as the phone rings. A request does
not arrive with a broker run attached to it, which is why the two are separate:
you should not have to re-fit to a market that has nothing to do with the price
you are being asked to make.

**Which marks the price stands on.** With **quote on the fit** ticked, the
parameters the last fit arrived at go with the request and are used for that
one price. Untick it, or fit nothing at all, and the price stands on the marks
as they are. The line above the quote sheet says which of the two it was, every
time. Either way nothing is left on the book — that is **keep the marks**, on
the fit, and it is a separate decision.

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

**Or paste it as columns**, which is how a run out of a chat window or a
spreadsheet usually arrives — `expiry, strike, bid/offer`, with a timestamp in
front if you have one:

```
09:15, 1M, ATM,    8.20/8.60
09:15, 3M, 1.0900, 8.10/8.50
09:20, 2M, 25d,    8.00/8.40
09:41, 1M, ATM,    8.25/8.65
```

The middle column takes the same things the pricing tab's **Strike** box takes:
`ATM`, an absolute strike, or a delta (`25d`, `25dp`, `-25d`). Both shapes can
be in one paste; the parser does not care which line came first. A column
header pasted along with the run is recognised as one and passed over.

| Strike column | What it means |
|---|---|
| `ATM` | the at-the-money |
| `1.0900` | the volatility at that strike. No `call` or `put` needed — the volatility at a strike is one number either way — but it needs a **forward feed**, because the surface works in strike over forward. A cross the feed does not quote counts as covered when it quotes **both legs**: EURUSD and USDJPY in the file are an EURJPY forward, and the sheet says once which triangle it used |
| `25d` | the 25 delta **call**. A bare delta names two strikes, one on each wing, so it takes the call and the row says so |
| `25dp`, `-25d` | the 25 delta put |

One rule to know: **a comma is a column boundary, and a price never straddles
one**. That is what tells `3M, 7.75, 8.30` (a choice price at the 7.75 strike)
from `3M 7.75 8.30` (the two-way at-the-money). If you write columns, write the
commas.

**Timestamps and requotes.** A line may start with `09:15`, `2024-02-28 09:15`
or `[09:15]`. When two lines quote the *same thing*, **the later timestamp
wins** — whichever order they were pasted in, so a stale line pasted at the
bottom of the run cannot become the live market. Without timestamps to compare,
the later line wins, because that is the only ordering an untimed line carries.

The quote that lost is not thrown away. It is listed under the paste with the
line that beat it, so a mistyped update shows up as a quote that went missing
rather than as nothing at all. It also still counts when **Learn widths from
this paste** measures the market: one tenor quoted twice is one live price and
two observations of how wide that broker shows it.

A time-only line takes the last date written above it. A run with no date
anywhere is ordered as a single day and says so — that ordering is wrong across
midnight, and it would rather tell you than quietly get it backwards.

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

Stages 1 and 2 are the **Fit** button, and what it produces is the *market
against the surface* table on the right: their bid and offer, where the model
was, where it is now, and how far it moved. There is no price in it. Whether
the fit reached the market is the fit's own question.

**Stage 3 — the quote.** Write what you are being asked for into **What we are
asked for**, one instrument a line, with **no prices on them**:

```
1M ATM in 100mm
3M 25d RR
2M 25d fly
6M 1.1000 call
1M/3M ATM spread
3M 25d RR jpy call over
```

The same words as the market box with the price left off. A number that reads
as a market is refused with the line rather than taken as a strike — pasting a
broker run in here would otherwise quote you levels nobody asked about. A risk
reversal asked for as `JPY call over` is answered in **that** convention, sign
and sides both, and the row says so.

Press **Quote**, and for every line:

| Column | What it is |
|---|---|
| Model | the surface's own mid, on whichever marks the price is standing on |
| Fair / Axe / Bank | the three things shading the mid, each separately |
| Skew | their total, capped; a `*` means the cap bound |
| Our bid / ask | the mid plus the shading, with the bank's width round it |
| Width | the width, and underneath, the rule that set it |
| Their market | what the market box quoted for this same instrument, if it did |
| Verdict | quoted; or, when the market box has it too, in line, our mid above or below theirs, or through their price |

**If the market box quoted the same instrument, it appears beside our price**
and the verdict compares the two, so *inside their market* still works. If it
did not, the line is priced just the same — which is the whole reason for
asking separately.

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
back, and so does a quote made on them. Tick **keep the marks** to leave the
fit on the loaded book — still in memory only, and **Reload workbook** discards
them.

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

### Saving your marks

Nothing you do on the **Vol marking** or **Market maker** tab is written back to
`vol_marks.xlsx`. The workbook stays exactly as it was, and **Reload workbook**
throws away everything you have marked since you started. That is deliberate —
it is somebody else's spreadsheet — but it is not what you want at five o'clock.

The **Saved marks** card at the bottom of the Vol marking tab writes it all to a
separate file beside the workbook instead: curve parameters, a cross's
correlation, the event schedule, ATM and smile overwrites, the market maker's
wing shifts, the anchor switch and the band treatment.

| Button | What it does |
|---|---|
| **Save marks** | writes every pair on the book to the file named above it |
| **Save this pair** | only the pair the marking tab is showing |
| **Load marks** | puts a saved file back on the book and redraws everything |

The Market maker tab has **Save marks to file** in its top bar, which writes the
same file. It only works with **keep the marks** ticked: without it the fit is
put back before the panel returns, so there would be nothing of it on the book
to save.

Loading **replaces** rather than adds. Overwrites and events are cleared before
the saved ones go on, so applying the same file twice cannot double an event. A
pair in the file that this workbook does not build is reported; a pair the file
does not mention is left exactly as the workbook has it, and that is reported
too. If one pair's marks will not go on, the rest still do and the message names
the one that did not.

To start with a file already on: `volkit.exe --session marks.json`, or an
`session = marks.json` line in `volkit.cfg` beside the executable.

The file is plain JSON and readable. Volatility numbers in it are in
**volatility points**, exactly as the screen shows them.

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

**`vol_marks.xlsx`** — the workbook.

`CONFIG` is **two columns**: `PAIRS`, one pair a row, and `TENORS`, the tenor
points. Nothing else.

```
PAIRS    TENORS
USDJPY   1w
EURUSD   2w
EURJPY   1m
EURGBP   3m
```

A pair with the dollar on one side is marked on its own backbone. A pair
without one is a **cross**, and a cross is never marked directly: it is broken
into the two dollar pairs the market quotes — `EURJPY` into `EURUSD` and
`USDJPY`, `EURGBP` into `EURUSD` and `GBPUSD`, `EURCNH` into `EURUSD` and
`USDCNH` — and what you mark for it is the **correlation** between them. A leg
you did not list is added, because a cross cannot be built without both of
them. Everything that was worked out rather than read is reported: in the
message box at the top of the page, and by `volkit check`.

The old layout still loads. A `COR` column is read as more pairs, and a column
named after a cross still names that cross's two legs and wins over the derived
ones — a sheet that says something explicitly is not second-guessed by a
convention.

`PARAMS` holds one column per pair: `initial`, `long term`, `ratevol`,
`addon`, `MR`, `rate corr`, `short decay`, then one row per event date.
One sheet per pair holds `expiry, ST 10D, ST 25D, RR 25D, RR 10D`.
Everything is in **vol points**. For a **cross**, the `initial` / `long term` /
`MR` cells mean correlation initial / final / decay — that is what "marked by
correlation" means, and it is unchanged.

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

**`vol_session.json`** — your saved marks (see *Saving your marks* above).
Written by the tool, not by hand, though it is readable and can be edited if you
know what you are doing: volatility numbers in it are in **volatility points**,
and everything else is the raw number the field on screen carries. It is never
read unless you ask for it, by pressing **Load marks** or by starting with
`--session`.

---

## 5. Command line

Every screen has a command-line equivalent. Options work before or after the
subcommand.

```
volkit check                              validate the workbook, list every problem
volkit serve --feed market_feed.csv       run the interface
volkit serve --auto-reload 30             ... and re-read the market feed when it changes
volkit tenors USDJPY --cut TK             ATM term structure
volkit smile  USDJPY 2026-11-23           the smile at one expiry
volkit vol    USDJPY 2026-11-23 --strike 152 --forward 149.9
volkit daily  USDJPY --horizon 1 --out USDJPY_daily_vol
volkit events USDJPY --horizon 1          what auto-load would pull in
volkit validate USDJPY                    hunt for competing smile calibrations
volkit listed 6J --expiry "2026-09-11 19:00" --forward 0.0068 --file quotes.txt
                                          fit a listed strike/vol table and compare it
volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 --rho -0.2
                                          ... with rho held there and the rest fitted around it
volkit analysis EURJPY --history vol_history.xlsx --horizon 7
volkit analysis USDJPY --history vol_history.xlsx --sabr
volkit analysis EURJPY --history vol_history.xlsx --horizon 7 --relative-value
                                          carry and roll, realized vs implied, fair value, triangle
volkit monitor EURUSD --history vol_history.xlsx
                                          what has moved: one panel per pair, as a table
volkit monitor --watch EURUSD --watch USDJPY:history@-1m --history vol_history.xlsx
                                          two panels, the second against a month ago
volkit monitor EURUSD --compare surface --compare history:-30d
volkit mark propose EURUSD --target curve.txt --out p.json
                                          plan and run the marking fit, and save what it proposes
volkit mark record EURUSD --proposal p.json --verdict edited
                                          tell it what you did to that proposal
volkit mark learn EURUSD                  what it has worked out about how you mark
volkit mark confer EURUSD --archive mm_archive.jsonl
                                          let the two agents settle on a re-mark
volkit agent fetch --sdr sdr/ --days 5    download DTCC's public dissemination files
volkit agent fetch --sdr sdr/ --since 2025-09-01
                                          ... or backfill the 366 days DTCC keeps
volkit agent trades EURUSD --invert --history vol_history.xlsx
                                          what printed, and the volatility each premium implies
volkit agent ingest --chats chats/ --sdr sdr/
                                          read today's broker chats and SDR files into the archive
volkit agent watch --chats chats/ --every 30
                                          ... and keep reading them while you work
volkit agent evidence EURUSD              how wide this pair has been shown, and where
volkit agent learn EURUSD --save          turn that into knowledge-bank widths
volkit agent quote EURUSD --record        make a two-way and keep a record of it
volkit agent outcome EURUSD --ref ID --result traded_ask
                                          say what happened to a price you showed
                                          the curve comparison panel, as a table
volkit session marks.json                 save every mark on the book to a file
volkit session marks.json --load          put a saved file back
volkit session marks.json --show          print what a saved file holds
volkit band USDHKD --feed market_feed.csv --hazard 3
                                          the managed-band read-out for a pegged pair
volkit mm EURUSD --target-source quotes < run.txt
                                          fit the curve and the wings, and report
volkit mm EURUSD --file run.txt --request ask.txt --fallback-spread 0.3
                                          fit, then quote what is asked for off it
volkit mm EURUSD --request ask.txt --target-source none --fallback-spread 0.3
                                          quote off the marks as they stand, no fit
volkit mm EURUSD --request ask.txt --target-source none --vega position.txt \
    --axe-scale 500 --history vol_history.xlsx
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

`--session PATH` belongs to no one subcommand either: it puts a saved set of
marks on the book before anything is priced, so a batch job and the screen
agree.

```
volkit --session marks.json vol USDJPY 2026-11-23 --strike 152
volkit serve --session marks.json
```

---

## 5a. The desk agent

The agent is in two places: a **card inside the Market maker tab**, and a
command. The card answers one question about the market you have pasted --
*is the width I am about to show the width this thing actually trades at* --
and the command does everything else, including making a price when nobody
has shown you anything at all.

### The card in the Market maker tab

It sits under the quote sheet, and it runs only when you press **Suggest** --
it never moves on its own, and it fits nothing, so it answers in a moment
without touching your curve, your wings or your marks.

What it gives you, per quoted row: what the market showed, what you would
show, and what the archive says this has actually been shown at recently --
with two extra columns on the quote sheet itself so each suggestion sits
beside the quote it is about. The verdict on each row is one of:

| verdict | what it means |
|---|---|
| agrees | your width and the market's are the same thing |
| tight | you would show tighter than this has been shown |
| wide | you would show wider |
| no rule | nothing in the bank matches, and here is what the archive supports |
| thin | not enough in the archive to have an opinion |

**Nothing moves.** A row marked `tight` still quotes at your bank's width; the
card is telling you a rule is out of date, and the rule changes in the
Knowledge bank card below it, by you. The two extra quote-sheet columns
disappear the moment you edit the paste, because a suggestion computed from an
older market has no business sitting beside a newer quote.

Three buttons:

- **Suggest** -- run it against what is pasted above.
- **Fetch from DTCC** -- download the last few days of public dissemination
  files straight from DTCC into your SDR folder, and read them. The box beside
  it is how many days back; the screen is capped at 30, because a long
  backfill is a command with somebody watching it (`volkit agent fetch --since
  2025-09-01`). Start the server with `--sdr DIR` to say where they go, and
  `--proxy` if your desk sits behind one.
- **Scan folders** -- read whatever is new in the chat and SDR folders this
  server was started with (`volkit serve --chats chats/ --sdr sdr/`). If none
  were named, it says so; the folders are a command-line setting and not a box
  on the page, because a path a web page can name is a path anything reaching
  that page can read.
- **File this run** -- put the market you are looking at into the archive, so
  the next morning's comparison knows about it. Put the broker's name in the
  Broker box first: filing the same run twice under one name files it once,
  and under two names it counts twice, which is right when two brokers really
  showed it and is worth knowing when they did not. The card says which
  happened.

The four settings are the same ones the command line takes: **Half-life** (how
fast an observation stops counting -- five days means a quote is worth half
after a working week), **Min evidence** (how many age-weighted observations
before it will state a width at all), **Lookback**, and **Tolerance** (how far
apart your width and the market's have to be before it says anything, as a
fraction; there is also a floor of 0.02 so a narrow butterfly is not flagged
over four thousandths).

### Putting the archive under the quote

The card only compares. To let the archive *make* a width, tick **widths
from the archive** on the toolbar. The quote's width ladder is then: a bank
rule if one matches, else the width the archive has seen this shown at (when
it holds enough), else the fallback typed on the panel, else no price -- and
every row says which rung it stood on. The first three settings above are
what the quote reads too, so the card and the quote agree about what the
archive holds. It is off until you tick it, because a width the market showed
is evidence and not a rule; when you are happy with one, **Learn widths**
writes it into the bank and it stops needing the switch. The archive's
*level* -- where this has recently been quoted against where your mark is --
appears on the row as a flag and moves nothing.

### Trades that printed

```
volkit agent fetch --sdr sdr/ --days 5
```

That is DTCC's public price dissemination -- the anonymised FX option trades
the CFTC requires to be published. It is free and public. DTCC keeps **366
days** and has nothing before **29 December 2023**; a date outside that is
refused with the reason before anything is asked for, and there is no file for
a Saturday, which is reported as "nothing published" rather than as an error.
A date already in your folder is not downloaded again.

If your desk has no route to the internet, run the fetch on a machine that
does and drop the files in the folder -- reading them has never needed a
network. If it goes out through a proxy, `--proxy http://host:port`, or just
let it read `https_proxy` from your environment.

```
volkit agent trades EURUSD --invert --history vol_history.xlsx
```

This is the part worth having. Each printed premium becomes the volatility it
implies:

```
2024-02-26T14:05  87D 1.1 call traded at 7.050 (K/F 1.0299, 250mm)
     inverted from a premium of 1,018,517 USD on a notional of 250,000,000 EUR
     (premium in USD per unit of EUR), against a forward of 1.06804 from the
     2024-02-26 row of the historical sheet, the sheet quotes no forward at 87D;
     the carry was interpolated between the 1M and 3M pillars, undiscounted --
     this package carries no rate curve, so the volatility is a touch low
```

Every row says what it used, because unlike everything else in the archive
this number is *derived*: the premium is a fact, the volatility is a fact
about the premium **and** the forward it was inverted against.

Three things it will not do, and you should know why:

- **It will not use today's forward for a trade from three weeks ago.** The
  forward comes from that date's row of your historical workbook. No workbook,
  or no row within a week of the trade, and the trade is refused by name --
  inverting last month's premium against this morning's forward is wrong by
  the whole of the carry since, and wrong quietly.
- **It will not invert a capped notional.** DTCC publishes large sizes as the
  cap, so the premium per unit would be wrong by however much the cap hid.
- **It does not discount.** This tool has never carried a rate curve.
  Undiscounted, the volatility reads *low* -- about 4% of it on a one-year
  option at 4% rates, and nothing worth mentioning inside a month. Add
  `--discount-rate 0.04` to remove it.

One more: expiries are taken at midnight UTC, because the file publishes a
date and no cut. On a one-week trade that reads a touch high.

### The rest of the command

Making a price with no market in front of you, the record of what you showed,
and what became of it.

It keeps a file beside your workbook, `mm_archive.jsonl`, holding four kinds of
thing:

| kind | what it is | what it is evidence of |
|---|---|---|
| quote | a market somebody showed | where the market is, and how wide it is shown |
| trade | a print out of an SDR file | where business actually got done |
| shown | a price **you** made | nothing about the market -- the record of what you did |
| outcome | what became of one of your prices | whether your market was right |

**Filling it.** Drop broker chats into a folder and dissemination files into
another, then:

```
volkit agent ingest --chats chats/ --sdr sdr/
```

Every file is read once, by content -- copying a log to a new name does not
import it twice, and a folder scanned every thirty seconds all day does not
slowly invent confidence in a width. A chat has to say which pair it is: the
file name (`EURUSD_2026-08-20.txt`), or a line in the file that is nothing but
a pair name, which is also how a chat covering three pairs gets split into
three. A file naming none is skipped with that reason and is not retried until
you change it. `volkit agent watch --chats chats/ --every 30` does the same
thing on a loop.

**Chats that are not in the house format.** Lines the quote parser already
reads go straight in. What is left -- "eurusd 1m running 8.2 at 8.6, 100 vega a
side" -- is handed to a local model if you have one, which rewrites it into the
same format, which the parser then has to accept. Anything it writes that the
parser refuses is refused and shown to you. Anything it writes containing a
number that was not in the chat is refused whole, and shown to you. Records
that came in this way are marked, and `--no-model-read` recomputes everything
without them, so you can compare the two before you trust it.

If you have no model running, nothing breaks: the house-format lines are still
read, everything downstream still works, and each command prints one line
saying there was no model.

**What it makes of it.**

```
volkit agent evidence EURUSD
```

Widths, ages, sources and where the market has been, per instrument and tenor
bucket. Two rules to know: a quote counts half after five days (`--half-life`),
and below two age-weighted observations (`--min-evidence`) you get "not enough"
rather than a number.

```
volkit agent learn EURUSD --save
```

Turns the widths with enough behind them into knowledge-bank rules, each
carrying the evidence in its text. Without `--save` it only proposes.

**The price.**

```
echo "1M ATM in 100mm vega
3M 25d RR
2M 25d fly" | volkit agent quote EURUSD --record
```

One instrument a line, no prices on them -- a line with a two-way on it is a
market somebody showed and belongs on the market-maker tab. What comes back,
for each:

```
EURUSD 1M ATM in 100mm vega: showing 5.525/6.075
  model mid: 5.887 vol points, the marked surface (SVI, NY cut) -- 1M is 30.4 days
  market level: 5.900 vol points, the archive -- last quoted 5.900 (today) ...  (not applied)
  width: 0.550 vol points, the bank: spread 0.550 on 31d ATM <=150mm vega
  floor: 0.300 vol points, the bank: floor 0.300 on anything -- already above it  (not applied)
  beaten rule: spread 0.350 on 31d ATM  (not applied)
  shading, fair value: +0.000 -- nothing shades this row
  shading, position: -0.138 -- the position at this tenor is +1.25 of a full axe
  shift, bank: +0.050 -- the bank: shift +0.050 on ATM
  our record: 5 price(s) shown, 5 answered, 100% traded, 0 on the bid, 5 on the offer  (not applied)
  mid: 5.800 -- 5.887 -0.088
  bid / offer: 5.525 / 6.075
  flag: 5 of 5 answered prices were lifted against 0 hit; the offer may be the
        cheap side -- shown here, and applied to nothing
  advice: check the ECB date before showing anything past it
```

The list is the explanation. The lines marked `(not applied)` are things you
should see that changed nothing, and they are there deliberately: a floor that
did not bind, a rule a more specific one beat, where the market has been, and
your own hit rate. If a local model is running it also writes two or three
sentences under the list -- and if that paragraph contains a number the list
does not, it is thrown away and you get the list, which is the worst that
happens.

**Where the width comes from**, in order: a rule in your knowledge bank; then
the archive, if enough recent quotes support one, and the row says so and
offers to write it in; then `--fallback-spread` if you typed one; then **no
price at all**, and the reason. There is no built-in default width anywhere in
this tool.

**What the agent will not do.** It will not move the mid onto the level the
archive has seen. Where the market has been is shown beside your mark and, when
the two disagree by enough to matter, flagged -- but a mid that follows the last
thing you were shown is a mid being led by whoever is about to trade with you.
If the flag is right, re-mark the surface on the marking tab. It also will not
turn your hit rate into a shift: a run of lifted offers is sometimes a mid
that is too low and sometimes a week of being the only one showing, and the
tool cannot tell those apart. It tells you which run you are having.

**Closing the loop.** `--record` writes the prices a run made into the archive
with the mid the model had at the time. When you find out what happened:

```
volkit agent archive EURUSD --kind shown        # find the id
volkit agent outcome EURUSD --ref 04d9d74f78596569 --result traded_ask
```

Results are `traded_bid`, `traded_ask`, `passed`, `missed`, `pulled` and
`done_away` (add `--away 8.35` when you know the level it went at). That is
what feeds the "our record" line and its flag next time.

**Pricing a past morning.** `--asof "2026-08-20 09:00"` values everything at
that instant *and* reads the archive only up to it, saying how many later
observations it left out. Nothing the agent shows you for a past date can have
been computed from what happened afterwards.

## 5b. The marking agent

A second agent, answering a different question. The desk agent asks *what do I
show*; this one asks *where should the surface be*.

It does **not** replace the fit on the marking tab. That fit is fine. What it
does is the judgement around it -- the part you currently do by hand every
morning:

- which knobs to leave free and which to pin;
- whether the targets you have can actually determine that many parameters;
- whether anything you were shown constrains the wings at all;
- and then, once the fit has run, whether to take the number or nudge it.

The last one is what it learns.

### Proposing

```
volkit mark propose EURUSD --target curve.txt --out proposal.json
```

`curve.txt` is `tenor vol` lines. What comes back is the plan, the fit, and
every knob that moved:

```
EURUSD: 1 knob(s)
  plan   pin initial_vol: pinned  [learned] this desk has not moved it in 11 instance(s)
  plan   pin mean_reversion: pinned  [learned] this desk has not moved it in 11 instance(s)
  plan   free knobs: long_term_vol  [rule] the screen's default set for this curve
  plan   wings: left alone  [rule] nothing quoted constrains the smile
  fit    4 target(s), rmse 0.0373 vol points, worst -0.0529 at 1Y
  learn  long_term_vol: fit said 8.665, this desk lands -0.13 from it -> 8.535
         [learned] over 11 answered proposal(s) this desk landed -0.130, spread ±0.010
  move   curve.long_term_vol: 6.95 -> 8.535
```

Every line is tagged `[rule]` or `[learned]`. A **rule** is something true of
the model -- four targets cannot pin down five parameters. A **learned** reason
is something true of *you*, and it always carries the number of instances
behind it, so you can see at a glance which kind you are disagreeing with.

Nothing is on the book. Nothing is written anywhere except the file `--out`
names.

### The card in the Market maker tab

The agent also sits on the Market maker tab as a card, under *The market
against the surface*, because the fit it plans is that tab's **Fit** button.
It reads exactly what Fit reads -- the market paste, the target curve and its
source, the conventions -- so **Propose** answers *how would you run the fit
that is on this screen, and what would come out*. There is no separate
market box on the card on purpose.

Two switches. **Agent chooses the knobs** lets it pick which parameters to
free, from what the targets can determine, what the quotes actually reach,
and what the journal says you never touch; untick it and it runs with the
boxes ticked under *What the fit may move*, and says the choice was yours.
**Score against the archive** asks the desk agent to judge the proposal at
every archived market -- what it fixed and what it broke -- before you see it.

Then you answer it, and the answer is what it learns from:

- **Accept** -- the proposal becomes the marks the quote stands on, the same
  way a fit's answer does; the quote sheet says so. With **keep the marks**
  ticked it also goes on the loaded book.
- **Take the plan onto the fit** -- the agent's knob choices are written into
  the fit panel's boxes and Fit runs. Adjust whatever you disagree with, press
  Fit again, and when you are done press **Record my fit as the edit**: the
  journal then holds your fit beside the agent's proposal on the same
  morning, which is the row it learns most from.
- **Reject** -- recorded as such; nothing moves.

The buttons disappear if you edit the paste after proposing -- a verdict on a
proposal about a market that is no longer on the screen is not a verdict.
Answering the same proposal twice is one line in the journal, not two. The
journal file is `mm_remarks.jsonl` beside the workbook; `volkit serve
--journal PATH` names another.

### Teaching it

```
volkit mark record EURUSD --proposal proposal.json --verdict edited \
    --session marks.json --note "long end always feels rich here"
```

That is the loop. Accept it, edit it on the marking tab and save your marks,
or reject it -- and say which. An **edited** proposal is the most useful thing
you can give it: it is the only place its number and yours sit side by side on
the same morning.

It also learns from re-marks you make without being asked, by comparing two
saved sessions, so it is not starting from nothing. But a verdict is worth
many diffs.

### What it will and will not conclude

```
volkit mark learn EURUSD
```

Under **five instances** for a knob it says nothing at all about it. Above
that it will tell you things like *not moved once in eleven instances -- this
desk leaves it alone*, and pin that knob next time.

It will only apply a correction when your answers **agree with each other**.
Land 0.12 below the fit every time and it learns that. Land 0.12 either side
of it and it says *this desk lands on both sides of the fit here* and changes
nothing -- because the median of six numbers is a number whatever those six
were, and it is only evidence when they point the same way.

A correction is capped at **half of whatever the fit itself moved**. A nudge on
a fitted number is a nudge; a nudge that can exceed the fit is a second fit
with much less behind it.

One thing to expect: once it starts pinning knobs, the fit's RMSE gets
*worse*. That is not a bug — fewer free parameters cannot fit as tightly. It
means the fit is now doing what you do rather than what the optimiser likes,
and the numbers are all there so you can decide whether you agree.

### The two agents together

```
volkit mark confer EURUSD --archive mm_archive.jsonl
```

The desk agent's flag — *the mark is 0.45 below where this has been quoted* —
is evidence the marking agent can use, and this is where it gets handed over.
The desk agent turns the archive into targets; the marking agent proposes; the
desk agent then scores the proposal at **every** archived point, including the
tenors the fit was not aimed at, and reports what it fixed and **what it
broke**:

```
critique: mixed: fixes 3, breaks 1; 2 -> 4 inside
  atm.1Y: 6.066 -> 7.249, -0.141 outside fixed
  atm.1W: 7.900 -> 8.402, +0.202 outside broke
```

If something broke, it weights that point up and tries again — at most three
rounds, then the best one comes to you. That last step is not automation
being polite: three rounds is enough to back off something that broke a
tenor, and past that it is fitting the archive's noise with extra steps.

It scores *inside the two-way the market actually showed*, not distance to the
middle of it — so it cannot make itself look good by walking your surface onto
the average of every market it has ever seen.

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
| *N quotes and M free parameters* | not an error: the listed fit is an exact interpolation, so its residuals mean nothing |
| *given, not fitted* | you have typed a value into one of the **hold** boxes on an Exchange-traded panel. The residuals are the best the free parameters can do at that value |
| *changed and could not be read* | auto-load found a new feed file and it would not parse. The old one is still on the book; fix the file and it is tried again on the next look |
| *this line matches N panels* | a position line did not say enough. Name the contract and the expiry so it can only mean one panel |
| *has no contract size* | a positions panel on a `CUSTOM` contract. Its money columns are per one unit of the base currency until you set **Contract size** on the panel |
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
