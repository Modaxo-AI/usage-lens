"""Two ways a request could be miscounted, found by checking this tool against
Anthropic's guidance and community trackers:

1. Anthropic's rule is to count each `message.id` once. Keying on `requestId`
   double-counts a reply that a sidechain replay rewrote under a new request id
   (ccusage PR #1155).
2. Newer Claude Code writes Workflow-agent transcripts in a subfolder of
   `subagents/` (anthropics/claude-code #97258); reading only the top level of
   that folder silently drops their usage.
"""
import json
import os
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.scan import discover, scan


def _assistant(sid, ts, msg_id, request_id, out=100, cache_read=1000, sidechain=False):
    line = {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj4",
            "isSidechain": sidechain,
            "message": {"id": msg_id, "role": "assistant", "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_read_input_tokens": cache_read}}}
    if request_id is not None:
        line["requestId"] = request_id
    return line


def _write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(l) for l in lines) + "\n")


class _Logs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.project = os.path.join(self.root, "-Users-x-proj4")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return scan(self.root, cache=FileCache(os.path.join(self.root, "c.json")))

    @staticmethod
    def output(payload):
        return sum(r[6] for r in payload["usage"])

    @staticmethod
    def requests(payload):
        return sum(r[12] for r in payload["usage"])


class TestCountEachMessageOnce(_Logs):
    def test_a_replay_under_a_new_request_id_is_counted_once(self):
        _write(os.path.join(self.project, "sess-a.jsonl"), [
            _assistant("sess-a", "2026-03-02T10:00:00.000Z", "msg_1", "req_1", out=100),
            # The same reply, rewritten by a sidechain replay under a new request id.
            _assistant("sess-a", "2026-03-02T10:05:00.000Z", "msg_1", "req_2", out=100, sidechain=True),
            _assistant("sess-a", "2026-03-02T10:06:00.000Z", "msg_2", "req_3", out=40),
        ])
        p = self.scan()
        self.assertEqual(self.requests(p), 2)
        self.assertEqual(self.output(p), 140)

    def test_the_same_message_across_files_is_counted_once(self):
        _write(os.path.join(self.project, "sess-a.jsonl"), [
            _assistant("sess-a", "2026-03-02T10:00:00.000Z", "msg_1", "req_1", out=100)])
        _write(os.path.join(self.project, "sess-a", "subagents", "agent-x.jsonl"), [
            _assistant("sess-a", "2026-03-02T10:01:00.000Z", "msg_1", "req_9", out=100, sidechain=True),
            _assistant("sess-a", "2026-03-02T10:02:00.000Z", "msg_2", "req_10", out=7, sidechain=True)])
        p = self.scan()
        self.assertEqual(self.requests(p), 2)
        self.assertEqual(self.output(p), 107)

    def test_the_most_complete_record_wins(self):
        # Subagent transcripts can keep a placeholder output count on some lines.
        _write(os.path.join(self.project, "sess-a.jsonl"), [
            _assistant("sess-a", "2026-03-02T10:00:00.000Z", "msg_1", "req_1", out=1),
            _assistant("sess-a", "2026-03-02T10:00:01.000Z", "msg_1", "req_1", out=250)])
        self.assertEqual(self.output(self.scan()), 250)

    def test_without_a_message_id_the_request_id_is_used(self):
        a = _assistant("sess-a", "2026-03-02T10:00:00.000Z", None, "req_1", out=5)
        b = _assistant("sess-a", "2026-03-02T10:00:01.000Z", None, "req_1", out=5)
        del a["message"]["id"], b["message"]["id"]
        _write(os.path.join(self.project, "sess-a.jsonl"), [a, b])
        p = self.scan()
        self.assertEqual((self.requests(p), self.output(p)), (1, 5))


class TestNestedSubagentTranscripts(_Logs):
    def test_workflow_transcripts_in_a_subfolder_are_read(self):
        _write(os.path.join(self.project, "sess-w.jsonl"), [
            _assistant("sess-w", "2026-03-02T10:00:00.000Z", "msg_main", "req_main", out=10)])
        _write(os.path.join(self.project, "sess-w", "subagents", "workflows", "wf-run-1.jsonl"), [
            _assistant("sess-w", "2026-03-02T10:01:00.000Z", "msg_wf", "req_wf", out=500, sidechain=True)])
        p = self.scan()
        self.assertEqual(self.output(p), 510)
        wf = [r for r in p["usage"] if r[6] == 500]
        self.assertEqual(wf[0][1], "sess-w", "credited to the parent session")
        self.assertNotEqual(wf[0][4], -1, "counted as subagent work, not main-thread")

    def test_discovery_lists_nested_files_with_their_meta(self):
        sub = os.path.join(self.project, "sess-w", "subagents")
        _write(os.path.join(sub, "agent-top.jsonl"), [])
        _write(os.path.join(sub, "workflows", "agent-deep.jsonl"), [])
        with open(os.path.join(sub, "workflows", "agent-deep.meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"agentType": "workflow"}, fh)
        _, subs = discover(self.root)
        found = {(agent_id, bool(meta)) for _, _, agent_id, _, meta in subs}
        self.assertEqual(found, {("top", False), ("deep", True)})

    def test_non_transcript_files_are_ignored(self):
        sub = os.path.join(self.project, "sess-w", "subagents")
        _write(os.path.join(sub, "agent-top.jsonl"), [])
        with open(os.path.join(sub, "agent-top.meta.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        _, subs = discover(self.root)
        self.assertEqual(len(subs), 1)


if __name__ == "__main__":
    unittest.main()
