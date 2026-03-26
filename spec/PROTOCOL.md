# fsrouter Protocol Specification

**Version:** 2.1.0

This document defines the behavior of a conforming fsrouter implementation. It is
the authoritative reference — when an implementation disagrees with this document,
the implementation is wrong.

---

## 1. Overview

fsrouter is a generic HTTP server that derives its route table, method dispatch,
and handler behavior entirely from a directory tree on the local filesystem. The
server has no built-in routes and no routing configuration language. Its only job
is to:

1. Walk a directory tree at startup to discover handlers.
2. Match incoming HTTP requests to handlers by path and method.
3. Execute handlers and return their output to the client.

The fundamental design constraint is a direct correspondence between filesystem
paths and URL paths. Every URL the server can handle traces back to a file or
directory at the matching location in the route tree. There is no separate
routing table, configuration file, or code-level route registration — the
directory layout is the only routing mechanism.

The rest of this document specifies exactly how each of those steps works.

---

## 2. Definitions

**Route directory**: The root directory the server scans for handlers.
Configured by the `ROUTE_DIR` environment variable (default: `./routes`).

**Segment**: One slash-delimited component of a URL path. The path
`/api/v1/users` has three segments: `api`, `v1`, `users`.

**Literal segment**: A directory whose name does not start with `:`. Matches
one URL segment exactly.

**Parameter segment**: A directory whose name starts with `:`. Matches any
single URL segment and captures its value. The name after the colon is the
parameter name (e.g., `:id` captures to the name `id`).

**Method file**: A file whose name (case-insensitive on discovery, but
conventionally uppercase) is one of the seven standard HTTP methods: `GET`,
`HEAD`, `POST`, `PUT`, `DELETE`, `PATCH`, `OPTIONS`.

**Handler**: A method file that has been matched to an incoming request. A
handler is classified as either *executable* or *static* based on its file
permissions.

**Implicit handler**: An executable file in the route tree whose name is not
an HTTP method. It handles all HTTP methods at the URL path corresponding to
its full filesystem path relative to the route directory. Unlike a method
file, its filename becomes a URL segment rather than being consumed as routing
metadata.

---

## 3. Route Discovery

### 3.1. Scanning

At startup, the server recursively walks the route directory. For every file
encountered, the server checks whether the filename (after uppercasing) is a
recognized HTTP method. If so, it is registered as a method handler. If not,
and the file is executable, it is registered as an implicit handler.
Non-executable, non-method files are not registered during scanning — they
remain reachable through filesystem fallback (§4.4) and may exist alongside
handlers (as templates, helper data, etc.) without affecting routing.

Symlinks are followed. If a method file is a symlink to another file, the
resolved target determines whether it is executable or static.

### 3.2. Route registration

For each discovered method file, the server registers a route by decomposing
the file's path relative to the route directory into segments.

Given `ROUTE_DIR=./routes` and a file at `./routes/api/v1/users/:id/GET`,
the registered route is:

    Method:   GET
    Pattern:  /api/v1/users/:id

Method files are the one place where filesystem paths and URL paths
intentionally differ: the method filename is consumed as routing metadata
rather than appearing in the URL.

For each discovered implicit handler, the server registers a route by
decomposing the file's full path (including filename) relative to the route
directory into segments:

Given `ROUTE_DIR=./routes` and an executable file at `./routes/api/health`,
the registered route is:

    Method:   (all)
    Pattern:  /api/health

Implicit handlers preserve the 1-to-1 correspondence between filesystem
paths and URL paths — the filename appears in the URL, and no directory
wrapper is needed. Every file in the route directory is reachable at exactly
its filesystem path relative to `ROUTE_DIR`.

### 3.3. Startup logging

After scanning, the server MUST log every registered route to stderr in the
format:

    <METHOD>  <PATTERN>  →  <ABSOLUTE_FILE_PATH>  [<TYPE>]

where `<METHOD>` is the HTTP method for method handlers or `*` for implicit
handlers, and `<TYPE>` is `exec` if the file is executable, `static`
otherwise. Routes SHOULD be logged in sorted order (by pattern, then method)
for readability.

### 3.4. Errors during scanning

If the route directory does not exist or is not readable, the server MUST exit
immediately with a non-zero status and log a diagnostic message.

If individual files within the tree are unreadable, the server SHOULD log a
warning and skip them rather than failing to start.

---

## 4. Request Matching

### 4.1. Path normalization

Before matching, the server normalizes the request path:

