"""Tools are ranked by the errors they caused, not by error rate, on the Tools &
errors tab and in the team view, as on the Overview's Friction card; a
built-in tool name that never once succeeded (one the model made up) is left
out, while an MCP tool that never succeeded is a broken integration and stays.
And a transcript that never got a reply isn't counted as a session started."""
import copy
import json
import unittest

from claude_usage import export as export_mod
from tests.jsrun import run_js
from tests.test_export import PERSON, _as_dicts, _payload
from tests.test_health_ui import TEAM, _friction_html, _team

DASHBOARD = ["static/filters.js", "static/habits.js", "static/app.js"]


class TestToolsTab(unittest.TestCase):
    def _chart(self, tools):
        rows = [["2026-10-26T10:00", "s1", name, calls, errors, 0] for name, calls, errors in tools]
        payload = {"summary": {"date_max": "2026-10-29"}, "usage": [], "tools": rows,
                   "sessions": [], "lines": [], "prs": []}
        snippet = (f"PAYLOAD = {json.dumps(payload)};"
                   "sessionById = {}; state.from = '2026-10-25'; state.to = '2026-10-29';"
                   "renderTools();"
                   # Charts are made in page order: calls by tool, then errors.
                   "const c = charts_made.filter(c => c.data && c.data.labels && "
                   "  c.data.labels.includes('Bash'))[1];"
                   "out({labels: c.data.labels, data: c.data.datasets[0].data, "
                   "     note: el('toolErrorNote').textContent});")
        return run_js(DASHBOARD, snippet)

    def test_most_errors_first_not_highest_rate(self):
        res = self._chart([("Edit", 20, 8), ("Bash", 1000, 60), ("Read", 500, 0)])
        self.assertEqual(res["labels"][:2], ["Bash", "Edit"])
        self.assertEqual(res["data"][:2], [60, 8])

    def test_an_invented_name_is_left_out_and_named(self):
        res = self._chart([("bash", 17, 17), ("Bash", 1000, 60), ("mcp__down__get", 12, 12)])
        self.assertNotIn("bash", res["labels"])
        self.assertIn("mcp__down__get", res["labels"])
        self.assertIn("bash (17 calls, none succeeded)", res["note"])


class TestTeamFrictionRanking(unittest.TestCase):
    def _tool(self, tool, integration, calls, errors, person="p0"):
        return [person, "2026-02-02", tool, integration, calls, errors]

    def test_most_errors_first(self):
        html = _friction_html([self._tool("mcp__small__y", "small", 25, 20),
                               self._tool("mcp__big__x", "big", 400, 40)])
        self.assertLess(html.index("big (MCP)"), html.index("small (MCP)"))

    def test_an_mcp_tool_that_never_succeeded_stays(self):
        html = _friction_html([self._tool("mcp__down__z", "down", 25, 25)])
        self.assertIn("down (MCP)", html)

    def test_an_invented_built_in_name_is_left_out(self):
        snippet = (_team([], tools=[self._tool("bash", "built-in", 30, 30),
                                    self._tool("Bash", "built-in", 300, 30)])
                   + "renderFriction(); out(el('#frictionTable tbody').innerHTML + "
                     "el('#ccToolsTable tbody').innerHTML);")
        html = run_js(TEAM, snippet)
        self.assertIn(">Bash<", html)
        self.assertNotIn(">bash<", html)

    def test_findings_name_the_integration_with_most_errors_first(self):
        tools = [self._tool("mcp__small__y", "small", 25, 20), self._tool("mcp__big__x", "big", 400, 60)]
        snippet = (f"const fs = findings({{people: [{{person: 'p0'}}], tools: {json.dumps(tools)}}}, '2026-02-09');"
                   "out(fs.filter(f => f.rule === 'integration').map(f => f.text));")
        texts = run_js(["static/team-findings.js"], snippet)
        self.assertTrue(texts[0].startswith("big (MCP)"), texts)


class TestSessionsStarted(unittest.TestCase):
    def test_a_transcript_that_never_got_a_reply_is_listed_but_not_counted(self):
        payload = copy.deepcopy(_payload())
        real = [s for s in payload["sessions"] if not s["derived"]]
        def started():
            rows = _as_dicts(export_mod.SUMMARY_COLUMNS, export_mod.summary_rows(payload, PERSON))
            return sum(int(r["sessions_started"]) for r in rows)
        before = started()
        real[0]["requests"] = 0
        after = started()
        self.assertEqual(after, before - 1)
        sid = real[0].get("session_id") or real[0]["id"]
        self.assertTrue(any(sid in row for row in export_mod.session_rows(payload, PERSON)))


if __name__ == "__main__":
    unittest.main()
