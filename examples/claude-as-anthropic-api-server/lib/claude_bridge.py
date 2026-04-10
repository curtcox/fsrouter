"""
Shared utilities for the Anthropic-compatible API server.
Bridges between Anthropic API format and the `claude` CLI.
"""
import json
import os
import subprocess
import sys
import time
import uuid


def read_request_body() -> dict:
    """Read and parse JSON request body from stdin."""
    body = sys.stdin.buffer.read()
    if not body:
        return {}
    return json.loads(body)


def respond(data: dict, status: int = 200) -> None:
    """Write a JSON response to stdout and exit with appropriate code."""
    print(json.dumps(data))
    if status == 200:
        sys.exit(0)
    elif status == 400:
        sys.exit(1)
    else:
        sys.exit(2)


def error_response(error_type: str, message: str, status: int = 400) -> None:
    """Return an Anthropic-style error response."""
    respond({
        "type": "error",
        "error": {
            "type": error_type,
            "message": message
        }
    }, status)


def generate_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def generate_batch_id() -> str:
    return "msgbatch_" + uuid.uuid4().hex[:20]


def map_model_alias(model: str) -> str:
    """Map model strings to claude CLI model flags."""
    aliases = {
        "claude-opus-4-6": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-haiku-4-5": "haiku",
        "claude-haiku-4-5-20251001": "haiku",
        "claude-sonnet-4-5-20250514": "sonnet",
        "claude-3-5-sonnet-20241022": "sonnet",
        "claude-3-5-haiku-20241022": "haiku",
        "claude-3-opus-20240229": "opus",
        "claude-3-sonnet-20240229": "sonnet",
        "claude-3-haiku-20240307": "haiku",
    }
    # If it's already an alias, pass through
    if model in ("opus", "sonnet", "haiku"):
        return model
    return aliases.get(model, "sonnet")


def build_prompt_from_messages(messages: list, system: str = None) -> str:
    """Convert Anthropic-style messages array into a single prompt string for the claude CLI."""
    parts = []
    if system:
        if isinstance(system, list):
            # System can be array of text blocks
            system_text = "\n".join(
                block.get("text", "") for block in system if block.get("type") == "text"
            )
        else:
            system_text = system
        parts.append(f"[System instructions]: {system_text}\n")

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Extract text from content blocks
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    text_parts.append(f"[Tool result for {block.get('tool_use_id', 'unknown')}]: {block.get('content', '')}")
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if role == "user":
            parts.append(f"Human: {text}")
        elif role == "assistant":
            parts.append(f"Assistant: {text}")

    return "\n\n".join(parts)


def call_claude(
    messages: list,
    model: str = "sonnet",
    system: str = None,
    max_tokens: int = 4096,
    temperature: float = None,
    timeout: int = 120,
) -> dict:
    """
    Call the claude CLI and return a raw result dict.
    Returns the CLI JSON output on success or {"error": "..."} on failure.

    The claude CLI with --output-format json returns:
    {
      "type": "result",
      "subtype": "success",
      "is_error": false,
      "result": "<response text>",
      "cost_usd": 0.01,
      "duration_ms": 1234,
      "usage": {...},
      "session_id": "...",
      ...
    }
    """
    prompt = build_prompt_from_messages(messages, system)
    model_flag = map_model_alias(model)

    cmd = [
        "claude",
        "-p",
        "--output-format", "json",
        "--model", model_flag,
        "--no-session-persistence",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Claude CLI timed out", "error_type": "api_error"}
    except FileNotFoundError:
        return {"error": "claude CLI not found. Install Claude Code first.", "error_type": "api_error"}

    # Try to parse JSON output (claude CLI always returns JSON with --output-format json)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Fallback: if stdout is not JSON, check stderr
        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else "Unknown error"
            return {"error": f"claude CLI error (exit {proc.returncode}): {stderr}", "error_type": "api_error"}
        # Treat raw stdout as the response text
        return {"result": proc.stdout.strip(), "is_error": False}

    # Check for errors in the JSON result
    if result.get("is_error", False):
        return {
            "error": result.get("result", "Unknown claude CLI error"),
            "error_type": "api_error",
        }

    return result


def format_anthropic_response(
    claude_result: dict,
    model: str,
    stop_reason: str = "end_turn",
) -> dict:
    """Format a claude CLI result into an Anthropic Messages API response."""
    msg_id = generate_message_id()

    # Check for error
    if "error" in claude_result:
        error_response("api_error", claude_result["error"], 500)

    text = claude_result.get("result", "")

    # Map claude CLI stop_reason to Anthropic API stop_reason
    cli_stop = claude_result.get("stop_reason", "")
    if cli_stop == "stop_sequence":
        stop_reason = "stop_sequence"
    elif cli_stop == "max_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    # Extract usage from claude CLI output if available
    usage = claude_result.get("usage", {})
    input_tokens = usage.get("input_tokens", max(1, len(text) // 4))
    output_tokens = usage.get("output_tokens", max(1, len(text) // 4))
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }


# Model catalog - used by the models endpoints
MODEL_CATALOG = [
    {
        "id": "claude-opus-4-6",
        "type": "model",
        "display_name": "Claude Opus 4.6",
        "created_at": "2025-05-14T00:00:00Z",
        "max_input_tokens": 200000,
        "max_tokens": 32000,
    },
    {
        "id": "claude-sonnet-4-6",
        "type": "model",
        "display_name": "Claude Sonnet 4.6",
        "created_at": "2025-05-14T00:00:00Z",
        "max_input_tokens": 200000,
        "max_tokens": 64000,
    },
    {
        "id": "claude-haiku-4-5",
        "type": "model",
        "display_name": "Claude Haiku 4.5",
        "created_at": "2024-10-01T00:00:00Z",
        "max_input_tokens": 200000,
        "max_tokens": 8192,
    },
]