1. Strip trailing slashes (except for the root path `/`).
2. Collapse consecutive slashes (`//` → `/`).
3. Decode percent-encoded characters per RFC 3986.
4. Reject paths containing `..` segments with 400 Bad Request.

### 4.2. Segment matching

The server matches request path segments against the route tree one level at a
time, starting from the root. At each level:

1. If a **literal child** matches the current segment exactly, descend into it.
2. Otherwise, if a **parameter child** exists, descend into it and record the
   captured value.
3. Otherwise, matching fails.

**Literal segments always take priority over parameter segments.** This is not
configurable.

### 4.3. Method matching

After path matching succeeds, the server checks what handlers the matched
node has, in this order:

1. If the node has a **method-specific handler** for the request's HTTP
   method, dispatch to that handler.
2. If the node has an **implicit handler**, dispatch to it. The implicit
   handler receives `REQUEST_METHOD` in its environment and may use it to
   differentiate methods if needed.
3. If the node has method-specific handlers for *other* methods (but not
   the requested one, and no implicit handler), return **405 Method Not
   Allowed** with an `Allow` header listing the available methods. The
   presence of any method file at a node claims that path for handler
   routing — filesystem fallback (§4.4) is not consulted, even for methods
   that have no handler at that node.
4. If path matching itself failed (no node matched), proceed to
   **§4.4 Filesystem Fallback**.

### 4.4. Filesystem Fallback

This mechanism ensures that any regular file in the route directory is reachable
at its corresponding URL path, preserving the 1-to-1 mapping between filesystem
paths and URLs even for files that are not method handlers. Handler routes
(method files) take priority when both exist for the same path.

When no handler route matches the request path (step 3 of §4.2 fails at any
level, or the matched node has no handler files at all), the server attempts to
serve the path directly from the filesystem before returning 404.

1. Construct the candidate path by joining `ROUTE_DIR` with the normalized
   path segments. Because `..` segments are already rejected (§4.1), the
   result is guaranteed to remain within `ROUTE_DIR`.
2. **Regular file** — if the candidate path is a regular file:
   - If the file is not executable, serve it:
     - Status: `200 OK`
     - `Content-Type`: detected from the file extension (same logic as §5.2).
     - Body: the raw file contents.
   - If the file is executable, execute it using the same contract as
     executable handlers (§5.3). The subprocess working directory, invocation,
     environment, stdin delivery, timeout handling, stdout interpretation, and
     Content-Type default all follow §5.3 exactly.
3. **Directory** — if the candidate path is a directory:
   1. If `index.html` exists and is a regular file, serve that file exactly as
      in step 2.
   2. Otherwise, if `index.htm` exists and is a regular file, serve that file
      exactly as in step 2.
   3. Otherwise, if one or more regular files whose names match `index.*` are
      present and executable, execute the lexicographically first such file
      exactly as in the executable regular-file branch of step 2.
   4. Otherwise return a directory listing:
      - Status: `200 OK`
      - `Content-Type: text/html; charset=utf-8`
      - Body: a simple HTML directory listing with hyperlinks to each entry.
        Subdirectories are shown with a trailing `/`.
4. **Not found** — otherwise return **404 Not Found** (§7.1).

The filesystem fallback is intentionally a last resort. Handler routes always
take priority. The 405 response is returned before this fallback is attempted
(if a node matched but lacked the requested method, the server returns 405
without consulting the filesystem).

### 4.5. HEAD requests

If a `HEAD` method file exists, it is used. If not, but a `GET` method file
exists, the server SHOULD handle `HEAD` by dispatching to the `GET` handler
and suppressing the response body (standard HTTP semantics). Implementations
MAY omit this fallback, but it is recommended.

---

## 5. Handler Execution

### 5.1. Classification

A handler is **executable** if any execute permission bit is set on the file
(i.e., `mode & 0111 != 0` on Unix systems). Otherwise it is **static**.

### 5.2. Static handlers

A static handler is served directly to the client using the platform's
equivalent of Go's `http.ServeFile`. The implementation SHOULD support:

- Content-Type detection from the file extension.
- `Last-Modified` / `If-Modified-Since` conditional responses.
- Range requests for partial content.

The HTTP status code for a successful static response is `200` (or `304`/`206`
per HTTP conditional/range semantics).

### 5.3. Executable handlers

An executable handler is invoked as a subprocess. The following subsections
define the subprocess contract precisely.

#### 5.3.1. Invocation

