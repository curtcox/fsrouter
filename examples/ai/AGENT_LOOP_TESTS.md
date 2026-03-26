# Agent Loop — Test Plan

Tests for the agent loop component specified in `AGENT_LOOP.md`.

Sections 1–10 are unit/component tests. They use a mock OpenRouter API (HTTP
stub) and a mock `ask_user` callback. The mock API is configured per-test to
return scripted responses. A fresh temp directory is used for `log_dir`,
`working_dir`, and `data/` on each test.

Section 11 is integration tests. They use a real OpenRouter API and a real
fsrouter server. Each test starts from an empty `working_dir` (containing only
the agent loop's own files) and asks the agent loop to build a complete
application. These tests verify the agent loop can drive real multi-step work
end-to-end.

---

## 1. Happy path

### 1.1 Single-turn answer

- AI returns a valid `{"type": "answer", ...}` on the first turn.
- Assert: result matches the output schema.
- Assert: `request.json` contains one entry.
- Assert: `response.json` contains one entry.
- Assert: `conversation.json` exists and has system message + one assistant message.
- Assert: budget decremented by 1.

### 1.2 Multi-turn with commands then answer

- Turn 1: AI requests `{"type": "command", "commands": [{"command": "echo hello", "purpose": "test"}]}`.
- Turn 2: AI returns a valid answer that references the command output.
- Assert: result matches schema.
- Assert: `commands.json` has one entry with `execution.stdout` = `"hello\n"`.
- Assert: budget decremented by 2 (turn 1 + turn 2; command is cache-miss-safe per mock reviewer, so +1 for review = 3 total).
- Assert: conversation.json shows system → assistant (command) → user (results) → assistant (answer).

### 1.3 Multiple commands in one turn

- AI requests 3 commands in a single turn. All safe (cached).
- Assert: all 3 appear in `commands.json`.
- Assert: all 3 results are returned to the AI in a single user message.
- Assert: budget decremented by 1 (only the AI turn itself; commands hit cache).

### 1.4 Feedback is included in conversation

- Invoke loop with `feedback` containing two items.
- Assert: `feedback.json` is written to `log_dir`.
- Assert: the request sent to the AI includes the feedback items as messages after the system message.

---

## 2. Response validation

### 2.1 Invalid JSON — recovery

- Turn 1: AI returns `"not json"`.
- Turn 2: AI returns valid answer.
- Assert: result is the valid answer.
- Assert: budget decremented by 2.
- Assert: conversation includes the validation error message between turns.

### 2.2 Wrong type field — recovery

- Turn 1: AI returns `{"type": "unknown"}`.
- Turn 2: AI returns valid answer.
- Assert: result is the valid answer.
- Assert: budget decremented by 2.

### 2.3 Answer fails schema — recovery

- Turn 1: AI returns `{"type": "answer", "answer": {"wrong": "shape"}}` (doesn't match output_schema).
- Turn 2: AI returns valid answer.
- Assert: result is the valid answer.
- Assert: the error message sent to the AI references the schema violation.

### 2.4 Command request missing required fields — recovery

- Turn 1: AI returns `{"type": "command", "commands": [{"cmd": "ls"}]}` (missing `command` and `purpose`).
- Turn 2: AI returns valid answer.
- Assert: no commands were executed on turn 1.
- Assert: budget decremented by 2.

### 2.5 Three consecutive validation failures — termination

- Turns 1–3: AI returns invalid JSON each time.
- Assert: result type is `error`.
- Assert: error details mention 3 consecutive validation failures.
- Assert: budget decremented by 3.
- Assert: no commands were executed.

### 2.6 Non-consecutive validation failures do not trigger termination

- Turn 1: AI returns invalid JSON.
- Turn 2: AI returns valid command request. Command runs.
- Turn 3: AI returns invalid JSON.
- Turn 4: AI returns invalid JSON.
- Turn 5: AI returns valid answer.
- Assert: result is the valid answer (consecutive counter reset after turn 2).

---

## 3. Command safety

### 3.1 Cache hit — safe

- Pre-populate `data/safety-cache.json` with `{"pattern": "echo *", "verdict": "safe", ...}`.
- AI requests `echo hello`.
- Assert: command runs without an AI review call.
- Assert: `commands.json` entry has `safety.source` = `"cache"`.
- Assert: no budget spent on review.

### 3.2 Cache hit — rejected

- Pre-populate cache with `{"pattern": "rm *", "verdict": "rejected", ...}`.
- AI requests `rm foo.txt`.
- Assert: command does NOT run (`execution` is null).
- Assert: the rejection is returned to the AI as a command result.
- Assert: no budget spent on review.

### 3.3 Cache miss — review says safe

- Empty cache. Mock review model returns `{"verdict": "safe", "reasoning": "...", "pattern": "ls *"}`.
- AI requests `ls -la`.
- Assert: review AI call is made (budget decremented by 1 for review).
- Assert: command runs.
- Assert: cache now contains an entry for `"ls *"` with verdict `"safe"`.

### 3.4 Cache miss — review says risky — user approves

- Empty cache. Mock review returns `{"verdict": "risky", ...}`. Mock `ask_user` returns "Approve".
- Assert: `ask_user` was called with the command and reasoning.
- Assert: command runs.
- Assert: cache entry added with verdict `"safe"` and source reflecting user approval.
- Assert: `commands.json` entry has `safety.source` = `"user"`.

### 3.5 Cache miss — review says risky — user rejects

- Empty cache. Mock review returns `{"verdict": "risky", ...}`. Mock `ask_user` returns "Reject".
- Assert: command does NOT run.
- Assert: cache entry added with verdict `"rejected"`.
- Assert: the rejection is returned to the AI as a command result.

### 3.6 Cache miss — review says blocked

- Empty cache. Mock review returns `{"verdict": "blocked", "reasoning": "destructive", ...}`.
- Assert: command does NOT run.
- Assert: `ask_user` was NOT called.
- Assert: cache entry added with verdict `"blocked"`.
- Assert: block reason returned to AI.

### 3.7 Review model response fails validation

- Mock review returns invalid JSON on first try, valid on second.
- Assert: review is retried.
- Assert: budget decremented by 2 for the review (original + retry).

### 3.8 Cache pattern wildcarding

- Cache contains `{"pattern": "grep -r *", "verdict": "safe", ...}`.
- AI requests `grep -r 'foo' src/` — should hit cache.
- AI requests `grep -r 'bar' lib/` — should also hit cache.
- AI requests `grep 'baz' file.txt` (no `-r` flag) — should NOT hit cache.
- Assert: first two run without review call; third triggers review.

---

## 4. Command execution

### 4.1 Successful command

- AI requests `echo hello`.
- Assert: `commands.json` entry has `exit_code` = 0, `stdout` = `"hello\n"`, `timed_out` = false.
- Assert: `duration_ms` is a positive number.

### 4.2 Failing command

- AI requests `false` (exits 1).
- Assert: `commands.json` entry has `exit_code` = 1.
- Assert: result is returned to AI (loop does not terminate).

### 4.3 Command with stderr

- AI requests a command that writes to stderr.
- Assert: `commands.json` entry captures both stdout and stderr separately.

### 4.4 Command timeout

- AI requests `sleep 60` (exceeds 30s timeout).
- Assert: `commands.json` entry has `timed_out` = true.
- Assert: timeout info is returned to the AI as a command result.
- Assert: loop continues (AI gets to decide what to do next).

### 4.5 Working directory

- Place a file `marker.txt` in `working_dir`.
- AI requests `cat marker.txt`.
- Assert: command succeeds and stdout contains the file contents.

### 4.6 Command with large output

- AI requests a command producing substantial output (e.g., `seq 10000`).
- Assert: full output is captured in `commands.json`.
- Assert: full output is returned to the AI.

---

## 5. Budget

### 5.1 Budget decrements on each AI call type

- Run a loop that makes: 1 primary call, 1 command safety review, 1 validation retry, then 1 final answer.
- Assert: budget decremented by 4.

### 5.2 Budget exhaustion mid-conversation

- Start with budget = 2. AI requests commands on turn 1 (budget → 1 after turn + 0 after review).
- Assert: result type is `budget_exhausted`.
- Assert: summary includes what was done (turn 1 completed, commands ran).
- Assert: `conversation.json` captures state up to exhaustion.

### 5.3 Budget exhaustion before first call

- Start with budget = 0.
- Assert: result type is `budget_exhausted` immediately.
- Assert: no AI calls made.
- Assert: no log files written for requests/responses.

### 5.4 Budget exhaustion during command safety review

- Budget = 2. Turn 1: AI requests a command (budget → 1). Cache miss triggers review (budget → 0).
- Assert: result type is `budget_exhausted`.
- Assert: the command did NOT run (review couldn't complete or completed but no budget for next turn).

### 5.5 HTTP retries do not consume budget

- Budget = 1. Mock API returns 500 on first attempt, valid answer on retry.
- Assert: result is the valid answer.
- Assert: budget decremented by 1 (not 2).

---

## 6. Logging

### 6.1 Log directory structure after single-turn

- Run single-turn loop (answer on first call).
- Assert: `log_dir/request.json` exists and is valid JSON.
- Assert: `log_dir/response.json` exists and is valid JSON.
- Assert: `log_dir/conversation.json` exists.
- Assert: `log_dir/commands.json` does NOT exist (no commands).
- Assert: `log_dir/feedback.json` does NOT exist (no feedback).

### 6.2 Log directory structure after multi-turn

- Run 3-turn loop (command, command, answer).
- Assert: `request.json` contains 3 entries (or 3 numbered files).
- Assert: `response.json` contains 3 entries (or 3 numbered files).
- Assert: `commands.json` has entries from both command turns.
- Assert: `conversation.json` reflects the full 3-turn conversation.

### 6.3 Feedback logging

- Invoke with non-empty `feedback`.
- Assert: `log_dir/feedback.json` exists and matches the input feedback.

### 6.4 Request body fidelity

- Run a loop. Read `request.json`.
- Assert: it contains the full OpenRouter request body including model, messages, and any schema parameters.
- Assert: the system message matches the rendered prompt template.

### 6.5 Response body fidelity

- Run a loop. Read `response.json`.
- Assert: it contains the raw OpenRouter response including token counts, model info, and the choices array.

### 6.6 Logs written even on error termination

- Trigger 3 consecutive validation failures.
- Assert: `request.json` and `response.json` each have 3 entries.
- Assert: `conversation.json` reflects all 3 failed turns.

### 6.7 Logs written on budget exhaustion

- Trigger budget exhaustion mid-conversation.
- Assert: all files written up to the point of exhaustion.
- Assert: `conversation.json` is complete through the last turn.

---

## 7. Error handling

### 7.1 API HTTP 500 — retries succeed

- Mock returns 500 once, then valid answer.
- Assert: result is the valid answer.
- Assert: budget decremented by 1.

### 7.2 API HTTP 500 — all retries fail

- Mock returns 500 four times (initial + 3 retries).
- Assert: result type is `error`.
- Assert: error includes HTTP status details.
- Assert: budget decremented by 1 (the single attempted call, not retries).

### 7.3 API HTTP 429 (rate limit)

- Mock returns 429 once, then valid answer.
- Assert: retried with backoff.
- Assert: result is the valid answer.

### 7.4 Network error (connection refused)

- Mock is down.
- Assert: retried 3 times.
- Assert: result type is `error` with connection details.

### 7.5 User aborts on risky command

- Mock review says risky. `ask_user` returns "Abort".
- Assert: result type is `user_aborted`.
- Assert: command did not run.
- Assert: all logs written up to that point.

### 7.6 User aborts via "Other" free-text

- `ask_user` returns the "Other" option with text "stop everything".
- Assert: the implementation treats this as abort (or whatever the spec-defined behavior is for unrecognized free-text — verify it's handled, not ignored).

---

## 8. Prompt template

### 8.1 Template variable substitution

- Template contains `{{change_description}}` and `{{context}}`.
- Invoke with matching `template_vars`.
- Assert: the system message in `request.json` has the placeholders replaced.

### 8.2 Missing template variable

- Template contains `{{foo}}`. `template_vars` does not include `foo`.
- Assert: the loop returns `error` (or raises) rather than sending `{{foo}}` literally to the AI.

### 8.3 Template file not found

- `goal` references a nonexistent prompt file.
- Assert: the loop returns `error` with a clear message about the missing file.

### 8.4 Loop appends protocol instructions

- Use a template that does NOT include the command/answer response format instructions.
- Assert: the system message in `request.json` still contains the response format instructions (the loop prepends/appends them per spec section 7).

### 8.5 Output schema included in system message

- Assert: the system message sent to the AI contains the JSON schema definition so the AI knows what shape to produce.

---

## 9. Safety cache persistence

### 9.1 Cache created on first use

- Start with no `data/safety-cache.json`.
- AI requests a command. Review says safe.
- Assert: `data/safety-cache.json` now exists with one entry.

### 9.2 Cache survives across invocations

- Invocation 1: command reviewed and cached as safe.
- Invocation 2 (new loop, same `data/` dir): same command pattern requested.
- Assert: no review call on invocation 2.

### 9.3 Corrupt cache file

- Write garbage to `data/safety-cache.json`.
- Run the loop. AI requests a command.
- Assert: a warning is logged.
- Assert: the command gets a fresh AI review (cache treated as empty).
- Assert: the cache file is overwritten with valid data.

### 9.4 Missing cache file

- Delete `data/safety-cache.json`.
- Assert: same behavior as 9.3 (fresh review, new file created).

### 9.5 Human-edited cache respected

- Manually add `{"pattern": "docker *", "verdict": "rejected", ...}` to the cache file.
- AI requests `docker build .`.
- Assert: command blocked without AI review call.

---

## 10. Edge cases

### 10.1 Empty commands array

- AI returns `{"type": "command", "commands": [], "reasoning": "..."}`.
- Assert: no commands executed.
- Assert: an empty command-results message is sent back (or the loop treats it as a validation error — either way it doesn't crash).

### 10.2 Very long AI response

- Mock returns a valid answer that is 1MB of JSON.
- Assert: correctly parsed, logged, and returned.

### 10.3 Command produces binary output

- AI requests a command whose stdout contains null bytes.
- Assert: `commands.json` captures the output (base64-encoded or escaped — however the implementation handles it, verify it's round-trippable).

### 10.4 Concurrent budget modification

- Not applicable per spec (no concurrency), but verify: if budget is externally set to 0 between turns, the loop terminates cleanly.

### 10.5 Log directory does not exist

- Pass a `log_dir` that doesn't exist yet.
- Assert: the loop creates it (or returns a clear error — either way, no crash).

### 10.6 Same model for primary and review

- Set `model` and `review_model` to the same value.
- Assert: everything works identically; the distinction is which prompt is used, not which model.

---

## 11. Integration — app creation from empty directory

These tests use a real OpenRouter API (requires `OPENROUTER_API_KEY`). Each
test starts a real fsrouter server with `working_dir` as the route directory.
The directory starts empty except for the agent loop's own files (`prompts/`,
`data/`, `logs/`). The agent loop is given a change description and must
create a working application by writing files into `working_dir/routes/`.

Each test has two phases: **creation** (the agent loop builds the app) and
**verification** (automated checks confirm the app works). The `ask_user`
callback auto-approves all safe/risky commands unless noted otherwise.

Skip these tests when `OPENROUTER_API_KEY` is not set.

### 11.1 QR code reader

**Change description:**

> Create a web app with a page that opens the device camera, detects QR codes
> in the video feed, and displays the decoded contents. When a QR code is
> detected, present the user with these options:
> - Execute it (if it looks like a shell command)
> - Open that web page in a new tab (if it looks like a URL)
> - Open that web page in this tab (if it looks like a URL)
> - Say it (use the Web Speech API to speak the contents aloud)
> - Use it as a prompt for an app change (redirect to the change form with the
>   QR contents pre-filled as the change description)
>
> The option list should be contextual — only show options that make sense for
> the detected content. Always show "Say it" and "Use as change prompt".

**Creation-phase assertions:**

- Agent loop returns successfully (not `error`, `budget_exhausted`, or
  `user_aborted`).
- At least one file exists under `working_dir/routes/`.
- `commands.json` shows commands were used to verify the files were written.
- All AI requests/responses are logged.

**Verification-phase assertions:**

- An HTTP GET to the app's root returns HTML with status 200.
- The HTML contains a `<video>` element or `getUserMedia` call (camera access).
- The HTML contains or references a QR code detection library (e.g., jsQR,
  zxing, or equivalent).
- The HTML contains the text "Say it" or equivalent for each expected action.
- The HTML references the change form URL for the "Use as change prompt"
  option.
- The HTML is valid (no unclosed tags, no syntax errors in inline scripts).
- Inline or referenced JavaScript parses without syntax errors (run through
  a JS parser or `node --check` if a `.js` file was created).

### 11.2 Network scanner

**Change description:**

> Create a web app that scans the local network and presents results to the
> user. The app should:
> - Discover machines on the local network (hostname, IP, MAC address where
>   available).
> - Discover services running on found machines (open ports, service names).
> - Present a topology graph showing the relationships between discovered
>   machines and the scanning host.
> - Display results in a table (machines and services) alongside the topology
>   graph.
> - The scan should run on the server side. The UI should poll for progress
>   and update as results come in.
>
> Use tools available on macOS (arp, ping, nmap if installed, etc.). Do not
> require any tools that aren't installed — detect what's available and adapt.

**Creation-phase assertions:**

- Agent loop returns successfully.
- Files exist under `working_dir/routes/`.
- The agent ran discovery commands during creation to determine what network
  tools are available (assert `commands.json` contains at least one of: `which
  nmap`, `which arp`, `command -v`, or equivalent).
- All AI requests/responses are logged.

**Verification-phase assertions:**

- An HTTP GET to the app's root returns HTML with status 200.
- The HTML contains a table or structured display for scan results (machines,
  IPs, services).
- The HTML contains or references a graph/topology visualization (e.g., SVG,
  canvas, a JS graph library, or a Graphviz rendering).
- A server-side handler exists that performs the actual scan (an executable
  file under `routes/` that invokes network commands).
- The scan handler is executable and starts without error when invoked
  directly (exit code 0 with empty or stub output, or exit code 1 with a
  meaningful error — not a crash).
- The scan handler does NOT require tools that aren't present — read the
  handler source and verify it checks for tool availability or was written
  for tools the agent confirmed exist.
- The UI includes a polling mechanism (setInterval, setTimeout, fetch loop,
  or SSE) to update results.

### 11.3 Cron/launchd scheduler frontend

**Change description:**

> Create a web app that provides a frontend for managing scheduled tasks on
> this macOS machine. The app should:
> - List all currently scheduled tasks (cron jobs for the current user, and
>   launchd user agents).
> - Show each task's schedule, command, and status (enabled/disabled for
>   launchd agents, active for cron).
> - Allow the user to add a new cron job (with fields for schedule expression,
>   command, and description).
> - Allow the user to remove an existing cron job.
> - Allow the user to enable/disable launchd user agents.
> - Refresh the list after any modification.
>
> All modifications happen server-side through the fsrouter handlers. The UI
> should confirm before any destructive action (removal, disable).

**Creation-phase assertions:**

- Agent loop returns successfully.
- Files exist under `working_dir/routes/`.
- The agent ran commands to inspect current scheduled tasks during creation
  (assert `commands.json` contains at least one of: `crontab -l`, `launchctl
  list`, `ls ~/Library/LaunchAgents`, or equivalent).
- All AI requests/responses are logged.

**Verification-phase assertions:**

- An HTTP GET to the app's root returns HTML with status 200.
- The HTML contains a list or table for displaying scheduled tasks.
- The HTML contains a form or input mechanism for adding a new cron job (at
  minimum: fields for schedule and command).
- The HTML contains delete/remove controls for existing entries.
- A server-side handler exists for listing tasks (GET) — invoke it and verify
  it returns valid JSON or structured output containing the current user's
  cron jobs (may be empty, but must not error).
- A server-side handler exists for adding tasks (POST) — verify the file
  exists and is executable.
- A server-side handler exists for removing tasks (DELETE or POST) — verify
  the file exists and is executable.
- The UI includes a confirmation step before removal (a `confirm()` call, a
  modal, or a two-step button).
- No handler runs `crontab` with unsanitized user input passed directly into
  a shell string — verify the handler source uses safe argument passing or
  temp file approach.

### 11.4 Cross-cutting assertions (apply to all 11.x tests)

These assertions apply to every integration test above in addition to the
test-specific ones.

**Logging completeness:**

- `log_dir` contains `conversation.json` reflecting the full multi-turn
  exchange.
- Every AI call has a corresponding entry in `request.json` / `response.json`.
- Every command executed has an entry in `commands.json` with non-null
  `execution`.
- Every blocked/rejected command has an entry in `commands.json` with
  `execution: null`.

**Budget accounting:**

- The number of entries in `request.json` / `response.json` (primary +
  review calls) equals the budget spent.
- Budget remaining = initial budget - budget spent.

**Safety cache:**

- `data/safety-cache.json` exists after the test.
- Common commands used during creation (ls, cat, grep, etc.) are cached as
  safe.
- If any commands were blocked or user-rejected, they appear in the cache.

**File hygiene:**

- All created files under `routes/` are either executable handlers or static
  files — no temp files, no `.swp`, no partial writes.
- Executable handlers have a valid shebang line.
- No handler contains hardcoded absolute paths to the test's temp directory.

**Recoverability:**

- Delete all files except `log_dir`. From `conversation.json`,
  `commands.json`, and `status.json` alone, reconstruct what files were
  created and what their purpose was. This is a manual/review assertion — the
  test verifies the log files contain enough information (file paths and
  content appear in command outputs or AI responses).
