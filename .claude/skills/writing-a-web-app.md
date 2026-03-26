# Writing a Web App for fsrouter

Use this skill when the user asks to create a web app, API, or service that
will be served by fsrouter. This covers building route directories, writing
handlers, serving static files, and structuring applications so they work
with any conforming fsrouter server implementation.

---

## Core Concept

Your directory tree **is** your router. There is no routing config, no
annotations, no code-level route registration. Every executable file in
`ROUTE_DIR` (default: `./routes`) is a handler at the URL matching its
filesystem path.

The simplest possible web app is a single executable file:

```
routes/
  hello               # executable → handles ALL methods at /hello
```

Method files (files named `GET`, `POST`, etc.) are the exception — use them
only when you need per-method dispatch at a single path.

---

## Quick Start

1. Create a route directory with handlers:

```
routes/
  hello               # handles /hello (all methods)
  health              # handles /health (all methods)
  users/
    GET                # handles GET /users specifically
    POST               # handles POST /users specifically
    :id/
      profile          # handles /users/:id/profile (all methods)
```

2. Make handlers executable and add a shebang:

```bash
chmod +x routes/hello routes/health routes/users/GET routes/users/POST
chmod +x routes/users/:id/profile
```

3. Start any fsrouter server:

```bash
ROUTE_DIR=./routes python3 python/fsrouter.py
# or: ROUTE_DIR=./routes go/fsrouter
# or: ROUTE_DIR=./routes bash bash/fsrouter.sh
# or any other implementation
```

---

## Two Kinds of Handlers

### Implicit Handlers (the default)

An executable file whose name is **not** an HTTP method handles **all
methods** at its URL path. The filename becomes part of the URL.

```
routes/health         # /health    — handles GET, POST, PUT, etc.
routes/api/status     # /api/status — handles all methods
```

The handler receives `REQUEST_METHOD` in its environment and can
differentiate if needed:

```bash
#!/bin/sh
echo "{\"method\": \"$REQUEST_METHOD\"}"
```

**Use implicit handlers when:**
- The endpoint only needs one behavior (most endpoints)
- You want the simplest possible structure
- Method dispatch isn't important

### Method Files (the exception)

Files named after HTTP methods (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`,
`HEAD`, `OPTIONS`) handle only that specific method. The filename is consumed
as routing metadata — it doesn't appear in the URL.

```
routes/users/
  GET                 # GET /users only
  POST                # POST /users only
```

Unhandled methods at a path with method files return **405 Method Not
Allowed**.

**Use method files when:**
- Different methods need completely different logic
- You want the server to enforce allowed methods with 405 responses

---

## Writing Handlers

### Executable Handlers (Dynamic Endpoints)

**Minimal handler** (`routes/hello`):

```bash
#!/bin/sh
echo '{"message": "hello world"}'
```

**Python handler** (`routes/users/GET`):

```python
#!/usr/bin/env python3
import json
print(json.dumps({"users": ["alice", "bob"]}))
```

**Rules:**
- Shebang is required — the server executes the file directly, not via a shell
- No command-line arguments are passed; all data comes via env vars and stdin
- stdout becomes the response body (default Content-Type: `application/json`)
- stderr is logged server-side (used as response body only if stdout is empty
  and exit code is non-zero)
- Exit code determines HTTP status: `0` → 200, `1` → 400, `2+` → 500
- Working directory is set to the handler file's parent directory

### Static Handlers (Fixed Responses)

A method file **without** the execute bit is served directly as a static file.
Content-Type is detected from the file extension. Useful for mock endpoints or
fixed responses.

```
routes/health/GET     # contains: {"status": "ok"}  (not executable)
```

---

## Receiving Request Data

### Path Parameters

Directories prefixed with `:` capture URL segments as environment variables:

```
routes/users/:id/profile      # PARAM_ID=42       for /users/42/profile
routes/hosts/:hostname/GET    # PARAM_HOSTNAME=sw1 for GET /hosts/sw1
```

Access in a handler:

```bash
#!/bin/sh
echo "{\"user_id\": \"$PARAM_ID\"}"
```

```python
#!/usr/bin/env python3
import os, json
print(json.dumps({"user_id": os.environ["PARAM_ID"]}))
```

Naming rules:
- `:id` → `PARAM_ID` (uppercased)
- `:run-id` → `PARAM_RUN_ID` (hyphens become underscores)

### Query Parameters

Query string keys are exposed as `QUERY_*` environment variables:

```
GET /users?status=active&limit=10
  → QUERY_STATUS=active
  → QUERY_LIMIT=10
```

If a key appears multiple times, only the first value is captured. Parse
`QUERY_STRING` directly if you need all values.

### Request Body

The request body is piped to the handler's stdin:

```python
#!/usr/bin/env python3
import sys, json
body = json.load(sys.stdin)
# process body...
print(json.dumps({"received": body}))
```

### Request Headers

All HTTP headers are available as `HTTP_*` variables:

```
Authorization: Bearer tok_123  → HTTP_AUTHORIZATION=Bearer tok_123
X-Request-Id: abc              → HTTP_X_REQUEST_ID=abc
```

Exception: `Content-Type` and `Content-Length` are available only as
`CONTENT_TYPE` and `CONTENT_LENGTH` (not duplicated as `HTTP_*`).

### Other Request Metadata

| Variable | Example |
|---|---|
| `REQUEST_METHOD` | `POST` |
| `REQUEST_URI` | `/api/users?active=true` |
| `REQUEST_PATH` | `/api/users` |
| `QUERY_STRING` | `active=true&limit=10` |
| `REMOTE_ADDR` | `127.0.0.1:52431` |
| `SERVER_NAME` | `localhost` |
| `SERVER_PORT` | `8080` |

---

## Returning Responses

### Success

Write the response body to stdout and exit with code 0:

```bash
#!/bin/sh
echo '{"created": true}'
```

### Client Error (400)

Exit with code 1:

```bash
#!/bin/sh
echo '{"error": "name is required"}' >&2
exit 1
```

### Server Error (500)

Exit with code 2 or higher:

```python
#!/usr/bin/env python3
import sys
print('{"error": "database unavailable"}', file=sys.stderr)
sys.exit(2)
```

Note: stderr is used as the response body only when stdout is empty and the
exit code is non-zero.

---

## Static Files and Filesystem Fallback

Non-executable, non-method files in the route directory are served at their
corresponding URL path via filesystem fallback:

```
routes/
  assets/
    style.css          # GET /assets/style.css → serves CSS
    logo.png           # GET /assets/logo.png  → serves PNG
  docs/
    index.html         # GET /docs/ → serves index.html
    guide.html         # GET /docs/guide.html → serves HTML
```

Directory index resolution order:
1. `index.html` → serve it
2. `index.htm` → serve it
3. Executable `index.*` → run the lexicographically first one
4. No index → return an HTML directory listing

---

## Routing Priority

1. **Literal segments beat parameter segments.** If both `routes/users/me/GET`
   and `routes/users/:id/GET` exist, `GET /users/me` always hits the literal
   `me` directory.

2. **Method files beat implicit handlers.** If `routes/items/GET` exists as a
   method file, `GET /items` dispatches to it. Other methods at that path
   return 405 (because method files claim the path).

3. **Registered routes beat filesystem fallback.** Implicit handlers and
   method files are registered at startup. Filesystem fallback is a last
   resort for non-executable files.

---

## Structuring Larger Apps

### Shared Logic

Put shared code in non-executable files (e.g., `lib/`). Since the handler's
working directory is its parent, use relative paths to find shared modules:

```
routes/
  lib/
    db.py
    helpers.py
  users               # implicit handler for /users
  orders/
    GET
    :id/
      GET
```

Handler importing shared code:

```python
#!/usr/bin/env python3
from pathlib import Path
import sys

# Find lib relative to this handler
lib = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(lib))

