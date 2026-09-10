# One market-maker tab, several currencies — and the whole book out at once

Built 2026-09-09. Companion to `claude/one-quote-and-the-client-record.md` and
`claude/kace-export-design.md`; the standing text is CLAUDE.md §11 and §20, in
`claude/screen-market-making.md` and `claude/kace-feed.md`.

## The premise

The market-maker tab had a **Pair** selector on its bar, and every card on the
tab answered for whatever it was set to. That is right for exactly one thing on
the screen — the *fit* — and wrong for everything else on it. A check reads a
pasted run and a quote reads a request box, and both are answered **per line**:
a broker's morning run is not one currency, and neither is the list of things
the phone is asking for. One selector meant a desk quoting three pairs worked
three times, and the archive, the bank and the tape each showed a third of what
they held.

So the pair moved **onto the marking agent card**, where the fit is, and
everything else reads the pair off the line it is on.

## What a line belongs to

`quotes.py` already knew: a line names its pair (`EURUSD 1M ATM 8.20/8.60`) or
sits under a heading that is nothing but a pair, and with no `pair` given every
quote comes back carrying its own. Two things changed.

- **A line's own pair resolves its direction word and its premium currency.**
  `_build`, `_build_structure` and `_build_request` took the caller's pair;
  reading a box with no pair given, `eur call over` had nothing to resolve
  against. They take `state.pair or pair` now, which is the same thing when a
  pair *was* given and the right thing when it was not.
- **`require_pair=True` refuses a line that names none** (`quotes.NO_PAIR`).
  This is the decision the whole change rests on. With no selector left on the
  tab, a bare line has nobody to belong to; pricing it against whichever pair
  the marking card happens to show would be a price against a curve nobody
  asked for, which is the silent default this tool exists to remove. It is the
  market-maker screen's reading and nobody else's — `volkit mm EURUSD` names
  the pair on the command line and reads a bare line as it always did.

## The sheets

`marketmaker.CheckSheet` and `.QuoteSheet` are what the screen posts now. Each
is a **loop over the panel above it**, not a new engine:

1. `pairs_named` reads the box once with no pair given and returns the pairs in
   order of first appearance, plus the lines that named none.
2. One panel runs per pair, `require_pair=True`, so the other pairs' lines are
   that pair's business and are passed over in this pair's parse.
3. The rows come back **in the order they were written**, each carrying `pair`.
   A run is read down the page; sorting by currency would be a different
   document from the one that was pasted.

Everything a pair's own panel said — its bank, its archive block, its client
record, its axe, its fair value, its printed tape, which marks it stood on — is
under `by_pair` in full. The sheet's own blocks are the sums, and the cards
under the sheet read `by_pair`.

**A sheet is a loop over the one pricing engine and never a second one.** A
test pins a row on a three-pair sheet against the row that pair's own panel
makes alone: model, bid, offer, width and rung.

Three consequences worth stating:

- **One pair's failure is one pair's line.** A pair the book does not build, an
  expiry in the past, a surface that will not read: caught per pair, reported in
  `by_pair[pair]["error"]` and in the warnings, and the rest of the sheet is
  answered. A single panel raising is right when the screen is one pair and
  wrong when one mistyped pair in a broker run would blank a check of the other
  four.
- **A bare line is reported once**, not once per pair on the sheet — it is
  refused by every pair's parse *and* by `pairs_named`, and `_merged_lines`
  keeps one entry per line number.
- **"Passed over" is dropped from the merged notes.** Each pair's parse says it
  passed over the other pairs' lines; on a sheet that prices all of them,
  nothing was passed over. A pair heading is one fact about the paste and is
  kept once.

## The cut and the interpolation

They were two boxes on this bar, which is two places to decide one thing. A cut
is chosen where the curve is marked, so both now come from the **Vol marking**
tab: `cut` travels on every request, and `methods` maps a pair to its
interpolation — that tab's choice for the pair it is showing, nothing for the
rest, which the server reads as each pair's own default (what the marking tab
would show the moment it was switched to it). The page re-runs both stages when
either moves, because a sheet run at last night's cut must not sit on the
screen looking current.

## Held marks

The marking card holds **one pair's** marks. `_marks_for` hands them to that
pair's panel and every other pair reads the book, each saying which. Laying a
EURUSD fit over USDJPY's rows would be a wrong answer that reads perfectly
well; refusing the whole sheet because one pair on it was not fitted would make
the card unusable beside it. The stale-stamp reading is unchanged — a set of
marks whose book has moved is still dropped and named — with the pair in front
of it on the sheet's own note, because only one of several pairs went stale.

