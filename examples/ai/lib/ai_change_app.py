#!/usr/bin/env python3
from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import re
import shlex
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
LOGS_ROOT = APP_ROOT / "logs"
AI_LOG_ROOT = LOGS_ROOT / "ai"
PROMPT_ROOT = APP_ROOT / "prompts"
DEFAULT_AI_BUDGET = 8
DEFAULT_COMMAND_TIMEOUT = 120
MAX_CONTEXT_ITEMS = 12
MAX_CONTEXT_LINES = 250
MAX_CONTEXT_CHARS = 14000
MAX_MANIFEST_ITEMS = 2000
MAX_CHANGE_FILES = 20
MAX_RECURSION_DEPTH = 2
MAX_VALIDATION_COMMAND_ATTEMPTS = 3
MAX_VALIDATION_RISK_SCORE = 2.0
DEFAULT_STRATEGY_RISK_THRESHOLD = 6.0
MODEL_FETCH_TIMEOUT = 8
CHAT_TIMEOUT = 60
REFRESH_SECONDS = 3
PROTECTED_PATH_PARTS = {".git"}
PROTECTED_RELATIVE_PREFIXES = {"examples/ai/data/", "examples/ai/logs/"}
ALLOWED_VALIDATION_COMMANDS = (
    ("pytest",),
    ("go", "test"),
    ("cargo", "test"),
    ("deno", "test"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("npm", "run", "build"),
    ("pnpm", "test"),
    ("pnpm", "run", "test"),
    ("pnpm", "run", "build"),
    ("yarn", "test"),
    ("yarn", "run", "test"),
    ("yarn", "run", "build"),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("python", "-m", "py_compile"),
    ("python3", "-m", "py_compile"),
    ("python", "spec/test-suite/run.py"),
    ("python3", "spec/test-suite/run.py"),
)


class AppError(Exception):
    pass


class InvalidAPIKeyError(AppError):
    pass


class BudgetExceededError(AppError):
    pass


class RiskReviewRequired(AppError):
    pass


class OpenRouterChatError(AppError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


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
    AI_LOG_ROOT.mkdir(parents=True, exist_ok=True)


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


def json_pretty(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def query_params() -> dict[str, str]:
    parsed = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def form_params() -> dict[str, str]:
    body = sys.stdin.buffer.read()
    content_type = os.environ.get("CONTENT_TYPE", "").lower()
    if "application/json" in content_type:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as err:
            raise AppError(f"Invalid JSON body: {err}") from err
        if not isinstance(payload, dict):
            raise AppError("JSON request body must be an object.")
        return {str(key): "" if value is None else str(value) for key, value in payload.items()}
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def emit_json(payload: dict | list, *, status: int = 200) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
    if status >= 500:
        raise SystemExit(2)
    if status >= 400:
        raise SystemExit(1)


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


def route_url(base_path: str, **params: str | int | None) -> str:
    items = [(key, str(value)) for key, value in params.items() if value is not None and value != ""]
    if not items:
        return base_path
    return f"{base_path}?{urllib.parse.urlencode(items)}"


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


def redacted_openrouter_headers() -> dict[str, str]:
    headers = openrouter_headers()
    if "Authorization" in headers:
        headers["Authorization"] = "Bearer [redacted]"
    return headers


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


def openrouter_chat(model: str, system_prompt: str, user_prompt: str) -> dict:
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
    details = {
        "request": {
            "method": "POST",
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "headers": redacted_openrouter_headers(),
            "json": payload,
        }
    }
    try:
        with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as response:
            response_text = response.read().decode("utf-8")
            raw = json.loads(response_text)
            details["response"] = {
                "status": response.status,
                "headers": dict(response.headers.items()),
                "body": response_text,
                "json": raw,
            }
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        details["response"] = {
            "status": err.code,
            "headers": dict(err.headers.items()),
            "body": detail,
        }
        if err.code in (401, 403):
            raise OpenRouterChatError("OpenRouter rejected the supplied API key.", details) from err
        raise OpenRouterChatError(f"OpenRouter chat request failed with HTTP {err.code}: {detail}", details) from err
    except urllib.error.URLError as err:
        details["response"] = {"error": str(err.reason)}
        raise OpenRouterChatError(f"Could not reach OpenRouter: {err.reason}", details) from err

    choices = raw.get("choices") or []
    if not choices:
        raise OpenRouterChatError("OpenRouter returned no choices.", details)
    message = choices[0].get("message", {})
    content = flatten_openrouter_content(message.get("content"))
    if not content:
        raise OpenRouterChatError("OpenRouter returned an empty completion.", details)
    details["assistant_message"] = content
    return details


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
            if name not in ignored_dirs
            and f"{relative_root}/{name}".strip("/").lower() not in {"examples/ai/data", "examples/ai/logs"}
        ]
        for filename in sorted(filenames):
            full_path = current_path / filename
            relative = full_path.relative_to(root).as_posix()
            if relative.lower().startswith("examples/ai/data/"):
                continue
            if relative.lower().startswith("examples/ai/logs/"):
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


def run_check_command(command: str, *, timeout_override: int | None = None) -> CommandResult:
    start = time.time()
    shell = os.environ.get("SHELL", "/bin/sh")
    timeout = timeout_override or int(os.environ.get("AI_CHANGE_COMMAND_TIMEOUT", str(DEFAULT_COMMAND_TIMEOUT)) or DEFAULT_COMMAND_TIMEOUT)
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


def _validation_command_segments(command: str) -> list[str]:
    candidate = command.strip()
    if not candidate:
        raise AppError("Generated validation command was empty.")
    if any(fragment in candidate for fragment in ("|", "||", ";", ">", "<", "`", "$(", "\n", "\r")):
        raise AppError("Generated validation command used blocked shell syntax.")
    return [segment.strip() for segment in candidate.split("&&") if segment.strip()]


def _tokens_match_prefix(tokens: list[str], prefix: tuple[str, ...]) -> bool:
    return len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == prefix


def _is_allowed_validation_tokens(tokens: list[str]) -> bool:
    for prefix in ALLOWED_VALIDATION_COMMANDS:
        if _tokens_match_prefix(tokens, prefix):
            return True
    return False


def validation_command_allowlist_issue(command: str) -> str | None:
    try:
        segments = _validation_command_segments(command)
    except AppError as err:
        return str(err)
    if not segments:
        return "Generated validation command did not include a runnable segment."
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError as err:
            return f"Could not parse validation command segment: {err}"
        if not tokens:
            return "Generated validation command contained an empty segment."
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            return "Environment variable assignments are not allowed in generated validation commands."
        if not _is_allowed_validation_tokens(tokens):
            return f"Segment is outside the strict validation allowlist: {segment}"
    return None


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


def ai_log_dir_for(change_id: str) -> Path:
    return AI_LOG_ROOT / change_id


def ai_log_path_for(change_id: str, call_id: str) -> Path:
    return ai_log_dir_for(change_id) / f"{call_id}.json"


def ai_call_url(change_id: str, call_id: str) -> str:
    return route_url("/ai-call", change=change_id, call=call_id)


def display_log_path(path: Path) -> str:
    try:
        return path.relative_to(APP_ROOT.parent.parent).as_posix()
    except ValueError:
        return str(path)


def served_app_url_for_path(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(APP_ROOT.resolve())
    except ValueError:
        return ""
    return "/" + relative.as_posix()


def served_app_path(value: str) -> str:
    raw = str(value).strip()
    if not raw or raw.startswith("/"):
        return ""
    try:
        repo_relative = (APP_ROOT.parent.parent / raw).resolve()
    except OSError:
        return ""
    return served_app_url_for_path(repo_relative)


def direct_target_file_url(relative_path: str) -> str:
    try:
        return served_app_url_for_path(resolve_target_path(relative_path))
    except AppError:
        return ""


def target_file_url(relative_path: str, *, start: int | None = None, end: int | None = None) -> str:
    direct_url = direct_target_file_url(relative_path)
    if direct_url:
        return direct_url
    return route_url("/file", path=relative_path, start=start, end=end)


def default_result() -> dict:
    return {
        "context_items": [],
        "applied_changes": [],
        "attempted_changes": [],
        "next_steps": [],
        "decomposition": [],
        "review": {},
        "risk_assessments": [],
    }


def command_result_from_dict(payload: dict) -> CommandResult:
    return CommandResult(
        command=str(payload.get("command", "")),
        exit_code=int(payload.get("exit_code", 0)),
        stdout=str(payload.get("stdout", "")),
        stderr=str(payload.get("stderr", "")),
        duration_seconds=float(payload.get("duration_seconds", 0)),
    )


def latest_risk_assessment(result: dict) -> dict:
    items = result.get("risk_assessments", [])
    if isinstance(items, list) and items:
        latest = items[-1]
        if isinstance(latest, dict):
            return latest
    return {}


def reset_result_for_retry(result: dict) -> dict:
    fresh = default_result()
    previous_assessments = result.get("risk_assessments", [])
    if isinstance(previous_assessments, list) and previous_assessments:
        fresh["risk_assessments"] = previous_assessments
    return fresh


def load_ai_calls(change_id: str) -> list[dict]:
    items: list[dict] = []
    path = ai_calls_path_for(change_id)
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


def load_ai_call_log(change_id: str, call_id: str) -> dict:
    payload = read_json(ai_log_path_for(change_id, call_id), {})
    if payload:
        return payload
    raise AppError("Unknown AI call log.")


def load_request(change_id: str) -> dict:
    request = read_json(request_path_for(change_id), {})
    if not request:
        raise AppError(f"Unknown change id: {change_id}")
    return request


def load_state(change_id: str) -> dict:
    return read_json(state_path_for(change_id), {})


def load_result(change_id: str) -> dict:
    return read_json(result_path_for(change_id), default_result())


def save_state(change_id: str, state: dict) -> None:
    write_json_atomic(state_path_for(change_id), state)


def save_result(change_id: str, result: dict) -> None:
    write_json_atomic(result_path_for(change_id), result)


def save_request(change_id: str, request: dict) -> None:
    write_json_atomic(request_path_for(change_id), request)


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


def create_change_request(description: str, model: str, ai_budget: int, favorite_model: bool) -> str:
    ensure_runtime_dirs()
    change_id = f"{now_slug()}-{uuid.uuid4().hex[:6]}"
    directory = change_dir(change_id)
    directory.mkdir(parents=True, exist_ok=True)
    request = {
        "id": change_id,
        "description": description,
        "strategy_notes": "",
        "allow_high_risk_strategy": False,
        "validation_command": "",
        "validation_command_source": "auto_generated",
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
    result = default_result()
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

    def strategy_notes(self) -> str:
        return str(self.request.get("strategy_notes", "")).strip()

    def persist(self) -> None:
        self.state["ai_calls_used"] = self.ai_calls_used
        self.state["updated_at"] = utc_timestamp()
        save_state(self.change_id, self.state)
        save_result(self.change_id, self.result)

    def persist_request(self) -> None:
        save_request(self.change_id, self.request)

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
        call_number = self.ai_calls_used + 1
        call_id = f"{call_number:03d}-{re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')}"
        raw_response = ""
        call_details: dict = {}
        error_message = ""
        try:
            call_details = openrouter_chat(self.model, system_prompt, user_prompt)
            raw_response = str(call_details.get("assistant_message", ""))
        except OpenRouterChatError as err:
            call_details = err.details
            error_message = str(err)
            if "rejected the supplied API key" in error_message.lower():
                error_message = "OpenRouter rejected the supplied API key."
        self.ai_calls_used += 1
        self.persist()
        log_record = {
            "timestamp": utc_timestamp(),
            "change_id": self.change_id,
            "call_id": call_id,
            "call_number": call_number,
            "prompt": stem,
            "model": self.model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "assistant_message": raw_response,
            "request": call_details.get("request", {}),
            "response": call_details.get("response", {}),
            "log_path": display_log_path(ai_log_path_for(self.change_id, call_id)),
        }
        if error_message:
            log_record["error"] = error_message
        write_json_atomic(ai_log_path_for(self.change_id, call_id), log_record)
        append_jsonl(
            ai_calls_path_for(self.change_id),
            {
                "timestamp": log_record["timestamp"],
                "call_id": call_id,
                "call_number": call_number,
                "prompt": stem,
                "model": self.model,
                "assistant_message": raw_response,
                "log_path": log_record["log_path"],
                "url": ai_call_url(self.change_id, call_id),
                "error": error_message,
            },
        )
        if error_message:
            if "rejected the supplied api key" in error_message.lower():
                raise InvalidAPIKeyError(error_message)
            raise AppError(error_message)
        payload = extract_json(raw_response)
        if isinstance(payload, dict):
            payload["__ai_call_id"] = call_id
            payload["__ai_call_url"] = ai_call_url(self.change_id, call_id)
            payload["__ai_log_path"] = log_record["log_path"]
        return payload

    def _validation_generation_rejections(self, attempts: list[dict]) -> str:
        lines: list[str] = []
        for item in attempts:
            reason = str(item.get("rejection_reason", "")).strip()
            if not reason:
                continue
            command = str(item.get("candidate_command", "")).strip() or "(empty command)"
            lines.append(f"- Attempt {item.get('attempt')}: {command} -> {reason}")
        return "\n".join(lines) if lines else "(none)"

    def _string_list(self, raw_items, *, limit: int = 8) -> list[str]:
        items: list[str] = []
        if isinstance(raw_items, list):
            for item in raw_items[:limit]:
                text = str(item).strip()
                if text:
                    items.append(text)
        return items

    def _score_validation_command_risk(self, payload: dict) -> tuple[float, str, list[str]]:
        raw_score = payload.get("risk_score", payload.get("score"))
        if isinstance(raw_score, str):
            raw_score = raw_score.strip()
        try:
            risk_score = float(raw_score)
        except (TypeError, ValueError) as err:
            raise AppError("Validation risk scoring did not return a numeric score.") from err
        if risk_score < 0 or risk_score > 10:
            raise AppError(f"Validation risk score must be between 0 and 10, got {risk_score}.")
        summary = str(payload.get("summary", payload.get("reason", ""))).strip()
        concerns = self._string_list(payload.get("concerns", []))
        return risk_score, summary, concerns

    def assess_strategy_risk(
        self,
        description: str,
        validation_before: CommandResult,
        context_items: list[dict],
        plan: dict,
        depth: int,
    ) -> dict:
        subchange_lines: list[str] = []
        raw_subchanges = plan.get("subchanges", [])
        if isinstance(raw_subchanges, list):
            for item in raw_subchanges[:3]:
                sub_description = str(item.get("description", "")).strip()
                title = str(item.get("title", "")).strip()
                if title and sub_description:
                    subchange_lines.append(f"- {title}: {sub_description}")
                elif sub_description:
                    subchange_lines.append(f"- {sub_description}")
        payload = self.ai_json(
            "strategy_risk",
            {
                "CHANGE_DESCRIPTION": description,
                "STRATEGY_NOTES": self.strategy_notes() or "(none)",
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "PLAN_ACTION": str(plan.get("action", "implement")).strip() or "implement",
                "PLAN_REASON": str(plan.get("reason", "")).strip() or "(none provided)",
                "PLAN_SUBCHANGES": "\n".join(subchange_lines) or "(none)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )
        risk_score, summary, concerns = self._score_validation_command_risk(payload)
        bypass_strategies = self._string_list(payload.get("bypass_strategies", payload.get("alternatives", [])))
        assessment = {
            "created_at": utc_timestamp(),
            "description": description,
            "depth": depth,
            "threshold": DEFAULT_STRATEGY_RISK_THRESHOLD,
            "risk_score": round(risk_score, 2),
            "summary": summary,
            "concerns": concerns,
            "bypass_strategies": bypass_strategies,
            "plan_action": str(plan.get("action", "implement")).strip() or "implement",
            "plan_reason": str(plan.get("reason", "")).strip(),
            "plan_subchanges": raw_subchanges if isinstance(raw_subchanges, list) else [],
            "strategy_notes": self.strategy_notes(),
            "ai_call_id": payload.get("__ai_call_id", ""),
            "status": "accepted" if risk_score <= DEFAULT_STRATEGY_RISK_THRESHOLD else "needs_user_review",
        }
        self.result.setdefault("risk_assessments", []).append(assessment)
        self.persist()
        self.log(
            f"Strategy risk assessment scored {risk_score:.2f} at depth {depth}"
            + (" and requires user review." if risk_score > DEFAULT_STRATEGY_RISK_THRESHOLD else ".")
        )
        return assessment

    def generate_validation_command(self) -> CommandResult:
        generation = {
            "max_attempts": MAX_VALIDATION_COMMAND_ATTEMPTS,
            "risk_threshold": MAX_VALIDATION_RISK_SCORE,
            "attempts": [],
        }
        self.result["validation_command_generation"] = generation
        self.persist()

        manifest = build_file_manifest()
        attempts = generation["attempts"]
        for attempt in range(1, MAX_VALIDATION_COMMAND_ATTEMPTS + 1):
            payload = self.ai_json(
                "validation_command",
                {
                    "CHANGE_DESCRIPTION": self.request["description"],
                    "SERVER_ROOT": str(target_root()),
                    "FILE_MANIFEST": manifest,
                    "ATTEMPT_NUMBER": str(attempt),
                    "MAX_ATTEMPTS": str(MAX_VALIDATION_COMMAND_ATTEMPTS),
                    "PREVIOUS_REJECTIONS": self._validation_generation_rejections(attempts),
                    "AI_CALLS_REMAINING": str(self.remaining_budget()),
                },
            )
            command = str(payload.get("command", "")).strip()
            generator_reason = str(payload.get("reason", payload.get("summary", ""))).strip()
            attempt_record: dict = {
                "attempt": attempt,
                "candidate_command": command,
                "generator_reason": generator_reason,
                "generator_ai_call_id": payload.get("__ai_call_id", ""),
            }
            issue = validation_command_allowlist_issue(command)
            if issue:
                attempt_record["accepted"] = False
                attempt_record["rejection_reason"] = issue
                attempts.append(attempt_record)
                self.persist()
                self.log(f"Rejected generated validation command attempt {attempt}: {issue}", level="error")
                continue

            risk_payload = self.ai_json(
                "validation_risk",
                {
                    "CHANGE_DESCRIPTION": self.request["description"],
                    "VALIDATION_COMMAND": command,
                    "GENERATION_REASON": generator_reason or "(none provided)",
                    "SERVER_ROOT": str(target_root()),
                    "ATTEMPT_NUMBER": str(attempt),
                    "MAX_ATTEMPTS": str(MAX_VALIDATION_COMMAND_ATTEMPTS),
                    "PREVIOUS_REJECTIONS": self._validation_generation_rejections(attempts),
                    "AI_CALLS_REMAINING": str(self.remaining_budget()),
                },
            )
            risk_score, risk_summary, risk_concerns = self._score_validation_command_risk(risk_payload)
            attempt_record["risk_score"] = round(risk_score, 2)
            attempt_record["risk_summary"] = risk_summary
            attempt_record["risk_ai_call_id"] = risk_payload.get("__ai_call_id", "")
            if risk_concerns:
                attempt_record["risk_concerns"] = risk_concerns
            if risk_score > MAX_VALIDATION_RISK_SCORE:
                issue = f"Risk score {risk_score:.2f} exceeded max {MAX_VALIDATION_RISK_SCORE:.2f}."
                attempt_record["accepted"] = False
                attempt_record["rejection_reason"] = issue
                attempts.append(attempt_record)
                self.persist()
                self.log(f"Rejected generated validation command attempt {attempt}: {issue}", level="error")
                continue

            preflight = run_check_command(command)
            attempt_record["preflight_result"] = preflight.to_dict()
            issue = validation_command_runtime_issue(command, preflight)
            if issue:
                attempt_record["accepted"] = False
                attempt_record["rejection_reason"] = issue
                attempts.append(attempt_record)
                self.persist()
                self.log(f"Rejected generated validation command attempt {attempt}: {issue}", level="error")
                continue
            if preflight.exit_code == 0:
                issue = "Preflight passed before edits; command is not specific enough to validate the requested change."
                attempt_record["accepted"] = False
                attempt_record["rejection_reason"] = issue
                attempts.append(attempt_record)
                self.persist()
                self.log(f"Rejected generated validation command attempt {attempt}: {issue}", level="error")
                continue

            attempt_record["accepted"] = True
            attempts.append(attempt_record)
            generation["accepted_command"] = command
            generation["accepted_risk_score"] = round(risk_score, 2)
            generation["accepted_risk_summary"] = risk_summary
            generation["accepted_preflight"] = preflight.to_dict()
            generation["accepted_ai_call_id"] = attempt_record["generator_ai_call_id"]
            generation["accepted_risk_ai_call_id"] = attempt_record["risk_ai_call_id"]
            self.request["validation_command"] = command
            self.request["validation_command_source"] = "generated"
            self.persist_request()
            self.persist()
            self.log(f"Accepted generated validation command on attempt {attempt}: {command}")
            return preflight

        reasons = [item.get("rejection_reason", "") for item in attempts if item.get("rejection_reason")]
        failure_reason = "No acceptable validation command was generated within 3 attempts."
        if reasons:
            failure_reason += f" Last rejection: {reasons[-1]}"
        generation["failure_reason"] = failure_reason
        self.persist()
        raise AppError(failure_reason)

    def choose_context(self, description: str, validation_before: CommandResult, depth: int) -> list[dict]:
        manifest = build_file_manifest()
        payload = self.ai_json(
            "context_selector",
            {
                "CHANGE_DESCRIPTION": description,
                "STRATEGY_NOTES": self.strategy_notes() or "(none)",
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
        call_id = str(payload.get("__ai_call_id", "")).strip()
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
                    "file_url": target_file_url(path),
                    "ai_call_id": call_id,
                }
            )
        if not selected:
            raise AppError("No valid filesystem context could be loaded from the AI response.")
        self.result.setdefault("context_items", []).extend(selected)
        self.persist()
        self.log(f"Selected {len(selected)} context item(s) for: {description}")
        return selected

    def plan(self, description: str, validation_before: CommandResult, context_items: list[dict], depth: int) -> dict:
        plan = self.ai_json(
            "plan_change",
            {
                "CHANGE_DESCRIPTION": description,
                "STRATEGY_NOTES": self.strategy_notes() or "(none)",
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )
        if isinstance(plan, dict):
            plan["__ai_call_id"] = str(plan.get("__ai_call_id", "")).strip()
        return plan

    def implement(self, description: str, validation_before: CommandResult, context_items: list[dict], depth: int) -> list[dict]:
        payload = self.ai_json(
            "implement_change",
            {
                "CHANGE_DESCRIPTION": description,
                "STRATEGY_NOTES": self.strategy_notes() or "(none)",
                "VALIDATION_COMMAND": validation_before.command,
                "VALIDATION_EXIT_CODE": str(validation_before.exit_code),
                "VALIDATION_STDOUT": validation_before.stdout or "(empty)",
                "VALIDATION_STDERR": validation_before.stderr or "(empty)",
                "CONTEXT_BLOCKS": render_context_blocks(context_items),
                "AI_CALLS_REMAINING": str(self.remaining_budget()),
                "RECURSION_DEPTH": str(depth),
            },
        )
        call_id = str(payload.get("__ai_call_id", "")).strip()
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
                    "ai_call_id": call_id,
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
                "STRATEGY_NOTES": self.strategy_notes() or "(none)",
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
        review = {"approved": approved, "summary": summary, "issues": issues, "ai_call_id": payload.get("__ai_call_id", "")}
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
        call_id = str(payload.get("__ai_call_id", "")).strip()
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
                        "ai_call_id": call_id,
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
        "file_url": target_file_url(item["path"]),
        "ai_call_id": item.get("ai_call_id", ""),
    }


def validation_command_runtime_issue(command: str, preflight: CommandResult) -> str:
    stderr = (preflight.stderr or "").strip()
    lowered = stderr.lower()
    executable = ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if parts:
        executable = parts[0]

    if preflight.exit_code in {126, 127}:
        if executable:
            return f"Executable not found or not runnable for validation command: {executable}"
        return "Validation command could not be executed in this environment."

    runtime_markers = (
        "command not found",
        "no such file or directory",
        "permission denied",
        "operation not permitted",
        "not recognized as an internal or external command",
    )
    if any(marker in lowered for marker in runtime_markers):
        if executable:
            return f"Executable not found or not runnable for validation command: {executable}"
        return f"Validation command could not be executed: {stderr or 'runtime lookup failure'}"
    return ""


def process_change_contract_issues(item: dict) -> list[str]:
    issues: list[str] = []
    if item["path"] != "examples/ai/bin/process_change.py":
        return issues
    if item["action"] == "delete":
        issues.append("Refusing to delete critical process_change entrypoint: examples/ai/bin/process_change.py")
        return issues
    after_content = item.get("after_content", "") or ""
    if 'if len(sys.argv) != 2:' not in after_content:
        issues.append("Refusing to remove required change-id CLI contract from examples/ai/bin/process_change.py")
    if 'worker_main(sys.argv[1])' not in after_content:
        issues.append("Refusing to remove worker_main invocation from examples/ai/bin/process_change.py")
    return issues


def local_review_issues(prepared_changes: list[dict]) -> list[str]:
    issues: list[str] = []
    for item in prepared_changes:
        path = item["path"].lower()
        if path.startswith(".git/"):
            issues.append(f"Refusing to modify repository metadata: {item['path']}")
        if path.startswith("examples/ai/data/"):
            issues.append(f"Refusing to modify runtime data: {item['path']}")
        if path.startswith("examples/ai/logs/"):
            issues.append(f"Refusing to modify runtime logs: {item['path']}")
        issues.extend(process_change_contract_issues(item))
    return issues


def review_rejection_message(review: dict, local_issues: list[str]) -> str:
    summary = str(review.get("summary", "")).strip() or "The proposed changes were not approved during review."
    issues = list(review.get("issues", []))
    if local_issues:
        issues.extend(local_issues)
    if not issues:
        return summary
    return summary + "\n\nReview issues:\n- " + "\n- ".join(str(issue).strip() for issue in issues if str(issue).strip())


def queue_change_retry(change_id: str, request: dict, state: dict, result: dict, *, message: str) -> None:
    request["validation_command"] = ""
    request["validation_command_source"] = "auto_generated"
    save_request(change_id, request)
    state["status"] = "queued"
    state["current_step"] = "Queued"
    state["error"] = ""
    state["updated_at"] = utc_timestamp()
    state.pop("completed_at", None)
    save_state(change_id, state)
    save_result(change_id, reset_result_for_retry(result))
    append_event(change_id, message)


def execute_planned_scope(
    workflow: Workflow,
    description: str,
    validation_before: CommandResult,
    depth: int,
    applied_changes: list[dict],
    context_items: list[dict],
    plan: dict,
) -> list[dict]:
    action = str(plan.get("action", "implement")).strip().lower()
    if action == "decompose" and depth < MAX_RECURSION_DEPTH:
        subchanges = plan.get("subchanges", [])
        if isinstance(subchanges, list) and 1 < len(subchanges) <= 3 and workflow.remaining_budget() >= len(subchanges) * 2:
            decomposition_record = {
                "description": description,
                "subchanges": subchanges,
                "depth": depth,
                "ai_call_id": plan.get("__ai_call_id", ""),
            }
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
    if local_issues or not review.get("approved"):
        rejection_message = review_rejection_message(review, local_issues)
        review["rejection_message"] = rejection_message
        workflow.result["review"] = review
        workflow.persist()
        raise AppError(rejection_message)
    workflow.set_step(f"Applying approved changes for depth {depth}")
    apply_prepared_changes(prepared)
    applied_changes.extend(prepared)
    workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
    workflow.persist()
    workflow.log(f"Applied {len(prepared)} change(s) for scope: {description}")
    return prepared


def execute_change_scope(workflow: Workflow, description: str, validation_before: CommandResult, depth: int, applied_changes: list[dict]) -> list[dict]:
    workflow.set_step(f"Gathering context for depth {depth}")
    context_items = workflow.choose_context(description, validation_before, depth)

    workflow.set_step(f"Planning change for depth {depth}")
    plan = workflow.plan(description, validation_before, context_items, depth)
    if depth == 0:
        workflow.set_step("Assessing implementation risk")
        assessment = workflow.assess_strategy_risk(description, validation_before, context_items, plan, depth)
        if assessment["risk_score"] > DEFAULT_STRATEGY_RISK_THRESHOLD and not workflow.request.get("allow_high_risk_strategy"):
            workflow.result["pending_risk_review"] = {
                "description": description,
                "depth": depth,
                "validation_before": validation_before.to_dict(),
                "context_items": context_items,
                "plan": plan,
            }
            workflow.state["status"] = "awaiting_risk_review"
            workflow.state["current_step"] = "Awaiting risk review"
            workflow.state["error"] = ""
            workflow.persist()
            workflow.log(
                f"Paused before implementation because strategy risk {assessment['risk_score']:.2f} exceeded the default threshold "
                f"of {DEFAULT_STRATEGY_RISK_THRESHOLD:.2f}.",
                level="error",
            )
            raise RiskReviewRequired("Awaiting user review for elevated strategy risk.")
        if assessment["risk_score"] > DEFAULT_STRATEGY_RISK_THRESHOLD:
            assessment["status"] = "accepted_by_override"
            assessment["decision_summary"] = "The user chose to continue despite the elevated strategy risk."
        else:
            assessment["status"] = "accepted"
            assessment["decision_summary"] = "The strategy stayed within the default risk threshold."
        workflow.result.pop("pending_risk_review", None)
        workflow.persist()

    return execute_planned_scope(workflow, description, validation_before, depth, applied_changes, context_items, plan)


def resume_after_risk_override(workflow: Workflow) -> None:
    pending = workflow.result.get("pending_risk_review", {})
    if not isinstance(pending, dict) or not pending:
        raise AppError("No paused risk-reviewed plan is available to resume.")
    before_payload = pending.get("validation_before", {})
    if not isinstance(before_payload, dict) or not before_payload.get("command"):
        raise AppError("The paused risk-reviewed plan is missing its validation context.")
    plan = pending.get("plan", {})
    if not isinstance(plan, dict) or not plan:
        raise AppError("The paused risk-reviewed plan is missing its execution plan.")
    context_items = pending.get("context_items", [])
    if not isinstance(context_items, list) or not context_items:
        raise AppError("The paused risk-reviewed plan is missing its selected context.")

    latest = latest_risk_assessment(workflow.result)
    if latest:
        latest["status"] = "accepted_by_override"
        latest["decision_summary"] = "The user chose to continue despite the elevated strategy risk."
    workflow.result.pop("pending_risk_review", None)
    workflow.persist()
    workflow.log("Resuming the paused plan after user override of the strategy risk warning.")

    before = command_result_from_dict(before_payload)
    applied_changes: list[dict] = []
    try:
        execute_planned_scope(
            workflow,
            str(pending.get("description", workflow.request["description"])),
            before,
            int(pending.get("depth", 0)),
            applied_changes,
            context_items,
            plan,
        )
    except Exception:
        if applied_changes:
            rollback_prepared_changes(applied_changes)
            workflow.log("An error occurred after resuming the paused plan; rolled the filesystem back.", level="error")
            workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
            workflow.result["rolled_back_after_error"] = True
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


def run_workflow(change_id: str) -> None:
    ensure_runtime_dirs()
    workflow = Workflow(change_id)
    if workflow.request.get("allow_high_risk_strategy") and workflow.result.get("pending_risk_review"):
        workflow.set_status("running")
        resume_after_risk_override(workflow)
        return
    workflow.set_status("running")
    workflow.set_step("Generating a validation command")
    try:
        before = workflow.generate_validation_command()
    except AppError as err:
        workflow.log(f"Validation command generation failed: {err}", level="error")
        workflow.set_status("error", error=str(err))
        return

    workflow.set_step("Checking whether the change already exists")
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
    except RiskReviewRequired:
        return
    except Exception as err:
        if applied_changes:
            rollback_prepared_changes(applied_changes)
            if isinstance(err, AppError) and workflow.result.get("review", {}).get("approved") is False:
                workflow.log("A later subchange was rejected during review; rolled back intermediate approved changes.", level="error")
            else:
                workflow.log("An error occurred after applying intermediate changes; rolled the filesystem back.", level="error")
            workflow.result["attempted_changes"] = [prepared_change_for_result(item) for item in applied_changes]
            workflow.result["rolled_back_after_error"] = True
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


def starter_prompt_dir() -> Path:
    return APP_ROOT / "starter-prompts"


def starter_prompt_specs() -> list[dict]:
    return [
        {
            "id": "cli-json-dry-run",
            "title": "Add --json and --dry-run to a render CLI",
            "difficulty": "Easy",
            "tech_focus": "Command line app",
            "suggested_budget": 3,
            "validation_command": "./bin/render --help && ./bin/render sample-manifest.json --dry-run --json",
            "why": "Shows how the app can make a small, structured CLI improvement with help text, flags, and stable output.",
            "repo_label": "Generic CLI project",
            "file": "cli-json-dry-run.txt",
        },
        {
            "id": "html-responsive-preview",
            "title": "Make a preview page responsive and accessible",
            "difficulty": "Easy",
            "tech_focus": "HTML",
            "suggested_budget": 3,
            "validation_command": "npm run build",
            "why": "Shows a focused front-end task with semantic markup, mobile layout, and visible UX polish.",
            "repo_label": "Generic web app",
            "file": "html-responsive-preview.txt",
        },
        {
            "id": "js-localstorage-settings",
            "title": "Persist recent template settings in localStorage",
            "difficulty": "Medium",
            "tech_focus": "JavaScript",
            "suggested_budget": 5,
            "validation_command": "npm test",
            "why": "Shows client-side state management and a reset flow without needing a backend migration.",
            "repo_label": "Generic browser app",
            "file": "js-localstorage-settings.txt",
        },
        {
            "id": "api-job-status",
            "title": "Add progress metadata to a job status API",
            "difficulty": "Medium",
            "tech_focus": "Web API",
            "suggested_budget": 5,
            "validation_command": "pytest tests/test_jobs_api.py",
            "why": "Shows how the app can extend a backend API contract and tighten response structure.",
            "repo_label": "Generic service/API project",
            "file": "api-job-status.txt",
        },
        {
            "id": "fullstack-live-progress",
            "title": "Show live progress for long-running jobs",
            "difficulty": "Medium",
            "tech_focus": "HTML + JavaScript + web API",
            "suggested_budget": 6,
            "validation_command": "npm test && npm run build",
            "why": "Shows a cross-layer change that links API polling to browser UI updates and user feedback.",
            "repo_label": "Generic full-stack app",
            "file": "fullstack-live-progress.txt",
        },
        {
            "id": "cli-api-batch-mode",
            "title": "Add batch submission with concurrency and resume",
            "difficulty": "Hard",
            "tech_focus": "Command line app + web API",
            "suggested_budget": 8,
            "validation_command": "pytest tests/test_batch_mode.py",
            "why": "Shows orchestration work with manifests, retry behavior, and long-running job handling.",
            "repo_label": "Generic automation pipeline",
            "file": "cli-api-batch-mode.txt",
        },
        {
            "id": "html-to-video-storyboard",
            "title": "Add storyboard mode to HTML-to-video-pipeline",
            "difficulty": "Hard",
            "tech_focus": "HTML + JavaScript + command line app + web API",
            "suggested_budget": 10,
            "validation_command": "npm test && node cli/render.js examples/storyboard.json --dry-run",
            "why": "Shows a self-referential, iterative example that spans preview rendering, manifests, and promotion into a full render flow.",
            "repo_label": "HTML-to-video-pipeline",
            "repo_url": "https://github.com/curtcox/HTML-to-video-pipeline",
            "file": "html-to-video-storyboard.txt",
        },
        {
            "id": "qr-reader-workflow",
            "title": "Add a QR code reader workflow to the app",
            "difficulty": "Hard",
            "tech_focus": "HTML + JavaScript + browser APIs + app integration",
            "suggested_budget": 8,
            "validation_command": "python3 -m py_compile examples/ai/lib/ai_change_app.py",
            "why": "Shows a browser-device feature that combines camera access, QR detection, and multiple follow-up actions inside the app.",
            "repo_label": "This AI change assistant",
            "file": "qr-reader-workflow.txt",
        },
        {
            "id": "network-scanner-ui",
            "title": "Add a network scanner with a topology view",
            "difficulty": "Hard",
            "tech_focus": "Command line tools + web UI + topology visualization",
            "suggested_budget": 9,
            "validation_command": "python3 -m py_compile examples/ai/lib/ai_change_app.py",
            "why": "Shows a system-level change that combines discovery commands, structured results, and a richer visual interface.",
            "repo_label": "This AI change assistant",
            "file": "network-scanner-ui.txt",
        },
        {
            "id": "scheduler-management-ui",
            "title": "Add a scheduler management UI for this machine",
            "difficulty": "Hard",
            "tech_focus": "System integration + web UI",
            "suggested_budget": 8,
            "validation_command": "python3 -m py_compile examples/ai/lib/ai_change_app.py",
            "why": "Shows a machine-management workflow that reads current scheduled work and safely adds or removes jobs from a browser.",
            "repo_label": "This AI change assistant",
            "file": "scheduler-management-ui.txt",
        },
    ]


def load_starter_prompts() -> list[dict]:
    prompts = []
    for spec in starter_prompt_specs():
        prompt_path = starter_prompt_dir() / spec["file"]
        prompt_text_value = read_text(prompt_path).strip()
        item = dict(spec)
        item["prompt"] = prompt_text_value
        item["file_url"] = served_app_url_for_path(prompt_path)
        prompts.append(item)
    return prompts


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


def render_ai_call_link(change_id: str, call_id: str, label: str = "View AI request and response") -> str:
    call_id = str(call_id).strip()
    if not call_id:
        return ""
    return f'<a href="{e(ai_call_url(change_id, call_id))}">{e(label)}</a>'


def render_ai_call_links(change_id: str, pairs: list[tuple[str, str]]) -> str:
    links = [render_ai_call_link(change_id, call_id, label) for label, call_id in pairs if str(call_id).strip()]
    links = [item for item in links if item]
    if not links:
        return ""
    return "<p>" + " | ".join(links) + "</p>"


def render_ai_activity(change_id: str, calls: list[dict]) -> str:
    if not calls:
        return ""
    rows = []
    for item in calls:
        label = f"Call {item.get('call_number', '?')}: {item.get('prompt', '')}"
        summary = str(item.get("assistant_message", "")).strip() or "(no assistant message)"
        row = f"<li><a href=\"{e(ai_call_url(change_id, str(item.get('call_id', ''))))}\">{e(label)}</a>"
        row += f" <span class=\"meta\">{e(item.get('timestamp', ''))}</span>"
        if item.get("error"):
            row += f"<p>{e(item['error'])}</p>"
        else:
            row += f"<p>{e(shorten(summary, 220))}</p>"
        row += "</li>"
        rows.append(row)
    return f"<section class=\"card\"><h2>AI call logs</h2><ul class=\"stack-list\">{''.join(rows)}</ul></section>"


def render_decomposition(change_id: str, items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        subchanges = item.get("subchanges", [])
        lines = "".join(f"<li>{e(str(sub.get('description', '')).strip())}</li>" for sub in subchanges if str(sub.get("description", "")).strip())
        row = f"<li><p><strong>Depth {e(item.get('depth', ''))}:</strong> {e(item.get('description', ''))}</p>"
        row += render_ai_call_links(change_id, [("View planning request and response", item.get("ai_call_id", ""))])
        if lines:
            row += f"<ul class=\"stack-list\">{lines}</ul>"
        row += "</li>"
        rows.append(row)
    return f"<section class=\"card\"><h2>Decomposition</h2><ul class=\"stack-list\">{''.join(rows)}</ul></section>"


def key_instructions_card(message: str) -> str:
    return f"""
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
 

def key_instructions_page(message: str) -> str:
    body = key_instructions_card(message)
    return html_page(
        "Configure OpenRouter",
        body,
        subtitle="A valid OpenRouter API key is required for validation-command generation, strategy risk assessment, context gathering, planning, implementation, review, and follow-up suggestions.",
    )


def render_recent_changes(changes: list[dict]) -> str:
    if not changes:
        return "<p class=\"empty\">No change requests yet.</p>"
    items = []
    for change in changes:
        items.append(
            f"<li><a href=\"/changes/{e(change['id'])}/detail\">{e(change['description'])}</a>"
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


def render_starter_gallery(starter_prompts: list[dict], selected_model: str) -> str:
    cards = []
    for item in starter_prompts:
        repo_line = ""
        if item.get("repo_url"):
            repo_line = f"<p><strong>Example repo:</strong> <a href=\"{e(item['repo_url'])}\">{e(item['repo_label'])}</a></p>"
        else:
            repo_line = f"<p><strong>Example repo:</strong> {e(item['repo_label'])}</p>"
        use_url = route_url(
            "/",
            tab="change",
            model=selected_model,
            description=item["prompt"],
            budget=item["suggested_budget"],
        )
        cards.append(
            f"""
<article class="gallery-card">
  <p class="gallery-meta">{e(item['difficulty'])} · {e(item['tech_focus'])}</p>
  <h3>{e(item['title'])}</h3>
  {repo_line}
  <p>{e(item['why'])}</p>
  <details>
    <summary>View starter prompt</summary>
    <pre>{e(item['prompt'])}</pre>
    <p><a href="{e(item['file_url'])}">Open prompt file</a></p>
  </details>
  <div class="inline-actions">
    <a class="chip-link" href="{e(use_url)}">Use as a starting point</a>
  </div>
</article>
"""
        )
    return "<section class=\"gallery-grid\">" + "".join(cards) + "</section>"


def handle_home() -> None:
    ensure_runtime_dirs()
    prefs = load_preferences()
    setup = {}
    try:
        models, model_notice = list_available_models()
    except InvalidAPIKeyError as err:
        models, model_notice = [], str(err)
        setup = {
            "required": True,
            "message": str(err),
            "steps": [
                "Create or copy an API key from https://openrouter.ai/keys.",
                "Export OPENROUTER_API_KEY before starting fsrouter.",
                "Optionally set AI_CHANGE_ROOT to point at another workspace.",
            ],
        }
    except AppError as err:
        models, model_notice = [], str(err)
        setup = {"required": False, "message": str(err)}

    params = query_params()
    selected_model = selected_model_value(params, prefs, models)
    emit_json(
        {
            "app": "fsrouter-ai-change-assistant",
            "message": "Use POST /changes to queue a request, then poll /changes/:id/detail for status.",
            "model_notice": model_notice,
            "models": models,
            "selected_model": selected_model,
            "preferences": prefs,
            "recent_changes": list_recent_changes(),
            "starter_prompts": load_starter_prompts(),
            "server_root": str(target_root()),
            "data_root": str(DATA_ROOT),
            "setup": setup,
            "endpoints": {
                "create_change": "/changes (POST)",
                "change_detail": "/changes/:id/detail",
                "change_action": "/changes/:id (POST)",
                "favorites": "/preferences (POST)",
                "file_slice": "/file?path=...&start=...&end=...",
                "context_item": "/context?change=...&index=...",
                "diff_item": "/diff?change=...&index=...",
                "ai_call_log": "/ai-call?change=...&call=...",
            },
        }
    )


def handle_preferences_post() -> None:
    params = form_params()
    action = params.get("action", "").strip()
    model = params.get("model", "").strip()
    if action not in {"add", "remove"}:
        emit_json(
            {
                "error": "invalid_action",
                "message": f"Unsupported preferences action: {action or '(empty)'}",
                "allowed_actions": ["add", "remove"],
            },
            status=400,
        )
    if not model:
        emit_json({"error": "invalid_model", "message": "Model is required."}, status=400)
    update_favorite_model(model, action)
    emit_json(
        {
            "status": "ok",
            "action": action,
            "model": model,
            "preferences": load_preferences(),
        }
    )


def handle_change_post() -> None:
    params = form_params()
    description = params.get("description", "").strip()
    model = params.get("model", "").strip()
    favorite = params.get("favorite_model", "") == "1"
    try:
        ai_budget = max(1, min(40, int(params.get("ai_budget", DEFAULT_AI_BUDGET))))
    except ValueError:
        ai_budget = DEFAULT_AI_BUDGET
    if not description or not model:
        emit_json(
            {
                "error": "missing_fields",
                "message": "Description and model are required.",
            },
            status=400,
        )
    if not openrouter_api_key():
        emit_json(
            {
                "error": "missing_openrouter_api_key",
                "message": "OPENROUTER_API_KEY is missing.",
                "setup_steps": [
                    "Create or copy an API key from https://openrouter.ai/keys.",
                    "Export OPENROUTER_API_KEY before starting fsrouter.",
                    "Restart fsrouter and retry the request.",
                ],
            },
            status=400,
        )
    change_id = create_change_request(description, model, ai_budget, favorite)
    spawn_worker(change_id)
    emit_json(
        {
            "status": "queued",
            "change_id": change_id,
            "change_path": f"/changes/{change_id}/detail",
            "poll_after_seconds": REFRESH_SECONDS,
            "description": description,
            "model": model,
            "ai_budget": ai_budget,
        }
    )


def handle_change_action_post() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    params = form_params()
    action = params.get("action", "").strip()
    request = load_request(change_id)
    state = load_state(change_id)
    result = load_result(change_id)

    if state.get("status") != "awaiting_risk_review":
        emit_json(
            {
                "error": "risk_review_not_active",
                "message": "The change is not currently waiting for strategy risk review.",
                "change_id": change_id,
                "status": state.get("status", "unknown"),
            },
            status=400,
        )

    if action == "ignore_risk":
        request["allow_high_risk_strategy"] = True
        request["risk_override_at"] = utc_timestamp()
        latest = latest_risk_assessment(result)
        if latest:
            latest["user_decision"] = "continue_anyway"
            latest["decision_at"] = utc_timestamp()
            latest["decision_summary"] = "The user chose to continue despite the elevated strategy risk."
        save_request(change_id, request)
        save_result(change_id, result)
        state["status"] = "queued"
        state["current_step"] = "Queued"
        state["error"] = ""
        state["updated_at"] = utc_timestamp()
        state.pop("completed_at", None)
        save_state(change_id, state)
        append_event(change_id, "User chose to continue despite the elevated strategy risk warning.")
        spawn_worker(change_id)
        emit_json(
            {
                "status": "queued",
                "change_id": change_id,
                "message": "Risk warning ignored; workflow resumed.",
                "poll_after_seconds": REFRESH_SECONDS,
            }
        )
        return

    if action == "revise_strategy":
        strategy_notes = params.get("strategy_notes", "").strip()
        if not strategy_notes:
            emit_json(
                {
                    "error": "missing_strategy_notes",
                    "message": "Provide strategy_notes before retrying.",
                    "change_id": change_id,
                },
                status=400,
            )
        request["strategy_notes"] = strategy_notes
        request["allow_high_risk_strategy"] = False
        request["risk_override_at"] = ""
        latest = latest_risk_assessment(result)
        if latest:
            latest["user_decision"] = "revise_strategy"
            latest["decision_at"] = utc_timestamp()
            latest["decision_summary"] = "The user requested a different strategy to lower the risk."
        queue_change_retry(
            change_id,
            request,
            state,
            result,
            message="User proposed a different strategy after reviewing the elevated risk.",
        )
        spawn_worker(change_id)
        emit_json(
            {
                "status": "queued",
                "change_id": change_id,
                "message": "Retry queued with revised strategy.",
                "strategy_notes": strategy_notes,
                "poll_after_seconds": REFRESH_SECONDS,
            }
        )
        return

    emit_json(
        {
            "error": "invalid_action",
            "message": f"Unsupported change action: {action or '(empty)'}",
            "allowed_actions": ["ignore_risk", "revise_strategy"],
        },
        status=400,
    )


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
  <p><a href="{e(item['file_url'])}">Open current file directly</a></p>
  {render_ai_call_links(change_id, [("View selector request and response", item.get("ai_call_id", ""))])}
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
  <p><a href="{e(item['file_url'])}">Open current file directly</a></p>
  {render_ai_call_links(change_id, [("View implementation request and response", item.get("ai_call_id", ""))])}
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


def render_next_steps(change_id: str, steps: list[dict], request: dict) -> str:
    if not steps:
        return ""
    rows = []
    for item in steps:
        url = route_url(
            "/",
            description=item.get("description", ""),
            model=request.get("model", ""),
            budget=request.get("ai_budget", ""),
        )
        rows.append(
            f"""
<li>
  <a href="{e(url)}">{e(item.get('description', ''))}</a>
  <p>{e(item.get('why', ''))}</p>
  {render_ai_call_links(change_id, [("View next-step request and response", item.get("ai_call_id", ""))])}
</li>
"""
        )
    return f"<section class=\"card\"><h2>Likely next steps</h2><ul class=\"stack-list\">{''.join(rows)}</ul></section>"


def render_risk_assessments(change_id: str, assessments: list[dict], state: dict, request: dict) -> str:
    if not assessments:
        return ""
    latest = assessments[-1]
    awaiting_review = state.get("status") == "awaiting_risk_review"
    rows = []
    for item in reversed(assessments[-5:]):
        concerns = item.get("concerns", [])
        concerns_html = "".join(f"<li>{e(concern)}</li>" for concern in concerns) or "<li>No specific risks were listed.</li>"
        bypass = item.get("bypass_strategies", [])
        bypass_html = "".join(f"<li>{e(strategy)}</li>" for strategy in bypass) or "<li>No bypass strategy was suggested.</li>"
        plan_subchanges = item.get("plan_subchanges", [])
        subchange_html = ""
        if isinstance(plan_subchanges, list) and plan_subchanges:
            details = []
            for subchange in plan_subchanges:
                description = str(subchange.get("description", "")).strip()
                title = str(subchange.get("title", "")).strip()
                if title and description:
                    details.append(f"<li>{e(title)}: {e(description)}</li>")
                elif description:
                    details.append(f"<li>{e(description)}</li>")
            if details:
                subchange_html = f"<p><strong>Planned subchanges:</strong></p><ul class=\"stack-list\">{''.join(details)}</ul>"
        decision_summary = str(item.get("decision_summary", "")).strip()
        row = (
            f"<li><p><strong>{e(item.get('created_at', ''))}</strong> "
            f"<span class=\"status-chip\">{e(item.get('status', ''))}</span></p>"
            f"<p><strong>Risk score:</strong> {e(item.get('risk_score', ''))} "
            f"(default threshold {e(item.get('threshold', DEFAULT_STRATEGY_RISK_THRESHOLD))})</p>"
            f"<p><strong>Plan:</strong> {e(item.get('plan_action', 'implement'))}</p>"
        )
        if item.get("plan_reason"):
            row += f"<p>{e(item.get('plan_reason', ''))}</p>"
        if item.get("summary"):
            row += f"<p><strong>Assessment:</strong> {e(item.get('summary', ''))}</p>"
        if item.get("strategy_notes"):
            row += f"<p><strong>User strategy notes:</strong> {e(item.get('strategy_notes', ''))}</p>"
        row += render_ai_call_links(change_id, [("View strategy-risk request and response", item.get("ai_call_id", ""))])
        row += f"<p><strong>Risks to consider:</strong></p><ul class=\"stack-list\">{concerns_html}</ul>"
        row += f"<p><strong>Lower-risk strategies:</strong></p><ul class=\"stack-list\">{bypass_html}</ul>"
        row += subchange_html
        if decision_summary:
            row += f"<p><strong>Decision:</strong> {e(decision_summary)}</p>"
        row += "</li>"
        rows.append(row)

    action_block = ""
    if awaiting_review:
        suggestions = latest.get("bypass_strategies", [])
        suggestions_html = ""
        if suggestions:
            suggestions_html = "<ul class=\"stack-list\">" + "".join(f"<li>{e(item)}</li>" for item in suggestions) + "</ul>"
        action_block = f"""
<section class="card warning-card">
  <h3>Risk review required</h3>
  <p>The workflow paused because the strategy risk score <strong>{e(latest.get('risk_score', ''))}</strong> is above the default threshold <strong>{e(latest.get('threshold', DEFAULT_STRATEGY_RISK_THRESHOLD))}</strong>.</p>
  <p>{e(latest.get('summary', 'Review the listed risks before continuing.'))}</p>
  {suggestions_html}
  <form method="post" action="/changes/{e(change_id)}" class="stack-form">
    <input type="hidden" name="action" value="ignore_risk">
    <button type="submit">Ignore risks and continue</button>
  </form>
  <form method="post" action="/changes/{e(change_id)}" class="stack-form">
    <input type="hidden" name="action" value="revise_strategy">
    <label>
      <span>Different strategy</span>
      <textarea name="strategy_notes" rows="6" required>{e(request.get('strategy_notes', ''))}</textarea>
    </label>
    <button type="submit" class="ghost-button">Retry with a different strategy</button>
  </form>
</section>
"""
    return (
        f"<section class=\"card\"><h2>Strategy risk assessment</h2>{action_block}"
        f"<ul class=\"stack-list\">{''.join(rows)}</ul></section>"
    )


def render_validation_generation(change_id: str, info: dict) -> str:
    attempts = info.get("attempts", [])
    if not attempts:
        return ""
    rows = []
    for item in attempts:
        attempt_no = item.get("attempt", "")
        command = item.get("candidate_command", "")
        outcome = "accepted" if item.get("accepted") else "rejected"
        reason = item.get("rejection_reason", "")
        risk_score = item.get("risk_score", "")
        risk_summary = item.get("risk_summary", "")
        preflight = item.get("preflight_result", {})
        preflight_text = ""
        if isinstance(preflight, dict) and preflight:
            preflight_text = f"Preflight exit: {preflight.get('exit_code', '')}"
        row = (
            f"<li><p><strong>Attempt {e(attempt_no)}:</strong> "
            f"<code>{e(command or '(empty)')}</code> "
            f"<span class=\"status-chip\">{e(outcome)}</span></p>"
        )
        row += render_ai_call_links(
            change_id,
            [
                ("View command-generation request and response", item.get("generator_ai_call_id", "")),
                ("View risk-check request and response", item.get("risk_ai_call_id", "")),
            ],
        )
        if risk_score != "":
            row += f"<p><strong>Risk score:</strong> {e(risk_score)}"
            if risk_summary:
                row += f" ({e(risk_summary)})"
            row += "</p>"
        if preflight_text:
            row += f"<p>{e(preflight_text)}</p>"
        if reason:
            row += f"<p>{e(reason)}</p>"
        row += "</li>"
        rows.append(row)
    accepted = info.get("accepted_command", "")
    failure_reason = info.get("failure_reason", "")
    extras = ""
    if accepted:
        extras += f"<p><strong>Accepted command:</strong> <code>{e(accepted)}</code></p>"
        extras += render_ai_call_links(
            change_id,
            [
                ("Accepted command request/response", info.get("accepted_ai_call_id", "")),
                ("Accepted risk-check request/response", info.get("accepted_risk_ai_call_id", "")),
            ],
        )
    if info.get("accepted_risk_score", "") != "":
        extras += (
            f"<p><strong>Accepted risk score:</strong> {e(info.get('accepted_risk_score'))} "
            f"(max {e(info.get('risk_threshold', MAX_VALIDATION_RISK_SCORE))})</p>"
        )
    if failure_reason:
        extras += f"<p><strong>Failure reason:</strong> {e(failure_reason)}</p>"
    return (
        f"<section class=\"card\"><h2>Validation command generation</h2>"
        f"{extras}<ul class=\"stack-list\">{''.join(rows)}</ul></section>"
    )


def handle_change_detail() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    request = load_request(change_id)
    state = load_state(change_id)
    result = load_result(change_id)
    events = load_events(change_id)
    ai_calls = load_ai_calls(change_id)
    status_value = state.get("status", "unknown")
    emit_json(
        {
            "change_id": change_id,
            "running": status_value in {"queued", "running"},
            "poll_after_seconds": REFRESH_SECONDS if status_value in {"queued", "running"} else None,
            "request": request,
            "state": state,
            "result": result,
            "events": events,
            "ai_calls": ai_calls,
        }
    )


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
    numbered = [
        {"line": number, "text": line}
        for number, line in enumerate(snippet.splitlines(), start=selected_start)
    ]
    emit_json(
        {
            "path": relative,
            "server_root": str(target_root()),
            "line_start": selected_start,
            "line_end": selected_end,
            "text": snippet,
            "numbered_lines": numbered,
        }
    )


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
    emit_json({"change_id": change_id, "index": index, "item": items[index]})


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
    emit_json({"change_id": change_id, "index": index, "item": items[index]})


def handle_ai_call_view() -> None:
    params = query_params()
    change_id = params.get("change", "")
    call_id = params.get("call", "")
    payload = load_ai_call_log(change_id, call_id)
    relative_log_path = str(payload.get("log_path", "")).strip()
    direct_log_path = served_app_path(relative_log_path)
    fallback_log_path = ""
    if not direct_log_path and relative_log_path and not relative_log_path.startswith("/"):
        fallback_log_path = route_url("/file", path=relative_log_path)
    emit_json(
        {
            "change_id": change_id,
            "call_id": call_id,
            "log_path": relative_log_path,
            "direct_log_path": direct_log_path,
            "fallback_log_path": fallback_log_path,
            "payload": payload,
        }
    )


def handle_change_artifact_view() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    params = query_params()
    kind = (params.get("kind") or params.get("type") or "result").strip().lower()

    if kind == "request":
        emit_json({"change_id": change_id, "kind": kind, "data": load_request(change_id)})
        return
    if kind == "state":
        emit_json({"change_id": change_id, "kind": kind, "data": load_state(change_id)})
        return
    if kind == "result":
        emit_json({"change_id": change_id, "kind": kind, "data": load_result(change_id)})
        return
    if kind == "events":
        emit_json({"change_id": change_id, "kind": kind, "data": load_events(change_id)})
        return
    if kind == "ai_calls":
        emit_json({"change_id": change_id, "kind": kind, "data": load_ai_calls(change_id)})
        return
    if kind == "context":
        result = load_result(change_id)
        items = result.get("context_items", [])
        try:
            index = int(params.get("index", "0"))
        except ValueError:
            index = 0
        if index < 0 or index >= len(items):
            raise AppError("Unknown context artifact index.")
        emit_json({"change_id": change_id, "kind": kind, "index": index, "data": items[index]})
        return
    if kind == "diff":
        result = load_result(change_id)
        items = result.get("applied_changes") or result.get("attempted_changes") or []
        try:
            index = int(params.get("index", "0"))
        except ValueError:
            index = 0
        if index < 0 or index >= len(items):
            raise AppError("Unknown diff artifact index.")
        emit_json({"change_id": change_id, "kind": kind, "index": index, "data": items[index]})
        return
    if kind in {"ai_call", "aicall"}:
        call_id = params.get("call", "").strip()
        if not call_id:
            raise AppError("The call query parameter is required for ai_call artifacts.")
        emit_json({"change_id": change_id, "kind": "ai_call", "data": load_ai_call_log(change_id, call_id)})
        return
    raise AppError(
        "Unknown artifact kind. Use one of: request, state, result, events, ai_calls, context, diff, ai_call."
    )


def handle_change_validation_post() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    request = load_request(change_id)
    state = load_state(change_id)
    result = load_result(change_id)
    status_value = state.get("status", "unknown")
    if status_value in {"queued", "running"}:
        emit_json(
            {"error": "change_in_progress", "message": "The change is already running.", "change_id": change_id},
            status=400,
        )
    if status_value == "awaiting_risk_review":
        emit_json(
            {
                "error": "risk_review_required",
                "message": "Handle risk review first with POST /changes/:id and action ignore_risk or revise_strategy.",
                "change_id": change_id,
            },
            status=400,
        )
    queue_change_retry(
        change_id,
        request,
        state,
        result,
        message="Manual validation rerun requested through /changes/:id/validation.",
    )
    spawn_worker(change_id)
    emit_json(
        {
            "status": "queued",
            "change_id": change_id,
            "previous_status": status_value,
            "message": "Validation rerun queued.",
            "poll_after_seconds": REFRESH_SECONDS,
        }
    )


def handle_change_recovery_post() -> None:
    change_id = os.environ.get("PARAM_ID", "")
    request = load_request(change_id)
    state = load_state(change_id)
    result = load_result(change_id)
    status_value = state.get("status", "unknown")
    if status_value not in {"rolled_back", "validation_failed", "error"}:
        emit_json(
            {
                "error": "recovery_not_available",
                "message": f"Recovery is only available for rolled_back, validation_failed, or error states (got {status_value}).",
                "change_id": change_id,
            },
            status=400,
        )
    params = form_params()
    strategy_notes = params.get("strategy_notes", "").strip()
    if strategy_notes:
        request["strategy_notes"] = strategy_notes
        request["allow_high_risk_strategy"] = False
        request["risk_override_at"] = ""
    queue_change_retry(
        change_id,
        request,
        state,
        result,
        message="Manual recovery queued through /changes/:id/recovery.",
    )
    spawn_worker(change_id)
    emit_json(
        {
            "status": "queued",
            "change_id": change_id,
            "previous_status": status_value,
            "message": "Recovery queued.",
            "strategy_notes": request.get("strategy_notes", ""),
            "poll_after_seconds": REFRESH_SECONDS,
        }
    )


def render_error_page(err: Exception) -> None:
    if isinstance(err, AppError):
        emit_json({"error": "bad_request", "message": str(err)}, status=400)
        return
    emit_json(
        {
            "error": "internal_error",
            "message": str(err),
            "traceback": traceback.format_exc(),
        },
        status=500,
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
