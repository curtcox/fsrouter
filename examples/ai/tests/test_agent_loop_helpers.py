import unittest

from examples.ai.agent_loop import (
    _command_pattern,
    _encode_output,
    _match_cache,
    _normalize_user_decision,
    _parse_primary_response,
    _parse_review_response,
    _validate_schema,
)
from examples.ai.tests.test_support import SUMMARY_SCHEMA


class AgentLoopHelperTests(unittest.TestCase):
    def test_parse_primary_answer_accepts_summary_schema(self):
        ok, parsed = _parse_primary_response(
            '{"type":"answer","answer":{"summary":"done"}}',
            SUMMARY_SCHEMA,
        )
        self.assertTrue(ok)
        self.assertEqual("answer", parsed["type"])
        self.assertEqual("done", parsed["answer"]["summary"])

    def test_parse_primary_command_rejects_missing_purpose(self):
        ok, error = _parse_primary_response(
            '{"type":"command","commands":[{"command":"ls"}]}',
            SUMMARY_SCHEMA,
        )
        self.assertFalse(ok)
        self.assertIn("purpose", error)

    def test_parse_review_response_requires_pattern(self):
        ok, error = _parse_review_response(
            '{"verdict":"safe","reasoning":"ok"}',
        )
        self.assertFalse(ok)
        self.assertIn("pattern", error)

    def test_command_pattern_keeps_flags_prefix(self):
        self.assertEqual("grep -r *", _command_pattern("grep -r foo src"))
        self.assertEqual("python3 *", _command_pattern("python3 script.py"))

    def test_match_cache_uses_exact_pattern(self):
        cache = [
            {"pattern": "grep -r *", "verdict": "safe"},
            {"pattern": "docker *", "verdict": "blocked"},
        ]
        self.assertEqual("safe", _match_cache(cache, "grep -r *")["verdict"])
        self.assertIsNone(_match_cache(cache, "grep *"))

    def test_encode_output_switches_to_base64_for_binary(self):
        encoded, mode = _encode_output(b"\x00\x01A")
        self.assertEqual("base64", mode)
        self.assertEqual("AAFB", encoded)

    def test_encode_output_decodes_utf8_text(self):
        encoded, mode = _encode_output("hello\n".encode("utf-8"))
        self.assertEqual("text", mode)
        self.assertEqual("hello\n", encoded)

    def test_normalize_user_decision_supports_structured_other_abort(self):
        decision = _normalize_user_decision(
            {"choice": "Other", "text": "stop everything now"}
        )
        self.assertEqual("abort", decision)

    def test_validate_schema_rejects_extra_properties(self):
        ok, error = _validate_schema(
            {"summary": "done", "extra": True},
            SUMMARY_SCHEMA,
        )
        self.assertFalse(ok)
        self.assertIn("$.extra is not allowed", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
