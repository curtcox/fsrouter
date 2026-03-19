# fsrouter

`fsrouter` is a filesystem-driven HTTP server: your directory tree defines your API.

This repository contains a protocol/specification plus multiple implementations of the same server.

## Repository layout

```text
.
├── LICENSE.md
├── README.md
├── go/
├── python/
├── ruby/
├── rust/
└── spec/
```

## What fsrouter does

Each implementation scans a route directory at startup and turns it into an HTTP routing table.

- Directories become URL path segments.
- Files named `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, and `OPTIONS` become handlers.
- Directories beginning with `:` are path parameters.
- Executable handler files run as subprocesses.
- Non-executable handler files are served as static content.

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

All implementations use the same environment variables:

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`

Typical usage:

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30
```

## Project goal

The purpose of this repository is to define `fsrouter` once at the protocol level and validate multiple implementations against the same behavior.