from db import get_users
import json
print(json.dumps(get_users()))
```

### Configuration via Environment

Handlers inherit the server's environment. Pass config through env vars:

```bash
DATABASE_URL=postgres://... API_KEY=sk-... ROUTE_DIR=./routes python3 python/fsrouter.py
```

Handlers read these directly — no framework needed.

### Data and Templates

Store templates, prompts, data files, and other assets alongside handlers.
They are accessible both via filesystem (from handlers using relative paths)
and via HTTP (through filesystem fallback):

```
routes/
  templates/
    email.html        # usable by handlers AND served at GET /templates/email.html
  data/
    config.json       # usable by handlers AND served at GET /data/config.json
```

---

## Server Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ROUTE_DIR` | `./routes` | Root of the route directory tree |
| `LISTEN_ADDR` | `:8080` | Bind address (`host:port` or `:port`) |
| `COMMAND_TIMEOUT` | `30` | Handler timeout in seconds |

---

## Complete Example: A Todo API

```
routes/
  todos               # List todos (GET) or create (POST) — implicit handler
  todos/
    :id/
      todo            # Get, update, or delete a specific todo
  lib/
    store.py          # Shared storage logic
```

`routes/todos`:

```python
#!/usr/bin/env python3
from pathlib import Path
import os, sys, json

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from store import list_todos, create_todo

method = os.environ["REQUEST_METHOD"]

if method == "GET":
    print(json.dumps(list_todos()))
elif method == "POST":
    body = json.load(sys.stdin)
    if "title" not in body:
        print(json.dumps({"error": "title required"}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(create_todo(body["title"])))
else:
    print(json.dumps({"error": "not supported"}), file=sys.stderr)
    sys.exit(1)
```

`routes/todos/:id/todo`:

```python
#!/usr/bin/env python3
from pathlib import Path
import os, sys, json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))
from store import get_todo, delete_todo

todo_id = os.environ["PARAM_ID"]
method = os.environ["REQUEST_METHOD"]

if method == "GET":
    todo = get_todo(todo_id)
    if not todo:
        print(json.dumps({"error": "not found"}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(todo))
elif method == "DELETE":
    if not delete_todo(todo_id):
        print(json.dumps({"error": "not found"}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"deleted": todo_id}))
```

Or use method files when per-method dispatch is cleaner:

```
routes/
  todos/
    GET              # List all todos
    POST             # Create a todo
    :id/
      GET            # Get one todo
      DELETE          # Delete a todo
  lib/
    store.py
```

Both structures work. Choose whichever is simpler for your use case.

---

## Checklist

Before running your app, verify:

- [ ] Every handler file has a shebang line (`#!/bin/sh`, `#!/usr/bin/env python3`, etc.)
- [ ] Every handler file is executable (`chmod +x`)
- [ ] Method files (if used) are named with uppercase HTTP methods (`GET`, `POST`, etc.)
- [ ] Parameter directories start with `:` (e.g., `:id`, `:name`)
- [ ] Handlers write JSON to stdout (the default Content-Type)
- [ ] Error responses use exit code 1 (client error) or 2+ (server error)
- [ ] No routing config exists outside the directory tree
- [ ] The app works the same regardless of which fsrouter server runs it
