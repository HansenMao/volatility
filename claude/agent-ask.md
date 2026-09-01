# volkit §19 — The question agent (`ask.py`)

Extracted verbatim from `CLAUDE.md` §19. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The quoting agent answers *what do I show* and hands back a price; the marking
agent answers *where should the surface be* and hands back a proposal. Each
has one output shape and a test pinning it. "How wide has the 3M fly been
shown this month, and by whom" has neither shape, so it is a **third agent**
rather than a conversation bolted onto one of the first two -- and it is
built on one rule that decides everything else about it:

- **It writes nothing.** It reads the archive, the journal, the knowledge bank
  and the surface and answers. It never prices, proposes, files a quote,
  journals a verdict or touches the book. The other two agents each have one
  writing route (§17 `file`, §18 `record`); this one has none, so a chat box
  can never be the way a width or a mark changed. A test asks about every
  topic and checks the archive and the journal byte-for-byte afterwards.
- **A question is parsed into a query, and volkit runs the query.**
  `ask.parse_question` reads the pair, tenor, instrument, delta, window and
  topic; `ask.TOPICS` is the one declaration of what can be asked (`widths`,
  `levels`, `trades`, `outcomes`, `shown`, `archive`, `journal`,
  `tendencies`, `marks`, `rules`), and every fact comes from `synthesis`,
  `marking.learn`, `curves.surface_curve`, the bank or the archive itself,
  tagged with its source. The surface is read in decimals and converted
  **once**, at this edge, to the points the archive beside it is in (§4).
- **The model may rewrite a question it cannot read; it may never answer
  one.** A question the grammar does not recognise is sent to the local model
  to be put into the grammar's own vocabulary, under `llm.invented_numbers`
  -- "the front end" may not come back as `1M`, because 1 is not in the
  question -- and the grammar then reads the rewrite. The paragraph at the
  end is `llm.narrate` over the fact list, refused whole if it holds a number
  the facts (or the question) do not. Without a model the answer is the fact
  list and `model_note` says so.
- **A question it cannot answer is refused with the list of what it can.**
  "What printed in the 3M" answered with what was *quoted* in the 3M would
  look exactly like the dissemination file. Asked to do something -- fetch,
  re-mark, record, quote -- it names the command or button that does it and
  does not.
- **A follow-up fills only its gaps, and says which.** "And the 3M?" after a
  widths question inherits the topic, pair and instrument and lists them in
  `Question.inherited`. `Conversation.from_json` rebuilds the previous
  question from its *text*, never from the posted structure, so a transcript
  cannot carry a pair the grammar would not have read.
- **The book is lazy.** `ask()` takes a `Book` or a callable; a question about
  the archive never pays for a workbook it does not read. The CLI caches one
  load per session, failures included.

Two ways in, like the other agents. `volkit agent ask PAIR "question"` on the
command line -- without a question it reads a line at a time -- and the **Ask
the record** card in the market-maker tab, under the marking agent, on
`/api/mm/ask`. Both belong to `mm`: a build without that tab has no archive
card to ask about, and excluding it takes all three agents.

- **The browser owns the transcript and posts it whole** (`const AK` in the
  page, pinned against `ask.panel_from_request`), kept in `localStorage`
  because it is a per-browser convenience and nothing else. The server keeps
  no turn; `AskPanel.run` rebuilds the previous question from the transcript's
  *text* and answers, so a turn on the card and the same turn in a shell are
  one function.
- **The evidence settings are the quoting-agent card's own boxes** (half-life,
  minimum evidence, lookback, whether model-read observations count), read
  by both, so the chat and the widths beside it never disagree about what
  the archive holds.
- **A workbook is optional to the route.** A server whose book failed to load
  still answers about the archive; a question about the surface says the
  surface is not there. The model is looked up per request, like the other
  agent routes, so starting Ollama mid-morning is enough.
