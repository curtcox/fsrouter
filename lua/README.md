# fsrouter (Lua)

This is a single-file Lua implementation of `fsrouter`.

It depends on:

- `LuaSocket`
- `LuaSystem`

It supports the same core protocol behavior as the other implementations:

- route discovery from a directory tree
- literal and parameter path matching
- executable handler subprocesses
- static method-file responses
- CGI-style response headers
- request metadata exposed as environment variables
- per-handler execution timeouts

The authoritative protocol reference is `../spec/PROTOCOL.md`.

## Install

On macOS with Homebrew:

```bash
brew install lua luarocks
luarocks install luasocket
luarocks install luasystem
```

Verify that the required modules are available:

```bash
lua -e "assert(require('socket')); assert(require('system')); print('ok')"
```

If your system has multiple Lua versions installed, make sure `luarocks` installs modules for the same `lua` executable you use to run `fsrouter.lua`.

## Run

```bash
lua fsrouter.lua
```

## Quick start

```bash
mkdir -p routes/hello
cat > routes/hello/GET <<'EOF'
#!/bin/sh
printf '{"message":"hello world"}'
EOF
chmod +x routes/hello/GET
lua fsrouter.lua
```

In another terminal:

```bash
curl http://localhost:8080/hello
```

## Configuration

- `ROUTE_DIR`
- `LISTEN_ADDR`
- `COMMAND_TIMEOUT`
