# The Vol bulk processing screen (`publish.py`, `overlay.py`, `exportseed.py`)

Bulk publishing in two halves.  **Input**: an outside file read into an
overlay, compared against the book, and -- pair by pair, only where ticked --
written over the book.  **Output**: every channel the desk's marks go out on,
each pair read from the book or from the overlay as chosen, with the
preflight, the export-policy tables and the log.
`claude/publishing-channels-design.md` is the design note this was built from
and says *why*; this file says what is there and what must not be broken.

## What it is

- **Four channels**, one registry (`publish.CHANNELS`): `kace` (a `RATE_FEED`
  message per pair, posted; `kace.py` builds it), `bloomberg` (a workbook of
  DCAP `PLContribFull` formulas in the desk's own block layout, which the
  desk opens and refreshes), `murex` (two BIFF8 `.xls` files through `xlwt`),
  `cos` (a one-sided CSV grid).  Every channel consumes one
  list of `PillarQuote` -- per pair, per tenor: an ATM two-way and the
  25d/10d RR and BF, each with its own two-way where the channel publishes
  one -- and differs only in container, pair list, tenor labels, width source
  and shade.  `kace.read_pillars` is the one reader of the book's marks at a
  pillar list; `kace.build` uses it too.
- **A destination is a channel, not a file.**  A channel's `write` returns a
  list of `ExportFile`, and Murex returns two: `DRV_MktData_FX_Vol_<date>.xls`
  with the ATM and `DRV_MktData_FX_Broker_<date>.xls` with the wings, off one
  pair list, one source choice, one preflight and one date, written together
  or not at all -- a desk that wrote one of them yesterday and the other
  today has a surface disagreeing with itself.  `needs` is therefore what the
  *destination* needs across every file it writes, so Murex wants the ATM and
  all four wings at every pillar and refuses by name without them; an
  ATM-only overlay no longer makes a whole Murex file on its own.  `murex_vol`
  and `murex_broker` were two channels and are read as the one (
  `publish.LEGACY_CHANNELS`, `channel_key`) wherever a channel is named -- a
  command line, an `EXPORT_PAIRS` row, a `SHADES` row -- and the same pair
  list typed under both old names is the merge, not a pair listed twice.
  `Build.file(name)` picks one of them; `Build.file()` with no name is
  refused for a two-file destination rather than answered with the first,
  and `/api/export/file` takes `file=` to say which.
- **Split by how many pairs, not by channel.** The kACE feed sub-tab on the
  Vol marking screen is the single-pair workbench and is unchanged.  The
  market-maker bar's bulk export is gone and not replaced there;
  `/api/kace/bulk` is removed, no back-compat.
- **The source is chosen per pair** (`publish.build(sources={pair: "book" |
  "overlay"})`, `source` the default for the rest).  A run is not all from
  one or all from the other; the preflight's coverage carries each pair's
  source and colours every tenor cell by where the row actually came from,
  the log entry carries the split, and a kACE post records its own pair's.
  A pair sent to the overlay that the overlay has no rows for is a book pair
  in fact (`coverage[].from`); an overlay row for a pair sent to the book is
  counted as `left_on_book`, never used silently.
- **The screen** (`p-export`, between Analysis and Market maker).  Left, the
  Input half: the file (path or pasted rows), one line per pair it carries
  (rows, tenors, whether the book holds them, a tick), **Overwrite book for
  ticked**, Revert, Clear; then **Compare** -- the book against the overlay
  at a chosen channel's widths, mids and both sides, the wings too.  Right,
  the Output half: destination, the pair picker with a book/overlay select
  beside every pair the overlay carries (and *from overlay* / *from book*
  for all of them at once), tier, multiplier, wings, scenario, tolerance,
  file date, key tenors, dry run; Build, Send, download; the Preflight; the
  Configuration tables; the Log.
- **Routes** (`screens.BY_NAME["export"]`): `/api/export/state`, `/build`,
  `/run`, `/overlay` (load / clear / apply with `pairs` / revert), `/compare`,
  `/file` (download), `/seed`.  The configuration tables go through the
  shared `/api/config` and `/api/config/save`, with `where: "export"` on the
  tabs this screen edits (`configsheets.EXPORT_TABS`); the Config window
  skips those and this screen shows only those.  One painter
  (`cfgPaintTabs(where)`) paints both.
