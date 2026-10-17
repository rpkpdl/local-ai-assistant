# Database reference

The assistant stores everything locally in SQLite. There are two database files:

| File | Owner | Contents |
|---|---|---|
| `memories.db` | This app | Memories, cached sources, fetched pages, fetch audit log, search usage |
| `.phoenix/phoenix.db` | Arize Phoenix | Traces (only when tracing is enabled) |

Both files are git-ignored. All timestamps are Unix epoch seconds (`REAL`); convert them in SQL with
`datetime(ts, 'unixepoch', 'localtime')`.

## `memories.db`

Tables are created automatically on first run by the module that owns them.

| Table | Defined in | Purpose |
|---|---|---|
| [`entities`, `memories`, `relationships`](#memory-tables) | `assistant/memory/store.py` | Long-term memory about the user |
| [`sources`](#sources) | `assistant/web/sources.py` | Known-good URL templates for recurring tasks |
| [`pages`](#pages) | `assistant/web/fetch.py` | Local cache of fetched page/API content |
| [`fetch_log`](#fetch_log) | `assistant/web/fetch.py` | Audit log of every fetch attempt |
| [`search_usage`](#search_usage) | `assistant/web/search.py` | Brave searches per month, for the quota cap |

### Memory tables

Written by the librarian (`assistant/memory/librarian.py`) after each turn, and read by `recall`,
the always-in-context profile and associative recall (`assistant/memory/store.py`). See
[memory.md](memory.md) for how they're used.

#### `entities`

Everyone and everything memories are about. The user is the one row with `type = 'user'`.

| Column | Type | Description |
|---|---|---|
| `id` | integer | Entity id |
| `type` | text | `user`, `person`, `pet`, `place`, `org` or `thing` |
| `name` | text | Display name, e.g. `Maya`. Unique per type; matched case-insensitively |
| `aliases` | text | JSON array of other names that resolve to this entity |
| `created_at` | real | When it was first seen |

#### `memories`

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL REFERENCES entities(id),
    kind TEXT NOT NULL, attribute TEXT, content TEXT NOT NULL, value TEXT,
    theme TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '',
    salience REAL NOT NULL DEFAULT 0.5, provenance TEXT NOT NULL DEFAULT 'stated',
    mode TEXT NOT NULL DEFAULT 'task', occurred_at TEXT, recorded_at REAL NOT NULL,
    last_seen_at REAL, last_surfaced_at REAL, hit_count INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER REFERENCES memories(id), supersede_reason TEXT)
```

| Column | Type | Description |
|---|---|---|
| `id` | integer | Memory id, shown to the model as `m12` |
| `entity_id` | integer | Who or what it's about (`entities.id`) |
| `kind` | text | `fact`, `event`, `series_point` (one measurement) or `lesson` (a lived experience) |
| `attribute` | text | Slot name for facts with one current value (`home_city`, `diet`), or the series name (`weight`). User facts with an attribute form the profile |
| `content` | text | The memory as one self-contained sentence |
| `value` | text | Structured value, e.g. `82.4` for a series point |
| `theme` | text | Comma-separated broad topics, e.g. `travel,flight,food` |
| `keywords` | text | Comma-separated retrieval keys the librarian wrote: names from the content plus words the user might say later (`flying, airport, layover`) |
| `salience` | real | 0 to 1, how much it matters. Ranks recall, gates association, adjusted by the consolidator |
| `provenance` | text | `stated` (the user said it), `observed` or `inferred`. Inferred is capped at 0.4 |
| `mode` | text | `task` or `personal` |
| `occurred_at` | text | ISO date it happened, if known (the one timestamp that isn't epoch seconds, since it's often just a date) |
| `recorded_at` | real | When it was stored |
| `last_seen_at` | real | Last time it was recalled or surfaced |
| `last_surfaced_at` | real | Last time associative recall offered it; it isn't offered again for 2 days |
| `hit_count` | integer | How often it was recalled or surfaced |
| `superseded_by` | integer | Set when a newer memory replaced this one (moved, corrected). Superseded rows are kept as history but never returned |
| `supersede_reason` | text | Why it was replaced, if the user said (`moved`, `wrong`) |

- `forget` deletes the memory and the older versions it superseded. Nothing else hard-deletes.
- Indexes: `(entity_id, attribute, occurred_at)` for series, and `salience`.

#### `memories_fts`

FTS5 index over `content`, `theme`, `keywords`, `attribute` and the entity name, with the
`porter` stemmer ("drinks" matches "drink"). Rowid = `memories.id`. Triggers on `memories` keep
it in sync, including manual `UPDATE`s. `recall` searches all columns; associative recall
searches only `theme` and `keywords`. FTS5 keeps its data in internal `memories_fts_*` tables;
don't edit those.

#### `relationships`

| Column | Type | Description |
|---|---|---|
| `id` | integer | Shown to the model as `r3` |
| `entity_a` | integer | Usually the user |
| `entity_b` | integer | The other entity: "*b* is *a*'s *link_type*" |
| `link_type` | text | e.g. `sister`, `manager`, `dog` |
| `salience`, `provenance`, `recorded_at` | | As in `memories` |
| `superseded_by` | integer | Reserved for corrections; not set by anything yet |

#### `memory_meta`

Key/value settings: `consolidated_at` (last consolidator run) and `v1_migrated` (rows copied
from the old table).

#### `memories_v1`

Only in databases created before the current schema: the original FTS5 table
(`id, content, tags, created_at`), renamed on first open. Its rows were copied into `memories` as
plain user facts. It's kept as a backup and nothing reads it; drop it with
`DROP TABLE memories_v1` once you're happy with the migration.

### `sources`

URL templates that worked for a kind of task, so repeat requests skip the web search. Written by
`save_source` after the URL was fetched successfully and the independent LLM check
(`assistant/judge.py`) approved it.

```sql
CREATE TABLE sources (
    id INTEGER PRIMARY KEY, task TEXT, url_template TEXT UNIQUE, notes TEXT,
    successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0, last_ok REAL, created_at REAL, check_reason TEXT)
```

| Column | Type | Description |
|---|---|---|
| `id` | integer | Source id, passed to `http_get` as `source_id` |
| `task` | text | What it's for, e.g. `currency exchange rate`. Matched by `find_sources` |
| `url_template` | text, unique | URL with `{placeholders}`, e.g. `https://api.frankfurter.dev/v1/latest?base={base}&symbols={target}` |
| `notes` | text | How to fill the placeholders and what the response contains |
| `successes` | integer | Successful fetches through this source (starts at 1) |
| `failures` | integer | Failed fetches through this source |
| `consecutive_failures` | integer | Failures in a row. At 3 the source is hidden from `find_sources` until saved again |
| `last_ok` | real | Time of the last successful fetch |
| `created_at` | real | When it was first saved |
| `check_reason` | text | The LLM check's explanation for approving it |

- Saving the same `url_template` again updates the existing row and resets `consecutive_failures`.
- `save_source(..., replaces_source_id=N)` saves the new source and deletes source `N`;
  `delete_source` removes one.
- `check_reason` was added after the table first existed; `assistant/web/sources.py` adds the column to older
  databases automatically.

### `pages`

Local copies of fetched content. `http_get` returns a cached copy when it's younger than
`max_age_minutes` (default 30), and `save_source` requires the example URL to have been fetched in
the last 24 hours.

```sql
CREATE TABLE pages (
    url TEXT PRIMARY KEY, fetched_at REAL, status INTEGER,
    content_type TEXT, text TEXT)
```

| Column | Type | Description |
|---|---|---|
| `url` | text, primary key | The URL as requested (before any redirects) |
| `fetched_at` | real | When it was fetched |
| `status` | integer | HTTP status; only successful (`200`) responses are stored |
| `content_type` | text | `text/html`, `application/json`, `text/plain`, `application/xml` or `text/xml` |
| `text` | text | Extracted text: HTML reduced to plain text, JSON compacted. Capped at 20,000 characters |

This is content from the internet. Treat it as untrusted.

### `fetch_log`

Every fetch attempt, including blocked ones. Written by `assistant/web/fetch.py`.

```sql
CREATE TABLE fetch_log (ts REAL, url TEXT, outcome TEXT, detail TEXT)
```

| Column | Type | Description |
|---|---|---|
| `ts` | real | When it happened |
| `url` | text | URL of this hop (each redirect is logged separately) |
| `outcome` | text | See below |
| `detail` | text | Reason or extra info, up to 500 characters |

| `outcome` | Meaning | Example `detail` |
|---|---|---|
| `ok` | Fetched and cached | `application/json 326 bytes` |
| `cache_hit` | Served from `pages` | |
| `redirect` | Followed a redirect | the next URL |
| `blocked` | Refused by a safety rule | `localhost resolves to non-public address 127.0.0.1`, `port 8080 not allowed`, `content-type 'image/png' not allowed` |
| `http_error` | Non-200 response | `404` |
| `error` | Network failure, timeout, too many redirects | the exception |

### `search_usage`

Brave Search requests per calendar month. `web_search` refuses once the count reaches
`BRAVE_MONTHLY_LIMIT` (default 1000, about the $5 monthly free credit).

```sql
CREATE TABLE search_usage (month TEXT PRIMARY KEY, count INTEGER)
```

| Column | Type | Description |
|---|---|---|
| `month` | text, primary key | `YYYY-MM` |
| `count` | integer | Requests sent to Brave that month, including failed ones |

## `.phoenix/phoenix.db`

Phoenix's own database, created by `run_phoenix.sh` / `start.sh` when `PHOENIX_COLLECTOR_ENDPOINT`
is set in `.env`. Its schema belongs to Phoenix and can change between versions, so browse it in
the Phoenix UI at http://127.0.0.1:6006 rather than querying it.

It holds, per conversation turn: prompts and responses, token counts and costs, tool inputs and
outputs, and the SQL text of every `memories.db` query made during the turn (without parameter
values). App traffic is in the `personal-assistant` project, eval runs in
`personal-assistant-evals` (one session per run).

## Looking at the data

In the app: the **Memories** and **Sources** tabs of the web UI's Verbose panel, or `/memories`
and `/sources` in the terminal chat.

With the `sqlite3` CLI (`sudo apt install sqlite3`):

```bash
# Current memories, most important first
sqlite3 -header -column memories.db \
  "SELECT m.id, e.name AS about, kind, attribute, round(salience, 2) AS sal, content
   FROM memories m JOIN entities e ON e.id = m.entity_id
   WHERE superseded_by IS NULL ORDER BY salience DESC"

# What was replaced, and by what
sqlite3 -header -column memories.db \
  "SELECT old.content AS was, new.content AS now, old.supersede_reason AS why
   FROM memories old JOIN memories new ON new.id = old.superseded_by"

sqlite3 -header -column memories.db \
  "SELECT id, task, successes, failures, url_template FROM sources ORDER BY id"

# Recent fetches, newest first
sqlite3 -header -column memories.db \
  "SELECT datetime(ts,'unixepoch','localtime') AS time, outcome, url, detail
   FROM fetch_log ORDER BY ts DESC LIMIT 20"

# Everything that was blocked
sqlite3 -header -column memories.db \
  "SELECT datetime(ts,'unixepoch','localtime') AS time, url, detail
   FROM fetch_log WHERE outcome = 'blocked'"

# Cached pages and their age in minutes
sqlite3 -header -column memories.db \
  "SELECT url, content_type, length(text) AS chars,
          round((strftime('%s','now') - fetched_at) / 60) AS age_min
   FROM pages ORDER BY fetched_at DESC"

sqlite3 -header -column memories.db "SELECT * FROM search_usage"
```

Or browse it in a local web page with [Datasette](https://datasette.io):
`pip install datasette && datasette memories.db` (http://127.0.0.1:8001).

## Resetting data

Stop the app first. Then either delete specific data:

```bash
sqlite3 memories.db "DELETE FROM pages"       # clear the page cache
sqlite3 memories.db "DELETE FROM fetch_log"   # clear the audit log
sqlite3 memories.db "DELETE FROM sources"     # forget all cached sources
sqlite3 memories.db "DELETE FROM memories; DELETE FROM relationships; DELETE FROM entities WHERE type != 'user'"
                                              # forget everything about the user
```

or remove `memories.db` entirely; it's recreated empty on the next start. To clear traces, delete
`.phoenix/`.
