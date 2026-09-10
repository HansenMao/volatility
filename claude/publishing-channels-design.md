# Publishing the marked surface — four channels and a Vol exporting bulk screen

Assessed 2026-09-10 against `BCFO Vols bbg output.xlsx`, `BCFO Vols spread DB.xlsx`,
`DRV_MktData_FX_Vol_20260910.xls`, `DRV_MktData_FX_Broker_20260910.xls` and `COS_86830_Bid.csv`.
Companion to `claude/kace-export-design.md` (built, and whose feed tab stays where it is) and
`claude/several-pairs-and-the-bulk-export.md` (whose bulk bar is replaced by the screen described
here).

## Short answer

Yes, all four, and they are one job rather than four. Every channel carries the same payload —
per pair, per tenor: an ATM two-way and the 25d/10d risk reversal and butterfly — and differs only
in the container, the pair list, the tenor labels, how wide the two-way is and whether the mid is
shaded. That payload is exactly what `kace.build(..., pillars_only=True)` already produces. What is
missing is not computation, it is **coverage**: the four channels between them want 41 pairs and 11
tenors. The workbook installed on 2026-09-10 marks 24 pairs including every dollar leg the channels
need, which leaves twenty crosses to derive or mark and two tenors to add. *Coverage* below has the
detail, and it is the only part of this that needs a person.

Three of the four are plain files and have no unknowns. The Bloomberg one publishes through a live
Excel add-in and is the only channel where the last hop cannot be owned by volkit; it is staged
accordingly.

Everything *bulk* lands on a new top-level **Vol exporting bulk** screen, together with the export
configuration tables and an **export overlay** — a file that temporarily overwrites ATM, RR and fly
for the export alone, leaving the marked book where it was. Single-pair export does not move: the
kACE feed sub-tab on the Vol marking screen keeps doing exactly what it does today.

## The four channels

### 1. kACE — `RATE_FEED` XML over HTTP (built)

`volkit/kace.py`, the kACE feed tab, `volkit kace`, `kace_posts.jsonl`. Described in
`claude/kace-export-design.md`. It is the reference implementation for everything below: build from
the book, show it, confirm, send, log what was sent with a hash.

**Its tab stays where it is**, on the Vol marking screen, and keeps every function it has — one
pair at a time, the message shown before it goes, the tier, multiplier, interpolation, wing source
and scenario beside it, Post and Post-clear, copy to clipboard. That is the single-pair workbench
and it is where the person marking a curve wants it: on the screen where they just marked it. The
new screen below is for *bulk* and does not take anything away from it.

### 2. Bloomberg — DCAP contribution

`BCFO Vols bbg output.xlsx` has no VBA. The publishing is done by **Bloomberg's Desktop
Contribution Application** add-in — the sheet's own column headers say *DCAP Formula* — as 3,610
cells of one shape:

```
=_xll.PLContribFull(<vol>*100, "BID"|"ASK", <ticker>, "Slot46", "TICKER", 3, , "Valid")
```

which is `PLContribFull(Value, TransactionType, SecurityId, RecordType, SecurityIdType, Precision,
FirmID, ConditionCode)`. The cell returns `5.840 (12:14:42.765166)` — the contributed value and the
publish timestamp — so the sheet's own acknowledgement is readable. The marks land on the Bloomberg
page **BCFO Vol Surface** (`NFPV` adds viewers, `FXPV` lets clients request prices off it).

Scope: 33 pairs × 11 tenors (`XAUUSD` stops at 1Y) × 5 instruments × two sides.

The ticker scheme is a rule, not a table. Two-letter Bloomberg currency codes —
`AUD AD`, `GBP BP`, `CAD CD`, `CHF SF`, `JPY JY`, `NZD ND`, `EUR EU`, `USD US`, `HKD HD`,
`CNH CG`, `XAU XU` — concatenated base then quote, then the instrument letter and the tenor with
`ON` written `1D`:

| instrument | suffix | example (AUDUSD 3M) |
|---|---|---|
| ATM | `V` | `ADUSV3M` |
| 25d RR | `RR` | `ADUSRR3M` |
| 10d RR | `RX` | `ADUSRX3M` |
| 25d BF | `B` | `ADUSB3M` |
| 10d BF | `BX` | `ADUSBX3M` |

**One exception, and it is uniform across all 33 pairs: the 1M 25-delta risk reversal is
`<prefix>VRR`, not `<prefix>RR1M`.** A legacy ticker. It has to be an entry in a table that
overrides the rule, and a test has to pin it, because a rule that is right 32 times out of 33 is
the kind of thing that gets "tidied" later.

