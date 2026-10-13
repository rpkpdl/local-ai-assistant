"""Run an eval, print a summary, and save the full result to evals/results/.

  python evals/run_eval.py memory --mode retrieval    free, deterministic: the memory store alone
  python evals/run_eval.py memory --mode agent        the full agent (calls the model, costs money)
  python evals/run_eval.py judge                      the source judge (calls Claude, costs money)

--model overrides ASSISTANT_MODEL for agent mode, e.g. --model gemma4:e4b to compare a local
model against Claude on the same questions. Every run uses a throwaway database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DATASETS = HERE / "datasets"
RESULTS = HERE / "results"


def seed_store(path: str, memories: dict[str, str]) -> dict[str, str]:
    """Write the seed memories; return memory id -> key."""
    from assistant.memory import SQLiteMemoryStore

    store = SQLiteMemoryStore(path)
    return {store.add(content).id: key for key, content in memories.items()}


def memory_retrieval(data: dict, k: int) -> list[dict]:
    from assistant.memory import SQLiteMemoryStore

    path = os.path.join(tempfile.mkdtemp(), "eval.db")
    keys = seed_store(path, data["memories"])
    store = SQLiteMemoryStore(path)
    rows = []
    for q in data["questions"]:
        ranked = [keys[m.id] for m in store.search(q["q"], limit=k)]
        rank = ranked.index(q["expects"]) + 1 if q["expects"] in ranked else None
        rows.append({**q, "ranked": ranked, "rank": rank, "hit": rank is not None})
    return rows


def memory_agent(data: dict) -> list[dict]:
    from assistant.agent import Agent

    path = os.path.join(tempfile.mkdtemp(), "eval.db")
    keys = seed_store(path, data["memories"])
    agent = Agent(db_path=path)
    rows = []
    for i, q in enumerate(data["questions"], 1):
        agent.reset()  # each question starts a fresh conversation
        recalled, answer, cost, error = set(), "", 0.0, None
        for ev in agent.stream_turn(q["q"]):
            if ev["type"] == "tool_end" and ev["name"] == "recall":
                recalled |= {key for mid, key in keys.items() if f"({mid}," in ev["output"]}
            elif ev["type"] == "text":
                answer += ev["delta"]
            elif ev["type"] == "error":
                error = ev["message"]
            elif ev["type"] == "done":
                cost = ev["cost"]
        answered = any(a in answer.lower() for a in q["answer_any"])
        rows.append({**q, "hit": q["expects"] in recalled, "answered": answered,
                     "recalled": sorted(recalled), "answer": answer.strip(), "cost": cost,
                     "error": error})
        print(f"  {i:2}/{len(data['questions'])} {'✓' if answered else '✗'} {q['q']}",
              file=sys.stderr)
    return rows


def judge(data: dict) -> tuple[list[dict], float]:
    import anthropic

    from assistant.agent import cost_of
    from assistant.judge import SourceJudge

    usage = []
    check = SourceJudge(anthropic.Anthropic(), on_usage=lambda msg, ms: usage.append(msg))
    rows = []
    for case in data["cases"]:
        verdict = check(case["task"], case["url_template"], case["example_url"], case["notes"],
                        case["content"])
        got = "approve" if verdict.approved else "reject"
        rows.append({"id": case["id"], "kind": case["kind"], "expect": case["expect"],
                     "got": got, "correct": got == case["expect"], "reason": verdict.reason})
        print(f"  {'✓' if got == case['expect'] else '✗'} {case['id']}: {got}", file=sys.stderr)
    cost = sum(cost_of(m.model, m.usage) or 0 for m in usage)
    return rows, cost


def summarize(rows: list[dict], group: str, fields: list[str]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[r[group]].append(r)
        groups["all"].append(r)
    return {g: {f: f"{sum(r[f] for r in rs)}/{len(rs)}" for f in fields}
            for g, rs in sorted(groups.items(), key=lambda kv: kv[0] == "all")}


def print_table(summary: dict, fields: list[str]) -> None:
    print(f"\n{'':12}" + "".join(f"{f:>12}" for f in fields))
    for group, scores in summary.items():
        print(f"{group:12}" + "".join(f"{scores[f]:>12}" for f in fields))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval", choices=["memory", "judge"])
    parser.add_argument("--mode", choices=["retrieval", "agent"], default="retrieval",
                        help="memory eval only")
    parser.add_argument("--k", type=int, default=5, help="top-k for retrieval mode")
    parser.add_argument("--model", help="override ASSISTANT_MODEL (agent mode)")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    # Keep evals isolated: no tracing, no web search quota, optional model override.
    os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = ""
    os.environ["BRAVE_API_KEY"] = ""
    if args.model:
        os.environ["ASSISTANT_MODEL"] = args.model
    from dotenv import load_dotenv
    load_dotenv(HERE.parent / ".env")  # API keys; never overrides the settings above

    started = time.time()
    if args.eval == "memory":
        data = json.loads((DATASETS / "memory_recall.json").read_text())
        if args.mode == "retrieval":
            rows, cost, fields = memory_retrieval(data, args.k), 0.0, ["hit"]
            name, model = f"memory_retrieval_k{args.k}", "sqlite-fts5"
        else:
            from assistant.agent import MODEL
            rows, fields = memory_agent(data), ["hit", "answered"]
            cost, name, model = sum(r["cost"] for r in rows), "memory_agent", MODEL
        summary = summarize(rows, "type", fields)
    else:
        data = json.loads((DATASETS / "source_judge.json").read_text())
        from assistant.judge import JUDGE_MODEL
        rows, cost = judge(data)
        fields, name, model = ["correct"], "source_judge", JUDGE_MODEL
        summary = summarize(rows, "kind", fields)

    print_table(summary, fields)
    print(f"\nmodel {model}, cost ${cost:.4f}, {time.time() - started:.0f} s")

    if not args.no_save:
        RESULTS.mkdir(exist_ok=True)
        slug = model.replace(":", "-").replace("/", "-")
        out = RESULTS / f"{time.strftime('%Y-%m-%d')}_{name}_{slug}.json"
        out.write_text(json.dumps({"eval": name, "model": model, "date": time.strftime("%F"),
                                   "cost": round(cost, 4), "summary": summary, "rows": rows},
                                  indent=2, ensure_ascii=False))
        print(f"saved {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
