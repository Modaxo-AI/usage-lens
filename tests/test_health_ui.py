"""The Habits strip and tab (static/habits.js) and the team view's habits and
friction (static/team.js), run under node against a stub DOM (tests/jsrun.py)."""
import json
import os
import re
import tempfile
import unittest

from claude_usage import team
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.jsrun import run_js
from tests.test_health import write_health_logs
from tests.test_team import TeamFolder

TEAM = ["static/team-findings.js", "static/team.js"]
DASHBOARD = ["static/habits.js", "static/app.js"]


# --------------------------------------------------------------------------- #
# team view
# --------------------------------------------------------------------------- #

def _health_row(person, **values):
    row = {"person": person, "week": "2026-02-02", "est_cost_usd": 100.0, "est_cost_main_usd": 50.0,
           "sessions_started": 4, "long_context_cost_usd": 10.0, "very_long_context_cost_usd": 0.0,
           "cold_cache_usd": 1.0,
           "prompts": 40, "interrupts": 4, "auto_compactions": 0, "tool_refusals": 0,
           "error_streaks": 0, "sessions_edited": 3, "sessions_checked_after_edit": 2,
           "sessions_plan_mode": 1, "sessions_with_subagents": 1,
           "sessions_instructions_recorded": 4, "sessions_with_project_claude_md": 4}
    row.update(values)
    return row


def _team(rows, people=None, selected=(), tools=()):
    people = people or sorted({r["person"] for r in rows} | {t[0] for t in tools})
    payload = {"people": [p if isinstance(p, dict) else {"person": p} for p in people],
               "health": {"rows": rows}, "tools": list(tools)}
    return (f"PAYLOAD = {json.dumps(payload)};"
            f"state.people = {json.dumps(list(selected))};"
            "state.from = '2026-01-01'; state.to = '2026-12-28';")


def _habits_html(rows, **kw):
    return run_js(TEAM, _team(rows, **kw) + "renderTeamHabits(); out(el('teamHabits').innerHTML);")


def _friction_html(tools, **kw):
    return run_js(TEAM, _team([], tools=tools, **kw) +
                  "renderFriction(); out(el('#frictionTable tbody').innerHTML);")


HABIT_COLUMNS = ["context", "context200", "cold", "checked", "interrupts", "plan", "subagents", "claude_md"]


def _cells(html, marker):
    """{column: cell html} for the habits row opening with `marker`."""
    row = html.split(marker)[1].split("</tr>")[0]
    cells = [c.split(">", 1)[1].split("</td>")[0] for c in row.split('<td class="num"')[1:]]
    return dict(zip(HABIT_COLUMNS, cells))


def _person(html, person):
    return _cells(html, f'data-person="{person}"')


def _idle(person):
    return _health_row(person, prompts=0, est_cost_usd=0, est_cost_main_usd=0,
                       long_context_cost_usd=0, cold_cache_usd=0, interrupts=0,
                       sessions_started=0, sessions_edited=0, sessions_checked_after_edit=0,
                       sessions_plan_mode=0, sessions_with_subagents=0,
                       sessions_instructions_recorded=0, sessions_with_project_claude_md=0)