One thing that looked wrong is not: **the HKDJPY block is fed from a source sheet named `JPYHKD`
with no sign flip on the risk reversals**, and the spread DB names that row `JPYHKD` too. The
desk's answer (2026-09-10) is that **HKDJPY carries the same sign convention as USDJPY** — HKD is
pegged to the dollar, so the pair behaves as USDJPY does and the skew goes out the same way round.
`JPYHKD` is a stale label on those two sheets, nothing more. volkit publishes HKDJPY from the
HKDJPY surface with `rr_25` as it stands, no flip, and a test pins that against USDJPY's sign.

### 3. Murex — `DRV_MktData_FX_Vol_<YYYYMMDD>.xls`

BIFF8, one sheet named `Sheet1&TODAY`, header row `ccy pair, Maturity, bid, ask`, then 32 pairs ×
11 tenors = 352 rows. ATM in vol points, 2 dp, tenors labelled `O/N 1W 2W 1M 2M 3M 6M 9M 1Y 2Y 3Y`,
pairs written with a slash (`AUD/USD`).

### 4. Murex — `DRV_MktData_FX_Broker_<YYYYMMDD>.xls`

Same workbook shape and the same 32 pairs in the same order. Header
`ccy pair, Maturity, Ordinate, fxbflyBid, fxbflyAsk, fxbrrBid, fxbrrAsk`, two rows per pair and
tenor for `Ordinate` 10 and 25 — so 704 rows. Butterfly and risk reversal in vol points, 2 dp.

Between them the two Murex files are the same payload as the kACE message, flattened.

### 5. COS — `COS_86830_Bid.csv`

A small grid: header `CUR PAIR,1W,2W,1M,3M,6M`, then 25 pairs. ATM only, one side only, no ask
file. Pairs are written with a slash and use the **CNY** label; those rows are to be fed **CNH**
data — `USD/CNY` in this file means the USDCNH curve, and likewise `AUD/CNY`, `CAD/CNY`, `EUR/CNY`,
`GBP/CNY`, `NZD/CNY`, `CNY/JPY`. The label is a COS naming convention and nothing more; the
workbook's own `USDCNY` sheet is a different curve and is not what this file wants.

## Decisions taken

- **The Murex files stay bid equals ask.** Both files carry the mid twice today and will continue
  to: the width is not what Murex is being told. The writer takes one value per row and writes it
  to both columns; there is no tier involved and no half-spread arithmetic to get wrong.
- **The Bloomberg ATM shade stays.** Every ATM two-way on that feed is published 0.2 vol below the
  marked mid on both sides. It is a **mid shade, not a width**, and is modelled as such (see
  *Widths and shades* below) rather than being folded into a spread ladder where it would quietly
  become asymmetric.
- **Murex files stay `.xls`.** BIFF8, not xlsx, not csv. That means a writer dependency
  (`xlwt`, pure Python, no native build) added to `requirements.txt` and to the PyInstaller spec.
  The sheet name is written literally as `Sheet1&TODAY`.
- **COS gets CNH data under CNY labels**, per pair, as listed above.
- **The COS width ladder is typed by hand, as a tier.** It is not derived from anything; the desk
  maintains it in the workbook like any other tier.
- **There is no COS ask file.** Bid only; the export writes one file.
- **Nothing about the Murex files changes but the numbers in them.** The loader on the other side
  may key off any of it and is a black box, so the rule is least change: the filename format
  `DRV_MktData_FX_Vol_<YYYYMMDD>.xls` and `DRV_MktData_FX_Broker_<YYYYMMDD>.xls` exactly as today,
  the same date semantics the current process produces, the sheet named `Sheet1&TODAY`, the same
  header row, the same pair order, slashes in the pair names, `O/N` for overnight, 2 dp, bid equal
  to ask, BIFF8. Whatever has been working keeps working.

  The one thing worth adding is a **warning, not a rename**: if the book's valuation date and the
  date the filename would carry disagree — a file built late in the evening, or a book loaded as of
  yesterday — the export says so before writing and asks. This is the `horDate` problem the kACE
  feed had, but the fix there was to change the value, and here it cannot be, so it becomes
  something the person is told rather than something volkit decides.

- **`COS_86830_Bid.csv` likewise**: the name, the `86830`, the header, the five tenor columns and
  the CNY labels all stay exactly as they are.

## Widths and shades

