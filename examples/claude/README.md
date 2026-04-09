# Anthropic-Compatible API Server

A local API server that exposes endpoints compatible with the [Anthropic Messages API](https://docs.anthropic.com/en/api/messages), powered by [fsrouter](https://github.com/curtcox/fsrouter) and the `claude` CLI. **No API key required.**

## How It Works

This server uses fsrouter's filesystem-based routing to map the directory tree under `routes/` directly to HTTP endpoints. Each handler is a Python script that translates Anthropic API requests into `claude` CLI calls and formats the responses to match the official API shape.

```
routes/
  v1/
    messages/
      POST                          → POST /v1/messages
      count_tokens/
        POST                        → POST /v1/messages/count_tokens
      batches/
        GET                         → GET  /v1/messages/batches
        POST                        → POST /v1/messages/batches
        :batch_id/
          GET                       → GET  /v1/messages/batches/:id
          cancel/POST               → POST /v1/messages/batches/:id/cancel
          results/GET               → GET  /v1/messages/batches/:id/results
    models/
      GET                           → GET  /v1/models
      :model_id/
        GET                         → GET  /v1/models/:id
```

## Prerequisites

- Python 3.8+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` command available)
- [fsrouter](https://github.com/curtcox/fsrouter) (clone it next to this project)

## Quick Start

```bash
# Clone fsrouter alongside this project
git clone https://github.com/curtcox/fsrouter.git

# Start the server
cd anthropic-api-server
./start.sh
```

The server starts on port 8082 by default. Customize with `PORT=9000 ./start.sh`.

## Usage

### Send a message (like the real API, minus the auth header)

```bash
curl http://localhost:8082/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "What is the capital of France?"}
    ]
  }'
```

### Response format (matches Anthropic API)

```json
{
  "id": "msg_abc123...",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "The capital of France is Paris."}
  ],
  "model": "claude-sonnet-4-6",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 15,
    "output_tokens": 8,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

### List models

```bash
curl http://localhost:8082/v1/models
```

### Count tokens

```bash
curl http://localhost:8082/v1/messages/count_tokens \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello, world!"}]
  }'
```

## Supported Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Create a message |
| POST | `/v1/messages/count_tokens` | Count input tokens |
| GET | `/v1/models` | List available models |
| GET | `/v1/models/:model_id` | Get model details |
| POST | `/v1/messages/batches` | Create a batch |
| GET | `/v1/messages/batches` | List batches |
| GET | `/v1/messages/batches/:batch_id` | Get batch status |
| POST | `/v1/messages/batches/:batch_id/cancel` | Cancel a batch |
| GET | `/v1/messages/batches/:batch_id/results` | Get batch results |

## Differences from the Official API

- **No authentication** — the `x-api-key` header is accepted but ignored
- **No streaming** — `stream: true` returns an error; use `stream: false`
- **Token counts are estimates** — the count_tokens endpoint uses a rough heuristic
- **Batch processing is synchronous** — batches are processed when results are requested
- **No vision/document support** — image and PDF content blocks are not forwarded to the CLI
- **Usage metrics are approximate** — based on character-to-token ratio estimates

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `PORT` | `8082` | Server listen port |
| `COMMAND_TIMEOUT` | `300` | Max seconds per handler execution |

## Architecture

Each route handler is an independent Python script executed as a subprocess by fsrouter. The handler:

1. Reads the JSON request body from stdin
2. Validates the request against the Anthropic API schema
3. Converts messages to a prompt string for the `claude` CLI
4. Invokes `claude -p --output-format json --model <model>`
5. Transforms the CLI output into the Anthropic API response format
6. Writes JSON to stdout (fsrouter sends it as the HTTP response)

## License

MIT
