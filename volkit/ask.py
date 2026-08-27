"""The third agent: what do you know, and where did it come from.

The quoting agent answers *what do I show* and hands back a price; the marking
agent answers *where should the surface be* and hands back a proposal.  Each
has one output shape and a test that pins it.  A question in English -- "how
wide has the 3M fly been shown this month, and by whom" -- has neither shape,
and bolting a conversation onto either agent would give it a second output
that is not a price or a proposal.  So it is a third agent, and it differs
from the other two in one way that decides everything else about it:

**It writes nothing.**  It reads the archive, the journal, the knowledge bank
and the surface, and it answers.  It never prices, never proposes, never
files a quote, never journals a verdict, never touches the book.  The other
two agents each have exactly one writing route and §17 / §18 say what may and
may not move through it; a chat box that could be the way a width changed
would have to defend that line by line.  This one cannot, and a test pins the
files byte-for-byte across a turn.

**A question is parsed into a query, and the query is run by volkit.**  The
grammar reads the question first; a local model may *rewrite* a question the
grammar could not read into the grammar's own vocabulary, under the same
numeric guard ``llm.py`` puts on everything else, and then the grammar reads
the rewrite.  What the model never does is answer.  Every fact in an answer
was computed by ``synthesis``, ``marking``, ``curves`` or the archive itself,
carries the source it came from, and the optional paragraph at the end is
narrated *from* those facts by ``llm.narrate`` -- refused whole if it holds
a number the facts do not.  Without a model the answer is the fact list, and
the answer says which of the two it was.

**A question it cannot answer is refused with the list of what it can.**  The
one thing worse than no answer is a plausible one to a question that was
misread: "what printed in the 3M" answered with what was *quoted* in the 3M
would sit on the screen looking like the dissemination file.

**It hands off rather than acts.**  Asked to fetch, to re-mark or to record,
it names the command or the button that does that and does not do it.  A
fetch is a command somebody watches; a re-mark goes through the marking
agent's card so the journal sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import archive as arch
from . import synthesis as syn
from .timeutil import TenorError, tenor_to_years

DAYS_IN_YEAR = 365.2425

#: What a question may be about.  One declaration; the grammar, the refusal
#: message and the answer all read it, so a topic the grammar recognises and
#: the answer cannot build is impossible by construction.
TOPICS = ("widths", "levels", "trades", "outcomes", "shown", "archive",
          "journal", "tendencies", "marks", "rules")

_TOPIC_HELP = {
    "widths": "how wide something has been shown (the archive's two-ways)",
    "levels": "where something has been quoted, against the mark",
    "trades": "what printed in the dissemination files, and the volatility it implies",
    "outcomes": "what became of the prices we showed",
    "shown": "the prices we made",
    "archive": "what the archive holds",
    "journal": "every time somebody moved a mark",
    "tendencies": "what this desk does after a fit",
    "marks": "where the surface is marked",
    "rules": "the knowledge bank's widths and shifts",
}

#: How the grammar hears each topic.  Whole words, lower case; a phrase is
#: matched as a phrase.  Order matters only for the refusal text.
_TOPIC_WORDS = {
    "widths": ("wide", "width", "widths", "spread", "spreads", "two-way", "two way",
               "bid/offer", "choice"),
    "levels": ("level", "levels", "quoted", "quoting", "where has", "where is the market",
               "where was", "run", "runs", "market level"),
    "trades": ("trade", "trades", "traded", "print", "prints", "printed", "dtcc", "sdr",
               "dissemination", "business", "notional", "premium"),
    "outcomes": ("outcome", "outcomes", "hit", "lifted", "hit rate", "became of",
                 "done away", "passed", "missed", "our record", "were we right"),
    "shown": ("we showed", "we show", "we made", "we have shown", "we've shown", "our price",
              "our prices", "prices we", "what did we show"),
    "archive": ("archive", "hold", "holds", "how many records", "how many observations",
                "what do you have", "what do you know", "what have you"),
    "journal": ("journal", "re-mark", "re-marked", "remark", "remarked", "moved the",
                "who moved", "changed the mark", "marking history"),
    "tendencies": ("tendency", "tendencies", "habit", "habits", "this desk", "the desk do",
                   "desk does", "learned", "learnt", "reluctant", "bias"),
    "marks": ("mark", "marks", "marked", "surface", "curve", "term structure",
              "where is the", "where are the", "where's the"),
    "rules": ("rule", "rules", "bank", "knowledge bank", "floor", "floors", "shift", "shifts"),
}

#: What the agent is asked to do and will not.  The answer names what does.
_HANDOFFS = (
    (("fetch", "download", "pull from dtcc", "get the file"),
     "this agent reads and does not fetch; 'volkit agent fetch --sdr DIR --days N' "
     "downloads the dissemination files, or the Fetch from DTCC button on the "
     "market-maker tab"),
    (("re-mark", "remark the", "move the mark", "change the mark", "set the atm", "mark the",
      "propose"),
     "this agent moves nothing; a re-mark goes through the marking agent's card so the "
     "journal sees it, or 'volkit mark propose PAIR --file run.txt'"),
    (("record", "file this", "file the", "save", "write"),
     "this agent writes nothing; 'volkit agent shown' records a price, 'volkit agent "
     "outcome' what became of it, and 'volkit mark record' a verdict on a proposal"),
    (("quote me", "make a price", "price the", "price me", "show me a price", "two way in"),
     "a price is the quoting agent's job: 'volkit agent quote PAIR' or the Quote button "
     "on the market-maker tab"),
)

_INSTRUMENTS = {
    "atm": ("atm", "at-the-money", "at the money", "straddle"),
    "rr": ("rr", "rrs", "risk reversal", "risk reversals", "riskies", "risky", "skew"),
    "fly": ("fly", "flies", "butterfly", "butterflies", "bf"),
    "strangle": ("strangle", "strangles"),
    "outright": ("outright", "outrights", "strike", "strikes"),
    "spread": ("calendar", "calendars", "calendar spread"),
}

_TENOR = re.compile(r"(?<![\w.])(\d{1,2})\s*([dwmy])(?:\b|(?=\d))", re.I)
_TENOR_WORDS = re.compile(r"\b(\d{1,2}|one|two|three|six|nine|twelve)\s+(day|week|month|year)s?\b",
                          re.I)
_DELTA = re.compile(r"\b(\d{1,2})\s*(?:d|delta|dl)\b", re.I)
#: Zero-width, so the candidates overlap: ``THE EUR/USD`` must offer EUR/USD
#: after THE EUR has been turned down, and a consuming match would step past it.
_PAIR = re.compile(r"(?=\b([A-Z]{3})[/ ]?([A-Z]{3})\b)")
_DAYS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s+days?\b", re.I)
_WEEKS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,2})\s+weeks?\b", re.I)
_SINCE = re.compile(r"\bsince\s+(\d{4}-\d{2}-\d{2})\b", re.I)
_WORDNUM = {"one": 1, "two": 2, "three": 3, "six": 6, "nine": 9, "twelve": 12}
_UNIT = {"day": "D", "week": "W", "month": "M", "year": "Y"}
_MAX_LIST = 12


class AskError(Exception):
    """A question that cannot be answered, with the reason."""


# --------------------------------------------------------------------------
@dataclass
class Question:
    """What was asked, as the query volkit will run."""

    text: str
    pair: str = ""
    topics: list[str] = field(default_factory=list)
    instrument: str | None = None
    delta: float | None = None
    tenor: str | None = None
    lookback_days: float | None = None
    since: str = ""                    # a YYYY-MM-DD the window starts at, when one was said
    who: bool = False                  # name the sources
    invert: bool = False               # trades: as volatilities
    handoff: str = ""                  # something this agent will not do, and what does
    inherited: list[str] = field(default_factory=list)   # what came from the turn before
    notes: list[str] = field(default_factory=list)
    rewritten: str = ""                # the model's rewrite, when one was used

    @property
    def days(self) -> float | None:
        if not self.tenor:
            return None
        try:
            return tenor_to_years(self.tenor) * DAYS_IN_YEAR
        except (TenorError, ValueError):
            return None

    def describe(self) -> str:
        bits = [self.pair or "no pair"]
        if self.tenor:
            bits.append(self.tenor)
        if self.instrument:
            what = self.instrument.upper()
            if self.delta is not None:
                what = f"{self.delta * 100:g}d {what}"
            bits.append(what)
        bits.append("about " + (", ".join(self.topics) if self.topics else "nothing recognised"))
        if self.lookback_days is not None:
            bits.append(f"over {self.lookback_days:g} days")
        if self.who:
            bits.append("naming sources")
        return " ".join(bits)


@dataclass(frozen=True)
class Fact:
    """One thing the answer rests on, and where it came from."""

    text: str
    source: str                        # archive, journal, surface, bank, note
    topic: str = ""

    def line(self) -> str:
        return f"[{self.source}] {self.text}"


@dataclass
class Answer:
    """What came back: facts first, prose after, and which of the two it was."""

    question: Question
    facts: list[Fact] = field(default_factory=list)
    refused: str = ""
    narration: str = ""
    narration_why: str = ""
    used_model: bool = False
    model_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused

    def fact_lines(self) -> list[str]:
        return [f.text for f in self.facts]

    def lines(self) -> list[str]:
        out = [f"asked: {self.question.describe()}"]
        if self.question.rewritten:
            out.append(f"  read by the model as: {self.question.rewritten}")
        for note in self.question.notes:
            out.append(f"  . {note}")
        if self.refused:
            out.append(f"cannot answer: {self.refused}")
            return out
        if self.narration:
            out.append("")
            out.append(self.narration)
            out.append("")
            out.append("from:")
        for f in self.facts:
            out.append("  " + f.line())
        if self.narration_why:
            out.append(f"  ({self.narration_why})")
        for note in self.notes:
            out.append(f"  . {note}")
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def to_json(self) -> dict:
        q = self.question
        return {
            "question": {
                "text": q.text, "pair": q.pair, "topics": list(q.topics),
                "instrument": q.instrument, "delta": q.delta, "tenor": q.tenor,
                "lookback_days": q.lookback_days, "who": q.who, "invert": q.invert,
                "inherited": list(q.inherited), "notes": list(q.notes),
                "rewritten": q.rewritten, "describe": q.describe(),
            },
            "ok": self.ok,
            "refused": self.refused,
            "facts": [{"text": f.text, "source": f.source, "topic": f.topic} for f in self.facts],
            "narration": self.narration,
            "narration_why": self.narration_why,
            "used_model": self.used_model,
            "model_note": self.model_note,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# the grammar
# --------------------------------------------------------------------------
def _has(low: str, words) -> bool:
    for w in words:
        if " " in w or "/" in w or "-" in w:
            if w in low:
                return True
        elif re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", low):
            return True
    return False


def _find_pair(text: str, known=None) -> str:
    """A six-letter pair in the question, the known list winning over shape."""
    upper = text.upper()
    wanted = None if known is None else {k.upper() for k in known}
    for m in _PAIR.finditer(upper):
        cand = m.group(1) + m.group(2)
        # ``THE ATM`` is three capitals, a space and three capitals.  With a
        # book the pair has to be one it builds; without one both halves have
        # to be currencies, so an English phrase never becomes a pair.
        if wanted is not None:
            if cand in wanted:
                return cand
        elif _looks_like_pair(cand):
            return cand
    return ""


_CCYS = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNH", "CNY", "HKD", "SGD",
         "SEK", "NOK", "DKK", "MXN", "ZAR", "TRY", "BRL", "KRW", "TWD", "INR", "PLN", "HUF",
         "CZK", "ILS", "THB", "IDR", "PHP", "MYR", "RUB", "SAR", "AED", "CLP", "COP", "PEN"}


def _looks_like_pair(cand: str) -> bool:
    return len(cand) == 6 and cand[:3] in _CCYS and cand[3:] in _CCYS and cand[:3] != cand[3:]


def _find_tenor(low: str) -> str | None:
    m = _TENOR.search(low)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = _TENOR_WORDS.search(low)
    if m:
        n = m.group(1).lower()
        n = _WORDNUM.get(n, n)
        # "last three months" is a window, not a tenor; the window regexes
        # run first and strip their text before this is called.
        return f"{int(n)}{_UNIT[m.group(2).lower()]}"
    return None


def _find_window(low: str) -> tuple[float | None, str, str]:
    """A lookback in days, a start date, and the text with the window taken out."""
    m = _SINCE.search(low)
    if m:
        return None, m.group(1), low[:m.start()] + low[m.end():]
    m = _DAYS.search(low)
    if m:
        return float(m.group(1)), "", low[:m.start()] + low[m.end():]
    m = _WEEKS.search(low)
    if m:
        return 7.0 * float(m.group(1)), "", low[:m.start()] + low[m.end():]
    for phrase, days in (("today", 1.0), ("this morning", 1.0), ("yesterday", 2.0),
                         ("this week", 7.0), ("last week", 7.0), ("past week", 7.0),
                         ("this month", 31.0), ("last month", 31.0), ("past month", 31.0),
                         ("this quarter", 93.0), ("last quarter", 93.0),
                         ("this year", 366.0), ("last year", 366.0), ("past year", 366.0),
                         ("all time", 3660.0), ("ever", 3660.0)):
        if phrase in low:
            return days, "", low.replace(phrase, " ")
    return None, "", low


def parse_question(text: str, *, pair: str = "", known_pairs=None,
                   previous: Question | None = None) -> Question:
    """Read a question into the query it is.

    ``previous`` is the turn before, and only *gaps* are filled from it: "and
    the 3M?" after a question about EURUSD widths is a question about EURUSD
    3M widths.  What was filled in is listed on the question, because an
    answer that quietly assumed the pair from two questions ago must not read
    like one that was asked about it.
    """
    q = Question(text=str(text or "").strip())
    if not q.text:
        raise AskError("nothing was asked")
    low = " " + re.sub(r"\s+", " ", q.text.lower()) + " "

    for words, what in _HANDOFFS:
        if _has(low, words):
            q.handoff = what
            break

    window, since_text, rest = _find_window(low)
    q.lookback_days, q.since = window, since_text
    if since_text:
        q.notes.append(f"window: {since_text} onwards")

    q.pair = _find_pair(q.text, known_pairs) or (pair or "").upper()
    # The delta is read and taken out before the tenor is looked for: ``25d``
    # is a delta and would otherwise read as a twenty-five day tenor.
    m = _DELTA.search(rest)
    if m:
        q.delta = int(m.group(1)) / 100.0
        rest = rest[:m.start()] + " " + rest[m.end():]
    q.tenor = _find_tenor(rest)
    for key, words in _INSTRUMENTS.items():
        if _has(rest, words):
            q.instrument = key
            break
    if q.instrument in ("rr", "fly", "strangle") and q.delta is None:
        q.notes.append(f"no delta was given for the {q.instrument.upper()}; every delta is shown")
    q.who = _has(low, ("who", "whom", "which broker", "which brokers", "sources", "from where",
                       "counterparty", "counterparties"))
    q.invert = _has(low, ("vol", "vols", "volatility", "implied", "imply", "implies"))

    for topic, words in _TOPIC_WORDS.items():
        if _has(rest, words):
            q.topics.append(topic)
    # "where is the mark" and "where has it been quoted" share words; a
    # question with both a market word and a mark word is about the gap,
    # which is the levels topic (it reports the mark beside the market).
    if "levels" in q.topics and "marks" in q.topics:
        q.topics.remove("marks")
    # A bare instrument or tenor with nothing else -- "and the 3M?" -- takes
    # the topics of the turn before.
    if previous is not None:
        if not q.topics and previous.topics:
            q.topics = list(previous.topics)
            q.inherited.append("topic")
        if not q.pair and previous.pair:
            q.pair = previous.pair
            q.inherited.append("pair")
        if q.instrument is None and previous.instrument and "topic" in q.inherited:
            q.instrument, q.delta = previous.instrument, previous.delta
            q.inherited.append("instrument")
        if q.tenor is None and previous.tenor and "topic" in q.inherited \
                and q.instrument == previous.instrument:
            q.tenor = previous.tenor
            q.inherited.append("tenor")
        if q.lookback_days is None and previous.lookback_days is not None \
                and "topic" in q.inherited:
            q.lookback_days = previous.lookback_days
            q.inherited.append("window")
    if q.inherited:
        q.notes.append("taken from the question before: " + ", ".join(q.inherited))
    return q


def _refusal(q: Question) -> str:
    cans = "; ".join(f"{t}: {_TOPIC_HELP[t]}" for t in TOPICS)
    return (f"the question was not understood as being about any of the things this agent "
            f"can answer -- {cans}. Say which, e.g. 'how wide has the 1M ATM been shown "
            f"this week' or 'what printed in EURUSD 3M'")


# --------------------------------------------------------------------------
# the model as a second reader of the question
# --------------------------------------------------------------------------
_REWRITE_SYSTEM = """\
You rewrite a trader's question about foreign exchange options into one short \
line of fixed vocabulary, so a program can read it. You are a translator, not \
an analyst, and you never answer the question.

