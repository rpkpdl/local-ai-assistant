# Findings

Things observed while building and using the assistant, newest first. Each entry records what
happened, the evidence (traces, eval numbers), the cause, and what was decided. The status line
is updated when a fix lands, so each finding ends with a measured result.

Examples use generic or synthetic details, never the real contents of the memory database.

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

**Status:** Open
