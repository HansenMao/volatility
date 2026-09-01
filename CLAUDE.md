# volkit — project context

Daily FX volatility marking and pricing tool. This file is the standing
context: what the project is, what has been decided, and what must not be
broken. Read it before changing anything.

It is deliberately short. The per-area detail — one file per screen, per agent,
and the reasoning behind every invariant — lives in `claude/` and is listed in
*Where the rest of this context lives* below. **Read a file when you are
working in its area, not before**; the rule you must not break is stated here,
and the file says why.

- `README.md` — install, run, architecture, how to extend
- `USER_MANUAL.md` — for the trader using it
- `MIGRATION.md` — every difference from the legacy tool, the convention audit,
  and the managed-currency work. **The authority on anything that moves marks.**
- `claude/` — the extracted sections of this file, plus design notes

---

## 1. What this is

A rebuild of a legacy tool (`vol.py`, `cvol.py`, `ssabr.py`, `vols.py`,
`common_functions.py`, `__main__.py`, `rv.py`), which is **kept untouched in
`legacy/`, for comparison**. That directory is in `.gitignore`, so the legacy
tool is on the desk and not in future commits -- it is somebody's old
program, it is not built, tested or imported here, and the last version of it
that was tracked is still in the history. Nothing in `volkit/` imports it.
`vols.py` reads the workbook's CONFIG sheet by its old column names, so the
sheet it was written against is kept as `files/vol_marks_legacy_format.xlsx`
-- same marks, old layout, and tracked, because volkit's own reader is tested
against it -- and the comparison still runs from `legacy/`. volkit reads
either (§4).

- ~35,000 lines across 50 modules, 845 tests, `unittest` only (no pytest).
  Tests live in `tests/test_volkit.py`, `tests/test_agent.py` (the desk
  agent, §17) and `tests/test_marking.py` (the marking agent, §18).
- Runtime deps: numpy, scipy, pandas, openpyxl. Plus `tzdata` on Windows.
- Deliberately no `pysabr`, `xlrd`, `tkcalendar`, and no web framework.
- **Six screens**, each with a command-line equivalent, in the order a desk
  reads them: Pricing, Vol marking, Monitor (`volkit monitor`), Exchange traded
  (`volkit listed`), Analysis (`volkit analysis`), Market maker (`volkit mm`).
  Monitor sits behind Vol marking because those are the two a morning starts
  on. `screens.SCREENS` is the one declaration of that order and a test pins
  the page's nav and panel map against it. One HTML file,
  `volkit/web/index.html`, is the whole front end. A build may be made
  **without** some of them, or with some **hidden** until asked for -- see §14.

## 2. Standing decisions

These were the user's explicit choices. Do not quietly reverse them.

| Decision | Consequence |
|---|---|
| **Correctness over parity** | Fix model bugs rather than reproduce them. But *document every change that moves a number*, and give the exact input change that restores the old figure. |
| **Model risk, don't remove it** | Never make a risk vanish by construction to make a model tidy. If a bad outcome is possible, put it in the model and let it be marked. (This came from a direct correction — see §6.) |
| **Implement fixes, don't just flag them** | When something is wrong, fix it and note what moves. Leave it switchable only when there is a real reason to reconcile against old marks. |
| **Web UI, not Tkinter** | Stdlib `http.server` only. No Flask/FastAPI. The desk machine may have nothing installed. |
| **Excel stays primary** | `vol_marks.xlsx` must keep working as-is. Abstract behind a data source; do not migrate to YAML. |
| **Nothing writes to the workbook** | Every mark a screen makes lives on the loaded book. What a session wants to keep goes into `session.py`'s own file *beside* the workbook, never into it -- see §13. The one exception is the **export** in §13, asked for by name: it writes the saved *file*, never the live book, and keeps the workbook it replaced beside it. |
| **Nothing fails silently** | The legacy `except: pass` returning `0.0000` is the anti-pattern this project exists to remove. Errors surface with the real message. |

## 3. Architecture

