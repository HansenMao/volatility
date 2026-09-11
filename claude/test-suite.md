# volkit — the test suite: layout, fixtures, and what costs time

Read this before adding a test module, before pointing a test at
`files/vol_marks.xlsx`, or when a CI run is slower than you expected.

`claude/development.md` (§10) is the command cookbook. This file is about the
shape of the suite and the two rules that keep it fast and keep it honest.

---

## The layout

1,145 tests in fifteen modules under `tests/`, `unittest` only. Twelve of them
were one file:

```
tests/_support.py          shared imports, paths, fixtures, helpers
tests/_fixtures.py         the test workbook, as literals (generated)
tests/_regenerate_fixture.py   rewrites the above from files/vol_marks.xlsx

tests/test_numerics.py     Black, SABR, smile shape, moments, numeric kernels
tests/test_calendar.py     dates, calendars, events, settlement conventions
tests/test_marketdata.py   the feed, market boxes, cross levels from the legs
tests/test_pricing.py      pricing, the book, banded/peg smiles
tests/test_marks.py        marking a curve, a smile, a quote; held fits going stale
tests/test_workbook.py     config tabs, sessions, reload, vega weights
tests/test_marketmaker.py  the model, its API, the panel
tests/test_quotegrammar.py parsing what a trader types
tests/test_screens.py      screens, web assets, packaging, startup config
tests/test_listed.py       listed options, positions, their clock
tests/test_analysis.py     analysis, relative value, history
tests/test_kace.py         the kACE feed and the bulk export
tests/test_agent.py        the desk agent (§17)
tests/test_marking.py      the marking agent (§18)
tests/test_publish.py      the bulk export (§22)
```

`tests/` is a **package**, and the twelve split modules share `_support.py`
through `from ._support import *`. That is why discovery needs the repository
root as the top-level directory:

```
python -m unittest discover -s tests -t .   # the whole suite
python -m unittest tests.test_workbook      # one area, which is the loop to work in
```

`-s tests` alone imports the modules unparented and every one of them fails to
import. `build_exe.py` passes `-t .`; so does CI.

The point of the split is the second line. A change to the export screen is
`python -m unittest tests.test_kace` in under a minute, not fifteen. Put a new
class in the module whose area it belongs to; if a genuinely new area appears,
add a module and a line to the matrix in `.github/workflows/test.yml`.

---

## Rule 1 — do not pin numbers off the shipped workbook

`files/vol_marks.xlsx` is a spreadsheet, which means it is somebody's file. An
export of the desk's own book landed on it once: thirty-six sheets, ten more
pairs, `TENORS` running `3w` where the sample ran `1d`, a fourth `SPREADS` tier,
the pair sheets flattened from formulas to numbers. Thirty-four failures and
eleven errors followed, over three Windows builds, none of them about the code.

So the suite owns its own copy of the numbers:

- **`tests/_fixtures.py`** holds every sheet's values as Python literals.
  Generated, never hand-edited.
- **`tests/_regenerate_fixture.py`** rewrites it from the shipped workbook.
  Run it deliberately, by a person, and commit the diff — which shows exactly
  which cells moved.
- **`BOOK`** (in `_support.py`) is that fixture written to a temporary file once
  per process. It is what a test reads. Read-only by convention.
- **`book_copy(directory, pairs=None)`** is the writable one — the replacement
  for `shutil.copy(WORKBOOK, wb)`.
- **`book_for(*pairs)`** is a read-only workbook carrying only those pairs,
  built once and shared.

With no `pairs`, the fixture is the shipped workbook cell for cell: same pairs,
same quotes, same bands, same fits, same warnings. That is what let ~200 test
sites move across without a single number being re-pinned.

### The exception, and it is small

Four tests must keep reading `WORKBOOK`, and each says so where it does:

| Test | Why |
|---|---|
| `test_marks.TestWingRatios` (whole class) | The thing being migrated *is* the sheet's array formulas. |
| `test_workbook.TestSessionIntoWorkbook.test_the_copy_loads_as_the_session_and_the_original_does_not_move` | Pins that the export keeps array formulas and their cached values. |
| `test_marketdata.TestMarketData.test_both_layouts_of_the_shipped_workbook_agree` | Compares the shipped file against `vol_marks_legacy_format.xlsx`. |
| `test_workbook` legacy-format reads (two) | Same: the old layout is the subject. |

