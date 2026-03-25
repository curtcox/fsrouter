# fsrouter

`fsrouter` is a filesystem-driven HTTP server: your directory tree defines your API.

The core idea is a 1-to-1 correspondence between files and URLs. You create a
file at a path, and it becomes reachable at the matching URL — no routing
configuration, no DSL, no annotations. The filesystem *is* the router. If you
need behavior beyond what the directory tree provides, you can add executable
handler scripts, but the directory structure remains the single source of truth
for what URLs exist.

This repository contains a protocol/specification plus multiple implementations of the same server.

## Repository layout

```text
.
├── bash/
├── deno/
├── examples/
├── groovy/
├── LICENSE.md
├── lua/
├── perl/
├── README.md
├── go/
├── java/
├── python/
├── ruby/
├── rust/
├── tools/
└── spec/
```

The `examples/` tree contains runnable sample apps built on top of fsrouter.

## What fsrouter does

Each implementation scans a route directory at startup and turns it into an HTTP routing table.

At its core:

- Directories become URL path segments.
- Files named `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, and `OPTIONS` become handlers.

Additionally:

- Directories beginning with `:` capture path parameters.
- Executable handler files run as subprocesses; non-executable files are served as static content.
- Any other file in the tree is served at its matching URL path.

Example:

```text
routes/
  users/
    GET
    POST
    :id/
      GET
      DELETE
```

Registers:

```text
GET     /users
POST    /users
GET     /users/:id
DELETE  /users/:id
```

## Implementations

### Go

Located in `go/`.

- Build:

```bash
cd go && go build -o fsrouter .
```

- Docs:
  - `go/README.md`

### Bash

Located in `bash/`.

- Run:

```bash
cd bash && bash fsrouter.sh
```

- Open on macOS:

```bash
cd bash && bash open.sh <file-or-url>
```

- Docs:
  - `bash/README.md`

### Deno

Located in `deno/`.

- Run:

```bash
cd deno && deno run --allow-net --allow-read --allow-run --allow-env fsrouter.ts
```

- Docs:
  - `deno/README.md`

### Groovy

Located in `groovy/`.

- Run:

```bash
cd groovy && groovy fsrouter.groovy
```

- Docs:
  - `groovy/README.md`

### Lua

Located in `lua/`.

- Dependencies:
  - `LuaSocket`
  - `LuaSystem`

- Run:

```bash
cd lua && lua fsrouter.lua
```

- Docs:
  - `lua/README.md`

### Perl

Located in `perl/`.

- Run:

```bash
cd perl && perl fsrouter.pl
```

- Docs:
  - `perl/README.md`

### Java

Located in `java/`.

- Build:

```bash
cd java && javac FSRouter.java
```

- Run:

```bash
cd java && java FSRouter
```

- Docs:
  - `java/README.md`

### Rust

Located in `rust/`.

- Build:

```bash
cd rust && cargo build --release
```

- Docs:
  - `rust/README.md`

### Python

Located in `python/`.

- Run:

```bash
cd python && python3 fsrouter.py
```

- Docs:
  - `python/README.md`

## Examples

### AI change assistant

Located in `examples/ai/`.

This example demonstrates the intended usage pattern: the route directory
(`examples/ai`) is served directly, so ordinary files are reachable at their
corresponding URL paths. Custom executable handlers are added only for derived
views (workflow state, snapshots, diffs) that don't correspond to static files.

- Run:

```bash
export OPENROUTER_API_KEY=your_key_here
ROUTE_DIR=examples/ai python3 python/fsrouter.py
```

- Docs:
  - `examples/ai/README.md`

### Ruby

Located in `ruby/`.

- Run:

```bash
cd ruby && ruby fsrouter.rb
```

- Docs:
  - `ruby/README.md`

## Protocol and compliance

The authoritative behavior lives in:

- `spec/PROTOCOL.md`

The shared compliance suite lives in:

- `spec/test-suite/`

Run it from the repository root:

### Test the Go implementation

```bash
python3 spec/test-suite/run.py
```

### Test the Bash implementation

```bash
FSROUTER_IMPL=bash python3 spec/test-suite/run.py
```

### Test the Deno implementation

```bash
FSROUTER_IMPL=deno python3 spec/test-suite/run.py
```

### Test the Groovy implementation

```bash
FSROUTER_IMPL=groovy python3 spec/test-suite/run.py
```

### Test the Lua implementation

```bash
FSROUTER_IMPL=lua python3 spec/test-suite/run.py
```

### Test the Perl implementation

```bash
FSROUTER_IMPL=perl python3 spec/test-suite/run.py
```

### Test the Rust implementation

```bash
FSROUTER_IMPL=rust python3 spec/test-suite/run.py
```

### Test the Python implementation

```bash
FSROUTER_IMPL=python python3 spec/test-suite/run.py
```

### Test the Ruby implementation

```bash
FSROUTER_IMPL=ruby python3 spec/test-suite/run.py
```

## Configuration

All implementations use the same core environment variables:

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`

Typical usage:

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30
```

`LISTEN_ADDR` controls whether the server accepts only local requests or
requests from beyond the local machine:

- `127.0.0.1:8080` or `[::1]:8080` for local-only access
- `:8080`, `0.0.0.0:8080`, or `[::]:8080` for access beyond the local machine

The repository also includes `tools/fsrouter-watch.py`, a lightweight wrapper
that provides automatic route reloads by restarting the chosen implementation
when route-discovery-relevant files change.

## Run modes

The examples below use the Python implementation because it runs directly:

```bash
python3 python/fsrouter.py
```

Replace that command with any implementation command from the sections above if
you prefer a different runtime.

### Local-only access, no hot reload

```bash
ROUTE_DIR=./routes LISTEN_ADDR=127.0.0.1:8080 COMMAND_TIMEOUT=30 \
python3 python/fsrouter.py
```

### Access beyond the local machine, no hot reload

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 \
python3 python/fsrouter.py
```

If you use a non-loopback bind, make sure your firewall and network policy allow
the port you chose.

### Local-only access, with hot reload

```bash
ROUTE_DIR=./routes LISTEN_ADDR=127.0.0.1:8080 COMMAND_TIMEOUT=30 \
python3 tools/fsrouter-watch.py -- python3 python/fsrouter.py
```

### Access beyond the local machine, with hot reload

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 \
python3 tools/fsrouter-watch.py -- python3 python/fsrouter.py
```

The watcher polls `ROUTE_DIR` and restarts the server automatically when the
discovered route tree changes, so adding, removing, renaming, or reclassifying
handler files does not require a manual restart.

## Project goal

The purpose of this repository is to define `fsrouter` once at the protocol level and validate multiple implementations against the same behavior.