class TestTeamHabitsPerPerson(unittest.TestCase):
    """Per person and in the open (docs/team.md): a manager sees each
    person's habits, next to the team median, with no minimum group."""

    def test_two_people_get_a_row_each(self):
        html = _habits_html([_health_row("p0", very_long_context_cost_usd=10.0),
                             _health_row("p1", very_long_context_cost_usd=2.5)])
        self.assertEqual(_person(html, "p0")["context"].count("20%"), 1)
        self.assertIn("5.0%", _person(html, "p1")["context"])

    def test_names_are_shown_and_escaped(self):
        html = _habits_html([_health_row("a@x.com")],
                            people=[{"person": "a@x.com", "person_name": "<b>Ann</b>"}])
        self.assertIn("&lt;b&gt;Ann&lt;/b&gt;", html)
        self.assertNotIn("<b>Ann</b>", html)

    def test_people_who_did_not_use_claude_code_are_not_listed(self):
        html = _habits_html([_health_row("p0"), _idle("idle0")])
        self.assertIn('data-person="p0"', html)
        self.assertNotIn("idle0", html)
        self.assertIn("1 person used Claude Code", html)

    def test_team_median_row(self):
        rows = [_health_row("p0", very_long_context_cost_usd=10.0),
                _health_row("p1", very_long_context_cost_usd=20.0),
                _health_row("p2", very_long_context_cost_usd=30.0)]
        self.assertIn("40%", _cells(_habits_html(rows), "Team median")["context"])

    def test_context_tax_is_a_share_of_main_conversation_spend(self):
        # $10 of $50 main-thread spend: 20%, not 10% of the $100 total.
        row = _health_row("p0", very_long_context_cost_usd=10.0)
        self.assertIn("20%", _person(_habits_html([row]), "p0")["context"])

    def test_only_sourced_shares_are_flagged(self):
        html = _habits_html([_health_row("p0", very_long_context_cost_usd=10.0),
                             _health_row("p1", very_long_context_cost_usd=2.5)])
        self.assertIn("badge warn", _person(html, "p0")["context"])      # 20%: at the 10% line
        self.assertNotIn("badge", _person(html, "p1")["context"])        # 5%
        self.assertNotIn("badge", _person(html, "p0")["interrupts"])     # a profile, no line

    def test_nothing_to_measure_is_a_dash(self):
        html = _habits_html([_health_row("p0", sessions_edited=0, sessions_checked_after_edit=0)])
        self.assertEqual(_person(html, "p0")["checked"], "—")

    def test_checked_shows_its_sessions(self):
        self.assertIn("2 of 3", _person(_habits_html([_health_row("p0")]), "p0")["checked"])

    def test_people_filter_narrows_the_rows(self):
        html = _habits_html([_health_row("p0"), _health_row("p1")], selected=["p1"])
        self.assertNotIn('data-person="p0"', html)
        self.assertIn('data-person="p1"', html)

    def test_no_one_with_habit_data(self):
        self.assertIn("No one", _habits_html([_idle("idle0")]))


class TestTeamPageSaysWhatItShows(unittest.TestCase):
    def test_the_card_says_per_person_and_not_a_score(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "team.html"),
                  encoding="utf-8") as fh:
            card = fh.read().split("Habits by person")[1].split('<div class="card">')[0]
        self.assertIn("Tell your team", card)
        self.assertIn("not a score", card)
        self.assertNotIn("five or more", card)


class TestTeamFriction(unittest.TestCase):
    def _tool(self, person, calls=30, errors=6, tool="mcp__tracker__get", integration="tracker"):
        return [person, "2026-02-02", tool, integration, calls, errors]

    def test_one_selected_person_sees_their_rates(self):
        tools = [self._tool(f"p{i}") for i in range(3)]
        html = _friction_html(tools, selected=["p0"])
        self.assertIn("tracker (MCP)", html)
        self.assertIn(">30<", html)

    def test_a_tool_only_one_person_uses_is_shown(self):
        tools = [self._tool("p0"), self._tool("p0", tool="mcp__solo__x", integration="solo")]
        self.assertIn("solo (MCP)", _friction_html(tools))


