"""A transcript line that is valid JSON but not the shape Claude Code writes
must cost that one record, never the whole scan. Each case here used to raise
out of parse_file (or parse_meta) and take every other file down with it."""
import json
import os
import tempfile
import unittest

from claude_usage.parser import parse_file, parse_meta

_GOOD = {"type": "assistant", "timestamp": "2026-01-01T10:00:00.000Z", "requestId": "req-1",
         "message": {"id": "msg-1", "model": "claude-sonnet-5",
                     "usage": {"input_tokens": 10, "output_tokens": 20}}}


class _TmpFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *lines, name="s.jsonl"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path


class TestOddRecordsAreSkipped(_TmpFile):
    def assert_survives(self, odd):
        facts = parse_file(self.write(odd, _GOOD), "main")
        self.assertEqual([r["response_id"] for r in facts.requests], ["msg-1"])
        return facts

    def test_a_byte_order_mark_does_not_cost_the_first_record(self):
        # A file saved by an editor on Windows can start with one.
        path = self.write(_GOOD)
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + data)
        self.assertEqual([r["response_id"] for r in parse_file(path, "main").requests], ["msg-1"])

    def test_a_json_array_line(self):
        self.assert_survives("[1, 2]")

    def test_a_json_null_line(self):
        self.assert_survives("null")

    def test_a_json_scalar_line(self):
        self.assert_survives('"just a string"')

    def test_a_string_message(self):
        self.assert_survives({"type": "user", "timestamp": "2026-01-01T09:00:00Z", "message": "hi"})

    def test_a_non_dict_usage(self):
        self.assert_survives({"type": "assistant", "timestamp": "2026-01-01T09:00:00Z",
                              "message": {"id": "msg-0", "model": "claude-sonnet-5", "usage": [1, 2]}})

    def test_non_dict_usage_details(self):
        # The record itself is still a request; only the malformed details are ignored.
        facts = parse_file(self.write({**_GOOD, "message": {**_GOOD["message"], "usage": {
            "input_tokens": 5, "output_tokens": 1, "cache_creation": "x",
            "output_tokens_details": 3, "server_tool_use": [1],
            "cache_creation_input_tokens": 7}}}), "main")
        rec, = facts.requests
        self.assertEqual((rec["input_tokens"], rec["cache_write_5m"], rec["cache_write_fallback"]), (5, 0, 7))

    def test_non_string_metadata_fields(self):
        facts = self.assert_survives({"type": "user", "timestamp": "2026-01-01T09:00:00Z",
                                      "cwd": ["/p"], "entrypoint": ["cli"], "version": {"v": 1},
                                      "gitBranch": 3})
        self.assertIsNone(facts.first_cwd)
        self.assertEqual(sum(facts.entrypoints.values()), 0)

    def test_a_numeric_timestamp(self):
        facts = self.assert_survives({"type": "user", "timestamp": 1767261600, "cwd": "/p"})
        self.assertEqual(facts.first_ts, "2026-01-01T10:00:00.000Z")

    def test_non_dict_tool_input_and_compact_metadata(self):
        self.assert_survives({"type": "assistant", "timestamp": "2026-01-01T09:00:00Z",
                              "message": {"content": [{"type": "tool_use", "id": "tu-1",
                                                       "name": "Skill", "input": "dataviz"}]}})
        self.assert_survives({"type": "system", "subtype": "compact_boundary",
                              "timestamp": "2026-01-01T09:00:00Z", "compactMetadata": 5})


class TestLimitNoticeDoesNotRelabelTheFile(_TmpFile):
    def test_requests_after_a_limit_notice_in_a_subagent_file_stay_subagent(self):
        notice = {"type": "assistant", "timestamp": "2026-01-01T09:00:00Z", "uuid": "u1",
                  "isApiErrorMessage": True, "error": "rate_limit",
                  "message": {"content": [{"type": "text",
                                           "text": "You've hit your weekly limit · resets 3pm (UTC)"}]}}
        facts = parse_file(self.write(notice, _GOOD), "sub")
        self.assertEqual(facts.kind, "sub")
        self.assertEqual(facts.limit_events[0]["kind"], "weekly")
        self.assertTrue(facts.requests[0]["is_sub"])


class TestParseMeta(_TmpFile):
    def test_null_meta_is_empty(self):
        self.assertEqual(parse_meta(self.write("null", name="a.meta.json")), {})

    def test_array_meta_is_empty(self):
        self.assertEqual(parse_meta(self.write("[1]", name="a.meta.json")), {})


if __name__ == "__main__":
    unittest.main()
