import json
from pathlib import Path
import unittest

from examples.ai.agent_loop import BudgetRef, run_agent_loop
from examples.ai.tests.test_support import AgentLoopBuildTestCase, FakeClient, make_response

class AgentLoopBuildHarnessTests(AgentLoopBuildTestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