The handler file is executed directly (not via a shell). The operating system
uses the file's shebang line (`#!`) or binary format to determine the
interpreter. The server MUST NOT wrap the invocation in `sh -c` or any other
shell.

The single argument to the executable is... nothing. Handlers receive no
command-line arguments from the server. All request data is conveyed through
environment variables and stdin.

#### 5.3.2. Working directory

The subprocess's working directory is set to the **parent directory** of the
handler file. A handler at `routes/api/reports/GET` runs with cwd
`routes/api/reports/`.

#### 5.3.3. Standard input

The request body, if any, is connected to the subprocess's stdin. For requests
with no body (typically GET, HEAD, DELETE), stdin is empty (immediately
reaches EOF). The server MUST NOT buffer the entire body before starting the
subprocess — it should pipe the body as it arrives when practical.

#### 5.3.4. Standard output

The subprocess writes its response body to stdout. The entire stdout content
is used as the response body with no interpretation or transformation by the
server.

The default `Content-Type` for executable handler output is
`application/json`. The rationale: fsrouter is designed for API servers where
JSON is the overwhelmingly common response format.

#### 5.3.5. Standard error

Stderr output is captured and logged by the server. It is never sent to the
client under normal circumstances.

**Exception:** If the handler exits with a non-zero exit code AND stdout is
completely empty, the server uses stderr as the response body. This allows
handlers to write structured error output to stderr as a fallback.

#### 5.3.6. Exit codes

| Exit code | HTTP status |
|-----------|-------------|
| 0         | 200 OK              |
| 1         | 400 Bad Request     |
| 2+        | 500 Internal Server Error |

#### 5.3.7. Timeout

The server enforces a per-handler execution timeout configured by the
`COMMAND_TIMEOUT` environment variable (default: 30 seconds).

If a handler exceeds the timeout:

1. The server kills the subprocess (SIGKILL or platform equivalent).
2. The server returns **504 Gateway Timeout** to the client.
3. The server logs the timeout event with the handler path and elapsed time.

#### 5.3.8. Execution failures

If the handler file cannot be executed at all (missing interpreter, permission
denied after initial check, binary format error), the server returns
**502 Bad Gateway** and logs the error.

---

## 6. Environment Variables

### 6.1. Server configuration

These variables configure the server itself. They are read once at startup.

| Variable | Default | Description |
|---|---|---|
| `ROUTE_DIR` | `./routes` | Path to the route directory tree |
| `LISTEN_ADDR` | `:8080` | Bind address in `host:port` or `:port` form |
| `COMMAND_TIMEOUT` | `30` | Handler timeout in seconds |

#### 6.1.1. Listen address forms

Implementations MUST accept the following `LISTEN_ADDR` forms:

- `:8080` — bind to port `8080` on all interfaces.
- `127.0.0.1:8080` — bind only to the IPv4 loopback interface.
- `0.0.0.0:8080` — bind to all IPv4 interfaces explicitly.
- `[::1]:8080` — bind only to the IPv6 loopback interface.
- `[::]:8080` — bind to all IPv6 interfaces explicitly.

More generally:

- `host:port` binds to the specified host and port.
- `:port` is shorthand for a wildcard bind on that port.
- `[ipv6-literal]:port` is the required bracketed form for IPv6 literals.

For this specification, "all interfaces" means the wildcard or unspecified
address for the chosen socket family. A server started with `:8080`,
`0.0.0.0:8080`, or `[::]:8080` is intended to accept requests from beyond the
local machine, subject to the operating system, firewall, and network policy.
A server started with `127.0.0.1:8080` or `[::1]:8080` is intended to accept
requests only from the local machine.

### 6.2. Request variables

These variables are set for every handler subprocess invocation. They do not
exist in the server's own environment — they are constructed per request.

#### 6.2.1. Standard request metadata

| Variable | Description | Example |
|---|---|---|
| `REQUEST_METHOD` | HTTP method | `POST` |
| `REQUEST_URI` | Full request URI including query string | `/api/users?active=true` |
| `REQUEST_PATH` | Path component only (no query string) | `/api/users` |
| `QUERY_STRING` | Raw query string (without leading `?`) | `active=true&limit=10` |
| `CONTENT_TYPE` | Value of the Content-Type request header | `application/json` |
| `CONTENT_LENGTH` | Value of the Content-Length request header | `47` |
| `REMOTE_ADDR` | Client address as `ip:port` | `127.0.0.1:52431` |
| `SERVER_NAME` | Hostname the server is listening on | `localhost` |
| `SERVER_PORT` | Port the server is listening on | `8080` |

