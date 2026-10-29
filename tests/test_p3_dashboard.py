"""Phase 3 fixes on the individual dashboard (static/app.js, habits.js, index.html).

Each class names the review finding it covers (SA-, OE-, HB-, TS-, TM-, EX-, RC-).
The pages' scripts run under node with a stub DOM (tests/jsrun.py)."""
import json
import os
import re
import unittest

from tests.jsrun import REPO, run_js

DASHBOARD = ["static/filters.js", "static/habits.js", "static/app.js"]
DAY = "2026-10-20"


def usage_row(sid, model="m", day=DAY, cost=1.0, tokens=100, skill="(none)", agent=-1,
              requests=1, imputed=0, think=0, think_unlogged=0, out=0):
    row = [f"{day}T10:00", sid, model, skill, agent] + [0] * 13
    row[5] = tokens
    row[6] = out
    row[10] = think
    row[12], row[13] = requests, cost
    row[16], row[17] = imputed, think_unlogged
    return row


def session(sid, project="p", day=DAY, **kw):
    s = {"id": sid, "project": project, "models": ["m"], "start": f"{day}T10:00:00",
         "end": f"{day}T11:00:00", "start_day": day, "end_day": day, "derived": False,
         "title": "t " + sid, "requests": 5, "active_ms": 60000, "n_subagents": 0,
         "est_cost_main": 1, "est_cost_sub": 0, "tool_calls": 0, "tool_errors": 0,
         "entrypoints": {}, "versions": {}, "branches": {}, "permission_modes": {}}
    s.update(kw)
    return s


def setup(payload=None, frm=DAY, to=DAY, **extra):
    base = {"summary": {"date_max": to}, "usage": [], "tools": [], "skillCalls": [], "agents": [],
            "sessions": [], "lines": [], "prs": [],
            "dims": {"projects": ["p"], "models": ["m"]}}
    base.update(payload or {})
    base.update(extra)
    return (f"PAYLOAD = {json.dumps(base)};"
            "sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));"
            "indexAgentProjects();"
            f"state.from = '{frm}'; state.to = '{to}'; state._minDate = '2026-01-01'; state._maxDate = '{to}';")


def run(snippet, payload=None, frm=DAY, to=DAY, **extra):
    return run_js(DASHBOARD, setup(payload, frm, to, **extra) + snippet)


# ---------------------------------------------------------------- SA ------- #

class TestAgentsTab(unittest.TestCase):
    def _tiles(self, rows, **kw):
        return run("renderAgents(); out(el('agentStatTiles').innerHTML)",
                   {"usage": rows, "sessions": [session("s1")]}, **kw)

    def test_sa1_imputed_output_is_named_in_captions_and_footnote(self):
        rows = [usage_row("s1", agent="Explore", tokens=1000, imputed=400, out=600),
                usage_row("s1", agent=-1, tokens=500, out=100)]
        html = self._tiles(rows)
        self.assertIn("incl. 400 imputed", html)
        note = run("renderAgents(); out(el('agentsFootnote').textContent)",
                   {"usage": rows, "sessions": [session("s1")]})
        self.assertIn("400 output tokens imputed", note)

    def test_sa2_thinking_share_says_logged_thinking(self):
        html = self._tiles([usage_row("s1", agent="Explore", think=10)])
        self.assertIn("of logged thinking", html)
        self.assertNotIn("of thinking tokens", html)
        self.assertIn("floor", html)          # THINKING_NOTE as the tooltip

    def test_sa4_table_says_it_is_capped_and_order_is_stable(self):
        agents = [{"session_id": "s1", "agent_id": f"a{i:04d}", "date": DAY, "agent_type": "Explore",
                   "spawn_depth": 1, "models": ["m"], "cost": 0, "tokens": 0, "description": ""}
                  for i in range(600)]
        out = run("renderAgents(); const h = el('#agentsTable tbody').innerHTML;"
                  "out({note: el('agentsTableNote').textContent, rows: (h.match(/<tr>/g) || []).length,"
                  "     first: h.indexOf('a0000') })",
                  {"agents": agents, "sessions": [session("s1")]})
        self.assertIn("newest 500 of 600", out["note"])
        self.assertEqual(out["rows"], 500)

    def test_sa4_no_note_when_everything_fits(self):
        agents = [{"session_id": "s1", "agent_id": "a", "date": DAY, "agent_type": "Explore",
                   "spawn_depth": 1, "models": ["m"], "cost": 0, "tokens": 0, "description": ""}]
        note = run("renderAgents(); out(el('agentsTableNote').textContent)",
                   {"agents": agents, "sessions": [session("s1")]})
        self.assertNotIn("Showing the newest", note)
        self.assertIn("whole run", note)       # SA-7: what the table's tokens and cost are

    def test_sa7_footnote_explains_start_day_counting(self):
        note = run("renderAgents(); out(el('agentsFootnote').textContent)", {"sessions": [session("s1")]})
        self.assertIn("day they started", note)


