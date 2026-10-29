"""Usage health in the export (schema 7) and the team view: the columns add up
to the payload, the tools file feeds friction by integration, and the team
view gets each person's habit rows."""
import datetime
import csv
import io
import json
import os
import tempfile
import unittest
import zipfile

from claude_usage import export as export_mod
from claude_usage import team
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.test_health import SENTINEL, write_health_logs
from tests.test_team import TeamFolder

PERSON = ("someone@example.com", "Some One")


def _health_payload():
    root = tempfile.mkdtemp()
    write_health_logs(root)
    return scan(root, cache=FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json")),
                tz=datetime.timezone.utc)  # exported in the default (UTC) export zone


def _dicts(columns, rows):
    return [dict(zip(columns, r)) for r in rows]


def _total(rows, column):
    return sum(float(r[column] or 0) for r in rows)


class TestHealthExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _health_payload()
        cls.summary = _dicts(export_mod.SUMMARY_COLUMNS, export_mod.summary_rows(cls.payload, PERSON))
        cls.sessions = {r["session_id"]: r for r in
                        _dicts(export_mod.SESSION_COLUMNS, export_mod.session_rows(cls.payload, PERSON))}

    def test_summary_totals_match_the_payload(self):
        h = self.payload["health"]
        self.assertEqual(_total(self.summary, "prompts"),
                         sum(r[4] for r in h["events"] if r[2] == "prompt"))
        self.assertEqual(_total(self.summary, "interrupts"), 1)
        self.assertEqual(_total(self.summary, "auto_compactions"), 1)
        self.assertEqual(_total(self.summary, "test_runs"), 2)       # one per session
        self.assertAlmostEqual(_total(self.summary, "cold_cache_usd"),
                               sum(r[5] for r in h["cache_misses"]), places=6)
        per_session_long = sum(s["health"]["long_context_cost"] for s in self.payload["sessions"])
        self.assertAlmostEqual(_total(self.summary, "long_context_cost_usd"), per_session_long, places=6)
        # Session-level counts: sess-a checked after its edit, sess-b didn't.
        self.assertEqual(_total(self.summary, "sessions_edited"), 2)
        self.assertEqual(_total(self.summary, "sessions_checked_after_edit"), 1)
        self.assertEqual(_total(self.summary, "sessions_plan_mode"), 1)
        self.assertEqual(_total(self.summary, "sessions_instructions_recorded"), 1)
        self.assertEqual(_total(self.summary, "sessions_with_project_claude_md"), 1)

    def test_session_columns_are_blank_when_not_recorded(self):
        a, b = self.sessions["sess-a"], self.sessions["sess-b"]
        self.assertEqual((a["checked_after_last_edit"], a["project_claude_md"], a["ended_on_interrupt"]),
                         ("yes", "yes", "yes"))
        self.assertEqual((b["checked_after_last_edit"], b["plan_mode"]), ("no", "yes"))
        self.assertIsNone(b["project_claude_md"])   # not recorded, not "no"
        # The fixtures carry no Claude Code version, so refusals and compactions
        # can't be told apart from "never happened".
        self.assertIsNone(a["tool_refusals"])
        self.assertIsNone(a["auto_compactions"])
        self.assertGreater(a["peak_context_tokens"], 200_000)

    def test_tools_file(self):
        rows = _dicts(export_mod.TOOL_COLUMNS, export_mod.tool_rows(self.payload, PERSON))
        self.assertEqual(_total(rows, "calls"), sum(r[3] for r in self.payload["tools"]))
        self.assertEqual(_total(rows, "errors"), sum(r[4] for r in self.payload["tools"]))
        self.assertEqual({r["integration"] for r in rows}, {"built-in"})
        self.assertEqual(export_mod.tool_integration("mcp__tracker__getIssue"), "tracker")
        self.assertEqual(export_mod.tool_integration("mcp__claude-in-chrome__navigate"), "claude-in-chrome")
        self.assertEqual(export_mod.tool_integration("Bash"), "built-in")
        self.assertEqual(export_mod.tool_integration("mcp__"), "built-in")

    def test_no_content_reaches_any_export_file(self):
        data = export_mod.zip_bytes(self.payload, PERSON)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(archive.namelist()), len(export_mod.KINDS))
            for name in archive.namelist():
                self.assertNotIn(SENTINEL, archive.read(name).decode("utf-8"), name)

    def test_every_health_column_is_summed_by_the_merge(self):
        # A summary column the merge doesn't sum is taken from one export only
        # when two exports split a week -- silently halving it.
        summed = set(team._SUMMED["summary"])
        self.assertEqual([c for c in export_mod.HEALTH_SUMMARY_COLUMNS + ["very_long_context_cost_usd"]
                          if c not in summed], [])

    def test_very_long_context_column_is_last_and_sums_to_sessions(self):
        from claude_usage import context as context_mod
        self.assertGreaterEqual(context_mod.EXPORT_SCHEMA_VERSION, 10)
        # Schema 11 appended week_active_days after it on the summary sheet.
        self.assertEqual(export_mod.SUMMARY_COLUMNS[-2], "very_long_context_cost_usd")
        self.assertEqual(export_mod.SESSION_COLUMNS[-1], "very_long_context_cost_usd")
        per_session = sum(s["health"]["very_long_context_cost"] for s in self.payload["sessions"]
                          if s.get("health"))
        self.assertAlmostEqual(_total(self.summary, "very_long_context_cost_usd"), per_session, places=6)

    def test_very_long_context_counts_a_600k_request(self):
        import tempfile, os
        from claude_usage.cache import FileCache
        from claude_usage.scan import scan
        from tests.test_health import _prompt, _response, _ts, _write
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cache_dir:
            _write(os.path.join(root, "-Users-x-big", "s-big.jsonl"),
                   [_prompt("p1", _ts(0)), _response("m1", _ts(1), inp=100, read=600_000)])
            payload = scan(root, cache=FileCache(os.path.join(cache_dir, "c.json")))
        rows = export_mod.summary_rows(payload, ("x@y.z", "X"))
        col = export_mod.SUMMARY_COLUMNS.index("very_long_context_cost_usd")
        main = export_mod.SUMMARY_COLUMNS.index("est_cost_main_usd")
        busy = [r for r in rows if r[3]]
        self.assertEqual(len(busy), 1)
        self.assertGreater(busy[0][col], 0)
        self.assertAlmostEqual(busy[0][col], busy[0][main], places=6)