openpyxl cannot write a formula and its cached value together, so a rebuilt
workbook carries the numbers and none of the formulas — nothing for a
formula-preservation test to find. Anything else that reaches for `WORKBOOK`
should be asked what it is really testing.

---

## Rule 2 — a test that names its pairs should say so

Every workbook load calibrates **every pair CONFIG lists**. A test that marks
USDJPY and reads it back was fitting thirteen other smiles each time, and a
`BookService` that re-reads the file to apply one setting did it again.

That was the single largest cost in the suite. `TestConfigurationIsMarkedNotWritten`
spent 896 seconds pinning peg bands; with `book_copy(d, pairs=("USDJPY","EURUSD"))`
it spends 28. `TestMarketMakerApi` went 563 → 16 the same way.

So: when a class knows which pairs it touches, give it a `PAIRS` tuple and pass
it. The settings tabs are always written whole — they are usually the subject —
and `make_workbook` trims the EVENTS columns and BANDS rows of dropped pairs so
a narrowed book does not warn about cells nothing reads. Naming a pair the
fixture does not carry raises, rather than quietly producing a workbook without
it.

---

## The smile fit is cached

`sabr.calibrate` is memoised (`functools.lru_cache`, `sabr.CACHE_SIZE`). It is a
pure function — every argument is a number, a bool or a frozen dataclass, and
`SabrCalibration` and `SabrParams` are frozen with tuple fields — so a cached
result cannot be changed by whoever received it first.

It is worth caching because a reload refits everything: re-reading the workbook
to pick up one edited cell recalibrated all fourteen pairs. That is a few
seconds of scipy per reload on the desk, and it was 97% of the suite's original
hour. The worst single test went 185s → 58s on the cache alone.

`calibrate.cache_clear()` empties it; nothing needs to. A changed quote is a
different key, not a stale entry.

**Still on the table:** `SmileSlice.build` is pure in the same way and is the
next hot spot — `TestMarketMakerPanel`'s slowest test spends 76 of its 80
seconds in `smile.fit_svi` under the wing fine-tune. It is *not* cached, because
`SmileSlice` is a plain mutable `@dataclass` holding writable numpy arrays,
unlike `SabrCalibration`. Caching it means freezing it or copying on the way
out; that is a decision about production pricing code, not a test tweak.

---

## Where the time goes

Sequentially the suite is about fifteen minutes. Measured per module, roughly:

```
test_marketmaker 164s   test_marks       133s   test_workbook    117s
test_publish     113s   test_analysis     73s   test_pricing      55s
test_marketdata   51s   test_numerics     38s   test_kace         38s
test_screens      38s   test_marking      33s   test_calendar     21s
test_listed       12s   test_quotegrammar  2s   test_agent         2s
```

`test_marketmaker` is the wall time in CI, and what is left in it is genuine
model work — SVI slice fitting inside the wing fine-tune — not workbook width.
If it grows, look at the fine-tune before looking at the fixture.

---

## CI

Two workflows, and the split between them is the point.

**`.github/workflows/test.yml`** runs the suite on `ubuntu-latest`, one job per
module per locale — thirty jobs, `fail-fast: false` so every module reports.
Wall time is whichever module is slowest. The `cp1252` half runs the same
modules under `PYTHONUTF8=0 LC_ALL=C`, which is the only way to catch a text
encoding bug written on a Mac before the Windows runner finds it. Both halves
install `esprima` and `xlrd` — not runtime dependencies, and deliberately not in
`requirements.txt`, but the front-end syntax test and the Murex round-trip skip
silently without them, which is how the Murex round-trip went untested for
months.

**`.github/workflows/build-windows.yml`** `needs:` that workflow and passes
`build_exe.py --skip-tests`. The suite used to be step 3 of `build_exe.py` on
the Windows runner, so every exe cost it: ninety minutes to be told a test was
pinned to the wrong workbook. Nothing ships untested — the build does not start
unless the suite is green — and the check that genuinely has to happen on
Windows, running the exe just built and making it price something, is
`build_exe.py`'s own smoke step and still runs.

`--skip-tests` by hand means nothing ran the suite at all. Only CI should pass
it.

`build_windows_github.sh` is unchanged and still checks that the workflow on
the default branch declares `onefile`, `hidden_tabs` and `exclude_tabs` before
dispatching.
