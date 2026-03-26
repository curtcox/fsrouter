import base64
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class BudgetRef:
    remaining: int


class ApiCallError(Exception):
    pass


def run_agent_loop(
    *,
    goal,
    template_vars,
    output_schema,
    model,
    review_model,
    budget,
    log_dir,
    working_dir,
    ask_user,
    feedback=None,
    prompts_dir=None,
    data_dir=None,
    client=None,
    sleep_fn=time.sleep,
    command_timeout=30,
):
    feedback = feedback or []
    log_dir = Path(log_dir)
    working_dir = Path(working_dir)
    prompts_dir = Path(prompts_dir or working_dir / "prompts")
    data_dir = Path(data_dir or working_dir / "data")
    log_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    client = client or _make_openrouter_client()
    requests_log = []
    responses_log = []
    commands_log = []

    if feedback:
        _write_json(log_dir / "feedback.json", feedback)

    try:
        system_prompt = _render_prompt(prompts_dir, goal, template_vars, output_schema)
    except Exception as exc:
        return {"type": "error", "error": str(exc)}

    conversation = [{"role": "system", "content": system_prompt}]
    for item in feedback:
        conversation.append({"role": "user", "content": json.dumps({"feedback": item}, separators=(",", ":"))})
    _write_json(log_dir / "conversation.json", conversation)

    consecutive_validation_failures = 0
    summary_events = []

    while True:
        if budget.remaining <= 0:
            _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
            return {"type": "budget_exhausted", "summary": _summarize(summary_events, conversation, commands_log)}

        request_body = {
            "model": model,
            "messages": conversation,
            "response_format": {"type": "json_object"},
        }
        budget.remaining -= 1

        try:
            response_body = _call_with_retries(client, request_body, sleep_fn)
        except Exception as exc:
            requests_log.append(request_body)
            responses_log.append({"error": str(exc)})
            _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
            return {"type": "error", "error": str(exc)}

        requests_log.append(request_body)
        responses_log.append(response_body)

        assistant_content = _extract_response_content(response_body)
        conversation.append({"role": "assistant", "content": assistant_content})

        ok, parsed_or_error = _parse_primary_response(assistant_content, output_schema)
        if not ok:
            consecutive_validation_failures += 1
            if consecutive_validation_failures >= 3:
                _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
                return {"type": "error", "error": f"3 consecutive validation failures: {parsed_or_error}"}
            conversation.append(
                {
                    "role": "user",
                    "content": f"Response validation failed: {parsed_or_error}. Reply with valid JSON only.",
                }
            )
            _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
            continue

        consecutive_validation_failures = 0
        parsed = parsed_or_error

        if parsed["type"] == "answer":
            _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
            return parsed["answer"]

        command_result = _handle_commands(
            commands=parsed["commands"],
            goal=goal,
            model=review_model,
            budget=budget,
            log_dir=log_dir,
            working_dir=working_dir,
            data_dir=data_dir,
            ask_user=ask_user,
            client=client,
            requests_log=requests_log,
            responses_log=responses_log,
            commands_log=commands_log,
            sleep_fn=sleep_fn,
            command_timeout=command_timeout,
            summary_events=summary_events,
        )
        if command_result.get("terminal") is not None:
            _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)
            return command_result["terminal"]

        conversation.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "command_results", "results": command_result["results"]},
                    separators=(",", ":"),
                ),
            }
        )
        _write_logs(log_dir, requests_log, responses_log, conversation, commands_log)


