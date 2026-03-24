# AI Change Assistant Example

This example is a filesystem-routed web app that accepts change requests and
uses OpenRouter to:

1. Check whether the requested change is already satisfied.
2. Gather filesystem context as file references with line ranges.
3. Show the captured context with links back to the underlying files.
4. Ask an AI model to plan and generate the change.
5. Decompose the work recursively when the model says the change is safer in
   smaller parts.
6. Review the generated edits before applying them.
7. Apply the changes to the filesystem.
8. Re-run the supplied validation command.
9. Show a linked list of the resulting diffs.
10. Suggest likely follow-up requests with prefilled links back to the form.

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
  - Timeout for the user-supplied validation command. Defaults to `120`.
- `OPENROUTER_HTTP_REFERER`
  - Optional referer header for OpenRouter requests.

## Notes

- Runtime state is written under `examples/ai/data/`.
- Prompt templates live in `examples/ai/prompts/` as plain text files so they
  can be diffed and versioned independently.
- The change worker runs in a detached subprocess, so the web request can return
  immediately and the change page can poll for progress.
