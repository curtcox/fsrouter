# AI Change Request Example — Spec

An fsrouter example app that lets a user describe a code change via a web form,
then uses AI (via OpenRouter) to plan, execute, validate, and report on that
change — targeting the filesystem under the running server's root directory.

Everything is logged to disk so the full state of any change is always
recoverable from the filesystem. Only one change pipeline runs at a time.
State is persisted for diagnostic and introspection purposes; the pipeline does
not resume after a server restart.

---

## 1. UI

A tabbed interface with two tabs: **Change** and **Settings**.

### 1.1 Change tab (primary)

- **Model selector** — pick from favorited models (populated from Settings).
  Defaults to the model last used.
- **Change description** — free-text area describing the desired change.
- **Budget** — max AI call count for this change. Defaults to the last budget
  used.
- **Submit** button.
- **Pause / Resume** button — visible while a pipeline is running. Pauses the
  pipeline after the current step completes, letting the user inspect state,
  edit context, or intervene before resuming. The pipeline never blocks waiting
  for user approval; it auto-proceeds unless paused.

After submission the Change tab becomes a live status view showing progress
through the pipeline (section 3). High-level status is shown by default; each
step includes expandable/linkable access to full details (context files, AI
request/response logs, diffs, validation output).

### 1.2 User questions

When the pipeline needs user input (validation choice, stall resolution,
clarification, etc.), it presents a **multiple-choice prompt** inline in the
status view. Each prompt includes:

- Named options (e.g., "Retry", "Revise", "Abort").
- An "Other" option with a free-text input for anything not covered by the
  named choices.

The pipeline pauses until the user answers. The question and the user's answer
are logged in `questions.json`.

### 1.3 Settings tab

- **Model browser** — fetched from `https://openrouter.ai/api/v1/models`.
  User can star/unstar models to build a favorites list. Favorites and
  last-used model are persisted to disk.
- **Default budget** — editable default for the budget field.

If no valid API key is available (see section 7), the entire UI is replaced by
a setup page with instructions for obtaining and configuring the
`OPENROUTER_API_KEY` environment variable.

---

## 2. Architecture

### 2.1 Pipeline process

The change pipeline runs as a **long-lived background process** spawned by the
submit handler. It writes all state to the filesystem under `logs/ai/`. The
UI reads pipeline state by polling the filesystem via HTTP — there is no
in-memory communication between the pipeline and the UI.

The pipeline process writes `status.json` atomically after every state change.
User answers to questions (section 1.2) are delivered by the UI writing a
response file that the pipeline polls for.

### 2.2 Model assignment

Different pipeline steps can use different AI models. The settings allow the
user to configure which model to use for which role:

- **Primary model** — used for change generation (3.3), context gathering
  (3.2), pre-check (3.1), and next-step suggestions (3.8). Selected on the
  Change tab per-submission.
- **Review model** — used for safety review (3.4) and command safety review
  (3.0). May be a cheaper/faster model. Configurable in Settings; defaults to
  the primary model.

Each AI call in the logs records which model was used.

---

## 3. Logging

All logs live under `examples/ai/logs/` (gitignored).

```
logs/
  ai/
    <change-id>/
      status.json          # current pipeline state + step results
      questions.json       # questions asked of the user + answers received
      steps/
        <step-number>-<step-name>/
          request.json     # full AI request body
          response.json    # full AI response body
          context.json     # list of context refs sent to AI
          edits.json       # structured edits produced (if applicable)
          commands.json    # local commands executed + output (if applicable)
          validation.txt   # validation command + full output (if applicable)
          feedback.json    # feedback from prior failures fed into this step
```

Every reference in the UI to an AI call, context gathering, command execution,
or validation result links to the corresponding log file so the user can view
exact request/response payloads and command output.

`status.json` is the single source of truth for where a change stands. Any
process can determine what has happened and what remains by reading it.

---

## 4. Change pipeline