`BCFO Vols spread DB.xlsx` answers where the Bloomberg feed's widths come from, and the answer is
simpler than the three-workbook chain suggests.

**`DB Spread`** is a table of *observed market two-ways* — the delta-neutral straddle, TK cut, at
1,000,000 base notional (the column headers say so: `AUDUSD TK USD 1,000,000 AUD`) — per pair, per
tenor, ON to 3Y. Twenty-four pairs in the top block; seven HKD crosses in a second block below,
kept separately rather than derived from their dollar legs.

**`G7`** and **`G7 Cross`** then do one thing each:

```
spread_USD    = DB Spread[pair, tenor]            + add_up
spread_HKD    = DB Spread[HKD block][pair, tenor] + add_up
spread_cross  = DB Spread[pair, tenor]            + add_up
```

and the add-ups live in a three-cell corner table on each tab:

| tab | overnight | other tenors | HKD column |
|---|---|---|---|
| `G7` | 0.00 | **0.20** | **0.20** |
| `G7 Cross` | 0.00 | 0.00 | — |

That is the whole logic. The published width is **an observed market width plus a policy add-up**,
the add-up being 0.2 vol everywhere except overnight and except the crosses, which go out at the
raw observed width.

This does not fit the existing tier table, and should not be forced into it. `KACE_SPREADS`
deliberately dropped the pair dimension — a width is a quoting policy, not a property of a
currency, which is why the pair is chosen on the screen beside the tier. The spread DB's widths are
the opposite kind of thing: they *are* a property of the currency, because they are what the market
is actually showing. Both are true, so both get a table:

- **`SPREADS`** (the tab currently called `KACE_SPREADS`, renamed in name as well as in fact) —
  tenor rows, a column per tier, as today. `default`, `wide`, `thin`, and now `cos`, typed by hand.
  Pair-independent. This is policy.
- **`MARKET_WIDTHS`** — a new pair × tenor table in the `DB Spread` shape, **typed by hand and
  maintained by the desk**, like every other configuration table. It is not imported from that
  workbook on a schedule and nothing refreshes it automatically: what is in the table is what goes
  out, and it is somebody's decision when it changes. This is observation, recorded deliberately.
- **`ADD_UPS`** — three numbers beside it: overnight, other tenors, and a crosses override.
- **`SHADES`** — the mid shift in vol points, by channel, with per-pair exceptions.

A channel then names its width source: a tier from `SPREADS`, or `MARKET_WIDTHS + ADD_UPS`.
Bloomberg names the second, kACE and COS the first, Murex names neither. If the quote archive's
learned width ladder is ever trusted enough to fill `MARKET_WIDTHS`, it would come in as a
*suggestion* the person accepts into the table — the archive already learns exactly this number
from broker quotes and DTCC prints — but the table stays hand-owned either way.

**These four are configuration tables on the Vol exporting bulk screen.** They live in the
workbook, as every other table does — it is the database — but they are edited on the export screen
and not in the Config window, because they are export policy and belong beside the thing they
govern. Same editor pattern as the Config window's tabs: read on load, edited in a grid, written
back with the marks, a blank cell falling through to its default rather than meaning zero. A pair
the channel wants and `MARKET_WIDTHS` does not have is **refused by name**, the way an unmarked
tenor is — a width silently defaulting to zero would publish a one-price two-way.

**A shade is not a width.** It moves the ATM mid, both sides, before the width is put around it.
Keeping it in its own table rather than folding it into a spread ladder is what keeps the two-way
symmetric about a mid the screen can show, and what makes "we publish Bloomberg 0.2 under our
marks" a sentence somebody can read off a table rather than infer from an asymmetric ladder.

**The shade table carries the exceptions too.** Today CHFJPY has no shade on the Bloomberg sheet
while the other 32 pairs are 0.2 under — and rather than deciding whether that was deliberate, the
table simply holds it. `SHADES` is a default per channel plus a row per pair that differs:

| channel | pair | shade |
|---|---|---|
| bloomberg | *(default)* | −0.20 |
| bloomberg | CHFJPY | 0.00 |
| kace | *(default)* | 0.00 |
| … | | |

So the exception is a line in a table somebody can read, change or delete, instead of a footnote in
a spreadsheet nobody opens. Whether CHFJPY keeps its zero is then a desk decision made whenever the
desk wants to make it, not a precondition of building this.

## Coverage — what is marked, and what is left

The four channels want **41 distinct pairs** across `O/N 1W 2W 1M 2M 3M 6M 9M 1Y 2Y 3Y`.

