#!/usr/bin/env bash
# Start a local Phoenix trace viewer at http://127.0.0.1:6006
set -euo pipefail
cd "$(dirname "$0")/.."

export PHOENIX_HOST=127.0.0.1                  # default is 0.0.0.0 (whole network, no auth)
export PHOENIX_WORKING_DIR="$PWD/.phoenix"     # traces stored here (SQLite), not in ~/.phoenix
export PHOENIX_TELEMETRY_ENABLED=false         # no FullStory / Scarf tracking
export PHOENIX_ALLOW_EXTERNAL_RESOURCES=false  # no web fonts or other outbound requests

exec .venv/bin/phoenix serve