class TestSkillsTab(unittest.TestCase):
    def _payload(self):
        rows = [usage_row("s1", skill="alpha", cost=3.0, tokens=300),
                usage_row("s1", skill="beta", cost=1.0, tokens=100, imputed=50, out=60)]
        return {"usage": rows, "sessions": [session("s1")]}

    def test_sa3_slash_command_invocations_are_counted_and_split(self):
        calls = [[DAY, "s1", "alpha", 2, "m", "tool"], [DAY, "s1", "alpha", 3, "m", "command"]]
        out = run("renderSkills(); out({tiles: el('skillStatTiles').innerHTML,"
                  " rows: el('#skillsTable tbody').innerHTML})",
                  dict(self._payload(), skillCalls=calls))
        self.assertIn("5 invocations", out["tiles"])
        self.assertIn("3 by slash command", out["tiles"])
        self.assertIn("2 by Claude through the Skill tool, 3 by slash command", out["rows"])

    def test_skill_call_columns_may_come_in_either_order(self):
        out = run("out([skillCallParts([1,2,3,4,'command','claude-x']), skillCallParts([1,2,3,4,'claude-x','tool']),"
                  "      skillCallParts([1,2,3,4])])")
        self.assertEqual(out[0], {"model": "claude-x", "via": "command"})
        self.assertEqual(out[1], {"model": "claude-x", "via": "tool"})
        self.assertEqual(out[2], {"model": None, "via": "tool"})

    def test_ts2_model_filter_keeps_only_the_calling_models_skill_calls(self):
        calls = [[DAY, "s1", "alpha", 2, "haiku", "tool"], [DAY, "s1", "alpha", 5, "opus", "tool"]]
        n = run("state.models = ['opus']; out(skillCallRows().map(r => r[3]))",
                dict(self._payload(), skillCalls=calls, sessions=[session("s1", models=["haiku", "opus"])]))
        self.assertEqual(n, [5])

    def test_sa1_skills_footnote_names_imputed_output(self):
        note = run("renderSkills(); out(el('skillsFootnote').textContent)", self._payload())
        self.assertIn("50 imputed output tokens", note)

    def test_sa8_skills_used_matches_table_rows_and_sessions_count_invocations(self):
        payload = self._payload()
        payload["skillCalls"] = [[DAY, "s2", "gamma", 1, "m", "tool"]]      # invoked, no tagged requests
        payload["sessions"].append(session("s2"))
        out = run("renderSkills(); out({tiles: el('skillStatTiles').innerHTML,"
                  " rows: el('#skillsTable tbody').innerHTML})", payload)
        self.assertIn('<div class="value">3</div>', out["tiles"])            # alpha, beta, gamma
        self.assertIn("2 with requests", out["tiles"])
        gamma_row = out["rows"].split("gamma")[1].split("</tr>")[0]
        self.assertTrue(gamma_row.rstrip().endswith('<td class="num">1</td>'), gamma_row)   # Sessions: 1, not 0

    def test_sa9_tiles_and_heatmap_follow_the_metric(self):
        out = run("state.skillsMetric = 'tokens'; renderSkills();"
                  "const t = el('skillStatTiles').innerHTML, h = el('skillHeatmapTitle').textContent;"
                  "state.skillsMetric = 'cost'; renderSkills();"
                  "out({t, h, t2: el('skillStatTiles').innerHTML, h2: el('skillHeatmapTitle').textContent})",
                  self._payload())
        self.assertIn("Tokens inside a skill", out["t"])
        self.assertIn("(tokens)", out["h"])
        self.assertIn("Cost inside a skill", out["t2"])
        self.assertIn("(cost)", out["h2"])

    def test_sa6_heatmap_has_an_other_column_for_the_projects_beyond_seven(self):
        rows = [usage_row("s%d" % i, skill="alpha", cost=10 - i) for i in range(9)]
        sessions = [session("s%d" % i, project="/a/p%d" % i) for i in range(9)]
        html = run("renderSkills(); out(el('skillHeatmap').innerHTML)", {"usage": rows, "sessions": sessions})
        self.assertIn(">Other<", html)
        # every project's spend is somewhere in the grid: 9 projects, 7 columns + Other
        self.assertEqual(html.count("alpha ×"), 8)

    def test_sa6_no_other_column_when_every_project_fits(self):
        rows = [usage_row("s1", skill="alpha")]
        html = run("renderSkills(); out(el('skillHeatmap').innerHTML)", {"usage": rows, "sessions": [session("s1")]})
        self.assertNotIn(">Other<", html)

    def test_sa9_empty_range_has_an_empty_state_and_no_lone_other(self):
        out = run("renderSkills(); out({hm: el('skillHeatmap').innerHTML, legend: el('skillWeeklyLegend').innerHTML})",
                  {"sessions": [session("s1")]})
        self.assertIn("Nothing in this range", out["hm"])
        self.assertNotIn("Other", out["legend"])

    def test_sa9_weekly_legend_gives_each_skill_its_own_colour(self):
        rows = [usage_row("s1", skill=f"sk{i}", cost=20 - i) for i in range(10)]
        colours = run("renderSkills(); out(charts_made.find(c => c.data.datasets.length > 1 && "
                      "c.data.datasets[0].stack === 's').data.datasets.map(d => d.backgroundColor))",
                      {"usage": rows, "sessions": [session("s1")]})
        # cssVar is empty under the stub, so compare what was asked for instead
        self.assertEqual(len(colours), 8)    # top 7 + Other


# ---------------------------------------------------------------- OE ------- #

