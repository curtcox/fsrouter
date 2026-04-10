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
PROBLEMS=0
FATAL=0

# ── Color helpers (degrade gracefully if no tty) ─────────────
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BOLD='\033[1m'
    RESET='\033[0m'
else
    RED='' GREEN='' YELLOW='' BOLD='' RESET=''
fi

ok()   { echo -e "  ${GREEN}[OK]${RESET}    $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${RESET}  $1"; PROBLEMS=$((PROBLEMS + 1)); }
fail() { echo -e "  ${RED}[FAIL]${RESET}  $1"; PROBLEMS=$((PROBLEMS + 1)); FATAL=$((FATAL + 1)); }

# ══════════════════════════════════════════════════════════════
#  Preflight Checks
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}Preflight checks${RESET}"
echo "────────────────────────────────────────────"

# ── 1. Python ────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    ok "Python 3 found: $PY_VERSION"
else
    fail "Python 3 not found"
    echo ""
    echo "       Resolution:"
    echo "         macOS:   brew install python3"
    echo "         Ubuntu:  sudo apt install python3"
    echo "         Fedora:  sudo dnf install python3"
    echo ""
fi

# ── 2. fsrouter.py ───────────────────────────────────────────
FSROUTER=""
for candidate in \
    "$SCRIPT_DIR/../../python/fsrouter.py" \
    "$SCRIPT_DIR/../fsrouter/python/fsrouter.py" \
    "$SCRIPT_DIR/fsrouter.py" \
    "$(which fsrouter.py 2>/dev/null || true)"
do
    if [ -f "$candidate" ]; then
        FSROUTER="$(cd "$(dirname "$candidate")" && pwd)/$(basename "$candidate")"
        break
    fi
done

if [ -n "$FSROUTER" ]; then
    ok "fsrouter.py found: $FSROUTER"
else
    fail "fsrouter.py not found"
    echo ""
    echo "       Resolution:"
    echo "         If you cloned the fsrouter repo, the server is inside it at:"
    echo "           examples/claude-as-anthropic-api-server/"
    echo "         Run start.sh from there — it auto-discovers ../../python/fsrouter.py"
    echo ""
    echo "         Otherwise, copy or symlink fsrouter.py into this directory:"
    echo "           cp /path/to/fsrouter/python/fsrouter.py ."
    echo ""
fi

# ── 3. Claude CLI installed ──────────────────────────────────
if command -v claude &>/dev/null; then
    CLAUDE_VERSION=$(claude --version 2>&1 || echo "unknown")
    ok "Claude CLI found: $CLAUDE_VERSION"
else
    fail "Claude CLI (claude) not found in PATH"
    echo ""
    echo "       The 'claude' command is required for endpoints that call Claude"
    echo "       (POST /v1/messages, POST /v1/sessions/:id/events, batch results)."
    echo ""
    echo "       Resolution:"
    echo "         Install Claude Code:"
    echo "           npm install -g @anthropic-ai/claude-code"
    echo ""
    echo "         Or see: https://docs.anthropic.com/en/docs/claude-code"
    echo ""
    echo "       Note: read-only endpoints (models, agents CRUD, etc.) will still work."
    echo ""
fi

