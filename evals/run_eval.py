"""Run an eval, print a summary, and save the full result to evals/results/.

  python evals/run_eval.py memory --mode retrieval      free, deterministic: the memory store alone
  python evals/run_eval.py memory --mode retrieval --enrich   same, after the librarian adds
                                                        keywords/attributes to the seeds (Claude)
  python evals/run_eval.py memory --mode agent          the full agent (calls the model)
  python evals/run_eval.py write                        the librarian: what gets stored (Claude)
  python evals/run_eval.py associate --mode retrieval   associative recall gate (free; --enrich)
  python evals/run_eval.py associate --mode agent       does the agent bring up the right memory
  python evals/run_eval.py judge                        the source judge (Claude)

--model overrides ASSISTANT_MODEL for agent mode, e.g. --model gemma4:e4b to compare a local
model against Claude on the same questions. --librarian-model picks the librarian's model for
`write` and `--enrich`. Every run uses a throwaway database, and the librarian is off during
agent-mode runs so the seeded memories stay as they are. With tracing on in .env, every LLM call
(and its cost) goes to the Phoenix project "personal-assistant-evals"; --no-trace disables it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DATASETS = HERE / "datasets"
RESULTS = HERE / "results"


class Usage:
    """Collects API usage from the librarian/judge so the run's cost can be reported."""

    def __init__(self):
        self.messages = []

    def __call__(self, message, ms):
        self.messages.append(message)

    @property
    def cost(self) -> float:
        from assistant.agent import cost_of
        return sum(cost_of(m.model, m.usage) or 0 for m in self.messages)


def case(name: str, **metadata):
    """Trace span for one eval case (the run is one Phoenix session)."""
    from assistant.tracing import case_span
    return case_span(name, **metadata)


def librarian(store, usage: Usage, model: str | None):
    import anthropic

    from assistant.memory.librarian import LIBRARIAN_MODEL, Librarian
    return Librarian(anthropic.Anthropic(), store, on_usage=usage, model=model or LIBRARIAN_MODEL)


def seed_store(path: str, memories: dict) -> dict[int, str]:
    """Write the seed memories; return memory id -> key.

    A seed is a plain string (stored like a row migrated from the first schema) or a dict of
    store.add() fields, plus "supersedes": <key> and "surfaced_days_ago": <days>.
    """
    from assistant.memory import MemoryStore

    store = MemoryStore(path)
    ids: dict[str, int] = {}
    for key, spec in memories.items():
        spec = {"content": spec} if isinstance(spec, str) else dict(spec)
        sup = spec.pop("supersedes", None)
        surfaced = spec.pop("surfaced_days_ago", None)
        spec.pop("key", None)
        m = store.add(spec.pop("content"), spec.pop("kind", "fact"),
                      supersedes=ids[sup] if sup else None, **spec)
        ids[key] = m.id
        if surfaced is not None:
            store.db.execute("UPDATE memories SET last_surfaced_at = ? WHERE id = ?",
                             (time.time() - surfaced * 86400, m.id))
    store.db.commit()
    return {mid: key for key, mid in ids.items()}


def enrich_seeds(path: str, memories: dict, usage: Usage, model: str | None) -> None:
    """Plain seeds get full structure; structured seeds only get keywords, like real rows."""
    from assistant.memory import MemoryStore

    store = MemoryStore(path)
    lib = librarian(store, usage, model)
    rows = store.all(include_superseded=True)
    plain = {i for i, (k, v) in enumerate(memories.items()) if isinstance(v, str)}
    with case("enrich seeds", seeds=len(rows)):
        lib.enrich([m for i, m in enumerate(rows) if i in plain], full=True)
        lib.enrich([m for i, m in enumerate(rows) if i not in plain], full=False)
    print(f"  enriched {len(rows)} seeds, ${usage.cost:.4f}", file=sys.stderr)


def profile_keys(store, keys: dict[int, str]) -> set[str]:
    return {keys[int(i)] for i in re.findall(r"\(m(\d+),", store.profile()) if int(i) in keys}


# --- memory recall -------------------------------------------------------------------------

def memory_retrieval(data: dict, k: int, enrich: bool, usage: Usage, model: str | None):
    from assistant.memory import MemoryStore

    path = os.path.join(tempfile.mkdtemp(), "eval.db")
    keys = seed_store(path, data["memories"])
    if enrich:
        enrich_seeds(path, data["memories"], usage, model)
    store = MemoryStore(path)
    in_profile = profile_keys(store, keys)
    rows = []
    for q in data["questions"]:
        ranked = [keys[m.id] for m in store.search(q["q"], limit=k, touch=False)]
        rank = ranked.index(q["expects"]) + 1 if q["expects"] in ranked else None
        found = rank is not None or q["expects"] in in_profile
        if q.get("rank1"):
            found = rank == 1
        stale = q.get("stale") in ranked or q.get("stale") in in_profile
        search = (rank == 1 if q.get("rank1") else rank is not None) and q.get("stale") not in ranked
        rows.append({**q, "ranked": ranked, "rank": rank, "in_profile": q["expects"] in in_profile,
                     "search": search, "hit": found and not stale})
    return rows


