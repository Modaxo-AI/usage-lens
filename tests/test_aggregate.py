"""End-to-end checks on the payload build: tool dating, fork detection, cache-write
token/cost agreement, and the CLI reconciliation."""
import collections
import json
import os
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.scan import scan

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")


def _payload():
    # A throwaway cache keeps the repo's real cache out of the tests.
    with tempfile.TemporaryDirectory() as tmp:
        return scan(FIXTURES_ROOT, cache=FileCache(os.path.join(tmp, "parsed.json")))


class TestToolDating(unittest.TestCase):
    """Tool calls belong on the day they happened, not the day the session opened."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def _tool_dates(self, session_id, name):
        # Column 0 is a 5-minute bucket; its first 10 chars are the local date.
        return {r[0][:10] for r in self.payload["tools"] if r[1] == session_id and r[2] == name}

    def test_calls_bucketed_by_their_own_date(self):
        # sess-span opens on 2026-02-01 and makes a second tool call on 02-05.
        self.assertEqual(self._tool_dates("sess-span", "Read"), {"2026-02-01"})
        self.assertEqual(self._tool_dates("sess-span", "Bash"), {"2026-02-05"})

    def test_session_start_date_is_not_used_for_every_call(self):
        dates = {r[0][:10] for r in self.payload["tools"] if r[1] == "sess-span"}
        self.assertEqual(dates, {"2026-02-01", "2026-02-05"})


class TestForkDetection(unittest.TestCase):
    """A transcript whose every request is also in another transcript is a
    resume/fork copy and must not be counted as its own session."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.by_id = {s["id"]: s for s in cls.payload["sessions"]}

    def test_full_copy_is_flagged_derived(self):
        self.assertTrue(self.by_id["sess-fork-1"]["derived"])

    def test_original_is_not_flagged(self):
        self.assertFalse(self.by_id["sess-main-1"]["derived"])

    def test_partial_overlap_is_not_a_fork(self):
        # sess-main-2 shares req-dup-1 with sess-main-1 but owns req-4.
        self.assertFalse(self.by_id["sess-main-2"]["derived"])

    def test_superset_keeps_the_shared_requests(self):
        # sess-main-1 holds more requests than the fork, so it owns req-2/req-3.
        self.assertEqual(self.by_id["sess-fork-1"]["requests"], 0)
        self.assertEqual(self.by_id["sess-fork-1"]["requests_in_file"], 2)
        self.assertEqual(self.by_id["sess-fork-1"]["est_cost_main"], 0.0)

    def test_session_count_excludes_forks(self):
        summary = self.payload["summary"]
        # Every transcript is a counted session, a fork copy, or an empty one.
        empty = sum(1 for x in self.payload["sessions"] if x["requests_in_file"] == 0)
        self.assertEqual(summary["total_transcripts"],
                         summary["total_sessions"] + summary["derived_sessions"] + empty)
        self.assertGreaterEqual(summary["derived_sessions"], 1)

    def test_fork_tool_calls_are_not_double_counted(self):
        # tu-mcp-1 appears in both sess-main-1 and its fork copy.
        calls = sum(r[3] for r in self.payload["tools"] if r[2] == "mcp__myserver__do_thing")
        self.assertEqual(calls, 1)

    def test_derived_sessions_own_no_requests(self):
        # The invariant behind the flag: a copy owns nothing, main thread or
        # subagent, so hiding it never hides cost that Total cost counts.
        owned = {}
        for r in self.payload["usage"]:
            owned[r[1]] = owned.get(r[1], 0) + r[12]
        for s in self.payload["sessions"]:
            if s["derived"]:
                self.assertEqual(owned.get(s["id"], 0), 0, s["id"])
                self.assertEqual(s["est_cost_main"] + s["est_cost_sub"], 0.0, s["id"])

    def test_every_request_has_exactly_one_owner(self):
        # Totals must not change when a transcript is copied: each request id is
        # counted once across the whole payload.
        total = sum(r[12] for r in self.payload["usage"])
        owners = sum(s["requests"] for s in self.payload["sessions"])
        self.assertEqual(total, owners)


def _request_line(sid, ts, msg_id, output=10, agent_id=None):
    line = {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj6",
            "requestId": "req-" + msg_id,
            "message": {"id": msg_id, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 1000, "output_tokens": output}}}
    if agent_id:
        line.update(agentId=agent_id, isSidechain=True)
    return line


