# Anthropic-Compatible API Server

A local API server that exposes endpoints compatible with the [Anthropic API](https://platform.claude.com/docs/en/api/overview), powered by [fsrouter](https://github.com/curtcox/fsrouter) and the `claude` CLI. **No API key required.**

Implements **38 endpoints** covering the full Anthropic API surface: Messages, Models, Batches, Agents, Sessions, Environments, Vaults, Files, and Skills.

## How It Works

fsrouter maps the directory tree under `routes/` directly to HTTP endpoints. Each handler is a Python script that translates API requests into `claude` CLI calls (for message generation) or local JSON-file storage (for resource CRUD) and formats responses to match the official API shape.

## Prerequisites

- Python 3.8+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [fsrouter](https://github.com/curtcox/fsrouter) (clone it next to this project)

## Quick Start

```bash
git clone https://github.com/curtcox/fsrouter.git
cd claude-as-anthropic-api-server
./start.sh
```

The server starts on port 8082 by default. Customize with `PORT=9000 ./start.sh`.

## Usage Examples

### Messages

```bash
curl http://localhost:8082/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Create an Agent + Environment + Session (Managed Agents flow)

```bash
# 1. Create an agent
AGENT_ID=$(curl -s http://localhost:8082/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Coding Assistant",
    "model": "claude-sonnet-4-6",
    "system": "You are a helpful coding assistant.",
    "tools": [{"type": "agent_toolset_20260401"}]
  }' | jq -r '.id')

# 2. Create an environment
ENV_ID=$(curl -s http://localhost:8082/v1/environments \
  -H "Content-Type: application/json" \
  -d '{
    "name": "python-env",
    "config": {
      "type": "cloud",
      "networking": {"type": "unrestricted"},
      "packages": {"pip": ["pandas", "numpy"]}
    }
  }' | jq -r '.id')

# 3. Create a session
SESSION_ID=$(curl -s http://localhost:8082/v1/sessions \
  -H "Content-Type: application/json" \
  -d "{
    \"agent\": \"$AGENT_ID\",
    \"environment_id\": \"$ENV_ID\",
    \"title\": \"My coding session\"
  }" | jq -r '.id')

# 4. Send events to the session
curl -s "http://localhost:8082/v1/sessions/$SESSION_ID/events" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [{
      "type": "user.message",
      "content": [{"type": "text", "text": "Write a fibonacci function in Python"}]
    }]
  }'
```

## All Endpoints (38 total)

### Messages (GA)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Create a message |
| POST | `/v1/messages/count_tokens` | Count input tokens |

### Models (GA)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/models` | List available models |
| GET | `/v1/models/:model_id` | Get model details |

### Message Batches (GA)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages/batches` | Create a batch |
| GET | `/v1/messages/batches` | List batches |
| GET | `/v1/messages/batches/:batch_id` | Get batch status |
| POST | `/v1/messages/batches/:batch_id/cancel` | Cancel a batch |
| GET | `/v1/messages/batches/:batch_id/results` | Get batch results |

### Agents (Beta: `managed-agents-2026-04-01`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/agents` | Create agent |
| GET | `/v1/agents` | List agents |
| GET | `/v1/agents/:agent_id` | Retrieve agent |
| POST | `/v1/agents/:agent_id` | Update agent |
| POST | `/v1/agents/:agent_id/archive` | Archive agent |

### Sessions (Beta: `managed-agents-2026-04-01`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/sessions` | Create session |
| GET | `/v1/sessions` | List sessions |
| GET | `/v1/sessions/:session_id` | Retrieve session |
| POST | `/v1/sessions/:session_id` | Update session |
| DELETE | `/v1/sessions/:session_id` | Delete session |
| POST | `/v1/sessions/:session_id/archive` | Archive session |
| POST | `/v1/sessions/:session_id/events` | Send events |
| GET | `/v1/sessions/:session_id/events` | List events |

### Environments (Beta: `managed-agents-2026-04-01`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/environments` | Create environment |
| GET | `/v1/environments` | List environments |
| GET | `/v1/environments/:environment_id` | Retrieve environment |
| POST | `/v1/environments/:environment_id` | Update environment |
| DELETE | `/v1/environments/:environment_id` | Delete environment |

### Vaults (Beta: `managed-agents-2026-04-01`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/vaults` | Create vault |
| GET | `/v1/vaults` | List vaults |
| GET | `/v1/vaults/:vault_id` | Retrieve vault |
| POST | `/v1/vaults/:vault_id` | Update vault |
| DELETE | `/v1/vaults/:vault_id` | Delete vault |

### Files (Beta: `files-api-2025-04-14`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/files` | Upload file |
| GET | `/v1/files` | List files |
| GET | `/v1/files/:file_id` | Retrieve file metadata |
| DELETE | `/v1/files/:file_id` | Delete file |

### Skills (Beta: `skills-2025-10-02`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/skills` | Create skill |
| GET | `/v1/skills` | List skills |

## Differences from the Official API

- **No authentication** — `x-api-key` and `anthropic-beta` headers are accepted but ignored
- **No streaming/SSE** — `stream: true` returns an error; session event streaming returns JSON instead of SSE
- **Token counts are estimates** — count_tokens uses a heuristic (~4 chars/token)
- **Batch processing is synchronous** — batches are processed when results are requested
- **No real containers** — environments are stored as configuration only; no actual container provisioning
- **No real credential storage** — vaults store metadata only, no actual secret management
- **Session events use claude CLI** — events are processed synchronously rather than via a long-running agent loop
- **Usage metrics are approximate** — based on claude CLI output when available

## Architecture

```
routes/                      ← fsrouter maps this to URLs
  v1/
    messages/POST            ← calls claude CLI, returns Anthropic response format
    agents/POST              ← CRUD backed by lib/store.py (JSON files)
    sessions/.../events/POST ← calls claude CLI within session context
    ...

lib/
  claude_bridge.py           ← claude CLI invocation + response formatting
  store.py                   ← JSON-file-backed CRUD for all resources

data/                        ← created at runtime
  agent.json                 ← stored agents
  session.json               ← stored sessions
  environment.json           ← stored environments
  vault.json                 ← stored vaults
  file.json                  ← file metadata
  skill.json                 ← stored skills
  batches/                   ← batch data
  files/                     ← uploaded file content
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `PORT` | `8082` | Server listen port |
| `COMMAND_TIMEOUT` | `300` | Max seconds per handler execution |

## License

MIT
