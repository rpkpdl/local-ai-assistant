# Memory Groundwork Spec — Lightweight Self-Improving Personal Assistant

> The original design spec, written before the build. What was built, and where it differs from this
> spec, is in [memory.md](memory.md). Measured results are in [evals/README.md](../evals/README.md).

## Context

A personal AI assistant, currently running on a mini PC with no GPU, with a target of eventually running on a phone or another lightweight device. It already has:

- **Recall tool**: looks up stored facts, such as the user's location, before asking again.
- **Source registry**: a search API, then an HTTP GET tool, then an LLM-as-judge that validates the source, then a cache of the source keyed by query type (weather, stock price, ...). Later queries hit the cached source directly. The user can say "I don't like this" to trigger re-validation.
- **SQLite** as the storage engine.

This spec adds the **memory groundwork**: a small, clean mechanism that follows the same pattern as the source registry.

> **Design principle:** storage stays simple and cheap, and the intelligence lives in the LLM at write time.
> The LLM acts as a *librarian*. It decides what gets stored, how important it is, and how it relates to what's already stored. Once it has decided, the memory is just a row. The LLM doesn't reason over stored rows again on every query.

## Goals

1. Run with very little overhead on CPU-only and mobile devices: one SQLite file, no vector DB, no graph DB.
2. Support **facts**, **time-series tracking**, and **relationships** with one shared set of primitives.
3. Enable **self-improvement**: the assistant knows more about the user, gets better at tasks, and learns from corrections over time.
4. Support **associative recall**: the current context brings up relevant past memories, gated so they only appear when they're useful.
5. Lay groundwork for **mode awareness** (task vs. personal) now, even if it isn't used yet.

## Non-goals (for now)

- Embeddings or vector search. Add them only if FTS5 keyword and theme search proves too weak.
- A graph database. Model relationships in SQLite tables instead.
- Task scheduling or reminders. Existing apps already handle these.

---

## 1. The Five Primitives

Every memory carries these five fields:

| Primitive | Meaning |
|---|---|
| **time** | When it happened, and when it was learned |
| **entity** | Who or what it's about (the user, a person, a place, a thing) |
| **content** | The actual fact, event, or value |
| **salience** | How much it matters (0.0 to 1.0). Drives consolidation and gating. |
| **provenance** | Where it came from: `stated` (user said it), `observed` (seen through tools or behavior), or `inferred` (LLM reasoning) |

## 2. Storage Layer — One SQLite File, Three Tables

```sql
-- Everything the assistant knows about
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,          -- 'user', 'person', 'place', 'thing', 'org', ...
    name        TEXT NOT NULL,
    aliases     TEXT,                   -- JSON array of alternate names
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(type, name)
);

-- Facts, events, and time-series points
CREATE TABLE memories (
    id           INTEGER PRIMARY KEY,
    entity_id    INTEGER NOT NULL REFERENCES entities(id),
    kind         TEXT NOT NULL,         -- 'fact' | 'event' | 'series_point' | 'lesson'
    attribute    TEXT,                  -- series/fact key, e.g. 'weight', 'home_city'
    content      TEXT NOT NULL,         -- natural-language content
    value        TEXT,                  -- optional structured value (number, JSON)
    theme        TEXT,                  -- comma-separated themes, e.g. 'travel,food'
    salience     REAL NOT NULL DEFAULT 0.5,
    provenance   TEXT NOT NULL,         -- 'stated' | 'observed' | 'inferred'
    mode         TEXT DEFAULT 'task',   -- 'task' | 'personal'
    occurred_at  TEXT,                  -- when it happened (may be null/unknown)
    recorded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,                  -- last time it was recalled or reinforced
    hit_count    INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER REFERENCES memories(id)  -- for corrections/updates
);

-- Links between entities
CREATE TABLE relationships (
    id          INTEGER PRIMARY KEY,
    entity_a    INTEGER NOT NULL REFERENCES entities(id),
    entity_b    INTEGER NOT NULL REFERENCES entities(id),
    link_type   TEXT NOT NULL,          -- 'sister_of', 'likes', 'works_at', ...
    salience    REAL NOT NULL DEFAULT 0.5,
    provenance  TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES relationships(id)
);

-- Full-text search for fuzzy recall
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, theme, attribute,
    content='memories', content_rowid='id'
);
-- (add triggers to keep memories_fts in sync on INSERT/UPDATE/DELETE)

CREATE INDEX idx_mem_series ON memories(entity_id, attribute, occurred_at);
CREATE INDEX idx_mem_theme  ON memories(theme);
CREATE INDEX idx_mem_sal    ON memories(salience);
```