The line may contain, in any order:
  a topic word, one or more of:  widths  levels  trades  outcomes  shown  \
archive  journal  tendencies  marks  rules
    widths      = how wide something has been shown (bid/offer spread)
    levels      = where something has been quoted, against our mark
    trades      = what printed in the DTCC / SDR dissemination files
    outcomes    = what became of the prices we showed (hit, lifted, passed)
    shown       = the prices we made
    archive     = what the archive holds
    journal     = every time somebody moved a mark
    tendencies  = what this desk tends to do after a fit
    marks       = where the surface is marked
    rules       = the knowledge bank's widths
  a currency pair as written in the question, e.g. EURUSD
  a tenor as written, e.g. 1M 3M 1Y
  an instrument: ATM, RR, FLY, strangle, outright
  a delta as written, e.g. 25d
  a window as written: today, this week, last 30 days, since 2026-08-01
  the word "who" if the question asks which brokers or sources
  the word "vol" if the question asks for the volatility trades imply

Rules you must not break:
1. Copy every number exactly as it appears in the question. Never write a \
number that is not in the question.
2. Never answer the question and never add a fact.
3. Reply with the one line and nothing else. If the question is about none of \
the topics, reply with exactly: NONE
"""


def _rewrite(model, text: str) -> tuple[str, str]:
    """The model's reading of the question, or ``("", why not)``."""
    from . import llm
    if model is None or not model.available():
        return "", (model.why_not if model is not None else "no model")
    reply = model.complete(_REWRITE_SYSTEM, f"Question: {text}\n")
    if not reply.ok:
        return "", reply.why
    line = reply.text.strip().splitlines()[0].strip().strip("`").strip()
    if not line or line.upper() == "NONE":
        return "", "the model did not recognise the question either"
    made_up = llm.invented_numbers(line, llm.numbers_in(text))
    if made_up:
        return "", (f"the model's reading of the question was refused: it contained "
                    f"{', '.join(made_up)}, which the question does not")
    return line, ""


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------
def _since(q: Question, now: datetime, default_days: float) -> tuple[datetime, float]:
    since_text = q.since
    if since_text:
        try:
            when = datetime.strptime(since_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return when, max(0.0, (now - when).total_seconds() / 86400.0)
        except ValueError:
            pass
    days = q.lookback_days if q.lookback_days is not None else default_days
    return now - timedelta(days=days), days


def _delta_matches(a, b) -> bool:
    return a is None or (b is not None and abs(a - b) < 1e-9)


def _bucket_label(q: Question) -> str | None:
    d = q.days
    return None if d is None else syn.bucket_of(d)[1]


def _sources(rows) -> str:
    names: dict[str, int] = {}
    for o in rows:
        name = o.counterparty or o.origin or o.source or "unnamed"
        names[name] = names.get(name, 0) + 1
    if not names:
        return "no sources"
    top = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{n} ({c})" for n, c in top[:8]) + (
        f" and {len(top) - 8} more" if len(top) > 8 else "")


