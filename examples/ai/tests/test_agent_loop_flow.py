import json
import urllib.error
import unittest

from examples.ai.agent_loop import BudgetRef, run_agent_loop
from examples.ai.tests.test_support import AgentLoopTestCase, FakeClient, RetryableError, make_response

class AgentLoopFlowTests(AgentLoopTestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
