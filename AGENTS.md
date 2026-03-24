# AGENTS

This repository defines a filesystem-driven HTTP server protocol and maintains
multiple implementations of the same behavior.

## Source of truth

- The authoritative behavior is the protocol spec in `spec/PROTOCOL.md`.
- If an implementation disagrees with the spec, the implementation is wrong.
- The shared compliance suite lives in `spec/test-suite/`.

When changing behavior:

1. Update `spec/PROTOCOL.md` first.
2. Update or add compliance tests in `spec/test-suite/tests/` when the behavior
   is part of base conformance.
3. Update affected implementations.
4. Update the top-level `README.md` and any implementation-specific README files
   whose usage or guarantees changed.

Optional capabilities should be documented clearly as optional and should not be
described as part of base conformance unless the compliance suite is updated to
test them.

## Repo map

- `spec/PROTOCOL.md`: protocol and conformance contract
- `spec/test-suite/`: shared behavioral tests
- `README.md`: repository-level overview and run instructions
- `tools/fsrouter-watch.py`: wrapper that provides automatic route reloads
- `go/`, `python/`, `rust/`, `bash/`, `deno/`, `ruby/`, `perl/`, `lua/`,
  `java/`, `groovy/`: language-specific implementations and docs

## Common commands

Run the default compliance suite from the repo root:

```bash
python3 spec/test-suite/run.py
```

Run a specific implementation:

```bash
FSROUTER_IMPL=python python3 spec/test-suite/run.py
FSROUTER_IMPL=bash python3 spec/test-suite/run.py
FSROUTER_IMPL=rust python3 spec/test-suite/run.py
```

Quick syntax check for the hot-reload wrapper:

```bash
python3 -m py_compile tools/fsrouter-watch.py
```

## Editing guidance

- Prefer editing source files, not generated artifacts.
- Do not hand-edit compiled `.class` files or built binaries such as
  `go/fsrouter`; change the corresponding source instead.
- Keep changes narrowly scoped to the behavior being modified.
- Preserve the repository's existing plain, dependency-light style.
- When adding cross-cutting features, think about all implementations, not just
  one.

## Read before making assumptions

- `LISTEN_ADDR` semantics, loopback vs wildcard binds, and optional automatic
  route reloading are defined in `spec/PROTOCOL.md`.
- The base compliance suite currently covers required behavior, not every
  optional capability.
- Some files in the tree are checked-in build outputs. Treat them as artifacts
  unless the task explicitly involves them.
