import os
import unittest

from claude_usage.parser import parse_file

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
MAIN_1 = os.path.join(FIXTURES, "-Users-x-proj1", "sess-main-1.jsonl")


class TestInFileDedupe(unittest.TestCase):
    def setUp(self):
        self.facts = parse_file(MAIN_1, "main")

    def _get(self, request_id):
        matches = [r for r in self.facts.requests if r["request_id"] == request_id]
        self.assertEqual(len(matches), 1, f"expected exactly one record for {request_id}")
        return matches[0]

    def test_keeps_largest_output_tokens(self):
        rec = self._get("req-dup-1")
        self.assertEqual(rec["output_tokens"], 300)

    def test_keeps_fields_from_the_winning_line_not_a_merge(self):
        rec = self._get("req-dup-1")
        self.assertEqual(rec["input_tokens"], 100)
        self.assertEqual(rec["cache_read_tokens"], 500)
        self.assertEqual(rec["cache_write_5m"], 10)
        self.assertEqual(rec["cache_write_1h"], 20)

    def test_no_duplicate_request_ids_in_output(self):
        ids = [r["request_id"] for r in self.facts.requests]
        self.assertEqual(len(ids), len(set(ids)))

    def test_distinct_requests_present(self):
        ids = {r["request_id"] for r in self.facts.requests}
        self.assertEqual(ids, {"req-dup-1", "req-2", "req-3", "msg-5"})

    def test_message_id_fallback_when_no_request_id(self):
        # This line has no top-level requestId, only message.id ("msg-5");
        # it must still be captured as its own request via that fallback.
        rec = self._get("msg-5")
        self.assertEqual(rec["output_tokens"], 9)


if __name__ == "__main__":
    unittest.main()
