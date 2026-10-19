# Findings

What building and running the assistant has taught us, backed by traces and eval numbers. Each
entry records what was observed, the evidence, the cause, and what was decided. Its status line
names the commit that resolved it and the measured result. Entries are newest first.

Examples use generic or synthetic details, never the real contents of the memory database.

## Key learnings

1. **Put the thinking at write time, not query time.** A one-off LLM pass that writes retrieval
   keywords for each memory fixed paraphrase recall (1/12 → 10/12) with plain keyword search, no
   embeddings needed.
2. **Don't make critical context depend on the model choosing a tool.** A small model never
   called `recall`, so core facts now sit in an always-present profile. Gemma then answered
   every location-dependent question.
3. **A gate only controls what you push.** An agent that searches on its own will find what the
   gate held back. Policy has to apply on both paths.
4. **Measure before assuming you need the big model.** On these tasks Haiku 5.5 matched Opus 5.5
   on every eval at 1/13 to 1/39 of the cost, including the prompt-injection check. The full
   breakdown is in [cost_analysis.md](cost_analysis.md).
5. **Check that the configuration you set is the one running.** Two model settings were silently
   ignored because they were read before `.env` loaded. Only checking the resolved values
   revealed it.
6. **Instrument evals like production.** Eval spend was invisible to the cost dashboard, and
   cross-thread trace context was being lost, until both were traced and verified.
7. **An infrastructure error isn't a wrong answer.** Eval rows that failed on an API error were
   counted as misses, so a run cut short looked like a quality drop. They're now reported
   separately.

## Status index

| Date | Finding | Status |
|---|---|---|
| 2026-10-19 | FastAPI's built-in tracing hid agent turns in Phoenix | Fixed on `fix/fastapi-otel-spans` |
| 2026-10-17 | Haiku 5.5 matches Opus 5.5 on every eval | Shipped, `571c317` |
| 2026-10-14 | Eval rows that errored were scored as misses | Fixed, `4db387e` |
| 2026-10-14 | Eval spend wasn't in Phoenix, and traces split across threads | Fixed, `a506f38` |
| 2026-10-14 | Write-time keywords fix paraphrase recall without vectors | Shipped, `a506f38` |
| 2026-10-14 | The association gate doesn't control the agent's own searches | Open |
| 2026-10-13 | Local model skips memory recall | Fixed, `a506f38` |

---

## 2026-10-19: FastAPI's built-in tracing hid agent turns in Phoenix

**Observed:** The Phoenix span list (root spans only) showed HTTP requests (`GET /api/state`,
`POST /api/chat`, ...) of kind `unknown` with no input or output, and the project stats showed
a total cost of $0. The agent's `turn` spans were missing from the root list.

**Evidence:** FastAPI 0.143 (installed 2026-10-13) emits OpenTelemetry spans for every request
through the global tracer provider, which `phoenix.otel.register()` sets. HTTP root spans go back
to the first traced turns. Until 2026-10-13 `turn` was still a root span of its own, because the
worker thread lost the trace context. After `a506f38` (`in_caller_context`, 2026-10-15) each turn
nested under `POST /api/chat`, so it dropped out of the root list. The stats panel only counts
the spans it lists, so none of the LLM cost showed up there.

**Cause:** An upstream default (FastAPI telemetry is on), together with our own fix for
cross-thread trace context. Neither change had a test of the trace shape.

**Decision / next step:** Turn off FastAPI telemetry with `FastAPI(telemetry={...})`, so `turn`
is the root again, and require `fastapi>=0.143`. `tests/test_server.py` now checks that HTTP
requests produce no spans; it fails without the fix.

**Status:** Fixed on branch `fix/fastapi-otel-spans` (not merged yet). Traces recorded before the
fix keep the old nesting.

---

## 2026-10-17: Haiku 5.5 matches Opus 5.5 on every eval at a fraction of the cost

**Observed:** Each turn ran two Opus calls (agent and librarian), about $0.03 to $0.07 per turn.

**Evidence:** The same evals, with Haiku as the agent, librarian and judge:

| Eval | claude-opus-5-5 | claude-haiku-5-5 |
|---|---|---|
| Memory recall, answered | 27/27 (3 not run) · $0.58 | 30/30 · $0.044 |
| Associative recall | 18/21 · $0.64 | 19/21 · $0.019 |
| Librarian (write) | 34/34 items · $0.47 | 34/34 items · $0.012 |
| Source judge, injections | 11/11, 5/5 · $0.05 | 11/11, 5/5 · $0.0013 |

Haiku's injection rejections cite the manipulation itself, as Opus's do. A live short turn on
Haiku cost $0.00054. Per-call costs, prices and monthly estimates are in
[cost_analysis.md](cost_analysis.md).

**Cause:** These tasks are short and well-specified: tool choice, structured extraction, and a
yes/no verdict. A smaller model handles them, and the evals are small (21-34 cases), so a gap
of one case is noise.

**Decision / next step:** Recommend Haiku for the agent, librarian and judge: three `.env`
settings. The code default stays Opus. Watch real turns in Phoenix for multi-step web tasks,
which the evals don't cover. Switching also turned up a bug: `JUDGE_MODEL` and
`LIBRARIAN_MODEL` were read before `.env` was loaded, so they silently stayed on Opus. `.env` now
loads when the package is imported, and a test covers it.

**Status:** Shipped in `571c317`: the three model settings exist, and Haiku is the recommended
setup. The code default is unchanged. The `.env` loading bug is fixed in the same commit, with a regression test.

---

## 2026-10-14: Eval rows that errored were scored as misses

**Observed:** The API credit balance ran out during the Opus memory-recall run. The last three
questions show as "not answered", which reads as a quality problem.

**Evidence:** The three rows in
`evals/results/2026-10-14_memory_agent_enriched_claude-opus-5-5_lib-claude-opus-5-5.json` have an
empty answer and `"error": "API error 400: ... credit balance is too low ..."`. The summary still
counts them as 0/3 for `profile`.

**Cause:** The runner stores the error on the row but scores the row like any other.

**Decision / next step:** Report errored rows separately: an `errors` column in the summary,
left out of the score, and a warning at the end of any run that had them. Associative-recall
agent runs now record errors too.

**Status:** Fixed in `4db387e`. The same results file now
summarizes as 27/27 answered with 3 errors, instead of 27/30. Results files saved before the fix
keep their old summaries; their rows have the `error` field.

---

## 2026-10-14: Eval spend wasn't in Phoenix, and traces split across threads

**Observed:** Phoenix showed $1.86 of app traffic, but none of the roughly $2.40 spent running evals
that day. Once evals were traced, each agent turn appeared as a separate trace from the eval case
that started it.

**Evidence:** `evals/run_eval.py` cleared `PHOENIX_COLLECTOR_ENDPOINT` on purpose. After that was
changed, a 2-question eval run produced 4 traces, not 2. A run with no LLM calls produced 157
traces containing only SQL queries.

**Cause:**
- Evals were deliberately left untraced to keep them isolated, which also hid their cost.
- `Agent.stream_turn` runs the turn on a worker thread, and OpenTelemetry's context doesn't follow
  a thread on its own. The turn span started a new trace.
- Every SQL query outside a span became its own root trace.

**Decision / next step:** Evals trace into their own Phoenix project, `personal-assistant-evals`,
with one session per run and one trace per case. The turn thread runs in the caller's trace
context (`tracing.in_caller_context`). SQL spans are recorded only inside a turn or tool.
Librarian calls nest under their turn.

**Status:** Fixed in `a506f38`. Re-run: 2 cases → 2 traces in one session, with tokens on every
LLM span. Runs before this commit are only in the results files.

---

## 2026-10-14: Write-time keywords fix paraphrase recall without vectors

**Observed:** Keyword search missed nearly every paraphrase (0/12, then 1/12 with the porter
stemmer) because the question and the memory share no words ("What's my usual hot beverage?"
vs "User drinks green tea every morning").

**Evidence:** The librarian now writes 8-20 `keywords` per memory: names from the content
plus words the user might say later. With the seeds enriched that way (one Claude call,
$0.06):

