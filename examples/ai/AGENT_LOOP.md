# Agent Loop — Spec

A reusable component that drives a single AI-powered task to completion. It
takes a goal and context, makes AI calls, lets the AI run local commands, and
returns a structured result — logging everything to disk along the way.

The agent loop is the core execution primitive used by every step of the change
pipeline (see SPEC.md). Each pipeline step invokes the agent loop with a
different prompt, context, and output schema. The pipeline orchestrates the
steps; the agent loop does the work within each step.

---

## 1. Interface

### 1.1 Inputs

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `goal` | string | yes | The prompt template name (references a file in `prompts/`). |
| `template_vars` | object | yes | Key-value pairs to fill placeholders in the prompt template (e.g., `{{change_description}}`). |
| `output_schema` | JSON Schema | yes | The expected structure of the AI's final answer. Enforced strictly. |
| `model` | string | yes | OpenRouter model ID to use for the primary AI calls in this loop. |
| `review_model` | string | yes | OpenRouter model ID for command safety reviews. May be the same as `model`. |
| `budget` | budget ref | yes | Shared mutable reference to the budget counter. Decremented on every AI call. |
| `log_dir` | path | yes | Directory where this invocation's logs are written. |
| `working_dir` | path | yes | The directory commands execute in (the server root). |
| `ask_user` | function | yes | Callback to pose a multiple-choice question to the user and block until answered. Accepts a question string and a list of named options (plus implicit "Other" with free-text). Returns the user's choice. |
| `feedback` | list of objects | no | Prior feedback to include in the prompt (rejection reasons, failure output, etc.). Default: empty. |

### 1.2 Output

A **result object** conforming to the provided `output_schema`, or one of:

- `budget_exhausted` — the budget hit zero before the AI produced a final
  answer. Includes a summary of work completed so far.
- `user_aborted` — the user chose to abort when asked a question.
- `error` — an unrecoverable error occurred (e.g., API unreachable after
  retries). Includes the error details.

---

## 2. Loop mechanics

The agent loop is a conversation with the AI. Each iteration is one
request/response pair. The loop continues until the AI produces a final answer
or a termination condition is hit.

### 2.1 Conversation structure

The conversation starts with:

1. **System message** — loaded from the prompt template file, with
   `template_vars` substituted. Includes the output schema definition and
   instructions for how to signal a final answer vs. a command request.
2. **Feedback messages** (if any) — each prior feedback item is appended as
   context so the AI knows what went wrong before.

Each subsequent iteration appends:

3. **Command result messages** — the output of commands the AI requested in
   the prior turn, delivered as the next user message.

The full conversation history is sent on every request. This is how the AI
maintains context across multiple command executions within a single loop
invocation.

### 2.2 AI response types

Every AI response must be valid JSON matching one of two shapes:

**Command request:**
```json
{
  "type": "command",
  "commands": [
    {
      "command": "grep -r 'TODO' src/",
      "purpose": "Find all TODO comments in the source tree"
    }
  ],
  "reasoning": "I need to understand the current state before proposing changes."
}
```

The AI may request one or more commands per turn. Commands execute sequentially
within a turn (the output of one may inform whether later ones in the same
batch are still relevant, but for simplicity they all run). All command results
are returned together in the next turn.

**Final answer:**
```json
{
  "type": "answer",
  "answer": { ... }
}
```

The `answer` field must conform to the `output_schema` provided at invocation.

### 2.3 Response validation

Every AI response is parsed and validated:

1. Must be valid JSON.
2. Must have `type` equal to `"command"` or `"answer"`.
3. If `"answer"`, the `answer` field must validate against `output_schema`.
4. If `"command"`, each entry in `commands` must have `command` (string) and
   `purpose` (string).

On validation failure:
- The error message is appended to the conversation as a user message.
- The AI is asked to try again.
- This retry counts as a budget decrement.
- After 3 consecutive validation failures, the loop terminates with an
  `error` result.

---

## 3. Command execution

When the AI requests commands, each command goes through the following before
running:

### 3.1 Safety cache lookup

Check the command against the **safety cache** — a JSON file at
`data/safety-cache.json` that records command patterns and their verdicts.

The cache stores entries as:
```json
{
  "pattern": "grep *",
  "verdict": "safe",
  "source": "review_model",
  "timestamp": "2026-03-26T12:00:00Z"
}
```

Matching is prefix-based on the command's executable name and flags (e.g.,
`grep` with any arguments matches a `grep *` pattern). On a cache hit with
verdict `"safe"`, the command proceeds without an AI review call. On a hit
with `"rejected"`, the command is blocked and the rejection is returned to
the AI.

### 3.2 AI safety review (on cache miss)

If no cache entry matches, an AI call (using `review_model`) evaluates the
command. The review prompt includes:

- The command string.
- The stated purpose.
- The working directory.
- The current goal (so the reviewer has context on why this command is being
  requested).