**How each memory shape maps to the tables:**

- **Fact**: one `memories` row with `kind='fact'`. A newer fact supersedes the older one through `superseded_by`. Don't delete the old row, so the history is kept.
- **Time-series**: many `memories` rows with `kind='series_point'` that share the same `entity_id` and `attribute`, ordered by `occurred_at`.
- **Relationship**: one `relationships` row.
- **Lesson / lived experience**: `kind='lesson'` or `'event'`, `provenance='stated'`, high salience, and theme tags. This is what drives association.

## 3. Logic Layer — Three Thin Helpers

### 3.1 Writer (the Librarian)

Runs on each user turn. It can run after the reply is sent so it doesn't add latency.

1. The LLM reads the user's message plus a small slice of relevant existing memories.
2. The LLM returns structured JSON that decides:
   - **Is anything worth storing?** Most turns should store nothing.
   - **What kind is it?** fact / event / series_point / lesson / relationship
   - **Which entity?** Resolve it to an existing entity (check aliases) or create a new one.
   - **Theme tags.** These are the hooks that make future association possible.
   - **Salience.** See the rules below.
   - **Provenance.** stated / observed / inferred
   - **Mode.** task / personal
   - **Does it update or contradict an existing memory?** If so, set `superseded_by`.
3. Plain code validates the JSON and writes the rows. The LLM never runs SQL directly.

**Salience rules (starting heuristics):**
- `stated` lived experience, especially with emotion or frustration behind it: **0.8 to 1.0**
- `stated` neutral fact: **0.6 to 0.7**
- `observed`: **0.4 to 0.6**
- `inferred`: **0.2 to 0.4**. Inferred memories should never outrank lived experience.
- User corrections: **high**. The assistant must not repeat a corrected mistake.

Suggested writer output schema:
```json
{
  "store": true,
  "items": [
    {
      "type": "memory",
      "entity": {"type": "user", "name": "self"},
      "kind": "lesson",
      "attribute": null,
      "content": "Flight was delayed and user was starving at the airport with no snacks",
      "theme": ["travel", "flight", "food"],
      "salience": 0.9,
      "provenance": "stated",
      "mode": "personal",
      "occurred_at": null,
      "supersedes": null
    }
  ]
}
```

### 3.2 Retriever

There are two entry points that share the same underlying queries.

**a) Direct recall.** The user asks something.
- Structured lookup first: entity plus attribute, latest non-superseded row.
- FTS5 search over `memories_fts` for fuzzy questions.
- Rank by `bm25 score × salience × recency`.

**b) Associative recall.** The current context triggers it, not a question.
- Extract themes and entities from the current turn (for example, "planning a trip" gives `travel`, `flight`, `airport`).
- Query memories that match those themes, non-superseded, ordered by salience.
- **Gate:** only surface a memory if **all** of these hold:
  - salience ≥ threshold (start around 0.75)
  - provenance is `stated`, or `observed` with high confidence
  - the LLM judges it relevant and useful *right now*
  - it hasn't already been surfaced recently (rate limit using `last_seen_at`)
- Otherwise say nothing. A good assistant knows when not to interject.

Update `last_seen_at` and `hit_count` on every recall, for both direct and associative recall.