| Recall eval, k=5, search only | direct | paraphrase | all 30 |
|---|---|---|---|
| plain seeds | 10/10 | 1/12 | 14/30 |
| enriched seeds | 9/10 | **10/12** | 24/30 |

Associative recall improved the same way. With hand-written themes only, the gate surfaced 2 of
11 helpful memories; with keywords, 11/11. For example, "About to take some Advil" finds the
ibuprofen reaction.

**Cause:** The expensive step (thinking of synonyms and situations) runs once per memory, at write
time, instead of per query. This is query expansion stored as data.

**Decision / next step:** Proposed, not yet decided: keep FTS5 and move vector search down the
roadmap, unless a held-out question set (written without seeing the keywords) scores much
lower. Existing memories need `python -m assistant.memory.consolidate --enrich` once to get
keywords.

**Status:** Shipped in `a506f38`. Haiku 5.5 as the librarian gives the same recall (30/30
answered in agent mode). Results: [evals/README.md](../evals/README.md).

---

## 2026-10-14: The association gate doesn't control the agent's own searches

**Observed:** In the associative recall eval on Opus, 3 of 10 "stay quiet" messages got a
memory mentioned anyway. Examples: an inferred, low-salience preference ("since you've asked for
a jazz playlist before"), and a lesson that was on its 2-day cooldown.

**Evidence:** None of the three came through `<possibly_relevant_memories>`. Opus called `recall`
itself on "recommend music for studying", "what should I get for dinner" and "searing a steak",
and used what it found. Gemma, which rarely calls `recall`, kept all 5 filter-gated probes quiet.

**Cause:** The gate (salience, provenance, cooldown) only decides what is pushed into the message.
Direct recall returns every current memory and doesn't show provenance, so the agent can't tell an
inferred guess from something the user said.

**Decision / next step:** Not fixed yet, because two of the three mentions were arguably helpful
(the takeout habit, the cast-iron burn), so the eval labels may be too strict. Cheap options, if
it's annoying in practice: show `inferred`/`observed` in `recall` output so the agent can hedge,
or mark memories surfaced in the last 2 days.

**Status:** Open

---

## 2026-10-13: Local model skips memory recall

**Observed:** Asked "When is it going to rain?". On Gemma 4 (e4b), the assistant replied that it
needed the user's location, although the location was stored in memory.

**Evidence:** The Phoenix trace for the turn shows one LLM call and no tool calls. The same
question was then run on a copy of the same memory database:

| Model | What it did | Result |
|---|---|---|
| claude-opus-5-5 | `recall("user location city home")` → `find_sources` → `http_get` (saved weather API) | Correct local forecast. $0.056, 12 s |
| gemma4:e4b | Answered directly with no tool calls (2 of 2 runs) | Asked for the location. $0, 10 s |

**Cause:**
- Small models are unreliable at deciding *when* to use a tool. The system prompt tells the
  agent to `recall` before answering anything that depends on the user, and even uses "weather
  here" as the example. Claude follows it; the 4B model doesn't.
- Keyword search also needs the right words. Claude found the memory partly because it
  rewrote "rain" into "location city home". A `recall("rain")` would miss "lives in <city>"
  (the 0/12 paraphrase result in [evals](../evals/README.md)).

Vector search alone wouldn't fix this, because the local model never searched at all.

**Decision / next step:**
- Add **core memories**: a small, always-in-context profile (location, name and other basics,
  size-capped) added to every request, so key facts don't depend on the model choosing to
  search. Search stays for everything else. When prompt caching is added, the profile goes in
  its own block after the fixed system prompt, so the fixed part still caches.
- Add "When is it going to rain?" (expects the location memory) to the memory eval, and run
  agent mode on both models to measure the gap.

**Status:** Fixed in `a506f38`. The profile holds user facts with an attribute, relationships and
tracked series, in its own system block. In the recall eval, gemma4:e4b answered all 3 profile
questions from the stored city or diet without a tool call ("Since you live in Austin...").
claude-haiku-5-5 also answered 3/3 (2026-10-17). The Opus run of those 3 hit the API credit
limit and wasn't measured.