# ── 4. Claude CLI authentication ─────────────────────────────
if command -v claude &>/dev/null; then
    AUTH_JSON=$(claude auth status 2>&1 || true)

    # Parse the JSON output from `claude auth status`
    LOGGED_IN=$(echo "$AUTH_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('loggedIn', False))
except:
    print('parse_error')
" 2>/dev/null || echo "parse_error")

    if [ "$LOGGED_IN" = "True" ]; then
        AUTH_METHOD=$(echo "$AUTH_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('authMethod', 'unknown'))
except:
    print('unknown')
" 2>/dev/null || echo "unknown")
        ok "Claude CLI authenticated (method: $AUTH_METHOD)"
    elif [ "$LOGGED_IN" = "False" ]; then
        fail "Claude CLI is NOT authenticated"
        echo ""
        echo "       This is why you're seeing:"
        echo "         {\"type\": \"error\", \"error\": {\"message\": \"Not logged in\"}}"
        echo ""
        echo "       Resolution — pick one:"
        echo ""
        echo "         Option A: Interactive login (recommended)"
        echo "           claude login"
        echo ""
        echo "         Option B: API key via environment variable"
        echo "           export ANTHROPIC_API_KEY=sk-ant-..."
        echo "           ./start.sh"
        echo ""
        echo "         Option C: API key setup command"
        echo "           claude setup-token"
        echo ""
        echo "       Troubleshooting:"
        echo "         - Run 'claude auth status' to see current auth state"
        echo "         - If using an API key, verify it at https://console.anthropic.com/settings/keys"
        echo "         - OAuth tokens can expire; re-run 'claude login' to refresh"
        echo "         - For CI/headless environments, use ANTHROPIC_API_KEY"
        echo ""
    else
        warn "Could not determine Claude CLI auth status"
        echo "       Output was: $AUTH_JSON"
        echo ""
        echo "       Try running 'claude auth status' manually to check."
        echo ""
    fi

    # ── 5. Claude CLI can actually reach the API ─────────────
    #    We always run this probe — even when auth status reports
    #    logged-in, the token could be expired or the API unreachable.
    if true; then
        PROBE_RESULT=$(echo "respond with only: ok" | claude -p --output-format json --model sonnet --no-session-persistence --bare 2>&1 || true)

        PROBE_ERROR=$(echo "$PROBE_RESULT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('is_error', False):
        print(data.get('result', 'unknown error'))
    else:
        print('ok')
except:
    print('parse_error')
" 2>/dev/null || echo "parse_error")

        if [ "$PROBE_ERROR" = "ok" ]; then
            ok "Claude API connectivity verified (test message succeeded)"
        elif [ "$PROBE_ERROR" = "parse_error" ]; then
            warn "Could not verify Claude API connectivity"
            echo "       The server may still work. Test it with:"
            echo "         curl http://localhost:${PORT}/v1/models"
            echo ""
        else
            # Tailor the message based on whether it looks like an auth problem
            case "$PROBE_ERROR" in
                *"Not logged in"*|*"login"*|*"logged in"*)
                    fail "Claude CLI is not logged in"
                    echo ""
                    echo "       This is the error you will see from the API:"
                    echo "         {\"type\":\"error\",\"error\":{\"message\":\"Not logged in\"}}"
                    echo ""
                    echo "       Resolution — pick one:"
                    echo ""
                    echo "         Option A: Interactive login (recommended)"
                    echo "           claude login"
                    echo ""
                    echo "         Option B: API key via environment variable"
                    echo "           export ANTHROPIC_API_KEY=sk-ant-..."
                    echo "           ./start.sh"
                    echo ""
                    echo "         Option C: Long-lived token"
                    echo "           claude setup-token"
                    echo ""
                    ;;
                *"Invalid API key"*|*"API key"*)
                    fail "Claude CLI has an invalid API key"
                    echo "       Error: $PROBE_ERROR"
                    echo ""
                    echo "       Resolution:"
                    echo "         - Verify your key at https://console.anthropic.com/settings/keys"
                    echo "         - Re-export:  export ANTHROPIC_API_KEY=sk-ant-..."
                    echo "         - Or switch to OAuth:  claude login"
                    echo ""
                    ;;
                *"rate limit"*|*"Rate limit"*|*"overloaded"*)
                    warn "Claude API returned a rate limit or overload error"
                    echo "       Error: $PROBE_ERROR"
                    echo ""
                    echo "       This is transient. The server will start, but requests may"
                    echo "       fail until capacity is available. Check:"
                    echo "         https://status.anthropic.com"
                    echo ""
                    ;;
                *)
                    fail "Claude CLI returned an error on test call"
                    echo "       Error: $PROBE_ERROR"
                    echo ""
                    echo "       Troubleshooting:"
                    echo "         - Check your internet connection"
                    echo "         - Verify auth: claude auth status"
                    echo "         - Check API status: https://status.anthropic.com"
                    echo "         - Run manually: echo 'hi' | claude -p --model sonnet"
                    echo ""
                    ;;
            esac
        fi
    fi
