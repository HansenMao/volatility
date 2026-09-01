# volkit §13 — Saving a session (`session.py`)

Extracted verbatim from `CLAUDE.md` §13. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The workbook is the book of record and **marking never writes to it**.
Everything the marking and market-maker screens do lives on the loaded book,
and a reload discards it -- the right default for a tool whose primary file is somebody
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

## Writing a session into the workbook (`session.export_workbook`)

The one deliberate exception to "nothing writes to the workbook", asked for
by name: `volkit session FILE --to-workbook [OUT]`, `/api/session/export`,
and **Write to workbook** on the marking screen.

**It exports the *file*, never the live book.** The button saves the session
first and then posts the path it wrote, and the route reads that file back
off disk -- so nothing reaches the book of record that is not already
written down, in plain JSON, beside it. That is the constraint the route is
built around: a screen cannot write a mark it has not first saved.

**Where it writes.** `in_place` is the *destination*, not merely permission
to be it -- naming no output and asking for in place used to write the
`_marked` copy and then report it as having gone into the workbook. The
button and the route write the loaded workbook in place (no `out` in the
payload); a named `out` is still a copy, and naming the workbook itself as
`out` is refused, since `out` means "a copy there". The CLI's default is
unchanged: `--to-workbook` writes `<name>_marked.xlsx`, and `--in-place`
writes the workbook.

**An in-place write keeps what it replaced**, as `<name>.bak-<stamp>.xlsx`
beside it (`session._backup_path`, stamped to the second in local time like
the file's own `saved`, and never overwriting an existing backup). What is
written is `blob` -- the file exactly as it was read at the top of the
export -- not a re-read that another writer could have moved underneath it.
This is the answer to openpyxl dropping images and charts: the round trip is
not reversible from the session file, so the file itself is kept. The button
asks first, in the page's own inline confirmation row (`#sessconfirm`, the
same idiom as the kACE post), never a browser dialog.

- **Every cell it writes is one the reader reads back.** Curve parameters
  and events go into the PARAMS rows the workbook always had (a cross's
  correlation into the same three cells). What the workbook had no cell for
  goes into rows `marketdata.overlay_label` now reads -- `atm 1m` (ATM
  overwrite, points), `slog25 3m` (smile overwrite, raw), `term rho10 decay`
  (one coefficient of a marked parameter term structure, raw), `shift rho25`
  (the market maker's wing shift), `anchor` -- and the band treatment into a
  `BANDS` sheet in `BandTreatment.to_request`'s own spelling. The reader
  puts them into `MarketData.overlays` in the session file's own shape and
  `Book._build_surface` applies them with `session.apply_block`, the same
  function that applies a session file. Writing a cell the tool would not
  read is the silent zero this project exists to remove, and a workbook
  written here loads as the session it came from: a test pins every kind of
  mark to 1e-9 through the ordinary reader.
- **openpyxl keeps a formula and drops its cached value**, and pandas reads
  a formula with no value as a blank quote -- every smile sheet is `=C2*3`.
  `_restore_formula_cache` reads the values from a `data_only` load and puts
  them back into the saved file's `<v />` slots, so the copy is readable
  before Excel has touched it. **A formula is not always a string**: Excel
  saves the same `=C2*3` as an *array* formula, openpyxl returns it as an
  `ArrayFormula` object and writes `<f t="array" ref="B2">`, and both halves
  of this missed it -- `_formula_cache` asked `startswith("=")` of a
  non-string and the substitution matched only a bare `<f>`. `session
  ._is_formula` is the one predicate now and the `<f ...>` element is copied
  through whole rather than rebuilt. The shipped workbook's smile sheets are
  array formulas throughout, so the copy came back with 126 blank quotes.
  Images and charts do not survive openpyxl, which is why an in-place write
  keeps a backup and why the CLI's default is still a copy.
- **A pair is replaced, never merged**, as on load. The one thing that cannot
  be replaced per pair is an event's *currency* weight, which every pair with
  that currency shares: the file's weights are written, none are removed, and
  a pair whose saved schedule lacks an event the sheet's weights would give
  it has its own cell set to cancel the legs -- said in the notes, because
  zeroing the weight would take it off the other pairs too.
- **Events go in in the sheet's own clock** (Hong Kong, the reader's
  `event_tz_offset_hours`), a row or a currency column the sheet lacks is
  added, and rows are appended after the last *labelled* row -- `max_row`
  counts formatted-but-empty rows, and the reader now skips a blank label
  rather than reporting `nan` as a parameter it cannot name.
- A pair the workbook has no PARAMS column for is reported and not added:
  CONFIG would need it too, and a pair that is half in a workbook is worse
  than one that is not.
