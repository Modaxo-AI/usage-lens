"""Usage-health measures: the parser's new signals, the payload's health
section, de-duplication across fork/resume copies, and privacy."""
import json
import os
import shutil
import tempfile
import unittest

from claude_usage import health
from claude_usage.cache import FileCache
from claude_usage.parser import parse_file
from claude_usage.pricing import rates_for
from claude_usage.scan import scan

SENTINEL = "SECRET_HEALTH_XYZ"
MODEL = "claude-sonnet-5"


def _ts(minute, hour=10, day=1):
    return f"2026-02-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def _prompt(uuid, ts, origin="human", mode="auto", **extra):
    rec = {"type": "user", "uuid": uuid, "timestamp": ts, "permissionMode": mode,
           "message": {"role": "user", "content": f"{SENTINEL} do the thing"}, **extra}
    if origin is not None:
        rec["origin"] = {"kind": origin}
    return rec


def _response(mid, ts, *, inp=100, read=0, w5=0, w1=0, out=10, miss=None, content=None):
    msg = {"id": mid, "role": "assistant", "model": MODEL,
           "usage": {"input_tokens": inp, "output_tokens": out,
                     "cache_read_input_tokens": read,
                     "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                        "ephemeral_1h_input_tokens": w1}},
           "content": content or [{"type": "text", "text": SENTINEL}]}
    if miss:
        msg["diagnostics"] = {"cache_miss_reason": miss}
    return {"type": "assistant", "uuid": "a-" + mid, "timestamp": ts, "requestId": "r-" + mid,
            "message": msg}


def _tool_use(mid, ts, tuid, name, inp):
    return _response(mid, ts, content=[{"type": "tool_use", "id": tuid, "name": name, "input": inp}])


def _tool_result(uuid, ts, tuid, is_error=False, text=None):
    return {"type": "user", "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tuid, "is_error": is_error,
                 "content": text or f"{SENTINEL} output"}]}}


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def write_health_logs(root):
    """Two sessions and a fork copy exercising every health signal; used
    here and by tests/test_health_team.py."""
    proj = os.path.join(root, "-Users-x-health")
    big = 300_000
    original = [
        _prompt("p1", _ts(0)),
        _response("m1", _ts(1), inp=1000, read=0, w1=0),                       # small
        _response("m2", _ts(2), inp=100, read=big, w1=1000),                    # long
        _tool_use("m3", _ts(3), "tu-edit", "Edit", {"file_path": SENTINEL}),
        _tool_result("r1", _ts(4), "tu-edit"),
        _tool_use("m4", _ts(5), "tu-test", "Bash", {"command": "pytest"}),
        _tool_result("r2", _ts(6), "tu-test"),
        _prompt("p2", _ts(0, hour=14)),
        _response("m5", _ts(1, hour=14), inp=100, read=0, w1=big,
                  miss={"type": "previous_message_not_found",
                        "cache_missed_input_tokens": big}),
        {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "timestamp": _ts(2, hour=14),
         "compactMetadata": {"trigger": "auto", "preTokens": 950000}},
        {"type": "attachment", "uuid": "n1", "timestamp": _ts(3, hour=14), "attachment": {
            "type": "instructions", "files": [{"type": "Project", "path": SENTINEL,
                                               "content": SENTINEL}]}},
        {"type": "attachment", "uuid": "q1", "timestamp": _ts(4, hour=14),
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": SENTINEL}},
        {"type": "user", "uuid": "i1", "timestamp": _ts(5, hour=14),
         "message": {"content": f"[Request interrupted by user] {SENTINEL}"}},
    ]
    _write(os.path.join(proj, "sess-a.jsonl"), original)
    # A fork copy: the original's prefix, re-written under a new session.
    _write(os.path.join(proj, "sess-a-fork.jsonl"), original[:7])
    # A session that edits after its last check, in plan mode.
    _write(os.path.join(proj, "sess-b.jsonl"), [
        _prompt("b1", _ts(0, day=2), mode="plan"),
        _tool_use("b2", _ts(1, day=2), "tu-b-test", "Bash", {"command": "npm test"}),
        _tool_result("b3", _ts(2, day=2), "tu-b-test", is_error=True),
        _tool_use("b4", _ts(3, day=2), "tu-b-edit", "Write", {"file_path": SENTINEL}),
        _tool_result("b5", _ts(4, day=2), "tu-b-edit", is_error=True),
        # The first edit failed (it is no edit); this one went through.
        _tool_use("b6", _ts(5, day=2), "tu-b-edit2", "Write", {"file_path": SENTINEL}),
        _tool_result("b7", _ts(6, day=2), "tu-b-edit2"),
    ])


class TestClassifyCommand(unittest.TestCase):
    def test_categories(self):
        cases = {
            "pytest -q": ["test"],
            "python3 -m unittest discover -s tests": ["test"],
            "cd app && npm run test -- --watch=false": ["test"],
            "FOO=1 uv run pytest tests/": ["test"],
            "go test ./...": ["test"],
            "./gradlew test": ["test"],
            "npx eslint src": ["lint"],
            "cargo clippy": ["lint"],
            "npm run build": ["build"],
            "make": ["build"],
            "git -C repo commit -m x": ["commit"],
            "gh pr create --fill": ["pr"],
            "ruff check . && pytest && git commit -m wip": ["commit", "lint", "test"],
        }
        for command, expected in cases.items():
            self.assertEqual(health.classify_command(command), expected, command)

    def test_non_checks(self):
        for command in ("ls -la", "git status", "cat test_report.txt", "echo pytest",
                        "grep -r build .", "git log --grep=commit", "", None, 42):
            self.assertEqual(health.classify_command(command), [], command)


class TestParserSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _parse(self, records, kind="main"):
        path = os.path.join(self.tmp, "s.jsonl")
        _write(path, records)
        return parse_file(path, kind)

    def _events(self, facts, kind):
        return [e for e in facts.health_events if e[2] == kind]

    def test_only_human_prompts_count(self):
        facts = self._parse([
            _prompt("p1", _ts(0), mode="plan"),
            _prompt("p2", _ts(1), origin="peer"),
            _prompt("p3", _ts(2), origin="task-notification"),
            _prompt("p4", _ts(3), origin=None),          # before `origin` existed
            _prompt("p5", _ts(4), isMeta=True),
            _prompt("p6", _ts(5), isCompactSummary=True),
            _tool_result("t1", _ts(6), "tu-1"),
        ])
        prompts = self._events(facts, "prompt")
        self.assertEqual([e[0] for e in prompts], ["p1", "p4"])
        self.assertEqual(prompts[0][3], "plan")

    def test_subagent_prompts_are_not_human_prompts(self):
        facts = self._parse([_prompt("p1", _ts(0))], kind="sub")
        self.assertEqual(self._events(facts, "prompt"), [])

    def test_interrupt_marker_and_how_the_file_ends(self):
        interrupt = {"type": "user", "uuid": "i1", "timestamp": _ts(2),
                     "message": {"content": [{"type": "text",
                                              "text": "[Request interrupted by user]"}]}}
        facts = self._parse([_prompt("p1", _ts(0)), _response("m1", _ts(1)), interrupt])
        self.assertEqual(len(self._events(facts, "interrupt")), 1)
        self.assertTrue(facts.ended_on_interrupt)

        tool_interrupt = _tool_result("t1", _ts(3), "tu-1", is_error=True,
                                      text="[Request interrupted by user for tool use]")
        facts = self._parse([_prompt("p1", _ts(0)), tool_interrupt, _response("m2", _ts(4))])
        self.assertEqual(len(self._events(facts, "interrupt")), 1)
        self.assertFalse(facts.ended_on_interrupt)

    def test_queued_denied_compacted_away(self):
        facts = self._parse([
            {"type": "attachment", "uuid": "q1", "timestamp": _ts(0),
             "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": SENTINEL}},
            {"type": "attachment", "uuid": "q2", "timestamp": _ts(1),
             "attachment": {"type": "queued_command", "commandMode": "task-notification",
                            "prompt": SENTINEL}},
            {"type": "user", "uuid": "d1", "timestamp": _ts(2), "toolDenialKind": "user-rejected",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "no"}]}},
            {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "timestamp": _ts(3),
             "compactMetadata": {"trigger": "manual", "preTokens": 900000}},
            {"type": "system", "subtype": "compact_boundary", "uuid": "c2", "timestamp": _ts(4),
             "compactMetadata": {"preTokens": 960000}},
            {"type": "system", "subtype": "away_summary", "uuid": "w1", "timestamp": _ts(5)},
            {"type": "system", "subtype": "stop_hook_summary", "uuid": "h1", "timestamp": _ts(6),
             "hookCount": 2},
        ])
        self.assertEqual(len(self._events(facts, "queued")), 1)
        self.assertEqual([e[3] for e in self._events(facts, "denial")], ["user-rejected"])
        self.assertEqual([e[3] for e in self._events(facts, "compact")], ["manual", "auto"])
        self.assertEqual(len(self._events(facts, "away")), 1)
        self.assertEqual(facts.stop_hook_runs, 1)

    def test_instruction_types_only(self):
        att = {"type": "attachment", "uuid": "n1", "timestamp": _ts(0), "attachment": {
            "type": "instructions", "files": [
                {"type": "Project", "path": f"/x/{SENTINEL}/CLAUDE.md", "content": SENTINEL},
                {"type": "User", "path": "/home/CLAUDE.md", "content": SENTINEL}]}}
        self.assertEqual(self._parse([att]).instruction_types, ["Project", "User"])
        self.assertIsNone(self._parse([_prompt("p1", _ts(0))]).instruction_types)

    def test_cache_miss_label_survives_streaming_dedupe(self):
        # The label rides on an early line; the line kept is the one with the
        # most output tokens.
        facts = self._parse([
            _response("m1", _ts(0), out=1, w1=5000,
                      miss={"type": "previous_message_not_found", "cache_missed_input_tokens": 4000}),
            _response("m1", _ts(0), out=50, w1=5000),
        ])
        (rec,) = facts.requests
        self.assertEqual(rec["output_tokens"], 50)
        self.assertEqual(rec["cache_miss"], "previous_message_not_found")
        self.assertEqual(rec["cache_missed_tokens"], 4000)

    def test_bash_checks_and_error_streaks(self):
        facts = self._parse([
            _tool_use("m1", _ts(0), "tu-1", "Bash", {"command": f"pytest -k {SENTINEL}"}),
            _tool_use("m2", _ts(1), "tu-2", "Bash", {"command": "ls"}),
            _tool_result("r1", _ts(2), "tu-1", is_error=True),
            _tool_result("r1b", _ts(2), "tu-1", is_error=True),   # repeated line
            _tool_result("r2", _ts(3), "tu-2", is_error=True),
            _tool_result("r3", _ts(4), "tu-3", is_error=True),
            _tool_result("r4", _ts(5), "tu-4"),
            _tool_result("r5", _ts(6), "tu-5", is_error=True),
        ])
        self.assertEqual(facts.tool_checks, {"tu-1": ["test"]})
        self.assertEqual(facts.error_streaks, [["tu-1", _ts(2), 3]])