Each step is recorded in `status.json` and visible in the UI. The pipeline
auto-proceeds through steps unless the user has pressed Pause or the pipeline
is waiting for a user answer (section 1.2).

If any step needs information it doesn't have, it asks the user rather than
guessing. If a step needs a capability it doesn't have, it requests tools or
clarification. All questions and answers are logged in `questions.json`.

The target of all changes is the filesystem under the running server's root
directory. Both text and binary files are in scope.

### 4.0 Command execution

Several pipeline steps allow the AI to run local commands (grep, find, awk,
test runners, build tools, etc.) to inspect or validate the codebase. Command
execution works as follows:

1. The AI requests a command to run.
2. Before execution, the command is checked against a **local safety cache** —
   a record of command patterns previously approved or rejected. If the cache
   has a match, the cached verdict is used without an AI call.
3. On cache miss, an AI call (using the review model) evaluates the command
   for risk (e.g., destructive operations, writes outside the server root,
   network access). The verdict and reasoning are logged and added to the
   cache.
4. If the command is flagged as risky, the user is asked to approve or reject
   it (section 1.2). The user's decision updates the cache.
5. The command runs and its full output (stdout, stderr, exit code) is logged
   in `commands.json` for the current step.
6. The output is returned to the requesting AI call.

This loop may repeat multiple times within a single step as the AI explores
the codebase or runs validation. All AI calls in this loop (including safety
reviews on cache miss) count against the budget.

### 4.1 Pre-check — has this change already been made?

Use AI to determine whether the described change is already present. The AI
may run local commands (section 4.0) to inspect the codebase — either in a
single pass for small codebases or across multiple calls for larger ones. If
the change appears done, show supporting evidence (file paths, line ranges,
reasoning) and stop. If uncertain, ask the user.

### 4.2 Context gathering

Produce a list of **change context references**: each is a file path (relative
to server root) plus optional line ranges. Use AI to determine which files and
regions are relevant; the AI may run local commands (section 4.0) to explore
the filesystem. Display the gathered context to the user with links to each
file/range. The user can add or remove context items while paused.

### 4.3 Change generation

Send the change description + gathered context to the AI. The AI responds with
a structured schema — either:

- **A set of edits** — concrete file operations (create, modify, delete) with
  full content for new files and search-and-replace pairs for text
  modifications. If an edit type doesn't fit these patterns (e.g., binary
  files), the problem is escalated to the user via section 1.2.
- **A decomposition** — the change should be broken into smaller sub-changes.
  Each sub-change re-enters the pipeline at step 4.1 (recursive). Sub-changes
  are tracked as children in `status.json`. There is no depth limit; budget and
  time are the only constraints.

**Stall detection:** Before executing a decomposition, check whether the
sub-changes are materially different from the parent. If the pipeline is not
making progress (e.g., repeated decompositions without any edits applied),
pause and ask the user how to proceed.

**Budget feasibility:** Before executing a decomposition, estimate whether the
remaining budget is sufficient for the sub-changes. If not, warn the user and
ask whether to proceed, extend the budget, or simplify.

### 4.4 Safety review

Use an AI call (review model) to review the proposed edits for safety and
reasonableness relative to the original request. The review result
(safe / risky / rejected) and reasoning are logged. If the review flags
issues, surface them to the user with options: approve anyway, revise, or
abort.

If rejected, the rejection reasoning is included in the next generation prompt
(return to 4.3) so the AI knows what to avoid.

### 4.5 Apply

Apply the edits to the filesystem. The directory is expected to be under git
version control; there is no built-in rollback mechanism. Record the
before/after state in the step log.