def _handle_commands(
    *,
    commands,
    goal,
    model,
    budget,
    log_dir,
    working_dir,
    data_dir,
    ask_user,
    client,
    requests_log,
    responses_log,
    commands_log,
    sleep_fn,
    command_timeout,
    summary_events,
):
    results = []
    cache, cache_warning = _load_safety_cache(data_dir / "safety-cache.json")
    if cache_warning:
        summary_events.append(cache_warning)

    for command_item in commands:
        entry = {
            "command": command_item["command"],
            "purpose": command_item["purpose"],
            "safety": None,
            "execution": None,
        }
        pattern = _command_pattern(command_item["command"])
        cached = _match_cache(cache, pattern)
        if cached is not None:
            verdict = cached["verdict"]
            reasoning = cached.get("reasoning", "")
            entry["safety"] = {
                "source": "cache",
                "verdict": verdict,
                "reasoning": reasoning,
            }
            if verdict == "safe":
                execution = _run_command(command_item["command"], working_dir, command_timeout)
                entry["execution"] = execution
                results.append(_command_result_payload(command_item["command"], execution))
            else:
                results.append(
                    {
                        "command": command_item["command"],
                        "status": "blocked",
                        "reason": reasoning or f"Command blocked by cached verdict: {verdict}",
                    }
                )
            commands_log.append(entry)
            continue

        review = _review_command(
            command_item=command_item,
            review_model=model,
            goal=goal,
            working_dir=working_dir,
            budget=budget,
            client=client,
            requests_log=requests_log,
            responses_log=responses_log,
            sleep_fn=sleep_fn,
        )
        if review.get("terminal") is not None:
            return {"terminal": review["terminal"]}

        verdict = review["verdict"]
        reasoning = review["reasoning"]
        pattern = review["pattern"]

        if budget.remaining <= 0:
            entry["safety"] = {"source": "review", "verdict": verdict, "reasoning": reasoning}
            commands_log.append(entry)
            _write_json(data_dir / "safety-cache.json", cache)
            return {"terminal": {"type": "budget_exhausted", "summary": _summarize(summary_events, [], commands_log)}}

        if verdict == "safe":
            entry["safety"] = {"source": "review", "verdict": "safe", "reasoning": reasoning}
            cache.append(_cache_entry(pattern, "safe", "review_model", reasoning))
            execution = _run_command(command_item["command"], working_dir, command_timeout)
            entry["execution"] = execution
            results.append(_command_result_payload(command_item["command"], execution))
        elif verdict == "blocked":
            entry["safety"] = {"source": "review", "verdict": "blocked", "reasoning": reasoning}
            cache.append(_cache_entry(pattern, "blocked", "review_model", reasoning))
            results.append({"command": command_item["command"], "status": "blocked", "reason": reasoning})
        else:
            answer = ask_user(
                f"Command review marked this risky: {command_item['command']}\nReason: {reasoning}",
                ["Approve", "Reject", "Abort"],
            )
            decision = _normalize_user_decision(answer)
            if decision == "abort":
                entry["safety"] = {"source": "user", "verdict": "risky", "reasoning": reasoning}
                commands_log.append(entry)
                _write_json(data_dir / "safety-cache.json", cache)
                return {"terminal": {"type": "user_aborted"}}
            if decision == "approve":
                entry["safety"] = {"source": "user", "verdict": "safe", "reasoning": reasoning}
                cache.append(_cache_entry(pattern, "safe", "user", reasoning))
                execution = _run_command(command_item["command"], working_dir, command_timeout)
                entry["execution"] = execution
                results.append(_command_result_payload(command_item["command"], execution))
            else:
                entry["safety"] = {"source": "user", "verdict": "rejected", "reasoning": reasoning}
                cache.append(_cache_entry(pattern, "rejected", "user", reasoning))
                results.append({"command": command_item["command"], "status": "rejected", "reason": reasoning})

        commands_log.append(entry)
        summary_events.append(command_item["command"])
        _write_json(data_dir / "safety-cache.json", cache)

    return {"results": results, "terminal": None}


def _review_command(
    *,
    command_item,
    review_model,
    goal,
    working_dir,
    budget,
    client,
    requests_log,
    responses_log,
    sleep_fn,
):
    failures = 0
    while True:
        if budget.remaining <= 0:
            return {"terminal": {"type": "budget_exhausted", "summary": f"Budget exhausted before reviewing {command_item['command']}"}}
        request_body = {
            "model": review_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Review a local command for safety. "
                        "Reply with JSON: "
                        '{"verdict":"safe|risky|blocked","reasoning":"...","pattern":"..."}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "goal": goal,
                            "command": command_item["command"],
                            "purpose": command_item["purpose"],
                            "working_dir": str(working_dir),
                        },
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        budget.remaining -= 1
        try:
            response_body = _call_with_retries(client, request_body, sleep_fn)
        except Exception as exc:
            requests_log.append(request_body)
            responses_log.append({"error": str(exc)})
            return {"terminal": {"type": "error", "error": str(exc)}}

        requests_log.append(request_body)
        responses_log.append(response_body)
        ok, parsed_or_error = _parse_review_response(_extract_response_content(response_body))
        if ok:
            return parsed_or_error
        failures += 1
        if failures >= 3:
            return {"terminal": {"type": "error", "error": f"3 consecutive review validation failures: {parsed_or_error}"}}


