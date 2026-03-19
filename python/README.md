# fsrouter (Python)

This is a dependency-free Python implementation of `fsrouter`.

It mirrors the repository's other implementations:

- route discovery from a directory tree
- literal and parameter path matching
- executable handler subprocesses
- static method-file responses
- CGI-style response headers
- request metadata exposed as environment variables
- per-handler execution timeouts

The authoritative protocol reference is `../spec/PROTOCOL.md`.

## Run

```bash
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 python3 fsrouter.py
```

## Quick start

```bash
mkdir -p routes/hello
cat > routes/hello/GET <<'EOF'
#!/bin/sh
printf '{"message":"hello world"}'
EOF
chmod +x routes/hello/GET
python3 fsrouter.py
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
