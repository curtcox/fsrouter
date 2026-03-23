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

## Open on macOS

```bash
bash open.sh <file-or-url>
```

The helper accepts one argument and resolves the route root like this:

- ordinary file: uses the file's parent directory as the route root
- folder: uses the folder itself as the route root
- archive: expands it and uses the extracted root directory
- Git URL: clones or updates the repository and uses the repository root

After resolving the route root, it starts `fsrouter.sh` on a local port in the foreground, streams the server logs to your terminal, opens your browser to the root page, and lets you stop the server with `Ctrl-C`.

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