fi

# ── 6. Port availability ─────────────────────────────────────
if command -v lsof &>/dev/null; then
    PORT_PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [ -n "$PORT_PID" ]; then
        PORT_CMD=$(ps -p "$PORT_PID" -o comm= 2>/dev/null || echo "unknown")
        fail "Port $PORT is already in use (PID $PORT_PID: $PORT_CMD)"
        echo ""
        echo "       Resolution:"
        echo "         Use a different port:   PORT=9000 ./start.sh"
        echo "         Or stop the process:    kill $PORT_PID"
        echo ""
    else
        ok "Port $PORT is available"
    fi
elif command -v ss &>/dev/null; then
    if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
        fail "Port $PORT appears to be in use"
        echo ""
        echo "       Resolution:  PORT=9000 ./start.sh"
        echo ""
    else
        ok "Port $PORT is available"
    fi
else
    ok "Port $PORT (could not verify availability — lsof/ss not found)"
fi

# ── 7. Route handlers ────────────────────────────────────────
ROUTE_DIR="$SCRIPT_DIR/routes"
if [ -d "$ROUTE_DIR/v1" ]; then
    HANDLER_COUNT=$(find "$ROUTE_DIR" -type f \( -name GET -o -name POST -o -name DELETE -o -name PUT -o -name PATCH \) | wc -l | tr -d ' ')
    # macOS find doesn't support -executable; use -perm +111 on macOS, -executable on Linux
    if [[ "$(uname)" == "Darwin" ]]; then
        EXEC_COUNT=$(find "$ROUTE_DIR" -type f \( -name GET -o -name POST -o -name DELETE -o -name PUT -o -name PATCH \) -perm +111 2>/dev/null | wc -l | tr -d ' ')
    else
        EXEC_COUNT=$(find "$ROUTE_DIR" -type f \( -name GET -o -name POST -o -name DELETE -o -name PUT -o -name PATCH \) -executable 2>/dev/null | wc -l | tr -d ' ')
    fi

    if [ "$HANDLER_COUNT" -eq 0 ]; then
        fail "No route handlers found in $ROUTE_DIR"
        echo "       The routes/ directory should contain handler files named GET, POST, etc."
        echo ""
    elif [ "$EXEC_COUNT" -eq 0 ]; then
        fail "All $HANDLER_COUNT handlers are not executable — no endpoints will work"
        echo ""
        echo "       Resolution:"
        echo "         chmod +x \$(find routes -type f \\( -name GET -o -name POST -o -name DELETE \\))"
        echo ""
    elif [ "$EXEC_COUNT" -lt "$HANDLER_COUNT" ]; then
        NOT_EXEC=$((HANDLER_COUNT - EXEC_COUNT))
        warn "$NOT_EXEC of $HANDLER_COUNT handlers are not executable"
        echo ""
        echo "       Resolution:"
        echo "         chmod +x \$(find routes -type f \\( -name GET -o -name POST -o -name DELETE \\))"
        echo ""
    else
        ok "$HANDLER_COUNT route handlers found (all executable)"
    fi
else
    fail "Routes directory not found: $ROUTE_DIR"
    echo ""
fi

# ── Summary ──────────────────────────────────────────────────
echo "────────────────────────────────────────────"
if [ "$PROBLEMS" -eq 0 ]; then
    echo -e "  ${GREEN}All checks passed.${RESET}"
    echo ""
elif [ "$FATAL" -gt 0 ]; then
    echo -e "  ${RED}${FATAL} error(s) found. Fix them before starting the server.${RESET}"
    echo ""
    exit 1
else
    echo -e "  ${YELLOW}${PROBLEMS} warning(s) — starting with degraded functionality.${RESET}"
    echo ""
fi

# ══════════════════════════════════════════════════════════════
#  Ensure data directories exist
# ══════════════════════════════════════════════════════════════
mkdir -p "$SCRIPT_DIR/data/batches"
mkdir -p "$SCRIPT_DIR/data/files"

# ══════════════════════════════════════════════════════════════
#  Banner
# ══════════════════════════════════════════════════════════════
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