## The cards below

- **Knowledge bank**: one table per pair, every pair the book builds (plus any
  the file holds that it does not), with one **Save bank** over all of them.
  `/api/mm/bank` takes `banks` — a pair to its rules — and **validates every
  pair before it sets any**, so one bad rule saves nothing, which is the same
  discipline the single-pair save always had.
- **Learn widths**: every pair on the card at once, each from the archive and
  its own lines of the paste. The paste is read per pair *without* a pair of its
  own, so a bare line is refused rather than counted once for every pair in
  turn.
- **File this run**: every pair the paste names, filed under itself; a bare line
  is listed with the reason. (`agent.paste_runs` is the one reader.)
- **Ask the record** still takes one pair — a question is about one — and falls
  back to the marking card's when the question names none.

A pre-existing bug turned up here: `agent.py` unpacked the parser's
`(line, text, why)` as `(line, why, text)`, so the bank and archive cards showed
the reason where the text should be. Fixed with the rest.

## The bulk export

> **Superseded (2026-09-10).** The bar described below came off the market-maker
> screen and is replaced by the **Vol bulk processing** screen -- every channel,
> the overlay, the export tables -- see `claude/publishing-channels-design.md`
> and `claude/screen-export.md`. `/api/kace/bulk` no longer exists. The
> key-tenors-only message (`Feed.pillars_only`) is unchanged.


The kACE feed tab posts **one pair**, which is right for the card it sits on: it
shows that pair's pillar table and posts what the table shows. What a morning
ends with is *every* pair going out, and doing that a pair at a time means
picking each one on the marking screen and pressing Post.

**Export vols** on the market-maker bar: pairs from a dropdown of checkboxes, a
**key tenors only** tick, a destination, one button.

- **The message is the feed tab's.** `/api/kace/bulk` calls `kace()` per pair
  with that tab's own remembered settings — tier, multiplier, interpolation,
  wings, scenario — which the page sends from those very boxes. There is no
  second set of width controls on this bar: two ways to choose a width is two
  different two-ways for one mark.
- **A dry run first, always.** Every message is built, listed (pair, nodes,
  pillars, and any that could not be built), and one confirmation asked. Only
  the pairs that built are then posted; a dry run logs nothing.
- **One post per pair, one line of the answer per pair.** A `RATE_FEED` is per
  pair — the 2026-09-01 note: a half-applied multi-pair message is worse than
  two round trips — so they go one after another, and a refusal is that pair's
  line and does not stop the next.
- **Key tenors only** (`Feed.pillars_only`, `build(pillars_only=True)`,
  `volkit kace --pillars-only`) writes the pillars' five nodes each and none of
  the calendar-day ATM nodes: 45 rather than 400-odd. The daily series is still
  built — a pillar's ATM is read off it — and simply not written, so the pillars
  posted are identical either way. It is a field of `kace_posts.jsonl`, because
  "what did we send kACE this morning" is not answered by a pair and a tier when
  one morning sent the whole curve and the next sent nine points.
- **The route belongs to the market-maker screen**; the feed tab's own routes
  stay the marking screen's. A build without the market-maker tab keeps the feed
  tab and loses the bulk button, which is the right way round.
- **Destinations are declared by the server** (`webapp.BULK_DESTINATIONS`, on
  `/api/state`), one today.

## The command line

`volkit mm PAIR` is unchanged: one pair, read as before. `volkit mm` **with no
pair** is the sheets — every line names its own pair, each answered against its
own curve, with a Pair column on both tables when there is more than one. That
keeps the rule that a screen and a shell are one function.

## State

- New tests: `TestSeveralPairsOnOneScreen` (8) and `TestBulkExport` (5), plus
  the field-list guards updated to pin the sheet readers, the paste reader
  without a pair, and the bank card's every-pair save.
- `tests/test_agent.py` gains the multi-pair paste and the refusal.
- Suites run in slices on the Mac: `test_agent` 127 OK, `test_marking` 67 with
  the seven known Python-3.10 `tomllib` failures, `test_volkit` clean over every
  class touched by this work (market maker, quotes, kACE, screens, web assets,
  held marks) and the known 3.10 `fromisoformat` failures elsewhere.
- Not checked: layout. There is no browser in this environment — the picker,
  the Pair columns and the per-pair blocks need a look on the desk.
