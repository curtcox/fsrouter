# fsrouter Protocol Specification

**Version:** 1.0.0

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

---

## 3. Route Discovery

### 3.1. Scanning

At startup, the server recursively walks the route directory. For every file
encountered, the server checks whether the filename (after uppercasing) is a
recognized HTTP method. All other files are ignored — they may exist alongside
method files (as templates, helper data, etc.) without affecting routing.

Symlinks are followed. If a method file is a symlink to another file, the
resolved target determines whether it is executable or static.

### 3.2. Route registration

For each discovered method file, the server registers a route by decomposing
the file's path relative to the route directory into segments.

Given `ROUTE_DIR=./routes` and a file at `./routes/api/v1/users/:id/GET`,
the registered route is:

    Method:   GET
    Pattern:  /api/v1/users/:id

### 3.3. Startup logging

After scanning, the server MUST log every registered route to stderr in the
format:

    <METHOD>  <PATTERN>  →  <ABSOLUTE_FILE_PATH>  [<TYPE>]

where `<TYPE>` is `exec` if the file is executable, `static` otherwise. Routes
SHOULD be logged in sorted order (by pattern, then method) for readability.

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

After path matching succeeds, the server checks whether the matched node has a
handler for the request's HTTP method.

- If yes, dispatch to that handler.
- If no, but handlers exist for *other* methods at that node, return
  **405 Method Not Allowed** with an `Allow` header listing the available
  methods.
- If path matching itself failed (no node matched), proceed to **§4.4 Filesystem Fallback**.

### 4.4. Filesystem Fallback

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
   - If the file is executable, execute it:
     - The subprocess working directory is the file's parent directory.
     - Invocation, environment, stdin delivery, and timeout handling follow the
       executable handler rules in §5.3.
     - The response status defaults from the exit code using the same mapping as
       §5.3 (`0 -> 200`, `1 -> 400`, all others -> `500`).
     - `Content-Type: text/plain`
     - Body: the subprocess's stdout exactly, without CGI header parsing.
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
4. **Not found** — otherwise return **404 Not Found** (§8.1).

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

The subprocess writes its response to stdout. The output is interpreted
according to the CGI-style response protocol defined in Section 6.

#### 5.3.5. Standard error

Stderr output is captured and logged by the server. It is never sent to the
client under normal circumstances.

**Exception:** If the handler exits with a non-zero exit code AND stdout is
completely empty, the server uses stderr as the response body. This allows
handlers to write structured error output to stderr as a fallback.

#### 5.3.6. Exit codes

| Exit code | Default HTTP status |
|-----------|---------------------|
| 0         | 200 OK              |
| 1         | 400 Bad Request     |
| 2+        | 500 Internal Server Error |

These defaults apply only when the handler does not emit a `Status` header
(see Section 6). If a `Status` header is present, it overrides the exit code
mapping entirely.

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

## 6. Response Protocol

The response protocol is modeled on CGI/1.1 (RFC 3875) but simplified. There
are two modes: **raw mode** (the default) and **header mode**.

### 6.1. Detection

The server inspects the first line of stdout. If it matches the pattern of an
HTTP header (`Token: Value`, where Token contains no whitespace or control
characters), the server attempts to parse CGI-style headers. Otherwise, the
entire stdout is treated as the response body (raw mode).

### 6.2. Raw mode

The entire stdout content is the response body. The server applies:

- `Content-Type: application/json` (default).
- HTTP status from exit code mapping (Section 5.3.6).

### 6.3. Header mode

The server reads lines from the start of stdout until it encounters a blank
line (a line containing only `\n` or `\r\n`). Each line before the blank line
is parsed as a header. Everything after the blank line is the response body.

If at any point a line does not look like a valid header, the server abandons
header parsing and falls back to raw mode (treating the entire stdout as body).

#### 6.3.1. Recognized headers

| Header | Effect |
|--------|--------|
| `Status: <code>` | Sets the HTTP response status code. The value is the first whitespace-delimited token, parsed as an integer. Any text after the code is ignored (e.g., `Status: 201 Created` sets status 201). |
| `Content-Type: <type>` | Sets the response Content-Type, overriding the default. |
| Any other `Key: Value` | Set as an HTTP response header on the outgoing response. |

#### 6.3.2. Header validity

A header line is valid if:

- It contains a colon (`:`).
- The portion before the colon (the key) is non-empty.
- The key contains no whitespace, control characters, or characters with
  code points below 33 or equal to 127.

### 6.4. Content-Type default

If no `Content-Type` header is emitted by the handler (either in header mode
or because the handler uses raw mode), the server defaults to
`application/json`.

The rationale: fsrouter is designed for API servers where JSON is the
overwhelmingly common response format. Handlers that return other formats
must declare them explicitly.

---

## 7. Environment Variables

### 7.1. Server configuration

These variables configure the server itself. They are read once at startup.

| Variable | Default | Description |
|---|---|---|
| `ROUTE_DIR` | `./routes` | Path to the route directory tree |
| `LISTEN_ADDR` | `:8080` | Bind address in `host:port` or `:port` form |
| `COMMAND_TIMEOUT` | `30` | Handler timeout in seconds |

### 7.2. Request variables