```
timeutil   one day-count (365.2425), one injected Clock, tenor parsing
           including the short-date codes (O/N, T/N, S/N, S/W)
numerics   bracketed solves, damped fixed points, panel integration
calendars  holiday calendars, the FX date construction (spot, settlement,
           expiry back from it), CSV overrides
timeweight intraday / weekend / holiday weighting
black      Black-76, its greeks, FX delta conventions, strike-from-delta
sabr       Hagan 2002 + calibration (closed-form alpha, global sweep)
smile      arbitrage-constrained SVI, vanna-volga, cached slices
banded     pegged pairs: Beta-on-band body + hazard-rate jump leg, and the
           marked treatment deciding how much the surface takes notice of it
events     dated vol bumps, weighted per currency and superposed per pair,
           joint height calibration, and the one table they all live in --
           the workbook's EVENTS sheet in memory
atm        the ATM term structure
cross      cross pairs from two legs and a correlation
surface    ATM + smile, greeks, delta strikes, RR / fly
exotics    digitals, one-touch / no-touch, overhedge buffers
pricing    multi-leg strips, strike/expiry specs, per-leg error isolation, and
           the one-number reading of them the marking screen asks for
marketdata validated Excel reader; CONFIG is two columns and a cross
           names its own dollar legs; EVENTS is a row per release, a column
           per currency and per pair
feed       spot / forward points from file, by tenor or by date, interpolated
book       all pairs, built in dependency order
listed     exchange traded options: paste parsing, least-squares SABR fit,
           comparison against the marked FX surface, and a position book with
           aggregated greeks both Black-Scholes and on the fitted smile
moments    risk-neutral distribution from a smile; two combined into a cross
history    historical spot / forwards / quotes; realized vol, skew, kurtosis
analytics  carry and roll, fair value, the cross triangle, indication pricing
relvalue   one score per expiry and strike, in volatility points: implied
           against realized in level and in shape, the roll and the forward
           carry, the cross triangle, and where each cell sits in its own
           history
curves     several vol curves side by side, and the same curve on other dates
monitor    small panels: what has moved between two points in time, per pair
quotes     a broker run, in English or in columns: outrights, RR, fly, spreads,
           timestamps and which of two quotes for one thing is live. And the
           same grammar with the price taken out: what is being asked for
knowledge  the per-pair knowledge bank: widths, floors, shifts, notes
marketmaker  two panels: the fit (curve to a target, wings to a market) and the
           quote (a two-way in each instrument asked for). They meet at the
           marks the fit hands back
archive    every observation the desk has kept: quotes shown, trades printed,
           prices we made and what became of them. Append-only, content-addressed
dtcc       fetching the public dissemination files from DTCC: which URL, whether
           a 200 is really a file, and what a 404 on a Saturday means
sdr        the public dissemination file, both layouts, zipped or not, without
           guessing
llm        a local model on a short leash: prose into the house grammar, and the
           finished decision into English. Every number it returns is checked
           against the text it was given
ingest     the watched folders, read once each by content
synthesis  the archive worked out: age-weighted widths, where the market has
           been, what became of our prices, what printed
agent      request in, price out, with the ordered list of ingredients that
           sum to it
remarks    every time somebody moved a mark, and what from: a re-mark is a diff
           of two snapshots, so nothing has to be instrumented
marking    the marking agent: how to run the fit, and what this desk does after
           it -- tendencies with counts on them, never a policy
rules      the marking agent's rules of thumb: what a desk believes before the
           journal knows anything, seeded into the sample so it can be outvoted
consult    what the two agents say to each other: a finding, a proposal, and a
           scored critique of what it broke
ask        the third agent: a question in English about what the tool holds,
           answered from the record with every fact sourced. Reads everything,
           writes nothing
webapp     JSON API + stdlib server;  web/index.html is the whole front end
cli        every screen has a command-line equivalent
screens    which screens a build has, shown or hidden; the one reader of the
           build's manifest, and of --enable-tab
kace       the marked surface as the kACE RATE_FEED message the desk's pricing
           platform takes; replaces the XML_poster workbook (§20)
session    the marks a session made, saved beside the workbook and put back
config     the startup settings file a double-clicked exe reads
paths      resource vs user-data paths (source and frozen), and the one
           text encoding every file is read and written in
preflight  startup checks (tzdata above all)
```

---

## Where the rest of this context lives

CLAUDE.md is the standing rules. Everything below is **the same text, moved**,
so `§N` references anywhere in the repo still resolve. Read a file when you are
working in its area — not before.

