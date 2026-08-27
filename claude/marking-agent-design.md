# The marking agent, and the two agents conferring

Added 2026-08-27 to `volkit`. Third in the series after `claude/desk-agent-design.md` and `claude/dtcc-download-and-inversion.md`.

## What it is

A second agent, answering a different question. The desk agent asks *what do I show*; this one asks *where should the surface be* — the judgement the marking screen's fitters leave to a person.

**The fitters do not change.** `fit_atm_curve` stays a cold fit, `tune_smile_shifts` stays a curve-wide additive shift under a hinge, every §11 rule stands. What a marker actually agonises over is the judgement *around* the fit — which knobs to free, whether the targets can determine that many parameters, whether anything constrains the wings — and then whether to take the number or nudge it. That is what the agent decides, and the nudging is what it learns.

Three new modules: `remarks.py` (the journal), `marking.py` (the agent), `consult.py` (the exchange). CLI: `volkit mark propose|confer|learn|journal|record`, owned by the marking screen.

## The overfitting problem, and what was done about it

A desk re-marks a curve a few times a day — a few dozen instances a month. That is enough for a handful of scalars with error bars and nowhere near enough for a function. So:

- **Tendencies, not a policy.** Per knob: has this desk been given the chance to move it and declined every time; how far does it typically move it; does it land systematically off the fit. Each carries its instance count and says nothing below a floor of five.
- **A correction must be a tendency, not a scatter.** A bias is applied only when `|median| > spread` (spread = half the IQR, which one outlier cannot set). Otherwise the row reads *this desk lands on both sides of the fit here* and nothing moves. **This single test is what stops the agent learning the desk's noise and quoting it back with confidence.**
- **A correction is capped at half of what the fit itself moved.** A nudge on a fitted number is a nudge; a nudge that can exceed the fit is a second, unexamined fit with a smaller sample behind it.
- **Age is not a weight**, unlike the quote archive. A width is a fact about a market that moves; how a desk marks is a fact about the desk. A habit from three months ago is still that desk's habit. What ages out is the window (a year).

## Learning from a human, without instrumenting anything

`session.capture_pair` already photographs every knob, so **a re-marking instance is a diff of two snapshots**. Nothing in the marking screen reports anything, nothing is forgotten when a control is added, and old session files can be turned into instances retroactively.

- **A verdict beats a diff.** `unprompted` is somebody marking; `accepted`/`edited`/`rejected` answer a proposal and carry what a diff cannot — the tool's number and the desk's, side by side, on the same morning. An **edited** proposal is the most valuable row in the file, and it is the whole reason the agent asks rather than only watching.
- **An absent smile shift is a zero; an absent tenor overwrite is not.** One is "they moved it", the other is "they left the curve to speak". Counting them alike loses exactly the decisions a marker thinks hardest about.
- **A rule and a learned reason are labelled apart** in every trace line. A rule is true of the model (four targets cannot determine five parameters); a learned reason is true of this desk and carries the count. Somebody disagreeing must see instantly which kind they are disagreeing with.
- **Every proposal says how much it learned from, including none.**
- **`marked()` verifies the restore rather than assuming it.** A surface left half-marked by a proposal nobody accepted, priced off all morning, is the worst possible outcome of a tool whose job is marking. A fault-injection test pins the guard.

## The two agents conferring

The quoting agent's most interesting output is a flag it is forbidden to apply — *the mark is 0.45 below where this has been quoted*. The marking agent's hardest input is what that flag contains. `volkit mark confer` connects them, in numbers:

1. **A finding** goes quote-side → mark-side: this instrument at this tenor is marked here, has been quoted there, over this many observations from this many brokers, this recently.
2. The mark side turns findings into what the existing fitters already take — a `CurveTarget` for the at-the-money, a two-way `MarketQuote` built from the *observed range* for a wing — and proposes. Only the ATM becomes a curve target: a risk reversal is a statement about shape, and feeding one to a fit that can only move the level asks a level to explain a skew.
3. **A critique** comes back: with that proposal on the book, how many observed markets does the surface sit inside, what improved, and **what it broke**.
4. The mark side weights what it broke and tries again, at most three rounds, then a person decides.

Two things stop this being circular — fit to the archive, score against the archive, of course it improved:

- **The score counts *inside the observed two-way*, not distance to its mid.** Anywhere sensible scores the same, so the loop cannot improve its score by walking the surface onto the middle of every market it has seen. Only leaving a market scores worse.
- **Every finding is scored, including ones no target was built from.** The tenors the fit was not aimed at are exactly where a re-mark gets caught doing damage.

**No language model is anywhere near this.** Both sides produce numbers; a model between them could only paraphrase, and `llm.py`'s numeric guard cannot check a negotiation. A model may describe the winning round, at the end.

## A consequence worth knowing

Once learned pins are in force the fit has fewer free parameters, so its **RMSE gets worse** — 0.015 → 0.037 vol points in the worked example. That is not a regression: the fit is now doing what the desk does rather than what the optimiser likes. The critique reports the trade-off numerically and the person adjudicates it. That tension is the honest output of this design and it is deliberately not hidden.

## Using it

```bash
volkit mark propose EURUSD --target curve.txt --out p.json     # plan, fit, propose
volkit mark record  EURUSD --proposal p.json --verdict edited --session marks.json
volkit mark learn   EURUSD                                     # what it has worked out
volkit mark journal EURUSD                                     # what is held
volkit mark confer  EURUSD --archive mm_archive.jsonl          # the two agents
```

## State

- **34 tests** in `tests/test_marking.py`; 184 passing on the Mac including the other suites. Total across the three test modules is now 632.
- Verified end to end offline: the journal, the tendencies, the plan, the proposal, the critique, the conference, and the full propose → human edits → record → learn cycle through the CLI.
- Documented: `CLAUDE.md` §18 plus the architecture table, README, `USER_MANUAL.md` §5b.

## What is still open

- **No UI card.** §10's four-piece rule has the model and the CLI done; the marking tab card, its route and its panel are the remaining two pieces.
- **Wing findings need real scipy.** The RR/fly path in `findings_from` reaches smile machinery the offline stub cannot run, so only the ATM half of the exchange has been exercised on this machine. It is reported as a note per point rather than failing, and should work on a real install — but it is untested.
- **The numeric web tests still have not run** against any of this session's changes: they need scipy and a real book, and file staging is still refusing with `session_stale_relogin`.
- **The learning has never seen a real journal.** Every tendency above was measured against synthetic instances built to have a known answer. The floors (5 instances, 4 corrections) and the `|median| > spread` test are judgement calls that should be revisited after a month of real marking.