def _parse_primary_response(content, output_schema):
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, str(exc)
    if data.get("type") not in {"command", "answer"}:
        return False, "type must be 'command' or 'answer'"
    if data["type"] == "answer":
        if "answer" not in data:
            return False, "answer response must include answer"
        ok, error = _validate_schema(data["answer"], output_schema)
        if not ok:
            return False, error
        return True, data
    if "commands" not in data or not isinstance(data["commands"], list):
        return False, "command response must include commands"
    for item in data["commands"]:
        if not isinstance(item, dict) or not isinstance(item.get("command"), str) or not isinstance(item.get("purpose"), str):
            return False, "each command must include string command and purpose"
    return True, data


def _parse_review_response(content):
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, str(exc)
    if data.get("verdict") not in {"safe", "risky", "blocked"}:
        return False, "review verdict must be safe, risky, or blocked"
    if not isinstance(data.get("reasoning"), str):
        return False, "review reasoning must be a string"
    if not isinstance(data.get("pattern"), str):
        return False, "review pattern must be a string"
    return True, data


def _call_with_retries(client, request_body, sleep_fn):
    delays = [0, 1, 2, 4]
    last_error = None
    for index, delay in enumerate(delays):
        if delay:
            sleep_fn(delay)
        try:
            return client(request_body)
        except Exception as exc:
            last_error = exc
            if index == len(delays) - 1:
                break
    raise ApiCallError(str(last_error))


def _extract_response_content(response_body):
    return response_body["choices"][0]["message"]["content"]


def _render_prompt(prompts_dir, goal, template_vars, output_schema):
    prompt_path = prompts_dir / f"{goal}.txt"
    if not prompt_path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {prompt_path}")
    template = prompt_path.read_text(encoding="utf-8")
    rendered = template
    placeholders = re.findall(r"{{\s*([a-zA-Z0-9_]+)\s*}}", template)
    for name in placeholders:
        if name not in template_vars:
            raise ValueError(f"Missing template variable: {name}")
        rendered = re.sub(r"{{\s*" + re.escape(name) + r"\s*}}", str(template_vars[name]), rendered)
    instructions = (
        "\n\nOutput schema:\n"
        + json.dumps(output_schema, indent=2, sort_keys=True)
        + '\n\nReply with JSON only. Use {"type":"command","commands":[{"command":"...","purpose":"..."}],"reasoning":"..."} '
        + 'to request commands. Use {"type":"answer","answer":...} when done.'
    )
    return rendered + instructions


def _validate_schema(value, schema, path="$"):
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            return False, f"{path} must be an object"
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                return False, f"{path}.{key} is required"
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                ok, error = _validate_schema(item, properties[key], f"{path}.{key}")
                if not ok:
                    return False, error
            elif schema.get("additionalProperties") is False:
                return False, f"{path}.{key} is not allowed"
        return True, None
    if expected_type == "array":
        if not isinstance(value, list):
            return False, f"{path} must be an array"
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                ok, error = _validate_schema(item, item_schema, f"{path}[{index}]")
                if not ok:
                    return False, error
        return True, None
    if expected_type == "string":
        return (True, None) if isinstance(value, str) else (False, f"{path} must be a string")
    if expected_type == "integer":
        return (True, None) if isinstance(value, int) and not isinstance(value, bool) else (False, f"{path} must be an integer")
    if expected_type == "number":
        return (True, None) if isinstance(value, (int, float)) and not isinstance(value, bool) else (False, f"{path} must be a number")
    if expected_type == "boolean":
        return (True, None) if isinstance(value, bool) else (False, f"{path} must be a boolean")
    if expected_type == "null":
        return (True, None) if value is None else (False, f"{path} must be null")
    if "enum" in schema:
        return (True, None) if value in schema["enum"] else (False, f"{path} must be one of {schema['enum']}")
    return True, None


