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

# Ensure data directory exists for batches
mkdir -p "$SCRIPT_DIR/data/batches"

echo "=============================================="
echo "  Anthropic-Compatible API Server"
echo "  Powered by fsrouter + claude CLI"
echo "=============================================="
echo ""
echo "  Base URL:  http://localhost:${PORT}"
echo "  Endpoints:"
echo "    POST /v1/messages"
echo "    POST /v1/messages/count_tokens"
echo "    GET  /v1/models"
echo "    GET  /v1/models/:model_id"
echo "    POST /v1/messages/batches"
echo "    GET  /v1/messages/batches"
echo "    GET  /v1/messages/batches/:batch_id"
echo "    POST /v1/messages/batches/:batch_id/cancel"
echo "    GET  /v1/messages/batches/:batch_id/results"
echo ""
echo "  No API key required!"
echo ""
echo "  Example:"
echo '    curl http://localhost:'"${PORT}"'/v1/messages \'
echo '      -H "Content-Type: application/json" \'
echo '      -d '"'"'{"model":"claude-sonnet-4-6","max_tokens":1024,"messages":[{"role":"user","content":"Hello!"}]}'"'"
echo ""
echo "=============================================="

export ROUTE_DIR="$SCRIPT_DIR/routes"
export LISTEN_ADDR=":${PORT}"
export COMMAND_TIMEOUT="${COMMAND_TIMEOUT:-300}"

exec python3 "$FSROUTER"