`CONTENT_TYPE` and `CONTENT_LENGTH` are set to the empty string if the
corresponding request headers are absent.

#### 6.2.2. Path parameters

For each parameter segment matched during routing, the server sets:

    PARAM_<NAME>=<value>

where `<NAME>` is the parameter name (from the directory name, minus the
leading colon) converted to uppercase. Hyphens in the parameter name are
converted to underscores.

| Directory name | Variable | Example value |
|---|---|---|
| `:id` | `PARAM_ID` | `42` |
| `:hostname` | `PARAM_HOSTNAME` | `R01-SW01` |
| `:run_id` | `PARAM_RUN_ID` | `val-20260318-001` |
| `:run-id` | `PARAM_RUN_ID` | `val-20260318-001` |

Path parameter values are the raw URL-decoded matched segment. No further
transformation is applied.

#### 6.2.3. Query parameters

For each unique query parameter key, the server sets:

    QUERY_<KEY>=<first_value>

where `<KEY>` is the parameter name converted to uppercase, with hyphens
replaced by underscores. If a key appears multiple times in the query string,
only the first value is used. Handlers that need all values should parse
`QUERY_STRING` directly.

| Query string | Variable | Value |
|---|---|---|
| `?status=active` | `QUERY_STATUS` | `active` |
| `?rack=R01` | `QUERY_RACK` | `R01` |
| `?per-page=20` | `QUERY_PER_PAGE` | `20` |
| `?x=1&x=2` | `QUERY_X` | `1` |

#### 6.2.4. Request headers

All HTTP request headers are forwarded as environment variables:

    HTTP_<NAME>=<first_value>

where `<NAME>` is the header name converted to uppercase with hyphens replaced
by underscores.

| Header | Variable |
|---|---|
| `Authorization: Bearer tok_123` | `HTTP_AUTHORIZATION=Bearer tok_123` |
| `Accept: text/html` | `HTTP_ACCEPT=text/html` |
| `X-Request-Id: abc-123` | `HTTP_X_REQUEST_ID=abc-123` |

**Exception:** `Content-Type` and `Content-Length` are not duplicated as
`HTTP_CONTENT_TYPE` / `HTTP_CONTENT_LENGTH`. They are available only as
`CONTENT_TYPE` and `CONTENT_LENGTH` (per CGI convention).

#### 6.2.5. Inherited environment

The subprocess inherits the server's own environment. Request-specific
variables (Sections 6.2.1–6.2.4) are appended. If a request variable collides
with a server environment variable, the request variable wins (it appears
later in the environment list, and most implementations use last-write-wins
semantics).

This means handlers have access to any environment the server was started
with (e.g., `DATABASE_URL`, `RVC_COMMAND_DIR`, etc.) without the server
needing to know about them.

---

## 7. Error Responses

The server produces its own error responses for routing and execution failures.
These are always JSON.

### 7.1. 404 Not Found

```json
{"error": "not_found", "path": "/no/such/route"}
```

### 7.2. 405 Method Not Allowed

```json
{"error": "method_not_allowed", "allow": ["GET", "POST"]}
```

The response includes an `Allow` header per RFC 9110.

### 7.3. 502 Bad Gateway

```json
{"error": "exec_failed", "message": "<OS error detail>"}
```

Returned when a handler file cannot be executed.

### 7.4. 504 Gateway Timeout

```json
{"error": "handler_timeout", "timeout_seconds": 30}
```

Returned when a handler exceeds `COMMAND_TIMEOUT`.

### 7.5. Content-Type

All server-generated error responses have `Content-Type: application/json`.

---

## 8. Concurrency

The server MUST handle multiple requests concurrently. Each handler invocation
is an independent subprocess — the server does not serialize execution.

There is no built-in limit on concurrent handler processes. Implementations
MAY add a concurrency limit as an optional feature, but it is not required by
this specification.

---

## 9. Graceful Shutdown

On receiving `SIGINT` or `SIGTERM` (or platform equivalent), the server:

1. Stops accepting new connections.
2. Waits for in-flight requests to complete, up to a 5-second deadline.
3. Kills any remaining handler subprocesses.
4. Exits with status 0.

---

## 10. Optional Capabilities

### 10.1. Automatic route reloading

An implementation MAY support automatic reloading of the discovered route tree
after filesystem changes. This capability MAY be implemented either inside the
server process or by a supervising wrapper that restarts the server
automatically.