class TestTeamHealth(unittest.TestCase):
    def setUp(self):
        self.payload = _health_payload()
        self.folder = TeamFolder()
        self.addCleanup(self.folder.close)

    def _people(self, n, stamp="20260210"):
        for i in range(n):
            email = f"p{i}@example.com"
            self.folder.zip(self.payload, email, email, stamp, machine=f"m{i}")

    def test_rows_reach_the_team_payload(self):
        self._people(5)
        payload = team.team_payload([self.folder.dir])
        self.assertNotIn("min_group", payload["health"])   # per person: no minimum group
        people = {r["person"] for r in payload["health"]["rows"]}
        self.assertEqual(len(people), 5)
        prompts = sum(r["prompts"] for r in payload["health"]["rows"])
        one = sum(r[4] for r in self.payload["health"]["events"] if r[2] == "prompt")
        self.assertEqual(prompts, 5 * one)
        tools = {r[0] for r in payload["tools"]}
        self.assertEqual(len(tools), 5)

    def test_exports_without_health_columns_are_not_counted_as_zero(self):
        self._people(4)
        # A fifth person on an older tool: same file, health columns blank.
        path = self.folder.zip(self.payload, "old@example.com", "old@example.com", "20260210", machine="old")
        with zipfile.ZipFile(path) as archive:
            files = {n: archive.read(n).decode("utf-8") for n in archive.namelist()}
        with zipfile.ZipFile(path, "w") as archive:
            for name, text in files.items():
                if "-summary-" in name:
                    rows = list(csv.reader(io.StringIO(text)))
                    blank = [i for i, c in enumerate(rows[0]) if c in export_mod.HEALTH_SUMMARY_COLUMNS]
                    for row in rows[1:]:
                        for i in blank:
                            row[i] = ""
                    buf = io.StringIO()
                    csv.writer(buf).writerows(rows)
                    text = buf.getvalue()
                archive.writestr(name, text)
        payload = team.team_payload([self.folder.dir])
        people = {r["person"] for r in payload["health"]["rows"]}
        self.assertEqual(len(people), 4)   # the older export has no habit data
        self.assertNotIn("old@example.com", people)

    def test_merged_files_include_tools(self):
        self._people(2)
        merged = team._load([self.folder.dir])
        files = team.merged_files(merged)
        rows = list(csv.DictReader(io.StringIO(files["team-tools.csv"])))
        self.assertEqual({r["person"] for r in rows}, {"p0@example.com", "p1@example.com"})
        self.assertTrue(all(r["integration"] for r in rows))
        summary = list(csv.DictReader(io.StringIO(files["team-summary.csv"])))
        self.assertIn("long_context_cost_usd", summary[0])
        self.assertNotIn(SENTINEL, json.dumps(files))


if __name__ == "__main__":
    unittest.main()
