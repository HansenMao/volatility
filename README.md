# volkit

FX volatility surface modelling — ATM term structure, dated events, SABR/SVI
smiles, cross pairs, rolldown, and a local web interface.

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

## The two panels

The web interface separates the two jobs the tool does.

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
```

Add `--asof "2024-02-28 12:00"` to price against a fixed valuation time.
Without it the current UTC time is used, once, at startup.

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
| `banded` | managed / pegged pairs: Beta-on-band body with a hazard-rate jump leg |
| `analytics` | rolldown / carry and indication pricing |
| `webapp`, `web/` | the local web interface |
| `cli` | command line |

## Extending

* **A new interpolator** — add it to `INTERPOLATORS` and to `SmileSlice.vol`.
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
* **A new product** — add it to `PRODUCTS` in `pricing.py` and a branch in
  `_price_leg`; the panel picks it up from `/api/state`.
* **A new overhedge profile** — add a shape to `_barrier_profile` in
  `exotics.py` and a name to `TOUCH_MODES`. Flat profiles use the closed form
  automatically; anything time-dependent routes to the simulator.

## Tests

```bash
python3 -m unittest discover -s tests -v      # 156 tests, no pytest needed
```

`pip install esprima` additionally enables a syntax check on the front-end
JavaScript; that test skips if it is absent.