The workbook installed 2026-09-10 (`files/vol_marks.xlsx`, the previous one kept as
`vol_marks.pre-20260910.xlsx`) marks **24 pairs** and builds all 24 cleanly. It closed the whole
dollar-leg gap in one go.

### Dollar legs — done

`AUDUSD`, `EURUSD`, `GBPUSD`, `NZDUSD`, `USDCAD`, `USDCHF`, `USDJPY`, `USDHKD`, `USDCNH`,
`USDSGD`, `XAUUSD` are all marked. Every one of the 41 pairs decomposes into two of these, so
there is no currency in any channel the book cannot now reach. `USDCNY`, `XAGUSD` and `EURCHF` are
marked as well and no channel asks for them — no harm, they simply do not appear in any file.

### Crosses — ten marked, twenty to mark or derive

Thirty crosses are wanted. Ten are marked: `AUDJPY`, `AUDNZD`, `CHFJPY`, `EURGBP`, `EURJPY`,
`EURCNH`, `GBPCNH`, `GBPJPY`, `GBPNZD`, `NZDCAD`. The remaining twenty:

| group | pairs | channels |
|---|---|---|
| HKD legs | AUDHKD, CADHKD, CHFHKD, EURHKD, GBPHKD, HKDJPY, NZDHKD, HKDCNH | Bloomberg, Murex |
| G7 crosses | AUDCAD, AUDCHF, EURAUD, EURCAD, EURNZD, GBPAUD, GBPCAD, NZDJPY | Bloomberg, Murex, most also COS |
| CNH crosses | AUDCNH, CADCNH, CNHJPY, NZDCNH | COS |

`AUDCNH`, `NZDCNH` and `CNHHKD` have sheets in the workbook but no rows yet. `CNHHKD` is `HKDCNH`
the other way up; whichever way it is marked, the export inverts to the channel's quotation and
says so.

Each of the twenty can be **marked** like any other pair, or **derived** from its two dollar legs.
The book can already do the derivation properly: `cross.py` gives the ATM exactly by the variance
triangle, and `moments.combine` gives the whole cross smile — including the risk reversals and
butterflies these files need — by integrating the two marked marginals against a Gaussian copula at
a marked correlation. Nothing is fitted and nothing is simulated, so the same inputs give the same
numbers to the last digit.

The cost of deriving is a correlation curve per cross rather than four smile ladders per cross —
much less to maintain, but it is a **modelled** wing where today's file carries a **quoted** one.

**Recommendation:** derive all twenty to begin with and run the export in dry-run beside the real
files for a week. Mark by hand only the crosses whose derived wings miss the published ones by more
than the tier's own width. The HKD legs are the safest of the three groups to derive — the peg
makes `USDHKD` nearly a constant against the dollar leg — and the CNH crosses the least safe.

### Tenors — the remaining gap

The new `CONFIG` tenor list is `1d 1w 2w 1m 2m 3m 6m 9m 1y`, and the pair sheets carry
`1W 2W 3W 1M 2M 3M 6M 9M 1Y 2Y` (3W on some, which no channel asks for). Loading the workbook and
reading the marks back confirms what that means in practice: **the marks run 1W to 1Y**. The 2Y
rows sitting on the sheets are ignored, because the tenor list does not name them.

- **O/N — nothing to mark, and `1d` in `CONFIG` is the right way to have it.** No sheet carries a
  1D row and none needs to: the ATM is the one-day point off the daily series and the wings borrow
  the shortest quoted tenor's, which is what `kace.py` already does for its O/N pillar.
- **2Y — add `2y` to `CONFIG` TENORS.** One cell. The marks are already there and are being thrown
  away.
- **3Y — still missing everywhere.** It needs `3y` in `CONFIG` TENORS *and* a 3Y row on every pair
  sheet. Nothing derives it: extrapolating the 2Y out a year and publishing the result as a quoted
  mark is precisely the kind of quiet fiction this tool exists to remove.

So what is left of the marking ask is **`2y` and `3y` in the tenor list, and a 3Y row per sheet** —
plus whichever crosses the dry-run week says are worth marking by hand.

## The Vol exporting bulk screen

Bulk vol exporting stops being a card on somebody else's screen. A new top-level entry in the nav —
**Vol exporting bulk**, between *Analysis* and *Market maker* — owns all of it.

The split is by *how many pairs*, not by channel:

| | single pair | many pairs |
|---|---|---|
| where | kACE feed sub-tab, Vol marking screen | Vol exporting bulk screen |
| what | unchanged — every function it has today | all four channels, the overlay, the config tables |