| Read this | When |
|---|---|
| `claude/invariants.md` (§4) | The reasoning behind every invariant listed below — the bug each one was written after. Read before changing anything the list names. |
| `claude/managed-currencies.md` (§6) | **Before touching `banded.py`**, or anything about pegged pairs, band treatments, hazard rates or `files/bands.csv`. |
| `claude/known-limitations.md` (§7) | Before "fixing" something that looks wrong — it may be a flagged, deliberate limitation. |
| `claude/screen-listed.md` (§8) | `listed.py`, exchange-traded options, the paste parser, positions and aggregated greeks. |
| `claude/screen-analysis.md` (§9) | `analytics.py`, `moments.py`, `history.py`, `relvalue.py` — carry and roll, fair value, realized, the cross triangle, the relative-value grid. |
| `claude/development.md` (§10) | The full command cookbook, `TestWebAssets`, PyInstaller and packaging rules, how to add a screen. |
| `claude/screen-market-making.md` (§11) | `quotes.py`, `knowledge.py`, `marketmaker.py` — the fit, the quote, the paste grammar, the knowledge bank. |
| `claude/screen-monitor.md` (§12) | `monitor.py`, `curves.py` — tiles and the curve-comparison panel. |
| `claude/session-files.md` (§13) | `session.py` — saving marks beside the workbook, and the one deliberate export *into* it. |
| `claude/build-and-screens.md` (§14) | `screens.py`, `--exclude-tab` / `--only-tabs` / `--hidden-tab`, trimmed builds. |
| `claude/feed-autoload-and-config.md` (§15–§16) | `--auto-reload`, the pricing auto-load checkbox, and `volkit.cfg` / `config.py`. |
| `claude/agent-quoting.md` (§17) | `archive.py`, `sdr.py`, `dtcc.py`, `llm.py`, `ingest.py`, `synthesis.py`, `agent.py`. |
| `claude/agent-marking.md` (§18) | `remarks.py`, `marking.py`, `consult.py`, `rules.py` — the marking agent and its rules of thumb. |
| `claude/agent-ask.md` (§19) | `ask.py` — the read-only question agent. |
| `claude/kace-feed.md` (§20) | `kace.py` — the RATE_FEED message, posting, the spread table. |

Design notes that are not standing context: `claude/kace-export-design.md`,
`claude/marking-agent-design.md`.

---

## 4. Invariants — breaking these is a regression

Each line is binding on its own. **`claude/invariants.md` has the full text of
every one** — what it costs, and the bug it was written after. Read it before
changing anything named here; a one-line rule is enough to obey and not enough
to safely amend.

**Model and numerics**

- **The clock is injected.** Nothing calls `datetime.utcnow()` inside the
  model. One `Clock` per book; same clock ⇒ identical numbers.
- **One year length: 365.2425.** The legacy had six.
- **Daily variances sum to the term variance** (to 2e-16).
- **Integration splits at known breakpoints**, then fixed Gauss-Legendre.
  Never adaptive `quad` on this integrand.
- **Every solve is bracketed** and raises `ConvergenceError` with a diagnosis.
  No bare `fsolve`, no fixed-iteration loops.
- **Smile slices are cached** per (expiry, method, cut, forward) — and the band
  treatment and the level the feed puts the band at are part of that key (§6).
- **Use `scipy.special` ufuncs** (`ndtr`, `ndtri`, `log_ndtr`), not
  `scipy.stats.norm`, in inner loops.
- **Realized and implied share one clock** (`history.volatility_time`), never
  calendar days and never a flat 252.
- **A shortcut through the model is measured, not assumed** — see
  `marketmaker.anchor_gap`.

**One place reads each thing**

- **A tenor is a settlement date, and the expiry comes back from it.**
  `calendars.fx_dates` is the one construction: spot date, then the tenor added
  and adjusted modified following with the end-of-month rule, then the expiry
  the spot lag back. Day tenors (`O/N`, `8D`) are expiry-first.
- **A quoted tenor sits on the volatility axis at its calendar expiry**
  (`calendars.expiry_years`, through `AtmCurve` / `VolSurface` / `Book
  .tenor_years`). `timeutil.tenor_to_years` is a nominal length and a sort key,
  never a placement.
- **A forward is read on the option's settlement date**
  (`Book.market_level_for`), as an offset on the feed's axis. A leg may state
  that **date** -- a broken date is a term of the trade -- never the placement.
  `Book.market_level(pair, t)` is only for a caller that has a time and not an
  expiry.
- **`Book.market_level` is the one place a level is read**, and a cross it does
  not hold it composes from its legs through `feed.compose_level`. A second
  copy of that arithmetic is a second place for the triangle's signs to be
  written upside down.
- **A feed pillar named by a tenor and one named by a date land on one axis**:
  years from the spot date -- a tenor pillar at its own delivery date. The
  valuation date comes from the book's clock, never the machine.
- **The workbook's CONFIG sheet is two columns**: the pairs and the tenors. A
  cross's legs come from `cross.dollar_legs`; explicitly named legs win. A pair
  CONFIG names must have a sheet behind it, and the reader says so.