class TestOutputAndEnv(unittest.TestCase):
    def test_oe3_and_oe8_env_counts_ignore_fork_toggle_and_no_reply_transcripts(self):
        sessions = [session("a", entrypoints={"cli": 1}, versions={"1.0.0": 1}),
                    session("b", requests=0, entrypoints={"cli": 1}),               # never got a reply
                    session("c", derived=True, requests=0, entrypoints={"desktop": 1})]
        snippet = ("state.showForks = false; renderEnv(); const a = charts['entrypoint'].data.datasets[0].data;"
                   "state.showForks = true; delete charts['entrypoint']; renderEnv();"
                   "out([a, charts['entrypoint'].data.datasets[0].data])")
        a, b = run(snippet, {"sessions": sessions})
        self.assertEqual(a, [1])
        self.assertEqual(b, [1])

    def test_oe3_global_tiles_are_not_moved_by_the_fork_toggle(self):
        owner = session("a", est_cost_unlogged=0.5)
        copy = session("c", derived=True, requests=0, est_cost_unlogged=0.25)
        out = run("state.showForks = false; renderGlobalStats(); const a = el('statTiles').innerHTML;"
                  "state.showForks = true; renderGlobalStats(); out([a, el('statTiles').innerHTML])",
                  {"sessions": [owner, copy]})
        self.assertEqual(out[0], out[1])

    def test_oe4_model_filter_leaves_lines_and_prs_unsplit_and_says_so(self):
        sessions = [session("a", models=["haiku"]), session("b", models=["opus"])]
        lines = [[f"{DAY}T10:00", "a", 10, 1], [f"{DAY}T10:00", "b", 90, 9]]
        prs = [{"session_id": "a", "date": DAY, "number": 1, "url": "https://x/1"},
               {"session_id": "b", "date": DAY, "number": 2, "url": "https://x/2"}]
        html = run("state.models = ['haiku']; renderEnv(); out(el('envStatTiles').innerHTML)",
                   {"sessions": sessions, "lines": lines, "prs": prs, "dims": {"projects": ["p"], "models": ["haiku", "opus"]}})
        self.assertIn("+100", html)
        self.assertIn("-10", html)
        self.assertIn("not recorded per model", html)
        self.assertIn('<div class="value">2</div>', html)

    def test_oe2_lines_caption_has_no_session_count_framing(self):
        html = run("renderEnv(); out(el('envStatTiles').innerHTML)",
                   {"sessions": [session("a")], "lines": [[f"{DAY}T10:00", "a", 3, 1]]})
        self.assertIn("from the edits Claude made in these dates", html)
        self.assertNotRegex(html, r"from \d+ of \d+ sessions")

    def test_oe5_version_chart_is_continuous_clipped_and_lists_only_plotted_versions(self):
        sessions = [session("a", day="2026-10-05", versions={"2.1.1": 5}),
                    session("b", day="2026-10-05", versions={"2.1.1": 2, "2.0.0": 9}),   # dominant 2.0.0
                    session("c", day="2026-10-19", versions={"2.1.1": 1})]
        sessions[0]["start_day"] = "2026-09-01"        # began before the range, ends inside it
        sessions[0]["end_day"] = "2026-10-20"
        out = run("renderEnv(); const c = charts['versions']; out({labels: c.data.labels,"
                  " series: c.data.datasets.map(d => [d.label, d.data])})",
                  {"sessions": sessions}, frm="2026-10-05", to="2026-10-25")
        self.assertEqual(out["labels"], ["2026-10-05", "2026-10-12", "2026-10-19"])   # a week with no session included
        by = dict((l, d) for l, d in out["series"])
        self.assertEqual(sorted(by), ["2.0.0", "2.1.1"])         # no zero legend series, no "Other"
        self.assertEqual(by["2.1.1"], [1, 0, 1])                  # a clamped into the first shown week

    def test_oe9_bucket_count_matches_the_axis_and_is_singular(self):
        out = run("const r = {}; for (const g of [10080, 'month', 1440, 480]) r[g] = [bucketCountFor(g), axisKeys(g).length];"
                  "state.from = state.to = '2026-10-20'; r.one = bucketCountFor(1440);"
                  "renderGrainControl(); r.summary = el('grainSummary').innerHTML; out(r)",
                  frm="2026-10-04", to="2026-10-12")      # Sunday to Monday: three calendar weeks
        for g in ("10080", "month", "1440", "480"):
            self.assertEqual(out[g][0], out[g][1], g)
        self.assertEqual(out["10080"][0], 3)
        self.assertEqual(out["one"], 1)
        self.assertIn("bar", out["summary"])

    def test_oe9_one_bar_is_not_plural(self):
        html = run("state.grain = 1440; renderGrainControl(); out(el('grainSummary').innerHTML)")
        self.assertIn("1 bar<", html + "<")
        self.assertNotIn("1 bars", html)

    def test_oe10_default_view_and_all_show_no_date_chip(self):
        out = run("state._minDate = '2026-01-01'; state._maxDate = '2026-10-25';"
                  "const d = defaultRange(); state.from = d.from; state.to = d.to;"
                  "renderFilterSummary(); const a = el('activeChips').innerHTML;"
                  "state.from = '2026-01-01'; state.to = '2026-10-25'; renderFilterSummary(); const b = el('activeChips').innerHTML;"
                  "state.from = '2026-10-01'; renderFilterSummary();"
                  "out({a, b, c: el('activeChips').innerHTML, d})")
        self.assertEqual(out["d"], {"from": "2026-10-21", "to": "2026-10-25"})
        self.assertNotIn("Date range", out["a"])
        self.assertNotIn("Date range", out["b"])
        self.assertIn("Date range", out["c"])

    def test_oe11_standalone_build_has_no_refresh_button(self):
        out = run_js(DASHBOARD, setup({"sessions": [session("a")], "summary": {"total_sessions": 1, "date_max": DAY},
                                       "generated_at": "2026-10-20T10:00:00Z", "files_parsed": 0, "files_cached": 0,
                                       "scan_ms": 1})
                     + "SERVED_BY_DASHBOARD = false; renderHeaderMeta(); out(el('refreshBtn').hidden)")
        self.assertTrue(out)
        served = run_js(DASHBOARD, setup({"sessions": [session("a")], "summary": {"total_sessions": 1, "date_max": DAY},
                                          "generated_at": "2026-10-20T10:00:00Z", "files_parsed": 0, "files_cached": 0,
                                          "scan_ms": 1}) + "renderHeaderMeta(); out(el('refreshBtn').hidden)")
        self.assertFalse(served)

    def test_oe12_charts_are_rebuilt_when_the_colour_scheme_changes(self):
        out = run("renderEnv(); const before = Object.keys(charts).length; let destroyed = 0;"
                  "Object.values(charts).forEach(c => { c.destroy = () => { destroyed++; }; });"
                  "rethemeCharts(); out({before, destroyed, after: Object.keys(charts).length})",
                  {"sessions": [session("a", entrypoints={"cli": 1})]})
        self.assertGreater(out["before"], 0)
        self.assertEqual(out["destroyed"], out["before"])

    def test_oe13_formatters_at_their_boundaries(self):
        out = run("out({a: fmtTokens(999999999), b: fmtTokens(999995000), c: fmtTokens(1.5e9),"
                  "     d: fmtDuration(59600), e: fmtDuration(59.6 * 60000), f: fmtDuration(0), g: fmtDuration(null),"
                  "     h: cleanBranchName('claude/repo-overview-d339f9'), i: cleanBranchName('release-abc123'),"
                  "     j: cleanBranchName('fix/deadbe') })")
        self.assertEqual(out["a"], "1.00B")
        self.assertEqual(out["c"], "1.50B")
        self.assertNotIn("1000", out["b"])
        self.assertEqual(out["d"], "1m")
        self.assertEqual(out["e"], "1.0h")
        self.assertEqual(out["f"], "0s")
        self.assertEqual(out["g"], "—")
        self.assertEqual(out["h"], "repo-overview")
        self.assertEqual(out["i"], "release-abc123")
        self.assertEqual(out["j"], "fix/deadbe")

    def test_oe14_export_tooltip_says_filters_are_ignored_and_dates_are_export_zone_days(self):
        title = run("SERVED_BY_DASHBOARD = true; wireExport(); out(el('exportBtn').title)", export_zone="Asia/Tokyo")
        self.assertIn("project and model filters are ignored", title)
        self.assertIn("days in the export zone (Asia/Tokyo)", title)

    def test_oe16_cli_notice_starts_at_the_oldest_cli_transcript_start(self):
        sessions = [session("a", day="2026-09-01", entrypoints={"cli": 1}),
                    session("b", day="2026-09-20", entrypoints={"cli": 1}),
                    session("d", day="2026-06-01", entrypoints={"claude-desktop": 1})]
        html = run("renderNotices(); out(el('notices').innerHTML)",
                   {"sessions": sessions, "history": {"cli_history_truncated": True, "cli_history_from": "2026-09-28",
                                                       "retention_days_default": 30}})
        self.assertIn("starts 2026-09-01", html)

    def test_oe16_static_build_marks_frozen_price_notices(self):
        payload = {"pricing": {"stale": True, "checked": "2026-01-01", "age_days": 300},
                   "price_check": {}, "generated_at": "2026-10-20T12:00:00", "sessions": []}
        html = run_js(DASHBOARD, setup(payload) + "SERVED_BY_DASHBOARD = false; renderNotices(); out(el('notices').innerHTML)")
        self.assertIn("this file does not re-check", html)
        html = run("renderNotices(); out(el('notices').innerHTML)", payload)
        self.assertNotIn("does not re-check", html)

    def test_oe16_filters_round_trip_through_the_address(self):
        out = run("PAYLOAD.dims = {projects: ['p'], models: ['m']}; state._minDate = '2026-01-01'; state._maxDate = '2026-10-25';"
                  "projectMS = {setValues(v){ this.v = v; }, getValues(){ return this.v || []; }};"
                  "modelMS = {setValues(v){ this.v = v; }, getValues(){ return this.v || []; }};"
                  "window.location = {search: '?from=2026-10-01&to=2026-10-10&grain=1440&project=p&model=nope&model=m', "
                  "                   hash: '#tools', pathname: '/'};"
                  "let written; window.history = {replaceState(a, b, url){ written = url; }};"
                  "applyUrlState('2026-01-01', '2026-10-25'); writeUrlState();"
                  "out({from: state.from, to: state.to, grain: state.grain, projects: state.projects, models: state.models, written})")
        self.assertEqual((out["from"], out["to"], out["grain"]), ("2026-10-01", "2026-10-10", 1440))
        self.assertEqual(out["projects"], ["p"])
        self.assertEqual(out["models"], ["m"])             # the unknown model is dropped
        self.assertIn("from=2026-10-01", out["written"])
        self.assertIn("grain=1440", out["written"])
        self.assertTrue(out["written"].endswith("#tools"))

    def test_oe16_bad_address_values_are_ignored(self):
        out = run("projectMS = {setValues(){}, getValues(){ return []; }}; modelMS = projectMS;"
                  "window.location = {search: '?from=nope&to=2099-01-01&grain=7'};"
                  "state.from = '2026-10-21'; state.to = '2026-10-25'; applyUrlState('2026-01-01', '2026-10-25');"
                  "out([state.from, state.to, state.grain])")
        self.assertEqual(out, ["2026-10-21", "2026-10-25", 480])

    def test_oe16_quick_range_buttons_say_they_count_back_from_the_last_data_day(self):
        # (the stub's button list is empty: check the string the page builds)
        src = open(os.path.join(REPO, "static/app.js"), encoding="utf-8").read()
        self.assertIn("of data, ending", src)


