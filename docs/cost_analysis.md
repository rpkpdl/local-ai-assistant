# Cost analysis

What the assistant's LLM calls cost, measured on the eval sets, and why Claude Haiku 5.5 is the
recommended model instead of Opus 5.5. Every number below comes from a saved results file in
[`evals/results/`](../evals/results/). The decision is recorded in [findings.md](findings.md)
("Haiku 5.5 matches Opus 5.5 on every eval").

## Summary

Running the same four eval sets with Haiku 5.5 as the agent, librarian and source judge cost
**$0.076**, against **$1.74** on Opus 5.5: **23× cheaper**. No score dropped by more than one
case, which is within noise for sets this small. A typical turn falls from about $0.03–0.04 to
about $0.001–0.002.

## Where the money goes

Each turn makes up to three kinds of LLM call:

| Role | When | Model setting |
|---|---|---|
| Agent | Every turn, once per tool-use step | `ASSISTANT_MODEL` |
| Librarian | Once per turn, after the reply, to decide what to store | `LIBRARIAN_MODEL` |
| Source judge | Only when the agent saves a new web source | `JUDGE_MODEL` |

All three default to `claude-opus-5-5` in code. The measurements below compare that default with
all three set to `claude-haiku-5-5` in `.env`, which is the recommended setup.

## Prices

List prices per million tokens, as used by the app (`PRICES` in `assistant/agent.py`) and by
Phoenix's built-in price table:

| Model | Input | Output | Cache read | Cache write (5 min) |
|---|---|---|---|---|
| claude-opus-5-5 | $4.00 | $20.00 | $0.20 | $5.00 |
| claude-haiku-5-5 | $0.10 | $0.50 | $0.01 | $0.125 |

Haiku is 40× cheaper per token. The measured runs come out 13–39× cheaper rather than 40×,
because Haiku sometimes takes more steps or writes longer answers.

## Eval runs

Each row is one complete run of an eval set. "Score" is the set's headline metric.

| Eval set | Cases | Opus 5.5: score · cost | Haiku 5.5: score · cost | Ratio |
|---|---|---|---|---|
| Memory recall, agent mode | 30 | 27/27 answered · $0.578 | 30/30 answered · $0.044 | 13× |
| Associative recall, agent mode | 21 | 18/21 · $0.645 | 19/21 · $0.019 | 35× |
| Librarian (write) | 34 | 34/34 items · $0.472 | 34/34 items · $0.012 | 39× |
| Source judge, incl. 5 injection attempts | 11 | 11/11 · $0.046 | 11/11 · $0.0013 | 36× |
| **Total** | | **$1.741** | **$0.076** | **23×** |

- Opus runs: 2026-10-13 (judge) and 2026-10-14 (the rest). Haiku runs: 2026-10-17, except
  the Haiku librarian run (2026-10-14).
- The Opus recall run lost 3 questions to an API credit error. They're left out of its score
  (see the findings entry "Eval rows that errored were scored as misses"), but its cost covers
  only the 27 that ran.
- Recall and associative-recall costs include one librarian `enrich` call over the seed
  memories: $0.061 and $0.033 on Opus, $0.0019 and $0.0009 on Haiku.

## Cost per call

The same runs, divided by the number of cases (enrich calls excluded):

| Unit | Opus 5.5 | Haiku 5.5 | Spread on Haiku (min – max) |
|---|---|---|---|
| Agent turn answering a memory question | $0.0191 | $0.0014 | $0.0003 – $0.018 |
| Agent turn with a past experience attached | $0.0291 | $0.0008 | $0.0003 – $0.0077 |
| Librarian call | $0.0139 | $0.00035 | |
| Source judge check | $0.0042 | $0.00012 | |

A typical turn is one agent turn plus one librarian call: about **$0.033–0.043 on Opus** and
**$0.0012–0.0018 on Haiku**. A turn that fetches the web and saves a source adds tool steps and a
judge check. A short live turn on Haiku ("what is 2+2?") measured **$0.00054**: $0.00027 for
the agent and $0.00027 for the librarian.

At 50 turns a day, that's roughly $50–65 a month on Opus, against about $2–3 on Haiku.

## How cost is tracked

- **Per turn, in the UI:** the app computes each call's cost from `PRICES` and shows the agent,
  judge and memory spend for each turn and for the whole chat.
- **In Phoenix** (http://127.0.0.1:6006), priced from Phoenix's own price table:
  - `personal-assistant`: app traffic, one trace per turn, with its agent, librarian and judge
    calls inside.
  - `personal-assistant-evals`: eval runs, one session per run and one trace per case. Runs
    before 2026-10-15 weren't traced, so their cost is only in the results files.
- **In results files:** every eval run saves its total `cost`, and agent-mode runs also save
  `cost` per row.

If the app's numbers and Phoenix's disagree, a price is out of date in one of them. The authoritative bill is the Anthropic Console's usage
page.

## Reproducing

```bash
.venv/bin/python evals/run_eval.py memory --mode agent --enrich --model claude-haiku-5-5 --librarian-model claude-haiku-5-5
.venv/bin/python evals/run_eval.py associate --mode agent --enrich --model claude-haiku-5-5 --librarian-model claude-haiku-5-5
.venv/bin/python evals/run_eval.py write --librarian-model claude-haiku-5-5
JUDGE_MODEL=claude-haiku-5-5 .venv/bin/python evals/run_eval.py judge
```

Swap in `claude-opus-5-5` for the comparison. The four Opus runs cost about $1.75.

## Limits of this analysis

- The eval sets are small (11–34 cases). The result is "no measurable loss on Haiku", not
  "Haiku is better".
- They cover short, well-defined tasks. Long multi-step web lookups aren't measured; watch those
  in Phoenix.
- Prompt caching isn't used yet. It would cut both models' input cost, since every call
  resends the system prompt and the tool definitions.
- Costs are list prices for the Claude API.
