# Local AI Assistant

[![CI](https://github.com/rpkpdl/local-ai-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/rpkpdl/local-ai-assistant/actions/workflows/ci.yml)

A personal AI assistant with long-term memory, built directly on the Anthropic Python SDK, with
no agent framework. All data (memories, saved web sources, fetched pages and the audit log) lives
in one local SQLite file that you own and can inspect.

The assistant remembers facts about you across sessions, looks things up on the web through a
hardened fetcher, and learns reusable data sources over time. Every source it saves is checked
by an independent LLM "judge" first. It runs on Claude by default, or fully on a local model
through Ollama.

## Features

- **Long-term memory:** `remember` / `recall` / `forget` tools backed by SQLite FTS5 full-text
  search with bm25 ranking. The model decides what's worth keeping.
- **Web lookups that improve over time:** the agent checks its saved sources first, then searches
  (Brave Search API), fetches, and saves good URLs as reusable templates
  (e.g. `https://api.example.com/weather?lat={lat}&lon={lon}`). Next time it skips the search.
- **LLM source judge:** before a source is saved, a separate model call with no tools approves or
  rejects it. If the user says a source is bad, the agent replaces it.
- **SSRF-guarded fetching:** a custom HTTP fetcher with DNS rebinding protection (details below).
- **Streaming web UI:** a single-file vanilla JS page with streaming markdown and a side panel
  showing live tool activity, token usage, cost per turn and per chat, memories and sources.
- **Terminal mode:** the same agent as a REPL.
- **Observability:** optional [Arize Phoenix](https://github.com/Arize-ai/phoenix) tracing via
  OpenInference. Every LLM call, tool call and SQLite query shows up as a span.
- **Cost tracking:** computed per API call, split into agent and judge spend.
- **Local model option:** run the agent on Ollama (e.g. Gemma) with the same code path.

## Architecture

```text
 Browser UI ──NDJSON stream──▶ FastAPI (server.py)          Terminal (cli.py)
                                      │                              │
                                      └──────────┬───────────────────┘
                                                 ▼
                                    Agent (agent.py)
                         Anthropic SDK tool runner, streaming
                                                 │
        ┌───────────────────┬───────────────────┼────────────────────┐
        ▼                   ▼                   ▼                    ▼
  Memory tools        find_sources         web_search          http_get
  (memory.py)         save_source          (web/search.py,     (web/fetch.py,
  SQLite FTS5         (web/sources.py)     Brave API +         SSRF guard,
                           │               monthly quota)      page cache,
                           ▼                                   audit log)
                    SourceJudge
                    (judge.py,
                 tool-less LLM check)

               All state ──▶ memories.db (SQLite)    Traces ──▶ Phoenix (optional)
```

```text
assistant/              the application package
├── agent.py            agent loop, system prompt, memory tools, model and pricing config
├── memory.py           MemoryStore interface: SQLite (FTS5) and JSON implementations
├── judge.py            independent approve/reject check for new sources
├── tracing.py          Phoenix / OpenInference setup, traced SQLite connection
├── server.py           FastAPI app: streaming chat, memories and sources endpoints
├── cli.py              terminal chat
├── static/index.html   web UI (no build step, no external resources)
└── web/                everything that touches the internet
    ├── tools.py        web tools exposed to the model
    ├── fetch.py        SSRF-guarded fetcher, page cache, audit log
    ├── search.py       Brave Search client with a monthly quota
    └── sources.py      saved URL templates per task
tests/                  offline pytest suite (security guards, server, memory)
evals/                  scored evals with datasets and saved results
scripts/run_phoenix.sh  local Phoenix trace UI
docs/database.md        every table in the database, with example queries
Dockerfile, docker-compose.yml
```

## Security design

The agent reads untrusted web content and can call tools, so the design assumes fetched pages may
try to manipulate it.

**Fetching (`assistant/web/fetch.py`)**
- Only `http`/`https` on ports 80 and 443.
- Every resolved IP must be public (`ipaddress.is_global`), and the connection is pinned to the IP
  that was checked. This blocks DNS rebinding, where a hostname resolves to a public IP during the
  check and a private one when connecting.
- Every redirect hop is re-checked (max 5).
- Content types are allow-listed (HTML, text, JSON, XML), with a 2 MB size cap and a cap on the
  text passed to the model.
- Every fetch, allowed or blocked, is written to an audit log.

**Prompt injection**
- Fetched content reaches the model wrapped in `<untrusted_web_content>` tags, and the system
  prompt treats it as data, never instructions.
- The source judge has no tools, so injected text can at worst change a verdict. It always runs
  on Claude, even when the agent runs on a small local model that is easier to talk round.

**Local server**
- Binds to `127.0.0.1` only, with a trusted host check. In Docker the app has to listen on
  `0.0.0.0` inside the container, so compose publishes the port on the host's `127.0.0.1` only.
  The result is the same: unreachable from the network.
- Origin checks plus JSON-only POSTs protect against CSRF.
- No auto-generated API docs routes.
- The markdown renderer escapes HTML before formatting and loads nothing from a CDN.

**Tracing** records SQL text only, never parameter values, so memories don't leak into traces.

## Why a local model option

Setting `ASSISTANT_MODEL` to an Ollama model (e.g. `gemma4:e4b`) runs the agent on your own
hardware through Ollama's Anthropic-compatible API. The SDK and tool runner stay the same.

- **Testing for free:** develop and test the agent loop, tools and UI without paying for API
  calls. Local turns are counted as $0.
- **Privacy:** conversations and memories never leave the machine.
- **Knowing the limits:** running the same tools on a small model shows where it struggles
  (tool choice, long fetched pages) and where a frontier model is worth paying for.
- **Groundwork for on-device use:** small models are getting good enough to run on phones. The
  long-term goal is an assistant that can run on a phone without a server.

Trade-offs: local runs have thinking disabled, and on a CPU a turn can take from 15 seconds to a
couple of minutes. Set Ollama's context length to at least 16k, because the default 4k silently
truncates fetched pages.

## Getting started

You need an [Anthropic API key](https://console.anthropic.com/). A
[Brave Search API key](https://brave.com/search/api/) is optional (enables web search).

```bash
git clone https://github.com/rpkpdl/local-ai-assistant.git
cd local-ai-assistant
cp .env.example .env      # then add your ANTHROPIC_API_KEY (and BRAVE_API_KEY for web search)
```

### Option 1: Docker (quickest)

```bash
docker compose up --build
```

- Assistant: http://127.0.0.1:8000
- Phoenix traces: http://127.0.0.1:6006

Memories and traces are kept in Docker volumes, so they survive rebuilds. The Docker setup runs
on Claude. For a local model, use option 2.

### Option 2: Run locally (for development, or a local model)

Requires Python 3.11+.

```bash
./start.sh                # web UI on http://127.0.0.1:8000
./start.sh --cli          # or chat in the terminal
```

`start.sh` creates the virtual environment, installs the package (`pip install -e ".[phoenix,dev]"`),
checks your `.env`, and starts Phoenix if tracing is enabled. To use a local model, install
[Ollama](https://ollama.com/), pull a model and set `ASSISTANT_MODEL` in `.env`.

| Setting in `.env` | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required |
| `BRAVE_API_KEY` | Enables web search. Without it, fetching and saved sources still work |
| `BRAVE_MONTHLY_LIMIT` | Monthly search cap (default 1000) |
| `ASSISTANT_MODEL` | e.g. `gemma4:e4b` to run on Ollama. Defaults to Claude |
| `OLLAMA_URL` | Defaults to `http://127.0.0.1:11434` |
| `PHOENIX_COLLECTOR_ENDPOINT` | Enables tracing. Phoenix UI on http://127.0.0.1:6006 |

## Testing

```bash
.venv/bin/pytest          # offline: no API key or network needed
.venv/bin/ruff check .
```

The tests focus on the security guards: blocked URLs, private and metadata IPs at connect time,
redirects into private networks, content types, Host/Origin/CSRF checks on the server, the
checks around saving sources, and the memory stores. CI runs them on every push, plus a Docker
build and smoke test.

### Keeping personal data out of the repo

The assistant's database holds real personal memories, so commits are checked before they
happen. `scripts/check_private.py` runs as a pre-commit hook. It blocks commits that contain API
keys, files that must stay local (`.env`, databases, traces), the text of any stored memory, or
terms listed in a local, gitignored `.private-terms` file. Examples, tests and evals use synthetic
data only. CI runs the same script for keys and forbidden files.

```bash
git config core.hooksPath .githooks   # once per clone
```

## Evals

Tests check correctness. [Evals](evals/) measure behavior, with scores tracked over time:

| Eval | What it measures | Latest result |
|---|---|---|
| Memory recall (retrieval) | Does memory search find the right fact? 22 questions, 12 of them paraphrased | 10/10 direct, **0/12 paraphrased** |
| Source judge | Approve good APIs, reject junk pages and 5 prompt-injection attempts | **11/11**, including all 5 injections |

The 0/12 is the known weakness of keyword search, now measured. It's the baseline for the
vector search on the roadmap.

```bash
.venv/bin/python evals/run_eval.py memory --mode retrieval   # free
.venv/bin/python evals/run_eval.py judge                     # ~$0.05
```

## Findings

[docs/findings.md](docs/findings.md) is a dated log of things observed while building this, each
with evidence, cause and outcome. For example, it shows why a 4B local model never searched
memory when Claude did.

## Known limitations

- `recall` is keyword search, so it misses paraphrases ("what do I like to drink" won't find a
  memory about tea).
- Conversation history is kept in memory and is lost on restart. Memories persist.
- Prompt caching isn't used yet.
- Only reachable from the machine it runs on.

## Roadmap

1. **Phone access:** secure remote access, an installable mobile web app (PWA), saved
   conversations and prompt caching.
2. **Voice:** speech-to-text with local Whisper, text-to-speech, then a hands-free mode. Store
   transcripts, not audio.
3. **Better recall:** hybrid keyword + vector search (fastembed + sqlite-vec).
4. **Multi-device sync:** UUID ids, timestamps and soft deletes.
5. **More evals:** run the memory eval through the full agent on Claude and on local models, and
   add cases for facts that change over time.