# ---------------------------------------------------------------- HB ------- #

HEALTH = {
    "bucket_minutes": 60,
    "thresholds": {"long_context_tokens": 200000, "context_bands": [100000, 200000, 500000],
                   "flag_share": 0.1, "error_streak": 3},
    "benchmarks": {"cost_per_active_day": {"average": 13.0, "p90": 30.0}},
    "sources": {"costs": "https://x", "best_practices": "https://y"},
    "context": [], "cache_misses": [], "events": [], "checks": [], "error_streaks": [],
}


def hsession(sid, day=DAY, recorded=None, **health):
    h = {"edits": 0, "checked_after_last_edit": None, "plan_mode": False, "instructions": None,
         "prompts": 1, "recorded": recorded or {"denials": True, "compactions": True},
         "compactions_auto": 0, "long_context_share": None, "peak_context": 0, "cold_cache_cost": 0,
         "max_error_streak": 0, "ended_on_interrupt": False}
    h.update(health)
    return session(sid, day=day, health=h)


def habits(snippet, sessions=(), usage=(), tools=(), events=(), **kw):
    payload = {"sessions": list(sessions), "usage": list(usage), "tools": list(tools),
               "health": dict(HEALTH, events=list(events))}
    return run(snippet, payload, **kw)


def tile(html, label):
    return html.split(f'<div class="label">{label}</div><div class="value">')[1].split("</div>")[0]


