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
| [`memories`](#memories) | `memory.py` | Facts about the user (long-term memory) |
| [`sources`](#sources) | `sources.py` | Known-good URL templates for recurring tasks |
| [`pages`](#pages) | `fetch.py` | Local cache of fetched page/API content |
| [`fetch_log`](#fetch_log) | `fetch.py` | Audit log of every fetch attempt |
| [`search_usage`](#search_usage) | `search.py` | Brave searches per month, for the quota cap |

### `memories`

Long-term facts about the user, written by the `remember` tool and searched by `recall`.

```sql
CREATE VIRTUAL TABLE memories USING fts5(id UNINDEXED, content, tags, created_at UNINDEXED)
```

| Column | Type | Description |
|---|---|---|
| `id` | text | 8-character hex id, shown by `recall` and used by `forget` |
| `content` | text | The fact, written to make sense on its own. Full-text indexed |
| `tags` | text | Space-separated topic tags, e.g. `preferences food`. Full-text indexed |
| `created_at` | real | When it was saved |

- It's an [FTS5](https://www.sqlite.org/fts5.html) full-text table. `recall` splits the query into
  words (3+ characters), ORs them together and ranks matches by `bm25`.
- FTS5 keeps its index in five internal tables: `memories_config`, `memories_content`,
  `memories_data`, `memories_docsize` and `memories_idx`. Don't read or edit them directly; use
  `memories`.
- With `--store json`, memories go to `memories.json` instead, and this table is unused.

### `sources`

URL templates that worked for a kind of task, so repeat requests skip the web search. Written by
`save_source` after the URL was fetched successfully and the independent LLM check
(`source_judge.py`) approved it.

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
- `check_reason` was added after the table first existed; `sources.py` adds the column to older
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

Every fetch attempt, including blocked ones. Written by `fetch.py`.

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
outputs, and the SQL text of every `memories.db` query (without parameter values).

## Looking at the data

In the app: the **Memories** and **Sources** tabs of the web UI's Verbose panel, or `/memories`
and `/sources` in the terminal chat.

With the `sqlite3` CLI (`sudo apt install sqlite3`):

```bash
sqlite3 -header -column memories.db "SELECT id, content, tags FROM memories"

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
sqlite3 memories.db "DELETE FROM memories"    # forget everything about the user
```

or remove `memories.db` entirely; it's recreated empty on the next start. To clear traces, delete
`.phoenix/`.
