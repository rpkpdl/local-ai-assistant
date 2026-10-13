#!/usr/bin/env bash
# Start everything the assistant needs, checking each piece first.
#
#   ./start.sh          web UI   (http://127.0.0.1:8000)
#   ./start.sh --cli    terminal chat instead
#
# Ctrl+C stops the app, and Phoenix too if this script started it.
set -euo pipefail
cd "$(dirname "$0")"

MODE="web"
[[ "${1:-}" == "--cli" ]] && MODE="cli"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }
env_has() { grep -qE "^$1=.+" .env 2>/dev/null; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

echo "Checking setup..."

# 1. Python environment and dependencies
if [[ ! -x .venv/bin/python ]]; then
  warn "No virtual environment - creating .venv"
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import anthropic, dotenv, httpx, fastapi, uvicorn, phoenix.otel" 2>/dev/null \
   || [[ pyproject.toml -nt .venv/.installed ]]; then
  warn "Installing dependencies (first run can take a few minutes)"
  .venv/bin/pip install -q -e ".[phoenix,dev]"
  touch .venv/.installed
fi
ok "Python environment"

# 2. Configuration
[[ -f .env ]] || fail ".env not found - copy .env.example to .env and add your keys"
env_has ANTHROPIC_API_KEY || fail "ANTHROPIC_API_KEY missing from .env"
ok "Anthropic API key"
if env_has ASSISTANT_MODEL && [[ "$(env_get ASSISTANT_MODEL)" != claude-* ]]; then
  if port_open 11434; then ok "Ollama running (local model $(env_get ASSISTANT_MODEL); judge stays on Claude)"
  else warn "ASSISTANT_MODEL=$(env_get ASSISTANT_MODEL) needs Ollama on 127.0.0.1:11434 - not reachable"; fi
fi
if env_has BRAVE_API_KEY; then ok "Brave API key (web search on)"
else warn "BRAVE_API_KEY not set - web search off (fetching and cached sources still work)"; fi

# 3. Phoenix tracing (only if enabled in .env)
PHOENIX_PID=""
cleanup() {
  if [[ -n "$PHOENIX_PID" ]]; then
    echo; echo "Stopping Phoenix..."
    kill "$PHOENIX_PID" 2>/dev/null || true
    wait "$PHOENIX_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if env_has PHOENIX_COLLECTOR_ENDPOINT; then
  if port_open 6006; then
    ok "Phoenix already running (http://127.0.0.1:6006)"
  else
    mkdir -p .phoenix
    scripts/run_phoenix.sh > .phoenix/phoenix.log 2>&1 &
    PHOENIX_PID=$!
    printf '  … starting Phoenix'
    for _ in $(seq 1 60); do
      port_open 6006 && break
      kill -0 "$PHOENIX_PID" 2>/dev/null || { echo; fail "Phoenix exited - see .phoenix/phoenix.log"; }
      printf '.'; sleep 1
    done
    echo
    port_open 6006 || fail "Phoenix didn't start within 60 s - see .phoenix/phoenix.log"
    ok "Phoenix started (http://127.0.0.1:6006, log: .phoenix/phoenix.log)"
  fi
else
  warn "Tracing off (set PHOENIX_COLLECTOR_ENDPOINT in .env to enable)"
fi

# 4. The app
echo
if [[ "$MODE" == "cli" ]]; then
  .venv/bin/python -m assistant.cli
else
  port_open 8000 && fail "Port 8000 is already in use - is the assistant already running?"
  ok "Starting web UI - press Ctrl+C to stop"
  .venv/bin/python -m assistant.server
fi
