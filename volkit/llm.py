"""A local model, on a short leash.

The model in this package does two jobs and is trusted with neither's answer.

**It rewrites prose into the house grammar.**  A broker run pasted out of a
chat window is not always the tidy shorthand ``quotes.py`` reads.  It is
"morning -- eurusd 1m running 8.2 at 8.6, 100 vega a side, 3m riskies 35/55
euro calls over".  The model turns that into the lines the grammar already
takes, and then **the grammar reads them**.  Nothing the model produces
reaches the archive without ``quotes.parse_quotes`` having accepted it; a line
the parser refuses is refused, reported with the text that produced it, and
never repaired.  The model is a translator between a human and a parser, not a
second parser.

**It writes the explanation.**  The quote, the width, the shading and every
input behind them are computed before any of this is called.  The model turns
that finished record into English.  It is given the numbers and told to use
those and no others.

Between those two jobs sits the one mechanism that makes this safe to run
unattended: **the numeric guard**.  Every number in anything the model returns
must already exist in what the model was given -- a level in the chat it was
reading, or a figure in the decision record it is describing.  A number that
does not is a number the model made up, and the whole output is refused rather
than the number quietly passing through into an archive that later becomes a
width, a rule and a price.  Language models are good at fluent numbers and
that is exactly the problem.  The guard is deliberately strict and it does
produce false refusals; a refused line is shown with its source text so a
person can type it in thirty seconds, which is the right trade against a
fabricated level nobody notices for a month.

The transport is ``urllib`` and nothing else.  This tool ships as a single
executable to a desk machine that may have no packages installed, so an SDK
dependency is not available to it, and the two endpoints worth supporting --
Ollama's own and the OpenAI-compatible one that llama.cpp, vLLM and LM Studio
all speak -- are a POST of JSON each.

**None of this is required.**  With no model configured or none running, every
other part of the agent works: files are ingested by the grammar alone, the
archive fills, the statistics compute, the price is made and the explanation
is the deterministic one.  What degrades is that prose the grammar cannot read
stays unread, and the explanation is a list rather than a paragraph.  The
panel says which of those it is, always -- a build that quietly used a model
and a build that quietly did not must never look the same.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BACKENDS = ("ollama", "openai")

#: Where each backend listens when nobody says otherwise.
DEFAULT_BASE = {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:8080"}

#: A number as it appears in text.  Deliberately includes a leading sign and a
#: bare decimal (``.35``), because a broker writes both.
_NUMBER = re.compile(r"[-+]?(?:\d+\.\d+|\.\d+|\d+)")


class LlmError(Exception):
    """The model was asked for something and could not answer."""


def _canonical(text: str) -> str:
    """One spelling per numeric value, so 8.2, 8.20 and 08.20 compare equal."""
    try:
        value = float(text)
    except ValueError:
        return text.strip()
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def numbers_in(text: str) -> set[str]:
    """Every number in a piece of text, canonically spelled."""
    return {_canonical(m.group(0)) for m in _NUMBER.finditer(str(text or ""))}


def invented_numbers(candidate: str, allowed: set[str]) -> list[str]:
    """Numbers in ``candidate`` that are not in ``allowed``.

    This is the guard.  It is a set membership test and not a similarity
    score, on purpose: "close to a number that was there" is the failure mode,
    not the safe case.  8.60 against a chat that said 8.6 passes because
    :func:`_canonical` spells them the same; 8.65 against that chat does not.
    """
    return sorted(numbers_in(candidate) - set(allowed), key=lambda s: (len(s), s))


@dataclass
class ModelConfig:
    """Which model, where, and how patient to be with it."""

    backend: str = "ollama"
    base_url: str = ""
    model: str = "llama3.1"
    timeout: float = 60.0
    temperature: float = 0.0        # extraction is transcription; there is one right answer
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise LlmError(f"backend {self.backend!r} is not one of {', '.join(BACKENDS)}")
        if not self.base_url:
            self.base_url = DEFAULT_BASE[self.backend]
        self.base_url = self.base_url.rstrip("/")

    @classmethod
    def from_env(cls, **overrides) -> "ModelConfig":
        """Configuration from the environment, overridden by anything passed.

        Environment rather than a new settings file: the settings file this
        tool has (``config.py``) is read only by a double-clicked executable,
        and a desk that wants a model wants it for the command line too.
        """
        raw = {
            "backend": os.environ.get("VOLKIT_LLM_BACKEND", "ollama"),
            "base_url": os.environ.get("VOLKIT_LLM_URL", ""),
            "model": os.environ.get("VOLKIT_LLM_MODEL", "llama3.1"),
            "timeout": float(os.environ.get("VOLKIT_LLM_TIMEOUT", "60") or 60),
            "enabled": os.environ.get("VOLKIT_LLM", "1") not in ("0", "off", "no", "false"),
        }
        raw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**raw)

    def describe(self) -> str:
        if not self.enabled:
            return "no model (turned off)"
        return f"{self.model} on {self.backend} at {self.base_url}"


@dataclass
class Reply:
    """What came back, and what it cost to get it."""

    text: str = ""
    ok: bool = False
    why: str = ""
    seconds: float = 0.0
    model: str = ""


@dataclass
class LocalModel:
    """A local endpoint, asked politely and never depended on."""

    config: ModelConfig = field(default_factory=ModelConfig)
    _checked: bool | None = field(default=None, repr=False)
    _why: str = field(default="", repr=False)

    # ----------------------------------------------------------------------
    def _post(self, path: str, payload: dict, *, timeout: float | None = None) -> dict:
        url = f"{self.config.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.config.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self, *, recheck: bool = False) -> bool:
        """Is there a model to talk to?  Cached, and cheap when the answer is no.

        The check has its own short timeout.  A desk opening this screen with
        nothing running must wait a second to be told so, not the full
        generation timeout -- a tool that appears to hang is a tool nobody
        opens twice.
        """
        if self._checked is not None and not recheck:
            return self._checked
        if not self.config.enabled:
            self._checked, self._why = False, "the model is turned off"
            return False
        path = "/api/tags" if self.config.backend == "ollama" else "/v1/models"
        try:
            req = urllib.request.Request(f"{self.config.base_url}{path}")
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                json.loads(resp.read().decode("utf-8"))
            self._checked, self._why = True, ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._checked = False
            self._why = f"nothing answered at {self.config.base_url}: {exc}"
        return self._checked

    @property
    def why_not(self) -> str:
        return self._why

    def complete(self, system: str, user: str, *, timeout: float | None = None) -> Reply:
        """One exchange.  Never raises; a failure comes back as ``ok=False``.

        Not raising is the point.  Every caller here has a deterministic path
        that works without the model, and an exception would make the model's
        absence an error in a screen that is meant to keep working without it.
        """
        import time
        if not self.available():
            return Reply(ok=False, why=self._why or "no model available")
        started = time.time()
        try:
            if self.config.backend == "ollama":
                out = self._post("/api/chat", {
                    "model": self.config.model, "stream": False,
                    "options": {"temperature": self.config.temperature},
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]},
                    timeout=timeout)
                text = (out.get("message") or {}).get("content", "")
            else:
                out = self._post("/v1/chat/completions", {
                    "model": self.config.model, "temperature": self.config.temperature,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]},
                    timeout=timeout)
                choices = out.get("choices") or []
                text = (choices[0].get("message") or {}).get("content", "") if choices else ""
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as exc:
            return Reply(ok=False, why=f"the model did not answer: {exc}",
                         seconds=time.time() - started)
        text = str(text or "").strip()
        if not text:
            return Reply(ok=False, why="the model returned nothing",
                         seconds=time.time() - started)
        return Reply(text=text, ok=True, seconds=time.time() - started,
                     model=self.config.model)


# --------------------------------------------------------------------------
# Job one: prose into the house grammar
# --------------------------------------------------------------------------
_EXTRACT_SYSTEM = """\
You transcribe foreign exchange option markets from chat messages into one \
fixed format. You are a transcriber, not an analyst.

