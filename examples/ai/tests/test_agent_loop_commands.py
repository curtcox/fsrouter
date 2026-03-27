import json
import unittest

from examples.ai.agent_loop import BudgetRef, run_agent_loop
from examples.ai.tests.test_support import AgentLoopTestCase, FakeClient, make_response

class AgentLoopCommandTests(AgentLoopTestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
