"""A branched or resumed transcript can carry on its parent's CLI cost counter
(same cost-state startTime) and so inherit the parent's running totals. Taking
each transcript's snapshot at face value counted one counter once per copy --
on a real log set that overstated CLI-reported cost by 70% and lines changed by
63%. These tests pin the counter to one row and one count."""
import datetime
import json
import os
import tempfile
import unittest

from claude_usage import export as export_mod
from claude_usage.cache import FileCache
from claude_usage.scan import scan

PERSON = ("someone@example.com", "Some One")


def _req(sid, ts, rid):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj3",
            "requestId": rid, "message": {"id": "m-" + rid, "role": "assistant",
            "model": "claude-sonnet-5", "usage": {"input_tokens": 1000, "output_tokens": 2000}}}


def _state(sid, ts, start, cost, lines):
    return {"type": "cost-state", "sessionId": sid, "timestamp": ts, "totalCostUSD": cost,
            "totalAPIDuration": 1000, "totalToolDuration": 100, "totalDuration": 2000,
            "totalLinesAdded": lines, "totalLinesRemoved": 1, "startTime": start,
            "modelUsage": {"claude-sonnet-5": {"inputTokens": 1000, "outputTokens": 2000,
                                               "costUSD": cost}}}


class TestSharedCounters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = os.path.join(cls.tmp.name, "-Users-x-proj3")
        os.makedirs(folder)
        files = {
            # The long session: most of the requests.
            "sess-parent": [_req("sess-parent", f"2026-03-02T10:0{i}:00.000Z", f"p{i}") for i in range(3)]
                           + [_state("sess-parent", "2026-03-02T10:05:00.000Z", 111, 1.00, 100)],
            # A short branch that carried on the same counter: its snapshot
            # includes everything the parent did.
            "sess-branch": [_req("sess-branch", "2026-03-02T11:00:00.000Z", "b0")]
                           + [_state("sess-branch", "2026-03-02T11:01:00.000Z", 111, 1.20, 120)],
            # Unrelated: its own counter.
            "sess-other": [_req("sess-other", "2026-03-03T09:00:00.000Z", "o0")]
                          + [_state("sess-other", "2026-03-03T09:01:00.000Z", 222, 0.50, 5)],
        }
        for sid, lines in files.items():
            with open(os.path.join(folder, sid + ".jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
        cls.payload = scan(cls.tmp.name, cache=FileCache(os.path.join(cls.tmp.name, "c.json")),
                           tz=datetime.timezone.utc)  # exported in the default (UTC) export zone
        cls.s = {s["id"]: s for s in cls.payload["sessions"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counter_shows_once_on_the_session_that_did_the_work(self):
        parent, branch = self.s["sess-parent"], self.s["sess-branch"]
        self.assertEqual(parent["cli_cost"], 1.20, "the counter's latest snapshot")
        self.assertEqual(parent["lines_added"], 120)
        self.assertEqual(parent["cli_counter_shared_with"], ["sess-branch"])
        self.assertIsNone(branch["cli_cost"])
        self.assertIsNone(branch["lines_added"])
        self.assertEqual(branch["cli_counted_in"], "sess-parent")

    def test_unshared_counters_are_untouched(self):
        other = self.s["sess-other"]
        self.assertEqual((other["cli_cost"], other["lines_added"]), (0.50, 5))
        self.assertIsNone(other["cli_counted_in"])

    def test_reconciliation_counts_each_counter_once_against_its_whole_group(self):
        row, = self.payload["reconciliation"]
        self.assertAlmostEqual(row["cli_cost"], 1.70, places=6)
        self.assertEqual(row["sessions"], 3)
        est_all = sum(r[13] for r in self.payload["usage"])
        self.assertAlmostEqual(row["est_cost"], est_all, places=6)

    def test_parent_compares_against_the_groups_estimate(self):
        model, = self.s["sess-parent"]["cli_models"]
        est_group = sum(r[13] for r in self.payload["usage"] if r[1] in ("sess-parent", "sess-branch"))
        self.assertAlmostEqual(model["est_cost"], est_group, places=6)

    def test_export_sums_no_longer_double_count(self):
        ctx = export_mod.build_context(self.payload, PERSON)
        summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                   for r in export_mod.summary_rows(self.payload, PERSON, ctx=ctx)]
        sessions = [dict(zip(export_mod.SESSION_COLUMNS, r))
                    for r in export_mod.session_rows(self.payload, PERSON, ctx=ctx)]
        self.assertEqual(sum(r["lines_added"] or 0 for r in summary), 125)
        self.assertEqual(sum(r["lines_added"] or 0 for r in sessions), 125)
        self.assertAlmostEqual(sum(r["cli_reported_cost_usd"] or 0 for r in sessions), 1.70, places=6)


if __name__ == "__main__":
    unittest.main()
