# volkit §10 — Working on this

Extracted verbatim from `CLAUDE.md` §10. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

```
python -m unittest discover -s tests        # 845 tests, ~10m
PYTHONUTF8=0 LC_ALL=C python -m unittest discover -s tests   # as a cp1252 Windows
                                           # box sees it: an ASCII locale is the
                                           # only way to catch an encoding bug
                                           # from a Mac before CI does
pip install esprima                         # enables the front-end JS syntax test
python -m volkit check                      # validate the workbook
python -m volkit events USDJPY --weights    # the pair's events leg by leg, and the weight table
python -m volkit vol USDJPY 5m --strike 25dp -v --feed files/market_feed.csv  # the vol query card
python -m volkit serve --feed files/market_feed.csv --history vol_history.xlsx
python -m volkit serve --auto-reload 30     # re-read the market feed when it changes
                                           # (the pricing tab has the same switch)
python -m volkit analysis EURJPY --history files/history_sample.xlsx --horizon 7
python -m volkit analysis USDJPY --history files/history_sample.xlsx --sabr \
    --realized-basis forward          # wings as (rho, nu), realized on the forward
python -m volkit analysis EURJPY --history files/history_sample.xlsx --horizon 7 \
    --relative-value --weight carry=0.4   # score the whole expiry / strike grid
python -m volkit mm EURUSD --target-source quotes < run.txt   # the fit, on its own
python -m volkit mm EURUSD --file run.txt --request ask.txt --fallback-spread 0.3
python -m volkit mm EURUSD --request ask.txt --target-source none   # the quote, on its own
python -m volkit mm EURUSD --learn < run.txt          # propose widths, --save writes them
python -m volkit mm EURUSD --request ask.txt --archive-width   # the archive on the width ladder
python -m volkit mark propose EURUSD --file run.txt --out p.json   # the marking-agent card's path
python -m volkit mark rules EURUSD                    # the rules of thumb, each against the desk
python -m volkit mark learn EURUSD --no-rules         # the desk-only answer, beside the one above
python -m volkit agent ask EURUSD "how wide has the 3M fly been shown this month, and by whom"
python -m volkit agent ask EURUSD --journal mm_remarks.jsonl   # interactive: a question a line
python -m volkit serve --journal mm_remarks.jsonl     # where the card's verdicts go
python3 files/make_history_sample.py        # regenerate the example history
python3 build_exe.py --host-check           # validate the packaging (Windows exe: on Windows)
python3 build_exe.py --only-tabs pricing,marking   # a build without the other three
./build_windows_github.sh                   # drive the Windows build on CI, fetch the exe
./build_windows_github.sh --explain         # print a failed run's own log
python3 build_exe.py --hidden-tab mm        # built, off until --enable-tab mm
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 --rho -0.2
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --positions book.txt        # aggregated greeks, BS and smile
python -m volkit listed 6E --expiry "2026-09-11 19:00" --forward 1.085 \
    --file quotes.txt --panels more.json --positions book.txt   # several contracts at once
python -m volkit band USDHKD --feed files/market_feed.csv --hazard 3
python -m volkit band USDHKD --feed files/market_feed.csv --fit hazard,weak_share   # propose the
                                           # break regime from both wings at every tenor; marks nothing
python -m volkit monitor EURUSD --history files/history_sample.xlsx \
    --watch EURUSD --watch USDJPY:history@-1m \
    --compare surface --compare history:-30d --field rr25
python -m volkit session marks.json                    # save every mark on the book
python -m volkit session marks.json --show             # what a file holds
python -m volkit --session marks.json vol USDJPY 2024-05-28   # price against them
```

- **There is no browser tooling in this environment.** Layout must be confirmed
  by the user. What *is* checked, as tests in `TestWebAssets`:
  - the JS parses under `esprima` (it tops out at ES2017, so `??` and `?.` are
    downlevelled before parsing — do not add newer syntax);
  - every `$('#id')` resolves to an id in the markup;
  - every class the script looks up with `querySelector('.x')` is one it also
    emits — the panel shell and the painter that fills it are separate
    functions, and nothing else would catch a rename between them;
  - every field the listed panel sends is one `panel_from_request` reads;
  - every field the market-maker panel sends is one `marketmaker
    .panel_from_request` reads, and the same for the curve-comparison panel
    (`curves.panel_from_request`) and the band card
    (`banded.BandTreatment.from_request`);
  - the markup balances and the five panel roots are **siblings**. A missing
    `</div>` once nested one panel inside another, which browsers repair
    silently while the tab renders nothing.
- **`volkit/__init__.py` binds its public names lazily** (PEP 562). Not a
  startup optimisation: `build_exe.py` reads `volkit.screens` to decide what
  to build, and it does that *before* its own dependency-install step -- it is
  what installs numpy. An eager `from .atm import ...` there dragged the
  numeric stack in behind `from volkit import screens` and killed the Windows
  build at its first line. Nothing in `screens`, `paths` or `config` may import
  numpy, scipy or pandas, directly or otherwise; a test pins it.
- **PyInstaller cannot cross-compile.** A Windows exe must be built on Windows
  or by the GitHub Actions workflow. `build_exe.py` is the single build entry
  point -- preflight, deps, **the workbook**, the full test suite,
  `volkit.spec`, staging the user's data beside the exe, then a smoke test of
  the executable it just built. The workbook step is `volkit check` on
  `files/vol_marks.xlsx` before the suite reads it: the suite pins numbers off
  that spreadsheet, so an edit made for reasons that have nothing to do with
  this code can fail the build -- and once did, thirty-one minutes in, with
  `EURGBP: no smile term structure`, because a USDHKD tab had replaced the
  EURGBP one while CONFIG went on naming EURGBP. Reading it first costs two
  seconds and names the sheet. `build_windows.bat` and the workflow are both thin wrappers around
  it, which is what keeps a desk build and a CI build identical. Off Windows
  it refuses instead of producing something unusable; `--host-check` builds
  the same spec for the host, which is how the spec is validated from here.
  Bundled vs staged is the thing to get right: the page and the calendar go
  inside (`paths.resource_dir()`), the workbook, feed and overrides go beside
  the exe (`paths.app_dir()`), and synthetic samples go in `samples/` so
  `find_data_file` cannot pick them up.
- Prefer adding a test that pins the *behaviour that was wrong*, with a comment
  naming the old bug. Most of the suite is written that way.
- A new screen is four pieces, in this order: the model in its own module, a
  `BookService` method plus a route, a CLI subcommand that calls the *same*
  function, and the panel in `index.html`. Doing the CLI from the same entry
  point is what keeps the two honest.
- Sample data files live in `files/` with the script that generates them
  beside them, seeded so they regenerate identically. Synthetic samples are
  never loaded by default — made-up numbers appearing on a screen nobody asked
  for is the same failure as a silent zero.