def memory_agent(data: dict, enrich: bool, usage: Usage, model: str | None):
    from assistant.agent import Agent

    path = os.path.join(tempfile.mkdtemp(), "eval.db")
    keys = seed_store(path, data["memories"])
    if enrich:
        enrich_seeds(path, data["memories"], usage, model)
    agent = Agent(db_path=path, librarian=False)
    in_profile = profile_keys(agent.store, keys)
    rows = []
    for i, q in enumerate(data["questions"], 1):
        agent.reset()  # each question starts a fresh conversation
        recalled, answer, cost, error = set(), "", 0.0, None
        with case(f"memory: {q['q']}", type=q["type"], expects=q["expects"]):
            events = list(agent.stream_turn(q["q"]))
        for ev in events:
            if ev["type"] == "tool_end" and ev["name"] == "recall":
                recalled |= {key for mid, key in keys.items() if f"(m{mid}," in ev["output"]}
            elif ev["type"] == "text":
                answer += ev["delta"]
            elif ev["type"] == "error":
                error = ev["message"]
            elif ev["type"] == "done":
                cost = ev["cost"]
        answered = any(a in answer.lower() for a in q["answer_any"])
        rows.append({**q, "hit": q["expects"] in recalled | in_profile, "answered": answered,
                     "recalled": sorted(recalled), "answer": answer.strip(), "cost": cost,
                     "error": error})
        print(f"  {i:2}/{len(data['questions'])} {'✓' if answered else '✗'} {q['q']}",
              file=sys.stderr)
    return rows


# --- librarian (write) ---------------------------------------------------------------------

CORE = ("kind_any", "content_any", "entity_any", "link_any")


def _check(exp: dict, it: dict, today: str, id2key: dict) -> dict[str, bool]:
    """Every check for one expected item against one produced item."""
    content = (it.get("content") or "").lower()
    c = {}
    if exp.get("type", "memory") != it.get("type"):
        return {"type": False}
    if "kind_any" in exp:
        c["kind_any"] = it.get("kind") in exp["kind_any"]
    if "content_any" in exp:
        c["content_any"] = any(w in content for w in exp["content_any"])
    if "entity_any" in exp:
        c["entity_any"] = any(w in (it.get("entity_name") or "").lower() for w in exp["entity_any"])
    if "link_any" in exp:
        c["link_any"] = any(w in (it.get("link_type") or "").lower() for w in exp["link_any"])
    if "attribute_any" in exp:
        attr = re.sub(r"\W+", "_", (it.get("attribute") or "").lower()).strip("_")
        c["attribute"] = attr in exp["attribute_any"]
    if "value" in exp:
        try:
            c["value"] = float(it.get("value")) == float(exp["value"])
        except (TypeError, ValueError):
            c["value"] = (it.get("value") or "") == exp["value"]
    if exp.get("occurred_today"):
        c["occurred_at"] = (it.get("occurred_at") or "").startswith(today)
    if "supersedes" in exp:
        c["supersedes"] = id2key.get(it.get("supersedes")) == exp["supersedes"]
    if "theme_any" in exp:
        c["theme"] = bool({t.lower() for t in it.get("theme") or []} & set(exp["theme_any"]))
    if "provenance" in exp:
        c["provenance"] = it.get("provenance") == exp["provenance"]
    if "mode" in exp:
        c["mode"] = it.get("mode") == exp["mode"]
    if "salience" in exp:
        lo, hi = exp["salience"]
        c["salience"] = lo <= float(it.get("salience") or 0) <= hi
    return c


