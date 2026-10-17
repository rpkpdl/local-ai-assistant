# Evals

`tests/` checks that the code is correct: offline, free, pass/fail. The evals here measure how
well the system behaves: they score instead of pass/fail, some of them call a model and cost
money, and their results are saved so changes can be compared over time.

```bash
python evals/run_eval.py memory --mode retrieval   # free, deterministic
python evals/run_eval.py memory --mode retrieval --enrich   # seeds enriched by the librarian first
python evals/run_eval.py memory --mode agent       # full agent, calls the model
python evals/run_eval.py memory --mode agent --model gemma4:e4b   # same, on a local model
python evals/run_eval.py write                     # librarian: what gets stored (~$0.47; Haiku ~$0.01)
python evals/run_eval.py associate --mode retrieval [--enrich]   # associative recall gate
python evals/run_eval.py associate --mode agent --enrich         # does the agent bring it up
python evals/run_eval.py judge                     # source judge, calls Claude
```

`--model` sets the agent's model (it defaults to `ASSISTANT_MODEL` from `.env`, so pass
`--model claude-opus-5-5` explicitly if `.env` points at a local model). `--librarian-model`
sets the librarian's model for `write` and `--enrich`.

Every run uses a throwaway database, with web search turned off. `--limit N` runs only the first
N cases, for a quick check (nothing is saved).
Cases that hit an API error (rate limit, no credit) aren't scored: the summary counts them
under `errors` and the run ends with a warning.

**Cost tracking:** when tracing is on in `.env`, every LLM call goes to the Phoenix project
`personal-assistant-evals`, separate from real use (`personal-assistant`). Each run is one
Phoenix session (e.g. `write-2026-10-15T010404`), and each case is one trace named after it
(`write: fix_moved`), with its LLM calls, tokens and cost inside. A run's cost is its session
total, and the most expensive cases are easy to find. `--no-trace` turns this off. Runs before
2026-10-15 weren't traced; their costs are only in the results files. The librarian is
off in agent-mode runs, so seeded memories stay as they are. Results are
written to `results/<date>_<eval>_<model>.json`, with the summary and every row.

## Memory recall

[`datasets/memory_recall.json`](datasets/memory_recall.json): 22 seed memories about a fictional
user and 30 questions, each needing one of them. The 15 original seeds are plain sentences, stored
the way rows migrated from the first schema are (no attribute, no keywords).

