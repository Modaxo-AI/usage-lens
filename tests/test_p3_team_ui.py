"""Phase 3 fixes to the team dashboard's page and findings (static/team.js,
static/team-findings.js, static/team.html), run under node against a stub DOM.
Each test names the finding it covers (TO-, SP-, TM- ids from the review ledger)."""
import json
import re
import unittest
from pathlib import Path

from tests.jsrun import run_js

REPO = Path(__file__).resolve().parent.parent
TEAM = ["static/team-findings.js", "static/team.js"]
F = ["static/team-findings.js"]
HTML = (REPO / "static" / "team.html").read_text(encoding="utf-8")

WEEK = "2026-09-14"
WINDOW = {"window_from": "2026-08-31", "window_to": "2026-09-27"}


def row(person, week, repo, cost, days=1, exact=None, machine="", partial=False, cli_gap=False,
        wdays=7, lines=None, removed=None, with_lines=0.0, sessions=1):
    """A 19-field weekly row (team.team_payload's layout, with the machine last)."""
    return [person, week, repo, cost, 0, 1, 1, days, partial, cli_gap, lines, removed, with_lines,
            sessions, wdays, 0.0, 0, exact, machine]


def setup(**payload):
    base = {"people": [], "weekly": [], "limits": [], "models": [], "prs": [], "tools": [],
            "sources": [], "warnings": [], "prices": None, "forecast": None, "health": {"rows": []}}
    base.update(payload)
    return (f"PAYLOAD = {json.dumps(base)}; state.people = [];"
            "state.from = '2026-08-31'; state.to = '2026-09-28';")


def render(fn, element, **payload):
    return run_js(TEAM, setup(**payload) + f"{fn}(); out(el({element!r}).innerHTML);")