class TestContinuedSessionKeepsItsSubagents(unittest.TestCase):
    """A session continued into a new transcript has its whole main thread
    copied into the continuation, which then owns those requests as the
    superset. The subagents it ran are still only its own, so it is a real
    session, not a copy -- hiding it drops their cost from every per-session
    view while Total cost keeps it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        project = os.path.join(self.tmp.name, "-Users-x-proj6")
        subagents = os.path.join(project, "sess-early", "subagents")
        os.makedirs(subagents)
        early = [_request_line("sess-early", "2026-01-01T10:00:00Z", "msg-1"),
                 _request_line("sess-early", "2026-01-01T10:05:00Z", "msg-2")]
        later = early + [_request_line("sess-later", "2026-01-02T10:00:00Z", "msg-3"),
                         _request_line("sess-later", "2026-01-02T10:05:00Z", "msg-4")]
        sub = [_request_line("sess-early", "2026-01-01T10:02:00Z", "msg-sub", agent_id="a1")]
        for path, lines in ((os.path.join(project, "sess-early.jsonl"), early),
                            (os.path.join(project, "sess-later.jsonl"), later),
                            (os.path.join(subagents, "agent-a1.jsonl"), sub)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
        self.payload = scan(self.tmp.name, cache=FileCache(os.path.join(self.tmp.name, "c.json")))
        self.by_id = {s["id"]: s for s in self.payload["sessions"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_owning_subagent_requests_is_not_a_copy(self):
        self.assertEqual(self.by_id["sess-early"]["requests"], 1)  # the subagent's
        self.assertFalse(self.by_id["sess-early"]["derived"])
        self.assertEqual(self.payload["summary"]["total_sessions"], 2)

    def test_session_rows_add_up_to_total_cost(self):
        total = sum(r[13] for r in self.payload["usage"])
        rows = sum(s["est_cost_main"] + s["est_cost_sub"]
                   for s in self.payload["sessions"] if not s["derived"])
        self.assertGreater(total, 0)
        self.assertAlmostEqual(rows, total, places=6)


class TestCacheWriteAccounting(unittest.TestCase):
    """A record with only the flat cache_creation_input_tokens total is billed as
    a 5m write, so it has to show up in the token columns as well."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_flat_cache_creation_counted_as_5m_tokens(self):
        rows = [r for r in self.payload["usage"]
                if r[1] == "sess-span" and r[0].startswith("2026-02-01")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], 1000)  # cache_write_5m column

    def test_no_row_charges_cost_without_counting_tokens(self):
        for r in self.payload["usage"]:
            tokens = r[5] + r[6] + r[7] + r[8] + r[9]
            if r[13] > 0:
                self.assertGreater(tokens, 0, f"row bills ${r[13]} against zero tokens: {r}")


class TestAgentTotalsMatchOverview(unittest.TestCase):
    """The Agents tab and the Overview's subagent share read the same numbers."""

    def test_agent_cost_equals_deduped_subagent_usage(self):
        payload = _payload()
        agents = sum(a["cost"] for a in payload["agents"])
        usage = sum(r[13] for r in payload["usage"] if r[4] != -1)
        self.assertAlmostEqual(agents, usage, places=6)

    def test_agent_tokens_equal_deduped_subagent_usage(self):
        payload = _payload()
        agents = sum(a["tokens"] for a in payload["agents"])
        usage = sum(r[5] + r[6] + r[7] + r[8] + r[9]
                    for r in payload["usage"] if r[4] != -1)
        self.assertEqual(agents, usage)


