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


class AgentLoopTests(unittest.TestCase):
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

    def test_single_turn_answer_logs_and_decrements_budget(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                )
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        self.assertEqual(2, budget.remaining)
        requests = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        responses = json.loads((self.log_dir / "response.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(requests))
        self.assertEqual(1, len(responses))
        self.assertEqual("system", conversation[0]["role"])
        self.assertEqual("assistant", conversation[1]["role"])
        self.assertIn("add feature", requests[0]["messages"][0]["content"])
        self.assertIn('"type":"answer"', requests[0]["messages"][0]["content"])
        self.assertFalse((self.log_dir / "commands.json").exists())
        self.assertFalse((self.log_dir / "feedback.json").exists())

    def test_multi_turn_command_review_and_answer(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "saw hello"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "saw hello"}, result)
        self.assertEqual(2, budget.remaining)
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        cache = json.loads((self.data_dir / "safety-cache.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(commands))
        self.assertEqual("hello\n", commands[0]["execution"]["stdout"])
        self.assertEqual("review", commands[0]["safety"]["source"])
        self.assertEqual("echo *", cache[0]["pattern"])
        self.assertEqual(["system", "assistant", "user", "assistant"], [item["role"] for item in conversation])
        requests = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        responses = json.loads((self.log_dir / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(3, len(requests))
        self.assertEqual(3, len(responses))

    def test_multiple_cached_commands_in_one_turn_only_spends_primary_budget(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [
                                {"command": "echo one", "purpose": "test"},
                                {"command": "echo two", "purpose": "test"},
                                {"command": "echo three", "purpose": "test"},
                            ],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {"pattern": "echo *", "verdict": "safe", "source": "review_model", "reasoning": "ok", "timestamp": "2026-03-26T12:00:00Z"}
                ]
            ),
            encoding="utf-8",
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        self.assertEqual(1, budget.remaining)
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual(3, len(commands))
        command_results = json.loads(conversation[2]["content"])
        self.assertEqual(3, len(command_results["results"]))

    def test_invalid_json_retries_with_validation_error(self):
        client = FakeClient(
            [
                make_response("not json"),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "fixed"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "fixed"}, result)
        self.assertEqual(1, budget.remaining)
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual("user", conversation[2]["role"])
        self.assertIn("Response validation failed", conversation[2]["content"])

    def test_wrong_type_retries_and_recovers(self):
        client = FakeClient(
            [
                make_response('{"type":"unknown"}'),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "fixed"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "fixed"}, result)
        self.assertEqual(1, budget.remaining)

    def test_answer_schema_failure_retries_and_mentions_violation(self):
        client = FakeClient(
            [
                make_response('{"type":"answer","answer":{"wrong":"shape"}}'),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "fixed"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "fixed"}, result)
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertIn("$.summary is required", conversation[2]["content"])

    def test_malformed_command_request_retries_without_executing(self):
        client = FakeClient(
            [
                make_response('{"type":"command","commands":[{"cmd":"ls"}]}'),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "fixed"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "fixed"}, result)
        self.assertFalse((self.log_dir / "commands.json").exists())

    def test_risky_command_rejected_by_user_does_not_run(self):
        asked = []

        def ask_user(question, options):
            asked.append((question, options))
            return "Reject"

        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "rm note.txt", "purpose": "cleanup"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "risky", "reasoning": "destructive", "pattern": "rm *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "blocked"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=ask_user,
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "blocked"}, result)
        self.assertEqual(1, len(asked))
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        cache = json.loads((self.data_dir / "safety-cache.json").read_text(encoding="utf-8"))
        self.assertIsNone(commands[0]["execution"])
        self.assertEqual("user", commands[0]["safety"]["source"])
        self.assertEqual("rejected", cache[0]["verdict"])

    def test_risky_command_approved_by_user_runs_and_is_cached_safe(self):
        asked = []

        def ask_user(question, options):
            asked.append((question, options))
            return "Approve"

        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "risky", "reasoning": "could write", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=ask_user,
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        self.assertEqual(1, len(asked))
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        cache = json.loads((self.data_dir / "safety-cache.json").read_text(encoding="utf-8"))
        self.assertEqual("user", commands[0]["safety"]["source"])
        self.assertEqual("hello\n", commands[0]["execution"]["stdout"])
        self.assertEqual("safe", cache[0]["verdict"])

    def test_blocked_review_does_not_ask_user_and_returns_block_reason(self):
        asked = []

        def ask_user(question, options):
            asked.append((question, options))
            return "Approve"

        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "rm -rf /tmp/x", "purpose": "cleanup"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "blocked", "reasoning": "destructive", "pattern": "rm -rf *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=ask_user,
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual([], asked)
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual("blocked", commands[0]["safety"]["verdict"])
        self.assertIsNone(commands[0]["execution"])
        self.assertIn("destructive", conversation[2]["content"])

    def test_budget_exhaustion_returns_summary_before_next_turn(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                )
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {
                        "pattern": "echo *",
                        "verdict": "safe",
                        "source": "review_model",
                        "timestamp": "2026-03-26T12:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        budget = BudgetRef(remaining=1)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("budget_exhausted", result["type"])
        self.assertIn("echo hello", result["summary"])
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual("hello\n", commands[0]["execution"]["stdout"])

    def test_missing_template_variable_returns_error(self):
        client = FakeClient([])
        budget = BudgetRef(remaining=1)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("error", result["type"])
        self.assertIn("Missing template variable", result["error"])

    def test_feedback_is_logged_and_sent_after_system_message(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                )
            ]
        )
        budget = BudgetRef(remaining=2)
        feedback = [{"kind": "review_rejection", "message": "avoid shell pipes"}]

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            feedback=feedback,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        logged_feedback = json.loads((self.log_dir / "feedback.json").read_text(encoding="utf-8"))
        request_log = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(feedback, logged_feedback)
        self.assertEqual("system", request_log[0]["messages"][0]["role"])
        self.assertEqual("user", request_log[0]["messages"][1]["role"])
        self.assertIn("avoid shell pipes", request_log[0]["messages"][1]["content"])
        self.assertEqual("primary", request_log[0]["model"])

    def test_cache_hit_rejected_returns_rejected_status_without_review_call(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "rm note.txt", "purpose": "cleanup"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "handled"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {
                        "pattern": "rm *",
                        "verdict": "rejected",
                        "source": "user",
                        "reasoning": "destructive",
                        "timestamp": "2026-03-26T12:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        budget = BudgetRef(remaining=3)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(2, len(client.requests))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        command_results = json.loads(conversation[2]["content"])
        self.assertEqual("rejected", command_results["results"][0]["status"])

    def test_review_invalid_json_retries_and_spends_budget_twice(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response("not json"),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=6)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        requests = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(4, len(requests))
        self.assertEqual(2, budget.remaining)

    def test_primary_api_retries_do_not_consume_extra_budget(self):
        client = FakeClient(
            [
                RetryableError("HTTP 500"),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=2)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        self.assertEqual(1, budget.remaining)
        self.assertEqual(2, len(client.requests))

    def test_all_api_retries_fail_returns_error_without_extra_budget_spend(self):
        client = FakeClient(
            [
                RetryableError("HTTP 500"),
                RetryableError("HTTP 500"),
                RetryableError("HTTP 500"),
                RetryableError("HTTP 500"),
            ]
        )
        budget = BudgetRef(remaining=2)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("error", result["type"])
        self.assertIn("HTTP 500", result["error"])
        self.assertEqual(1, budget.remaining)
        self.assertEqual(4, len(client.requests))

    def test_http_429_retries_and_succeeds(self):
        client = FakeClient(
            [
                RetryableError("HTTP 429"),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=2)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)

    def test_network_error_retries_and_returns_error(self):
        client = FakeClient(
            [
                RetryableError("connection refused"),
                RetryableError("connection refused"),
                RetryableError("connection refused"),
                RetryableError("connection refused"),
            ]
        )
        budget = BudgetRef(remaining=2)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("error", result["type"])
        self.assertIn("connection refused", result["error"])

    def test_corrupt_cache_is_replaced_with_valid_json_after_review(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        (self.data_dir / "safety-cache.json").write_text("{not json", encoding="utf-8")
        budget = BudgetRef(remaining=5)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        repaired_cache = json.loads((self.data_dir / "safety-cache.json").read_text(encoding="utf-8"))
        self.assertEqual("echo *", repaired_cache[0]["pattern"])
        warnings = json.loads((self.log_dir / "warnings.json").read_text(encoding="utf-8"))
        self.assertIn("Corrupt safety cache ignored", warnings[0])

    def test_cache_survives_across_invocations(self):
        first_client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        second_client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo again", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.root / "logs" / "run-1",
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=first_client,
            sleep_fn=lambda seconds: None,
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=3),
            log_dir=self.root / "logs" / "run-2",
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=second_client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(2, len(second_client.requests))

    def test_missing_cache_file_gets_created_on_first_review(self):
        cache_path = self.data_dir / "safety-cache.json"
        if cache_path.exists():
            cache_path.unlink()
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertTrue(cache_path.exists())

    def test_human_edited_cache_is_respected(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "docker build .", "purpose": "build"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {"pattern": "docker *", "verdict": "rejected", "source": "user", "reasoning": "blocked", "timestamp": "2026-03-26T12:00:00Z"}
                ]
            ),
            encoding="utf-8",
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=3),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(2, len(client.requests))

    def test_three_consecutive_validation_failures_return_error(self):
        client = FakeClient(
            [
                make_response("not json"),
                make_response('{"type":"wat"}'),
                make_response('{"type":"answer","answer":{"wrong":"shape"}}'),
            ]
        )
        budget = BudgetRef(remaining=5)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("error", result["type"])
        self.assertIn("3 consecutive validation failures", result["error"])
        requests = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        responses = json.loads((self.log_dir / "response.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual(3, len(requests))
        self.assertEqual(3, len(responses))
        self.assertEqual("assistant", conversation[-1]["role"])

    def test_validation_failure_counter_resets_after_valid_command_turn(self):
        client = FakeClient(
            [
                make_response("not json"),
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response("not json"),
                make_response("still not json"),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=8)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)

    def test_budget_zero_before_first_call_returns_budget_exhausted_without_request_logs(self):
        client = FakeClient([])
        budget = BudgetRef(remaining=0)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("budget_exhausted", result["type"])
        self.assertFalse((self.log_dir / "request.json").exists())
        self.assertFalse((self.log_dir / "response.json").exists())

    def test_budget_accounting_combined_case_spends_four(self):
        client = FakeClient(
            [
                make_response("not json"),
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=6)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        self.assertEqual(2, budget.remaining)

    def test_budget_exhaustion_during_review_stops_before_command_runs(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=2)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("budget_exhausted", result["type"])
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertIsNone(commands[0]["execution"])

    def test_user_abort_from_other_text_returns_user_aborted(self):
        def ask_user(question, options):
            return {"choice": "Other", "text": "stop everything"}

        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "rm note.txt", "purpose": "cleanup"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "risky", "reasoning": "destructive", "pattern": "rm *"},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=4)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=ask_user,
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("user_aborted", result["type"])
        self.assertTrue((self.log_dir / "request.json").exists())
        self.assertTrue((self.log_dir / "response.json").exists())
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertIsNone(commands[0]["execution"])

    def test_missing_prompt_file_returns_error(self):
        client = FakeClient([])
        budget = BudgetRef(remaining=1)

        result = run_agent_loop(
            goal="missing",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("error", result["type"])
        self.assertIn("Prompt template not found", result["error"])

    def test_system_message_contains_schema_and_protocol_instructions(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                )
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="primary",
            budget=BudgetRef(remaining=2),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        request_log = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        system_message = request_log[0]["messages"][0]["content"]
        self.assertIn('"summary"', system_message)
        self.assertIn('{"type":"command"', system_message)
        self.assertIn('{"type":"answer"', system_message)

    def test_response_body_is_logged_verbatim(self):
        raw = make_response(
            json.dumps(
                {"type": "answer", "answer": {"summary": "done"}},
                separators=(",", ":"),
            )
        )
        client = FakeClient([raw])

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=2),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        responses = json.loads((self.log_dir / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(raw, responses[0])

    def test_same_model_for_primary_and_review_still_works(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "echo *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="same-model",
            review_model="same-model",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        requests = json.loads((self.log_dir / "request.json").read_text(encoding="utf-8"))
        self.assertEqual({"summary": "done"}, result)
        self.assertEqual("same-model", requests[0]["model"])
        self.assertEqual("same-model", requests[1]["model"])


@unittest.skipUnless(os.environ.get("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY not set")
class AgentLoopIntegrationTests(unittest.TestCase):
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

    _repo_root = Path(__file__).resolve().parents[3]

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
        self.fail("fsrouter server did not start")

    def _run_build(self, change_description, change_id):
        return run_agent_loop(
            goal="build",
            template_vars={"change_description": change_description, "context": "Create files directly under routes/ as needed."},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1"),
            review_model=os.environ.get("OPENROUTER_REVIEW_MODEL", os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1-mini")),
            budget=BudgetRef(remaining=int(os.environ.get("AGENT_LOOP_TEST_BUDGET", "25"))),
            log_dir=self.log_dir / change_id,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
        )

    def _read_text_if_exists(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_qr_code_reader_integration(self):
        result = self._run_build(
            (
                "Create a web app with a page that opens the device camera, detects QR codes in the "
                "video feed, and displays the decoded contents. When a QR code is detected, present "
                "actions that fit the content and always include Say it and Use as change prompt."
            ),
            "qr-code-reader",
        )
        self.assertNotIn(result.get("type") if isinstance(result, dict) else None, {"error", "budget_exhausted", "user_aborted"})
        self.assertTrue(any(self.routes_dir.rglob("*")))
        commands = json.loads((self.log_dir / "qr-code-reader" / "commands.json").read_text(encoding="utf-8"))
        self.assertTrue(any("routes" in entry["command"] or "find" in entry["command"] or "ls" in entry["command"] for entry in commands))
        server, base_url = self._start_server()
        try:
            html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
        finally:
            server.terminate()
            server.wait(timeout=5)
        self.assertTrue("<video" in html or "getUserMedia" in html)
        self.assertTrue("jsQR" in html or "zxing" in html or "qr" in html.lower())
        self.assertTrue("Say it" in html or "say it" in html.lower())
        self.assertIn("change", html.lower())

    def test_network_scanner_integration(self):
        result = self._run_build(
            (
                "Create a web app that scans the local network on the server side, shows progress, "
                "renders results in a table, and includes a topology graph."
            ),
            "network-scanner",
        )
        self.assertNotIn(result.get("type") if isinstance(result, dict) else None, {"error", "budget_exhausted", "user_aborted"})
        commands = json.loads((self.log_dir / "network-scanner" / "commands.json").read_text(encoding="utf-8"))
        self.assertTrue(any(("which nmap" in entry["command"]) or ("which arp" in entry["command"]) or ("command -v" in entry["command"]) for entry in commands))
        server, base_url = self._start_server()
        try:
            html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
        finally:
            server.terminate()
            server.wait(timeout=5)
        self.assertTrue("<table" in html or "services" in html.lower())
        self.assertTrue("svg" in html.lower() or "canvas" in html.lower() or "graph" in html.lower())
        self.assertTrue("setInterval" in html or "setTimeout" in html or "fetch(" in html)

    def test_scheduler_frontend_integration(self):
        result = self._run_build(
            (
                "Create a web app frontend for cron and launchd task management on macOS, including "
                "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                "agents, and confirming destructive actions."
            ),
            "scheduler",
        )
        self.assertNotIn(result.get("type") if isinstance(result, dict) else None, {"error", "budget_exhausted", "user_aborted"})
        commands = json.loads((self.log_dir / "scheduler" / "commands.json").read_text(encoding="utf-8"))
        self.assertTrue(any(("crontab -l" in entry["command"]) or ("launchctl list" in entry["command"]) or ("LaunchAgents" in entry["command"]) for entry in commands))
        server, base_url = self._start_server()
        try:
            html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
        finally:
            server.terminate()
            server.wait(timeout=5)
        self.assertTrue("<table" in html or "scheduled" in html.lower())
        self.assertTrue("form" in html.lower() or "schedule" in html.lower())
        self.assertTrue("confirm(" in html or "remove" in html.lower())

    def test_log_dir_attribute_exists(self):
        self.assertTrue(
            hasattr(self, "log_dir"),
            "setUp must create self.log_dir (not self.logs_dir); "
            "FakeClient-based integration tests depend on this name",
        )
        self.assertTrue(self.log_dir.is_dir(), f"self.log_dir ({self.log_dir}) must be a directory")

    def test_plan_prompt_file_exists(self):
        plan_path = self.prompts_dir / "plan.txt"
        self.assertTrue(
            plan_path.is_file(),
            f"setUp must create {plan_path}; FakeClient-based tests use goal='plan' "
            "and will silently return error dicts if the prompt file is missing",
        )

    def test_build_prompt_file_exists(self):
        build_path = self.prompts_dir / "build.txt"
        self.assertTrue(
            build_path.is_file(),
            f"setUp must create {build_path}; _run_build tests use goal='build' "
            "and will silently return error dicts if the prompt file is missing",
        )

    def test_missing_prompt_returns_error_not_crash(self):
        client = FakeClient([])
        result = run_agent_loop(
            goal="nonexistent_goal",
            template_vars={"change_description": "x", "context": "x"},
            output_schema={"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}, "additionalProperties": False},
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=3),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )
        self.assertEqual("error", result["type"])
        self.assertIn("Prompt template not found", result["error"])
        self.assertEqual(0, len(client.requests),
            "No API calls should be made when the prompt file is missing")

    def test_fakeclient_plan_goal_does_not_error_on_prompt(self):
        client = FakeClient([
            make_response(json.dumps({"type": "answer", "answer": {"summary": "ok"}}, separators=(",", ":")))
        ])
        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "test", "context": "none"},
            output_schema={"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}, "additionalProperties": False},
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=2),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )
        self.assertNotEqual("error", result.get("type"),
            f"goal='plan' should not fail with a prompt error; got: {result}")

    def test_repo_root_contains_fsrouter_script(self):
        script = self._repo_root / "python" / "fsrouter.py"
        self.assertTrue(
            script.is_file(),
            f"Expected fsrouter.py at {script}; _repo_root ({self._repo_root}) "
            "may be wrong — it must not contain hardcoded user paths",
        )

    def test_repo_root_is_derived_from_file_location(self):
        expected = Path(__file__).resolve().parents[3]
        self.assertEqual(
            expected,
            self._repo_root,
            f"_repo_root must be derived from __file__, not hardcoded; "
            f"expected {expected}, got {self._repo_root}",
        )

    def test_cache_pattern_wildcard_matching(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [
                                {"command": "grep -r foo src", "purpose": "search"},
                                {"command": "grep -r bar lib", "purpose": "search"},
                                {"command": "grep baz file.txt", "purpose": "search"},
                            ],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "grep *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {
                        "pattern": "grep -r *",
                        "verdict": "safe",
                        "source": "review_model",
                        "reasoning": "harmless",
                        "timestamp": "2026-03-26T12:00:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        budget = BudgetRef(remaining=4)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(3, len(client.requests))
        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual("cache", commands[0]["safety"]["source"])
        self.assertEqual("cache", commands[1]["safety"]["source"])
        self.assertEqual("review", commands[2]["safety"]["source"])

    def test_empty_commands_array_round_trips_without_crashing(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {"type": "command", "commands": [], "reasoning": "nothing to run"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=3)

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual({"summary": "done"}, result)
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        command_results = json.loads(conversation[2]["content"])
        self.assertEqual([], command_results["results"])

    def test_failing_command_is_logged_and_returned(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "false", "purpose": "fail"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "false *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual(1, commands[0]["execution"]["exit_code"])

    def test_command_with_stderr_captures_separate_streams(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "python3 -c \"import sys; print('out'); print('err', file=sys.stderr)\"", "purpose": "streams"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "python3 -c *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual("out\n", commands[0]["execution"]["stdout"])
        self.assertEqual("err\n", commands[0]["execution"]["stderr"])

    def test_working_directory_is_used_for_command_execution(self):
        (self.working_dir / "marker.txt").write_text("marker\n", encoding="utf-8")
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "cat marker.txt", "purpose": "read"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "cat *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual("marker\n", commands[0]["execution"]["stdout"])

    def test_large_command_output_is_fully_logged_and_returned(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "python3 -c \"print('x'*200000)\"", "purpose": "large"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "python3 -c *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=5),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        conversation = json.loads((self.log_dir / "conversation.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(commands[0]["execution"]["stdout"]), 200000)
        command_results = json.loads(conversation[2]["content"])
        self.assertGreaterEqual(len(command_results["results"][0]["stdout"]), 200000)

    def test_very_long_ai_response_is_logged_and_returned(self):
        long_summary = "x" * (1024 * 1024)
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": long_summary}},
                        separators=(",", ":"),
                    )
                )
            ]
        )

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=BudgetRef(remaining=2),
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(long_summary, result["summary"])
        responses = json.loads((self.log_dir / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(long_summary, json.loads(responses[0]["choices"][0]["message"]["content"])["answer"]["summary"])

    def test_external_budget_drop_between_turns_terminates_cleanly(self):
        budget = BudgetRef(remaining=3)

        class BudgetDroppingClient(FakeClient):
            def __call__(self, request):
                response = super().__call__(request)
                if len(self.requests) == 1:
                    budget.remaining = 0
                return response

        client = BudgetDroppingClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "echo hello", "purpose": "test"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                )
            ]
        )
        (self.data_dir / "safety-cache.json").write_text(
            json.dumps(
                [
                    {"pattern": "echo *", "verdict": "safe", "source": "review_model", "reasoning": "ok", "timestamp": "2026-03-26T12:00:00Z"}
                ]
            ),
            encoding="utf-8",
        )

        result = run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual("budget_exhausted", result["type"])

    def test_command_timeout_is_logged_and_returned(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "python3 -c \"import time; time.sleep(0.2)\"", "purpose": "wait"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "python3 -c *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
            command_timeout=0.05,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertTrue(commands[0]["execution"]["timed_out"])

    def test_binary_output_is_base64_logged(self):
        client = FakeClient(
            [
                make_response(
                    json.dumps(
                        {
                            "type": "command",
                            "commands": [{"command": "python3 -c \"import sys; sys.stdout.buffer.write(b'\\x00\\x01A')\"", "purpose": "binary"}],
                            "reasoning": "inspect",
                        },
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"verdict": "safe", "reasoning": "harmless", "pattern": "python3 -c *"},
                        separators=(",", ":"),
                    )
                ),
                make_response(
                    json.dumps(
                        {"type": "answer", "answer": {"summary": "done"}},
                        separators=(",", ":"),
                    )
                ),
            ]
        )
        budget = BudgetRef(remaining=5)

        run_agent_loop(
            goal="plan",
            template_vars={"change_description": "add feature", "context": "none"},
            output_schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            },
            model="primary",
            review_model="review",
            budget=budget,
            log_dir=self.log_dir,
            working_dir=self.working_dir,
            ask_user=lambda question, options: "Approve",
            prompts_dir=self.prompts_dir,
            data_dir=self.data_dir,
            client=client,
            sleep_fn=lambda seconds: None,
        )

        commands = json.loads((self.log_dir / "commands.json").read_text(encoding="utf-8"))
        self.assertEqual("AAFB", commands[0]["execution"]["stdout_base64"])


    # ------------------------------------------------------------------
    # Diagnostic tests – surface actual values behind integration failures
    # ------------------------------------------------------------------

    def test_network_scanner_budget_is_sufficient(self):
        """Diagnose: does the network-scanner build exhaust its budget?"""
        budget = BudgetRef(remaining=int(os.environ.get("AGENT_LOOP_TEST_BUDGET", "25")))
        initial = budget.remaining
        result = self._run_build(
            (
                "Create a web app that scans the local network on the server side, shows progress, "
                "renders results in a table, and includes a topology graph."
            ),
            "network-scanner-diag",
        )
        result_type = result.get("type") if isinstance(result, dict) else "answer"
        log_path = self.log_dir / "network-scanner-diag"
        requests_path = log_path / "request.json"
        num_requests = len(json.loads(requests_path.read_text(encoding="utf-8"))) if requests_path.exists() else 0
        commands_path = log_path / "commands.json"
        num_commands = len(json.loads(commands_path.read_text(encoding="utf-8"))) if commands_path.exists() else 0

        self.assertNotEqual(
            "budget_exhausted", result_type,
            f"Budget exhausted after {num_requests} API calls and {num_commands} commands "
            f"(started with {initial}). Increase AGENT_LOOP_TEST_BUDGET or simplify the prompt.",
        )

    def test_qr_code_reader_html_contains_camera_elements(self):
        """Diagnose: what does the QR-reader index page actually contain?"""
        result = self._run_build(
            (
                "Create a web app with a page that opens the device camera, detects QR codes in the "
                "video feed, and displays the decoded contents. When a QR code is detected, present "
                "actions that fit the content and always include Say it and Use as change prompt."
            ),
            "qr-diag",
        )
        result_type = result.get("type") if isinstance(result, dict) else "answer"
        if result_type in {"error", "budget_exhausted", "user_aborted"}:
            self.fail(f"Build did not complete: type={result_type}, result={result}")

        route_files = sorted(str(p.relative_to(self.routes_dir)) for p in self.routes_dir.rglob("*") if p.is_file())
        self.assertTrue(route_files, "No route files were created")

        server, base_url = self._start_server()
        try:
            html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
        finally:
            server.terminate()
            server.wait(timeout=5)

        snippet = html[:2000]
        has_video = "<video" in html
        has_getUserMedia = "getUserMedia" in html
        self.assertTrue(
            has_video or has_getUserMedia,
            f"Index HTML has neither <video nor getUserMedia.\n"
            f"Route files: {route_files}\n"
            f"HTML (first 2000 chars):\n{snippet}",
        )

    def test_scheduler_commands_include_system_probes(self):
        """Diagnose: what commands did the scheduler build actually run?"""
        result = self._run_build(
            (
                "Create a web app frontend for cron and launchd task management on macOS, including "
                "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                "agents, and confirming destructive actions."
            ),
            "scheduler-diag",
        )
        result_type = result.get("type") if isinstance(result, dict) else "answer"
        if result_type in {"error", "budget_exhausted", "user_aborted"}:
            self.fail(f"Build did not complete: type={result_type}, result={result}")

        commands_path = self.log_dir / "scheduler-diag" / "commands.json"
        self.assertTrue(commands_path.exists(), "commands.json was not written")
        commands = json.loads(commands_path.read_text(encoding="utf-8"))
        actual_cmds = [entry["command"] for entry in commands]

        expected_any = ["crontab -l", "launchctl list", "LaunchAgents"]
        found = [kw for kw in expected_any if any(kw in cmd for cmd in actual_cmds)]
        self.assertTrue(
            found,
            f"None of {expected_any} appear in the {len(actual_cmds)} commands that were run.\n"
            f"Actual commands:\n" + "\n".join(f"  - {c}" for c in actual_cmds),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