def _filter_obs(q: Question, rows):
    out = []
    for o in rows:
        if q.instrument and o.instrument != q.instrument:
            continue
        if not _delta_matches(q.delta, o.delta):
            continue
        if q.tenor and o.tenor.upper() != q.tenor.upper():
            continue
        out.append(o)
    return out


def _answer_widths(q, ans, s: syn.Synthesis, archive, since) -> None:
    label = _bucket_label(q)
    rows = [w for w in s.widths
            if (not q.instrument or w.instrument == q.instrument)
            and _delta_matches(q.delta, w.delta)
            and (label is None or w.bucket == label)]
    if not rows:
        ans.facts.append(Fact(
            f"{q.pair}: no width evidence in the archive for "
            f"{q.instrument.upper() if q.instrument else 'any instrument'}"
            f"{' ' + label if label else ''} in the last {s.lookback_days:.0f} days", "archive",
            "widths"))
    for w in rows:
        ans.facts.append(Fact(f"{q.pair} {w.describe()}", "archive", "widths"))
    if q.who:
        quotes = _filter_obs(q, archive.query(pair=q.pair, kinds="quote", since=since))
        quotes = [o for o in quotes if o.width is not None]
        ans.facts.append(Fact(f"{q.pair} two-ways came from: {_sources(quotes)}",
                              "archive", "widths"))