def _command_pattern(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return command.strip()
    if not parts:
        return ""
    prefix = [parts[0]]
    for token in parts[1:]:
        if token.startswith("-"):
            prefix.append(token)
        else:
            break
    return " ".join(prefix) + " *"


def _match_cache(cache, pattern):
    for entry in cache:
        if entry.get("pattern") == pattern:
            return entry
    return None


def _load_safety_cache(path):
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], f"Corrupt safety cache ignored: {path}"
    if not isinstance(data, list):
        return [], f"Invalid safety cache ignored: {path}"
    return data, None


def _cache_entry(pattern, verdict, source, reasoning):
    return {
        "pattern": pattern,
        "verdict": verdict,
        "source": source,
        "reasoning": reasoning,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _run_command(command, working_dir, timeout):
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(working_dir),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout, stdout_mode = _encode_output(completed.stdout)
        stderr, stderr_mode = _encode_output(completed.stderr)
        return {
            "stdout": stdout if stdout_mode == "text" else "",
            "stderr": stderr if stderr_mode == "text" else "",
            "stdout_base64": stdout if stdout_mode == "base64" else None,
            "stderr_base64": stderr if stderr_mode == "base64" else None,
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": max(1, int((time.time() - started) * 1000)),
        }
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_mode = _encode_output(exc.stdout or b"")
        stderr, stderr_mode = _encode_output(exc.stderr or b"")
        return {
            "stdout": stdout if stdout_mode == "text" else "",
            "stderr": stderr if stderr_mode == "text" else "",
            "stdout_base64": stdout if stdout_mode == "base64" else None,
            "stderr_base64": stderr if stderr_mode == "base64" else None,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": max(1, int((time.time() - started) * 1000)),
        }


def _encode_output(data):
    if not data:
        return "", "text"
    if b"\x00" in data:
        return base64.b64encode(data).decode("ascii"), "base64"
    return data.decode("utf-8", errors="replace"), "text"


def _command_result_payload(command, execution):
    payload = {
        "command": command,
        "status": "completed",
        "stdout": execution["stdout"],
        "stderr": execution["stderr"],
        "exit_code": execution["exit_code"],
        "timed_out": execution["timed_out"],
    }
    if execution.get("stdout_base64") is not None:
        payload["stdout_base64"] = execution["stdout_base64"]
    if execution.get("stderr_base64") is not None:
        payload["stderr_base64"] = execution["stderr_base64"]
    return payload


def _normalize_user_decision(answer):
    if isinstance(answer, dict):
        value = answer.get("choice") or answer.get("option") or answer.get("text") or ""
    else:
        value = str(answer)
    lowered = value.strip().lower()
    if "abort" in lowered or "stop" in lowered:
        return "abort"
    if "approve" in lowered:
        return "approve"
    return "reject"


def _summarize(summary_events, conversation, commands_log):
    if commands_log:
        return "Completed work before stopping: " + ", ".join(entry["command"] for entry in commands_log)
    if summary_events:
        return "Completed work before stopping: " + ", ".join(summary_events)
    if conversation:
        return "Completed work before stopping: started conversation"
    return "No work completed"


def _write_logs(log_dir, requests_log, responses_log, conversation, commands_log):
    if requests_log:
        _write_json(log_dir / "request.json", requests_log)
    if responses_log:
        _write_json(log_dir / "response.json", responses_log)
    _write_json(log_dir / "conversation.json", conversation)
    if commands_log:
        _write_json(log_dir / "commands.json", commands_log)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_openrouter_client():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ApiCallError("OPENROUTER_API_KEY is not set")

    def _client(request_body):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiCallError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ApiCallError(f"Network error: {exc}") from exc

    return _client