class TestHabitsTab(unittest.TestCase):
    def test_hb3_compactions_are_not_called_unrecorded_when_no_session_ended_in_range(self):
        # The session is still open: it ends after the range, so no session "ended" in it.
        s = hsession("a", day="2026-10-20")
        s["end"] = s["end_day"] = "2026-10-28"
        s["end"] = "2026-10-28T10:00:00"
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [s], [usage_row("a")])
        self.assertEqual(tile(html, "Compactions"), "0")
        self.assertNotIn("not in these logs before a later Claude Code version", html)

    def test_hb3_still_says_not_recorded_when_active_sessions_really_lack_it(self):
        s = hsession("a", recorded={"denials": False, "compactions": False})
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [s], [usage_row("a")])
        self.assertEqual(tile(html, "Compactions"), "—")
        self.assertIn("not in these logs before a later Claude Code version", html)

    def test_hb3_a_quiet_range_says_so_instead_of_not_recorded(self):
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [])
        self.assertIn("no session worked in these dates", html)
        self.assertIn("no session ended in these dates", html)

    def test_hb4_tool_calls_per_prompt_counts_main_conversation_calls_only(self):
        events = [[f"{DAY}T10:00", "a", "prompt", "auto", 4]]
        tools = [[DAY, "a", "Bash", 20, 0, 0, "m"], [DAY, "a", "Read", 100, 0, 1, "m"]]   # 100 are a subagent's
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [hsession("a")], [usage_row("a")], tools, events)
        self.assertEqual(tile(html, "Tool calls per prompt"), "5.0")
        self.assertNotIn("Requests per prompt", html)
        self.assertIn("~5 actions for novices, ~12 for experts", html)

    def test_hb4_hb11_under_a_model_filter_it_is_a_dash_with_a_note(self):
        events = [[f"{DAY}T10:00", "a", "prompt", "auto", 4]]
        tools = [[DAY, "a", "Bash", 20, 0, 0, "m"]]
        html = habits("state.models = ['m']; renderHabits(); out(el('habitTiles').innerHTML)",
                      [hsession("a")], [usage_row("a")], tools, events)
        self.assertEqual(tile(html, "Tool calls per prompt"), "—")
        self.assertIn("not split by model", html)

    def test_hb11_tooltip_says_subagents_are_left_out(self):
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [hsession("a")], [usage_row("a")])
        self.assertIn("subagents&#39; calls left out", html)

    def test_hb5_checks_are_called_shell_checks(self):
        html = habits("renderHabits(); out(el('habitCheckTiles').innerHTML)", [hsession("a")], [usage_row("a")])
        self.assertIn("Shell check after last edit", html)
        self.assertIn("Shell test runs", html)
        strip = habits("renderHabitStrip(); out(el('habitsStrip').innerHTML)",
                       [hsession("a", edits=2, checked_after_last_edit=True)], [usage_row("a")])
        self.assertIn("ran a shell test, lint or build", strip)
        tags = habits("out(sessionHabitTags(PAYLOAD.sessions[0]).map(t => t[1]).join('|'))",
                      [hsession("a", edits=2, checked_after_last_edit=False)], [usage_row("a")])
        self.assertIn("shell test, lint or build", tags)

    def test_hb6_footnote_quotes_only_what_anthropic_says(self):
        html = open(os.path.join(REPO, "static/index.html"), encoding="utf-8").read()
        self.assertIn("Give Claude a check to run", html)
        self.assertNotIn("don't ship what you can't verify", html)
        self.assertNotIn("don&rsquo;t ship what you can&rsquo;t verify", html)

    def test_hb10_note_says_which_group_each_figure_counts(self):
        note = habits("renderHabits(); out(el('habitPopulations').textContent)", [hsession("a")], [usage_row("a")])
        self.assertIn("ended on them", note)
        self.assertIn("tool calls per prompt counts main-conversation tool calls", note)

    def test_hb12_cold_cache_tag_says_the_line_is_the_tools_own(self):
        tags = habits("out(sessionHabitTags(PAYLOAD.sessions[0]))",
                      [hsession("a", cold_cache_cost=2.0)], [usage_row("a")])
        tip = [t for t in tags if t[0].startswith("cold cache")][0][1]
        self.assertIn("this tool's own", tip)
        self.assertIn("avoidable", tip)

    def test_hb15_away_recaps_are_shown(self):
        events = [[f"{DAY}T10:00", "a", "prompt", "auto", 2], [f"{DAY}T10:00", "a", "away", None, 3]]
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", [hsession("a")], [usage_row("a")], (), events)
        self.assertIn("3 away recaps", html)

    def test_hb16_median_prompts_per_session_keeps_the_half(self):
        sessions = [hsession("a", prompts=4), hsession("b", prompts=5)]
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", sessions, [usage_row("a"), usage_row("b")])
        self.assertEqual(tile(html, "Prompts per session"), "4.5")
        sessions = [hsession("a", prompts=4), hsession("b", prompts=6)]
        html = habits("renderHabits(); out(el('habitTiles').innerHTML)", sessions, [usage_row("a"), usage_row("b")])
        self.assertEqual(tile(html, "Prompts per session"), "5")


