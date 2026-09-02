# Configuration lives in the workbook

The settings a desk maintained by hand used to be a loose CSV each, sitting
beside `vol_marks.xlsx`. They are tabs of the workbook now. One file carries
the marks and the settings that go with them, so a desk that copies the
workbook onto a new machine gets a tool that does the same thing there.

| Was | Is | Read by |
|---|---|---|
| `files/bands.csv` | `PEG_BANDS` tab | `banded.load_bands` -> `Book._default_bands` |
| `files/kace_spreads.csv` | `KACE_SPREADS` tab | `kace.SpreadTable.load` |
| `files/holiday_overrides.csv` | `HOLIDAYS` tab | `CalendarSet.load_overrides_sheet` -> `Book._default_calendars` |

`market_feed.csv` stays a file on purpose: it is market data with an `asof`,
overwritten daily, and a file is easier to overwrite than a tab in a workbook
Excel may have open.

## The reader

`volkit/configsheets.py` is the one place a configuration tab is read.

- A tab is a table with a header row. **The header is found, not assumed to be
  row 1**, so the prose that used to sit at the top of each CSV sits above it.
- **A row whose first cell starts with `#` is a comment, wherever it appears.**
  The bands tab keeps its "deliberately NOT listed" block below the data for
  exactly this reason.
- Column headings match case- and space-insensitively (`Weak Share` ->
  `weak_share`).
- An **absent tab is `None`** and an **empty one is `[]`**: a workbook that was
  never given this configuration and a desk that deliberately emptied it are
  different answers.
- `Row.number` is the row as Excel numbers it, because an error about a
  configuration tab is read by somebody about to go and fix it.

**Adding a configuration later is a tab and a parser**: name the tab in
`configsheets.SHEETS` so an error can say what it was for, and call
`configsheets.read_rows` for it. No new file appears beside the exe.

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
`marketmaker.learn_from_panel` still uses `DEFAULT_CALENDARS` deliberately:
there is no book there, it reads a paste and proposes rules.

## Writing the tabs into an existing workbook

Not with openpyxl. openpyxl drops the **cached value** of every formula cell it
round-trips, and the smile sheets' wing columns are array formulas -- a
round-tripped workbook reads back as 126 "blank quote" problems until Excel has
opened and recalculated it. The tabs were injected as worksheet parts straight
into the package instead, with only `workbook.xml`, `workbook.xml.rels` and
`[Content_Types].xml` amended. Strings inline (no `sharedStrings.xml`), no cell
styles (no `styles.xml`). Same trap `session._restore_formula_cache` exists to
work around.