def _answer_levels(q, ans, s: syn.Synthesis, book, cut, method) -> None:
    rows = [lv for lv in s.levels
            if (not q.instrument or lv.instrument == q.instrument)
            and _delta_matches(q.delta, lv.delta)
            and (not q.tenor or lv.tenor.upper() == q.tenor.upper())]
    if not rows:
        ans.facts.append(Fact(
            f"{q.pair}: no quoted level in the archive for "
            f"{q.tenor or 'any tenor'} {q.instrument.upper() if q.instrument else ''}".strip(),
            "archive", "levels"))
        return
    curve = _surface(book, q.pair, cut, method, ans) if book is not None else None
    for lv in rows:
        ans.facts.append(Fact(f"{q.pair} {lv.describe()}", "archive", "levels"))
        if curve is not None and lv.enough:
            mark = _mark_at(curve, lv.tenor, lv.instrument, lv.delta)
            if mark is not None:
                gap, why = lv.gap_to(mark)
                ans.facts.append(Fact(
                    f"{q.pair} {lv.tenor} {lv.instrument.upper()} is marked {mark:.3f}; "
                    f"{why}", "surface", "levels"))


def _mark_at(curve, tenor, instrument, delta):
    p = curve.at(tenor)
    if p is None:
        return None
    if instrument == "atm":
        return p.values.get("atm")
    tag = None if delta is None else f"{int(round(delta * 100))}"
    if instrument == "rr" and tag:
        return p.values.get(f"rr{tag}")
    if instrument in ("fly", "strangle") and tag:
        return p.values.get(f"bf{tag}")
    return None


