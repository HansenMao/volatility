# volkit — project context

Daily FX volatility marking and pricing tool. This file is the standing
context: what the project is, what has been decided, and what must not be
broken. Read it before changing anything.

- `README.md` — install, run, architecture, how to extend
- `USER_MANUAL.md` — for the trader using it
- `MIGRATION.md` — every difference from the legacy tool, the convention audit,
  and the managed-currency work. **The authority on anything that moves marks.**

---

## 1. What this is

A rebuild of a legacy tool (`vol.py`, `cvol.py`, `ssabr.py`, `vols.py`,
`common_functions.py`, `__main__.py`, `rv.py`), which is **still present in the
repo root, untouched, for comparison**. Nothing in `volkit/` imports it.

- ~7,000 lines across 22 modules, 156 tests, `unittest` only (no pytest).
- Runtime deps: numpy, scipy, pandas, openpyxl. Plus `tzdata` on Windows.
- Deliberately no `pysabr`, `xlrd`, `tkcalendar`, and no web framework.

## 2. Standing decisions

These were the user's explicit choices. Do not quietly reverse them.

| Decision | Consequence |
|---|---|
| **Correctness over parity** | Fix model bugs rather than reproduce them. But *document every change that moves a number*, and give the exact input change that restores the old figure. |
| **Model risk, don't remove it** | Never make a risk vanish by construction to make a model tidy. If a bad outcome is possible, put it in the model and let it be marked. (This came from a direct correction — see §6.) |
| **Implement fixes, don't just flag them** | When something is wrong, fix it and note what moves. Leave it switchable only when there is a real reason to reconcile against old marks. |
| **Web UI, not Tkinter** | Stdlib `http.server` only. No Flask/FastAPI. The desk machine may have nothing installed. |
| **Excel stays primary** | `vol_marks.xlsx` must keep working as-is. Abstract behind a data source; do not migrate to YAML. |
| **Nothing fails silently** | The legacy `except: pass` returning `0.0000` is the anti-pattern this project exists to remove. Errors surface with the real message. |

## 3. Architecture

```
timeutil   one day-count (365.2425), one injected Clock, tenor parsing
numerics   bracketed solves, damped fixed points, panel integration
calendars  holiday calendars, spot/expiry rolls, CSV overrides
timeweight intraday / weekend / holiday weighting
black      Black-76, FX delta conventions, strike-from-delta
sabr       Hagan 2002 + calibration (closed-form alpha, global sweep)
smile      arbitrage-constrained SVI, vanna-volga, cached slices
banded     pegged pairs: Beta-on-band body + hazard-rate jump leg
events     dated vol bumps, joint height calibration
atm        the ATM term structure
cross      cross pairs from two legs and a correlation
surface    ATM + smile, greeks, delta strikes, RR / fly
exotics    digitals, one-touch / no-touch, overhedge buffers
pricing    multi-leg strips, strike/expiry specs, per-leg error isolation
marketdata validated Excel reader
feed       spot / forward points from file, interpolated
econ       scheduled economic events (rules + dated table)
book       all pairs, built in dependency order
analytics  rolldown / carry, indication pricing
webapp     JSON API + stdlib server;  web/index.html is the whole front end
cli        every screen has a command-line equivalent
paths      resource vs user-data paths (source and frozen)
preflight  startup checks (tzdata above all)
```

## 4. Invariants — breaking these is a regression

- **The clock is injected.** Nothing calls `datetime.utcnow()` inside the model.
  One `Clock` per book; same clock ⇒ identical numbers.
- **One year length: 365.2425.** The legacy had six.
- **Daily variances sum to the term variance** (holds to 2e-16). Any change to
  the day grid must preserve this.
- **Integration splits at known breakpoints** (hourly edges, event times), then
  fixed Gauss-Legendre. Order 5 ⇒ order 20 changes nothing. Never reach for
  adaptive `quad` on this integrand.
- **Every solve is bracketed** and raises `ConvergenceError` with a diagnosis.
  No bare `fsolve`, no fixed-iteration loops.
- **Smile slices are cached** per (expiry, method, cut, forward). The legacy
  re-ran a 12-parameter optimisation per strike query.
- **Use `scipy.special` ufuncs** (`ndtr`, `ndtri`, `log_ndtr`), not
  `scipy.stats.norm`, in inner loops. That alone was a 13x calibration speedup.

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
5. **Joint event calibration**, modified-following expiry rolls, UK holiday
   observation rule.

## 6. Managed / pegged currencies — read this before touching `banded.py`

The user corrected an earlier design that forced out-of-band probability to
zero: *"you can't force out of band probability to 0. The probability is real I
just need a possible adjustment to better model the jump risk."*

So the model is a **regime mixture**, not a bounded distribution:

- Peg-intact body: Beta on the band (U-shaped when a,b < 1, which matches the
  realised edge-seeking distribution).
- Break leg: a **hazard rate** λ, two-sided and asymmetric, with marked jump
  sizes and post-break volatilities.
- Breach probability is a calibrated **output**, and positive.
- Break risk is a **marked input**, never inferred from a butterfly — a wider
  body and a higher hazard both raise the ATM, so a joint fit is degenerate.
  `solve_hazard=True` inverts it deliberately and reports the sensitivity.

Useful finding: the band alone gives a *negative* USDHKD risk reversal against
a quoted positive one. Most of the quoted skew is peg-break premium.

## 7. Known limitations (flagged, not fixed)

- **Same-day expiries cannot be priced.** `cut_vol` normalises by whole
  volatility days; today's expiry has zero, so vol is zero and Black rejects it.
- **No discount curve.** All premiums are undiscounted forward values.
- **Half-day holidays** (Christmas Eve, day after Thanksgiving) are full days.
- **The band model is library/CLI only**; it is not yet a UI interpolation
  method. The surface works in strike/forward ratio space while a band is
  absolute — that plumbing is the outstanding piece.
- **Central bank dates in `econ_events.csv` are provisional** and BoJ 2026 is
  partly filled.
- **The RR-sign question from `test.py`** (legacy comment says rho = −0.383 for
  a positive RR) was never settled; `pysabr` is not installed. volkit's own
  convention is verified by round-trip.

## 8. Working on this

```
python -m unittest discover -s tests        # 156 tests, ~16s
pip install esprima                         # enables the front-end JS syntax test
python -m volkit check                      # validate the workbook
python -m volkit serve --feed files/market_feed.csv
```

- **There is no browser tooling in this environment.** The front end is
  verified structurally: the JS is parsed with `esprima` and every
  `$('#id')` reference is checked against the markup, both as tests. Layout
  must be confirmed by the user.
- **PyInstaller cannot cross-compile.** A Windows exe must be built on Windows
  (`build_windows.bat`) or by the GitHub Actions workflow. The spec is
  validated by building on the host.
- Prefer adding a test that pins the *behaviour that was wrong*, with a comment
  naming the old bug. Most of the suite is written that way.
