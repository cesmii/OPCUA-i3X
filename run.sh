#!/usr/bin/env bash
# Start the i3xua wrapper.
#
# Usage:
#   ./run.sh                   # uses ./config.yaml
#   ./run.sh path/to/my.yaml
#
# Env vars consumed by config expansion (see config.example.yaml):
#   I3XUA_TOKEN   — bearer token when server.auth.mode == "bearer"
#                       (the example config references ${I3XUA_TOKEN})
set -euo pipefail

CONFIG="${1:-config.yaml}"

# Bearer token only matters if the config sets auth.mode: bearer. The current
# config.yaml ships with auth.mode: none so this is just a sensible default
# for when you flip it back on.
export I3XUA_TOKEN="${I3XUA_TOKEN:-dev-token}"

# Defensive: kill any stale instance on the same port so restarts are clean.
if command -v pkill >/dev/null 2>&1; then
  pkill -9 -f "i3xua --config" 2>/dev/null || true
  sleep 0.3
fi

echo "starting i3xua with config=${CONFIG}"
echo "  bearer token fallback: I3XUA_TOKEN=${I3XUA_TOKEN}"
exec uv run i3xua --config "${CONFIG}"