def _surface(book, pair, cut, method, ans):
    from . import curves
    try:
        book = book() if callable(book) else book
    except Exception as exc:  # noqa: BLE001 - the book is optional here
        ans.notes.append(f"the surface could not be loaded: {exc}")
        return None
    if book is None:
        return None
    try:
        curve = curves.surface_curve(book, pair, cut=cut, method=method)
    except Exception as exc:  # noqa: BLE001
        ans.notes.append(f"the surface for {pair} could not be read: {exc}")
        return None
    # ``curves`` is decimals throughout, by its own declaration; everything a
    # person reads here is in volatility points, like the archive beside it.
    # One conversion, at this edge, so an at-the-money of 0.057 never sits on
    # a line next to a quoted 8.400 -- which it did.
    for pt in curve.points:
        pt.values = {k: (None if v is None else v * 100.0) for k, v in pt.values.items()}
    return curve


def _answer_trades(q, ans, s: syn.Synthesis, archive, since, days, now, hist_pair,
                   discount_rate) -> None:
    label = _bucket_label(q)
    summary = [t for t in s.trades if label is None or t.bucket == label]
    rows = [o for o in archive.query(pair=q.pair, kinds="trade", since=since, until=now)
            if o.action.upper() not in ("CANC", "EROR", "TERM")]
    if label is not None:
        kept = []
        for o in rows:
            d = syn.days_of(o.expiry_date or o.tenor, asof=o.when or now)
            if d is not None and syn.bucket_of(d)[1] == label:
                kept.append(o)
        rows = kept
    if not rows:
        ans.facts.append(Fact(f"{q.pair}: nothing printed in the last {days:.0f} days"
                              f"{' ' + label if label else ''} in the archive; 'volkit agent "
                              f"fetch' brings the dissemination files in", "archive", "trades"))
        return
    for t in summary:
        ans.facts.append(Fact(f"{q.pair} printed {t.describe()}", "archive", "trades"))
    for o in rows[-_MAX_LIST:]:
        ans.facts.append(Fact(f"{o.at[:16]} {o.describe()}"
                              + (" (size capped)" if o.notional_capped else ""),
                              "archive", "trades"))
    if len(rows) > _MAX_LIST:
        ans.notes.append(f"{len(rows) - _MAX_LIST} older trade(s) not listed")
    if q.invert:
        if hist_pair is None:
            ans.facts.append(Fact(
                "the volatility a print implies needs the forward on the trade's own date, "
                "which comes from the historical workbook; none is loaded (--history)",
                "note", "trades"))
            return
        vols, notes = syn.invert_trades(archive, q.pair, asof=now, hist_pair=hist_pair,
                                        lookback_days=days, discount_rate=discount_rate)
        if q.tenor:
            vols = [v for v in vols if syn.bucket_of(v.days)[1] == label]
        for v in vols[-_MAX_LIST:]:
            ans.facts.append(Fact(f"{v.at[:16]} {v.describe()}", "archive", "trades"))
        for n in notes:
            ans.facts.append(Fact(n, "note", "trades"))