Write one line per market quoted, and nothing else -- no heading, no \
commentary, no explanation, no code fences.

Each line is:   <tenor> <what> <bid>/<offer> [in <size>]

  tenor    1W 2W 1M 2M 3M 6M 1Y, or a date as YYYY-MM-DD
  what     ATM
           25d RR   (add "<CCY> call over" or "<CCY> put over" only if the
                     message says which way round it is)
           25d FLY
           an absolute strike followed by "call" or "put", e.g. 1.1000 call
           <near>/<far> spread   for a calendar spread
  bid/offer   as written in the message
  size     e.g. "in 100mm vega", only if the message gives one

Rules you must not break:

1. Copy every number exactly as it appears in the message. Never round, \
convert, average, complete or tidy a number. Never write a number that is not \
in the message.
2. If a quote is one-sided, or you cannot tell the tenor, or you cannot tell \
whether a number is a bid or an offer, leave that quote out entirely. \
Leaving it out is correct; guessing is not.
3. Ignore anything that is not a quoted market: greetings, opinions, colour, \
positions, requests, trade confirmations.
4. If a message gives a time, put it at the start of the line: "09:15 1M ATM \
8.20/8.60".

If there are no quotes at all, reply with exactly: NONE
"""


@dataclass
class Extraction:
    """Lines the model produced, and what became of each."""

    lines: list[str] = field(default_factory=list)          # accepted, in house grammar
    refused: list[tuple[str, str]] = field(default_factory=list)   # line, why
    notes: list[str] = field(default_factory=list)
    used_model: bool = False
    seconds: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def extract_quotes(model: LocalModel, text: str, *, pair: str = "",
                   max_chars: int = 12000) -> Extraction:
    """Turn chat prose into candidate lines, refusing anything invented.

    The lines are *candidates*.  They are returned to be parsed, and the
    caller parses them -- this function does not import ``quotes`` so that the
    guard here and the grammar there stay two separate gates rather than one
    function that could grow a shortcut past both.
    """
    out = Extraction()
    if not str(text or "").strip():
        out.notes.append("nothing to read")
        return out
    if not model.available():
        out.notes.append(
            f"no local model, so the prose was left unread ({model.why_not}); "
            f"lines already in the house format were still read by the parser")
        return out

    body = str(text)
    if len(body) > max_chars:
        # Truncated at a line boundary and *reported*.  A silently clipped chat
        # log is a morning of quotes that never existed.
        cut = body[:max_chars].rsplit("\n", 1)[0]
        out.notes.append(
            f"the text was {len(body)} characters and only the first {len(cut)} were read; "
            f"split the file if the rest matters")
        body = cut

    prompt = (f"Currency pair: {pair or 'not stated -- do not invent one'}\n\n"
              f"Message:\n{body}\n")
    reply = model.complete(_EXTRACT_SYSTEM, prompt)
    out.seconds = reply.seconds
    if not reply.ok:
        out.notes.append(f"the prose was left unread: {reply.why}")
        return out
    out.used_model = True

    allowed = numbers_in(body)
    for raw in reply.text.splitlines():
        line = raw.strip().strip("`").strip()
        if not line or line.upper() == "NONE":
            continue
        if line.startswith(("#", "//")) or line.endswith(":"):
            out.refused.append((line, "it is commentary, not a quote"))
            continue
        made_up = invented_numbers(line, allowed)
        if made_up:
            # The whole line goes, not the offending number.  A line with one
            # invented figure in it is a line the model was reasoning about
            # rather than transcribing, and the rest of it is not more
            # trustworthy for being arithmetically unremarkable.
            out.refused.append(
                (line, f"it contains {', '.join(made_up)}, which {'are' if len(made_up) > 1 else 'is'} "
                       f"not in the message"))
            continue
        out.lines.append(line)
    if not out.lines and not out.refused:
        out.notes.append("the model found no quotes in this text")
    if out.refused:
        out.notes.append(
            f"{len(out.refused)} line(s) the model wrote were refused; they are shown so they "
            f"can be typed by hand if they are real")
    return out


# --------------------------------------------------------------------------
# Job two: the finished record into English
# --------------------------------------------------------------------------
_NARRATE_SYSTEM = """\
You explain a foreign exchange option price that has already been decided. \
You are describing a decision, not making one.

