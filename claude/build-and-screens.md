# volkit §14 — Building without some of the screens (`screens.py`)

Extracted verbatim from `CLAUDE.md` §14. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

`build_exe.py --exclude-tab` / `--only-tabs` chooses which of them a build
contains. The names are written into the bundle as `volkit/data/screens.txt`,
and `screens.py` is the **only** thing that reads it; everything else asks
there. No manifest means every screen, which is what running from source and a
plain `pyinstaller volkit.spec` both give. `VOLKIT_SCREENS` selects a subset
where there is no manifest, which is how the excluded case is tested; a
manifest beats it, because the manifest is the build's own decision and an
environment variable must not quietly put back a screen somebody left out.

A screen has **three** states, not two. `--hidden-tab` builds one and leaves it
off until the exe is started with `--enable-tab NAME` (or an `enable-tab` line
in `volkit.cfg`); the manifest writes it as `name hidden`. Off, it is turned
away by the same route and subcommand checks as an excluded screen and says the
*other* sentence — how to switch it on, which is the whole difference. Asking
for a screen the build does not contain is an error rather than a no-op, a
build may not hide every screen, and the smoke test checks both halves of a
hidden one: off by default, and really on with the switch. `screens.activate`
is read off argv before the parser is built, because the flag changes which
subcommands the parser has; `enabled.cache_clear()` drops the manifest cache
with it, since a stale half is worse than no cache.

- **An excluded screen is gone three ways**: no tab and no boot work (the page
  keys off `screens` in `/api/state`), routes refused **by name** with a 404
  that also says what the build does have, and subcommands not registered --
  with `cli._excluded_request` answering *"the Market maker screen was excluded
  from this build"* rather than argparse's *invalid choice*, which in a trimmed
  build is a lie.
- **Ownership is declared once**, in `SCREENS`. A route or a subcommand belongs
  to exactly one screen (claimed twice ⇒ an assertion at import); anything
  shared -- `/api/state`, `/api/reload`, `check`, `serve` -- belongs to none and
  always works.
- **No code is removed.** numpy and scipy are the size of a build, not
  `analytics.py`, and an import that vanished would turn a wrong build into a
  stack trace instead of a sentence. It is also **not a permission system**:
  anyone who can run the exe can run a build that has the tab.
- **The build's own steps follow the selection.** The smoke test runs what the
  build has -- `tenors` belongs to marking -- and checks that each excluded
  subcommand really fails. The test suite always runs with every screen: a
  `VOLKIT_SCREENS` left in the shell would otherwise turn a trimmed run into a
  green build.
- **The manifest is written under `build/`**, never into `volkit/data/`: a build
  must not leave the source tree quietly missing a screen.