def _answer_outcomes(q, ans, s: syn.Synthesis) -> None:
    label = _bucket_label(q)
    rows = [oc for oc in s.outcomes
            if (not q.instrument or oc.instrument == q.instrument)
            and (label is None or oc.bucket == label)]
    if not rows:
        ans.facts.append(Fact(f"{q.pair}: no price we showed has an outcome recorded"
                              f"{' for ' + label if label else ''}", "archive", "outcomes"))
    for oc in rows:
        ans.facts.append(Fact(f"{q.pair} {oc.describe()}", "archive", "outcomes"))
        kind, why = oc.lean()
        if kind:
            ans.facts.append(Fact(f"{q.pair} {oc.instrument.upper()} {oc.bucket}: {why} -- "
                                  f"shown here, and applied to nothing", "archive", "outcomes"))


def _answer_shown(q, ans, archive, since, now) -> None:
    rows = _filter_obs(q, archive.query(pair=q.pair, kinds="shown", since=since, until=now))
    if not rows:
        ans.facts.append(Fact(f"{q.pair}: no prices of ours are recorded in the window",
                              "archive", "shown"))
        return
    ans.facts.append(Fact(f"{q.pair}: {len(rows)} price(s) we showed", "archive", "shown"))
    for o in rows[-_MAX_LIST:]:
        ans.facts.append(Fact(f"{o.at[:16]} {o.describe()}"
                              + (f" to {o.counterparty}" if o.counterparty else ""),
                              "archive", "shown"))


def _answer_archive(q, ans, archive, s: syn.Synthesis) -> None:
    rows = [r for r in archive.summary() if not q.pair or r["pair"] == q.pair]
    if not rows:
        ans.facts.append(Fact(f"the archive at {archive.path} holds nothing"
                              + (f" for {q.pair}" if q.pair else ""), "archive", "archive"))
        return
    for r in rows:
        ans.facts.append(Fact(
            f"{r['pair']}: {r['records']} record(s) -- {r['quote']} quote, {r['trade']} trade, "
            f"{r['shown']} shown, {r['outcome']} outcome; {r['first'][:10]} to {r['last'][:10]}"
            + (f"; {r['model_read']} read by a model" if r["model_read"] else ""),
            "archive", "archive"))
    if q.pair:
        ans.facts.append(Fact(f"{q.pair}: {s.counted} observation(s) inside the last "
                              f"{s.lookback_days:.0f} days", "archive", "archive"))
        for n in s.notes:
            ans.facts.append(Fact(n, "archive", "archive"))


def _answer_journal(q, ans, journal, now) -> None:
    if journal is None:
        ans.facts.append(Fact("no re-marking journal is loaded (--journal)", "note", "journal"))
        return
    since, days = _since(q, now, 365.0)
    rows = journal.query(pair=q.pair, since=since, until=now)
    if not rows:
        ans.facts.append(Fact(f"{q.pair}: nothing in the journal in the last {days:.0f} days",
                              "journal", "journal"))
        return
    ans.facts.append(Fact(f"{q.pair}: {len(rows)} re-marking instance(s) in the last "
                          f"{days:.0f} days, {sum(1 for e in rows if e.answered)} answering a "
                          f"proposal", "journal", "journal"))
    for e in rows[-_MAX_LIST:]:
        ans.facts.append(Fact(e.describe() + (f" -- {e.note}" if e.note else ""),
                              "journal", "journal"))


def _answer_tendencies(q, ans, journal, now) -> None:
    if journal is None:
        ans.facts.append(Fact("no re-marking journal is loaded (--journal)", "note",
                              "tendencies"))
        return
    from . import marking
    _, days = _since(q, now, 365.0)
    t = marking.learn(journal, q.pair, asof=now, lookback_days=days)
    for line in t.lines():
        ans.facts.append(Fact(line.strip(), "journal", "tendencies"))


def _answer_marks(q, ans, book, cut, method) -> None:
    if book is None:
        ans.facts.append(Fact("no workbook is loaded, so there is no surface to read", "note",
                              "marks"))
        return
    curve = _surface(book, q.pair, cut, method, ans)
    if curve is None:
        return
    ans.facts.append(Fact(f"{q.pair}: {curve.source}, as of {curve.asof[:16]}", "surface",
                          "marks"))
    points = [p for p in curve.points if not q.tenor or p.tenor.upper() == q.tenor.upper()]
    if q.tenor and not points:
        ans.facts.append(Fact(f"{q.pair}: {q.tenor} is not a quoted tenor on this book; "
                              f"the quoted ones are "
                              + ", ".join(p.tenor for p in curve.points), "surface", "marks"))
    for p in points:
        v = p.values
        if p.message and v.get("atm") is None:
            ans.facts.append(Fact(f"{q.pair} {p.tenor}: could not be read -- {p.message}",
                                  "surface", "marks"))
            continue

        def f(key):
            x = v.get(key)
            return "n/a" if x is None else f"{x:.3f}"

        if q.instrument == "atm":
            body = f"ATM {f('atm')}"
        elif q.instrument == "rr":
            body = f"25d RR {f('rr25')}, 10d RR {f('rr10')}"
        elif q.instrument in ("fly", "strangle"):
            body = f"25d fly {f('bf25')}, 10d fly {f('bf10')}"
        else:
            body = (f"ATM {f('atm')}, 25d RR {f('rr25')}, 25d fly {f('bf25')}, "
                    f"10d RR {f('rr10')}, 10d fly {f('bf10')}")
        ans.facts.append(Fact(f"{q.pair} {p.tenor}: {body}"
                              + (f" ({p.message})" if p.message else ""), "surface", "marks"))