You will be given the decision as a list of facts with numbers attached. \
Write two or three short sentences for a trader: what is being shown, what \
set the width, what moved the mid off the model, and anything the record \
flags as thin or stale.

Rules you must not break:

1. Use only the numbers in the facts you are given. Never compute a new \
number -- no differences, no percentages, no averages, no rounding.
2. Never add a reason that is not in the facts.
3. If the facts say something is thin, stale or missing, say so plainly.
4. No preamble, no heading, no bullet points, no sign-off. Plain sentences.
"""


def narrate(model: LocalModel, facts: list[str], *, extra_numbers=()) -> tuple[str, str]:
    """English for a decision, or ``("", why not)``.

    ``facts`` is the decision record already rendered as lines -- the same
    lines the deterministic explanation shows.  The narration is refused
    whole if it contains a number those lines do not, which means the worst
    case is the reader gets the list instead of the paragraph.
    """
    if not facts:
        return "", "there is nothing to explain"
    if not model.available():
        return "", model.why_not or "no model available"
    reply = model.complete(_NARRATE_SYSTEM, "Facts:\n" + "\n".join(f"- {f}" for f in facts))
    if not reply.ok:
        return "", reply.why
    allowed = set()
    for line in facts:
        allowed |= numbers_in(line)
    allowed |= {_canonical(str(x)) for x in extra_numbers}
    made_up = invented_numbers(reply.text, allowed)
    if made_up:
        return "", (f"the explanation was refused: it contained {', '.join(made_up)}, which the "
                    f"decision does not; the itemised version below is what the numbers are")
    return reply.text.strip(), ""
