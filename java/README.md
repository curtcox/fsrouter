# fsrouter (Java)

This is a JDK-only Java implementation of `fsrouter`.

It mirrors the repository's other implementations:

- route discovery from a directory tree
- literal and parameter path matching
- executable handler subprocesses
- static method-file responses
- CGI-style response headers
- request metadata exposed as environment variables
- per-handler execution timeouts

The authoritative protocol reference is `../spec/PROTOCOL.md`.

## Build

```bash
javac FSRouter.java
```

## Run

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 java FSRouter
```

## Quick start

```bash
mkdir -p routes/hello
cat > routes/hello/GET <<'EOF'
#!/bin/sh
printf '{"message":"hello world"}'
EOF
chmod +x routes/hello/GET
javac FSRouter.java
java FSRouter
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
