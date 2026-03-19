# fsrouter (Rust)

This is a Rust implementation of `fsrouter`, the filesystem-driven HTTP server in this repository.

It mirrors the behavior of the Go implementation in `../go`:

- Route discovery from a directory tree
- Literal and parameter path matching
- Executable handler subprocesses
- Static method-file responses
- CGI-style response headers
- Request metadata exposed as environment variables
- Per-handler execution timeouts

The authoritative protocol reference is `../spec/PROTOCOL.md`.

## Build

```bash
cargo build --release
```

## Run

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 cargo run --release
```

## Quick start

```bash
mkdir -p routes/hello
cat > routes/hello/GET <<'EOF'
#!/bin/sh
printf '{"message":"hello world"}'
EOF
chmod +x routes/hello/GET
cargo run --release
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
