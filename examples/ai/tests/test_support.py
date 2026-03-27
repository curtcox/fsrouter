import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
import unittest
from pathlib import Path

from examples.ai.agent_loop import BudgetRef, run_agent_loop


SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
    "additionalProperties": False,
}


def make_response(content):
    return {
        "id": "resp-1",
        "model": "mock-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 1},
    }


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected API call")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RetryableError(Exception):
    pass


class AgentLoopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompts_dir = self.root / "prompts"
        self.prompts_dir.mkdir()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.log_dir = self.root / "logs" / "run-1"
        self.working_dir = self.root / "work"
        self.working_dir.mkdir()
        (self.prompts_dir / "plan.txt").write_text(
            "Goal: {{change_description}}\nContext: {{context}}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(
        self,
        *,
        goal="plan",
        template_vars=None,
        output_schema=None,
        model="primary",
        review_model="review",
        budget=None,
        ask_user=None,
        feedback=None,
        client=None,
        sleep_fn=None,
        command_timeout=30,
    ):
        return run_agent_loop(
            goal=goal,
            template_vars=template_vars or {
                "change_description": "add feature",
                "context": "none",
            },
            output_schema=output_schema or SUMMARY_SCHEMA,
            model=model,
            review_model=review_model,
            budget=budget or BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=ask_user or (lambda question, options: "Approve"),
            feedback=feedback,
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=sleep_fn or (lambda seconds: None),
            command_timeout=command_timeout,
        )


class AgentLoopBuildTestCase(unittest.TestCase):
    _repo_root = Path(__file__).resolve().parents[3]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.working_dir = self.root / "app"
        self.working_dir.mkdir()
        self.routes_dir = self.working_dir / "routes"
        self.routes_dir.mkdir()
        self.prompts_dir = self.working_dir / "prompts"
        self.prompts_dir.mkdir()
        self.data_dir = self.working_dir / "data"
        self.data_dir.mkdir()
        self.log_dir = self.working_dir / "logs"
        self.log_dir.mkdir()
        (self.prompts_dir / "plan.txt").write_text(
            "Goal: {{change_description}}\nContext: {{context}}\n",
            encoding="utf-8",
        )
        (self.prompts_dir / "build.txt").write_text(
            (
                "Build the requested fsrouter web app inside routes/.\n"
                "You may run local shell commands to inspect the environment and create files.\n"
                "When finished, return a short summary of what you built.\n"
                "Request: {{change_description}}\n"
                "Context: {{context}}\n"
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _free_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _start_server(self):
        port = self._free_port()
        env = os.environ.copy()
        env["ROUTE_DIR"] = str(self.routes_dir)
        env["LISTEN_ADDR"] = f"127.0.0.1:{port}"
        env["COMMAND_TIMEOUT"] = "30"
        server = subprocess.Popen(
            ["python3", str(self._repo_root / "python" / "fsrouter.py")],
            cwd=str(self._repo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base_url + "/") as response:
                    response.read()
                    return server, base_url
            except Exception:
                time.sleep(0.2)
        server.terminate()
        server.wait(timeout=5)
        if server.stdout:
            server.stdout.close()
        if server.stderr:
            server.stderr.close()
        self.fail("fsrouter server did not start")

    def _stop_server(self, server):
        server.terminate()
        server.wait(timeout=5)
        if server.stdout:
            server.stdout.close()
        if server.stderr:
            server.stderr.close()

    def _load_json_if_exists(self, path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _build_debug_context(self, change_id, result):
        log_path = self.log_dir / change_id
        requests = self._load_json_if_exists(log_path / "request.json") or []
        responses = self._load_json_if_exists(log_path / "response.json") or []
        commands = self._load_json_if_exists(log_path / "commands.json") or []
        warnings = self._load_json_if_exists(log_path / "warnings.json") or []
        conversation = self._load_json_if_exists(log_path / "conversation.json") or []
        route_files = sorted(
            str(path.relative_to(self.routes_dir))
            for path in self.routes_dir.rglob("*")
            if path.is_file()
        )
        conversation_tail = [
            {"role": item.get("role"), "content": item.get("content", "")[:240]}
            for item in conversation[-4:]
        ]
        return (
            f"result={result}\n"
            f"requests={len(requests)} responses={len(responses)} commands={len(commands)} warnings={len(warnings)}\n"
            f"route_files={route_files}\n"
            f"conversation_tail={json.dumps(conversation_tail, indent=2)}"
        )

    def _run_build(self, change_description, change_id):
        return run_agent_loop(
            goal="build",
            template_vars={
                "change_description": change_description,
                "context": "Create files directly under routes/ as needed.",
            },
            output_schema=SUMMARY_SCHEMA,
            model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1"),
            review_model=os.environ.get(
                "OPENROUTER_REVIEW_MODEL",
                os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
            ),
            budget=BudgetRef(
                remaining=int(os.environ.get("AGENT_LOOP_TEST_BUDGET", "25"))
            ),
            log_dir=self.log_dir / change_id,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
        )

    def _read_text_if_exists(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""
