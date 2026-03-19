# fsrouter

A generic HTTP server whose routes, methods, and handlers are defined entirely by a directory tree. No routing config, no framework — your filesystem *is* your API.

## Quick start

```
go build -o fsrouter .
mkdir -p routes/hello
```

Create `routes/hello/GET`:

```bash
#!/bin/sh
echo '{"message": "hello world"}'
```

```
chmod +x routes/hello/GET
./fsrouter
```

```
curl http://localhost:8080/hello
# → {"message": "hello world"}
```

## How routing works

The server scans a directory tree at startup and builds a route table from the structure it finds. Each directory is a URL path segment. Files named after HTTP methods (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`) are handlers for that path and method.

Given this filesystem:

```
routes/
  api/
    users/
      GET               # list users
      POST              # create user
      :id/
        GET             # get one user
        PUT             # update user
        DELETE          # delete user
    health/
      GET               # health check
```

The server registers:

```
GET     /api/users
POST    /api/users
GET     /api/users/:id
PUT     /api/users/:id
DELETE  /api/users/:id
GET     /api/health
```

### Path parameters

Directories whose names start with `:` are wildcards that match any single path segment. The matched value is passed to the handler as an environment variable.

A directory named `:id` makes the matched value available as `PARAM_ID`. A directory named `:hostname` becomes `PARAM_HOSTNAME`. The variable name is always uppercased.

Multiple parameters work naturally:

```
routes/
  projects/
    :project_id/
      tasks/
        :task_id/
          GET
```

A request to `/projects/acme/tasks/42` sets `PARAM_PROJECT_ID=acme` and `PARAM_TASK_ID=42`.

### Matching priority

Literal (exact-match) path segments always take priority over parameter segments at the same level. Given both:

```
routes/
  users/
    me/
      GET             # matches /users/me
    :id/
      GET             # matches /users/alice, /users/42, etc.
```

A request to `/users/me` matches the literal. Everything else falls through to `:id`.

## Handler types

### Executable handlers

If a method file has its execute bit set (`chmod +x`), the server runs it as a subprocess when a matching request arrives. Handlers can be shell scripts, Python scripts, compiled binaries, or anything else that's executable.

The subprocess receives:

| Channel | Content |
|---|---|
| **stdin** | The request body (empty for GET/DELETE) |
| **stdout** | The response sent back to the client |
| **stderr** | Logged server-side; used as response body on error if stdout is empty |
| **cwd** | Set to the handler file's parent directory |
| **env** | Inherits server env plus request variables (see below) |

### Static handlers

If a method file does *not* have its execute bit set, it is served as a static file using Go's built-in `http.ServeFile`. This gives you MIME type detection from the file extension, `If-Modified-Since` / `Last-Modified` handling, and range requests for free.

This is useful for endpoints that return fixed content:

```
routes/
  api/
    schema/
      GET             # a plain JSON file, mode 644, served as-is
```

## Environment variables

Every executable handler receives the following environment variables in addition to the server's own environment.

### Request metadata

| Variable | Example | Description |
|---|---|---|
| `REQUEST_METHOD` | `POST` | HTTP method |
| `REQUEST_URI` | `/api/users?active=true` | Full request URI |
| `REQUEST_PATH` | `/api/users` | Path without query string |
| `QUERY_STRING` | `active=true&limit=10` | Raw query string |
| `CONTENT_TYPE` | `application/json` | Request Content-Type header |
| `CONTENT_LENGTH` | `47` | Request body length |
| `REMOTE_ADDR` | `127.0.0.1:52431` | Client address |
| `SERVER_NAME` | `localhost` | Host the server is bound to |
| `SERVER_PORT` | `8080` | Port the server is bound to |

### Path parameters

Each `:name` segment in the route becomes `PARAM_<NAME>` with the matched value:

| Route directory | URL | Variable |
|---|---|---|
| `:id` | `/users/42` | `PARAM_ID=42` |
| `:hostname` | `/devices/sw01` | `PARAM_HOSTNAME=sw01` |
| `:run_id` | `/runs/run-001` | `PARAM_RUN_ID=run-001` |

### Query parameters

Each query parameter becomes `QUERY_<NAME>` with its first value:

```
GET /api/users?status=active&limit=10
```

Sets `QUERY_STATUS=active` and `QUERY_LIMIT=10`.

### Request headers

All request headers are forwarded as `HTTP_<NAME>` with dashes replaced by underscores:

| Header | Variable |
|---|---|
| `Authorization` | `HTTP_AUTHORIZATION` |
| `Accept` | `HTTP_ACCEPT` |
| `X-Request-Id` | `HTTP_X_REQUEST_ID` |

## Response protocol

### Default behavior (just print your output)

For the simplest case, a handler prints its response to stdout and exits. The server applies these defaults:

| Exit code | HTTP status |
|---|---|
| 0 | 200 OK |
| 1 | 400 Bad Request |
| 2+ | 500 Internal Server Error |

Content-Type defaults to `application/json`.

```bash
#!/bin/sh
# routes/api/time/GET
echo "{\"time\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
```

### CGI-style headers (when you need control)

If a handler needs to set a custom status code, content type, or response headers, it prints CGI-style headers followed by a blank line before the body:

```bash
#!/bin/sh
# routes/api/widgets/POST — return 201 Created
echo "Status: 201"
echo "Content-Type: application/json"
echo "X-Widget-Id: w-12345"
echo ""
echo '{"id": "w-12345", "name": "sprocket"}'
```

Recognized pseudo-headers:

| Header | Effect |
|---|---|
| `Status: <code>` | Sets the HTTP response status code |
| `Content-Type: <type>` | Overrides the default `application/json` |
| Any other `Key: Value` | Set as a response header |

The blank line separating headers from body is required. If the server doesn't detect valid headers at the start of stdout, the entire output is treated as the response body.

### Error handling

If a handler exits non-zero and produces nothing on stdout, the server uses stderr as the response body. This lets handlers write structured error JSON to stderr:

```bash
#!/bin/sh
echo '{"error": "NOT_FOUND", "message": "No such widget"}' >&2
exit 1
```

Stderr is always logged server-side regardless of exit code.

If a handler exceeds the configured timeout, the server kills it and returns `504 Gateway Timeout`.

If a handler can't be executed at all (e.g., missing interpreter), the server returns `502 Bad Gateway`.

## Configuration

All configuration is through environment variables:

| Variable | Default | Description |
|---|---|---|
| `ROUTE_DIR` | `./routes` | Path to the route directory tree |
| `LISTEN_ADDR` | `:8080` | Address and port to bind |
| `COMMAND_TIMEOUT` | `30` | Maximum handler execution time in seconds |

```bash
ROUTE_DIR=/opt/myapp/routes LISTEN_ADDR=:3000 COMMAND_TIMEOUT=60 ./fsrouter
```

## Practical patterns

### Wrapping CLI tools

The most common pattern — a handler that translates request parameters into CLI flags:

```bash
#!/bin/sh
# routes/api/v1/devices/:hostname/ping/POST
exec my-ping-tool --host "$PARAM_HOSTNAME" --timeout "${QUERY_TIMEOUT:-10}"
```

### Reading the request body

POST/PUT handlers receive the body on stdin:

```bash
#!/bin/sh
# routes/api/v1/topology/PUT
cat - | topology-load --file /dev/stdin
```

```python
#!/usr/bin/env python3
# routes/api/v1/data/POST
import sys, json
body = json.load(sys.stdin)
# ... process ...
print(json.dumps({"received": len(body)}))
```

### Serving stored results

For read-only endpoints that return files from a data directory:

```bash
#!/bin/sh
# routes/api/v1/runs/:run_id/devices/:hostname/GET
DATA_DIR="${DATA_DIR:-./data}"
file="$DATA_DIR/runs/$PARAM_RUN_ID/devices/$PARAM_HOSTNAME.json"
if [ -f "$file" ]; then
  cat "$file"
else
  echo '{"error": "NOT_FOUND", "message": "No results for this device"}' >&2
  exit 1
fi
```

### Returning non-JSON content

```bash
#!/bin/sh
# routes/api/v1/runs/:run_id/devices/:hostname/logs/raw/GET
echo "Content-Type: text/plain"
echo ""
cat "data/runs/$PARAM_RUN_ID/devices/$PARAM_HOSTNAME.log"
```

### Using the working directory

Each handler's cwd is set to its own directory, so sibling files are accessible with relative paths:

```
routes/
  api/
    reports/
      GET               # handler
      template.sql      # used by GET
```

```bash
#!/bin/sh
# routes/api/reports/GET
sqlite3 "$DB_PATH" < template.sql
```

## Startup output

On launch, the server logs every discovered route, its method, filesystem path, and whether it's executable or static:

```
2026/03/18 14:00:00 routes from ./routes:
2026/03/18 14:00:00   GET     /api/health                  → /opt/app/routes/api/health/GET [exec]
2026/03/18 14:00:00   GET     /api/users                   → /opt/app/routes/api/users/GET [exec]
2026/03/18 14:00:00   POST    /api/users                   → /opt/app/routes/api/users/POST [exec]
2026/03/18 14:00:00   GET     /api/users/:id               → /opt/app/routes/api/users/:id/GET [exec]
2026/03/18 14:00:00   GET     /api/schema                  → /opt/app/routes/api/schema/GET [static]
2026/03/18 14:00:00 listening on :8080 (timeout 30s)
```

## Signals

The server shuts down gracefully on `SIGINT` or `SIGTERM`, waiting up to 5 seconds for in-flight requests to complete before exiting.

## Limitations

- Routes are built once at startup. Adding or removing handler files requires a restart.
- No built-in middleware (auth, CORS, rate limiting). Add these in a reverse proxy or by wrapping the `ServeHTTP` method.
- No WebSocket or streaming support. Responses are buffered in memory before sending.
- Handler stdout+stderr are held in memory. Handlers that produce very large output (hundreds of MB) should be fronted by a file-based approach instead.