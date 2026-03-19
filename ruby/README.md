# fsrouter (Ruby)

This is a stdlib-only Ruby implementation of `fsrouter`.

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
ROUTE_DIR=./routes LISTEN_ADDR=:8080 COMMAND_TIMEOUT=30 ruby fsrouter.rb
```

## Quick start

```bash
mkdir -p routes/hello
cat > routes/hello/GET <<'EOF'
#!/bin/sh
printf '{"message":"hello world"}'
EOF
chmod +x routes/hello/GET
ruby fsrouter.rb
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
