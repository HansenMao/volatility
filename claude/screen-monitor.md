# volkit §12 — Monitor (`monitor.py`, `curves.py`)

Extracted verbatim from `CLAUDE.md` §12. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

A sixth UI tab, and the one a desk opens first: *what has moved*. A **tile** is
one pair and two points in time, showing all five quoted numbers and the change
between them, tenor by tenor. Either end is any source `curves.py` builds
except a paste, so a tile is "the surface against last week's close", "the
surface against the quotes it was fitted to", or two dated rows against each
other.

- **Nothing here builds a curve.** `curves.build_curve` is the one dispatch,
  shared with the comparison panel; a second copy would be a second place for
  a source to be added to only one screen.
- **A paste cannot be a tile end.** A tile is rebuilt on every refresh and a
  paste cannot be rebuilt, so it is refused rather than silently frozen.
- **A broken end does not empty the tile**: the levels that could be read stay,
  and the tile carries the reason it has no change. A tenor one end does not
  quote is a blank change, not a missing row.
- **Two dated ends that land on the same row say so.** A column of zeros
  otherwise reads as a quiet market rather than as a comparison that never
  happened. Only checked for two *dated* sources -- the surface and the
  workbook quotes are both stamped with the valuation time and comparing them
  is a perfectly good thing to do.
- **A big move is graded in `monitor.py`, not in the browser.** One threshold
  in volatility points (`MonitorPanel.big`, `--big`, the *Big move* box),
  converted to decimals once at the request edge; two tiers, at the threshold
  and at twice it. The screen shades the cell and the command line stars it
  from the same `grade` the panel returns, so the two cannot mark different
  cells. It grades **every** field, not the highlighted one -- what has moved
  may not be what was being watched -- and it is a grade and not a filter:
  nothing is hidden, dropped or reordered by it. A threshold of zero is the
  one way to turn it off, and grades nothing rather than everything. Each tile
  counts its own graded cells (`moved`, `moved_hard`) and the panel sums them,
  so a tile whose table the reader never reaches still says how much is in it.
- **The curve comparison panel lives here**, not on Analysis, and so does
  its command (`volkit monitor --compare`). `/api/history` belongs to no
  screen: two screens read the historical workbook, so loading it is a shell
  job like `/api/reload`.