- **Command line:** `volkit export CHANNEL [--pairs ...] [--overlay FILE]
  [--book-pairs ...] [--compare] [--tier] [--multiplier] [--wings]
  [--tolerance] [--file-date] [--confirm-date] [--out-dir] [--dry-run]`, and
  `volkit export --init-tables` to seed the tables a workbook lacks.
  `volkit export murex` writes both Murex files.
  `volkit kace` is untouched.

## The tables (all in the workbook, edited on this screen)

| tab | shape | who reads it |
|---|---|---|
| `SPREADS` (was `KACE_SPREADS`) | tenor rows, a column per tier: `default`, `wide`, ... | kACE (pillars and widths), the market-maker fallback tier |
| `MARKET_WIDTHS` | tenor rows, a column per pair: the observed market ATM two-way | Bloomberg |
| `ADD_UPS` | `pair` (`default`, `crosses`, or a pair), `overnight`, `other` -- most specific row wins | Bloomberg, on top of `MARKET_WIDTHS` |
| `WING_WIDTHS` | `pair` (`default`, `crosses`, or a pair), `tenor`, `rr25`, `rr10`, `bf25`, `bf10` -- most specific row wins | Bloomberg: each wing goes out two-way about its mark |
| `SHADES` | `channel`, `pair` (blank = the channel's default), `shade` | every channel: the ATM mid shift, both sides, before the width |
| `COS_WIDTHS` | `pair` (`default`, `crosses`, or a pair), `tenor`, `width` -- most specific row wins | COS: how far under the mid the bid sits, **one-sided** |
| `EXPORT_PAIRS` | `channel`, `pair`, `label`, `feed_from`, `last_tenor`, `note`, in the file's order | every channel: which pairs it publishes and from which curve; kACE with no rows publishes the book's pairs |

- **A shade is not a width, and the wings are never shaded.** The ATM mid
  moves by the shade and the ATM width goes around it; a wing is its mark
  with its own width around it (`PillarQuote.side`).  That is what the desk's
  sheet does: `(C-J/200)-0.002` for the ATM, `D-K/200` for a wing.
- **Murex is bid = ask**, always; no tier, no arithmetic.
- **`EXPORT_PAIRS` is typed, not guessed.** Seeded once from the desk's own
  files (`files/reference/`, captured 2026-09-10, `exportseed.py`): the 33
  Bloomberg blocks in block order (XAUUSD to 1Y), the 32 Murex pairs in row
  order with the slash labels, the 25 COS pairs with the CNY labels fed CNH.
  `feed_from` names the book's curve where it differs; a pair marked the
  other way up is inverted -- the risk reversals change sign, the ATM and
  flies do not -- and says so.  `last_tenor` caps a pair.
- **The seeds reproduce the desk's sheet**: `MARKET_WIDTHS` is the sheet's
  total ATM width less the add-up the `ADD_UPS` rule assigns (0 overnight,
  0.2 otherwise; 0 for crosses; the seven HKD legs the G7 tab carried get
  their own 0.2 rows), `WING_WIDTHS` is the sheet's `K..N` per pair and
  tenor, `SHADES` is −0.2 for every Bloomberg ATM the G7 and G7 Cross sheets
  carry -- CHFJPY included, the design note's zero was not what the file did --
  and **0 for the four EM/PM pairs** (USDCNH, USDHKD, HKDCNH, XAUUSD), whose
  block is a separate sheet on the desk's side and whose two-ways sit exactly
  half the width either side of the mid.
- **The COS width is its own table, not a tier.** `SPREADS` says a width is a
  quoting policy and is pair-independent; the desk's `Guideline` sheet is not
  -- 0.8/0.6/0.5/0.5/0.5 for most pairs, 1.0/0.8 at the front for USDJPY,
  0.9/0.7 for the yen crosses, a flat 0.5 for USDCNH and AUDNZD, so no one
  column reproduces it.  `COS_WIDTHS` is that sheet, pair by pair, and it is
  **one-sided**: the file carries a bid and no ask, so the number on the tab
  is the whole distance under the mid and is the number on the desk's sheet.
- **What COS starts from is different, deliberately.** The desk's sheet takes
  its base from a broker paste -- the market's delta-neutral *bid*, rounded
  down to a tenth -- and subtracts the ladder from that.  Here the base is the
  marked mid, book or overlay, like every other channel: one surface goes out
  on all four feeds.  The numbers differ from the spreadsheet's by whatever
  the book's mid differs from the market's bid.
- **`KACE_SPREADS` is read under its old name** (`configsheets.LEGACY_NAMES`)
  and renamed to `SPREADS`, in place, the first time the tab is written.

## Invariants

- **Refused by name, never short.** A pillar the channel wants that neither
  the book nor the overlay supplies, a pair `MARKET_WIDTHS`, `WING_WIDTHS` or
  `COS_WIDTHS` does not carry, a tier the tab does not have, a tenor past the last quoted
  one (an ATM and a wing out there would be extrapolated): each is a refusal
  naming the pair and tenor, and no file is written and no message posted.
  A refused run is still logged.
- **The file date is the book's valuation date, and a disagreement with the
  machine's date is said before writing** and needs `confirm_date`
  (`--confirm-date`) or an explicit `file_date`.  The Murex filenames and
  format otherwise change as little as possible: the loader is a black box.
  `tests/test_publish.py` puts the reference files through the overlay and
  gets them back grid for grid.
- **Every export is logged** in `publish_log.jsonl` beside the workbook
  (`kace.PostLog`; the old `kace_posts.jsonl` is carried over once).  Each
  entry carries the channel, the `source` (`"marks"`, or the overlay's
  `{file, sha256, rows, rows_outside_book}`), the per-pair split under
  `sources`, and a hash of what was produced.  The row is *a file was
  written*, so a Murex run appears twice, one row per file with its own name,
  size and hash, and `publish.write_file` returns a list of entries.
- **Bloomberg tickers are a rule plus one table.**  Two-letter codes
  (`publish.BBG_CODES`), base then quote, then the instrument letter and the
  tenor with `O/N` written `1D`; `BBG_LEGACY_TICKERS` overrides it for the
  1M 25d risk reversal (`<prefix>VRR`).  All 3,610 of the desk's formula
  cells come out of the rule (a test walks them).  HKDJPY carries the same
  sign convention as USDJPY -- no flip.  The formula string is the sheet's
  own, with the ticker read from its cell:
  `=_xll.PLContribFull(B3*100,"BID",O3,"Slot46","TICKER",3,,"Valid")`.

## The overlay (`overlay.py`)

- CSV or xlsx, `pair, tenor, atm, rr25, rr10, bf25, bf10`, the workbook's
  `RR 25D` / `ST 25D` spellings as aliases, `atm_bid`/`atm_ask` instead of
  `atm` bypassing the tier for that row.  Read once, hashed (`sha256`).
- **The book does not constrain it; the channel does.**  A row for a pair or
  tenor the book lacks is a good row; a row the channel does not publish is
  ignored and counted.  Blank cells fall through to the book where the book
  has that pair and tenor; where neither has a value the row is refused by
  name.  An overlay row is looked up by the *published* pair.
- **Loaded, it changes nothing** (export only).  The Output side reads it
  only for the pairs set to `overlay`; the kACE feed sub-tab posts the marks
  and says so.
- **Overwrite book** (`export_overlay {"action": "apply", "pairs": [...]}`)
  is per pair: `session.capture` first, written as `pre-overlay-<stamp>.json`
  beside the session file (once, on the first apply; a later apply of more
  pairs lands on the same snapshot); then `overlay.apply_to_book(pairs=)`
  puts the ticked pairs' rows the book can hold on as an ATM tenor overwrite
  plus typed quotes, and recalibrates.  **Revert** puts the snapshot back
  whole.  While any pair is written over, `session_export` (the one workbook
  write) **refuses by name** and points at Revert; `session_save` writes the
  intersection and reports the overflow.  A build that asks for the overlay
  on a written-over pair reads the book -- the rows are on it -- and the
  notes say so.  `reload(discard=True)` drops the applied state with the
  session.
- **Compare** (`publish.compare`, `/api/export/compare`, `--compare`) builds
  the channel twice -- every pair from the book, every pair from the overlay
  -- and joins on pair and tenor, so the two-ways compared are the ones the
  channel would publish: same tier or market width, same shade, same wing
  widths on both.  Mid, bid and ask for the ATM and each wing; a tenor one
  side cannot supply is listed as book-only or overlay-only.
- **kACE under an overlay:** a pair read whole off the book goes through
  `kace.build` with its daily series; a pair read from the overlay, or one
  the book does not hold, goes pillars-only and the notes say so.

## Still hand-typed, by design

The `cos` tier on `SPREADS`.  Everything else has a seed from the desk's own
files, and once seeded is the desk's.  The crosses the book does not mark
come in through the overlay until they are marked.
