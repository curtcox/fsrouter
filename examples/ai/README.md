# AI Change Assistant Example

This example is a filesystem-routed API that accepts change requests and uses
OpenRouter to:

1. Check whether the requested change is already satisfied.
2. Generate a validation command, enforce a strict command allowlist, score
   validation risk, and require the preflight check to fail before edits.
3. Gather filesystem context as file references with line ranges.
4. Plan and strategy-risk-review the implementation before edits.
5. Generate, review, and apply edits.
6. Re-run validation and capture diffs, events, and AI call logs.

## Spec alignment

`fsrouter` v2 executable handlers no longer emit CGI-style response headers.
This example now returns JSON bodies from executable routes and uses exit codes
for status mapping (`0 -> 200`, `1 -> 400`, `2+ -> 500`) per `spec/PROTOCOL.md`.

## URL-to-filesystem principle

This example serves `examples/ai` directly as `ROUTE_DIR`:

- Ordinary files remain directly reachable through filesystem fallback (for
  example `/starter-prompts/...`, `/assets/...`, `/logs/ai/...`).
- Dynamic behavior is exposed mostly through implicit executable handlers
  (`index.py`, `file`, `context`, `diff`, `ai-call`, etc.).
- Method files are used only for `POST` endpoints that share the same URL path
  segment namespace (for example `/changes/:id` actions).

## Run

From the repository root:

```bash
export OPENROUTER_API_KEY=your_key_here
ROUTE_DIR=examples/ai python3 python/fsrouter.py
```

Then call the API root:

```bash
curl -s http://localhost:8080/ | jq .
```

## Main endpoints

- `GET /`
  - Home metadata, models, preferences, starter prompts, and recent changes.
- `POST /changes`
  - Queue a new change request (`description`, `model`, `ai_budget`, optional
    `favorite_model=1`).
- `GET /changes/:id/detail`
  - Full workflow state (`request`, `state`, `result`, `events`, `ai_calls`).
- `POST /changes/:id`
  - Risk-review actions while paused (`action=ignore_risk` or
    `action=revise_strategy` with `strategy_notes`).
- `POST /changes/:id/validation`
  - Manually queue a rerun.
- `POST /changes/:id/recovery`
  - Queue recovery for failed/rolled-back/error states.
- `GET /changes/:id/artifact?kind=...`
  - Fetch stored artifacts (`request`, `state`, `result`, `events`, `ai_calls`,
    `context`, `diff`, `ai_call`).
- `GET /file`, `GET /context`, `GET /diff`, `GET /ai-call`
  - Direct artifact helper views by query parameters.
- `POST /preferences`
  - Favorite model management (`action=add|remove`, `model`).

## Optional environment variables

- `AI_CHANGE_ROOT`
  - Filesystem root to inspect and edit. Defaults to the repository root.
- `AI_CHANGE_COMMAND_TIMEOUT`
  - Timeout for generated validation commands before/after edits. Defaults to
    `120`.
- `OPENROUTER_HTTP_REFERER`
  - Optional referer header for OpenRouter requests.

## Notes

- Runtime state is written under `examples/ai/data/`.
- Full OpenRouter request/response logs are written under `examples/ai/logs/ai/`.
- Prompt templates live in `examples/ai/prompts/` as plain text files.
- Starter prompts live in `examples/ai/starter-prompts/`.
- The change worker runs in a detached subprocess so create/action endpoints can
  return immediately while clients poll `/changes/:id/detail`.
