#!/bin/bash
#
# Start the Anthropic-compatible API server using fsrouter.
#
# Usage:
#   ./start.sh                    # Uses default port 8082
#   PORT=9000 ./start.sh          # Custom port
#   COMMAND_TIMEOUT=300 ./start.sh # 5 min handler timeout (recommended)
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8082}"

# Find fsrouter.py — check common locations
FSROUTER=""
for candidate in \
    "$SCRIPT_DIR/../fsrouter/python/fsrouter.py" \
    "$SCRIPT_DIR/fsrouter.py" \
    "$(which fsrouter.py 2>/dev/null || true)"
do
    if [ -f "$candidate" ]; then
        FSROUTER="$candidate"
        break
    fi
done

if [ -z "$FSROUTER" ]; then
    echo "ERROR: fsrouter.py not found."
    echo "Clone it alongside this project:"
    echo "  git clone https://github.com/curtcox/fsrouter.git"
    echo "Or copy fsrouter.py into this directory."
    exit 1
fi

# Ensure data directories exist
mkdir -p "$SCRIPT_DIR/data/batches"
mkdir -p "$SCRIPT_DIR/data/files"

cat << BANNER
==============================================
  Anthropic-Compatible API Server
  Powered by fsrouter + claude CLI
==============================================

  Base URL:  http://localhost:${PORT}

  ── Messages (GA) ──────────────────────────
    POST /v1/messages
    POST /v1/messages/count_tokens

  ── Models (GA) ────────────────────────────
    GET  /v1/models
    GET  /v1/models/:model_id

  ── Batches (GA) ───────────────────────────
    POST /v1/messages/batches
    GET  /v1/messages/batches
    GET  /v1/messages/batches/:batch_id
    POST /v1/messages/batches/:batch_id/cancel
    GET  /v1/messages/batches/:batch_id/results

  ── Agents (Beta) ──────────────────────────
    POST /v1/agents
    GET  /v1/agents
    GET  /v1/agents/:agent_id
    POST /v1/agents/:agent_id
    POST /v1/agents/:agent_id/archive

  ── Sessions (Beta) ────────────────────────
    POST /v1/sessions
    GET  /v1/sessions
    GET  /v1/sessions/:session_id
    POST /v1/sessions/:session_id
    DELETE /v1/sessions/:session_id
    POST /v1/sessions/:session_id/archive
    POST /v1/sessions/:session_id/events
    GET  /v1/sessions/:session_id/events

  ── Environments (Beta) ────────────────────
    POST /v1/environments
    GET  /v1/environments
    GET  /v1/environments/:environment_id
    POST /v1/environments/:environment_id
    DELETE /v1/environments/:environment_id

  ── Vaults (Beta) ──────────────────────────
    POST /v1/vaults
    GET  /v1/vaults
    GET  /v1/vaults/:vault_id
    POST /v1/vaults/:vault_id
    DELETE /v1/vaults/:vault_id

  ── Files (Beta) ───────────────────────────
    POST /v1/files
    GET  /v1/files
    GET  /v1/files/:file_id
    DELETE /v1/files/:file_id

  ── Skills (Beta) ──────────────────────────
    POST /v1/skills
    GET  /v1/skills

  No API key required!

  Example:
    curl http://localhost:${PORT}/v1/messages \\
      -H "Content-Type: application/json" \\
      -d '{"model":"claude-sonnet-4-6","max_tokens":1024,"messages":[{"role":"user","content":"Hello!"}]}'

==============================================
BANNER

export ROUTE_DIR="$SCRIPT_DIR/routes"
export LISTEN_ADDR=":${PORT}"
export COMMAND_TIMEOUT="${COMMAND_TIMEOUT:-300}"

exec python3 "$FSROUTER"
