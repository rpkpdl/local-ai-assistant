# Evals

`tests/` checks that the code is correct: offline, free, pass/fail. The evals here measure how
well the system behaves: they score instead of pass/fail, some of them call a model and cost
money, and their results are saved so changes can be compared over time.

```bash
python evals/run_eval.py memory --mode retrieval   # free, deterministic
python evals/run_eval.py memory --mode agent       # full agent, calls the model
python evals/run_eval.py memory --mode agent --model gemma4:e4b   # same, on a local model
python evals/run_eval.py judge                     # source judge, calls Claude
```

Every run uses a throwaway database, with tracing and web search turned off. Results are
written to `results/<date>_<eval>_<model>.json`, with the summary and every row.

## Memory recall

[`datasets/memory_recall.json`](datasets/memory_recall.json): 15 seed facts about a fictional
user and 22 questions, each needing one of those facts.

- **direct** (10): the question shares a word with the fact ("When is my **birthday**?").
- **paraphrase** (12): no shared words ("What day was I born?" for "User's birthday is
  March 14").

Modes:
- **retrieval** asks the memory store directly. It's a hit if the expected fact is in the top k.
- **agent** runs the whole agent on each question in a fresh conversation. `hit` means the
  agent's own `recall` calls found the fact. `answered` means the answer contains the expected
  detail. The agent can rephrase its search, so it can recover some paraphrases that raw
  retrieval misses.

### Results

| Date | Mode | Model | direct | paraphrase | all | Cost |
|---|---|---|---|---|---|---|
| 2026-10-13 | retrieval (k=5) | SQLite FTS5 (bm25) | 10/10 | 0/12 | 10/22 | $0 |

Keyword search finds every direct question and none of the paraphrases. That's the
baseline for the planned hybrid keyword + vector search.

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

The saved verdict reasons show each injection was rejected *because* of the manipulation
attempt, not just because the domain was unknown.

## Ideas for more cases

- Paraphrases that a vector search could still get wrong (near-duplicate facts, negations).
- Facts that changed over time ("moved from Austin to Denver"): does `forget` + `remember` work?
- Injection in other places: search result snippets, redirects, very long pages where the
  instruction sits past the part the judge sees.