class TestHealthPayload(unittest.TestCase):
    """End to end through scan(): two sessions, one of them with a fork copy."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        write_health_logs(cls.root)
        cache = FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json"))
        cls.payload = scan(cls.root, cache=cache)
        cls.sessions = {s["id"]: s for s in cls.payload["sessions"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def test_long_context_share_and_peak(self):
        h = self.sessions["sess-a"]["health"]
        self.assertGreater(h["peak_context"], health.LONG_CONTEXT_TOKENS)
        self.assertEqual(h["long_context_requests"], 2)
        self.assertEqual(h["main_requests"], 5)
        self.assertAlmostEqual(h["long_context_share"],
                               h["long_context_cost"] / h["main_cost"], places=3)
        bands = {r[3] for r in self.payload["health"]["context"] if r[1] == "sess-a"}
        self.assertEqual(bands, {0, 2})

    def test_cold_cache_cost_is_priced_at_the_write_rate(self):
        rates = rates_for(MODEL, ts=_ts(1, hour=14))
        expected = 300_000 / 1e6 * (rates["cache_write_1h"] - rates["cache_read"])
        h = self.sessions["sess-a"]["health"]
        self.assertAlmostEqual(h["cold_cache_cost"], expected, places=5)
        (row,) = self.payload["health"]["cache_misses"]
        self.assertEqual(row[3], health.EXPIRED_MISS)
        self.assertEqual(row[6], 300_000)

    def test_fork_copy_counts_nothing_twice(self):
        h = self.payload["health"]
        prompts = sum(r[4] for r in h["events"] if r[2] == "prompt" and r[1].startswith("sess-a"))
        self.assertEqual(prompts, 2)
        tests_run = sum(r[3] for r in h["checks"] if r[2] == "test" and r[1].startswith("sess-a"))
        self.assertEqual(tests_run, 1)
        # Every record the copy repeats is credited to the original.
        self.assertFalse(any(r[1] == "sess-a-fork" for r in h["events"] + h["checks"]))

    def test_session_flags(self):
        a = self.sessions["sess-a"]["health"]
        self.assertEqual((a["prompts"], a["interrupts"], a["queued_prompts"]), (2, 1, 1))
        self.assertEqual((a["compactions_auto"], a["compactions_manual"]), (1, 0))
        self.assertTrue(a["ended_on_interrupt"])
        self.assertEqual(a["instructions"], ["Project"])
        self.assertTrue(a["checked_after_last_edit"])
        self.assertFalse(a["plan_mode"])

        b = self.sessions["sess-b"]["health"]
        self.assertFalse(b["checked_after_last_edit"])   # edited after the test run
        self.assertTrue(b["plan_mode"])
        self.assertIsNone(b["instructions"])             # not recorded, not "none"
        self.assertEqual(b["max_error_streak"], 2)
        self.assertFalse(b["ended_on_interrupt"])

    def test_measures_missing_from_old_versions_are_not_recorded(self):
        # The fixtures carry no version, as in logs older than these records.
        self.assertEqual(self.sessions["sess-a"]["health"]["recorded"],
                         {"denials": False, "compactions": False})
        self.assertTrue(health._recorded({"2.1.300": 4}, "2.1.246"))
        self.assertTrue(health._recorded({"2.1.99": 1, "2.1.246": 1}, "2.1.246"))
        self.assertFalse(health._recorded({"2.1.245": 9, "2.0.999": 1}, "2.1.246"))

    def test_benchmarks_and_thresholds_are_sourced_in_the_payload(self):
        h = self.payload["health"]
        self.assertEqual(h["benchmarks"]["cost_per_active_day"]["average"], 13.0)
        self.assertEqual(h["thresholds"]["flag_share"], 0.10)
        self.assertEqual(h["bucket_minutes"], 60)
        self.assertTrue(all(r[0].endswith(":00") for r in h["events"]))

    def test_no_content_reaches_the_payload(self):
        # The sentinel sits in prompts, a queued prompt, an interrupt's tail,
        # tool inputs (a Bash command, edited paths), tool results and
        # instruction-file paths and contents.
        self.assertNotIn(SENTINEL, json.dumps(self.payload))


if __name__ == "__main__":
    unittest.main()
