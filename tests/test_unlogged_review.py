"""The "not in transcripts" figure: what Claude Code's own counter saw that no
transcript holds. Two ways it read high."""
import json
import os
import tempfile
import unittest

from claude_usage.aggregate import build_payload
from claude_usage.parser import parse_file


def _reply(mid, ts, model="claude-opus-5-5", inp=10, out=100, cache_read=0):
    return {"type": "assistant", "timestamp": ts, "uuid": mid + "-u", "requestId": "req-" + mid,
            "message": {"id": mid, "model": model, "stop_reason": "end_turn", "content": [],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": 0}}}


def _counter(start_ms, cost, model_usage):
    return {"type": "cost-state", "totalCostUSD": cost, "startTime": start_ms, "modelUsage": model_usage}


class TestUnlogged(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name, records):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return parse_file(path, "main")

    def _sessions(self, **files):
        payload = build_payload({sid: {"facts": f, "folder": "proj"} for sid, f in files.items()},
                                [], "2026-10-29T00:00:00+00:00")
        return {s["id"]: s for s in payload["sessions"]}

    def test_a_fork_winning_copied_requests_does_not_make_them_unlogged(self):
        # A: 10 replies and a counter covering exactly them. B, a fork with its
        # own counter, copies 6 of them and adds 20, so it wins the 6. A's
        # counter still saw them in A's own file: nothing is missing.
        a = [_reply(f"m{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(10)]
        a.append(_counter(1760954400000, 0.0204, {"claude-opus-5-5": {
            "inputTokens": 100, "outputTokens": 1000, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.0204}}))
        b = ([_reply(f"m{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(6)]
             + [_reply(f"n{i}", f"2026-10-20T12:{i:02d}:00.000Z") for i in range(20)])
        b.append(_counter(1760961600000, 0.0408, {"claude-opus-5-5": {
            "inputTokens": 200, "outputTokens": 2000, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.0408}}))
        s = self._sessions(A=self._file("A.jsonl", a), B=self._file("B.jsonl", b))
        self.assertEqual(s["A"]["est_cost_unlogged"], 0)
        self.assertEqual(s["B"]["est_cost_unlogged"], 0)

    def test_many_small_haiku_calls_are_not_priced_at_the_long_prompt_tier(self):
        # The counter saw 40 background calls of about 5K tokens each that no
        # transcript holds. Added together they pass Haiku 5.5's 100K
        # threshold; no single call did.
        a = [_reply("m0", "2026-10-20T10:00:00.000Z")]
        a.append(_counter(1760954400000, 0.01, {
            "claude-opus-5-5": {"inputTokens": 10, "outputTokens": 100, "cacheReadInputTokens": 0,
                                "cacheCreationInputTokens": 0, "costUSD": 0.00204},
            "claude-haiku-5-5": {"inputTokens": 4000, "outputTokens": 2000, "cacheReadInputTokens": 200000,
                                 "cacheCreationInputTokens": 0, "costUSD": 0.0034}}))
        s = self._sessions(A=self._file("A.jsonl", a))
        haiku = {m["model"]: m for m in s["A"]["unlogged_models"]}["claude-haiku-5-5"]
        self.assertAlmostEqual(haiku["cost"], 0.0034, places=6)


if __name__ == "__main__":
    unittest.main()