class TestTwoMachinesDays(unittest.TestCase):
    """TO-1 / TO-6: a person with two machines has between the busier machine's
    days and both added together, and spend per day shows that range."""

    def weekly(self, cost=105.0):
        return [row("a", WEEK, "r", cost / 2, exact=3, machine="m1"),
                row("a", WEEK, "r", cost / 2, exact=3, machine="m2")]

    def test_the_week_has_a_range_not_the_larger_machine_as_exact(self):
        res = run_js(F, f"out(weekFigure({json.dumps(self.weekly())}));")
        self.assertEqual((res["lo"], res["hi"], res["approx"]), (3, 6, True))

    def test_one_machine_stays_exact(self):
        rows = [row("a", WEEK, "r", 5, exact=3, machine="m1"), row("a", WEEK, "r2", 5, exact=3, machine="m1")]
        res = run_js(F, f"out(weekFigure({json.dumps(rows)}));")
        self.assertEqual((res["lo"], res["hi"], res["approx"]), (3, 3, False))

    def test_the_sum_is_capped_at_seven(self):
        rows = [row("a", WEEK, "r", 5, exact=5, machine="m1"), row("a", WEEK, "r", 5, exact=4, machine="m2")]
        res = run_js(F, f"out(weekFigure({json.dumps(rows)}));")
        self.assertEqual((res["lo"], res["hi"]), (5, 7))

    def test_per_day_carries_its_range(self):
        res = run_js(F, f"out(perActiveDay({json.dumps(self.weekly())}));")[0]
        self.assertEqual((res["daysLo"], res["daysHi"]), (3, 6))
        self.assertAlmostEqual(res["perDayHi"], 35.0)   # $105 over the fewest days
        self.assertAlmostEqual(res["perDayLo"], 17.5)   # over the most
        self.assertTrue(res["approx"])

    def test_the_per_day_finding_does_not_fire_when_more_days_would_clear_it(self):
        weekly = self.weekly()  # $35 a day at 3 days, $17.50 at 6
        fs = run_js(F, f"out(findings({{people: [{{person: 'a'}}], weekly: {json.dumps(weekly)}}}, '2026-09-21'));")
        self.assertEqual([f for f in fs if f["rule"] == "per_day"], [])

    def test_the_per_day_finding_fires_when_even_the_most_days_leave_them_over(self):
        weekly = self.weekly(cost=300.0)  # $100 a day at 3 days, $50 at 6: over $30 either way
        fs = run_js(F, f"out(findings({{people: [{{person: 'a'}}], weekly: {json.dumps(weekly)}}}, '2026-09-21'));")
        (f,) = [f for f in fs if f["rule"] == "per_day"]
        self.assertIn("at least $50", f["text"])
        self.assertIn("≈", f["text"])

    def test_the_table_shows_the_range_with_an_approximation_mark(self):
        people = [dict(WINDOW, person="a")]
        html = render("renderPerDay", "#perDayTable tbody", people=people, weekly=self.weekly())
        self.assertIn("≈3–6", html)
        self.assertIn("$18–$35", html)

    def test_an_exact_person_is_shown_exact(self):
        people = [dict(WINDOW, person="a")]
        weekly = [row("a", WEEK, "r", 60.0, exact=3, machine="m1")]
        html = render("renderPerDay", "#perDayTable tbody", people=people, weekly=weekly)
        self.assertNotIn("≈", html)
        self.assertIn("$20", html)

    def test_the_week_is_a_floor_for_seat_use(self):
        p = dict(WINDOW, person="a", machines=2)
        rows = self.weekly()
        res = run_js(F, f"out(personInRange({json.dumps(p)}, {json.dumps(rows)}, [], [], "
                        f"{{from: '2026-09-14', to: '2026-09-20'}}, null, {{allRows: {json.dumps(rows)}}}));")
        self.assertTrue(res["lower"])
        self.assertEqual((res["daysLo"], res["daysHi"]), (3, 6))
        html = run_js(TEAM, setup(people=[p], weekly=rows) + "state.from = state.to = '2026-09-14'; "
                      "renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        self.assertIn("≥60%", html)  # 3 days of the week's 5 workdays: a floor
        self.assertIn("worked on more than one machine", html)


class TestFindingsOrderAndGuards(unittest.TestCase):
    def test_to5_per_day_finding_lists_the_worst_first(self):
        weekly = [row(p, WEEK, "r", cost, exact=1, machine="m") for p, cost in
                  [("a", 40.0), ("b", 400.0), ("c", 90.0), ("d", 200.0), ("e", 50.0)]]
        fs = run_js(F, f"out(findings({{people: [], weekly: {json.dumps(weekly)}}}, '2026-09-21'));")
        (f,) = [f for f in fs if f["rule"] == "per_day"]
        self.assertTrue(f["text"].startswith("b ($400/day), d ($200/day), c ($90/day) and 2 more"), f["text"])

    def test_to7_unknown_project_is_never_a_repository(self):
        weekly = [row("a", WEEK, "(unknown project)", 90.0), row("a", WEEK, "real/repo", 10.0)]
        fs = run_js(F, f"out(findings({{people: [], weekly: {json.dumps(weekly)}}}, '2026-09-21'));")
        self.assertEqual([f for f in fs if f["rule"] == "concentration"], [])
        weekly = [row("a", WEEK, "x/one", 90.0), row("a", WEEK, "x/two", 10.0)]
        fs = run_js(F, f"out(findings({{people: [], weekly: {json.dumps(weekly)}}}, '2026-09-21'));")
        self.assertEqual(len([f for f in fs if f["rule"] == "concentration"]), 1)

    def test_to3_failing_integrations_are_one_finding_worst_first(self):
        tools = [["p", WEEK, "mcp__s%d__t" % i, "s%d" % i, 100, 10 + i] for i in range(9)]
        tools.append(["p", WEEK, "mcp__ok__t", "ok", 100, 1])
        fs = run_js(F, f"out(findings({{people: [], tools: {json.dumps(tools)}}}, '2026-09-21'));")
        found = [f for f in fs if f["rule"] == "integration"]
        self.assertEqual(len(found), 1)
        text = found[0]["text"]
        self.assertTrue(text.startswith("9 integrations fail often: s8 (MCP) 18%"), text)
        self.assertIn("and 6 more", text)
        self.assertNotIn("ok (MCP)", text)

    def test_to10_the_benchmark_says_where_it_comes_from_and_tool_thresholds_are_labelled(self):
        self.assertIn("across enterprise deployments", HTML)
        note = HTML.split('id="findingsList"')[1].split("</p>")[0]
        for threshold in ("40%", "20 or more calls", "500k", "7 days"):
            self.assertIn(threshold, note)
        self.assertIn("tool&rsquo;s own", note)
        self.assertIn("across enterprise deployments",
                      run_js(F, "out(findings({people: [], weekly: %s}, '2026-09-21'));"
                             % json.dumps([row("a", WEEK, "r", 500.0, exact=1, machine="m")]))[0]["text"])

    def test_to4_the_model_footnote_does_not_say_output_tokens_use_the_allowance(self):
        self.assertNotIn("what use up the allowance", HTML)
        self.assertNotIn("what use up the allowance", (REPO / "static" / "team.js").read_text(encoding="utf-8"))
        self.assertIn("whole context", HTML)


class TestHistoryDeleted(unittest.TestCase):
    """TO-2: a week whose CLI history is deleted says nothing about seat use."""

    RANGE = "{from: '2026-09-14', to: '2026-09-27'}"

    def person(self, rows):
        p = dict(WINDOW, person="a", machines=1, seat_price=100)
        return run_js(F, f"out(personInRange({json.dumps(p)}, {json.dumps(rows)}, [], [], {self.RANGE}, "
                         f"{{per: 'month'}}, {{allRows: {json.dumps(rows)}}}));")

    def test_all_weeks_deleted_is_not_idle_and_has_no_seat_use_or_value(self):
        rows = [row("a", WEEK, "", 0.0, exact=0, cli_gap=True), row("a", "2026-09-21", "", 0.0, exact=0, cli_gap=True)]
        res = self.person(rows)
        self.assertTrue(res["historyDeleted"])
        self.assertFalse(res["idle"])
        self.assertIsNone(res["seatUse"])
        self.assertIsNone(res["value"])

    def test_some_weeks_deleted_leave_them_out_of_seat_use(self):
        rows = [row("a", WEEK, "r", 10.0, exact=5), row("a", "2026-09-21", "", 0.0, exact=0, cli_gap=True)]
        res = self.person(rows)
        self.assertEqual(res["workdays"], 5)  # the deleted week's five workdays are not counted
        self.assertAlmostEqual(res["seatUse"], 1.0)
        self.assertFalse(res["historyDeleted"])

    def test_a_truly_idle_person_is_still_idle(self):
        rows = [row("a", WEEK, "", 0.0, exact=0), row("a", "2026-09-21", "", 0.0, exact=0)]
        res = self.person(rows)
        self.assertTrue(res["idle"])
        self.assertFalse(res["historyDeleted"])

    def test_the_tile_and_the_idle_finding_leave_them_out(self):
        people = [dict(WINDOW, person="a", machines=1)]
        weekly = [row("a", w, "", 0.0, exact=0, cli_gap=True) for w in ("2026-09-07", "2026-09-14", "2026-09-21")]
        html = run_js(TEAM, setup(people=people, weekly=weekly) + "renderTiles(); out(el('teamTiles').innerHTML);")
        self.assertIn("0 active · 0 no activity in these weeks · 1 with CLI history deleted", html)
        html = run_js(TEAM, setup(people=people, weekly=weekly) + "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertNotIn("no Claude Code activity", html)

    def test_the_people_table_says_so(self):
        people = [dict(WINDOW, person="a", machines=1)]
        weekly = [row("a", w, "", 0.0, exact=0, cli_gap=True) for w in ("2026-09-07", "2026-09-14", "2026-09-21")]
        html = render("renderPeople", "#peopleTable tbody", people=people, weekly=weekly)
        self.assertIn("history deleted", html)
        self.assertNotIn(">idle<", html)


class TestNoLogsExport(unittest.TestCase):
    """SP-2 (page side): a one-day export with nothing in it says nothing about the seat."""

    def test_it_is_not_idle_and_has_no_seat_use(self):
        p = {"person": "k", "window_from": "2026-09-21", "window_to": "2026-09-21", "window_days": 1, "no_usage_window": True}
        res = run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], {{from: '2026-09-21', to: '2026-09-27'}}, null, "
                        "{allRows: []}));")
        self.assertTrue(res["noLogs"])
        self.assertFalse(res["idle"])
        self.assertIsNone(res["seatUse"])

    def test_a_thirty_day_no_logs_window_is_not_idle_either(self):
        # (T1) The same rule for both schemas: the export holds no logs, so nothing says the seat is idle.
        p = {"person": "k", "window_from": "2026-08-29", "window_to": "2026-09-27", "window_days": 30, "no_usage_window": True}
        res = run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], {{from: '2026-09-21', to: '2026-09-27'}}, null, "
                        "{allRows: []}));")
        self.assertTrue(res["noLogs"])
        self.assertFalse(res["idle"])

    def test_a_thirty_day_window_that_is_not_flagged_no_usage_is_idle(self):
        p = {"person": "k", "window_from": "2026-08-29", "window_to": "2026-09-27", "window_days": 30}
        res = run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], {{from: '2026-09-21', to: '2026-09-27'}}, null, "
                        "{allRows: []}));")
        self.assertFalse(res["noLogs"])
        self.assertTrue(res["idle"])