**Two tiers of association. Always prefer tier 2:**
1. *Generic LLM advice* ("flights get delayed, pack snacks"). Any assistant could say this, so it's low value.
2. *The user's lived experience* ("last time your flight was delayed you were starving, so want to pack snacks?"). This is high value because it's specific to the user.

### 3.3 Consolidator

A background job that runs daily or when the device is idle or charging.

- **Reinforce:** raise salience on memories with rising `hit_count` or repeated mentions.
- **Decay:** slowly lower salience on memories that haven't been recalled in a long time. Never decay `stated` lessons or corrections below a floor.
- **Merge:** use the LLM to collapse near-duplicate memories into one stronger row.
- **Promote patterns:** many series points or events can become one summarized fact (for example, "user orders takeout most Fridays"), stored with `provenance='inferred'`.
- **Never hard-delete** unless the user explicitly asks to forget something. When they do, delete the row and anything derived only from it.

## 4. Mode Awareness and Guardrails

- The `mode` column (`task` / `personal`) is cheap to add now and can be used later.
- Use mode for **tone**. Venting and asking for a fix call for different responses.
- **Light guardrails** (build these freely): if the user seems frustrated, slow down, simplify, offer a break or a different approach.
- **Real distress** (build the hand-off, not the counseling): be warm, don't try to act as a therapist, and point the user toward real people such as friends, professionals, or crisis lines.
- **Avoid designing it as a companion that encourages dependence.** It should add to the user's life and nudge them toward human connection rather than replace it.
- This part is mostly prompt and policy wording, not engineering.

## 5. User Correction Loop

This works the same way as the source registry's "I don't like this":
- The user corrects a memory. Write the corrected memory and set `superseded_by` on the old one.
- If the user explains *why* (wrong, stale, never said that), store the reason. It makes later re-validation smarter.
- "Forget X" deletes the row and anything derived only from it.

## 6. Suggested Module Layout (Python)

```
memory/
  schema.sql          # tables, FTS5, triggers, indexes
  store.py            # thin SQLite access layer (no LLM)
  writer.py           # librarian: LLM call -> validated JSON -> rows
  retriever.py        # direct + associative recall, ranking, gate
  consolidator.py     # reinforce / decay / merge / promote
  prompts/
    writer.txt
    association_gate.txt
  tests/
```

## 7. Build Phases

1. **Schema + store.py**: tables, FTS5 sync triggers, and CRUD functions.
2. **Writer**: LLM extraction to JSON, validation, and writes. Test with sample conversations.
3. **Retriever (direct)**: structured lookup plus FTS5 ranking.
4. **Retriever (associative)**: theme extraction and the salience gate. Test it with the airport-snacks scenario.
5. **Consolidator**: reinforce and decay first, then merge and promote.
6. **Correction loop and mode tagging.**
7. **Mobile check**: measure DB size, query latency, and LLM calls per turn on target hardware.

## 8. Acceptance Scenario

1. User: "Ugh, my flight got delayed and I was starving, no snacks."
   The writer stores a `lesson` with theme `travel,flight,food`, `provenance=stated`, and salience about 0.9.
2. Weeks later, user: "I'm flying to Portland next week."
   The associative retriever finds the lesson, it passes the gate, and the assistant says something like: "Last time your flight got delayed you were stuck hungry. Want a reminder to pack snacks?"
3. User: "Is it going to rain tomorrow?"
   The assistant answers using the stored location and the cached weather source. No association fires because nothing relevant passes the gate.

## 9. Open Questions

- What should the salience threshold for associative surfacing be? Tune it based on how annoying the suggestions feel in practice.
- Should the writer run on every turn, or be batched at the end of a session to save LLM calls on mobile?
- Should themes be free text or a controlled vocabulary? A small controlled list makes matching more reliable.
- When, if ever, does FTS5 stop being good enough? Define a metric before adding embeddings.
