# volkit — user manual

FX volatility marking and pricing. An ATM term structure with dated events, a
SABR/SVI smile, cross pairs built from their legs, vanilla and exotic pricing,
and a browser interface.

---

## 1. Starting it

**Packaged (Windows):** double-click `volkit.exe`. A console window opens,
prints a URL, and your browser opens on it. Keep the console open — closing it
stops the tool. Press `Ctrl+C` in the console to stop cleanly.

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
| `bands.csv` | managed/pegged trading bands | no |
| `holiday_overrides.csv` | extra holiday dates | no |

If the tool cannot find the workbook, pass it: `volkit.exe -w C:\path\to\vol_marks.xlsx`

---

## 2. The two tabs

### Pricing

Each **column is one option**; each row is a field. Add columns with
**+ Option**, copy the last with **Duplicate last**, and delete one with the
**Remove** button in its column (or **− Remove last**). Columns are remembered
between sessions. Prices refresh as you type unless you untick **auto-price**.

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
* **Events** — dated volatility bumps. **Auto-load** pulls scheduled economic
  releases for the pair's currencies; edit, add or delete rows, then **Apply**
  to re-solve the heights.

Everything here feeds the pricing tab immediately.

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
```

Add `--asof "2026-08-23 12:00"` to price against a fixed valuation time.
Without it, the current UTC time is taken once at startup and held.

---

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *time zone database is unavailable* | Windows has no IANA database. `pip install tzdata`, or use the packaged exe which bundles it |
| *workbook not found* | pass `-w path\to\vol_marks.xlsx` |
| Console flashes and closes | run it from a terminal to read the error, or check the message before the "Press Enter" prompt |
| *ATM volatility is zero* | the expiry is today or in the past. Same-day expiries have no whole volatility days and cannot be quoted on this basis |
| *lies outside the managed band* | you priced a pegged pair outside its band; the lognormal smile is not valid there |
| *N distinct parameter sets reprice these quotes* | the smile is not uniquely determined. Run `volkit validate` |
| *only N% of its spike prices into…* | an event sits near the day roll (legacy event mode only) |
| A leg shows a red error | read it — errors are never replaced by a zero |
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
  not something inferred from a butterfly.
