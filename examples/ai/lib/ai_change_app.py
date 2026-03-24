#!/usr/bin/env python3
from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = APP_ROOT / "data"
CHANGES_ROOT = DATA_ROOT / "changes"
PROMPT_ROOT = APP_ROOT / "prompts"
DEFAULT_AI_BUDGET = 8
DEFAULT_COMMAND_TIMEOUT = 120
MAX_CONTEXT_ITEMS = 12
MAX_CONTEXT_LINES = 250
MAX_CONTEXT_CHARS = 14000
MAX_MANIFEST_ITEMS = 2000
MAX_CHANGE_FILES = 20
MAX_RECURSION_DEPTH = 2
MODEL_FETCH_TIMEOUT = 8
CHAT_TIMEOUT = 60
REFRESH_SECONDS = 3
PROTECTED_PATH_PARTS = {".git"}
PROTECTED_RELATIVE_PREFIXES = {"examples/ai/data/"}


class AppError(Exception):
    pass


class InvalidAPIKeyError(AppError):
    pass


class BudgetExceededError(AppError):
    pass


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 3),
        }


def target_root() -> Path:
    configured = os.environ.get("AI_CHANGE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return APP_ROOT.parent.parent.resolve()


def ensure_runtime_dirs() -> None:
    CHANGES_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_slug() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def query_params() -> dict[str, str]:
    parsed = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def form_params() -> dict[str, str]:
    body = sys.stdin.buffer.read()
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def response_headers(status: int = 200, content_type: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
    if status != 200:
        print(f"Status: {status}")
    print(f"Content-Type: {content_type}")
    if extra:
        for key, value in extra.items():
            print(f"{key}: {value}")
    print()


def html_page(title: str, body: str, *, subtitle: str = "", refresh_seconds: int | None = None, notice: str = "") -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    subtitle_html = f"<p class=\"subtitle\">{e(subtitle)}</p>" if subtitle else ""
    notice_html = f"<div class=\"notice\">{notice}</div>" if notice else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  {refresh}
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <main class="shell">
    <header class="hero">
      <a class="eyebrow" href="/">fsrouter AI change example</a>
      <h1>{e(title)}</h1>
      {subtitle_html}
    </header>
    {notice_html}
    {body}
  </main>
</body>
</html>
"""


def redirect(location: str, status: int = 303) -> None:
    response_headers(status=status, extra={"Location": location})


def route_url(path: str, **params: str | int | None) -> str:
    items = [(key, str(value)) for key, value in params.items() if value is not None and value != ""]
    if not items:
        return path
    return f"{path}?{urllib.parse.urlencode(items)}"


def prompt_text(name: str, values: dict[str, str]) -> str:
    text = read_text(PROMPT_ROOT / name)
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def preferences_path() -> Path:
    return DATA_ROOT / "preferences.json"


def models_cache_path() -> Path:
    return DATA_ROOT / "models_cache.json"


def default_preferences() -> dict:
    return {
        "default_validation_command": "",
        "favorite_models": [],
        "last_budget": DEFAULT_AI_BUDGET,
        "last_model": "",
    }


def load_preferences() -> dict:
    prefs = read_json(preferences_path(), default_preferences())
    merged = default_preferences()
    merged.update(prefs)
    favorites = []
    for model in merged.get("favorite_models", []):
        model = str(model).strip()
        if model and model not in favorites:
            favorites.append(model)
    merged["favorite_models"] = favorites
    try:
        merged["last_budget"] = max(1, int(merged.get("last_budget", DEFAULT_AI_BUDGET)))
    except (TypeError, ValueError):
        merged["last_budget"] = DEFAULT_AI_BUDGET
    merged["default_validation_command"] = str(merged.get("default_validation_command", "")).strip()
    merged["last_model"] = str(merged.get("last_model", "")).strip()
    return merged


def save_preferences(prefs: dict) -> None:
    write_json_atomic(preferences_path(), prefs)


def save_last_used_preferences(model: str, budget: int, favorite: bool) -> None:
    prefs = load_preferences()
    prefs["last_model"] = model
    prefs["last_budget"] = budget
    if favorite and model and model not in prefs["favorite_models"]:
        prefs["favorite_models"].append(model)
    save_preferences(prefs)


def update_favorite_model(model: str, action: str) -> None:
    prefs = load_preferences()
    favorites = prefs["favorite_models"]
    if action == "add" and model and model not in favorites:
        favorites.append(model)
    if action == "remove":
        favorites = [item for item in favorites if item != model]
    prefs["favorite_models"] = favorites
    save_preferences(prefs)


def save_settings_preferences(default_validation_command: str) -> None:
    prefs = load_preferences()
    prefs["default_validation_command"] = default_validation_command.strip()
    save_preferences(prefs)


def openrouter_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def openrouter_headers() -> dict[str, str]:
    api_key = openrouter_api_key()
    if not api_key:
        raise InvalidAPIKeyError("OPENROUTER_API_KEY is not configured.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "https://github.com/openai/codex"),
        "X-Title": "fsrouter ai example",
    }


def flatten_openrouter_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def list_available_models() -> tuple[list[dict], str]:
    api_key = openrouter_api_key()
    cache = read_json(models_cache_path(), {"models": [], "fetched_at": ""})
    if not api_key:
        raise InvalidAPIKeyError("OPENROUTER_API_KEY is missing.")

    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers=openrouter_headers(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_FETCH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise InvalidAPIKeyError("OpenRouter rejected the supplied API key.") from err
        if cache.get("models"):
            return cache["models"], f"Model list refresh failed with HTTP {err.code}; showing cached results."
        raise AppError(f"Unable to load models: HTTP {err.code}") from err
    except urllib.error.URLError as err:
        if cache.get("models"):
            return cache["models"], f"Could not reach OpenRouter; showing cached model results ({err.reason})."
        return [], f"Could not reach OpenRouter to load the model list ({err.reason}). You can still type a model id manually."

    models: list[dict] = []
    for item in payload.get("data", []):
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        models.append(
            {
                "id": model_id,
                "name": str(item.get("name", model_id)).strip() or model_id,
                "context_length": item.get("context_length"),
                "pricing": item.get("pricing", {}),
            }
        )
    models.sort(key=lambda item: item["id"])
    write_json_atomic(models_cache_path(), {"fetched_at": utc_timestamp(), "models": models})
    return models, ""


def openrouter_chat(model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=openrouter_headers(),
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        if err.code in (401, 403):
            raise InvalidAPIKeyError("OpenRouter rejected the supplied API key.") from err
        raise AppError(f"OpenRouter chat request failed with HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise AppError(f"Could not reach OpenRouter: {err.reason}") from err

    choices = raw.get("choices") or []
    if not choices:
        raise AppError("OpenRouter returned no choices.")
    message = choices[0].get("message", {})
    content = flatten_openrouter_content(message.get("content"))
    if not content:
        raise AppError("OpenRouter returned an empty completion.")
    return content


def strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def extract_json(text: str):
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    first_object = cleaned.find("{")
    last_object = cleaned.rfind("}")
    if first_object != -1 and last_object != -1 and last_object > first_object:
        candidate = cleaned[first_object : last_object + 1]
        return json.loads(candidate)

    first_array = cleaned.find("[")
    last_array = cleaned.rfind("]")
    if first_array != -1 and last_array != -1 and last_array > first_array:
        candidate = cleaned[first_array : last_array + 1]
        return json.loads(candidate)

    raise AppError("AI response did not contain valid JSON.")


def relative_target_path(value: str) -> str:
    rel = value.strip().replace("\\", "/")
    if not rel or rel.startswith("/"):
        raise AppError(f"Invalid path reference: {value!r}")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise AppError(f"Invalid path reference: {value!r}")
    normalized = "/".join(parts)
    lowered = normalized.lower()
    for protected in PROTECTED_RELATIVE_PREFIXES:
        if lowered.startswith(protected.lower()):
            raise AppError(f"Refusing to modify protected runtime path: {normalized}")
    if any(part in PROTECTED_PATH_PARTS for part in parts):
        raise AppError(f"Refusing to modify protected path: {normalized}")
    return normalized


def resolve_target_path(relative_path: str) -> Path:
    rel = relative_target_path(relative_path)
    root = target_root()
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise AppError(f"Path escapes the configured server root: {relative_path}")
    return candidate


def build_file_manifest() -> str:
    root = target_root()
    entries: list[str] = []
    ignored_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "node_modules"}

    for current_root, dirnames, filenames in os.walk(root):
        current_path = Path(current_root)
        relative_root = current_path.relative_to(root).as_posix()
        dirnames[:] = [
            name
            for name in dirnames
            if name not in ignored_dirs and f"{relative_root}/{name}".strip("/").lower() != "examples/ai/data"
        ]
        for filename in sorted(filenames):
            full_path = current_path / filename
            relative = full_path.relative_to(root).as_posix()
            if relative.lower().startswith("examples/ai/data/"):
                continue
            try:
                size = full_path.stat().st_size
            except OSError:
                continue
            entries.append(f"{relative} | {size} bytes")
            if len(entries) >= MAX_MANIFEST_ITEMS:
                break
        if len(entries) >= MAX_MANIFEST_ITEMS:
            break

    if not entries:
        return "(no visible files)"
    if len(entries) >= MAX_MANIFEST_ITEMS:
        entries.append("... manifest truncated ...")
    return "\n".join(entries)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise AppError(f"{path.relative_to(target_root()).as_posix()} is not UTF-8 text.") from err


def slice_lines(text: str, start_line: int | None, end_line: int | None) -> tuple[int, int, str]:
    lines = text.splitlines()
    total = len(lines)
    start = 1 if start_line in (None, 0) else max(1, int(start_line))
    end = total if end_line in (None, 0) else min(total, int(end_line))
    if total == 0:
        return 1, 1, ""
    if end < start:
        end = start
    selected = lines[start - 1 : end]
    content = "\n".join(selected)
    if selected and text.endswith("\n"):
        content += "\n"
    return start, end, content


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def render_context_blocks(items: list[dict]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        header = f"Context {index}: {item['path']}:{item['start_line']}-{item['end_line']}"
        blocks.append(header)
        blocks.append(item["content"])
        blocks.append("")
    combined = "\n".join(blocks).strip()
    return shorten(combined, MAX_CONTEXT_CHARS)


def run_check_command(command: str) -> CommandResult:
    start = time.time()
    shell = os.environ.get("SHELL", "/bin/sh")
    timeout = int(os.environ.get("AI_CHANGE_COMMAND_TIMEOUT", str(DEFAULT_COMMAND_TIMEOUT)) or DEFAULT_COMMAND_TIMEOUT)
    try:
        proc = subprocess.Popen(
            [shell, "-lc", command],
            cwd=str(target_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        exit_code = proc.returncode or 0
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        exit_code = 124
        stderr = (stderr + "\n" if stderr else "") + f"Command timed out after {timeout} seconds."
    return CommandResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.time() - start,
    )


def change_dir(change_id: str) -> Path:
    return CHANGES_ROOT / change_id


def request_path_for(change_id: str) -> Path:
    return change_dir(change_id) / "request.json"


def state_path_for(change_id: str) -> Path:
    return change_dir(change_id) / "state.json"


def result_path_for(change_id: str) -> Path:
    return change_dir(change_id) / "result.json"


def events_path_for(change_id: str) -> Path:
    return change_dir(change_id) / "events.jsonl"


def ai_calls_path_for(change_id: str) -> Path:
    return change_dir(change_id) / "ai_calls.jsonl"


def load_request(change_id: str) -> dict:
    request = read_json(request_path_for(change_id), {})
    if not request:
        raise AppError(f"Unknown change id: {change_id}")
    return request


def load_state(change_id: str) -> dict:
    return read_json(state_path_for(change_id), {})


def load_result(change_id: str) -> dict:
    return read_json(result_path_for(change_id), {"context_items": [], "applied_changes": [], "attempted_changes": [], "next_steps": []})


def save_state(change_id: str, state: dict) -> None:
    write_json_atomic(state_path_for(change_id), state)


def save_result(change_id: str, result: dict) -> None:
    write_json_atomic(result_path_for(change_id), result)


def append_event(change_id: str, message: str, *, level: str = "info") -> None:
    append_jsonl(
        events_path_for(change_id),
        {"timestamp": utc_timestamp(), "level": level, "message": message},
    )


def load_events(change_id: str) -> list[dict]:
    items: list[dict] = []
    path = events_path_for(change_id)
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def create_change_request(description: str, validation_command: str, model: str, ai_budget: int, favorite_model: bool) -> str:
    ensure_runtime_dirs()
    change_id = f"{now_slug()}-{uuid.uuid4().hex[:6]}"
    directory = change_dir(change_id)
    directory.mkdir(parents=True, exist_ok=True)
    request = {
        "id": change_id,
        "description": description,
        "validation_command": validation_command,
        "model": model,
        "ai_budget": ai_budget,
        "favorite_model": favorite_model,
        "created_at": utc_timestamp(),
        "server_root": str(target_root()),
    }
    state = {
        "status": "queued",
        "created_at": request["created_at"],
        "updated_at": request["created_at"],
        "current_step": "Queued",
        "ai_calls_used": 0,
        "error": "",
    }
    result = {
        "context_items": [],
        "applied_changes": [],
        "attempted_changes": [],
        "next_steps": [],
        "decomposition": [],
        "review": {},
    }
    write_json_atomic(request_path_for(change_id), request)
    write_json_atomic(state_path_for(change_id), state)
    write_json_atomic(result_path_for(change_id), result)
    append_event(change_id, "Change request queued.")
    save_last_used_preferences(model, ai_budget, favorite_model)
    return change_id


def list_recent_changes(limit: int = 12) -> list[dict]:
    ensure_runtime_dirs()
    items: list[dict] = []
    for path in CHANGES_ROOT.iterdir():
        if not path.is_dir():
            continue
        request = read_json(path / "request.json", {})
        state = read_json(path / "state.json", {})
        if not request:
            continue
        items.append(
            {
                "id": request.get("id", path.name),
                "description": request.get("description", ""),
                "created_at": request.get("created_at", ""),
                "status": state.get("status", "unknown"),
                "current_step": state.get("current_step", ""),
            }
        )
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return items[:limit]


def spawn_worker(change_id: str) -> None:
    worker_path = APP_ROOT / "bin" / "process_change.py"
    log_path = change_dir(change_id) / "worker.log"
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [sys.executable, str(worker_path), change_id],
            cwd=str(APP_ROOT),
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )


class Workflow:
    def __init__(self, change_id: str):
        self.change_id = change_id
        self.request = load_request(change_id)
        self.state = load_state(change_id)
        self.result = load_result(change_id)
        self.model = self.request["model"]
        self.budget_total = int(self.request["ai_budget"])
        self.ai_calls_used = int(self.state.get("ai_calls_used", 0))

    def persist(self) -> None:
        self.state["ai_calls_used"] = self.ai_calls_used
        self.state["updated_at"] = utc_timestamp()
        save_state(self.change_id, self.state)
        save_result(self.change_id, self.result)

    def log(self, message: str, *, level: str = "info") -> None:
        append_event(self.change_id, message, level=level)

    def set_step(self, label: str) -> None:
        self.state["current_step"] = label
        self.persist()
        self.log(label)

    def set_status(self, status: str, *, error: str = "") -> None:
        self.state["status"] = status
        self.state["error"] = error
        if status in {"completed", "already_satisfied", "error", "validation_failed", "rolled_back"}:
            self.state["completed_at"] = utc_timestamp()
        self.persist()

    def remaining_budget(self) -> int:
        return max(0, self.budget_total - self.ai_calls_used)

    def ai_json(self, stem: str, values: dict[str, str]):
        if self.ai_calls_used >= self.budget_total:
            raise BudgetExceededError("The configured AI call budget has been exhausted.")
        system_prompt = prompt_text(f"{stem}_system.txt", values)
        user_prompt = prompt_text(f"{stem}_user.txt", values)
        raw_response = openrouter_chat(self.model, system_prompt, user_prompt)
        self.ai_calls_used += 1
        self.persist()
        append_jsonl(
            ai_calls_path_for(self.change_id),
            {
                "timestamp": utc_timestamp(),
                "prompt": stem,
                "model": self.model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response": raw_response,
            },
        )
        return extract_json(raw_response)

    def choose_context(self, description: str, validation_before: CommandResult, depth: int) -> list[dict]:
        manifest = build_file_manifest()
        payload = self.ai_json(
            "context_selector",
            {
                "CHANGE_DESCRIPTION": description,
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "FILE_MANIFEST": manifest,
                "SERVER_ROOT": str(target_root()),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )
        raw_items = payload.get("context", [])
        if not isinstance(raw_items, list) or not raw_items:
            raise AppError("The AI did not return any usable context references.")

        selected: list[dict] = []
        for item in raw_items[:MAX_CONTEXT_ITEMS]:
            path = relative_target_path(str(item.get("path", "")).strip())
            full_path = resolve_target_path(path)
            if not full_path.exists() or not full_path.is_file():
                continue
            text = read_text_file(full_path)
            start = item.get("start_line")
            end = item.get("end_line")
            line_start, line_end, content = slice_lines(text, start, end)
            if line_end - line_start + 1 > MAX_CONTEXT_LINES:
                line_end = line_start + MAX_CONTEXT_LINES - 1
                _, _, content = slice_lines(text, line_start, line_end)
            selected.append(
                {
                    "scope": description,
                    "path": path,
                    "reason": str(item.get("reason", "")).strip(),
                    "start_line": line_start,
                    "end_line": line_end,
                    "content": content,
                    "file_url": route_url("/file", path=path, start=line_start, end=line_end),
                }
            )
        if not selected:
            raise AppError("No valid filesystem context could be loaded from the AI response.")
        self.result.setdefault("context_items", []).extend(selected)
        self.persist()
        self.log(f"Selected {len(selected)} context item(s) for: {description}")
        return selected

    def plan(self, description: str, validation_before: CommandResult, context_items: list[dict], depth: int) -> dict:
        return self.ai_json(
            "plan_change",
            {
                "CHANGE_DESCRIPTION": description,
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )

    def implement(self, description: str, validation_before: CommandResult, context_items: list[dict], depth: int) -> list[dict]:
        payload = self.ai_json(
            "implement_change",
            {
                "CHANGE_DESCRIPTION": description,
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )
        raw_changes = payload.get("changes", [])
        if not isinstance(raw_changes, list) or not raw_changes:
            raise AppError("The AI did not propose any file changes.")
        if len(raw_changes) > MAX_CHANGE_FILES:
            raise AppError("The AI proposed too many file changes for a single run.")
        prepared: list[dict] = []
        seen_paths: set[str] = set()
        for item in raw_changes:
            action = str(item.get("action", "")).strip().lower()
            if action not in {"create", "update", "delete"}:
                raise AppError(f"Unsupported file action: {action}")
            path = relative_target_path(str(item.get("path", "")).strip())
            if path in seen_paths:
                raise AppError(f"The AI proposed multiple edits for the same path in one scope: {path}")
            seen_paths.add(path)
            full_path = resolve_target_path(path)
            before_exists = full_path.exists()
            before_content = ""
            if before_exists:
                if full_path.is_symlink():
                    raise AppError(f"Refusing to edit symlinked path: {path}")
                if full_path.is_dir():
                    raise AppError(f"Refusing to edit directory path: {path}")
                before_content = read_text_file(full_path)
            description_text = str(item.get("description", "")).strip() or f"{action.title()} {path}"
            after_content = ""
            if action in {"create", "update"}:
                if "content" not in item:
                    raise AppError(f"The AI did not provide file contents for {path}.")
                after_content = str(item.get("content", ""))
            diff_text = "\n".join(
                difflib.unified_diff(
                    before_content.splitlines(),
                    after_content.splitlines(),
                    fromfile=f"{path} (before)",
                    tofile=f"{path} (after)",
                    lineterm="",
                )
            )
            prepared.append(
                {
                    "scope": description,
                    "action": action,
                    "path": path,
                    "full_path": str(full_path),
                    "description": description_text,
                    "before_exists": before_exists,
                    "before_content": before_content,
                    "after_content": after_content,
                    "diff": diff_text,
                }
            )
        return prepared

    def review(self, description: str, validation_before: CommandResult, context_items: list[dict], prepared_changes: list[dict]) -> dict:
        diffs = []
        for item in prepared_changes:
            diffs.append(f"Change: {item['description']}\nPath: {item['path']}\nAction: {item['action']}\n{item['diff']}\n")
        payload = self.ai_json(
            "review_change",
            {
                "CHANGE_DESCRIPTION": description,
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "PROPOSED_DIFFS": shorten("\n".join(diffs), 24000),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
            },
        )
        approved = bool(payload.get("approved"))
        issues = payload.get("issues", [])
        summary = str(payload.get("summary", "")).strip()
        review = {"approved": approved, "summary": summary, "issues": issues}
        self.result["review"] = review
        self.persist()
        return review

    def suggest_next_steps(self, validation_after: CommandResult) -> list[dict]:
        if self.remaining_budget() <= 0:
            return []
        summary_lines = []
        for item in self.result.get("applied_changes", []):
            summary_lines.append(f"- {item['path']}: {item['description']}")
        payload = self.ai_json(
            "next_steps",
            {
                "CHANGE_DESCRIPTION": self.request["description"],
                "VALIDATION_COMMAND": self.request["validation_command"],
                "VALIDATION_AFTER_EXIT_CODE": str(validation_after.exit_code),
                "VALIDATION_AFTER_STDOUT": validation_after.stdout or "(empty)",
                "VALIDATION_AFTER_STDERR": validation_after.stderr or "(empty)",
                "APPLIED_CHANGES": "\n".join(summary_lines) or "(no applied changes)",
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
            },
        )
        steps = payload.get("next_steps", [])
        normalized: list[dict] = []
        if isinstance(steps, list):
            for item in steps[:5]:
                description = str(item.get("description", "")).strip()
                if not description:
                    continue
                normalized.append(
                    {
                        "description": description,
                        "check_command": str(item.get("check_command", self.request["validation_command"])).strip(),
                        "why": str(item.get("why", "")).strip(),
                    }
                )
        self.result["next_steps"] = normalized
        self.persist()
        return normalized


def safe_write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def apply_prepared_changes(prepared_changes: list[dict]) -> None:
    for item in prepared_changes:
        path = Path(item["full_path"])
        if item["action"] == "delete":
            if path.exists():
                path.unlink()
            continue
        safe_write_file(path, item["after_content"])


def rollback_prepared_changes(prepared_changes: list[dict]) -> None:
    for item in reversed(prepared_changes):
        path = Path(item["full_path"])
        if item["before_exists"]:
            safe_write_file(path, item["before_content"])
        elif path.exists():
            path.unlink()
            parent = path.parent
            while parent != target_root() and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def prepared_change_for_result(item: dict) -> dict:
    return {
        "scope": item["scope"],
        "action": item["action"],
        "path": item["path"],
        "description": item["description"],
        "diff": item["diff"],
        "file_url": route_url("/file", path=item["path"]),
    }


def local_review_issues(prepared_changes: list[dict]) -> list[str]:
    issues: list[str] = []
    for item in prepared_changes:
        path = item["path"].lower()
        if path.startswith(".git/"):
            issues.append(f"Refusing to modify repository metadata: {item['path']}")
        if path.startswith("examples/ai/data/"):
            issues.append(f"Refusing to modify runtime data: {item['path']}")
    return issues


def execute_change_scope(workflow: Workflow, description: str, validation_before: CommandResult, depth: int, applied_changes: list[dict]) -> list[dict]:
    workflow.set_step(f"Gathering context for depth {depth}")
    context_items = workflow.choose_context(description, validation_before, depth)

    workflow.set_step(f"Planning change for depth {depth}")
    plan = workflow.plan(description, validation_before, context_items, depth)
    action = str(plan.get("action", "implement")).strip().lower()
    if action == "decompose" and depth < MAX_RECURSION_DEPTH:
        subchanges = plan.get("subchanges", [])
        if isinstance(subchanges, list) and 1 < len(subchanges) <= 3 and workflow.remaining_budget() >= len(subchanges) * 2:
            decomposition_record = {"description": description, "subchanges": subchanges, "depth": depth}
            workflow.result.setdefault("decomposition", []).append(decomposition_record)
            workflow.persist()
            collected: list[dict] = []
            for index, item in enumerate(subchanges, start=1):
                sub_description = str(item.get("description", "")).strip()
                if not sub_description:
                    continue
                workflow.log(f"Executing subchange {index}: {sub_description}")
                collected.extend(execute_change_scope(workflow, sub_description, validation_before, depth + 1, applied_changes))
            if collected:
                return collected

    workflow.set_step(f"Generating changes for depth {depth}")
    prepared = workflow.implement(description, validation_before, context_items, depth)
    workflow.set_step(f"Reviewing changes for depth {depth}")
    local_issues = local_review_issues(prepared)
    review = workflow.review(description, validation_before, context_items, prepared)
    review_issues = review.get("issues", [])
    if local_issues:
        review_issues = list(review_issues) + local_issues
        review["issues"] = review_issues
        workflow.result["review"] = review
        workflow.persist()
    if local_issues or not review.get("approved"):
        raise AppError("The proposed changes were not approved during review.")
    workflow.set_step(f"Applying approved changes for depth {depth}")
    apply_prepared_changes(prepared)
    applied_changes.extend(prepared)
    workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
    workflow.persist()
    workflow.log(f"Applied {len(prepared)} change(s) for scope: {description}")
    return prepared


def run_workflow(change_id: str) -> None:
    ensure_runtime_dirs()
    workflow = Workflow(change_id)
    workflow.set_status("running")
    workflow.set_step("Checking whether the change already exists")
    before = run_check_command(workflow.request["validation_command"])
    workflow.result["validation_before"] = before.to_dict()
    workflow.persist()

    if before.exit_code == 0:
        workflow.result["existing_evidence"] = {
            "summary": "The validation command already passed before any edits were attempted.",
            "command_result": before.to_dict(),
        }
        workflow.log("Validation already passed; no filesystem changes were needed.")
        workflow.set_status("already_satisfied")
        if workflow.remaining_budget() > 0:
            try:
                workflow.set_step("Suggesting next steps")
                workflow.suggest_next_steps(before)
            except Exception as err:
                workflow.log(f"Skipping next-step suggestions: {err}", level="error")
        return

    applied_changes: list[dict] = []
    try:
        execute_change_scope(workflow, workflow.request["description"], before, 0, applied_changes)
    except Exception:
        if applied_changes:
            rollback_prepared_changes(applied_changes)
            workflow.log("An error occurred after applying intermediate changes; rolled the filesystem back.", level="error")
            workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
            workflow.persist()
        raise

    workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
    workflow.persist()

    workflow.set_step("Running validation after changes")
    after = run_check_command(workflow.request["validation_command"])
    workflow.result["validation_after"] = after.to_dict()
    if after.exit_code != 0:
        rollback_prepared_changes(applied_changes)
        workflow.log("Validation failed after applying changes; rolled the filesystem back.", level="error")
        workflow.set_status("rolled_back", error="Validation failed after applying the AI-generated changes.")
        workflow.persist()
        return

    workflow.result["applied_changes"] = [prepared_change_for_result(item) for item in applied_changes]
    workflow.result["attempted_changes"] = []
    workflow.persist()
    if workflow.remaining_budget() > 0:
        try:
            workflow.set_step("Suggesting next steps")
            workflow.suggest_next_steps(after)
        except Exception as err:
            workflow.log(f"Skipping next-step suggestions: {err}", level="error")
    workflow.set_status("completed")


def render_command_result(result: dict, heading: str) -> str:
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    return f"""
<section class="card">
  <h2>{e(heading)}</h2>
  <p><strong>Command:</strong> <code>{e(result.get("command", ""))}</code></p>
  <p><strong>Exit code:</strong> {e(result.get("exit_code", ""))} <strong>Duration:</strong> {e(result.get("duration_seconds", ""))}s</p>
  <details open>
    <summary>stdout</summary>
    <pre>{e(stdout or "(empty)")}</pre>
  </details>
  <details>
    <summary>stderr</summary>
    <pre>{e(stderr or "(empty)")}</pre>
  </details>
</section>
"""


def key_instructions_page(message: str) -> str:
    body = f"""
<section class="card">
  <h2>OpenRouter setup required</h2>
  <p>{e(message)}</p>
  <ol>
    <li>Create or copy an API key from <a href="https://openrouter.ai/keys">OpenRouter</a>.</li>
    <li>Export it before starting fsrouter: <code>export OPENROUTER_API_KEY=your_key_here</code>.</li>
    <li>Optional: point the example at a different workspace with <code>export AI_CHANGE_ROOT=/path/to/project</code>.</li>
    <li>Restart the server and reload this page.</li>
  </ol>
  <p>This example keeps runtime state under <code>{e(DATA_ROOT)}</code> and reads files relative to <code>{e(target_root())}</code>.</p>
</section>
"""
    return html_page("Configure OpenRouter", body, subtitle="A valid OpenRouter API key is required for context gathering, planning, implementation, review, and follow-up suggestions.")


def render_recent_changes(changes: list[dict]) -> str:
    if not changes:
        return "<p class=\"empty\">No change requests yet.</p>"
    items = []
    for change in changes:
        items.append(
            f"<li><a href=\"/changes/{e(change['id'])}\">{e(change['description'])}</a>"
            f" <span class=\"status-chip\">{e(change['status'])}</span>"
            f" <span class=\"meta\">{e(change['created_at'])}</span></li>"
        )
    return "<ul class=\"stack-list\">" + "".join(items) + "</ul>"


def render_favorites(prefs: dict) -> str:
    favorites = prefs.get("favorite_models", [])
    if not favorites:
        return "<p class=\"empty\">No favorite models saved yet.</p>"
    rows = []
    for model in favorites:
        rows.append(
            f"""
<li class="favorite-row">
  <a class="chip-link" href="{e(route_url('/', model=model))}">{e(model)}</a>
  <form method="post" action="/preferences">
    <input type="hidden" name="action" value="remove">
    <input type="hidden" name="model" value="{e(model)}">
    <button type="submit" class="ghost-button">Remove</button>
  </form>
</li>
"""
        )
    return "<ul class=\"favorites\">" + "".join(rows) + "</ul>"


def selected_model_value(params: dict[str, str], prefs: dict, models: list[dict]) -> str:
    requested = params.get("model", "").strip()
    if requested:
        return requested
    if prefs.get("last_model"):
        return prefs["last_model"]
    if prefs.get("favorite_models"):
        return prefs["favorite_models"][0]
    if models:
        return models[0]["id"]
    return ""


def render_model_picker(models: list[dict], favorites: list[str], selected_model: str, field_name: str, label_text: str) -> str:
    if not models:
        return f"""
<label>
  <span>{e(label_text)}</span>
  <input type="text" name="{e(field_name)}" value="{e(selected_model)}" required>
</label>
"""


def render_primary_model_picker(models: list[dict], favorites: list[str], selected_model: str) -> tuple[str, str]:
    if not favorites:
        if models:
            message = "No favorite models are saved yet, so the primary tab is temporarily showing the full catalog. Add favorites in Settings to keep this list focused."
        else:
            message = "No favorite models are saved yet and the live catalog is unavailable, so enter a model id here for now and save favorites in Settings later."
        return render_model_picker(models, favorites, selected_model, "model", "Favorite model"), message

    favorite_models = [item for item in models if item["id"] in set(favorites)]
    selected = selected_model if selected_model in favorites else favorites[0]
    if selected_model and selected_model not in favorites:
        notice = f"Last selected model {selected_model} is not a favorite yet, so the primary tab is using {selected}."
    else:
        notice = ""
    return render_model_picker(favorite_models, favorites, selected, "model", "Favorite model"), notice

    favorite_set = {model for model in favorites}
    known_ids = {item["id"] for item in models}
    favorite_options = [item for item in models if item["id"] in favorite_set]
    all_other_options = [item for item in models if item["id"] not in favorite_set]

    def option_markup(item: dict) -> str:
        details = []
        if item.get("name") and item["name"] != item["id"]:
            details.append(item["name"])
        if item.get("context_length"):
            details.append(f"{item['context_length']} ctx")
        label = " | ".join(details) if details else item["id"]
        selected_attr = " selected" if item["id"] == selected_model else ""
        return f"<option value=\"{e(item['id'])}\"{selected_attr}>{e(item['id'])} - {e(label)}</option>"

    extra_option = ""
    if selected_model and selected_model not in known_ids:
        extra_option = f"<option value=\"{e(selected_model)}\" selected>{e(selected_model)} - current saved value</option>"

    favorite_markup = "".join(option_markup(item) for item in favorite_options)
    other_markup = "".join(option_markup(item) for item in all_other_options)
    favorites_group = f"<optgroup label=\"Favorites\">{favorite_markup}</optgroup>" if favorite_markup else ""
    catalog_group = f"<optgroup label=\"OpenRouter catalog\">{other_markup}</optgroup>"
    return f"""
<label>
  <span>{e(label_text)}</span>
  <select name="{e(field_name)}" required>
    {extra_option}
    {favorites_group}
    {catalog_group}
  </select>
</label>
"""


def handle_home() -> None:
    ensure_runtime_dirs()
    prefs = load_preferences()
    try:
        models, model_notice = list_available_models()
    except InvalidAPIKeyError as err:
        response_headers()
        print(key_instructions_page(str(err)))
        return
    except AppError as err:
        models, model_notice = [], str(err)

    params = query_params()
    active_tab = "settings" if params.get("tab", "").strip() == "settings" else "change"
    selected_model = selected_model_value(params, prefs, models)
    budget = params.get("budget", str(prefs["last_budget"]))
    description = params.get("description", "")
    validation_command = params.get("check", prefs.get("default_validation_command", ""))
    recent_changes_html = render_recent_changes(list_recent_changes())
    favorites_html = render_favorites(prefs)
    model_warning = f"<p class=\"banner\">{e(model_notice)}</p>" if model_notice else ""
    model_source_note = ""
    if models:
        model_source_note = (
            f"<p class=\"banner\">Loaded {len(models)} current models from "
            "<code>https://openrouter.ai/api/v1/models</code>.</p>"
        )
    primary_model_picker, primary_model_notice = render_primary_model_picker(models, prefs.get("favorite_models", []), selected_model)
    primary_notice_html = f"<p class=\"banner\">{e(primary_model_notice)}</p>" if primary_model_notice else ""
    validation_summary = (
        f"<p class=\"banner\"><strong>Validation command:</strong> <code>{e(validation_command)}</code></p>"
        if validation_command
        else "<p class=\"banner\">Set a default validation command in Settings before submitting a change.</p>"
    )
    submit_disabled = " disabled" if not validation_command else ""
    change_checked = " checked" if active_tab == "change" else ""
    settings_checked = " checked" if active_tab == "settings" else ""
    body = f"""
<section class="tabs">
  <input type="radio" name="home-tab" id="tab-change" class="tab-toggle"{change_checked}>
  <input type="radio" name="home-tab" id="tab-settings" class="tab-toggle"{settings_checked}>
  <div class="tab-strip">
    <label for="tab-change" class="tab-label">Change</label>
    <label for="tab-settings" class="tab-label">Settings</label>
  </div>
  <section class="tab-panel tab-panel-change">
    <section class="card">
      <h2>Request a change</h2>
      <p>Pick a favorite model, describe the change, set the AI budget, and submit.</p>
      {model_warning}
      {primary_notice_html}
      {validation_summary}
      <form method="post" action="/changes" class="stack-form">
        <input type="hidden" name="validation_command" value="{e(validation_command)}">
        {primary_model_picker}
        <label>
          <span>Change description</span>
          <textarea name="description" rows="8" required>{e(description)}</textarea>
        </label>
        <label>
          <span>Total AI call budget</span>
          <input type="number" min="1" max="40" name="ai_budget" value="{e(budget)}" required>
        </label>
        <label class="checkbox-row">
          <input type="checkbox" name="favorite_model" value="1">
          <span>Keep the chosen model in favorites</span>
        </label>
        <button type="submit"{submit_disabled}>Queue change request</button>
      </form>
    </section>
  </section>
  <section class="tab-panel tab-panel-settings">
    <section class="grid">
      <section class="card">
        <h2>Settings</h2>
        <p>Store the validation command and manage how the main tab is preconfigured.</p>
        {model_warning}
        {model_source_note}
        <form method="post" action="/preferences" class="stack-form">
          <input type="hidden" name="action" value="save_settings">
          <label>
            <span>Default validation command</span>
            <input type="text" name="default_validation_command" value="{e(validation_command)}" placeholder="python3 spec/test-suite/run.py">
          </label>
          <button type="submit" class="ghost-button">Save settings</button>
        </form>
        <p><strong>Server root:</strong> <code>{e(target_root())}</code></p>
        <p><strong>Last model:</strong> <code>{e(prefs.get('last_model') or '(none yet)')}</code></p>
        <p><strong>Last budget:</strong> {e(prefs.get('last_budget'))}</p>
      </section>
      <section class="card">
        <h2>Favorite models</h2>
        {favorites_html}
        <form method="post" action="/preferences" class="inline-form">
          <input type="hidden" name="action" value="add">
          {render_model_picker(models, prefs.get("favorite_models", []), selected_model, "model", "Add a favorite model")}
          <button type="submit" class="ghost-button">Add favorite</button>
        </form>
      </section>
    </section>
  </section>
</section>
<section class="card">
  <h2>Recent change requests</h2>
  {recent_changes_html}
</section>
"""
    response_headers()
    print(html_page("AI change assistant", body, subtitle="This fsrouter example uses OpenRouter to plan, review, apply, validate, and document requested filesystem changes."))


def handle_preferences_post() -> None:
    params = form_params()
    action = params.get("action", "").strip()
    model = params.get("model", "").strip()
    default_validation_command = params.get("default_validation_command", "").strip()
    if action in {"add", "remove"} and model:
        update_favorite_model(model, action)
    if action == "save_settings":
        save_settings_preferences(default_validation_command)
    redirect("/?tab=settings")


def handle_change_post() -> None:
    params = form_params()
    description = params.get("description", "").strip()
    validation_command = params.get("validation_command", "").strip()
    model = params.get("model", "").strip()
    favorite = params.get("favorite_model", "") == "1"
    try:
        ai_budget = max(1, min(40, int(params.get("ai_budget", DEFAULT_AI_BUDGET))))
    except ValueError:
        ai_budget = DEFAULT_AI_BUDGET
    if not description or not validation_command or not model:
        response_headers(status=400)
        print(html_page("Missing fields", "<section class=\"card\"><p>Description, validation command, and model are required.</p><p><a href=\"/\">Back</a></p></section>"))
        return
    if not openrouter_api_key():
        response_headers()
        print(key_instructions_page("OPENROUTER_API_KEY is missing."))
        return
    change_id = create_change_request(description, validation_command, model, ai_budget, favorite)
    spawn_worker(change_id)
    redirect(f"/changes/{change_id}")


def render_context_items(change_id: str, items: list[dict]) -> str:
    if not items:
        return "<p class=\"empty\">No context was captured.</p>"
    rows = []
    for index, item in enumerate(items):
        rows.append(
            f"""
<li>
  <a href="{e(route_url('/context', change=change_id, index=index))}">{e(item['path'])}:{e(item['start_line'])}-{e(item['end_line'])}</a>
  <span class="meta">{e(item.get('scope', ''))}</span>
  <p>{e(item.get('reason', ''))}</p>
</li>
"""
        )
    return "<ul class=\"stack-list\">" + "".join(rows) + "</ul>"


def render_change_items(change_id: str, items: list[dict], heading: str) -> str:
    if not items:
        return ""
    rows = []
    for index, item in enumerate(items):
        rows.append(
            f"""
<li>
  <a href="{e(route_url('/diff', change=change_id, index=index))}">{e(item['path'])}</a>
  <span class="status-chip">{e(item['action'])}</span>
  <p>{e(item['description'])}</p>
  <p><a href="{e(item['file_url'])}">Open current file view</a></p>
</li>
"""
        )
    return f"<section class=\"card\"><h2>{e(heading)}</h2><ul class=\"stack-list\">{''.join(rows)}</ul></section>"


def render_events(events: list[dict]) -> str:
    if not events:
        return "<p class=\"empty\">No workflow events yet.</p>"
    rows = []
    for item in events[-30:]:
        rows.append(f"<li><span class=\"meta\">{e(item.get('timestamp', ''))}</span> {e(item.get('message', ''))}</li>")
    return "<ul class=\"stack-list\">" + "".join(rows) + "</ul>"


def render_next_steps(steps: list[dict], request: dict) -> str:
    if not steps:
        return ""
    rows = []
    for item in steps:
        url = route_url(
            "/",
            description=item.get("description", ""),
            check=item.get("check_command", request.get("validation_command", "")),
            model=request.get("model", ""),
            budget=request.get("ai_budget", ""),
        )
        rows.append(
            f"""
<li>
  <a href="{e(url)}">{e(item.get('description', ''))}</a>
  <p>{e(item.get('why', ''))}</p>
</li>
"""
        )
    return f"<section class=\"card\"><h2>Likely next steps</h2><ul class=\"stack-list\">{''.join(rows)}</ul></section>"


def handle_change_detail() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    request = load_request(change_id)
    state = load_state(change_id)
    result = load_result(change_id)
    events = load_events(change_id)

    running = state.get("status") in {"queued", "running"}
    refresh = REFRESH_SECONDS if running else None
    summary = f"Model {request.get('model')} with an AI budget of {request.get('ai_budget')} calls."
    status_card = f"""
<section class="card">
  <h2>Request</h2>
  <p>{e(request.get('description', ''))}</p>
  <p><strong>Status:</strong> <span class="status-chip">{e(state.get('status', 'unknown'))}</span></p>
  <p><strong>Current step:</strong> {e(state.get('current_step', ''))}</p>
  <p><strong>Server root:</strong> <code>{e(request.get('server_root', ''))}</code></p>
  <p><strong>Validation command:</strong> <code>{e(request.get('validation_command', ''))}</code></p>
  <p><a href="/">Start another change</a></p>
</section>
"""
    pieces = [status_card]
    if "existing_evidence" in result:
        evidence = result["existing_evidence"]
        pieces.append(
            f"""
<section class="card">
  <h2>Existing evidence</h2>
  <p>{e(evidence.get('summary', ''))}</p>
</section>
"""
        )
    if result.get("validation_before"):
        pieces.append(render_command_result(result["validation_before"], "Validation before changes"))
    if result.get("context_items"):
        pieces.append(
            f"<section class=\"card\"><h2>Context used for the change</h2>{render_context_items(change_id, result['context_items'])}</section>"
        )
    if result.get("review"):
        review = result["review"]
        issues = review.get("issues", [])
        issues_html = "".join(f"<li>{e(issue)}</li>" for issue in issues) or "<li>No review issues were reported.</li>"
        pieces.append(
            f"""
<section class="card">
  <h2>Review</h2>
  <p>{e(review.get('summary', ''))}</p>
  <p><strong>Approved:</strong> {e(review.get('approved', False))}</p>
  <ul class="stack-list">{issues_html}</ul>
</section>
"""
        )
    if result.get("validation_after"):
        pieces.append(render_command_result(result["validation_after"], "Validation after changes"))
    if result.get("applied_changes"):
        pieces.append(render_change_items(change_id, result["applied_changes"], "Applied changes"))
    if result.get("attempted_changes"):
        pieces.append(render_change_items(change_id, result["attempted_changes"], "Attempted changes"))
    pieces.append(render_next_steps(result.get("next_steps", []), request))
    pieces.append(f"<section class=\"card\"><h2>Workflow events</h2>{render_events(events)}</section>")
    if state.get("error"):
        pieces.append(f"<section class=\"card\"><h2>Error</h2><pre>{e(state['error'])}</pre></section>")
    response_headers()
    print(html_page(f"Change {change_id}", "".join(piece for piece in pieces if piece), subtitle=summary, refresh_seconds=refresh))


def handle_file_view() -> None:
    params = query_params()
    relative = params.get("path", "")
    start = params.get("start", "")
    end = params.get("end", "")
    full_path = resolve_target_path(relative)
    text = read_text_file(full_path)
    line_start = int(start) if start.isdigit() else None
    line_end = int(end) if end.isdigit() else None
    selected_start, selected_end, snippet = slice_lines(text, line_start, line_end)
    numbered = []
    for number, line in enumerate(snippet.splitlines(), start=selected_start):
        numbered.append(f"{number:>5} | {line}")
    body = f"""
<section class="card">
  <h2>{e(relative)}</h2>
  <p><strong>Server root:</strong> <code>{e(target_root())}</code></p>
  <p><strong>Lines:</strong> {e(selected_start)}-{e(selected_end)}</p>
  <pre>{e(chr(10).join(numbered) or '(empty file)')}</pre>
  <p><a href="/">Back</a></p>
</section>
"""
    response_headers()
    print(html_page(relative, body, subtitle="Current filesystem view"))


def handle_context_view() -> None:
    params = query_params()
    change_id = params.get("change", "")
    try:
        index = int(params.get("index", "0"))
    except ValueError:
        index = 0
    result = load_result(change_id)
    items = result.get("context_items", [])
    if index < 0 or index >= len(items):
        raise AppError("Unknown context item.")
    item = items[index]
    body = f"""
<section class="card">
  <h2>{e(item['path'])}:{e(item['start_line'])}-{e(item['end_line'])}</h2>
  <p><strong>Scope:</strong> {e(item.get('scope', ''))}</p>
  <p><strong>Reason:</strong> {e(item.get('reason', ''))}</p>
  <p><a href="{e(item['file_url'])}">Open current file slice</a></p>
  <pre>{e(item.get('content', ''))}</pre>
  <p><a href="/changes/{e(change_id)}">Back to change</a></p>
</section>
"""
    response_headers()
    print(html_page("Context item", body, subtitle="Stored context snapshot used by the workflow"))


def handle_diff_view() -> None:
    params = query_params()
    change_id = params.get("change", "")
    try:
        index = int(params.get("index", "0"))
    except ValueError:
        index = 0
    result = load_result(change_id)
    items = result.get("applied_changes") or result.get("attempted_changes") or []
    if index < 0 or index >= len(items):
        raise AppError("Unknown change diff.")
    item = items[index]
    body = f"""
<section class="card">
  <h2>{e(item['path'])}</h2>
  <p><strong>Action:</strong> {e(item['action'])}</p>
  <p>{e(item['description'])}</p>
  <p><a href="{e(item['file_url'])}">Open current file</a></p>
  <pre>{e(item.get('diff', '(no diff available)'))}</pre>
  <p><a href="/changes/{e(change_id)}">Back to change</a></p>
</section>
"""
    response_headers()
    print(html_page("Change diff", body, subtitle="Stored diff for this requested change"))


def render_error_page(err: Exception) -> None:
    response_headers(status=500)
    print(
        html_page(
            "Error",
            f"<section class=\"card\"><p>{e(str(err))}</p><pre>{e(traceback.format_exc())}</pre><p><a href=\"/\">Back</a></p></section>",
            subtitle="The example app hit an unexpected error.",
        )
    )


def worker_main(change_id: str) -> int:
    try:
        run_workflow(change_id)
    except Exception as err:
        state = load_state(change_id)
        state["status"] = "error"
        state["error"] = f"{err}\n\n{traceback.format_exc()}"
        state["current_step"] = "Failed"
        write_json_atomic(state_path_for(change_id), state)
        append_event(change_id, f"Workflow failed: {err}", level="error")
        return 1
    return 0