If an implementation or wrapper advertises automatic reloading, it MUST:

1. Detect changes that would alter the startup-discovered route tree, without
   requiring manual operator action.
2. Re-run route discovery after those changes.
3. Apply the updated route tree to subsequent requests.

Changes that alter the discovered route tree include, at minimum:

- Creating, deleting, renaming, or moving method files.
- Changing whether a method file is executable.
- Creating, deleting, renaming, or moving directories or symlinks that affect
  a registered route path.

Implementations MAY watch more broadly and reload on any change beneath
`ROUTE_DIR`, even if some of those changes would not strictly require a reload.

After a successful reload:

- New requests MUST use the newly discovered route tree.
- In-flight requests MAY complete against the previously active route tree.
- The implementation SHOULD log the refreshed route table using the same format
  as startup logging (§3.3).

If a reload attempt fails, the implementation SHOULD log a diagnostic message.
It MAY either continue serving the last known-good route tree or, in a
wrapper-restart design, briefly stop accepting requests until the server is
running again. The important property is that no manual restart is required.

Base conformance does not require automatic reloading. It is an optional,
standardized capability.

---

## 11. Non-Requirements

The following are explicitly out of scope for a conforming implementation:

- **HTTPS / TLS termination.** Use a reverse proxy.
- **WebSocket or streaming responses.** Handler output is buffered.
- **Request body size limits.** Handlers are responsible for their own input
  validation.
- **Authentication, authorization, CORS, rate limiting, or any middleware.**
  These belong in a reverse proxy or in the handler scripts themselves.
- **CGI-style response headers.** The server does not parse handler stdout for
  headers, status codes, or Content-Type overrides. Handlers that need custom
  response metadata should use a reverse proxy or application-level conventions.
- **Response envelope wrapping.** The server does not modify handler output.
  If an application wants `{"status":"ok","data":...}` envelopes, the handlers
  produce them.
- **Logging format configuration.** Log output goes to stderr in an
  implementation-defined format.

---

## 12. Compliance Checklist

An implementation is conforming if it passes all of the following behavioral
tests. Each test corresponds to an entry in `spec/test-suite/tests/`.

The base compliance suite covers the required behavior in Sections 1–9 and
does not currently test the optional capability in Section 10.

| # | Test | Requirement |
|---|------|-------------|
| 1 | Simple GET returns handler stdout as JSON | §5.3.4 |
| 2 | POST body is delivered to handler stdin | §5.3.3 |
| 3 | Path parameters are set in PARAM_* env vars | §6.2.2 |
| 4 | Query parameters are set in QUERY_* env vars | §6.2.3 |
| 5 | Literal segments take priority over parameter segments | §4.2 |
| 6 | Multiple path parameters in one route work | §6.2.2 |
| 7 | Non-zero exit with empty stdout uses stderr as body | §5.3.5 |
| 8 | Handler exceeding timeout returns 504 | §5.3.7 |
| 9 | Valid path with wrong method returns 405 with Allow header | §4.3 |
| 10 | No matching path returns 404 | §4.3 |
| 11 | Non-executable method file is served as static content | §5.2 |
| 12 | All seven HTTP methods are dispatched correctly | §4.3 |
| 13 | Request headers are available as HTTP_* env vars | §6.2.4 |
| 14 | Server environment is inherited by handlers | §6.2.5 |
| 15 | Handler cwd is set to handler's parent directory | §5.3.2 |
| 16 | Trailing slashes are normalized | §4.1 |
| 17 | Hyphens in param/query names become underscores | §6.2.2, §6.2.3 |
| 18 | Arbitrary file at ROUTE_DIR/<path> is served directly (§4.4 fallback) | §4.4 |
| 19 | Directory at ROUTE_DIR/<path> returns HTML listing (§4.4 fallback) | §4.4 |
| 20 | Directory fallback prefers `index.html` over listing | §4.4 |
| 21 | HTML directory index wins over executable `index.*` | §4.4 |
| 22 | Executable `index.*` is run when no HTML index exists | §4.4, §5.3 |
| 23 | Executable `index.*` runs with its parent directory as cwd | §4.4, §5.3.2 |
| 24 | Executable non-method file is registered and run as implicit handler | §3.1, §3.2, §5.3.4 |
| 25 | Implicit handler runs with its parent directory as cwd | §3.2, §5.3.2 |
| 26 | Implicit handler responds to any HTTP method | §4.3 |
| 27 | Implicit handler receives path parameters | §3.2, §6.2.2 |