def _answer_rules(q, ans, bank) -> None:
    if bank is None:
        ans.facts.append(Fact("no knowledge bank is loaded", "note", "rules"))
        return
    pk = bank.for_pair(q.pair)
    rules = [r for r in pk.rules
             if (not q.instrument or not r.instrument or r.instrument == q.instrument)
             and (q.delta is None or r.delta is None or abs(r.delta - q.delta) < 1e-9)]
    if not rules:
        ans.facts.append(Fact(f"{q.pair}: the knowledge bank holds no rule for this", "bank",
                              "rules"))
        return
    ans.facts.append(Fact(f"{q.pair}: {len(rules)} bank rule(s)", "bank", "rules"))
    for r in rules[:_MAX_LIST * 2]:
        ans.facts.append(Fact(r.describe() + (f" -- {r.text}" if r.text else ""), "bank", "rules"))


_ANSWER_SYSTEM = """\
You answer a trader's question about foreign exchange options from a list of \
facts that have already been looked up. You are describing what the record \
says, not forming a view.

Write two or three short sentences answering the question from the facts. \
Where the facts say something is thin, stale, missing or not applied, say so \
plainly.

Rules you must not break:

1. Use only the numbers in the facts you are given. Never compute a new \
number -- no differences, no percentages, no averages, no rounding.
2. Never add a fact, a reason or an opinion that is not in the list.
3. No preamble, no heading, no bullet points, no sign-off. Plain sentences.
"""


# --------------------------------------------------------------------------
def ask(text: str, *, archive: arch.Archive, pair: str = "", book=None, journal=None,
        bank=None, hist=None, model=None, asof: datetime | None = None,
        half_life: float = syn.DEFAULT_HALF_LIFE,
        min_effective: float = syn.DEFAULT_MIN_EFFECTIVE, lookback_days: float = 90.0,
        include_model_read: bool = True, cut: str = "NY", method: str | None = None,
        discount_rate: float | None = None, previous: Question | None = None,
        known_pairs=None, narrate: bool = True) -> Answer:
    """Answer one question from what this tool holds.  Reads everything, writes nothing.

    ``book`` may be a ``Book`` or a zero-argument callable that loads one, so
    a question that never touches the surface never pays for it.  ``hist`` is
    the historical workbook (for the forward a printed trade is inverted
    against) and is optional like everything else here.
    """
    now = asof or datetime.now(timezone.utc)
    try:
        q = parse_question(text, pair=pair, known_pairs=known_pairs, previous=previous)
    except AskError as exc:
        q = Question(text=str(text or ""))
        return Answer(question=q, refused=str(exc))
    ans = Answer(question=q)
    ans.model_note = ("model: " + model.config.describe()
                      if model is not None and model.available()
                      else "no model" + (f": {model.why_not}" if model is not None and model.why_not
                                         else ""))

    if q.handoff:
        ans.refused = q.handoff
        return ans

    if not q.topics:
        line, why = _rewrite(model, q.text)
        if line:
            again = parse_question(line, pair=q.pair or pair, known_pairs=known_pairs,
                                   previous=previous)
            if again.topics:
                again.text, again.rewritten = q.text, line
                again.notes = q.notes + again.notes
                q = again
                ans.question = q
                ans.used_model = True
        elif why and model is not None:
            ans.notes.append(why)
    if not q.topics:
        ans.refused = _refusal(q)
        return ans

    needs_pair = [t for t in q.topics if t != "archive"]
    if needs_pair and not q.pair:
        ans.refused = (f"a question about {', '.join(needs_pair)} needs a currency pair, and "
                       f"none was given or carried over")
        return ans

    since, days = _since(q, now, lookback_days)
    s = None
    if any(t in q.topics for t in ("widths", "levels", "trades", "outcomes", "archive")):
        s = syn.synthesize(archive, q.pair or "", asof=now, half_life=half_life,
                           min_effective=min_effective, lookback_days=days,
                           include_model_read=include_model_read) if q.pair else \
            syn.Synthesis(pair="", asof=now, lookback_days=days)
    hist_pair = None
    if hist is not None and q.pair:
        hist_pair = getattr(hist, "pairs", {}).get(q.pair)

    for topic in q.topics:
        if topic == "widths":
            _answer_widths(q, ans, s, archive, since)
        elif topic == "levels":
            _answer_levels(q, ans, s, book, cut, method)
        elif topic == "trades":
            _answer_trades(q, ans, s, archive, since, days, now, hist_pair, discount_rate)
        elif topic == "outcomes":
            _answer_outcomes(q, ans, s)
        elif topic == "shown":
            _answer_shown(q, ans, archive, since, now)
        elif topic == "archive":
            _answer_archive(q, ans, archive, s)
        elif topic == "journal":
            _answer_journal(q, ans, journal, now)
        elif topic == "tendencies":
            _answer_tendencies(q, ans, journal, now)
        elif topic == "marks":
            _answer_marks(q, ans, book, cut, method)
        elif topic == "rules":
            _answer_rules(q, ans, bank)

    if narrate and model is not None and ans.facts:
        from . import llm
        # The question's own numbers are allowed in the answer: a trader who
        # asked about the 1M is not being lied to by a sentence that says 1M.
        prose, why = llm.narrate(model, ans.fact_lines(), system=_ANSWER_SYSTEM,
                                 extra_numbers=llm.numbers_in(q.text))
        if prose:
            ans.narration, ans.used_model = prose, True
        else:
            ans.narration_why = why
    return ans


