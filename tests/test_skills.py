import json
import os
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.parser import parse_file
from claude_usage.scan import scan

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
MAIN_1 = os.path.join(FIXTURES, "-Users-x-proj1", "sess-main-1.jsonl")


class TestSkillParsing(unittest.TestCase):
    def setUp(self):
        self.facts = parse_file(MAIN_1, "main")

    def test_skill_tool_invocation_captured(self):
        skills = [s["skill"] for s in self.facts.skill_invocations]
        self.assertEqual(skills, ["dataviz"])

    def test_skill_invocation_keeps_its_tool_use_id(self):
        self.assertEqual([s["id"] for s in self.facts.skill_invocations], ["tu-skill-1"])

    def test_attribution_skill_on_request(self):
        rec = next(r for r in self.facts.requests if r["request_id"] == "req-dup-1")
        self.assertEqual(rec["skill"], "dataviz")

    def test_request_without_attribution_skill_is_none(self):
        rec = next(r for r in self.facts.requests if r["request_id"] == "req-2")
        self.assertIsNone(rec["skill"])


def _skill_call(sid, ts, msg_id, tool_use_id):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj5",
            "requestId": "req-" + msg_id,
            "message": {"id": msg_id, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                        "content": [{"type": "tool_use", "id": tool_use_id, "name": "Skill",
                                     "input": {"skill": "dataviz"}}]}}


class TestSkillCallsAcrossForkCopies(unittest.TestCase):
    """A fork/resume copy repeats the original's Skill calls. Tool calls are
    counted once by tool_use_id; skill calls have to be too, and credited to the
    original session rather than the copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        project = os.path.join(self.tmp.name, "-Users-x-proj5")
        os.makedirs(project)
        original = [_skill_call("sess-orig", "2026-01-01T10:00:00Z", "msg-a", "tu-skill-a"),
                    _skill_call("sess-orig", "2026-01-01T10:05:00Z", "msg-b", "tu-skill-b")]
        # The copy sorts first by name, so file order alone would credit it.
        for name, lines in (("sess-orig.jsonl", original), ("a-copy.jsonl", original[:1])):
            with open(os.path.join(project, name), "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_skill_call_counted_once_on_the_original(self):
        payload = scan(self.tmp.name, cache=FileCache(os.path.join(self.tmp.name, "c.json")))
        derived = {s["id"] for s in payload["sessions"] if s["derived"]}
        self.assertEqual(derived, {"a-copy"})
        rows = payload["skillCalls"]
        self.assertEqual(sum(r[3] for r in rows), 2)
        self.assertEqual({r[1] for r in rows}, {"sess-orig"})


if __name__ == "__main__":
    unittest.main()