The review AI returns a structured verdict:
```json
{
  "verdict": "safe" | "risky" | "blocked",
  "reasoning": "...",
  "pattern": "..."
}
```

- `"safe"` — the command is harmless. The pattern is added to the cache.
  The command proceeds.
- `"risky"` — the command could be dangerous but might be legitimate. The
  user is asked to approve or reject (via `ask_user`). The user's decision
  and the pattern are added to the cache.
- `"blocked"` — the command is clearly dangerous (e.g., `rm -rf /`). The
  command does not run. The pattern is added to the cache. The block reason
  is returned to the AI as a command result.

This AI call counts against the budget.

### 3.3 Execution

Approved commands run as child processes:
- Working directory: `working_dir`.
- Timeout: 30 seconds (matches fsrouter's default `COMMAND_TIMEOUT`).
- Captured: stdout, stderr, exit code.
- If the command times out, that fact is recorded and returned to the AI.

### 3.4 Logging

Every command — whether it ran, was cached-safe, cached-rejected, reviewed,
or user-decided — is appended to `commands.json` in `log_dir`:

```json
{
  "command": "grep -r 'TODO' src/",
  "purpose": "Find all TODO comments",
  "safety": {
    "source": "cache" | "review" | "user",
    "verdict": "safe" | "risky" | "blocked" | "rejected",
    "reasoning": "..."
  },
  "execution": {
    "stdout": "...",
    "stderr": "...",
    "exit_code": 0,
    "timed_out": false,
    "duration_ms": 42
  }
}
```

Commands that were blocked or rejected have `execution: null`.

---

## 4. Budget accounting

Every AI call the loop makes decrements the shared budget counter:

- Primary AI calls (conversation turns): 1 per turn.
- Command safety reviews (cache miss): 1 per review.
- Validation retries: 1 per retry.

Before each AI call, the loop checks the budget. If the budget is zero:

1. Write a summary of work completed so far to the log.
2. Return `budget_exhausted` with the summary.

The caller (pipeline) is responsible for asking the user to extend or abort
and re-invoking the loop with the extended budget and accumulated conversation
if the user extends.

---

## 5. Logging

The loop writes the following files to `log_dir`:

| File | Contents | Written when |
|------|----------|-------------|
| `request.json` | Full OpenRouter request body | Before every AI call |
| `response.json` | Full OpenRouter response body | After every AI call |
| `commands.json` | Array of command entries (section 3.4) | After each command batch |
| `feedback.json` | The feedback list passed at invocation | At loop start, if non-empty |
| `conversation.json` | The full conversation history | After every turn |

When multiple AI calls happen within a single loop invocation (multi-turn
conversation), `request.json` and `response.json` are written as arrays — one
entry per turn, in order. Alternatively, they may be written to numbered files
(`request-1.json`, `request-2.json`, ...) — the implementation should pick
whichever is simpler, but must be consistent.

`conversation.json` is the full message array sent on the most recent request.
It serves as a snapshot of the loop's state — if the loop terminated early
(budget, error, abort), this file plus the other logs fully reconstruct what
happened.

---

## 6. Error handling

| Condition | Behavior |
|-----------|----------|
| OpenRouter API returns HTTP error | Retry up to 3 times with exponential backoff (1s, 2s, 4s). Each retry does NOT decrement budget. If all retries fail, return `error`. |
| AI response fails validation | Append error to conversation, retry (section 2.3). Counts against budget. |
| Command times out | Return timeout info to AI as command result. AI decides next action. |
| Command exits non-zero | Return full stderr/stdout + exit code to AI. AI decides next action. |
| `ask_user` returns abort | Return `user_aborted` immediately. |
| Budget reaches zero | Return `budget_exhausted` (section 4). |
| 3 consecutive validation failures | Return `error` with details of what the AI kept getting wrong. |

---

## 7. Prompt template contract

Prompt templates used with the agent loop must include the following
instructions (the loop prepends/appends these if not present):

- The output schema definition.
- Instructions to respond with `{"type": "command", ...}` to request commands.
- Instructions to respond with `{"type": "answer", ...}` when done.
- That all responses must be valid JSON — no markdown fencing, no preamble.

The template's own content defines the goal, constraints, and context specific
to the pipeline step invoking the loop.

---

## 8. Safety cache details

The safety cache (`data/safety-cache.json`) persists across changes and server
restarts. It is scoped to the server root — if the server root changes, the
cache should be reset.

Cache entries are keyed by command pattern (executable name + flag signature).
Arguments that look like file paths or variable data are wildcarded in the
pattern so that `grep -r 'foo' src/` and `grep -r 'bar' lib/` share a cache
entry.

The cache file is human-readable and human-editable. A user can manually add,
remove, or change entries to pre-approve or pre-block command patterns.

If the cache file is missing or corrupt, the loop proceeds as if the cache is
empty (all commands get AI review). A warning is logged.