class TestCacheSaving(unittest.TestCase):
    """Dollars saved by prompt caching, net of what the cache cost to write."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_every_usage_row_carries_a_saving(self):
        for r in self.payload["usage"]:
            self.assertEqual(len(r), 18, r)
            self.assertIsInstance(r[15], (int, float))
            self.assertIsInstance(r[16], int, "imputed output tokens")
            self.assertIsInstance(r[17], int, "responses without a thinking count")

    def test_saving_is_zero_without_cache_reads(self):
        # A row with no cache reads and no cache writes cannot have saved
        # anything; it must not inherit a number from somewhere else.
        for r in self.payload["usage"]:
            if r[7] == 0 and r[8] == 0 and r[9] == 0:
                self.assertEqual(r[15], 0, r)

    def test_write_premium_is_netted_off(self):
        from claude_usage.aggregate import _request_cache_saving
        # Writes only, no reads: the cache has cost money and saved nothing yet,
        # so the figure must be negative rather than zero.
        rec = {"model": "claude-opus-5-5", "ts": "2026-10-02T12:00:00Z", "cache_read_tokens": 0,
               "cache_write_5m": 1_000_000, "cache_write_1h": 0,
               "cache_write_fallback": 0}
        self.assertLess(_request_cache_saving(rec), 0)

    def test_reads_dominate_a_realistic_record(self):
        from claude_usage.aggregate import _request_cache_saving
        rec = {"model": "claude-opus-5-5", "ts": "2026-10-02T12:00:00Z", "cache_read_tokens": 10_000_000,
               "cache_write_5m": 1_000_000, "cache_write_1h": 0,
               "cache_write_fallback": 0}
        # 10M reads at (4.00 - 0.20) = $38.00, less 1M writes at (5.00 - 4.00) = $1.00
        self.assertAlmostEqual(_request_cache_saving(rec), 37.0, places=6)

    def test_unknown_model_saves_nothing_rather_than_raising(self):
        from claude_usage.aggregate import _request_cache_saving
        rec = {"model": "claude-not-a-model", "ts": "2026-10-02T12:00:00Z", "cache_read_tokens": 10_000_000,
               "cache_write_5m": 0, "cache_write_1h": 0, "cache_write_fallback": 0}
        self.assertEqual(_request_cache_saving(rec), 0.0)


class TestReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_cli_model_names_are_normalized(self):
        # The fixture reports "claude-opus-5-5[1m]"; it must line up with the
        # bare model name the transcripts record, not form a separate row.
        models = {r["model"] for r in self.payload["reconciliation"]}
        self.assertIn("claude-opus-5-5", models)
        self.assertNotIn("claude-opus-5-5[1m]", models)

    def test_reconciliation_carries_both_sides(self):
        row = next(r for r in self.payload["reconciliation"] if r["model"] == "claude-opus-5-5")
        self.assertAlmostEqual(row["cli_cost"], 0.5, places=6)
        self.assertGreater(row["est_cost"], 0)
        self.assertGreater(row["est_tokens"], 0)
        self.assertEqual(row["cli_tokens"], 1040)

    def test_session_exposes_per_model_rows(self):
        session = next(s for s in self.payload["sessions"] if s["id"] == "sess-span")
        self.assertEqual([r["model"] for r in session["cli_models"]], ["claude-opus-5-5"])


class TestParsedMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.span = next(s for s in cls.payload["sessions"] if s["id"] == "sess-span")

    def test_mode_records_feed_permission_modes(self):
        # `mode` is the common form; reading only `permission-mode` left most
        # sessions with an empty chart.
        self.assertEqual(self.span["permission_modes"], {"normal": 1})

    def test_continued_in_reads_the_real_field(self):
        self.assertEqual(self.span["continued_in"], "sess-next-abc")

    def test_continued_in_never_points_at_itself(self):
        for s in self.payload["sessions"]:
            self.assertNotEqual(s["continued_in"], s["id"])

    def test_fast_mode_requests_counted(self):
        self.assertEqual(self.payload["summary"]["fast_mode_requests"], 1)


if __name__ == "__main__":
    unittest.main()


class TestBucketGranularity(unittest.TestCase):
    """Usage/tool/skill rows carry a 5-minute bucket so the browser can roll up
    to any coarser grain. Day-level aggregation in the payload made sub-day
    charts impossible."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_payload_declares_its_base_grain(self):
        self.assertEqual(self.payload["base_bucket_minutes"], 5)

    def test_usage_buckets_are_minute_resolution(self):
        for r in self.payload["usage"]:
            self.assertRegex(r[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", r)

    def test_tool_buckets_are_minute_resolution(self):
        for r in self.payload["tools"]:
            self.assertRegex(r[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", r)

    def test_buckets_align_to_five_minute_slots(self):
        # Slots must align to the hour, or a coarser roll-up drifts from
        # wall-clock boundaries like 00:00 / 08:00 / 16:00.
        for r in self.payload["usage"]:
            self.assertEqual(int(r[0][14:16]) % 5, 0, r[0])

    def test_summary_dates_stay_calendar_days(self):
        for key in ("date_min", "date_max"):
            self.assertRegex(self.payload["summary"][key], r"^\d{4}-\d{2}-\d{2}$")

    def test_rebucketing_did_not_change_totals(self):
        # Finer buckets must only split rows, never add or drop usage.
        by_day = collections.defaultdict(float)
        for r in self.payload["usage"]:
            by_day[r[0][:10]] += r[13]
        self.assertAlmostEqual(sum(by_day.values()),
                               sum(r[13] for r in self.payload["usage"]), places=6)

    def test_two_calls_in_one_session_land_in_different_buckets(self):
        # sess-span's two tool calls are four days apart.
        buckets = {r[0] for r in self.payload["tools"] if r[1] == "sess-span"}
        self.assertEqual(len(buckets), 2, buckets)


def _activity_line(sid, ts, uuid, kind="user", msg_id=None):
    """One transcript record with a uuid. With a msg_id it is a request (an
    assistant line); without, a plain record such as a typed prompt."""
    if msg_id:
        line = _request_line(sid, ts, msg_id)
        line["uuid"] = uuid
        return line
    line = {"type": kind, "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj6",
            "message": {"role": "user", "content": "hi"}}
    if uuid:
        line["uuid"] = uuid
    return line


def _minute(n, hour=10):
    return "2026-01-01T{:02d}:{:02d}:00.000Z".format(hour, n)


class TestForkActiveTime(unittest.TestCase):
    """A fork's transcript starts with a byte-copy of the original's records, uuids
    included. Active time must not count that history a second time (BUG-10)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "-Users-x-proj6")
        os.makedirs(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, lines):
        with open(os.path.join(self.project, name + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def _scan(self):
        payload = scan(self.tmp.name, cache=FileCache(os.path.join(self.tmp.name, "c.json")))
        return payload, {s["id"]: s for s in payload["sessions"]}

    def _brief_scenario(self, orig_minutes, copied):
        """The original runs over 4 minutes; the fork copies the first `copied`
        of its records, then adds three of its own over another 4 minutes an
        hour later. Returns the payload's sessions by id."""
        orig = [_activity_line("sess-orig", _minute(m), "u-o%d" % i, msg_id="m-o%d" % i)
                for i, m in enumerate(orig_minutes)]
        fork = orig[:copied] + [_activity_line("sess-fork", _minute(m, 11), "u-f%d" % m,
                                               msg_id="m-f%d" % m) for m in (0, 2, 4)]
        self._write("sess-orig", orig)
        self._write("sess-fork", fork)
        return self._scan()[1]

    def test_summed_active_time_is_exact_when_the_fork_ranks_first(self):
        # The fork file holds 5 requests to the original's 3, so it is visited
        # first and keeps the copied records; the original keeps its third.
        by_id = self._brief_scenario((0, 2, 4), copied=2)
        self.assertFalse(by_id["sess-fork"]["derived"])  # it owns requests of its own
        self.assertEqual(by_id["sess-fork"]["active_ms"], 6 * 60_000)
        self.assertEqual(by_id["sess-orig"]["active_ms"], 2 * 60_000)  # o2 -> o4
        self.assertEqual(sum(s["active_ms"] for s in by_id.values()), 8 * 60_000)  # 10 before

    def test_summed_active_time_is_exact_when_the_original_ranks_first(self):
        # Six requests in the original (two at the last minute) against the
        # fork file's five: the original is visited first.
        by_id = self._brief_scenario((0, 1, 2, 3, 4, 4), copied=2)
        self.assertEqual(by_id["sess-orig"]["active_ms"], 4 * 60_000)
        self.assertEqual(by_id["sess-fork"]["active_ms"], 4 * 60_000)
        self.assertEqual(sum(s["active_ms"] for s in by_id.values()), 8 * 60_000)

    def test_nothing_is_lost_when_the_original_ranks_first(self):
        # The original holds more requests than the fork, so it is visited first
        # and keeps the shared prefix; the fork keeps only what it wrote itself.
        orig = [_activity_line("sess-orig", _minute(m), "u-o%d" % m, msg_id="m-o%d" % m)
                for m in (0, 2, 4, 6, 8)]
        fork = orig[:2] + [_activity_line("sess-fork", _minute(m, 11), "u-f%d" % m,
                                          msg_id="m-f%d" % m) for m in (0, 3)]
        self._write("sess-orig", orig)
        self._write("sess-fork", fork)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-orig"]["active_ms"], 8 * 60_000)
        self.assertEqual(by_id["sess-fork"]["active_ms"], 3 * 60_000)  # 13 minutes before

    def test_fork_active_start_is_its_first_non_copied_record(self):
        orig = [_activity_line("sess-orig", _minute(m), "u-o%d" % m, msg_id="m-o%d" % m)
                for m in (0, 2, 4, 6, 8)]
        fork = orig[:2] + [_activity_line("sess-fork", _minute(m, 11), "u-f%d" % m,
                                          msg_id="m-f%d" % m) for m in (0, 3)]
        self._write("sess-orig", orig)
        self._write("sess-fork", fork)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-fork"]["active_start"], _minute(0, 11))
        self.assertEqual(by_id["sess-orig"]["active_start"], _minute(0))
        # `start` stays the earliest record in the file: the team merge recognises a
        # continuation by it matching the original's (started_at, project).
        self.assertEqual(by_id["sess-fork"]["start"], _minute(0))

    def test_prompts_count_as_activity_not_only_requests(self):
        orig = [_activity_line("sess-orig", _minute(m), "u-o%d" % m, msg_id="m-o%d" % m)
                for m in (0, 2, 4, 6, 8)]
        fork = orig[:2] + [_activity_line("sess-fork", _minute(0, 11), "u-prompt"),
                           _activity_line("sess-fork", _minute(1, 11), "u-f1", msg_id="m-f1"),
                           _activity_line("sess-fork", _minute(3, 11), "u-f2", msg_id="m-f2")]
        self._write("sess-orig", orig)
        self._write("sess-fork", fork)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-fork"]["active_ms"], 3 * 60_000)  # 1 + 2 minutes

    def test_the_gap_into_a_forks_first_own_prompt_is_kept(self):
        # Typing a prompt after the last copied record is time the user spent: the
        # gap ends on a record the fork wrote, so the fork counts it.
        orig = [_activity_line("sess-orig", _minute(m), "u-o%d" % m, msg_id="m-o%d" % m)
                for m in (0, 2, 4, 6, 8)]
        fork = orig[:2] + [_activity_line("sess-fork", _minute(5), "u-prompt"),
                           _activity_line("sess-fork", _minute(6), "u-f", msg_id="m-f")]
        self._write("sess-orig", orig)
        self._write("sess-fork", fork)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-fork"]["active_ms"], (3 + 1) * 60_000)  # o2 -> prompt -> reply
        self.assertEqual(by_id["sess-fork"]["active_start"], _minute(5))

    def test_records_without_a_uuid_are_kept_in_their_own_file(self):
        lines = [_activity_line("sess-a", _minute(m), None) for m in (0, 2, 4)]
        lines[0].update(type="assistant")
        copy = [dict(l, sessionId="sess-b") for l in lines]
        for sid, ls in (("sess-a", lines), ("sess-b", copy)):
            self._write(sid, ls)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-a"]["active_ms"], 4 * 60_000)
        self.assertEqual(by_id["sess-b"]["active_ms"], 4 * 60_000)

    def test_a_uuid_repeated_inside_one_file_is_still_counted(self):
        # Only a record already kept from an earlier file is a copy.
        lines = [_activity_line("sess-a", _minute(m), "u-same") for m in (0, 2, 4)]
        self._write("sess-a", lines)
        _, by_id = self._scan()
        self.assertEqual(by_id["sess-a"]["active_ms"], 4 * 60_000)


class TestActiveTimeWithoutCopies(unittest.TestCase):
    """Moving the measurement into aggregate must not change a session that has no
    copies. These are the values the transcript fixtures gave before it moved."""

    def test_fixture_sessions_keep_their_active_time(self):
        by_id = {s["id"]: s for s in _payload()["sessions"]}
        self.assertEqual({k: v["active_ms"] for k, v in by_id.items()},
                         {"sess-fork-1": 3000, "sess-main-1": 11000,
                          "sess-main-2": 1000, "sess-span": 8000})
        for s in by_id.values():
            self.assertEqual(s["active_start"], s["start"])
