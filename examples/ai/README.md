# AI Change Assistant Example

This example is a filesystem-routed web app that accepts change requests and
uses OpenRouter to:

1. Check whether the requested change is already satisfied.
2. Generate a validation command from the request, enforce a strict command
   allowlist, risk-score it, and require the preflight check to fail before any
   edits are attempted.
3. Gather filesystem context as file references with line ranges.
4. Show the captured context with links back to the underlying files.
5. Ask an AI model to plan and generate the change.
6. Decompose the work recursively when the model says the change is safer in
   smaller parts.
7. Review the generated edits before applying them.
8. Apply the changes to the filesystem.
9. Re-run the generated validation command.
10. Show a linked list of the resulting diffs.
11. Suggest likely follow-up requests with prefilled links back to the form.

It also includes a gallery of starter change requests so users can browse
examples, learn what kinds of changes the app can make, and prefill the form
with a customizable starting point.

## Run

From the repository root:

```bash
export OPENROUTER_API_KEY=your_key_here
ROUTE_DIR=examples/ai/routes python3 python/fsrouter.py
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
- Prompt templates live in `examples/ai/prompts/` as plain text files so they
  can be diffed and versioned independently.
- Validation command generation uses up to three AI attempts, each attempt
  consumes budget, and each candidate must pass allowlist and risk checks
  before preflight execution.
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
