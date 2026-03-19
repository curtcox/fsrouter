# fsrouter (Bash)

This is a single-file Bash implementation of `fsrouter`.

It supports the same core protocol behavior as the other implementations:

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
bash fsrouter.sh
```

## Quick start

```bash
mkdir -p routes/hello
printf '#!/bin/sh\nprintf '\''{"message":"hello world"}'\''\n' > routes/hello/GET
chmod +x routes/hello/GET
bash fsrouter.sh
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