# ---------------------------------------------------------------- TS ------- #

class TestSessionsTab(unittest.TestCase):
    def test_ts6_scatter_plots_whole_session_cost_against_whole_active_time(self):
        s = session("a", active_ms=600000, est_cost_main=9, est_cost_sub=1)
        pt = run("renderSessions(); out(charts_made[0].data.datasets[0].data[0])",
                 {"sessions": [s], "usage": [usage_row("a", cost=2.0)]})
        self.assertEqual((pt["x"], pt["y"]), (10, 10))

    def test_ts9_a_transcript_with_no_reply_is_badged_and_the_caption_counts_both(self):
        sessions = [session("a"), session("b", requests=0)]
        out = run("renderSessions(); out({html: el('#sessionsTable tbody').innerHTML,"
                  " cap: el('sessionsCaption').textContent})", {"sessions": sessions})
        self.assertEqual(out["html"].count("no reply"), 1)
        self.assertIn("1 session, plus 1 transcript that never got a reply", out["cap"])

    def test_ts10_fork_toggle_counts_only_the_copies_in_the_filters(self):
        sessions = [session("a"), session("c", derived=True, requests=0),
                    session("far", day="2026-03-01", derived=True, requests=0)]
        out = run("renderSessions(); out(el('forkToggle').textContent)", {"sessions": sessions})
        self.assertEqual(out, "Show 1 fork/resume copy")

    def test_ts13_a_continuation_shows_and_filters_by_its_own_first_record(self):
        # `start` is the original's (copied); the work was done 10 days later.
        s = session("a", day="2026-10-10", active_start="2026-10-20T08:00:00",
                    end="2026-10-20T11:00:00", end_day="2026-10-20")
        s["start"] = "2026-10-10T10:00:00"
        out = run("out({day: sessionStartDay(s), raw: sessionRawStartDay(s), time: sessionStartTime(s),"
                  " inRange: sessionInFilters(s)})".replace("(s)", "(PAYLOAD.sessions[0])"),
                  {"sessions": [s]}, frm="2026-10-18", to="2026-10-20")
        self.assertEqual(out["day"], "2026-10-20")
        self.assertEqual(out["raw"], "2026-10-10")
        self.assertEqual(out["time"], "2026-10-20T08:00:00")
        self.assertTrue(out["inRange"])
        out = run("out(sessionInFilters(PAYLOAD.sessions[0]))", {"sessions": [dict(s, end="2026-10-12T10:00:00", end_day="2026-10-12")]},
                  frm="2026-10-18", to="2026-10-20")
        self.assertFalse(out)

    def test_ts16_sort_flips_text_columns_open_ascending_and_project_sorts_by_label(self):
        sessions = [session("a", project="/a/y/zeta", title="B"), session("b", project="/z/b/alpha", title="A")]
        out = run("sessionSort = {key: 'project', dir: 1};"
                  "out(sortSessions(PAYLOAD.sessions).map(s => s.id))", {"sessions": sessions})
        self.assertEqual(out, ["b", "a"])            # ".../b/alpha" < ".../y/zeta", though "/z/..." > "/a/..." as paths
        out = run("const th = {dataset: {sort: 'title'}, classList: {toggle(){}}, setAttribute(k, v){ this[k] = v; }};"
                  "document.querySelectorAll = () => [th]; sessionSort = {key: 'start', dir: -1};"
                  "renderSessionsTable = () => {}; wireSessionSort(); th.onclick(); const first = sessionSort.dir;"
                  "wireSessionSort(); th.onclick(); out([first, sessionSort.dir, th['aria-sort']])", {"sessions": sessions})
        self.assertEqual(out[:2], [1, -1])           # a text column opens ascending, then flips

    def test_ts16_rows_and_headers_are_keyboard_reachable_and_pr_links_dont_toggle(self):
        s = session("a", prs=[{"url": "https://x/1", "number": 1}])
        html = run("renderSessions(); out(el('#sessionsTable tbody').innerHTML)", {"sessions": [s]})
        self.assertIn('tabindex="0"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('onclick="event.stopPropagation()"', html)

    def test_ts17_counts_have_thousands_separators(self):
        s = session("a", tool_calls=12345, n_subagents=1234, tool_errors=1500)
        html = run("renderSessions(); out(el('#sessionsTable tbody').innerHTML)", {"sessions": [s]})
        self.assertIn("12,345 (1,500 err)", html)
        self.assertIn(">1,234<", html)

    def test_ts17_expanding_a_row_gives_the_main_and_subagent_tool_split(self):
        s = session("a", tool_calls=100, main_tool_calls=30, tool_errors=4)
        out = run("renderSessions(); const tr = {dataset: {idx: '0'}, nextElementSibling: null, after(x){ this.row = x; },"
                  "  setAttribute(){}}; document.createElement = () => ({});"
                  "toggleSessionDetail(tr, PAYLOAD.sessions); out(tr.row.innerHTML)", {"sessions": [s]})
        self.assertIn("30 in the main conversation", out)
        self.assertIn("70 by subagents", out)

    def test_ts1_a_session_without_a_counter_or_edits_shows_dashes_not_zeros(self):
        s = session("a", cli_cost=None, lines_added=None, lines_removed=None)
        html = run("renderSessions(); out(el('#sessionsTable tbody').innerHTML)", {"sessions": [s]})
        self.assertNotIn("+0/-0", html)
        self.assertNotIn("CLI", html)
        cells = re.findall(r'<td class="num">(.*?)</td>', html)
        self.assertEqual(cells.count("—"), 2, cells)      # CLI cost and lines


class TestToolsTab(unittest.TestCase):
    def _tools(self, rows, snippet="renderTools();", **kw):
        payload = {"tools": [[DAY, "s1", n, c, e, 0] + ([m] if m else []) for n, c, e, m in rows],
                   "sessions": [session("s1", models=["haiku", "opus"])]}
        return run(snippet, payload, **kw)

    def test_ts2_the_model_filter_keeps_only_calls_made_by_that_model(self):
        n = self._tools([("Bash", 10, 1, "haiku"), ("Bash", 90, 9, "opus")],
                        "state.models = ['opus']; out(toolRows().map(r => r[3]))")
        self.assertEqual(n, [90])

    def test_ts2_an_old_payload_without_the_column_falls_back_to_the_sessions_models(self):
        n = self._tools([("Bash", 10, 1, None)], "state.models = ['opus']; out(toolRows().map(r => r[3]))")
        self.assertEqual(n, [10])
        n = self._tools([("Bash", 10, 1, None)], "state.models = ['sonnet']; out(toolRows().map(r => r[3]))")
        self.assertEqual(n, [])

    def _labels(self, rows):
        return self._tools(rows, "renderTools(); out({err: charts_made.filter(c => c.data.datasets[0].backgroundColor "
                                 "&& Array.isArray(c.data.datasets[0].backgroundColor))[0].data.labels,"
                                 " note: el('toolErrorNote').textContent, mcp: el('mcpNote').textContent,"
                                 " servers: charts['mcpServer'] && charts['mcpServer'].data.labels})")

    def test_tm4_a_real_tool_that_fails_every_time_stays(self):
        out = self._labels([("WebSearch", 60, 60, "m"), ("Bash", 200, 20, "m")])
        self.assertIn("WebSearch", out["err"])

    def test_tm4_an_unknown_unprefixed_name_that_never_succeeded_is_left_out_and_named(self):
        out = self._labels([("bash", 17, 17, "m"), ("Bash", 200, 20, "m")])
        self.assertNotIn("bash", out["err"])
        self.assertIn("bash (17 calls, none succeeded)", out["note"])
        self.assertIn("isn't a Claude Code built-in", out["note"])

    def test_tm4_a_stray_mcp_tool_name_is_listed_apart_and_not_added_to_its_servers_rate(self):
        out = self._labels([("mcp__jira__getIssue", 40, 0, "m"), ("mcp__jira__getIsue", 38, 38, "m"),
                            ("Bash", 100, 10, "m")])
        self.assertNotIn("mcp__jira__getIsue", out["err"])
        self.assertIn("mcp__jira__getIsue (38 calls, none succeeded)", out["note"])
        self.assertIn("never worked while other tools on its server did", out["note"])
        tip = self._tools([("mcp__jira__getIssue", 40, 0, "m"), ("mcp__jira__getIsue", 38, 38, "m")],
                          "renderTools(); const c = charts['mcpServer'];"
                          " out(c.options.plugins.tooltip.callbacks.label({raw: 40, dataIndex: 0}))")
        self.assertIn("40 calls, 0 errors", tip)

    def test_tm4_a_whole_mcp_server_that_never_works_is_kept(self):
        out = self._labels([("mcp__down__a", 12, 12, "m"), ("Bash", 200, 20, "m")])
        self.assertIn("mcp__down__a", out["err"])

    def test_tm4_habits_worst_tool_follows_the_same_rule(self):
        tools = [[DAY, "a", "bash", 30, 30, 0, "m"], [DAY, "a", "mcp__s__x", 20, 0, 0, "m"],
                 [DAY, "a", "mcp__s__y", 25, 25, 0, "m"], [DAY, "a", "WebSearch", 15, 15, 0, "m"]]
        name = habits("out(habitMeasures({from: '%s', to: '%s'}).worstTool.name)" % (DAY, DAY),
                      [hsession("a")], [usage_row("a")], tools)
        self.assertEqual(name, "WebSearch")

    def test_ts8_the_errors_chart_says_it_is_the_top_15_and_what_is_left_out(self):
        rows = [(f"mcp__srv{i}__t", 20, 20 - i, "m") for i in range(18)] + [("Edit", 5, 3, "m")]
        out = self._labels(rows)
        self.assertEqual(len(out["err"]), 15)
        self.assertIn("Showing the 15 tools with the most errors of 18; 3 more", out["note"])
        self.assertIn("1 tool with fewer than 10 calls (3 errors) aren't ranked.", out["note"])

    def test_ts14_the_server_rollup_leaves_out_core_tools_and_merges_names_of_one_service(self):
        rows = [("Bash", 500, 5, "m"), ("mcp__claude_ai_Atlassian__a", 4, 0, "m"),
                ("mcp__plugin_atlassian_atlassian__b", 6, 0, "m"),
                ("mcp__2888b309-b804-4b3f-bab5-50f0dc687b18__c", 3, 0, "m")]
        out = self._labels(rows)
        self.assertEqual(out["servers"], ["atlassian", "connector 2888b309"])
        self.assertIn("500 calls", out["mcp"])

    def test_ts15_calls_without_errors_say_so_not_nothing_in_this_range(self):
        # the stub keeps no DOM; check the message the page asks for
        msg = self._tools([("Bash", 50, 0, "m")],
                          "let seen = []; setChartEmpty = (c, e, m) => { seen.push(m); };"
                          "renderTools(); out(seen.filter(Boolean))")
        self.assertTrue(any(m["title"] == "No tool errors in this range" and "50 tool calls, none failed" in m["body"]
                            for m in msg), msg)


class TestExportAndReconcile(unittest.TestCase):
    def test_ex3_all_sends_no_dates_and_a_range_sends_them(self):
        out = run("state._minDate = '2026-01-01'; state._maxDate = '2026-10-25'; state.from = '2026-01-01'; state.to = '2026-10-25';"
                  "const all = exportUrl(); state.from = '2026-10-01'; out([all, exportUrl()])")
        self.assertEqual(out[0], "/api/export/all.zip")
        self.assertEqual(out[1], "/api/export/all.zip?from=2026-10-01&to=2026-10-25")

    def _rec(self, sessions, frm="2026-10-20", to="2026-10-21"):
        cli = [{"model": "m", "est_cost": 1, "est_tokens": 10, "cli_cost": 1, "cli_tokens": 10}]
        for s in sessions:
            if not s["derived"]:                 # the counter sits on the owner only
                s["cli_models"] = cli
        return run("state.models = []; out(reconciliationRows())", {"sessions": sessions}, frm=frm, to=to)

    def test_rc1_a_shared_counter_whose_member_runs_past_the_range_is_left_out(self):
        owner = session("a", day="2026-10-20", cli_counter_shared_with=["b"])
        member = session("b", day="2026-10-20", end_day="2026-10-23", end="2026-10-23T09:00:00", derived=True, requests=0)
        self.assertEqual(self._rec([owner, member]), [])

    def test_rc1_a_shared_counter_wholly_inside_the_range_counts_once_per_member(self):
        owner = session("a", day="2026-10-20", cli_counter_shared_with=["b"])
        member = session("b", day="2026-10-20", derived=True, requests=0)
        rows = self._rec([owner, member])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessions"], 2)

    def test_rc1_a_member_that_began_before_the_range_leaves_the_counter_out(self):
        owner = session("a", day="2026-10-20", cli_counter_shared_with=["b"])
        member = session("b", day="2026-10-10", end_day="2026-10-20", derived=True, requests=0)
        self.assertEqual(self._rec([owner, member]), [])


class TestStaticDocs(unittest.TestCase):
    def test_interface_doc_no_longer_claims_every_section_has_a_table(self):
        text = open(os.path.join(REPO, "docs/interface.md"), encoding="utf-8").read()
        self.assertNotIn("Every section has a table view", text)
        self.assertIn("top 15", text)

    def test_usage_health_doc_matches_the_ui(self):
        text = open(os.path.join(REPO, "docs/usage-health.md"), encoding="utf-8").read()
        self.assertNotRegex(text, r"each tag links to its fix")
        self.assertNotIn("last ~5 sessions", text)
        self.assertNotIn("Only with a privacy decision (§4) |", text)
        self.assertIn("Tool calls per prompt", text)


if __name__ == "__main__":
    unittest.main()
