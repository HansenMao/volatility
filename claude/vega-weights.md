# §21 Vega weights, the bump, and the realized weighting

`volkit/vegaweights.py`, the workbook's `Vega Weights` tab, the **bump** on
the marking screen's ATM term structure card, and the **suggestion** measured
off the historical book on the Workbook card. Read this before changing any of
them.

## What a weight is

A desk does not mark a term structure one tenor at a time. It has a view on
the front end, moves it, and the rest of the curve follows in a fixed
proportion: the 1M goes up a vol, the 3M goes up 0.65 of one, the 1Y 0.40.
Those proportions are the vega weights. Until this existed they lived in
somebody's head and were typed into the overwrite column tenor by tenor.

**A weight is a move ratio, not a vega.** The cell says how far that tenor
moves when the anchor moves 1.00. Each tenor moves

    move x w(tenor) / w(anchor)

so the anchor moves exactly what was typed whatever its own weight is, and
only the *ratios* matter -- a column of 2.00 / 1.00 / 0.65 and one of 200 /
100 / 65 are the same view, and a test pins that.

The other convention -- a weight proportional to the tenor's vega, with the
move inversely proportional to it -- is real on other desks and is
deliberately **not** this one. The number a marker checks against the realized
table on the same screen has to be the same number in both places, and a beta
is what the realized table can measure.

## The tab

`Vega Weights`, on the workbook, read by `vegaweights.load_vega_weights` ->
`Book._default_vega_weights` -> `Book.vega_weights`.

- Columns are `tenor`, `default`, an optional `note`, and **one per pair** that
  has its own curve shape. A column that is none of those and is not six
  letters is refused by name: a stray heading read as a pair column is a weight
  that silently applies to nothing.
- **The fallback is per cell, not per column.** A pair column with a blank 2Y
  takes the default at 2Y. A desk with a view on the front end of USDJPY and
  none on its back end should not have to retype the back end to say so.
- A weight is finite and that is all. **Negative is allowed on purpose**: it is
  what a measured beta comes out as when the back end has been trading against
  the front, and refusing it here would make the realized table on the same
  screen suggest a number the tab cannot hold. Zero is allowed too -- a tenor
  pinned while the rest of the curve moves -- and is refused only when it is
  the *anchor*, where it is a division by nothing rather than a view.
- The tab is the one on the desk's own workbook, spelled the way the desk
  spelled it. It held one unheaded column of numbers under `USDCNH` and
  nothing read it; the migration gave it a header, put those numbers in a
  `USDCNH` column and left `default` **empty**, because the only shape the
  workbook held was a managed pair's and handing it to every other pair would
  have been inventing a view. A pair the tab cannot weight is reported, not
  guessed at.
- It is an open-column tab: see `claude/config-tabs.md`, *A tab whose columns
  are the desk's*.

## The bump

`vegaweights.bump_levels` -> `BookService.atm_bump` -> `POST /api/atm/bump`,
and the `bump` disclosure on the ATM term structure card. `volkit vega PAIR
--anchor 1M --move 0.25` is the same table on the command line.

- **It makes nothing new.** What it writes are per-tenor ATM overwrites --
  exactly the marks a person could have typed one row at a time -- so the
  session carries them, the card's overwrite count reports them and *Clear ATM
  overwrites* undoes them. That is why the bump row is allowed to be a
  disclosure that carries no count of its own.
- **Every level is read before any of them is written.** An overwrite changes
  what `AtmCurve.term_vol` interpolates at the tenors either side of it, so a
  bump applied row by row would move each tenor off a curve the previous row
  had already moved. A test pins that showing and applying agree.
- `Show` and `Apply` are one route with a flag, and **apply recomputes rather
  than replaying the table on screen**: between the two the curve may have been
  marked somewhere else, and a bump that replayed a stale preview would put the
  old levels back under the guise of moving them.
- Refused whole, before a single row is computed: an anchor that is not on the
  curve, one the tab cannot weight, one weighted zero, a workbook with no tab,
  a move that was not typed. Not refused whole: a single row that would go
  non-positive, or a tenor the tab has no weight for. Those keep their place
  and carry their reason, and the rest of the curve still moves.

`GET /api/vega` is the same table without a move: which column this pair
reads and which tenors the tab cannot weight. The bump row asks for it when it
is opened and on every pair change, so a marker sees "weights: USDCNH · no
weight at 3W" *before* pressing anything -- a tenor with no weight does not
move, and reading that off the result table is reading it one press late.

## The realized weighting

`vegaweights.realized_weights` -> `BookService.vega_realized` -> `POST
/api/vega/realized`, in a shut disclosure under the `Vega Weights` table on
the Workbook card. `volkit vega PAIR --realized --history F --lookback 180`
prints it.

Each tenor's daily *change* in at-the-money volatility, regressed on the
anchor's, over the lookback. Changes rather than levels and absolute rather
than log changes, because what the tab holds is a move ratio in vol points: a
1.00 point move of the anchor against a 0.64 point move of the 3M is a weight
of 0.64 whatever level either of them is at.

- Second moments are about **zero**, not about the mean, like every other
  realized figure here (`history.realized`, `history.vol_dynamics`): the drift
  in a volatility series over a lookback is far smaller than the noise in
  estimating it. The consequence worth knowing is that **the anchor's own beta
  is exactly 1 by construction** -- a table showing anything else for it has a
  bug in it rather than a market in it.
- Three columns, because two of them exist to qualify the third:
  `beta == corr * sd_ratio`, exactly. A tenor with a high standard-deviation
  ratio and a low correlation moves a lot *on its own*, and a weighting taken
  off the sd-ratio would mark a move that nothing said was coming. Beta is what
  the button suggests; the other two are why you would not accept it.
- A tenor that cannot be measured -- the sheet quotes nothing there, or the
  window holds too few paired observations -- keeps its place with a reason.
- **It is a suggestion and is never written.** It fills the boxes in the tab's
  table above it, adding the column and any missing tenor row, and the desk
  presses *Write Vega Weights*. The column it fills is chosen beside the
  button: the measured pair's own, or the tab's `default` -- which is the one
  a desk seeds first and has no other way to fill from what the market did. What the market did last quarter is
  evidence about the shape, not the shape: the desk's view of the next event
  is not in it.
