# Memory

How the assistant remembers things about the user. The design follows
[memory_planning.md](memory_planning.md): storage stays cheap and simple, and the thinking
happens once, when a memory is written. Table details are in [database.md](database.md#memory-tables).

## The pieces

```
 user turn ──▶ associate(): matching lived experiences ─┐
                                                        ▼
            system prompt + <user_profile> ──▶ agent (recall / forget tools) ──▶ reply
                                                                                  │
                     librarian: what to store, supersede, keywords ◀──────────────┘
                                   │
                                   ▼
               entities · memories · relationships · memories_fts   (memories.db)
                                   ▲
                 consolidate(): reinforce / decay salience, daily
```

| Piece | Where | LLM? | What it does |
|---|---|---|---|
| Store | `assistant/memory/store.py` | no | Rows, FTS5 sync triggers, search, profile, associative gate, migration from the first schema |
| Librarian | `assistant/memory/librarian.py` | 1 call per turn | Decides what to store from the turn (usually nothing), with kind, attribute, theme, keywords, salience, provenance, mode, and which memory it supersedes. Plain code validates the JSON and writes the rows |
| Profile | `MemoryStore.profile()` | no | User facts that have an attribute (home city, diet...), relationships, and the latest point of each tracked series. Always in the system prompt, in its own block after the fixed prompt |
| Direct recall | `recall` tool → `MemoryStore.search()` | no | FTS5 over content, theme, keywords, attribute and entity name. Ranked by bm25 × salience × recency. Superseded rows never show; a matching series comes back newest first |
| Associative recall | `MemoryStore.associate()` | no (the agent decides) | Matches the user's message against `theme` and `keywords` only. Gate: lesson or event (or a fact without an attribute), salience ≥ 0.75, stated or observed, current, not offered in the last 2 days. Matches are attached to the message as `<possibly_relevant_memories>`; the system prompt tells the agent to bring one up only if it clearly helps right now |
| Consolidator | `assistant/memory/consolidate.py` | no | Runs at startup if the last run was over a day ago. Memories recalled since the last run gain 0.03; memories idle for 30 days lose about 0.06 a month, down to a floor (0.7 for stated lessons, events and corrections, 0.4 for stated facts) |
| Enrich | `Librarian.enrich()` | 1 call per 25 memories | Adds the same structure to plain memories: rows migrated from the first schema, or eval seeds |

## Choices that differ from the spec, and why

- **Keywords column.** The spec has themes only. The librarian also writes 8-20 keywords per
  memory: names from the content, plus words the user might say later ("flying, airport,
  layover" for a delayed-flight lesson). This is query expansion done once at write time
  instead of embeddings at query time. It took paraphrase recall from 1/12 to 12/12 in the eval
  (see [findings](findings.md)).
- **Associative recall doesn't call an LLM.** The spec's gate ends with "the LLM judges it relevant
  now". Here the agent itself is that judge, because the candidates ride along in its context.
  This adds no extra call and no latency before the reply.
- **`last_surfaced_at` separate from `last_seen_at`.** A direct `recall` shouldn't stop a memory
  from being offered later, so the rate limit has its own column.
- **Timestamps.** `recorded_at`, `last_seen_at` and the others are epoch seconds like the rest of
  the database. `occurred_at` is an ISO date, because it's often only a day.
- **The agent no longer has a `remember` tool.** The librarian stores memories after every
  turn, and two writers would duplicate them. `recall` and `forget` remain.
- **`forget` deletes the memory and the versions it superseded**, so an old fact doesn't come back
  when the user wants something gone.
- **The JSON store was removed.** The new model (entities, supersede chains, FTS sync) is
  SQLite-specific, and the spec asks for one SQLite file.

## Cost and latency

The librarian runs after the reply has streamed, before the turn's `done` event, so the answer
isn't delayed but the input box waits for it. The web UI shows "Updating memory…".
It defaults to `claude-opus-5-5` at effort `low`. In the write eval that cost about $0.014 per
turn and scored 34/34. `claude-haiku-5-5` scored 34/34 on decisions and items (one missed series
value, now filled in by code) for about $0.0004 per turn. Set `LIBRARIAN_MODEL=claude-haiku-5-5` in
`.env` to switch, or `ASSISTANT_LIBRARIAN=off` to disable it. Since 2026-10-17 this machine runs the
agent, librarian and source judge all on Haiku (about $0.0005 for a short turn), after Haiku
matched or beat Opus on every eval (see [findings](findings.md)). The librarian always runs on Claude,
even when the agent runs on Ollama.

## Migrating an existing database

Opening an old `memories.db` renames the original FTS5 table to `memories_v1` and copies its rows
into `memories` as plain user facts. Nothing is deleted. Plain facts have no attribute or keywords,
so they don't reach the profile and only match exact words until enriched:

```bash
.venv/bin/python -m assistant.memory.consolidate --enrich   # one Claude call per 25 memories
```

## Not built yet

- Consolidator merge (near-duplicates → one row) and pattern promotion (many events → one inferred
  fact). Both need an LLM pass.
- Relationship corrections (`relationships.superseded_by` exists but nothing sets it).
- Mode is stored but only used through the tone guidance in the system prompt.
- Running the librarian in the background after `done`, so the next message isn't held up.
