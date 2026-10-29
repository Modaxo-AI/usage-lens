"""Phase 4b fixes on the individual dashboard (I1-I5): stray MCP names judged over
all time, the counter span for "not in transcripts", one connector one bar, the
address written back after it is corrected, and the tool-name wording."""
import unittest

from tests.test_p3_dashboard import DASHBOARD, DAY, run, session, run_js, setup


def tools_payload(rows):
    return {"tools": [r for r in rows], "sessions": [session("s1")]}


class TestStrayNamesAllTime(unittest.TestCase):
    def test_i1_a_real_tool_with_one_failed_call_in_range_is_not_a_stray(self):
        # all-time it worked 50 times; in the range its only call failed
        tools = [["2026-09-01", "s1", "mcp__jira__getIssue", 50, 0, 0, "m"],
                 [DAY, "s1", "mcp__jira__getIssue", 1, 1, 0, "m"],
                 [DAY, "s1", "mcp__jira__other", 20, 0, 0, "m"]]
        out = run("out([...strayMcpTools()])", tools_payload(tools))
        self.assertEqual(out, [])

    def test_i1_a_name_that_never_worked_all_time_is_a_stray_even_with_no_success_in_range(self):
        tools = [["2026-09-01", "s1", "mcp__jira__getIssue", 50, 0, 0, "m"],
                 [DAY, "s1", "mcp__jira__getIsue", 12, 12, 0, "m"]]
        out = run("out([...strayMcpTools()])", tools_payload(tools))
        self.assertEqual(out, ["mcp__jira__getIsue"])

    def test_i1_applied_in_range_the_ranking_and_habits_keep_the_real_tool(self):
        tools = [["2026-09-01", "s1", "mcp__jira__getIssue", 50, 0, 0, "m"],
                 [DAY, "s1", "mcp__jira__getIssue", 12, 12, 0, "m"],
                 [DAY, "s1", "mcp__jira__other", 20, 0, 0, "m"]]
        out = run("renderTools(); out({labels: charts_made.filter(c => Array.isArray(c.data.datasets[0].backgroundColor))[0].data.labels,"
                  " note: el('toolErrorNote').textContent})", tools_payload(tools))
        self.assertIn("mcp__jira__getIssue", out["labels"])
        self.assertNotIn("none succeeded", out["note"])


class TestServerLabels(unittest.TestCase):
    def test_i3_one_connector_added_three_ways_is_one_server(self):
        out = run("out(['mcp__atlassian__a', 'mcp__claude_ai_Atlassian__b', 'mcp__claude_ai_Atlassian_2__c',"
                  "'mcp__plugin_atlassian_atlassian__d'].map(mcpServerOf))")
        self.assertEqual(set(out), {"atlassian"})

    def test_i3_the_rollup_has_one_bar_and_a_uuid_server_stays_apart(self):
        tools = [[DAY, "s1", n, 3, 0, 0, "m"] for n in
                 ["mcp__atlassian__a", "mcp__claude_ai_Atlassian__b", "mcp__claude_ai_Atlassian_2__c",
                  "mcp__2888b309-b804-4b3f-bab5-50f0dc687b18__x"]]
        out = run("renderTools(); out({s: charts['mcpServer'].data.labels, d: charts['mcpServer'].data.datasets[0].data,"
                  " note: el('mcpNote').textContent})", tools_payload(tools))
        self.assertEqual(out["s"], ["atlassian", "connector 2888b309"])
        self.assertEqual(out["d"][0], 9)
        self.assertIn("can't be matched", out["note"])

    def test_i3_strays_group_servers_by_the_same_label(self):
        tools = [[DAY, "s1", "mcp__claude_ai_Atlassian_2__good", 10, 0, 0, "m"],
                 [DAY, "s1", "mcp__atlassian__typo", 5, 5, 0, "m"]]
        out = run("out([...strayMcpTools()])", tools_payload(tools))
        self.assertEqual(out, ["mcp__atlassian__typo"])


