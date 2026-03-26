import json
import tempfile
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