These variables are set for every handler subprocess invocation. They do not
exist in the server's own environment — they are constructed per request.

#### 7.2.1. Standard request metadata

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

#### 7.2.2. Path parameters

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

#### 7.2.3. Query parameters

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

#### 7.2.4. Request headers

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

#### 7.2.5. Inherited environment

The subprocess inherits the server's own environment. Request-specific
variables (Sections 7.2.1–7.2.4) are appended. If a request variable collides
with a server environment variable, the request variable wins (it appears
later in the environment list, and most implementations use last-write-wins
semantics).

This means handlers have access to any environment the server was started
with (e.g., `DATABASE_URL`, `RVC_COMMAND_DIR`, etc.) without the server
needing to know about them.

---

## 8. Error Responses

The server produces its own error responses for routing and execution failures.
These are always JSON.

### 8.1. 404 Not Found

```json
{"error": "not_found", "path": "/no/such/route"}
```

### 8.2. 405 Method Not Allowed

```json
{"error": "method_not_allowed", "allow": ["GET", "POST"]}
```

The response includes an `Allow` header per RFC 9110.

### 8.3. 502 Bad Gateway

```json
{"error": "exec_failed", "message": "<OS error detail>"}
```

Returned when a handler file cannot be executed.

### 8.4. 504 Gateway Timeout

```json
{"error": "handler_timeout", "timeout_seconds": 30}
```

Returned when a handler exceeds `COMMAND_TIMEOUT`.

### 8.5. Content-Type

All server-generated error responses have `Content-Type: application/json`.

---

## 9. Concurrency

The server MUST handle multiple requests concurrently. Each handler invocation
is an independent subprocess — the server does not serialize execution.

There is no built-in limit on concurrent handler processes. Implementations
MAY add a concurrency limit as an optional feature, but it is not required by
this specification.

---

## 10. Graceful Shutdown

On receiving `SIGINT` or `SIGTERM` (or platform equivalent), the server:

1. Stops accepting new connections.
2. Waits for in-flight requests to complete, up to a 5-second deadline.
3. Kills any remaining handler subprocesses.
4. Exits with status 0.

---

## 11. Non-Requirements

The following are explicitly out of scope for a conforming implementation:

- **HTTPS / TLS termination.** Use a reverse proxy.
- **WebSocket or streaming responses.** Handler output is buffered.
- **Hot reloading.** The route tree is built once at startup.
- **Request body size limits.** Handlers are responsible for their own input
  validation.
- **Authentication, authorization, CORS, rate limiting, or any middleware.**
  These belong in a reverse proxy or in the handler scripts themselves.
- **Response envelope wrapping.** The server does not modify handler output.
  If an application wants `{"status":"ok","data":...}` envelopes, the handlers
  produce them.
- **Logging format configuration.** Log output goes to stderr in an
  implementation-defined format.

---

## 12. Compliance Checklist

An implementation is conforming if it passes all of the following behavioral
tests. Each test corresponds to an entry in `spec/test-suite/tests/`.

| # | Test | Requirement |
|---|------|-------------|
| 1 | Simple GET returns handler stdout as JSON | §5, §6.2 |
| 2 | POST body is delivered to handler stdin | §5.3.3 |
| 3 | Path parameters are set in PARAM_* env vars | §7.2.2 |
| 4 | Query parameters are set in QUERY_* env vars | §7.2.3 |
| 5 | Literal segments take priority over parameter segments | §4.2 |
| 6 | Multiple path parameters in one route work | §7.2.2 |
| 7 | Handler can set status code via `Status:` header | §6.3.1 |
| 8 | Handler can set custom response headers | §6.3.1 |
| 9 | Handler can override Content-Type | §6.3.1 |
| 10 | Non-zero exit with empty stdout uses stderr as body | §5.3.5 |
| 11 | Handler exceeding timeout returns 504 | §5.3.7 |
| 12 | Valid path with wrong method returns 405 with Allow header | §4.3 |
| 13 | No matching path returns 404 | §4.3 |
| 14 | Non-executable method file is served as static content | §5.2 |
| 15 | All seven HTTP methods are dispatched correctly | §4.3 |
| 16 | Request headers are available as HTTP_* env vars | §7.2.4 |
| 17 | Server environment is inherited by handlers | §7.2.5 |
| 18 | Handler cwd is set to handler's parent directory | §5.3.2 |
| 19 | Trailing slashes are normalized | §4.1 |
| 20 | Hyphens in param/query names become underscores | §7.2.2, §7.2.3 |
| 21 | Arbitrary file at ROUTE_DIR/<path> is served directly (§4.4 fallback) | §4.4 |
| 22 | Directory at ROUTE_DIR/<path> returns HTML listing (§4.4 fallback) | §4.4 |
| 23 | Directory fallback prefers `index.html` over listing | §4.4 |
| 24 | HTML directory index wins over executable `index.*` | §4.4 |
| 25 | Executable `index.*` is run when no HTML index exists | §4.4, §5.3 |
| 26 | Executable `index.*` runs with its parent directory as cwd | §4.4, §5.3.2 |
| 27 | Executable filesystem fallback file is run and returned as `text/plain` | §4.4 |
| 28 | Executable filesystem fallback file runs with its parent directory as cwd | §4.4 |