class TestNotInTranscriptsSpan(unittest.TestCase):
    def _html(self, sessions):
        return run("renderGlobalStats(); out(el('statTiles').innerHTML)", {"sessions": sessions})

    def test_i2_a_shared_counter_whose_member_runs_past_the_range_is_not_counted(self):
        owner = session("a", est_cost_unlogged=0.5, cli_counter_shared_with=["b"])
        member = session("b", derived=True, requests=0, end_day="2026-10-23", end="2026-10-23T09:00:00")
        html = self._html([owner, member])
        self.assertNotIn("with a counter", html)

    def test_i2_a_counter_wholly_inside_the_range_still_counts(self):
        owner = session("a", est_cost_unlogged=0.5, cli_counter_shared_with=["b"])
        member = session("b", derived=True, requests=0)
        html = self._html([owner, member])
        self.assertIn("with a counter", html)


class TestAddressWrittenBack(unittest.TestCase):
    def test_i4_invalid_params_are_removed_from_the_address_at_load(self):
        snippet = ("state.from = ''; state.to = ''; state._minDate = ''; state._maxDate = '';"
                   "state.projects = []; state.models = [];"
                   "class MultiSelect { constructor(m, o) { this.o = o; this.v = []; } setValues(v) { this.v = v; }"
                   " getValues() { return this.v; } setOptions() {} }; projectMS = null; modelMS = null;"
                   "window.location = {search: '?from=nope&to=2099-01-01&grain=7&project=zzz&model=nope', pathname: '/', hash: ''};"
                   "let written = null; window.history = {replaceState(a, b, url){ written = url; }};"
                   "populateFilters(); out(written)")
        written = run(snippet, {"usage": [usage_row_for("s1")], "summary": {"date_max": DAY}})
        self.assertEqual(written, "/")

    def test_i4_a_valid_address_is_kept(self):
        snippet = ("state.from = ''; state.to = ''; state._minDate = ''; state._maxDate = '';"
                   "state.projects = []; state.models = [];"
                   "class MultiSelect { constructor(m, o) { this.o = o; this.v = []; } setValues(v) { this.v = v; }"
                   " getValues() { return this.v; } setOptions() {} }; projectMS = null; modelMS = null;"
                   "window.location = {search: '?from=2026-10-19&to=2026-10-20&grain=1440', pathname: '/', hash: ''};"
                   "let written = null; window.history = {replaceState(a, b, url){ written = url; }};"
                   "populateFilters(); out(written)")
        written = run(snippet, {"usage": [usage_row_for("s1", "2026-10-01"), usage_row_for("s1")], "summary": {"date_max": DAY}})
        self.assertIn("from=2026-10-19", written)
        self.assertIn("grain=1440", written)


class TestToolNameWording(unittest.TestCase):
    def test_i5_documented_wording_and_extra_real_tools(self):
        out = run("renderTools(); out({note: el('toolErrorNote').textContent,"
                  " extra: ['SearchSkills', 'DesignSync', 'PublishPlugin', 'ListSkills', 'ListPlugins'].map(n => BUILTIN_TOOLS.has(n)),"
                  " x: 1})",
                  tools_payload([[DAY, "s1", "bash", 17, 17, 0, "m"], [DAY, "s1", "Bash", 200, 20, 0, "m"]]))
        self.assertIn("isn't in Claude Code's documented tool list", out["note"])
        self.assertNotIn("built-in", out["note"])
        self.assertEqual(out["extra"], [True] * 5)

    def test_i5_a_real_undocumented_tool_that_always_fails_stays_in_the_ranking(self):
        out = run("renderTools(); out(charts_made.filter(c => Array.isArray(c.data.datasets[0].backgroundColor))[0].data.labels)",
                  tools_payload([[DAY, "s1", "SearchSkills", 15, 15, 0, "m"], [DAY, "s1", "Bash", 200, 20, 0, "m"]]))
        self.assertIn("SearchSkills", out)

    def test_i5_no_built_in_wording_is_left_in_the_page_scripts(self):
        import os
        from tests.jsrun import REPO
        for f in ("static/habits.js", "static/app.js"):
            with open(os.path.join(REPO, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("isn't a Claude Code built-in", src, f)
            self.assertNotIn("not a Claude Code built-in", src, f)


def usage_row_for(sid, day=DAY):
    from tests.test_p3_dashboard import usage_row
    return usage_row(sid, day=day)


if __name__ == "__main__":
    unittest.main()