**Verification loop:** After applying, re-read each modified file and confirm
the intended edits are present. If application failed (e.g., the expected
content wasn't found for a search-and-replace), retry with corrected
context. Log each attempt.

### 4.6 Validate

The AI determines what validation is appropriate for the change (e.g., run
tests, lint, type-check, try a build) and executes it via section 4.0. If the
AI cannot determine an appropriate validation, it asks the user. The chosen
validation command and its full output are logged.

If validation fails:
- The failure output is included as feedback context for the next attempt.
- The user is asked whether to retry (returns to 4.3 with failure context),
  revise the request, or abort.

### 4.7 Report

Show the user:

- A list of every file changed, with a description of each change and a link
  to the diff.
- Links to every AI call's request/response logs.

### 4.8 Suggest next steps

Use an AI call to consider likely follow-up changes. If any are identified,
present them as links that pre-fill the Change tab with the suggested
description.

---

## 5. Budget enforcement

Every AI call — including command safety reviews — decrements the budget
counter in `status.json`. When the budget is exhausted mid-pipeline:

1. Pause the pipeline.
2. Show the user what has been completed and what remains.
3. Ask the user to either extend the budget (specify additional calls) or
   abort.

Budget state persists across page reloads via `status.json`.

---

## 6. Feedback loops

Failed or rejected steps feed context back into subsequent attempts:

- **Budget exhausted** — the user's extension (or abort) is recorded and
  informs whether/how the pipeline resumes.
- **Safety review rejected** — the rejection reasoning is included in the
  next generation prompt so the AI knows what to avoid.
- **Validation failed** — the failure output is included in the next
  generation prompt.
- **Context insufficient** — if the AI reports it needs more context, the
  user is prompted to supply it.
- **Stall detected** — if decomposition is looping without progress, the
  user is informed and asked how to proceed.
- **Command rejected** — if the user rejects a risky command, the rejection
  is fed back to the AI so it can try an alternative approach.

All feedback is logged in the step's `feedback.json`.

---

## 7. Prompt management

All prompts and prompt templates live as individual plain-text files under
`examples/ai/prompts/`, one file per prompt. This makes them easy to compare,
version, and edit independently. Templates use a simple placeholder syntax
(e.g., `{{change_description}}`, `{{context}}`).

---

## 8. AI integration

- All AI calls go through `https://openrouter.ai/api/v1/chat/completions`.
- API key comes exclusively from the `OPENROUTER_API_KEY` environment variable.
  There is no UI for entering or persisting an API key.
- Model list is fetched from `https://openrouter.ai/api/v1/models`.
- Every request and response is logged in full to `logs/ai/`.
- AI responses that carry structured data (context lists, edits,
  decompositions, review verdicts, next-step suggestions) use strict JSON
  schemas. The schemas are defined once and enforced on every response; parse
  failures are retried with the error fed back to the AI.
- User preferences (favorites, last model, last budget, review model) are
  persisted to a JSON file under `examples/ai/data/` (gitignored).

---

## 9. Handler implementation

Route handlers are executable scripts. The implementation should use whichever
language is best suited from those readily available on macOS — factoring in
shared infrastructure needs (e.g., if most handlers need JSON parsing, HTTP
calls, and access to shared logging utilities, that favors a common language
for those handlers).

All handlers receive request data via environment variables and stdin per the
fsrouter protocol (see `spec/PROTOCOL.md`).

---

## 10. Filesystem layout

```
examples/ai/
  SPEC.md               # this file
  AGENT_LOOP.md         # agent loop spec (standalone component)
  routes/               # fsrouter route directory
    ...                 # route handlers for the UI and API
  prompts/              # one plain-text file per prompt template
  data/                 # gitignored — user prefs, model favorites, safety cache
  logs/                 # gitignored — full AI call logs, pipeline state
  .gitignore            # ignores data/ and logs/
```

The agent loop (see `AGENT_LOOP.md`) is the core execution primitive. Each
pipeline step (4.1–4.8) invokes the agent loop with a step-specific prompt,
context, and output schema. The loop handles AI calls, command execution,
safety review, budget accounting, and logging. The pipeline orchestrates the
steps; the loop does the work within each step.
