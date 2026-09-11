# Configuration lives in the workbook

The settings a desk maintained by hand used to be a loose CSV each, sitting
beside `vol_marks.xlsx`. They are tabs of the workbook now. One file carries
the marks and the settings that go with them, so a desk that copies the
workbook onto a new machine gets a tool that does the same thing there.

| Was | Is | Read by |
|---|---|---|
| `files/bands.csv` | `PEG_BANDS` tab | `banded.load_bands` -> `Book._default_bands` |
| `files/kace_spreads.csv` | `SPREADS` tab (was `KACE_SPREADS`) | `kace.SpreadTable.load` |
| `files/holiday_overrides.csv` | `HOLIDAYS` tab | `CalendarSet.load_overrides_sheet` -> `Book._default_calendars` |
| (never a file) | `CONVENTIONS` tab | `marketdata.load_conventions` -> `ExcelSource._load_conventions` -> `PairSpec.conventions` -> `VolSurface.conv` |

Two more were never files. `WING_RATIOS` came off the pair sheets' own
formulas (`volkit migrate-wings`), and `Vega Weights` was already a tab of the
desk's workbook -- one unheaded column of numbers under `USDCNH` that nothing
read -- until `vegaweights.load_vega_weights` was written for it.

Six more came with the bulk export (`claude/screen-export.md`) and are
**export policy**: `MARKET_WIDTHS` (tenor rows, a column per pair -- the
observed market ATM two-way), `ADD_UPS` (the policy add-up on top of it),
`WING_WIDTHS` (the two-way width of each wing on the Bloomberg feed, per pair
and tenor), `SHADES` (the ATM mid shift by channel, with per-pair rows),
`COS_WIDTHS` (how far under the mid the COS bid sits, per pair and tenor --
one-sided, because that file carries a bid and no ask) and
`EXPORT_PAIRS` (which pairs each channel publishes, in the file's order). They
are read and written exactly like the rest, but **edited on the Vol bulk
processing screen rather than in the Config window** -- `configsheets.EXPORT_TABS` names them and
the config route sends `where: "export"` for each -- because they belong beside
the thing they govern. `KACE_SPREADS` was renamed `SPREADS` at the same time:
the tiers are the COS file's ladder as well as the feed's, and a tab named after
one channel holding another's policy is a name that lies. `LEGACY_NAMES` reads
the old name when the new is absent and `write_rows` renames the tab, in place
with its prose, the first time it is written; `renamed_tabs` reports which
state a workbook is in.

`CROSS_CORR` is the newest, and it is **not** export policy: it is what the
*book* builds a cross with.  `pair`, `tenor`, `correlation` -- a correlation
typed rung by rung, which takes the place of that pair's fitted `initial` /
`long_term` / `mean_reversion` wherever the tab names it (`cross.py`'s
`MarkedCorrelation`; between rungs it is linear in time, outside them flat).
The desk's own CNH cross workbook has always carried a correlation per tenor
rather than three coefficients, and an exponential fitted through that ladder
reproduces none of its rungs, so the tab is the ladder itself.  Edited in the
Config window like the rest, and seeded with the desk's eight CNH crosses by
the same command that seeds the export tables.

`market_feed.csv` stays a file on purpose: it is market data with an `asof`,
overwritten daily, and a file is easier to overwrite than a tab in a workbook
Excel may have open. It has since taken the **discount curves** as well (the
`<CCY>OIS` rows, below), which is the same argument the other way round: a
rate that has to agree with a forward belongs in the file the forward is in.

## The reader

`volkit/configsheets.py` is the one place a configuration tab is read.

- A tab is a table with a header row. **The header is found, not assumed to be
  row 1**, so the prose that used to sit at the top of each CSV sits above it.
- **A row whose first cell starts with `#` is a comment, wherever it appears.**
  The bands tab keeps its "deliberately NOT listed" block below the data for
  exactly this reason.
- Column headings match case- and space-insensitively (`Weak Share` ->
  `weak_share`), and so does `Row.raw` / `Row.text` / `Row.real` on the way
  back out -- a screen asking for `USDJPY` and a sheet that stored it as
  `usdjpy` are asking about one column.
- **A tab is found however the workbook capitalises its name**
  (`configsheets.match_sheet`): exactly first, then ignoring case, spaces and
  underscores. `Vega Weights` is what the desk called that tab years before
  anything read it, and a workbook where somebody has since typed
  `VEGA_WEIGHTS` is the same tab. A tab written back keeps the name the file
  already has, rather than acquiring a second one beside it.
- An **absent tab is `None`** and an **empty one is `[]`**: a workbook that was
  never given this configuration and a desk that deliberately emptied it are
  different answers.
- `Row.number` is the row as Excel numbers it, because an error about a
  configuration tab is read by somebody about to go and fix it.
- **A session may hold a tab instead of the workbook** (`read_rows(...,
  overlay={SHEET: [row dicts]})`, `rows_from_records`). The rows a desk has
  applied in the Config window become the same `Row` objects the sheet
  produces, so every reader reads a marked band exactly the way it reads a
  written one -- one parser, one set of error messages. A sheet named in the
  overlay is not read off the file at all; a tab the workbook has not got is
  still read from the overlay, so a setting can be added to a workbook that
  never had the tab.

## A tab is marked, not written (added 2026-09-08)

The tabs used to go straight into the workbook: `write_config_tabs`, then a
reload, which is why the card refused to save one while the session had any
marks -- a reload is what throws marks away, so a band cost a morning. They
are part of the **session** now, and the shape is:

1. The Config window posts `/api/config/save`. `BookService.config_save` puts
   the rows into `self.config_edits` and calls `_rebuild`, which captures the
   session, reads the workbook again **with the overlay**, and puts the marks
   back with `session.apply_document`. Nothing is written and nothing is lost.
2. `Book.from_excel(config=...)` holds them as `Book.config_tabs` and hands
   them to every reader; `session.capture` writes them into the session file
   under `config`; `session.config_tabs_from_doc` reads them back.
3. A session file that carries tabs is applied by **rebuilding**, never by
   layering: `BookService.session_load` sets `config_edits` and reloads before
   applying the marks, and `cli._book` reads the file before it builds the
   book. `apply_document` cannot do it and says so rather than pretending
   (`config_fingerprint`).
4. `session.export_workbook` writes them with the marks, before the
   `WING_RATIOS` merge, in the same backup and the same history line. That is
   the whole of "one write": **Write to workbook** on the marking screen.
5. `session.check_config_tabs` is the one validation and runs wherever a tab
   arrives -- the window's Apply, the export, `write_config_tabs`, and
   `config_tabs_from_doc` -- so a heading the tab's own reader would refuse
   cannot be stored in a session now and discovered at the next load.
6. **Reload workbook** (`reload(discard=True)`, the only caller with it) drops
   them with the marks: it is the button that goes back to what the file says.
   Every other reload is one made *in order* to apply them and keeps them.

`write_config_tabs` still exists, unchanged in behaviour, as the command
line's way in and as what `_apply_config_tabs` is shared with.

**Adding or removing a pair is not a setting** and still writes the workbook
on its own (`session.add_pair` / `remove_pair`, `BookService.config_pair`): a
pair is a CONFIG row, a PARAMS column and a **sheet**, so there is nothing for
the reader to read until the file has it. It no longer costs the session's
marks -- it goes through `_rebuild` too -- and the window says so where it is
done.

## A tab whose columns are the desk's

Most tabs have exactly the columns `EDITABLE` declares, because a writer that
guessed at them would reorder a desk's own columns on every save. Three are
listed in `configsheets.OPEN_COLUMNS`, which maps each to **what one of its
extra columns is**:

| Tab | Fixed columns | And one more per |
|---|---|---|
| `Vega Weights` | `tenor`, `default`, `note` | **pair** with its own curve shape |
| `SPREADS` | `tenor`, `default`, `note` | **tier** — a spreading policy (kACE, COS, the fallback rung) |
| `MARKET_WIDTHS` | `tenor`, `note` | **pair** — the observed market two-way, for Bloomberg |

"Open" was never one rule, which is why the kind is stored rather than a flag:
a pair column is six letters and is written back as a proper noun (`USDJPY`,
not `usdjpy`), a tier column is a name the desk chose and is written the way
it is read, so the name in the dropdown, the name on `--kace-tier` and the
heading on the sheet are one string.

- `configsheets.columns_for(sheet, rows)` is the one place the written column
  list is decided: the fixed columns, then every other column the rows carry
  in the order it first appears, with `note` kept at the right-hand end
  however many the tab grows. `spell_open_column` decides how each is written.
- `write_rows` takes a separate `header=` argument for the columns that
  *identify* the old header row. Looked for by every column, a tab gaining its
  first extra column would find no header at all, keep the `#` lines from below
  the data as prose and write them above the new one.
- `configsheets.check_open_columns` refuses an extra column the tab's own
  reader would refuse **before** anything holds it, from
  `session.check_config_tabs`, using `open_column_ok` for the tab's kind.
  Accepted, a mistyped heading is refused on the next load and the tab is
  unreadable until somebody opens the workbook in Excel and fixes it by hand.
- The Config window renders whatever columns the server sends for the tab, and
  has an **Add pair** / **Add tier** box beside the Apply button — the label,
  the placeholder and the box's own validation all come from the kind the
  server sent (`open`, `shape`). A column the desk added carries a `✕` on its
  own heading that takes it off again; the fixed columns do not, because a tab
  that had lost one of them would be unreadable on the next load. Both are
  marks like any other: the column goes out of the table, `Apply` puts it into
  the session, and **Write to workbook** takes it out of the file.

**Adding a configuration later is a tab and a parser**: name the tab in
`configsheets.SHEETS` so an error can say what it was for, and call
`configsheets.read_rows` for it. No new file appears beside the exe.

## CONVENTIONS (added 2026-09-04)

`pair, premium, atmf beyond`, optional — a pair with no row takes the
market's conventions from `black.DeltaConvention.for_pair`:

- `premium` is the currency the option premium is paid in, one of the pair's
  own. The quoted delta is **premium adjusted iff that is the base currency**.
  Default: USD when it is in the pair, else the base — so EURUSD/GBPUSD/AUDUSD
  are unadjusted, USDJPY/USDCNH adjusted, and **every cross is adjusted**
  (the legacy `ccy[0:3] == 'USD'` rule read all crosses as unadjusted).
- `atmf beyond` is the tenor beyond which the ATM strike is the **forward**
  rather than the delta-neutral straddle; `never` / `always` allowed; blank is
  `1y`. `VolSurface.__post_init__` resolves the tenor on the pair's own
  calendar (`DeltaConvention.resolved`) so the boundary pillar itself stays on
  the straddle side whatever the calendar makes its length.
  `black.atm_strike` (not `dns_strike`) is what every smile anchor and
  calibration reads; `ATM` in a strike box is the convention at that tenor,
  `ATMF` and `DNS`/`50d` name one of the two outright.
- `delta` is `spot` (default) or `forward`: whether the pair quotes spot
  delta out to the boundary. Spot delta needs the base currency's discount
  factor, which comes off the **market feed** (below); without one the slice
  reads forward delta and says so (`DeltaConvention.at` -> `delta_note`).

## Discounting: the feed's OIS rows (was the RATES tab, moved 2026-09-07)

`RATES` was a tab of the workbook -- `currency, tenor, rate`, simple, DF =
`1/(1+r t)` -- and it is **retired** (`configsheets.RETIRED`; a workbook that
still carries it is told once at load, from `Book._retired_tabs`). It was the
wrong place, for one reason: the two factors a pair is priced with are not
independent. `F = S x DF_base / DF_term` is an identity, and two deposit
curves typed into a spreadsheet do not satisfy it -- the gap is the
cross-currency basis. Discounting off curves that disagree with the forward on
the same screen is how an option and its hedge come out of one tool at two
prices.

So the rates live in the **market feed**, beside the forwards they have to
agree with. A row whose first column is `<CCY>OIS` is a rate and not a level:
`USDOIS,1W,4.32`, in % p.a., **annually compounded** (`DF = (1+r)^-t`),
interpolated linearly in years between listed tenors and flat outside. Read by
`feed.MarketFeed._read_ois` into `MarketFeed.ois` (a `discount.OISCurve` per
currency); a row that cannot be read is a feed *problem* named by its line
number, and does not take the rest of the file with it.

**One stated curve and the rest implied.** `discount.DiscountCurves`:

- `USD` is the anchor and the only currency that discounts off its own rows.
- Every other currency's factor is implied from the anchor through that
  currency's own dollar pair (`cross.usd_leg`): `DF_JPY = DF_USD x S/F` on
  `USDJPY`, `DF_EUR = DF_USD x F/S` on `EURUSD`. That is the discount rate
  *including* basis, because the basis is in the forward.
- A cross needs no special case. `EURJPY` discounts off EUR and JPY, each
  implied through its own dollar leg, and their ratio is the composed cross
  outright **exactly** -- the feed builds a cross the same way
  (`feed.compose_level`), so the identity closes. There is a test on this.
- A currency the feed also states a curve for **still discounts off the
  implied factor**. Its own curve is read by `DiscountCurves.basis` /
  `basis_report` and nowhere else: using it would break the identity for every
  pair it appears in. The one exception is a feed with no `USDOIS` at all --
  there is then no anchor to be inconsistent with, and a stated curve answers
  rather than nothing.
- `DiscountCurves.factor(ccy, t)` returns the factor **and how it was got**,
  which is what the pills and the notes say; `df` is the factor alone.

`Book.discount` builds the object on every ask (two references and a string)
rather than holding it as a field, and for the same reason the band's forward
lookup is a closure: `serve` and the analysis CLI both load the feed *after*
the book, and a captured one would leave every delta a forward delta for the
life of the process. Each surface gets `discount_lookup =
book.discount_factor`, `VolSurface.slice_conv(t)` builds the slice's
`DeltaConvention` with the base currency's factor in `df_foreign`,
`black.delta` multiplies by it and `black.strike_from_delta` divides the
target by it -- so every slice quantity (calibration wings,
`strike_from_delta`, `smile_table`, `smile_delta`, `quick_vol`, the pricing
rows) is a spot delta out to the boundary and a forward delta beyond. The term
currency's factor discounts the forward premium in `pricing._discounted`
(`premium_pv_*`, `pv_amount`, `None` without one). A currency the feed cannot
reach is never guessed: forward delta and undiscounted premium, with the
warning `<pair>: quotes spot delta but the feed cannot discount <ccy>` at
load, and the reason on the row (*forward delta (no AUD discount factor from
the feed)*).

**Collateral (CSA).** Discounting a Y cashflow at the FX-implied Y factor is
exactly a **USD-collateralised CSA** -- convert at the forward, discount at
USD OIS, convert back at spot, and `DF_USD x S/F` is what falls out. So the
default above has a name, and it is the one most interbank option business
runs under. `DiscountCurves.csa_df(ccy, t, collateral)` is the general
formula:

    PV_Y = A x DF_C(T) x F(Y->C at T) / S(Y->C)

`C = USD` returns `factor()` unchanged -- one arithmetic, not two. `C = Y`
makes the FX leg 1 and leaves `DF_Y`, that currency's **own** OIS curve used
directly, which is the only place a stated non-anchor curve discounts
anything. A third currency reaches the pair through the cross the feed
composes. A collateral currency with no stated rows falls back to the anchor
and the source string says so. Both go through the one `_fx_ratio`, read off
the pair's own spelling, because a reciprocal discount factor looks plausible.

`OptionLeg.csa` carries it, `pricing._discounted` is the **only** caller
(`Book.csa_discount_factor`), and `LegResult.csa` / `csa_source` come back so
a premium discounted two ways cannot read the same. **It must not reach
`df_foreign`**: a delta is a hedge ratio, not a discounted cashflow, and the
spot delta the market quotes is defined off `F = S x DF_base/DF_term` -- the
forward's factors, whatever the collateral. There is a test that the vol, the
strike, the delta and the forward premium are identical under every CSA and
only `pv_amount` follows. On the screen it is a `CSA` row of the pricing grid,
per leg (a CSA is a property of the agreement, not of the pair), and an
ordinary row of the Inputs section -- it and the per-leg `Premium` row sat
behind a `hidden | shown` toggle until that toggle turned out to be
unreachable off the right edge of a scrolling grid; nothing hides them now
(`claude/invariants.md`, under *a card may be shut*).

**What this moved.** Loading a feed now changes deltas as well as levels: on a
dollar-base pair the 25-delta quotes become spot deltas, so the strikes a
smile is fitted at move by a fraction of a per cent. The vols are quotes and
move for none of it. `/api/feed` carries `rates`, `anchored` and `basis`, and
the feed pill has an **OIS** pill beside it -- a warning when the file has no
`USDOIS`, because that is the difference between a spot delta and a forward
one on every major.

## Two things that are easy to confuse

- **`PEG_BANDS` is not `BANDS`.** `PEG_BANDS` is the defended range -- policy.
  `BANDS` is the marking *treatment* applied to a pegged pair (mode, hazard,
  jump sizes, blend) including an optional override of the edges. Kept two
  tabs so a treatment cannot be mistaken for the policy it is applied to.
- **Holidays are the book's calendars, not the process's.** `Book.from_excel`
  builds a *copy* of `DEFAULT_CALENDARS` and loads the tab into it. A book that
  added dates to the shared set would change the expiry of every book loaded
  after it. A workbook with no `HOLIDAYS` tab gets the shared set itself,
  unchanged.

Note that the overrides were previously loadable but never loaded -- a Chinese
New Year in `holiday_overrides.csv` moved no expiry. They now apply.
`agent.learn_widths` (which replaced `marketmaker.learn_from_panel`) reads a
paste with no book behind it and so resolves nothing on a calendar: the
archive keeps a tenor as written and buckets it by nominal days.

A tab written back keeps **its place in the tab bar** as well as its name:
`write_rows` deletes and recreates the sheet, and one recreated at the end of
the workbook is a tab that jumps on somebody every time a setting is saved.

## Writing the tabs into an existing workbook

Not with openpyxl. openpyxl drops the **cached value** of every formula cell it
round-trips, and the smile sheets' wing columns are array formulas -- a
round-tripped workbook reads back as 126 "blank quote" problems until Excel has
opened and recalculated it. The tabs were injected as worksheet parts straight
into the package instead, with only `workbook.xml`, `workbook.xml.rels` and
`[Content_Types].xml` amended. Strings inline (no `sharedStrings.xml`), no cell
styles (no `styles.xml`). Same trap `session._restore_formula_cache` exists to
work around.