def write_eval(data: dict, usage: Usage, model: str | None):
    from assistant.memory import MemoryStore

    today = time.strftime("%Y-%m-%d")
    rows = []
    for i, c in enumerate(data["cases"], 1):
        path = os.path.join(tempfile.mkdtemp(), "eval.db")
        existing = {e["key"]: e for e in c.get("existing", [])}
        id2key = seed_store(path, existing)
        store = MemoryStore(path)
        before = usage.cost
        with case(f"write: {c['id']}", category=c["category"]):
            items = librarian(store, usage, model).decide(
                c["user"], c.get("assistant", ""), c.get("previous_user"))
        unmatched, matched, field_fails = list(items), [], []
        for exp in c["expect"]:
            best = None
            for it in unmatched:
                checks = _check(exp, it, today, id2key)
                if checks.get("type", True) and all(checks.get(f, True) for f in CORE):
                    best = (it, checks)
                    break
            if best:
                unmatched.remove(best[0])
                matched.append(best[0])
                field_fails += [f for f, ok in best[1].items() if not ok]
        all_matched = len(matched) == len(c["expect"])
        rows.append({
            "id": c["id"], "category": c["category"],
            "decision": bool(items) == bool(c["expect"]),
            "items": all_matched,
            "fields": all_matched and not field_fails,
            "clean": all_matched and not unmatched,
            "field_fails": field_fails, "extras": len(unmatched),
            "produced": items, "cost": round(usage.cost - before, 5)})
        ok = rows[-1]["items"] and rows[-1]["decision"]
        print(f"  {i:2}/{len(data['cases'])} {'✓' if ok else '✗'} {c['id']}"
              + (f"  (fields: {', '.join(field_fails)})" if field_fails else "")
              + (f"  (+{len(unmatched)} extra)" if unmatched else ""), file=sys.stderr)
    return rows


# --- associative recall --------------------------------------------------------------------

def _probe_group(p: dict) -> str:
    return "fire" if p["expects"] else f"quiet_{p.get('gate', 'filter')}"


def associate_eval(data: dict, mode: str, enrich: bool, usage: Usage, model: str | None):
    from assistant.memory import MemoryStore

    base = os.path.join(tempfile.mkdtemp(), "base.db")
    keys = seed_store(base, data["memories"])
    if enrich:
        enrich_seeds(base, data["memories"], usage, model)
    rows = []
    for i, p in enumerate(data["probes"], 1):
        path = os.path.join(tempfile.mkdtemp(), "probe.db")
        shutil.copy(base, path)  # fresh copy: surfacing marks a memory, which mustn't leak
        row = {**p, "group": _probe_group(p)}
        if mode == "retrieval":
            got = [keys[m.id] for m in MemoryStore(path).associate(p["q"], mark=False)]
            row["candidates"] = got
            row["pass"] = (any(k in got for k in p["expects"]) if p["expects"] else not got)
        else:
            from assistant.agent import Agent
            agent = Agent(db_path=path, librarian=False)
            answer, cost = "", 0.0
            with case(f"associate: {p['q']}", group=row["group"]):
                events = list(agent.stream_turn(p["q"]))
            for ev in events:
                if ev["type"] == "text":
                    answer += ev["delta"]
                elif ev["type"] == "done":
                    cost = ev["cost"]
            attached = agent.messages[0]["content"][1:] if agent.messages else []
            row["candidates"] = [keys[int(m)] for b in attached
                                 for m in re.findall(r"\(m(\d+),", b["text"]) if int(m) in keys]
            low = answer.lower()
            if p["expects"]:
                row["pass"] = any(w in low for w in p["mention_any"])
            else:
                row["pass"] = not any(w in low for w in p["must_not_mention"])
            row.update(answer=answer.strip(), cost=cost)
        rows.append(row)
        print(f"  {i:2}/{len(data['probes'])} {'✓' if row['pass'] else '✗'} [{row['group']}] "
              f"{p['q']}  -> {row['candidates']}", file=sys.stderr)
    return rows


# --- source judge --------------------------------------------------------------------------

def judge(data: dict, usage: Usage) -> list[dict]:
    import anthropic

    from assistant.judge import SourceJudge

    check = SourceJudge(anthropic.Anthropic(), on_usage=usage)
    rows = []
    for c in data["cases"]:
        with case(f"judge: {c['id']}", kind=c["kind"], expect=c["expect"]):
            verdict = check(c["task"], c["url_template"], c["example_url"], c["notes"],
                            c["content"])
        got = "approve" if verdict.approved else "reject"
        rows.append({"id": c["id"], "kind": c["kind"], "expect": c["expect"],
                     "got": got, "correct": got == c["expect"], "reason": verdict.reason})
        print(f"  {'✓' if got == c['expect'] else '✗'} {c['id']}: {got}", file=sys.stderr)
    return rows