class TestTools(unittest.TestCase):
    """TM-2, TM-3, TM-4 (and TO-9's wording): which tools are shown and how."""

    def tool(self, name, integration, calls, errors, person="p0"):
        return [person, WEEK, name, integration, calls, errors]

    def analyse(self, tools):
        return run_js(F, f"out(analyzeTools({json.dumps(tools)}));")

    def test_tm4_a_real_tool_that_fails_every_time_is_kept(self):
        res = self.analyse([self.tool("WebSearch", "built-in", 60, 60), self.tool("ToolSearch", "built-in", 1, 1)])
        self.assertEqual({r["label"] for r in res["rows"]}, {"WebSearch", "ToolSearch"})
        self.assertEqual(res["invented"], [])

    def test_tm4_a_name_that_is_not_a_tool_and_never_succeeded_is_invented(self):
        res = self.analyse([self.tool("bash", "built-in", 17, 17), self.tool("Bash", "built-in", 100, 10)])
        self.assertEqual([r["label"] for r in res["rows"]], ["Bash"])
        self.assertEqual(res["invented"], [{"tool": "bash", "calls": 17}])

    def test_tm4_an_unknown_name_that_did_succeed_is_kept(self):
        res = self.analyse([self.tool("BrandNewTool", "built-in", 10, 3)])
        self.assertEqual([r["label"] for r in res["rows"]], ["BrandNewTool"])

    def test_tm4_a_misspelled_mcp_tool_is_listed_apart_and_not_in_its_servers_rate(self):
        res = self.analyse([self.tool("mcp__jira__search", "jira", 40, 0), self.tool("mcp__jira__serach", "jira", 38, 38)])
        (jira,) = res["rows"]
        self.assertEqual((jira["calls"], jira["errors"]), (40, 0))
        self.assertEqual(res["neverWorked"], [{"server": "jira (MCP)", "tool": "mcp__jira__serach", "calls": 38}])

    def test_tm4_a_server_whose_every_tool_failed_stays_whole(self):
        res = self.analyse([self.tool("mcp__down__a", "down", 25, 25), self.tool("mcp__down__b", "down", 5, 5)])
        (down,) = res["rows"]
        self.assertEqual((down["calls"], down["errors"]), (30, 30))
        self.assertEqual(res["neverWorked"], [])

    def test_tm4_the_finding_ignores_a_misspelled_name(self):
        tools = [self.tool("mcp__jira__search", "jira", 40, 0), self.tool("mcp__jira__serach", "jira", 38, 38)]
        fs = run_js(F, f"out(findings({{people: [], tools: {json.dumps(tools)}}}, '2026-09-21'));")
        self.assertEqual([f for f in fs if f["rule"] == "integration"], [])

    def friction(self, tools, **kw):
        return run_js(TEAM, setup(tools=tools, **kw) + "renderFriction(); out({main: el('#frictionTable tbody').innerHTML, "
                      "more: el('#frictionMoreBody').innerHTML, notes: el('frictionNotes').innerHTML, "
                      "cc: el('#ccToolsTable tbody').innerHTML, wrapHidden: el('frictionMoreWrap').hidden});")

    def test_tm2_every_mcp_server_is_listed_and_the_rest_wait_under_show_more(self):
        tools = [self.tool(f"mcp__s{i}__t", f"s{i}", 50, 5 + i) for i in range(15)]
        tools += [self.tool(f"Tool{i}", "built-in", 100, 90 - i) for i in range(10)]
        res = self.friction(tools)
        self.assertEqual(res["main"].count("<tr>"), 15)   # all 15 servers, though the cap is 12
        self.assertNotIn("Tool0", res["main"])
        self.assertEqual(res["more"].count("<tr>"), 10)   # the built-ins, all there
        self.assertFalse(res["wrapHidden"])

    def test_tm2_few_rows_need_no_expander(self):
        res = self.friction([self.tool("mcp__s__t", "s", 50, 5)])
        self.assertTrue(res["wrapHidden"])
        self.assertEqual(res["more"], "")

    def test_tm3_people_are_those_who_hit_errors_with_counts(self):
        tools = [self.tool("mcp__s__t", "s", 30, 6, "bo"), self.tool("mcp__s__t", "s", 30, 0, "ann"),
                 self.tool("mcp__s__t", "s", 30, 2, "cy")]
        res = self.friction(tools, people=[{"person": p, "display_name": p.title()} for p in ("ann", "bo", "cy")])
        self.assertIn("Bo (6), Cy (2)", res["main"])
        self.assertNotIn("Ann", res["main"])

    def test_tm4_the_page_lists_never_succeeding_names_apart_and_escapes_them(self):
        tools = [self.tool("mcp__jira__search", "jira", 40, 0), self.tool("mcp__jira__<b>x</b>", "jira", 3, 3),
                 self.tool("<i>bash</i>", "built-in", 4, 4)]
        res = self.friction(tools)
        self.assertIn("never once succeeded", res["notes"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", res["notes"])
        self.assertIn("&lt;i&gt;bash&lt;/i&gt;", res["notes"])
        self.assertNotIn("<b>x", res["notes"])
        self.assertIn("never succeeded (made up by the model, or not available)", res["notes"])

    def test_tm5_people_without_tool_data_are_named(self):
        people = [{"person": "old", "display_name": "Olga"}, {"person": "new", "display_name": "Nia"}]
        sources = [{"person": "old", "files": ["summary"]}, {"person": "new", "files": ["summary", "tools"]}]
        weekly = [row("old", WEEK, "r", 10.0), row("new", WEEK, "r", 10.0)]
        res = self.friction([self.tool("mcp__s__t", "s", 50, 5, "new")], people=people, sources=sources, weekly=weekly)
        self.assertIn("No tool data from Olga", res["notes"])
        self.assertNotIn("Nia", res["notes"])


class TestHabitsNameWhoIsMissing(unittest.TestCase):
    """TM-5 and TM-10."""

    def habits(self, rows, **kw):
        return run_js(TEAM, setup(health={"rows": rows}, **kw) + "renderTeamHabits(); out(el('teamHabits').innerHTML);")

    def hrow(self, person, **values):
        base = {"person": person, "week": WEEK, "est_cost_usd": 100.0, "est_cost_main_usd": 50.0, "sessions_started": 4,
                "long_context_cost_usd": 10.0, "very_long_context_cost_usd": 0.0, "cold_cache_usd": 1.0, "prompts": 40,
                "interrupts": 4, "sessions_edited": 3, "sessions_checked_after_edit": 2, "sessions_plan_mode": 1,
                "sessions_with_subagents": 1, "sessions_instructions_recorded": 4, "sessions_with_project_claude_md": 4}
        base.update(values)
        return base

    def test_a_person_with_usage_but_no_habit_data_is_named_and_counted(self):
        people = [{"person": "new", "display_name": "Nia"}, {"person": "old", "display_name": "Olga"}]
        weekly = [row("new", WEEK, "r", 10.0), row("old", WEEK, "r", 10.0)]
        html = self.habits([self.hrow("new")], people=people, weekly=weekly)
        self.assertIn("1 of the 2 people who used Claude Code", html)
        self.assertIn("Olga", html)
        self.assertIn("schema 7", html)

    def test_everyone_with_data_keeps_the_plain_footnote(self):
        people = [{"person": "new", "display_name": "Nia"}]
        html = self.habits([self.hrow("new")], people=people, weekly=[row("new", WEEK, "r", 10.0)])
        self.assertIn("1 person used Claude Code in the selected weeks", html)
        self.assertNotIn("Not shown", html)

    def test_tm10_a_share_that_displays_as_ten_percent_carries_the_badge(self):
        people = [{"person": "a"}]
        html = self.habits([self.hrow("a", cold_cache_usd=9.96, est_cost_usd=100.0)], people=people,
                           weekly=[row("a", WEEK, "r", 100.0)])
        self.assertIn('<span class="badge warn">10.0%</span>', html)

    def test_tm10_cost_per_1k_lines_says_what_a_line_is(self):
        self.assertIn("lines added plus removed", HTML)


class TestOutput(unittest.TestCase):
    """TM-1 and TM-9."""

    def test_tm1_spend_with_line_data_is_a_share_of_the_sessions_that_started(self):
        # The week's own cost is $100 but the sessions that started in it cost $300 in all
        # (they ran on into other weeks), $290 of them with line data: 97%, never 290%.
        rows = [row("a", WEEK, "r", 100.0, lines=900.0, removed=100.0, with_lines=290.0)]
        res = run_js(F, f"out(outputByPerson({json.dumps(rows)}, [], {json.dumps([['a', WEEK, 300.0]])}));")["a"]
        self.assertAlmostEqual(res["lineCoverage"], 290 / 300)
        self.assertLessEqual(res["lineCoverage"], 1.0)
        self.assertAlmostEqual(res["perKLines"], 290.0)  # 290 / 1000 lines * 1000

    def test_tm1_without_session_costs_there_is_no_share(self):
        rows = [row("a", WEEK, "r", 100.0, lines=900.0, removed=100.0, with_lines=290.0)]
        res = run_js(F, f"out(outputByPerson({json.dumps(rows)}, []));")["a"]
        self.assertIsNone(res["lineCoverage"])

    def test_tm1_the_page_passes_the_selected_weeks_session_costs(self):
        people = [dict(WINDOW, person="a")]
        rows = [row("a", WEEK, "r", 100.0, lines=900.0, removed=100.0, with_lines=290.0)]
        costs = [["a", WEEK, 300.0], ["a", "2026-01-05", 5000.0]]  # the second is outside the range
        html = render("renderOutput", "#outputTable tbody", people=people, weekly=rows, session_costs=costs)
        self.assertIn("97%", html)

    def prs(self, items):
        return render("renderPrs", "#prTable tbody", prs=items, people=[{"person": "a"}])

    def pr(self, **kw):
        base = {"person": "a", "pr_url": "u", "pr_repo": "o/r", "opened_on": "2026-09-15", "session_id": "s",
                "in_project_repo": "yes", "session_cost_usd": 10.0, "session_prs": 1}
        base.update(kw)
        return base

    def test_tm9_zero_elsewhere_and_unknown_are_different_cells(self):
        known = self.prs([self.pr()])
        self.assertRegex(known, r'<td class="num">0</td>\s*<td class="num">\$10')
        unknown = self.prs([self.pr(in_project_repo="")])
        self.assertIn("?", unknown)
        self.assertNotRegex(unknown, r'<td class="num">0</td>\s*<td class="num">\$10')

    def test_tm9_a_blank_session_cost_is_not_a_free_pr(self):
        html = self.prs([self.pr(session_cost_usd=None), self.pr(session_cost_usd=30.0, session_prs=1)])
        # Two PRs but only one with a cost: the total is $30 and the average over the one that has it.
        self.assertIn("$30", html)
        self.assertIn("1 of 2 PRs", html)
        html = self.prs([self.pr(session_cost_usd=None)])
        self.assertNotIn("$0", html)


class TestChartsAndTables(unittest.TestCase):
    def charts(self, fn, **payload):
        return run_js(TEAM, setup(**payload) + f"{fn}(); out(charts_made.map(c => ({{labels: c.data.labels, "
                      "sets: c.data.datasets.map(d => ({label: d.label, data: d.data}))})));")

    def model(self, person, week, family, cost, model):
        return [person, week, family, "main", cost, 100.0, 0.0, model]

    def test_to8_the_mix_chart_is_by_exact_model_not_family(self):
        models = [self.model("a", WEEK, "opus", 10.0, "claude-opus-5-5"), self.model("a", WEEK, "opus", 4.0, "claude-opus-4-1")]
        (chart,) = self.charts("renderMix", people=[{"person": "a"}], models=models)
        self.assertEqual({s["label"] for s in chart["sets"]}, {"Opus 5.5", "Opus 4.1"})

    def test_to12_a_ninth_model_is_named_not_folded_into_an_other_of_one(self):
        models = [self.model("a", WEEK, "x", 100.0 - i, f"claude-opus-5-{i}") for i in range(9)]
        (chart,) = self.charts("renderMix", people=[{"person": "a"}], models=models)
        self.assertEqual(len(chart["sets"]), 9)
        self.assertNotIn("Other", [s["label"] for s in chart["sets"]])
        models.append(self.model("a", WEEK, "x", 1.0, "claude-opus-6-0"))
        (chart,) = self.charts("renderMix", people=[{"person": "a"}], models=models)
        self.assertEqual([s["label"] for s in chart["sets"]][-1], "Other")
        self.assertEqual(len(chart["sets"]), 9)

    def test_to12_the_model_trend_names_a_ninth_model_too(self):
        models = [self.model("a", WEEK, "x", 100.0 - i, f"claude-opus-5-{i}") for i in range(9)]
        (chart,) = self.charts("renderModelTrend", people=[dict(WINDOW, person="a")], models=models,
                               weekly=[row("a", WEEK, "r", 1.0)])
        self.assertEqual(len(chart["sets"]), 9)
        self.assertNotIn("Other", [s["label"] for s in chart["sets"]])

    def test_to11_weeks_outside_an_export_are_blank_and_people_without_spend_have_no_legend_entry(self):
        people = [dict(WINDOW, person="a"), {"person": "b", "window_from": "2026-09-14", "window_to": "2026-09-27"},
                  dict(WINDOW, person="z")]
        sources = [{"person": "a", **WINDOW}, {"person": "b", "window_from": "2026-09-14", "window_to": "2026-09-27"},
                   {"person": "z", **WINDOW}]
        weekly = [row("a", "2026-09-07", "r", 5.0), row("b", "2026-09-14", "r", 7.0)]
        (chart,) = self.charts("renderTrend", people=people, sources=sources, weekly=weekly)
        by = {s["label"]: s["data"] for s in chart["sets"]}
        self.assertNotIn("z", by)                       # no spend: no legend entry
        weeks = chart["labels"]
        # The label is in the reader's locale ("Sep 7", "7 Sep", "9月7日"), so it is
        # formatted the way the page does rather than spelled out.
        label = run_js([], "out(new Date('2026-09-07T00:00:00').toLocaleDateString("
                           "undefined, { day: 'numeric', month: 'short' }))")
        i = next(n for n, w in enumerate(weeks) if label in w)
        self.assertIsNone(by["b"][i])                   # before b's export: unknown, not $0
        self.assertEqual(by["a"][i], 5.0)

    def test_to11_more_than_nine_people_fold_into_other_and_the_part_week_is_marked(self):
        short = {"window_from": "2026-08-31", "window_to": "2026-09-24"}  # ends before that week's Sunday
        people = [dict(short, person=f"p{i}") for i in range(11)]
        sources = [{"person": p["person"], **short} for p in people]
        weekly = [row(f"p{i}", WEEK, "r", 100.0 - i) for i in range(11)] + [row("p0", "2026-09-21", "r", 1.0)]
        (chart,) = self.charts("renderTrend", people=people, sources=sources, weekly=weekly)
        labels = [s["label"] for s in chart["sets"]]
        self.assertIn("Other (3 people)", labels)
        self.assertEqual(len(labels), 8 + 1 + 1)  # 8 named, Other, the line
        self.assertTrue(chart["labels"][-1].endswith("(part)"))  # the exports end Sep 24, before that week's Sunday


class TestHeatmap(unittest.TestCase):
    def heat(self, weekly, metric="cost", **kw):
        return run_js(TEAM, setup(weekly=weekly, **kw) + f"state.metric = {metric!r}; state.from = '2026-09-07'; "
                      "state.to = '2026-09-21'; renderHeatmap(); out(el('heatmap').innerHTML);")

    def test_sp5_a_week_outside_the_exports_is_not_shown_as_no_activity(self):
        people = [{"person": "a", "window_from": "2026-09-14", "window_to": "2026-09-20"}]
        sources = [{"person": "a", "window_from": "2026-09-14", "window_to": "2026-09-20"}]
        other = [row("b", "2026-09-07", "r", 1.0), row("b", "2026-09-21", "r", 1.0)]  # these set the weeks shown
        html = self.heat([row("a", WEEK, "r", 5.0, exact=2, machine="m")] + other, people=people, sources=sources)
        a_row = html.split("</tr>")[1]
        self.assertEqual(a_row.count("hm-cell uncovered"), 2)  # the weeks before and after
        self.assertIn("not covered by their exports", html)
        self.assertIn("a week none of that person", HTML)

    def test_sp5_a_covered_idle_week_is_still_no_activity(self):
        people = [dict(WINDOW, person="a")]
        sources = [{"person": "a", **WINDOW}]
        other = [row("b", "2026-09-07", "r", 1.0), row("b", "2026-09-21", "r", 1.0)]
        html = self.heat([row("a", WEEK, "r", 5.0, exact=2, machine="m")] + other, people=people, sources=sources)
        self.assertNotIn("uncovered", html.split("</tr>")[1])
        self.assertIn("no activity", html)

    def test_sp14_a_week_one_machine_covers_whole_is_not_a_part_week(self):
        people = [dict(WINDOW, person="a")]
        weekly = [row("a", WEEK, "r", 5.0, exact=2, machine="m1", partial=True, wdays=3),
                  row("a", WEEK, "r", 5.0, exact=2, machine="m2", partial=False)]
        html = self.heat(weekly, people=people)
        self.assertNotIn("part-week", html)
        weekly[1][8] = True
        self.assertIn("part-week", self.heat(weekly, people=people))

    def test_sp15_the_days_view_marks_an_estimated_cell_with_an_approximation_sign(self):
        people = [dict(WINDOW, person="a")]
        weekly = [row("a", WEEK, "r", 5.0, exact=3, machine="m1"), row("a", WEEK, "r", 5.0, exact=3, machine="m2")]
        html = self.heat(weekly, metric="days", people=people)
        self.assertIn(">≈3</td>", html)
        self.assertIn("(estimated, between 3 and 6)", html)   # the tooltip gives the range (T7)

    def test_sp16_one_active_day_is_singular(self):
        people = [dict(WINDOW, person="a")]
        html = self.heat([row("a", WEEK, "r", 5.0, exact=1, machine="m")], people=people)
        self.assertIn("1 active day", html)
        self.assertNotIn("1 active days", html)
        self.assertIn("1 request,", html)


class TestPeopleAndSources(unittest.TestCase):
    def test_sp9_windows_and_the_gap_between_exports_are_listed(self):
        p = {"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-27", "machines": 1, "exports": 2,
             "schema_version": 12, "windows": [["2026-08-31", "2026-09-06"], ["2026-09-21", "2026-09-27"]],
             "gaps": [["2026-09-07", "2026-09-20"]], "gap_days": 14}
        html = render("renderPeople", "#peopleTable tbody", people=[p])
        self.assertIn("2026-08-31 – 2026-09-06<br>2026-09-21 – 2026-09-27", html)
        self.assertIn("14 days between exports not covered", html)

    def test_sp16_headers(self):
        self.assertIn("Extra usage enabled", HTML)
        limits = HTML.split('id="limitsTable"')[1].split("</thead>")[0]
        self.assertIn("Usage tier", limits)
        self.assertNotIn("<th>Seat</th>", limits)

    def test_sp16_every_schema_version_a_person_has_is_shown(self):
        p = {"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-27", "machines": 1, "exports": 2,
             "schema_version": 12}
        sources = [{"person": "a", "schema_version": 12, "files": []}, {"person": "a", "schema_version": 9, "files": []}]
        html = render("renderPeople", "#peopleTable tbody", people=[p], sources=sources)
        self.assertIn("also 9", html)

    def test_sp16_the_limits_status_of_an_export_with_no_allowance_is_not_never_hit(self):
        p = {"person": "a", **WINDOW}
        html = run_js(TEAM, setup(people=[p]) + "renderLimits(); out(el('#limitsTable tbody').innerHTML);")
        self.assertNotIn("never hit", html)

    def test_sp10_skipped_exports_are_listed_and_escaped(self):
        skipped = [{"source": "d (x, 2026)", "person": "<b>x</b>", "reason": "lacks columns <i>"}]
        html = render("renderSources", "#sourcesTable tbody", skipped=skipped,
                      sources=[{"person": "a", "source": "z.zip", "exported_at": "2026-09-30T10:00:00-07:00",
                                "window_from": "2026-09-01", "window_to": "2026-09-30", "schema_version": 12,
                                "machine_id": "m", "files": ["summary"]}])
        self.assertIn("skipped", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("lacks columns &lt;i&gt;", html)

    def test_to14_export_times_are_shown_as_written_with_year_and_offset(self):
        self.assertEqual(run_js(TEAM, "out(fmtAsWritten('2026-10-29T10:05:00-07:00'));"), "2026-10-29 10:05 UTC-07:00")
        self.assertEqual(run_js(TEAM, "out(fmtAsWritten('2026-10-29T10:05:00Z'));"), "2026-10-29 10:05 UTC")
        self.assertEqual(run_js(TEAM, "out(fmtAsWritten('20261029'));"), "20261029")
        self.assertEqual(run_js(TEAM, "out(fmtAsWritten(null));"), "—")

    def test_to13_an_inverted_range_is_swapped(self):
        self.assertEqual(run_js(TEAM, "out(orderWeeks('2026-09-21', '2026-09-07'));"), ["2026-09-07", "2026-09-21", True])
        self.assertEqual(run_js(TEAM, "out(orderWeeks('2026-09-07', '2026-09-21'));"), ["2026-09-07", "2026-09-21", False])


if __name__ == "__main__":
    unittest.main()