- **`timeutil.parse_datetime` is the one timestamp reader.** An offset is
  converted, never discarded. Do not re-add a local `.replace("T", " ")`.
- **A tenor's unit may be spelled out, and a date may leave its year off.**
  `1wk` is `1W`; `06 Nov` is the first 6-Nov on or after the reference date,
  which is the injected clock and never the machine. Tenor-versus-date is
  `parse_tenor`, never the length of the string.
- **`pricing.resolve_legs`, `pricing.resolve_strike` and `pricing.quick_vol`**
  are the one reading of a leg's market, a typed strike and the marking
  screen's vol query. A strike read two ways can be read two different ways.
- **Every text file is UTF-8, said once, in `paths`** — `read_text`,
  `open_text`, `write_text`. Never `Path.read_text()` or bare `open()` on text;
  the default is the *locale* encoding. A test walks the source for this.
- **A file that is not UTF-8 is read anyway, and says so.** `paths.decode_text`
  is the one ladder — UTF-8, then UTF-16 by its byte order mark, then the
  machine's own ANSI code page — and never guesses between code pages. Every
  fallback lands in `paths.ENCODING_NOTES` and `cli.main` prints it.
- **The streams speak UTF-8 before anything prints.**
  `paths.use_utf8_streams` is the one call and it is the first line of
  `launcher.main` and `cli.main`. A packaged exe printing a Chinese path to a
  cp1252 stream used to die before it had done anything.
- **A data file is read and closed, never held open** (`marketdata
  .open_workbook`). These are somebody else's files and Excel needs to save.

**Units, signs and edges**

- **Volatility points at the edges, decimals in the middle.** Each boundary
  converts exactly once.
- **Units and signs are decided once per source, never per row**, and the
  *level* is never evidence of the unit — a managed pair marks its ATM at a
  third of a point. A volatility is the number it was written as, in a paste
  as much as in a sheet, and nothing is refused for straddling 1.0.
  `vol_unit='decimal'` is something a person says.
- **An event is weighted per currency and a pair adds its two legs**:
  `bump == sum(weights) + adjust`, always. `events.superpose` is the one place
  the legs meet. A total that disagrees with its parts is refused, never
  averaged.
- **The workbook's EVENTS sheet is the one place an event lives.** A row per
  release, a column per currency and per pair; a currency weight is *shared*
  by every pair with that currency, a pair's cell is its adjustment alone. A
  pair's schedule is derived (`EventBook.for_pair`), never stored beside it. A
  dated row left on PARAMS is reported, not read.
- **The events panel is typed in Hong Kong time; the model stays UTC.** `EVTZ`
  in the page is the one declaration.

**Screens and the server**

- **The server holds no screen state.** The browser owns every panel and posts
  it whole, so every endpoint is a pure function of its request plus the book.
- **No response may carry a non-finite float** (`webapp._finite`).
- **A row that cannot be computed keeps its place and carries its reason.**
- **A screen shows a field only where the model reads it.** A box that can be
  filled in and is then ignored is a silent zero with a cursor in it.
- **A panel is read before it is repainted.** A spinner written into the div
  holding the fields removes them.
- **A reload does not move the screen.** `boot()` reruns; `fillSel` keeps what
  was chosen.
- **A card may be shut, but a mark may not be hidden.** A shut control counts
  its overwrites in its own heading.
- **The Results rows repeat no input box** — what was resolved is written back
  into the box it was asked in. The marking screen's vol query deliberately
  parts from this: the box keeps the *request*, the answer line reports the
  resolution. Its Strike and Delta boxes are one point on the smile asked two
  ways: one request at a time, and the resolution goes into the other box's
  *placeholder*, never its value.
- **A volatility is shown to two decimals, said once** — `VOLDP` in the page,
  read by `vnum` / `vsgn` / `anPct` / `anSgn` / `moCell`. What is *typed* keeps
  its full precision, and so do the command line and every export.
- **The cut selectors offer what this desk marks on** — `SHOWN_CUTS` and
  `cutList()`, the one filter, with `STATE.cuts` named once in the page.
  `atm.CUTS` still holds all four and `--cut LDN` still answers.
- **The smile chart's strike axis is a scale, not a model change.** The slice
  is always built in moneyness.
- **A fit and a quote are two calls, and the marks travel between them** in the
  browser (`capture_marks` / `applied_marks`). The book holds exactly what the
  panel shows — one number, one spelling.

## 5. Things that moved marks vs the legacy tool

All documented in `MIGRATION.md`. In rough order of impact:

1. **Cross triangle sign.** `AUDJPY = AUDUSD × USDJPY` needs `+2ρ`, not `−2ρ`.
   Changes AUDJPY, EURJPY, EURCNH, GBPCNH. Negating those four correlation
   cells reproduces the old numbers exactly (verified to 1e-12).
   `Book(legacy_cross_sign=True)` A/Bs it.
2. **Event windows.** A bump is now the 24 hours *after* the release, not the
   NY-cut day containing it. The old reading gave a 12x height and +35 vol
   points of next-day contamination for an event a minute before the roll.
3. **DST-aware cuts and weekly close.** NY cut is 15:00Z in winter, 14:00Z in
   summer. Weekly close follows NY 17:00. `dst_aware_cuts=False` restores.
4. **SVI.** One arbitrage-constrained slice (5 params, 5 points) replaces three
   summed slices (12 params, 5 points, unconstrained).
5. **The FX date construction.** A tenor resolves to a settlement date and the
   expiry comes back from it; a tenor sits on the volatility axis at that
   calendar expiry rather than at a nominal year fraction, and every forward is
   read on the option's settlement date. Under 0.05 vol points at any tenor,
   under a pip of forward, and at a quoted pillar the forward is now exactly
   the published swap points. No switch. MIGRATION.md 1.6.
6. **Joint event calibration**, modified-following expiry rolls, UK holiday
   observation rule.

## 6. Managed / pegged currencies

**Read `claude/managed-currencies.md` before touching `banded.py`.** The one
thing to know without opening it: the user corrected an earlier design that
forced out-of-band probability to zero — *"you can't force out of band
probability to 0. The probability is real I just need a possible adjustment to
better model the jump risk."* So the model is a **regime mixture**: a Beta body
on the band plus a hazard-rate break leg. Breach probability is a calibrated
**output** and is positive; break risk is a **marked input**, never inferred
from the at-the-money.

## 7. Known limitations (flagged, not fixed)

Each is deliberate and documented. **`claude/known-limitations.md` says why,
and what is reported instead** — read it before "fixing" one.

- Same-day expiries cannot be priced (zero volatility days).
- No discount curve anywhere; all premiums are undiscounted forward values.
- Half-day holidays are full days.
- The band model needs a forward feed, and refuses rather than guessing.
- The cross RR/fly triangle assumes a Gaussian copula and ignores the change of
  measure between the legs' domestic currencies.
- Fair value is a first-order break-even, not a valuation.
- Realized moments are projected onto a tenor assuming independence.
- Listed-option comparisons are unadjusted for American exercise, futures-vs-
  forward convexity and the exchange settlement time.
- The relative-value carry signal stretches a first-order break-even into the
  wings; the shape signal inherits SABR's lack of mean reversion.
- The managed-float reading is a heuristic on measured numbers and is **not**
  the authority — `files/bands.csv` is (§6).
- The carry weight is not tapered by the regime, on purpose.

## 10. Working on this

**`claude/development.md` is the full command cookbook and the packaging
rules.** The essentials:

```
python -m unittest discover -s tests        # 845 tests, ~10m
PYTHONUTF8=0 LC_ALL=C python -m unittest discover -s tests   # as a cp1252 box
python -m volkit check                      # validate the workbook
python -m volkit serve --feed files/market_feed.csv --history vol_history.xlsx
```

- **There is no browser tooling in this environment.** Layout must be confirmed
  by the user. What *is* checked is in `TestWebAssets` — the JS parses under
  `esprima` (ES2017: no `??`, no `?.`), every `$('#id')` resolves, every class
  the script queries is one it emits, every field a panel sends is one its
  `panel_from_request` reads, and the five panel roots are siblings.
- **`volkit/__init__.py` binds its public names lazily** (PEP 562). Nothing in
  `screens`, `paths` or `config` may import numpy, scipy or pandas, directly or
  otherwise — `build_exe.py` reads `volkit.screens` *before* installing them. A
  test pins it.
- **PyInstaller cannot cross-compile**, and `build_exe.py` is the single build
  entry point. Bundled (`paths.resource_dir()`) vs staged beside the exe
  (`paths.app_dir()`) is the thing to get right.
- **A new screen is four pieces, in this order**: the model in its own module,
  a `BookService` method plus a route, a CLI subcommand calling the *same*
  function, and the panel in `index.html`.
- Prefer a test that pins **the behaviour that was wrong**, with a comment
  naming the old bug. Most of the suite is written that way.
- Sample data lives in `files/` with its generator beside it, seeded. Synthetic
  samples are never loaded by default.