def summarize(rows: list[dict], group: str, fields: list[str]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[r[group]].append(r)
        groups["all"].append(r)
    return {g: {f: f"{sum(r[f] for r in rs)}/{len(rs)}" for f in fields}
            for g, rs in sorted(groups.items(), key=lambda kv: kv[0] == "all")}


def print_table(summary: dict, fields: list[str]) -> None:
    print(f"\n{'':14}" + "".join(f"{f:>12}" for f in fields))
    for group, scores in summary.items():
        print(f"{group:14}" + "".join(f"{scores[f]:>12}" for f in fields))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval", choices=["memory", "write", "associate", "judge"])
    parser.add_argument("--mode", choices=["retrieval", "agent"], default="retrieval",
                        help="memory and associate evals")
    parser.add_argument("--k", type=int, default=5, help="top-k for retrieval mode")
    parser.add_argument("--enrich", action="store_true",
                        help="librarian adds keywords/attributes to the seeds first (calls Claude)")
    parser.add_argument("--model", help="override ASSISTANT_MODEL (agent mode)")
    parser.add_argument("--librarian-model", help="model for the librarian (write, --enrich)")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--no-trace", action="store_true", help="don't send traces to Phoenix")
    parser.add_argument("--limit", type=int, help="only the first N cases (quick checks)")
    args = parser.parse_args()

    # Keep evals isolated: no web search quota, optional model override. Traces (and so costs)
    # go to their own Phoenix project when tracing is on in .env; --no-trace turns them off.
    if args.no_trace:
        os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = ""
    os.environ["PHOENIX_PROJECT_NAME"] = "personal-assistant-evals"
    os.environ["BRAVE_API_KEY"] = ""
    if args.model:
        os.environ["ASSISTANT_MODEL"] = args.model
    from dotenv import load_dotenv
    load_dotenv(HERE.parent / ".env")  # API keys; never overrides the settings above
    import assistant.tracing as tracing
    tracing.setup_tracing()  # before any client or database is created, so every call is traced
    # one Phoenix session per run, named like the results file
    tracing.SESSION_ID = f"{args.eval}{'-' + args.mode if args.eval in ('memory', 'associate') else ''}" \
        f"-{time.strftime('%Y-%m-%dT%H%M%S')}"

    from assistant.memory.librarian import LIBRARIAN_MODEL
    lib_model = args.librarian_model or LIBRARIAN_MODEL
    usage = Usage()
    started = time.time()
    agent_cost = 0.0
    if args.eval == "memory":
        data = json.loads((DATASETS / "memory_recall.json").read_text())
        data["questions"] = data["questions"][:args.limit]
        suffix = "_enriched" if args.enrich else ""
        if args.mode == "retrieval":
            rows = memory_retrieval(data, args.k, args.enrich, usage, lib_model)
            fields = ["search", "hit"]  # search: top-k alone; hit: top-k or the profile
            name, model = f"memory_retrieval_k{args.k}{suffix}", "sqlite-fts5"
        else:
            from assistant.agent import MODEL
            rows = memory_agent(data, args.enrich, usage, lib_model)
            fields, name, model = ["hit", "answered"], f"memory_agent{suffix}", MODEL
            agent_cost = sum(r["cost"] for r in rows)
        summary = summarize(rows, "type", fields)
    elif args.eval == "write":
        data = json.loads((DATASETS / "memory_write.json").read_text())
        data["cases"] = data["cases"][:args.limit]
        rows = write_eval(data, usage, lib_model)
        fields, name, model = ["decision", "items", "fields", "clean"], "memory_write", lib_model
        summary = summarize(rows, "category", fields)
    elif args.eval == "associate":
        data = json.loads((DATASETS / "memory_associate.json").read_text())
        data["probes"] = data["probes"][:args.limit]
        rows = associate_eval(data, args.mode, args.enrich, usage, lib_model)
        suffix = "_enriched" if args.enrich else ""
        if args.mode == "retrieval":
            name, model = f"memory_associate_retrieval{suffix}", "sqlite-fts5"
        else:
            from assistant.agent import MODEL
            name, model = f"memory_associate_agent{suffix}", MODEL
            agent_cost = sum(r["cost"] for r in rows)
        fields = ["pass"]
        summary = summarize(rows, "group", fields)
    else:
        data = json.loads((DATASETS / "source_judge.json").read_text())
        data["cases"] = data["cases"][:args.limit]
        from assistant.judge import JUDGE_MODEL
        rows = judge(data, usage)
        fields, name, model = ["correct"], "source_judge", JUDGE_MODEL
        summary = summarize(rows, "kind", fields)

    cost = usage.cost + agent_cost
    print_table(summary, fields)
    uses_librarian = args.eval == "write" or args.enrich
    print(f"\nmodel {model}" + (f", librarian {lib_model}" if uses_librarian else "")
          + f", cost ${cost:.4f}, {time.time() - started:.0f} s")

    if not args.no_save and not args.limit:  # partial runs aren't results
        RESULTS.mkdir(exist_ok=True)
        slug = model.replace(":", "-").replace("/", "-")
        if args.enrich or args.eval == "write":
            slug += "" if args.eval == "write" else f"_lib-{lib_model}"
        out = RESULTS / f"{time.strftime('%Y-%m-%d')}_{name}_{slug}.json"
        out.write_text(json.dumps({"eval": name, "model": model, "librarian_model": lib_model
                                   if uses_librarian else None, "date": time.strftime("%F"),
                                   "cost": round(cost, 4), "summary": summary, "rows": rows},
                                  indent=2, ensure_ascii=False, default=str))
        print(f"saved {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