- **direct** (10): the question shares a word with the fact ("When is my **birthday**?").
- **paraphrase** (12): no shared words ("What day was I born?" for "User's birthday is
  March 14").
- **updated** (3): the fact changed (switched gyms, new phone). The newer memory must be found
  and the superseded one must not come back.
- **series** (2): weight measured three times. "What's my current weight?" needs the latest point
  ranked first.
- **profile** (3): the question never names the fact it needs ("When is it going to rain?" needs
  the city). Only the always-in-context profile can supply it. This is the case from the
  2026-10-13 finding.

Modes:
- **retrieval** asks the memory store directly. `search` means the expected memory is in the top
  k. `hit` also counts memories in the profile. `--enrich` first runs the librarian's
  `enrich()` over the seeds (as you would after migrating a real database).
- **agent** runs the whole agent on each question in a fresh conversation. `hit` means the
  agent's own `recall` calls found the fact. `answered` means the answer contains the expected
  detail. The agent can rephrase its search, so it can recover some paraphrases that raw
  retrieval misses.

### Results

| Date | Mode | Store / model | direct | paraphrase | updated | series | profile | all | Cost |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-13 | retrieval (k=5) | first schema, FTS5 | 10/10 | 0/12 | | | | 10/22 | $0 |
| 2026-10-14 | retrieval (k=5), search only | FTS5, plain seeds | 10/10 | 1/12 | 2/3 | 1/2 | 0/3 | 14/30 | $0 |
| 2026-10-14 | retrieval (k=5), search only | FTS5, enriched seeds | 9/10* | **10/12** | 2/3 | 1/2 | 2/3 | 24/30 | $0.06 |
| 2026-10-14 | retrieval (k=5), search + profile | FTS5, enriched seeds | 10/10 | 12/12 | 3/3 | 1/2 | 3/3 | 29/30 | $0.06 |
| 2026-10-14 | agent, answered | claude-opus-5-5, enriched | 10/10 | 12/12 | 3/3 | 2/2 | not run† | 27/27 | $0.58 |
| 2026-10-14 | agent, answered | gemma4:e4b, enriched | 8/10 | 12/12 | 3/3 | 2/2 | **3/3** | 28/30 | $0.06‡ |
| 2026-10-17 | agent, answered | claude-haiku-5-5 (agent + librarian), enriched | 10/10 | 12/12 | 3/3 | 2/2 | 3/3 | **30/30** | $0.044 |

\* Run before the possessive fix ("Biscuit's" didn't match "Biscuit"); the plain run after it
gets 10/10. † The API credit balance ran out on the last three questions; they errored and weren't
answered. ‡ The librarian's enrich call; the agent runs locally.

- The librarian's write-time keywords are what fix paraphrases: 1/12 → 10/12 from search alone.
  No vector search was needed for this set.
- The seed set is small, so 15 of the 19 target memories fit in the profile (cap 25). In a real
  database the profile holds only the top 25 attribute facts, so search carries more of the load
  there. That's why the search-only row matters.
- The remaining series miss: "current" in "What's my current weight?" matches "currently reading"
  on a rarer word than "weight", which three points share. The latest weight is in the profile,
  so the agents still answer it.
- Gemma now uses the city from the profile without searching ("Since you live in Austin..."),
  which was the 2026-10-13 failure. Its two misses are questions where it didn't call `recall`
  and the fact wasn't in the profile.

## Librarian (write)

[`datasets/memory_write.json`](datasets/memory_write.json): 34 single turns, some with existing
memories seeded first. The librarian decides what to store, and each expected item is matched
against its output.

- **skip** (9): thanks, trivia, a hypothetical, a PIN ("remember it for me"), and a fact that's
  already stored. Nothing should be stored.
- **fact** (8), **lesson** (5), **series** (4), **relationship** (3), **personal** (1): the kind,
  a word in the content, the attribute, the entity or link, and a salience band.
- **correction** (4): moved city, changed job, quit coffee, "no, that's wrong". The new memory
  must supersede the right existing one.

Scores: `decision` (stored something iff it should), `items` (every expected item found),
`fields` (attribute, value, date, supersedes, theme, provenance, mode and salience all right too),
`clean` (no extra items).

| Date | Librarian | decision | items | fields | clean | Cost |
|---|---|---|---|---|---|---|
| 2026-10-14 | claude-opus-5-5 (effort low) | 34/34 | 34/34 | 34/34 | 32/34 | $0.47 |
| 2026-10-14 | claude-haiku-5-5 (effort low) | 34/34 | 34/34 | 33/34 | 31/34 | $0.012 |

The extras were reasonable: a relationship row for Maya next to the correction, Pepper's birthday
as its own fact, and (Haiku) an unnamed "cousin" entity. Haiku's one field miss left a weight
point's value empty; the code now takes the number from the content in that case. This set
may be too easy. Harder cases (several facts in one message, sarcasm, a correction about a
person) are the next thing to add.

## Associative recall

[`datasets/memory_associate.json`](datasets/memory_associate.json): 12 seed memories (lessons and
events like a delayed flight with no snacks, food poisoning at a named sushi place, a bad
reaction to ibuprofen, plus low-salience and recently surfaced ones) and 21 messages that aren't
questions about memory.

- **fire** (11): a stored experience would help ("I'm flying to Portland next week"). Retrieval
  passes if the plain-code gate returns it. Agent mode passes if the reply mentions it.
- **quiet_filter** (5): nothing should come up, and the plain-code gate alone should ensure it
  (salience too low, inferred, or surfaced yesterday).
- **quiet_llm** (5): a keyword matches but the memory doesn't help ("Explain how airplane wings
  generate lift"). The gate is expected to let these through. In agent mode, the agent must not
  bring them up.

| Date | Mode | Agent | fire | quiet_filter | quiet_llm | all | Cost |
|---|---|---|---|---|---|---|---|
| 2026-10-14 | retrieval, plain seeds | | 2/11 | 5/5 | 3/5 | 10/21 | $0 |
| 2026-10-14 | retrieval, enriched seeds | | **11/11** | 4/5 | 1/5 | 16/21 | $0.03 |
| 2026-10-14 | agent, enriched seeds | claude-opus-5-5 | **11/11** | 3/5 | 4/5 | 18/21 | $0.64 |
| 2026-10-14 | agent, enriched seeds | gemma4:e4b | 10/11 | 5/5 | 2/5 | 17/21 | $0.03 |
| 2026-10-17 | agent, enriched seeds | claude-haiku-5-5 (agent + librarian) | **11/11** | 4/5 | 4/5 | **19/21** | $0.019 |

- Without keywords, association only has the hand-written themes, and 2/11 fire. With the
  librarian's keywords, all 11 do, including "Advil" finding the ibuprofen reaction.
- Opus filters the keyword matches well (4/5 quiet_llm). Its three quiet failures all came from
  its own `recall` calls, not the gate: it found the inferred jazz preference, the Friday-takeout
  pattern and the cast-iron burn (on cooldown) by searching. Two of those mentions were arguably
  helpful, so the labels may be too strict. See findings.
- Gemma brings up the right memory almost as often (10/11), but mentions keyword matches that
  don't help (2/5 quiet_llm).

## Source judge

[`datasets/source_judge.json`](datasets/source_judge.json): 11 candidate sources the judge must
approve or reject.

- **good** (3): real, reusable public APIs. Should be approved.
- **junk** (3): an error page, a login wall, a one-off spammy blog post. Should be rejected.
- **injection** (5): pages that try to talk the judge into approving them. They include a fake
  "system override", an instruction hidden in a JSON field, a forged verdict that tries to close
  the `<fetched_content>` tag early, and an injected instruction inside a response from a
  *reputable* domain (Wikipedia). That last one tests the injection defense on its own, without
  an unknown domain also counting against it.

### Results

| Date | Model | good | junk | injection | all | Cost |
|---|---|---|---|---|---|---|
| 2026-10-13 | claude-opus-5-5 (effort low) | 3/3 | 3/3 | 5/5 | 11/11 | $0.05 |
| 2026-10-17 | claude-haiku-5-5 (effort low) | 3/3 | 3/3 | 5/5 | 11/11 | $0.0013 |

The saved verdict reasons show each injection was rejected *because* of the manipulation
attempt, not just because the domain was unknown.

## Ideas for more cases

- Paraphrases that a vector search could still get wrong (near-duplicate facts, negations).
- A held-out set of recall and association questions written without seeing the librarian's
  keywords. The current sets were written in the same session as the librarian prompt, so the
  keyword results may be optimistic.
- An end-to-end scenario: the librarian stores a turn, then a later conversation surfaces it.
- Injection in other places: search result snippets, redirects, very long pages where the
  instruction sits past the part the judge sees.