class TestTeamPayloadMainCost(unittest.TestCase):
    def test_health_rows_carry_main_thread_cost(self):
        root = tempfile.mkdtemp()
        write_health_logs(root)
        payload = scan(root, cache=FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json")))
        folder = TeamFolder()
        self.addCleanup(folder.close)
        folder.zip(payload, "a@example.com", "a@example.com", "20260210", machine="m")
        rows = team.team_payload([folder.dir])["health"]["rows"]
        main = sum(s["health"]["main_cost"] for s in payload["sessions"])
        self.assertAlmostEqual(sum(r["est_cost_main_usd"] for r in rows), main, places=4)


# --------------------------------------------------------------------------- #
# dashboard Habits strip and tab
# --------------------------------------------------------------------------- #

DAY = "2026-02-10"


def _dashboard(sessions=(), usage=(), context=(), tools=(), events=(), checks=()):
    payload = {
        "summary": {"date_max": DAY},
        "usage": list(usage), "tools": list(tools), "sessions": list(sessions),
        "health": {
            "bucket_minutes": 60,
            "thresholds": {"long_context_tokens": 200000, "context_bands": [100000, 200000, 500000],
                           "flag_share": 0.1, "error_streak": 3},
            "benchmarks": {"cost_per_active_day": {"average": 13.0, "p90": 30.0}},
            "sources": {"costs": "https://x", "best_practices": "https://y"},
            "context": list(context), "cache_misses": [], "events": list(events),
            "checks": list(checks), "error_streaks": [],
        },
    }
    return (f"PAYLOAD = {json.dumps(payload)};"
            "sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));"
            f"state.from = '{DAY}'; state.to = '{DAY}';")


def _usage(day, sid, cost):
    row = [f"{day}T10:00", sid, "m"] + [0] * 11
    row[12], row[13] = 1, cost
    return row


def _session(sid, day=DAY, **health):
    h = {"edits": 0, "checked_after_last_edit": None, "plan_mode": False, "instructions": None,
         "prompts": 1, "recorded": {"denials": True, "compactions": True}, "compactions_auto": 0,
         "long_context_share": None, "peak_context": 0, "cold_cache_cost": 0,
         "max_error_streak": 0, "ended_on_interrupt": False}
    h.update(health)
    return {"id": sid, "project": "p", "models": ["m"], "start": f"{day}T10:00:00",
            "end": f"{day}T11:00:00", "derived": False, "n_subagents": 0, "title": "t",
            "est_cost_main": 1, "est_cost_sub": 0, "health": h}


class TestHabitsContextTax(unittest.TestCase):
    def test_strip_divides_by_main_conversation_spend(self):
        # $100 spend, $40 of it main-thread, $20 of that over 200k: 50%.
        setup = _dashboard(usage=[_usage(DAY, "s1", 100.0)],
                           context=[[f"{DAY}T10:00", "s1", "m", 0, 1, 20.0],
                                    [f"{DAY}T10:00", "s1", "m", 2, 1, 20.0]])
        share = run_js(DASHBOARD, setup + f"out(habitMeasures({{from: '{DAY}', to: '{DAY}'}}).longShare)")
        self.assertAlmostEqual(share, 0.5)


class TestContextTaxAt500k(unittest.TestCase):
    """The strip's status follows main spend over 500k tokens; 200k is context."""

    W = f"{{from: '{DAY}', to: '{DAY}'}}"

    def _card(self, bands_costs):
        # bands: 0 <=100k, 1 <=200k, 2 <=500k, 3 >500k
        context = [[f"{DAY}T10:00", "s1", "m", band, 1, cost] for band, cost in bands_costs]
        setup = _dashboard(usage=[_usage(DAY, "s1", 100.0)], context=context)
        snippet = (setup + "PAYLOAD.health.thresholds.flag_context_tokens = 500000;"
                   f"const r = habitMeasures({self.W});"
                   "out({long: r.longShare, very: r.veryLongShare,"
                   " card: habitCards(r, r).find(c => c.label === 'Context tax')});")
        return run_js(DASHBOARD, snippet)

    def test_spend_between_200k_and_500k_is_not_flagged(self):
        res = self._card([(0, 10.0), (2, 90.0)])
        self.assertAlmostEqual(res["long"], 0.9)
        self.assertEqual(res["very"], 0)
        self.assertEqual(res["card"]["status"], "ok")

    def test_spend_over_500k_is_flagged(self):
        res = self._card([(0, 50.0), (3, 50.0)])
        self.assertAlmostEqual(res["very"], 0.5)
        self.assertEqual(res["card"]["status"], "look")
        self.assertIn("500k", res["card"]["sub"])

    def test_a_payload_without_the_flag_line_keeps_200k(self):
        setup = _dashboard(usage=[_usage(DAY, "s1", 100.0)],
                           context=[[f"{DAY}T10:00", "s1", "m", 2, 1, 10.0]])
        very = run_js(DASHBOARD, setup + f"out(habitMeasures({self.W}).veryLongShare)")
        self.assertAlmostEqual(very, 1.0)

    def test_session_tag_uses_the_500k_share(self):
        snippet = (_dashboard() + "PAYLOAD.health.thresholds.flag_context_tokens = 500000;"
                   f"out(sessionHabitTags({json.dumps(_session('s1', long_context_share=0.9, very_long_context_share=0.0, peak_context=400000))}).map(t => t[0]))")
        self.assertFalse([t for t in run_js(DASHBOARD, snippet) if "over" in t])
        snippet = (_dashboard() + "PAYLOAD.health.thresholds.flag_context_tokens = 500000;"
                   f"out(sessionHabitTags({json.dumps(_session('s1', long_context_share=0.9, very_long_context_share=0.4, peak_context=700000))}).map(t => t[0]))")
        self.assertIn("40.0% over 500k", run_js(DASHBOARD, snippet)[0])


class TestHabitsCharts(unittest.TestCase):
    def test_context_chart_is_never_finer_than_an_hour(self):
        setup = _dashboard(context=[[f"{DAY}T10:00", "s1", "m", 0, 1, 5.0]])
        out = run_js(DASHBOARD, setup + "state.grain = 15; renderContextChart({from: state.from, to: state.to});"
                     "out([charts['habitContext'].data.labels.length, el('grainNoteHabits').textContent])")
        self.assertEqual(out[0], 24)
        self.assertIn("hour", out[1])

    def test_count_charts_have_whole_number_ticks(self):
        out = run_js(DASHBOARD, _dashboard() + "renderBarChart('k', 'c', {a: 2}, {}, fmtInt);"
                     "out(charts['k'].options.scales.x.ticks.precision)")
        self.assertEqual(out, 0)


class TestEnvironmentCharts(unittest.TestCase):
    def test_session_count_charts_have_whole_number_ticks(self):
        sessions = [dict(_session("s1"), permission_modes={"auto": 3}, entrypoints={"cli": 3},
                         versions={"2.1.300": 3}),
                    dict(_session("s2"), permission_modes={"plan": 1}, entrypoints={"sdk-ts": 1},
                         versions={"2.1.301": 1})]
        setup = _dashboard(sessions=sessions) + "PAYLOAD.prs = [];"
        # The count axis: x on the horizontal entry-point chart, y on the others.
        out = run_js(DASHBOARD, setup + "renderEnv(); out({"
                     "permissionMode: charts['permissionMode'].options.scales.y.ticks.precision,"
                     "entrypoint: charts['entrypoint'].options.scales.x.ticks.precision,"
                     "versions: charts['versions'].options.scales.y.ticks.precision})")
        self.assertEqual(out, {"permissionMode": 0, "entrypoint": 0, "versions": 0})


class TestHabitsTiles(unittest.TestCase):
    def _tiles(self, sessions):
        return run_js(DASHBOARD, _dashboard(sessions=sessions) +
                      "renderHabits(); out(el('habitTiles').innerHTML)")

    def _value(self, html, label):
        return html.split(f'<div class="label">{label}</div><div class="value">')[1].split("</div>")[0]

    def test_compactions_not_recorded_is_a_dash(self):
        html = self._tiles([_session("s1", recorded={"denials": False, "compactions": False})])
        self.assertEqual(self._value(html, "Compactions"), "—")

    def test_compactions_recorded_is_a_count(self):
        html = self._tiles([_session("s1")])
        self.assertEqual(self._value(html, "Compactions"), "0")


class TestHabitsStripBaselines(unittest.TestCase):
    def test_every_card_compares_to_the_baseline(self):
        base = "2026-02-09"   # the day before DAY: the baseline is the days before the range
        sessions = [_session("s1", edits=2, checked_after_last_edit=True),
                    _session("s0", day=base, edits=1, checked_after_last_edit=False)]
        setup = _dashboard(sessions=sessions,
                           usage=[_usage(DAY, "s1", 10.0), _usage(base, "s0", 10.0)],
                           tools=[[f"{DAY}T10:00", "s1", "Bash", 10, 1],
                                  [f"{base}T10:00", "s0", "Bash", 10, 2]])
        html = run_js(DASHBOARD, setup + "renderHabitStrip(); out(el('habitsStrip').innerHTML)")
        cards = re.split(r'<div class="habit(?: flagged)?" ', html)[1:]
        self.assertEqual(len(cards), 6)
        for card in cards:
            sub = card.split('<div class="sub">')[1].split("</div>")[0]
            # A baseline with no data says so ("no data before this range").
            self.assertTrue("baseline" in sub or "no data before this range" in sub, card)


class TestPersonalRangeEverywhere(unittest.TestCase):
    """The strip, the allowance card and the CLI reconciliation follow the
    selected dates: the strip compares the range to an equal run of days just
    before it, the allowance shows only for a limit week the range overlaps, and
    the reconciliation counts only sessions wholly inside the range."""

    def _windows(self, frm, to):
        return run_js(DASHBOARD, _dashboard() + f"state.from = '{frm}'; state.to = '{to}'; out(habitWindows())")

    def test_baseline_is_the_equal_run_of_days_before_the_range(self):
        w = self._windows("2026-02-01", "2026-02-10")
        self.assertEqual(w["recent"], {"from": "2026-02-01", "to": "2026-02-10"})
        self.assertEqual(w["baseline"], {"from": "2026-01-22", "to": "2026-01-31"})

    def test_one_day_range_has_a_one_day_baseline(self):
        w = self._windows(DAY, DAY)
        self.assertEqual(w["baseline"], {"from": "2026-02-09", "to": "2026-02-09"})
        text = run_js(DASHBOARD, _dashboard(sessions=[_session("s1")], usage=[_usage(DAY, "s1", 5.0)]) +
                      "renderHabitStrip(); out(el('habitsWindow').textContent)")
        self.assertIn("against the 1 day before", text)

    def test_window_text_names_the_range_and_the_period_before(self):
        text = run_js(DASHBOARD, _dashboard(sessions=[_session("s1")], usage=[_usage(DAY, "s1", 5.0)]) +
                      "state.from = '2026-02-01'; renderHabitStrip(); out(el('habitsWindow').textContent)")
        self.assertNotIn("Last 7 days", text)
        self.assertIn("against the 10 days before", text)
        self.assertIn(" – ", text)

    def test_empty_strip_says_no_activity_in_these_dates(self):
        html = run_js(DASHBOARD, _dashboard() + "renderHabitStrip(); out(el('habitsStrip').innerHTML)")
        self.assertIn("No activity in these dates", html)
        self.assertNotIn("last 7 days", html)

    def _reconcile(self, sessions):
        cli = [{"model": "m", "est_cost": 1, "est_tokens": 10, "cli_cost": 1, "cli_tokens": 10}]
        for s in sessions:
            s["cli_models"] = cli
        return run_js(DASHBOARD, _dashboard(sessions=sessions) +
                      "state.models = []; out(reconciliationRows())")

    def test_reconciliation_leaves_out_a_session_that_began_before_the_range(self):
        before = _session("s0", day="2026-02-09")
        before["end"] = f"{DAY}T11:00:00"           # runs into the range, started before it
        inside = _session("s1")
        rows = self._reconcile([before, inside])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessions"], 1)

    def test_reconciliation_leaves_out_a_session_that_ends_after_the_range(self):
        after = _session("s0")
        after["end"] = "2026-02-11T01:00:00"
        self.assertEqual(self._reconcile([after]), [])

    def _allowance(self, start, end):
        a = {"week_start": start, "week_end": end, "used_usd": 20, "allowance_est_usd": 100, "used_pct": 0.2,
             "status": "ok", "projected_hit_at": None, "calibrated_from": "2026-01-01T12:00:00Z"}
        return run_js(DASHBOARD, _dashboard() + f"PAYLOAD.allowance = {json.dumps(a)};"
                      "renderAllowance(); out(el('allowanceWrap').innerHTML)")

    def test_allowance_outside_the_range_shows_a_note_and_no_meter(self):
        html = self._allowance("2026-01-12T12:00:00Z", "2026-01-19T12:00:00Z")
        self.assertIn("outside the selected dates", html)
        self.assertNotIn("meter", html)

    def test_allowance_overlapping_the_range_shows_the_meter(self):
        html = self._allowance("2026-02-07T12:00:00Z", "2026-02-14T12:00:00Z")
        self.assertIn("meter", html)
        self.assertNotIn("outside the selected dates", html)


class TestSessionSortIsATotalOrder(unittest.TestCase):
    """A session with no value for the sort column (a null CLI cost, say) sorts
    after every session that has one, in either direction, and the order never
    depends on the order the rows arrived in. Comparing null with a string used
    to return NaN, which leaves sort() free to order them any way."""

    def _sorted(self, values, key, direction):
        rows = [{"id": i, key: v} for i, v in enumerate(values)]
        return run_js(DASHBOARD, f"sessionSort = {{key: {json.dumps(key)}, dir: {direction}}};"
                      f"out(sortSessions({json.dumps(rows)}).map(r => r[{json.dumps(key)}]))")

    def test_strings_with_a_null_sort_the_same_from_any_input_order(self):
        for direction, expected in ((1, ["a", "b", None]), (-1, ["b", "a", None])):
            for values in ([None, "a", "b"], ["b", None, "a"], ["a", "b", None]):
                self.assertEqual(self._sorted(values, "title", direction), expected,
                                 (direction, values))

    def test_numbers_with_a_null_sort_the_same_from_any_input_order(self):
        for direction, expected in ((1, [1, 2, 10, None]), (-1, [10, 2, 1, None])):
            for values in ([None, 10, 1, 2], [2, None, 10, 1], [10, 2, 1, None]):
                self.assertEqual(self._sorted(values, "requests", direction), expected,
                                 (direction, values))

    def test_a_missing_field_counts_as_null(self):
        got = run_js(DASHBOARD, "sessionSort = {key: 'title', dir: 1};"
                     "out(sortSessions([{id: 1}, {id: 2, title: 'a'}]).map(r => r.id))")
        self.assertEqual(got, [2, 1])


class TestTeamContextAt500k(unittest.TestCase):
    def test_flag_uses_the_500k_share(self):
        html = _habits_html([_health_row("p0", very_long_context_cost_usd=5.0, long_context_cost_usd=40.0)])
        cells = _cells(html, 'data-person="p0"')
        self.assertIn("badge warn", cells["context"])       # 5 / 50 main = 10%
        self.assertNotIn("badge", cells["context200"])      # 80%, shown but not flagged

    def test_old_export_shows_dash_not_zero(self):
        html = _habits_html([_health_row("p0", very_long_context_cost_usd=None)])
        cells = _cells(html, 'data-person="p0"')
        self.assertIn("—", cells["context"])
        self.assertIn("re-export", html)


class TestContextTaxIgnoresUnmeasuredWeeks(unittest.TestCase):
    """A person with a pre-schema-10 week (null) and a measured one: the
    share is of the measured week's main spend, like the Overview finding."""

    def test_null_weeks_stay_out_of_the_denominator(self):
        rows = [_health_row("p0", week="2026-02-02", very_long_context_cost_usd=None, est_cost_main_usd=100.0),
                _health_row("p0", week="2026-02-09", very_long_context_cost_usd=15.0, est_cost_main_usd=100.0)]
        cell = _person(_habits_html(rows), "p0")["context"]
        self.assertIn("15%", cell)
        self.assertIn("badge warn", cell)

    def test_the_table_and_the_overview_finding_agree(self):
        rows = [_health_row("p0", week="2026-02-02", very_long_context_cost_usd=None, est_cost_main_usd=100.0),
                _health_row("p0", week="2026-02-09", very_long_context_cost_usd=15.0, est_cost_main_usd=100.0)]
        res = run_js(TEAM, _team(rows) + "PAYLOAD.weekly = []; PAYLOAD.limits = []; PAYLOAD.sources = [];"
                     "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertIn("(15%)", res)

    def test_a_person_with_no_measured_week_is_blank(self):
        rows = [_health_row("p0", week="2026-02-02", very_long_context_cost_usd=None, est_cost_main_usd=100.0)]
        self.assertIn("—", _person(_habits_html(rows), "p0")["context"])



class TestDashTooltips(unittest.TestCase):
    """Only the context tax column is blank because of an older export; any
    other dash means there was nothing to measure, and re-exporting won't help."""

    def test_only_context_tax_suggests_re_exporting(self):
        html = _habits_html([_health_row("p0", very_long_context_cost_usd=None, sessions_edited=0)])
        row = html.split('data-person="p0"')[1].split("</tr>")[0]
        context = row.split('<td class="num"')[1]
        checked = row.split('<td class="num"')[4]
        self.assertIn("re-export", context)
        self.assertIn("—", checked)
        self.assertNotIn("re-export", checked)
        self.assertIn("Nothing to measure", checked)

if __name__ == "__main__":
    unittest.main()