**What moves, and what does not:**

- the **kACE feed sub-tab stays put**, with all of its functions. One pair, the message shown before
  it goes, tier and multiplier and interpolation and wings and scenario, Post, Post-clear, copy. It
  is the workbench beside the marking, and nothing about it changes.
- the **bulk export bar comes off the Market maker screen's main panel** and is not replaced there.
  Its job — pairs from a dropdown, key tenors or the daily series, a destination, one confirmation,
  one post per pair — is done by the new screen, with more control around it.

No back-compat for the bulk path, in the pattern of the last two reorganisations: `/api/mm/export`
becomes `/api/export/run` and the Market maker screen loses the bar outright. `volkit kace` is
untouched — it is the single-pair command and it is in the desk's fingers — and bulk gets its own
`volkit export --channel <name> [--pairs ...] [--overlay FILE]`.

**The cards, in order down the screen:**

1. **Overlay** — load, inspect, clear (below).
2. **Channel** — destination (kACE, Bloomberg, Murex vol, Murex broker, COS), pairs, width source
   and tier, multiplier, shade, wing source (marks or fitted), pillars-only, scenario where the
   channel has one, dry run. One *Build* and one *Send*, the kACE feed tab's two-step confirm kept
   exactly as it is.
3. **Preflight** — what this run will and will not do, before it does it:
   - **coverage**: the channel wants N pairs and M tenors; the book supplies these; **these are
     refused by name**. A short file written silently is the failure mode this card exists to kill.
   - **overlay diff**, when one is loaded: every changed pair and tenor in vol points, largest move
     first, and a typed tolerance beyond which the run is refused rather than warned about.
   - **width and shade**, resolved: what each pillar's two-way will actually be, and where the
     number came from.
4. **Configuration** — `MARKET_WIDTHS`, `ADD_UPS`, `SHADES` and the `SPREADS` tiers, as editable
   grids, all four hand-maintained. They are export policy, so they live here rather than in the
   Config window, and are written back to the workbook with the marks like every other
   configuration tab.
5. **Log** — the recent entries of `publish_log.jsonl`, filterable by channel.

The tables are **edited here and read everywhere**. The kACE feed sub-tab goes on resolving its
tier exactly as it does now; it simply reads a table whose editor has moved, the way it already
reads `KACE_SPREADS` without owning it. One place to change a width, every export sees the change.

`kace_posts.jsonl` becomes `publish_log.jsonl` beside the workbook, with a `channel` field, the
same hash of what was produced, and the same rule: every export, sent or refused, is appended. "What
did we send Murex this morning" then has an answer that is not somebody's memory. The old kACE log
is read on start-up and its entries carried over with `channel: "kace"` so the history is not lost.

## The export overlay

The requirement: all ATM, RR and fly data can be temporarily overwritten from an input file, the
export built from that, and the main curves on the other screens either untouched or trivially
reverted.

**The file.** CSV or xlsx, columns `pair, tenor, atm, rr25, rr10, bf25, bf10` — the workbook's own
`RR 25D` / `ST 25D` spellings accepted as aliases — in vol points. Partial by design: a blank cell
is not an override, it falls through to the marked curve, so a file that touches only USDJPY's 1M
risk reversal is a legitimate two-cell file. A pair or tenor the book does not have is **refused by
name**, never skipped. The overlay may carry `atm_bid` and `atm_ask` instead of `atm`, in which
case the tier is bypassed for that row and the preflight says so.

**Two modes, and the default is the safe one.**

- **Export only (default).** The overlay is applied to a *view* of the book built for the bulk
  export and nothing else. Every other screen — pricing, marking, and the kACE feed sub-tab
  included — shows the marks as they were; there is nothing to revert because nothing changed. The
  screen carries a loud badge — *overlay active: 34 overrides from `2026-09-10-client-run.csv`* —
  for as long as one is loaded. A single-pair kACE post made while an overlay sits on the bulk
  screen therefore goes out on the **marks**, not the overlay, which is the right way round but is
  worth a line on the kACE tab saying so.
- **Apply to session.** For when the overlay should be visible on the pricing and marking screens
  too — and then the kACE sub-tab posts from it as well, because at that point it *is* the book.
  `session.capture()` first, written beside the session file as `pre-overlay-<timestamp>.json`, then
  `session.apply_document` into the live book. A **Revert** button restores that snapshot; it is one
  click and it is the whole mechanism, because the session machinery already does exactly this.

