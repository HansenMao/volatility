# volkit §15–§16 — Auto-loading the feed, and starting a build nobody types at

Extracted verbatim from `CLAUDE.md` §15–§16. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.
## Auto-loading the market feed (`--auto-reload`, and the pricing checkbox)

**Only the feed is watched.** Three files are read and they have three
different lives, and only one of them is worth chasing:

- The **workbook** is the book of record, and this session's marks are *not*
  in it -- nothing writes to the workbook (§2, §13). Re-reading it is exactly
  what throws a morning's marking away, so it stays on `Reload workbook`,
  where somebody has to mean it.
- The **historical sheet** is a record of what happened, not a market. It does
  not move during a session in any way a screen needs to chase.
- The **feed** is a publication. It is republished all morning and a price
  quoted off a stale spot is simply wrong.

Off unless asked for, because a number that reloads underneath somebody
reading it is its own kind of silent change. Two ways on, one setting:
`serve --auto-reload [SECONDS]` (or `auto-reload = 30` in `volkit.cfg`) at
startup, and the **auto-load** checkbox on the pricing toolbar
(`POST /api/auto`, `BookService.set_auto`) at any time. The switch is the
server's, not a browser's: one watcher, one interval, whatever is open.

- **A changed feed is read once its write time has stopped moving**, the same
  stamp on two passes, rather than after so many seconds of quiet. A feed is
  written in pieces and half a feed is not a market; and a file stamped by
  another machine can be seconds *ahead* of this one's clock, which a
  wall-clock settle would hold back for as long as the two disagreed. It costs
  one tick. `auto_check(settle=False)` is the by-hand check -- the *Check the
  feed now* button -- because somebody who pressed it knows they have saved.
- **A feed is read into a local before it goes on the book**, so a half
  written one does not leave the screen with no market at all.
- **The same message about the same file is said once.** `_auto_record`
  suppresses the repeat -- but never the retry, and a failed read does not
  advance the remembered write time, so a file caught half written is tried
  again.
- **No feed file means the switch says so.** `auto_state().available` is what
  greys it out; a checkbox that can be turned on and then quietly does nothing
  is the same failure as a box that is filled in and ignored (§4).
- **The page polls one integer**, `auto.seq`, and rebuilds only when something
  actually happened. It moves per event, so a watcher that did nothing cannot
  make the screen flicker. `/api/auto` belongs to **no** screen: the feed is
  read by several of them, and the switch sits on the pricing tab only because
  that is where a stale spot does damage.

## Starting a build nobody types at (`config.py`)

A double-clicked exe gets no command line, so `volkit.cfg` beside it is one:
`key = value` becomes `--key value`, `command =` is the subcommand, a boolean
becomes a bare flag or nothing, keys may repeat, `#` comments.

- **Read only when nothing was typed.** Anything on the command line means the
  file stays shut; a file that partly overrode what somebody just typed would
  be the most confusing possible arrangement. `--config PATH` reads a named one
  whatever else was typed and appends what was typed after it; `--no-config`
  reads none. The same subcommand in both places is a merge, two different ones
  a refusal.
- **What it read is printed.** A packaged app taking silent orders from a file
  nobody remembers writing is a swallowed error with better manners.
- **Option names are not validated here.** A misspelled key becomes an option
  argparse has never heard of, and argparse names it and stops — a better error
  than this module could invent, and one that cannot drift out of step with the
  real options. Line *shape* is validated, because `port 8900` with no `=`
  would otherwise vanish.
- **The launcher puts `serve` in front**, not at the end. Every option here is
  either global or a subcommand's, and both parse after the command name; a
  settings file of nothing but options would otherwise leave them in front of
  it, where argparse cannot place them.
- The value is the rest of the line, so a Windows path with spaces needs no
  quoting. Only the `command` line is split, on shell rules.
