# Findings

Things observed while building and using the assistant, newest first. Each entry records what
happened, the evidence (traces, eval numbers), the cause, and what was decided. The status line
is updated when a fix lands, so each finding ends with a measured result.

Examples use generic or synthetic details, never the real contents of the memory database.
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
roadmap, unless a held-out question set (written without seeing the keywords) scores much lower. Existing memories
need `python -m assistant.memory.consolidate --enrich` once to get keywords.

**Status:** Shipped in the memory groundwork (uncommitted as of this entry). Results:
[evals/README.md](../evals/README.md).

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

**Status:** Fixed in the memory groundwork (uncommitted as of 2026-10-14). The profile holds user
facts with an attribute, relationships and tracked series, in its own system block. In the recall
eval, gemma4:e4b answered all 3 profile questions using the stored city or diet without a tool
call ("Since you live in Austin..."). The Opus run of those 3 hit the API credit limit, so it
wasn't measured.