**The rule that keeps the book of record clean:** while an overlay is applied to the session, the
workbook write path **refuses by name**. An overlay is by definition not marked, and the workbook
is the desk's book of record; letting one leak in through a routine save is the single worst thing
this feature could do. The refusal names the overlay and points at Revert. A person who genuinely
wants the overlay marked can revert, type the numbers, and mark them.

**Provenance.** Every `publish_log.jsonl` entry carries `overlay: {file, sha256, rows}` — or its
absence. A file exported from an overlay can then never be mistaken, a week later, for one built
from the marks. The same fields go in the Bloomberg workbook's own header row and in the kACE
message's notes.

## Implementation

### Shape

A new `volkit/publish.py` holding a channel registry and the per-channel writers, plus
`volkit/overlay.py` for the file above. Each channel is a small record: its pair list, its
pair-label map, its tenor list and labels, its width source and default tier, its shade, and a
`write(quotes) -> bytes` formatter. `kace.py` keeps its own message builder and registers as one of
the channels rather than being folded in.

The payload every channel consumes is one list of per-pillar quotes — ATM bid/offer, RR25, RR10,
BF25, BF10 — which is what `Feed` already carries under `pillars_only=True`. Extract that into a
plain `PillarQuote` so nothing about kACE's XML leaks into a CSV writer.

`BULK_DESTINATIONS` in `webapp.py` goes from `("kace",)` to
`("kace", "bloomberg", "murex_vol", "murex_broker", "cos")`, and the bar's dropdown fills itself, as
it was built to.

### Bloomberg, in two stages

Bloomberg's own DCAP manual documents no interface but the Excel add-in — no REST, no file drop, no
COM or ActiveX path — so volkit can own everything up to the contribution and not the contribution
itself.

- **Stage 1 — volkit writes the sheet.** It generates the output workbook already wired with the
  DCAP formulas and today's numbers; the desk opens it and presses Refresh. No Bloomberg dependency
  in volkit, DCAP keeps its own audit trail, and `PB G7 and crosses input.xlsx`, `BCFO Vols spread
  DB.xlsx` and the manual copying all go away. A partially-covered sheet still refreshes, which is
  what makes this the right first cut while pairs are still being marked.
- **Stage 2 — volkit drives Excel.** `pywin32` on the desk machine: poke the value cells, fire the
  DCAP refresh, read the returned `(timestamp)` strings back as confirmation and append them to
  `publish_log.jsonl`. One button beside the kACE Post. This adds Excel and a COM path to the
  Windows build's surface, which is why it is stage 2 and not stage 1.
- **Not proposed: native contribution.** `blpapi` would remove Excel entirely but needs a separate
  Bloomberg contribution agreement. That is a question for the Bloomberg representative, not a
  coding decision, and it does not block stages 1 and 2.

### Tests

The three file channels are byte-comparable, so the tests are the honest kind: take today's real
files as fixtures, build from a book marked to the same numbers, and compare row for row. For
Bloomberg, compare the generated formula strings and ticker per cell against the sheet's, including
the 33 `VRR` cells. For the overlay: a partial file changes exactly the cells it names and nothing
else; an unknown pair is refused; an applied overlay blocks the workbook write; Revert restores the
captured session byte for byte.

## Still to come, not blocking

1. **The COS width ladder** has to be typed into the `cos` tier before that channel can be run.
   Everything else about COS is settled, and the other three channels do not wait for it.
2. **`MARKET_WIDTHS` has to be typed in** — the Bloomberg channel cannot resolve a width for a pair
   the table does not carry, and refuses by name rather than defaulting. Seeding it from the
   `DB Spread` figures captured in this note is a sensible first fill.
3. **CHFJPY's shade** sits in `SHADES` as a per-pair zero, reproducing today's sheet. Whether it
   stays zero is a desk decision that can be made — and unmade — by editing one row, whenever.

## What this replaces

`BCFO Vols bbg output.xlsx`, `BCFO Vols bbg input_EM PM.xlsx`, `PB G7 and crosses input.xlsx`,
`BCFO Vols spread.xlsx`, `BCFO Vols spread DB.xlsx` and
`vol data file for Uploading to Murex v1.1.xls` — the whole `QuickLink` tab, in other words —
collapse into one export off the marked book, per channel, from one screen.

`BCFO Vols spread DB.xlsx` is the exception worth naming: its numbers do not disappear, they move
into `MARKET_WIDTHS` and `ADD_UPS` and are maintained there by hand, on the export screen, instead
of in a workbook on a network drive with two other workbooks pointing at it.
