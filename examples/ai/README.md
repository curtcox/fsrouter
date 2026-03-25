# AI Change Assistant Example

This example is a filesystem-routed web app that accepts change requests and
uses OpenRouter to:

1. Check whether the requested change is already satisfied.
2. Generate a validation command from the request, enforce a strict command
   allowlist, risk-score it, and require the preflight check to fail before any
   edits are attempted.
3. Gather filesystem context as file references with line ranges.
4. Show the captured context with links back to the underlying files.
5. Ask an AI model to plan the change, then run a dedicated strategy risk
   assessment against that plan.
6. Pause and explain the risks whenever the strategy score exceeds the default
   threshold, letting the user continue anyway or retry with a safer strategy.
7. Generate the change after risk review.
8. Decompose the work recursively when the model says the change is safer in
   smaller parts.
9. Review the generated edits before applying them.
10. Apply the changes to the filesystem.
11. Re-run the generated validation command.
12. Show a linked list of the resulting diffs.
13. Suggest likely follow-up requests with prefilled links back to the form.

It also includes a gallery of starter change requests so users can browse
examples, learn what kinds of changes the app can make, and prefill the form
with a customizable starting point.

## URL-to-filesystem principle

This example is meant to follow fsrouter's model as literally as possible:

- Serve `examples/ai` itself as `ROUTE_DIR`.
- Treat ordinary files under `examples/ai/` as directly addressable at the same
  URL path, using fsrouter's normal filesystem fallback.
- Use custom handlers only for derived or dynamic views such as change status,
  stored context snapshots, diffs, AI call summaries, and file slices that do
  not map cleanly to a plain static URL.

## Run

From the repository root:

```bash
export OPENROUTER_API_KEY=your_key_here
ROUTE_DIR=examples/ai python3 python/fsrouter.py
```

Then open [http://localhost:8080](http://localhost:8080).

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
- When you run the example with `ROUTE_DIR=examples/ai`, files under
  `examples/ai/` are directly reachable through fsrouter's normal filesystem
  fallback, including saved AI logs at `/logs/ai/...`.
- Starter prompts are served directly from `/starter-prompts/...`, assets from
  `/assets/...`, and other ordinary example files from matching URL paths.
- Prompt templates live in `examples/ai/prompts/` as plain text files so they
  can be diffed and versioned independently.
- Validation command generation uses up to three AI attempts, each attempt
  consumes budget, and each candidate must pass allowlist and risk checks
  before preflight execution.
- Strategy risk assessment happens after planning. Scores above the default
  threshold pause the workflow until the user explicitly accepts the risk or
  provides a different strategy.
- Starter gallery prompts live in `examples/ai/starter-prompts/` as individual
  plain text files.
- The starter gallery includes simple and advanced examples across CLI, HTML,
  JavaScript, web API, and machine-facing workflows such as QR reading, network
  scanning, and scheduling UI ideas.
- The change worker runs in a detached subprocess, so the web request can return
  immediately and the change page can poll for progress.
- The home page loads the complete current model catalog from
  `https://openrouter.ai/api/v1/models` and presents it as the picker the user
  chooses from.