# --------------------------------------------------------------------------
@dataclass
class Conversation:
    """The turns so far.  The browser owns it and posts it whole; the CLI holds
    it for the length of one session.  Only the last parsed question is ever
    read back, and only to fill gaps in the next one."""

    turns: list[dict] = field(default_factory=list)
    last: Question | None = None

    def add(self, answer: Answer) -> None:
        self.turns.append({"q": answer.question.text, "a": answer.to_json()})
        if answer.ok:
            self.last = answer.question

    @classmethod
    def from_json(cls, turns) -> "Conversation":
        """Rebuild the one thing the next turn needs from a posted transcript.

        The previous *question* is re-parsed from its text rather than trusted
        from the posted structure, so a transcript cannot carry a pair or a
        topic the grammar would not have read."""
        conv = cls()
        for t in list(turns or []):
            text = str((t or {}).get("q", "")).strip()
            ok = bool(((t or {}).get("a") or {}).get("ok", True))
            if not text or not ok:
                continue
            try:
                conv.last = parse_question(text, previous=conv.last)
            except AskError:
                continue
            conv.turns.append({"q": text})
        return conv


# --------------------------------------------------------------------------
# the card
# --------------------------------------------------------------------------
@dataclass
class AskPanel:
    """The chat card inside the market-maker tab, posted whole.

    The browser owns the transcript and posts it with every question, so the
    server holds no conversation (§4) and ``volkit agent ask`` reproduces a
    turn exactly.  The evidence settings are the quoting-agent card's own boxes,
    read here as they are read there, so the two never disagree about what
    the archive holds.
    """

    pair: str
    text: str
    transcript: list = field(default_factory=list)
    half_life: float = syn.DEFAULT_HALF_LIFE
    min_effective: float = syn.DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 90.0
    include_model_read: bool = True
    cut: str = "NY"
    method: str | None = None
    narrate: bool = True

    def run(self, book, archive: arch.Archive, *, journal=None, bank=None, hist=None,
            model=None, clock=None) -> dict:
        now = clock.now if clock is not None else (
            book.clock.now if book is not None else datetime.now(timezone.utc))
        conv = Conversation.from_json(self.transcript)
        known = None
        if book is not None:
            try:
                known = list(book.pairs)
            except Exception:  # noqa: BLE001 - a fake or a half-loaded book
                known = None
        out = ask(self.text, archive=archive, pair=self.pair, book=book, journal=journal,
                  bank=bank, hist=hist, model=model, asof=now, half_life=self.half_life,
                  min_effective=self.min_effective, lookback_days=self.lookback_days,
                  include_model_read=self.include_model_read, cut=self.cut,
                  method=self.method, previous=conv.last, known_pairs=known,
                  narrate=self.narrate)
        body = out.to_json()
        body["topics"] = list(TOPICS)
        body["turns"] = len(conv.turns)
        return body


def _num(payload: dict, key: str, default: float) -> float:
    raw = payload.get(key, default)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise AskError(f"{key} must be a number, not {raw!r}") from None


def panel_from_request(payload: dict) -> AskPanel:
    """The card's payload, validated.  Every key here is one the page sends."""
    payload = payload or {}
    text = str(payload.get("text", "") or "").strip()
    if not text:
        raise AskError("nothing was asked")
    transcript = payload.get("transcript") or []
    if not isinstance(transcript, list):
        raise AskError("transcript must be a list of turns")
    method = payload.get("method")
    return AskPanel(
        pair=str(payload.get("pair", "") or "").upper(),
        text=text,
        transcript=transcript[-40:],
        half_life=_num(payload, "half_life", syn.DEFAULT_HALF_LIFE),
        min_effective=_num(payload, "min_effective", syn.DEFAULT_MIN_EFFECTIVE),
        lookback_days=_num(payload, "lookback_days", 90.0),
        include_model_read=bool(payload.get("include_model_read", True)),
        cut=str(payload.get("cut", "NY") or "NY"),
        method=(str(method) if method not in (None, "", "default") else None),
        narrate=bool(payload.get("narrate", True)),
    )
