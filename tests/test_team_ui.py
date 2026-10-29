"""The team dashboard's tiles, run-rate and People table (static/team.js), run
under node against a stub DOM (tests/jsrun.py)."""
import json
import re
import unittest
from pathlib import Path

from tests.jsrun import run_js

REPO = Path(__file__).resolve().parent.parent
TEAM = ["static/team-findings.js", "static/team.js"]
DASHBOARD = ["static/habits.js", "static/app.js"]  # app.js reads habits.js at load


def _setup(**payload):
    base = {"people": [], "weekly": [], "limits": [], "models": [], "prs": [], "tools": [],
            "sources": [], "warnings": [], "prices": None, "forecast": None, "health": {"rows": []}}
    base.update(payload)
    return (f"PAYLOAD = {json.dumps(base)}; state.people = [];"
            "state.from = '2026-08-31'; state.to = '2026-09-28';")


def _tiles(**payload):
    html = run_js(TEAM, _setup(**payload) + "renderTiles(); out(el('teamTiles').innerHTML);")
    return dict(re.findall(r'<div class="label">(.*?)</div><div class="value">.*?</div>'
                           r'<div class="sub">(.*?)</div>', html))


WINDOW = {"window_from": "2026-08-31", "window_to": "2026-09-27"}


class TestPeopleTile(unittest.TestCase):
    ROWS = [["a", "2026-09-14", "r", 5.0, 0, 1, 1, 1, False, False, None, None, 0.0, 1, 7, 0.0, 0, 1]]

    def test_a_person_with_usage_in_the_range_is_active(self):
        people = [dict(WINDOW, person="a"), dict(WINDOW, person="b")]
        self.assertEqual(_tiles(people=people, weekly=self.ROWS)["People"],
                         "1 active · 1 no activity in these weeks")

    def test_a_person_the_range_does_not_reach_is_not_covered(self):
        people = [dict(WINDOW, person="a"), {"person": "b", "window_from": "2026-01-05", "window_to": "2026-01-25"}]
        self.assertEqual(_tiles(people=people, weekly=self.ROWS)["People"],
                         "1 active · 0 no activity in these weeks · 1 not covered by these weeks")

    def test_nobody_idle(self):
        self.assertEqual(_tiles(people=[dict(WINDOW, person="a")], weekly=self.ROWS)["People"],
                         "1 active · 0 no activity in these weeks")


class TestRangeFigureWording(unittest.TestCase):
    """Seat use, value per seat and the People table follow the selected
    weeks, and the page says so."""

    def test_tiles_say_they_cover_the_selected_weeks(self):
        tiles = _tiles(people=[{"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-30"}],
                       prices={"source": "seat-prices.json", "per": "month"})
        self.assertIn("selected weeks", tiles["Median seat use"])
        self.assertIn("selected weeks", tiles["Value per seat"])
        self.assertNotIn("whole export window", tiles["Median seat use"] + tiles["Value per seat"])

    def test_value_tile_headlines_the_team_ratio_and_gives_the_median_below(self):
        window = {"window_from": "2026-08-31", "window_to": "2026-09-27", "seat_price": 200}
        people = [dict(window, person=p) for p in ("a", "b", "c")]
        weekly = [_row18("a", "2026-09-14", "r", 300.0), _row18("b", "2026-09-14", "r", 10.0),
                  _row18("c", "2026-09-14", "r", 20.0)]
        snippet = (_setup(people=people, weekly=weekly, prices={"source": "seat-prices.json", "per": "month"}) +
                   "state.from = '2026-09-14'; state.to = '2026-09-14'; renderTiles(); out(el('teamTiles').innerHTML);")
        html = run_js(TEAM, snippet)
        value, sub = re.search(r'<div class="label">Value per seat</div><div class="value">(.*?)</div>'
                               r'<div class="sub">(.*?)</div>', html).groups()
        seat = 200 / (365.25 / 12) * 7  # one week of a monthly seat
        self.assertEqual(value, f"{330 / (3 * seat):.1f}×")  # 2.4×: the team's total, not the median
        self.assertIn(f"median seat {20 / seat:.1f}×", sub)  # 0.4×
        self.assertIn("team", sub)
        self.assertIn("selected weeks", sub)

    def test_people_table_says_it_follows_the_selected_weeks(self):
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        note = html.split('id="peopleTable"')[1].split('</p>')[0]
        self.assertIn("selected weeks", note)
        self.assertNotIn("whole export window", note)
        self.assertNotIn("doesn&rsquo;t", note)
        value = html.split('id="valueTable"')[1].split('</p>')[0]
        self.assertIn("selected weeks", value)
        self.assertNotIn("not the selected weeks", value)


class TestRunRate(unittest.TestCase):
    """The run-rate is computed in the browser for the selected people and
    weeks (rangeRunRate), by the rule of the server's team.overage_forecast:
    complete weeks the selected exports cover, not every week before this one."""

    # a's export ends on Thursday Oct 1, so the week of Sep 28 is part-seen.
    PEOPLE = [{"person": "a", "window_from": "2026-08-31", "window_to": "2026-10-01"},
              {"person": "b", "window_from": "2026-08-31", "window_to": "2026-10-01"}]
    LIMITS = [{"person": "a", "limit_type": "weekly", "hit_at": f"{day}T10:00:00",
               "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": usd}
              for day, usd in (("2026-09-01", 40), ("2026-09-08", 60), ("2026-09-15", 50),
                               ("2026-09-22", 50), ("2026-10-01", 900))]
    # Another person's episodes: only counted while b is selected.
    OTHER = [{"person": "b", "limit_type": "weekly", "hit_at": "2026-09-16T10:00:00",
              "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": 400}]
    WEEKLY = [["a", w, "p", 1, 1, 1, 1, 1, False, False]
              for w in ("2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28")]

    def _render(self, selected=(), limits=None, people=None, extra=""):
        snippet = (_setup(people=people or self.PEOPLE, limits=limits or self.LIMITS + self.OTHER, weekly=self.WEEKLY)
                   + "todayUTC = () => '2026-10-15';" + f"state.people = {json.dumps(list(selected))};" + extra
                   + "renderOverage(); out({note: el('forecastNote').textContent, charts: charts_made});")
        return run_js(TEAM, snippet)

    def _line(self, got):
        return next((d for d in got["charts"][-1]["data"]["datasets"] if d["label"] == "Run-rate"), None)

    def test_the_line_and_note_are_for_the_selected_people_and_weeks(self):
        got = self._render(selected=["a"])
        self.assertIn("$50.00/week", got["note"])      # (40 + 60 + 50 + 50) / 4; the Oct 1 hit is in a part week
        self.assertIn("4 complete week", got["note"])
        self.assertIn("4 episodes", got["note"])
        self.assertIn("$200", got["note"])
        self.assertIn("$650", got["note"])
        self.assertIn("for the selected people and weeks", got["note"])
        self.assertNotIn("everyone in the exports", got["note"])
        self.assertEqual(set(self._line(got)["data"]), {50.0})

    def test_another_person_changes_the_run_rate_only_when_selected(self):
        self.assertIn("$50.00/week", self._render(selected=["a"])["note"])
        # Everyone: (40 + 60 + (50 + 400) + 50) / 4.
        self.assertIn("$150.00/week", self._render()["note"])
        # Only b, with a second episode of 400 in another week: (400 + 400) / 4.
        self.assertIn("$200.00/week", self._render(selected=["b"], limits=self.LIMITS + self.OTHER
                      + [dict(self.OTHER[0], hit_at="2026-09-09T10:00:00")])["note"])

    def test_a_narrower_range_changes_the_run_rate(self):
        got = self._render(selected=["a"], extra="state.from = '2026-09-14';")
        self.assertIn("2 complete week", got["note"])
        self.assertIn("$50.00/week", got["note"])
        self.assertIn("2 episodes", got["note"])

    def test_a_range_with_no_complete_week_says_so(self):
        got = self._render(selected=["a"], extra="state.from = '2026-09-28'; state.to = '2026-09-28';")
        self.assertIn("No complete weeks", got["note"])
        self.assertIsNone(self._line(got))

    def test_weeks_before_the_selected_exports_begin_are_not_zero_weeks(self):
        people = [{"person": "a", "window_from": "2026-09-14", "window_to": "2026-10-01"},
                  {"person": "b", "window_from": "2026-08-31", "window_to": "2026-10-01"}]
        limits = [dict(self.LIMITS[0], hit_at="2026-09-15T10:00:00", usage_after_hit_usd=60),
                  dict(self.LIMITS[0], hit_at="2026-09-22T10:00:00", usage_after_hit_usd=30)]
        got = self._render(selected=["a"], people=people, limits=limits)
        self.assertIn("$45.00/week", got["note"])      # (60 + 30) / 2, not / 4
        self.assertIn("2 complete week", got["note"])
        bars = got["charts"][-1]["data"]["datasets"][0]["data"]
        self.assertEqual(bars[:2], [None, None])       # Aug 31 and Sep 7: unknown, not $0

    def test_people_without_an_export_window_have_no_complete_week(self):
        got = self._render(people=[{"person": "a"}, {"person": "b"}])
        self.assertIn("No complete weeks", got["note"])


class TestRunRateHonesty(unittest.TestCase):
    def _note(self, hits, people=None):
        limits = [{"person": "a@x.com", "limit_type": "weekly", "hit_at": f"{day}T10:00:00Z",
                   "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": usd} for day, usd in hits]
        people = people or [dict(p, window_from="2026-08-31", window_to="2026-09-27") for p in PEOPLE]
        weekly = [["a@x.com", w, "p", 1, 1, 1, 1, 1, False, False]
                  for w in ("2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28")]
        return run_js(TEAM, _setup(people=people, limits=limits, weekly=weekly) + "todayUTC = () => '2026-10-15';" +
                      "renderOverage(); out({note: el('forecastNote').textContent, charts: charts_made});")

    def test_one_episode_is_not_projected(self):
        res = self._note([("2026-09-15", 15.51)])
        self.assertIn("One episode so far", res["note"])
        self.assertNotIn("$50", res["note"])
        self.assertEqual([d["label"] for d in res["charts"][-1]["data"]["datasets"]], ["Extra usage"])

    def test_the_gate_and_the_count_are_the_basis_episodes(self):
        # Older weekly episodes sit before the selected weeks: they must not count.
        old = [("2026-07-07", 20), ("2026-07-14", 20), ("2026-07-21", 20)]
        self.assertIn("One episode so far", self._note(old + [("2026-09-15", 20)])["note"])
        res = self._note(old + [("2026-09-01", 20), ("2026-09-08", 20), ("2026-09-15", 20)])
        self.assertIn("from 3 episodes", res["note"])
        self.assertIn("$15.00/week", res["note"])       # 60 over the 4 basis weeks
        self.assertIn("Run-rate", [d["label"] for d in res["charts"][-1]["data"]["datasets"]])
        self.assertIn("No extra usage so far", self._note(old)["note"])


class TestNumberFormatters(unittest.TestCase):
    """The shared formatters: never "$∞" or "NaN%", round before choosing a
    unit, and never show a negative zero. Same behaviour on both pages."""

    @staticmethod
    def _fmt(files, expr):
        return run_js(files, f"out({expr})")

    def _both(self, expr, expected):
        for files in (DASHBOARD, TEAM):
            self.assertEqual(self._fmt(files, expr), expected, f"{files[0]}: {expr}")

    def test_rounding_up_to_the_next_unit_is_made_before_the_unit_is_chosen(self):
        self._both("fmtTokens(999950)", "1.0M")
        self._both("fmtTokens(999949)", "999.9k")
        self._both("fmtTokens(999.6)", "1.0k")
        self.assertEqual(self._fmt(TEAM, "fmtTokens(999999999)"), "1.0B")
        self.assertEqual(self._fmt(TEAM, "fmtTokens(999949999)"), "999.9M")

    def test_ordinary_values_are_unchanged(self):
        self._both("fmtTokens(0)", "0")
        self._both("fmtTokens(null)", "0")
        self._both("fmtTokens(999)", "999")
        self._both("fmtTokens(1500)", "1.5k")
        self.assertEqual(self._fmt(DASHBOARD, "fmtTokens(2500000)"), "2.50M")
        self.assertEqual(self._fmt(TEAM, "fmtTokens(2500000)"), "2.5M")
        self.assertEqual(self._fmt(TEAM, "fmtTokens(2500000000)"), "2.50B")
        self._both("fmtCost(1234.5)", "$1,234.50")
        self._both("fmtCost(null)", "$0.00")
        self._both("fmtInt(1234567)", "1,234,567")

    def test_values_that_are_not_finite_are_a_dash(self):
        for expr in ("fmtCost(Infinity)", "fmtCost(-Infinity)", "fmtCost(NaN)", "fmtTokens(Infinity)",
                     "fmtTokens(NaN)", "fmtInt(Infinity)", "fmtPct(Infinity)", "fmtPct(NaN)",
                     "fmtPct(null)"):
            self._both(expr, "—")
        for expr in ("fmtCost0(Infinity)", "fmtCost0(NaN)"):
            self._both(expr, "—")

    def test_negative_zero_is_zero(self):
        self._both("fmtCost(-0.001)", "$0.00")
        self._both("fmtInt(-0.4)", "0")
        self._both("fmtTokens(-0.4)", "0")
        self._both("fmtPct(-0.00001)", "0.0%")
        self._both("fmtCost0(-0.4)", "$0")

    def test_percentages_keep_their_precision(self):
        self.assertEqual(self._fmt(DASHBOARD, "fmtPct(0.1234)"), "12.3%")
        self.assertEqual(self._fmt(TEAM, "fmtPct(0.1234)"), "12%")
        self.assertEqual(self._fmt(TEAM, "fmtPct(0.0456)"), "4.6%")


class TestHeatmapLayout(unittest.TestCase):
    """The personal dashboard's heatmap is a grid of divs; the team one is a
    table. A grid rule that matches the table splits its header row from its
    body, so the week headings drift off their columns."""

    def test_grid_rule_does_not_match_the_team_table(self):
        css = (REPO / "static" / "app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # a leading comment would defeat fullmatch
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if "display: grid" in body or "display:grid" in body:
                for part in selector.split(","):
                    part = part.strip()
                    self.assertFalse(re.fullmatch(r"\.heatmap", part),
                                     f"'{part}' also matches table.heatmap")

    def test_the_div_heatmap_keeps_its_grid_and_no_rule_leaks_onto_the_table(self):
        css = (REPO / "static" / "app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
        grid = [body for sel, body in rules if "div.heatmap" in [p.strip() for p in sel.split(",")]]
        self.assertTrue(any(re.search(r"display:\s*grid", b) for b in grid), "div.heatmap lost its grid")
        for sel, _ in rules:
            for part in sel.split(","):
                part = part.strip()
                # A rule starting at a bare .heatmap reaches table.heatmap and its cells too.
                self.assertFalse(re.match(r"\.heatmap\b", part), f"'{part}' also matches table.heatmap")


class TestTabs(unittest.TestCase):
    PANELS = ["overview", "spend", "seats", "habits", "output", "data"]

    def test_page_has_a_panel_per_tab(self):
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        for tab in self.PANELS:
            self.assertIn(f'id="tab-{tab}"', html)
            self.assertIn(f'data-tab="{tab}"', html)
        self.assertLess(html.index("static/team-findings.js"), html.index("static/team.js"))

    def test_cards_sit_in_their_tabs(self):
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        def panel(tab):
            return html.split(f'id="tab-{tab}"')[1].split('class="tabpanel')[0]
        self.assertIn('id="heatmap"', panel("seats"))
        self.assertIn('id="limitsTable"', panel("seats"))
        self.assertIn('id="prTable"', panel("output"))
        self.assertIn('id="teamHabits"', panel("habits"))
        self.assertIn('id="frictionTable"', panel("habits"))
        self.assertIn('id="peopleTable"', panel("data"))
        self.assertIn('id="mixChart"', panel("spend"))
        self.assertIn('id="teamTiles"', panel("overview"))

    def test_switching_renders_only_the_active_tab(self):
        snippet = (_setup(people=[{"person": "a"}]) +
                   "const calls = []; Object.keys(RENDERERS).forEach(k => { RENDERERS[k] = () => calls.push(k); });"
                   "showTab('spend'); showTab('nonsense'); out({calls, tab: currentTab});")
        res = run_js(TEAM, snippet)
        self.assertEqual(res, {"calls": ["spend", "overview"], "tab": "overview"})


PEOPLE = [{"person": "a@x.com", "display_name": "Ann <b>A</b>"}, {"person": "b@x.com", "display_name": "Bo"}]
# The same people with an export window: the model and repository trends leave a week no
# selected export touches blank, so their tests need the weeks to be covered.
PEOPLE_COVERED = [dict(p, window_from="2026-08-31", window_to="2026-09-27") for p in PEOPLE]


class TestNamesNotCounts(unittest.TestCase):
    def test_pr_table_names_people_with_counts(self):
        prs = [{"person": "a@x.com", "pr_repo": "gh/r", "opened_on": "2026-09-15", "in_project_repo": "yes",
                "session_cost_usd": 10, "session_prs": 1},
               {"person": "a@x.com", "pr_repo": "gh/r", "opened_on": "2026-09-16", "in_project_repo": "yes",
                "session_cost_usd": 10, "session_prs": 1},
               {"person": "b@x.com", "pr_repo": "gh/r", "opened_on": "2026-09-16", "in_project_repo": "no",
                "session_cost_usd": 10, "session_prs": 1}]
        html = run_js(TEAM, _setup(people=PEOPLE, prs=prs) + "renderPrs(); out(el('#prTable tbody').innerHTML);")
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt; (2)", html)
        self.assertIn("Bo (1)", html)

    def test_names_are_escaped(self):
        tools = [["a@x.com", "2026-09-14", "x", "srv", 30, 9]]
        html = run_js(TEAM, _setup(people=PEOPLE, tools=tools) +
                      "renderFriction(); out(el('#frictionTable tbody').innerHTML);")
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt;", html)
        self.assertNotIn("<b>A</b>", html)

    def test_claude_code_tools_are_split_out(self):
        tools = [["a@x.com", "2026-09-14", "TaskStop", "built-in", 30, 9],
                 ["a@x.com", "2026-09-14", "Bash", "built-in", 30, 3]]
        res = run_js(TEAM, _setup(people=PEOPLE, tools=tools) +
                     "renderFriction(); out({main: el('#frictionTable tbody').innerHTML,"
                     " cc: el('#ccToolsTable tbody').innerHTML});")
        self.assertIn("Bash", res["main"])
        self.assertNotIn("TaskStop", res["main"])
        self.assertIn("TaskStop", res["cc"])


class TestLabelsOnThePage(unittest.TestCase):
    def test_one_person_is_singular(self):
        limits = [{"person": "a@x.com", "hit_at": "2026-09-15T10:00:00Z", "limit_type": "weekly",
                   "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": 1}]
        self.assertEqual(_tiles(people=PEOPLE, limits=limits)["Limit hits"], "1 person")

    def test_limits_table_uses_tier_labels(self):
        people = [{"person": "a@x.com", "display_name": "Ann", "rate_limit_tier": "default_claude_max_5x"}]
        html = run_js(TEAM, _setup(people=people) + "renderLimits(); out(el('#limitsTable tbody').innerHTML);")
        self.assertIn("Max 5×", html)
        self.assertNotIn("default_claude_max_5x", html)


class TestHeatmapDays(unittest.TestCase):
    def test_active_days_are_capped_at_the_week(self):
        weekly = [["a@x.com", "2026-09-14", "r1", 5, 0, 1, 1, 5, False, False, None, None, 0, 1, 7],
                  ["a@x.com", "2026-09-14", "r2", 5, 0, 1, 1, 5, False, False, None, None, 0, 1, 7]]
        html = run_js(TEAM, _setup(people=PEOPLE, weekly=weekly) +
                      "state.metric = 'days'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn(">≈7</td>", html)  # estimated days carry "≈" (P3 SP-15)
        self.assertNotIn(">10</td>", html)


WEEKLY = [["a@x.com", "2026-09-14", "gh/big", 300.0, 0, 10, 1, 5, False, False, 900.0, 100.0, 200.0, 2, 7],
          ["b@x.com", "2026-09-14", "gh/small", 100.0, 0, 5, 1, 5, False, False, None, None, 0.0, 1, 7],
          ["b@x.com", "2026-09-21", "gh/big", 50.0, 0, 5, 1, 2, False, False, None, None, 0.0, 1, 7]]


def _render(fn, target, **payload):
    return run_js(TEAM, _setup(people=PEOPLE, **payload) + f"{fn}(); out(el('{target}').innerHTML);")


class TestNewViews(unittest.TestCase):
    def test_repo_table_shares_and_people(self):
        html = _render("renderRepoSpend", "#repoTable tbody", weekly=WEEKLY)
        self.assertLess(html.index("gh/big"), html.index("gh/small"))
        self.assertIn("78%", html)                      # 350 of 450
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt;", html)
        self.assertIn("Bo", html)

    def test_per_day_table_marks_the_benchmark(self):
        html = _render("renderPerDay", "#perDayTable tbody", weekly=WEEKLY)
        self.assertIn("$60", html)                      # Ann: 300 / 5 days
        self.assertIn("above 90th percentile", html)

    def test_output_table(self):
        prs = [{"person": "a@x.com", "pr_repo": "gh/big", "opened_on": "2026-09-15", "in_project_repo": "yes",
                "session_cost_usd": 40.0, "session_prs": 2}]
        html = _render("renderOutput", "#outputTable tbody", weekly=WEEKLY, prs=prs)
        self.assertIn("$20", html)                      # cost per PR
        self.assertIn("900", html)                      # lines added
        self.assertIn("$200", html)                     # 200 / 1000 lines * 1000

    def test_findings_link_to_tabs(self):
        html = _render("renderFindings", "findingsList", weekly=WEEKLY)
        self.assertIn('href="#spend"', html)
        self.assertIn("78% of spend", html)

    def test_findings_are_escaped(self):
        limits = [{"person": "a@x.com", "hit_at": "2026-09-15T10:00:00Z", "limit_type": "spend_cap",
                   "locked_out": True, "blocked_hours": 3, "usage_after_hit_usd": 0}]
        html = _render("renderFindings", "findingsList", limits=limits)
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt;", html)
        self.assertNotIn("<b>A</b>", html)

    def test_freshness_names_zone_and_flags_stale(self):
        sources = [{"person": "a@x.com", "source": "a.zip", "exported_at": "2020-01-01T00:00:00+00:00",
                    "window_from": "2019-12-01", "window_to": "2020-01-01", "bucket_zone": "UTC",
                    "schema_version": 10, "machine_id": "m", "files": ["summary"]}]
        html = _render("renderFreshness", "freshness", sources=sources)
        self.assertIn("UTC", html)
        self.assertIn("badge warn", html)

    def test_trend_stacks_people_and_counts_active(self):
        res = run_js(TEAM, _setup(people=PEOPLE, weekly=WEEKLY) +
                     "state.from = '2026-09-14'; state.to = '2026-09-21'; renderTrend(); out(charts_made[charts_made.length - 1]);")
        labels = [d["label"] for d in res["data"]["datasets"]]
        self.assertIn("Active people", labels)
        active = next(d for d in res["data"]["datasets"] if d["label"] == "Active people")
        self.assertEqual(active["data"], [2, 1])

    def test_repo_trend_has_a_series_per_repo_by_week(self):
        res = run_js(TEAM, _setup(people=PEOPLE_COVERED, weekly=WEEKLY) +
                     "state.from = '2026-09-14'; state.to = '2026-09-21'; renderRepoTrend();"
                     "out(charts_made[charts_made.length - 1]);")
        series = {d["label"]: d["data"] for d in res["data"]["datasets"]}
        self.assertEqual(series, {"gh/big": [300.0, 50.0], "gh/small": [100.0, 0]})

    def test_value_per_seat_card(self):
        people = [{"person": "a@x.com", "display_name": "Ann", "rate_limit_tier": "default_claude_max_5x",
                   "seat_price": 100.0, "window_from": "2026-09-01", "window_to": "2026-09-28"}]
        weekly = [["a@x.com", "2026-09-14", "r", 400.0, 0, 1, 1, 3, False, False, None, None, 0.0, 1, 7]]
        html = run_js(TEAM, _setup(people=people, weekly=weekly, prices={"source": "seat-prices.json", "per": "month"}) +
                      "renderValue(); out(el('#valueTable tbody').innerHTML);")
        self.assertIn("4.3×", html)  # the 28-day window: 400 ÷ (100 ÷ 30.4375 × 28)
        self.assertIn("Max 5×", html)
        html = run_js(TEAM, _setup(people=people) + "renderValue(); out(el('#valueTable tbody').innerHTML);")
        self.assertIn("seat-prices.json", html)

    def test_empty_selection_renders_empty_states(self):
        snippet = (_setup(people=PEOPLE, weekly=WEEKLY) + "state.people = ['nobody'];"
                   "renderFindings(); renderRepoSpend(); renderPerDay(); renderOutput();"
                   "out({f: el('findingsList').innerHTML, r: el('#repoTable tbody').innerHTML,"
                   " d: el('#perDayTable tbody').innerHTML, o: el('#outputTable tbody').innerHTML});")
        res = run_js(TEAM, snippet)
        self.assertIn("Nothing stands out", res["f"])
        for key in ("r", "d", "o"):
            self.assertIn("empty-state", res[key])


class TestHostileNames(unittest.TestCase):
    """Repos, people and tools named like Object.prototype members are ordinary
    names; the page must still load (renderAll runs on the Overview tab)."""

    NAMES = ["toString", "constructor", "valueOf", "hasOwnProperty", "__proto__"]

    def _data(self):
        people = [{"person": n, "display_name": n.upper()} for n in self.NAMES]
        weekly = [[n, "2026-09-14", r, 10.0, 0, 1, 1, 2, False, False, 5.0, 1.0, 5.0, 1, 7]
                  for n in self.NAMES for r in self.NAMES]
        prs = [{"person": n, "pr_repo": n, "opened_on": "2026-09-15", "in_project_repo": "no",
                "session_cost_usd": 10.0, "session_prs": 1} for n in self.NAMES]
        tools = [[n, "2026-09-14", n, "built-in", 30, 9] for n in self.NAMES]
        sources = [{"person": n, "source": "x.zip", "exported_at": "2026-10-13T00:00:00+00:00",
                    "machine_id": n, "files": []} for n in self.NAMES]
        limits = [{"person": n, "hit_at": "2026-09-15T10:00:00", "limit_type": "weekly",
                   "locked_out": True, "blocked_hours": 2, "usage_after_hit_usd": 1} for n in self.NAMES]
        return dict(people=people, weekly=weekly, prs=prs, tools=tools, sources=sources, limits=limits)

    def test_every_renderer_survives_hostile_names(self):
        snippet = (_setup(**self._data()) + "state.from = '2026-09-14'; state.to = '2026-09-14';"
                   "Object.keys(RENDERERS).forEach(k => { showTab(k); });"
                   "out({repo: el('#repoTable tbody').innerHTML, pr: el('#prTable tbody').innerHTML,"
                   " limits: el('#limitsTable tbody').innerHTML, fresh: el('freshness').innerHTML});")
        res = run_js(TEAM, snippet)
        for name in self.NAMES:
            self.assertIn(name, res["repo"])
        self.assertEqual(res["pr"].count("<tr>"), 5)
        self.assertEqual(res["limits"].count("<tr>"), 5)
        self.assertEqual(res["fresh"].count("<tr>"), 6)       # header + five machines

    def test_findings_do_not_stop_the_overview(self):
        res = run_js(TEAM, _setup(**self._data()) + "state.from = '2026-09-14'; state.to = '2026-09-14';"
                     "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertIn("TOSTRING", res)
        self.assertNotIn("NaN", res)


class TestFindingsOverflow(unittest.TestCase):
    def test_more_than_six_findings_are_expandable(self):
        items = [{"rule": f"r{i}", "tab": "spend", "text": f"Finding {i} <b>x</b>"} for i in range(8)]
        html = run_js(TEAM, _setup(people=PEOPLE) + f"findings = () => {json.dumps(items)};"
                      "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertEqual(html.count("<li>"), 8)
        details = html.split("<details>")[1].split("</details>")[0]
        self.assertIn("<summary>+2 more</summary>", details)
        self.assertEqual(details.count("<li>"), 2)
        self.assertIn("Finding 7 &lt;b&gt;x&lt;/b&gt;", details)
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn("Finding 6", html.split("<details>")[0])

    def test_six_or_fewer_have_no_details(self):
        items = [{"rule": f"r{i}", "tab": "spend", "text": f"Finding {i}"} for i in range(6)]
        html = run_js(TEAM, _setup(people=PEOPLE) + f"findings = () => {json.dumps(items)};"
                      "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertNotIn("details", html)


class TestFreshnessPerMachine(unittest.TestCase):
    def _sources(self):
        base = {"source": "x.zip", "window_from": "2026-09-01", "window_to": "2026-09-28",
                "bucket_zone": "UTC", "schema_version": 10, "files": []}
        return [dict(base, person="a@x.com", machine_id="A", exported_at="2099-01-01T00:00:00+00:00"),
                dict(base, person="a@x.com", machine_id="B", exported_at="2020-01-01T00:00:00+00:00"),
                dict(base, person="b@x.com", machine_id="C", exported_at="2099-01-01T00:00:00+00:00")]

    def test_a_stale_second_machine_gets_its_own_row_and_badge(self):
        html = _render("renderFreshness", "freshness", sources=self._sources())
        self.assertEqual(html.count("<tr><td>"), 3)
        self.assertEqual(html.count("badge warn"), 1)
        self.assertIn("Bucketed in", html)
        self.assertNotIn("Days and weeks in", html)

    def test_badge_and_finding_agree(self):
        res = run_js(TEAM, _setup(people=PEOPLE, weekly=WEEKLY, sources=self._sources()) +
                     "renderFreshness(); renderFindings();"
                     "out({fresh: el('freshness').innerHTML, f: el('findingsList').innerHTML});")
        self.assertIn("badge warn", res["fresh"])
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt;", res["f"])
        self.assertNotIn("Bo", res["f"])


class TestHeatmapExactDays(unittest.TestCase):
    def test_exact_days_win_over_summed_project_days(self):
        weekly = [_row18("a@x.com", "2026-09-14", "r1", 5, days=5, exact=3),
                  _row18("a@x.com", "2026-09-14", "r2", 5, days=5, exact=3)]
        html = run_js(TEAM, _setup(people=PEOPLE, weekly=weekly) +
                      "state.metric = 'days'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn(">3</td>", html)
        self.assertNotIn(">7</td>", html)


class TestHeatmapCapIsTheWidestWindow(unittest.TestCase):
    def test_a_narrow_window_on_one_machine_does_not_cap_the_week(self):
        weekly = [["a@x.com", "2026-09-14", "r1", 5, 0, 1, 1, 2, False, False, None, None, 0, 1, 2],
                  ["a@x.com", "2026-09-14", "r2", 5, 0, 1, 1, 3, False, False, None, None, 0, 1, 7]]
        html = run_js(TEAM, _setup(people=PEOPLE, weekly=weekly) +
                      "state.metric = 'days'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn(">≈5</td>", html)

    def test_a_part_week_everywhere_still_caps(self):
        weekly = [["a@x.com", "2026-09-14", "r1", 5, 0, 1, 1, 3, False, False, None, None, 0, 1, 3],
                  ["a@x.com", "2026-09-14", "r2", 5, 0, 1, 1, 2, False, False, None, None, 0, 1, 3]]
        html = run_js(TEAM, _setup(people=PEOPLE, weekly=weekly) +
                      "state.metric = 'days'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn(">≈3</td>", html)
        self.assertNotIn(">≈5</td>", html)



def _row(person, week, repo, cost, days=1, lines=None, removed=None, with_lines=0.0, wdays=7):
    return [person, week, repo, cost, 0, 1, 1, days, False, False, lines, removed, with_lines, 1, wdays]


def _row18(person, week, repo, cost, days=1, exact=None, sessions=1, sub=0.0, retries=0, wdays=7):
    """An 18-field weekly row (team.team_payload's layout)."""
    return [person, week, repo, cost, 0, 1, 1, days, False, False, None, None, 0.0, sessions, wdays,
            sub, retries, exact]


class TestPolishedViews(unittest.TestCase):
    """Deferred review items on the rendered page."""

    def test_repo_table_leaves_out_near_zero_people(self):
        weekly = [_row("a@x.com", "2026-09-14", "gh/r", 100.0), _row("b@x.com", "2026-09-14", "gh/r", 0.2)]
        html = _render("renderRepoSpend", "#repoTable tbody", weekly=weekly)
        self.assertIn("Ann &lt;b&gt;A&lt;/b&gt; $100", html)
        self.assertNotIn("Bo", html)

    def test_repo_table_names_the_other_bucket(self):
        weekly = [_row("a@x.com", "2026-09-14", f"gh/r{i}", 100.0 - i) for i in range(10)]
        html = _render("renderRepoSpend", "#repoTable tbody", weekly=weekly)
        self.assertIn("Other (2 repos)", html)

    def test_repo_trend_keeps_a_real_repo_named_other_apart(self):
        weekly = [_row("a@x.com", "2026-09-14", f"gh/r{i}", 100.0 - i) for i in range(10)]
        weekly.append(_row("a@x.com", "2026-09-14", "Other", 95.5))
        res = run_js(TEAM, _setup(people=PEOPLE_COVERED, weekly=weekly) +
                     "state.from = '2026-09-14'; state.to = '2026-09-14'; renderRepoTrend();"
                     "out(charts_made[charts_made.length - 1].data.datasets.map(d => [d.label, d.data[0]]));")
        labels = dict(res)
        self.assertEqual(labels["Other"], 95.5)
        self.assertIn("Other (3 repos)", labels)
        self.assertAlmostEqual(labels["Other (3 repos)"], 93 + 92 + 91)

    def test_benchmark_marks_follow_the_benchmark(self):
        html = run_js(TEAM, _setup(people=PEOPLE, weekly=[_row("a@x.com", "2026-09-14", "r", 300.0, days=5)]) +
                      "BENCHMARK.average = 20; BENCHMARK.p90 = 40; renderPerDay(); out(el('#perDayTable tbody').innerHTML);")
        self.assertIn('title="$20 average"', html)
        self.assertIn('title="$40: 90th percentile"', html)

    def test_freshness_badge_matches_the_finding(self):
        sources = [{"person": "a@x.com", "source": "a.zip", "exported_at": "2020-01-01T00:00:00+00:00",
                    "window_from": "2019-12-01", "window_to": "2020-01-01", "bucket_zone": "UTC",
                    "schema_version": 10, "machine_id": "m", "files": ["summary"]}]
        html = _render("renderFreshness", "freshness", sources=sources)
        self.assertRegex(html, r"more than \d+ days old")

    def test_output_row_without_line_data_is_blank(self):
        html = _render("renderOutput", "#outputTable tbody", weekly=[_row("b@x.com", "2026-09-14", "r", 50.0)])
        row = html.split("Bo")[1].split("</tr>")[0]
        cells = re.findall(r'<td class="num">(.*?)</td>', row)
        self.assertEqual(cells[2:5], ["—", "—", "—"])  # lines added, removed, cost per 1k lines

    def test_value_per_seat_blank_where_unknown(self):
        people = [{"person": "a@x.com", "display_name": "Ann", "est_cost_usd": 100.0,
                   "seat_cost_in_window": None, "value_multiple": None, "extra_usage_usd": 0}]
        html = run_js(TEAM, _setup(people=people, prices={"source": "seat-prices.json", "per": "month"}) +
                      "renderValue(); out(el('#valueTable tbody').innerHTML);")
        cells = re.findall(r'<td class="num">(.*?)</td>', html)
        self.assertEqual(cells[1:], ["—", "—", "—"])


class TestTabWiring(unittest.TestCase):
    def test_initial_tab_comes_from_the_hash_and_wiring_happens_once(self):
        snippet = ("window.location = {hash: '#spend'}; const calls = [];"
                   "window.addEventListener = t => calls.push(t);"
                   "el('tabstrip').addEventListener = t => calls.push('strip:' + t);"
                   "initTabs(); initTabs(); out({calls, tab: currentTab});")
        self.assertEqual(run_js(TEAM, _setup() + snippet), {"calls": ["strip:click", "hashchange"], "tab": "spend"})

    def test_unknown_hash_opens_overview(self):
        snippet = "window.location = {hash: '#nope'}; initTabs(); out(currentTab);"
        self.assertEqual(run_js(TEAM, _setup() + snippet), "overview")

    def test_tabs_carry_aria(self):
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        strip = html.split('id="tabstrip"')[1].split("</div>")[0]
        self.assertEqual(strip.count('role="tab"'), 6)
        self.assertEqual(html.count('role="tabpanel"'), 6)
        snippet = ("const btns = ['overview', 'spend'].map(t => ({dataset: {tab: t}, classList: {toggle() {}},"
                   " setAttribute(k, v) { this[k] = v; }}));"
                   "document.querySelectorAll = s => s.includes('button') ? btns : [];"
                   "RENDERERS.spend = () => {}; showTab('spend'); out(btns.map(b => b['aria-selected']));")
        self.assertEqual(run_js(TEAM, _setup() + snippet), ["false", "true"])


def _model_row(person, week, model, cost, out=0):
    family = model.split("-")[1] if model.startswith("claude-") else "other"
    return [person, week, family, "main", cost, out, 0, model]


class TestModelTrend(unittest.TestCase):
    MODELS = [_model_row("a@x.com", "2026-09-14", "claude-opus-5-5", 30.0, 100),
              _model_row("b@x.com", "2026-09-14", "claude-opus-5-5", 10.0, 50),
              _model_row("a@x.com", "2026-09-14", "claude-sonnet-5-5", 5.0, 900),
              _model_row("b@x.com", "2026-09-21", "claude-sonnet-5-5", 7.0, 300)]
    WEEKLY = [_row("a@x.com", "2026-09-14", "r", 35.0), _row("b@x.com", "2026-09-21", "r", 7.0)]

    def _chart(self, metric="cost", people=(), models=None):
        snippet = (_setup(people=PEOPLE_COVERED, weekly=self.WEEKLY, models=models or self.MODELS) +
                   f"state.people = {json.dumps(list(people))}; state.modelMetric = '{metric}';"
                   "state.from = '2026-09-14'; state.to = '2026-09-21'; renderModelTrend();"
                   "const c = charts_made[charts_made.length - 1];"
                   "out(c.data.datasets.map(d => [d.label, d.data]));")
        return dict(run_js(TEAM, snippet))

    def test_cost_by_exact_model_per_week_summed_over_people(self):
        self.assertEqual(self._chart(), {"Opus 5.5": [40.0, 0], "Sonnet 5.5": [5.0, 7.0]})

    def test_output_tokens(self):
        self.assertEqual(self._chart("tokens"), {"Sonnet 5.5": [900, 300], "Opus 5.5": [150, 0]})

    def test_follows_the_people_filter(self):
        self.assertEqual(self._chart(people=["b@x.com"]), {"Opus 5.5": [10.0, 0], "Sonnet 5.5": [0, 7.0]})

    def test_more_than_eight_models_fold_into_other(self):
        models = [_model_row("a@x.com", "2026-09-14", f"claude-opus-{i}", 100.0 - i) for i in range(10)]
        res = self._chart(models=models)
        self.assertEqual(len(res), 9)
        self.assertEqual(res["Other"], [92.0 + 91.0, 0])

    def test_the_card_sits_on_overview_with_a_toggle(self):
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        overview = html.split('id="tab-overview"')[1].split('class="tabpanel')[0]
        self.assertIn('id="modelTrendChart"', overview)
        self.assertIn('id="modelMetricRow"', overview)
        self.assertLess(overview.index('id="trendChart"'), overview.index('id="modelTrendChart"'))

class TestRangeFollowing(unittest.TestCase):
    """People table, tiles and Value per seat follow state.from / state.to."""

    PEOPLE = [{"person": "a@x.com", "display_name": "Ann", "window_from": "2026-08-31",
               "window_to": "2026-09-27", "seat_price": 200, "seat_use": 0.99, "est_cost_usd": 999,
               "sessions": 999, "prs": 99, "seat_idle": "no", "last_active_date": "2026-09-20"}]
    WEEKLY = [_row18("a@x.com", "2026-09-07", "r", 10.0, days=2, exact=2, sessions=1, retries=1),
              _row18("a@x.com", "2026-09-14", "r", 30.0, days=4, exact=4, sessions=3, retries=2)]
    PRS = [{"person": "a@x.com", "opened_on": "2026-09-08", "session_cost_usd": 1, "session_prs": 1},
           {"person": "a@x.com", "opened_on": "2026-09-16", "session_cost_usd": 1, "session_prs": 1},
           {"person": "a@x.com", "opened_on": "2026-09-17", "session_cost_usd": 1, "session_prs": 1}]
    PRICES = {"source": "seat-prices.json", "per": "month"}

    def _cells(self, fr, to, **extra):
        payload = dict(people=self.PEOPLE, weekly=self.WEEKLY, prs=self.PRS, prices=self.PRICES, **extra)
        html = run_js(TEAM, _setup(**payload) + f"state.from = '{fr}'; state.to = '{to}';"
                      "renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        row = html.split("<tr>")[1]
        return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)

    def test_people_table_follows_the_range(self):
        one = self._cells("2026-09-14", "2026-09-14")
        both = self._cells("2026-09-07", "2026-09-14")
        # columns: person, seat, usage, extra, seat use, last active, sessions, cost, sub share, PRs,
        # seat cost, value, retries
        self.assertEqual(one[6], "3")
        self.assertEqual(both[6], "4")
        self.assertEqual(one[7], "$30")
        self.assertEqual(both[7], "$40")
        self.assertEqual(one[9], "2")
        self.assertEqual(both[9], "3")
        self.assertEqual(one[12], "2")
        self.assertEqual(both[12], "3")
        self.assertNotIn("999", "".join(one))

    def test_last_active_is_a_week_and_idle_is_a_badge(self):
        one = self._cells("2026-09-14", "2026-09-14")
        self.assertIn("week of", one[5])
        self.assertNotIn("idle", one[5])
        idle = self._cells("2026-09-21", "2026-09-21")
        self.assertIn("idle", idle[5])

    OUTSIDE = {"person": "z@x.com", "display_name": "Zed", "window_from": "2026-01-05",
               "window_to": "2026-01-25", "seat_price": 200}

    def test_a_range_outside_the_window_shows_dashes_and_no_idle_badge(self):
        html = run_js(TEAM, _setup(people=[self.OUTSIDE], weekly=self.WEEKLY, prs=self.PRS, prices=self.PRICES) +
                      "renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        cells = re.findall(r"<td[^>]*>(.*?)</td>", html.split("<tr>")[1], re.S)
        for i in (4, 6, 7, 8, 9, 10, 11, 12):
            self.assertEqual(cells[i], "—", i)
        self.assertNotIn("badge warn", cells[5])
        self.assertIn("not covered by these weeks", cells[5])
        value = run_js(TEAM, _setup(people=[self.OUTSIDE], weekly=self.WEEKLY, prices=self.PRICES) +
                       "renderValue(); out(el('#valueTable tbody').innerHTML);")
        self.assertEqual(value.count("—"), 5)  # tier (none), usage, seat cost, extra, value

    def test_tiles_count_an_uncovered_person_apart_and_skip_them_in_medians(self):
        snippet = (_setup(people=[self.OUTSIDE] + self.PEOPLE, weekly=self.WEEKLY, prices=self.PRICES) +
                   "renderTiles(); out(el('teamTiles').innerHTML);")
        html = run_js(TEAM, snippet)
        self.assertIn("1 not covered by these weeks", html)
        self.assertIn("1 active · 0 no activity in these weeks", html)

    def test_the_idle_finding_skips_an_uncovered_person(self):
        html = run_js(TEAM, _setup(people=[self.OUTSIDE], weekly=[], sources=[]) +
                      "renderFindings(); out(el('findingsList').innerHTML);")
        self.assertNotIn("no Claude Code activity", html)

    def test_limits_and_prs_are_computed_once_per_render(self):
        snippet = (_setup(people=self.PEOPLE * 1 + [self.OUTSIDE], weekly=self.WEEKLY, prs=self.PRS, prices=self.PRICES) +
                   "let n = 0; const real = limitRows; limitRows = () => { n++; return real(); };"
                   "renderPeople(); out(n);")
        self.assertEqual(run_js(TEAM, snippet), 1)

    def test_tiles_follow_the_range(self):
        def tiles(fr, to):
            snippet = (_setup(people=self.PEOPLE, weekly=self.WEEKLY, prices=self.PRICES) +
                       f"state.from = '{fr}'; state.to = '{to}'; renderTiles(); out(el('teamTiles').innerHTML);")
            return dict(re.findall(r'<div class="label">(.*?)</div><div class="value">(.*?)</div>',
                                   run_js(TEAM, snippet)))
        one, both = tiles("2026-09-14", "2026-09-14"), tiles("2026-09-07", "2026-09-14")
        self.assertNotEqual(one["Median seat use"], both["Median seat use"])
        self.assertNotEqual(one["Value per seat"], both["Value per seat"])

    def test_people_tile_counts_idle_in_the_range(self):
        snippet = (_setup(people=self.PEOPLE, weekly=self.WEEKLY) +
                   "state.from = '2026-09-21'; state.to = '2026-09-21'; renderTiles(); out(el('teamTiles').innerHTML);")
        html = run_js(TEAM, snippet)
        self.assertIn("0 active · 1 no activity in these weeks", html)
        self.assertNotIn("whole export window", html)

    def test_value_seat_cost_for_one_week(self):
        html = run_js(TEAM, _setup(people=self.PEOPLE, weekly=self.WEEKLY, prices=self.PRICES) +
                      "state.from = '2026-09-14'; state.to = '2026-09-14'; renderValue();"
                      "out(el('#valueTable tbody').innerHTML);")
        want = "$" + f"{round(200 / (365.25 / 12) * 7):,}"
        self.assertIn(f'<td class="num">{want}</td>', html)

    def test_approximate_seat_use_is_marked(self):
        weekly = [_row18("a@x.com", "2026-09-14", "r", 30.0, days=4, exact=None)]
        html = run_js(TEAM, _setup(people=self.PEOPLE, weekly=weekly, prices=self.PRICES) +
                      "state.from = '2026-09-14'; state.to = '2026-09-14';"
                      "renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        self.assertIn("≈", html)
        self.assertIn("re-export", html)
        exact = run_js(TEAM, _setup(people=self.PEOPLE, weekly=self.WEEKLY, prices=self.PRICES) +
                       "state.from = '2026-09-14'; state.to = '2026-09-14';"
                       "renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        self.assertNotIn("≈", exact)

    def test_idle_finding_follows_the_range(self):
        def idle(fr, to):
            snippet = (_setup(people=self.PEOPLE, weekly=self.WEEKLY, sources=[]) +
                       f"state.from = '{fr}'; state.to = '{to}'; renderFindings(); out(el('findingsList').innerHTML);")
            return run_js(TEAM, snippet)
        self.assertIn("no Claude Code activity in these weeks", idle("2026-09-21", "2026-09-21"))
        self.assertNotIn("no Claude Code activity", idle("2026-09-14", "2026-09-14"))


class TestSnapshotsAndUtcWeeks(unittest.TestCase):
    LA = {"TZ": "America/Los_Angeles"}
    # Ann's export ends in the week of Sep 21; Bo's in the week of Sep 14.
    PEOPLE = [{"person": "a@x.com", "display_name": "Ann", "window_from": "2026-08-31", "window_to": "2026-09-24",
               "allowance_week_used_pct": 0.83, "allowance_status": "on_pace_to_hit",
               "projected_limit_hit_at": "2026-09-27T10:00:00Z", "rate_limit_tier": "default_claude_max_5x"}]

    def _limits_cells(self, fr, to, **extra):
        html = run_js(TEAM, _setup(**dict(dict(people=self.PEOPLE), **extra)) +
                      f"state.from = '{fr}'; state.to = '{to}'; renderLimits();"
                      "out(el('#limitsTable tbody').innerHTML);")
        return html, re.findall(r"<td[^>]*>.*?</td>", html.split("<tr>")[1], re.S)

    def test_monday_utc_hit_lands_in_its_week(self):
        limit = {"person": "a@x.com", "limit_type": "weekly", "hit_at": "2026-09-28T00:30:00Z",
                 "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": 5}
        snippet = (_setup(people=self.PEOPLE, limits=[limit]) +
                   "state.from = '2026-09-28'; state.to = '2026-09-28'; out(limitRows().length);")
        # In Los Angeles that instant is still Sunday evening, Sep 27.
        self.assertEqual(run_js(TEAM, snippet, env=self.LA), 1)
        self.assertEqual(run_js(TEAM, snippet.replace("2026-09-28'; state.to", "2026-09-21'; state.to")
                                .replace("state.to = '2026-09-28'", "state.to = '2026-09-21'"), env=self.LA), 0)

    def test_the_snapshot_cells_show_only_for_the_export_week(self):
        _, inside = self._limits_cells("2026-09-21", "2026-09-21")
        self.assertIn("83%", inside[6])
        self.assertIn("On pace to hit", inside[7])
        self.assertNotIn("—", inside[8])
        self.assertNotIn("outside the selected weeks", "".join(inside))

    def test_a_range_ending_before_the_export_week_dashes_the_snapshot(self):
        _, cells = self._limits_cells("2026-08-31", "2026-09-14")
        for i in (6, 7, 8):
            self.assertIn("—", cells[i])
            self.assertIn('title="As of their export (week of 2026-09-21), outside the selected weeks"', cells[i])
        self.assertNotIn("83%", "".join(cells))
        self.assertNotIn("On pace", "".join(cells))
        self.assertNotIn("never hit", "".join(cells))

    def test_findings_pass_the_snapshot_flag(self):
        def has(fr, to):
            html = run_js(TEAM, _setup(people=self.PEOPLE, sources=[]) +
                          f"state.from = '{fr}'; state.to = '{to}'; renderFindings(); out(el('findingsList').innerHTML);")
            return "on pace to hit" in html
        self.assertTrue(has("2026-09-21", "2026-09-21"))
        self.assertFalse(has("2026-08-31", "2026-09-14"))


class TestApproximateSeatUseIsBounded(unittest.TestCase):
    """An old one-machine export still knows the person's total active days; the
    People table, spend per day and the heatmap use it (F1)."""

    # Sep 14 - Sep 27 has 10 workdays and 8 active days; both weeks had 3 projects of 3 days.
    PERSON = {"person": "a@x.com", "display_name": "Ann", "window_from": "2026-09-14", "window_to": "2026-09-27",
              "machines": 1, "person_active_days": 8, "window_workdays": 10}
    WEEKLY = [_row18("a@x.com", w, r, 10.0, days=3) for w in ("2026-09-14", "2026-09-21") for r in "xyz"]

    def _seat_use(self, person=None, fr="2026-09-14", to="2026-09-21"):
        html = run_js(TEAM, _setup(people=[person or self.PERSON], weekly=self.WEEKLY) +
                      f"state.from = '{fr}'; state.to = '{to}'; renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        return re.findall(r"<td[^>]*>.*?</td>", html.split("<tr>")[1], re.S)[4]

    def test_all_weeks_show_the_exports_own_figure_without_an_approximation(self):
        cell = self._seat_use()
        self.assertIn("80%", cell)
        self.assertNotIn("≈", cell)
        self.assertNotIn("title=", cell)

    def test_one_week_is_a_bounded_estimate_with_the_range_in_the_tooltip(self):
        cell = self._seat_use(fr="2026-09-14", to="2026-09-14")
        self.assertIn("≈", cell)
        self.assertIn("Between 3 and 5 active days", cell)
        self.assertIn("re-export", cell)

    def test_two_machines_keep_the_capped_estimate(self):
        cell = self._seat_use(dict(self.PERSON, machines=2))
        self.assertIn("≈", cell)
        self.assertIn("100%", cell)        # 14 capped days over 10 workdays
        self.assertNotIn("between", cell)

    def test_spend_per_active_day_uses_the_same_days(self):
        html = run_js(TEAM, _setup(people=[self.PERSON], weekly=self.WEEKLY) +
                      "state.from = '2026-09-14'; state.to = '2026-09-21'; renderPerDay(); out(el('#perDayTable tbody').innerHTML);")
        self.assertIn('<td class="num">8</td>', html)

    def test_the_heatmap_marks_an_estimated_week(self):
        html = run_js(TEAM, _setup(people=[self.PERSON], weekly=self.WEEKLY) +
                      "state.from = '2026-09-14'; state.to = '2026-09-21'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn("≈4 active days", html)
        exact = [_row18("a@x.com", "2026-09-14", "x", 10.0, days=3, exact=3)]
        html = run_js(TEAM, _setup(people=[self.PERSON], weekly=exact) +
                      "state.from = '2026-09-14'; state.to = '2026-09-14'; renderHeatmap(); out(el('heatmap').innerHTML);")
        self.assertIn("3 active days", html)
        self.assertNotIn("≈", html)


class TestGapsBetweenExportsAreNotCovered(unittest.TestCase):
    """A person's coverage is each export's own window, not the span from the
    first start to the last end (F2)."""

    # Exports Aug 3 - 16 and Sep 28 - Oct 11: 10 workdays each, nothing known between.
    PERSON = {"person": "a@x.com", "display_name": "Ann", "window_from": "2026-08-03", "window_to": "2026-10-11",
              "machines": 1, "person_active_days": 8, "window_workdays": 20, "seat_price": 300}
    SOURCES = [{"person": "a@x.com", "window_from": "2026-08-03", "window_to": "2026-08-16"},
               {"person": "a@x.com", "window_from": "2026-09-28", "window_to": "2026-10-11"}]
    WEEKLY = [_row18("a@x.com", w, "r", 10.0, days=2, exact=2) for w in ("2026-08-03", "2026-08-10", "2026-09-28", "2026-10-05")]
    PRICES = {"source": "seat-prices.json", "per": "month"}

    def _people(self, fr, to):
        html = run_js(TEAM, _setup(people=[self.PERSON], weekly=self.WEEKLY, sources=self.SOURCES, prices=self.PRICES) +
                      f"state.from = '{fr}'; state.to = '{to}'; renderPeople(); out(el('#peopleTable tbody').innerHTML);")
        return re.findall(r"<td[^>]*>.*?</td>", html.split("<tr>")[1], re.S)

    def test_a_range_inside_the_gap_is_not_covered(self):
        cells = self._people("2026-08-24", "2026-09-14")
        self.assertIn("not covered by these weeks", cells[5])
        self.assertNotIn("idle", cells[5])
        for i in (4, 6, 7, 10, 11):
            self.assertIn("—", cells[i], i)

    def test_a_gap_range_gives_no_idle_finding_or_idle_tile(self):
        base = _setup(people=[self.PERSON], weekly=self.WEEKLY, sources=self.SOURCES) + "state.from = '2026-08-24'; state.to = '2026-09-14';"
        self.assertNotIn("no Claude Code activity", run_js(TEAM, base + "renderFindings(); out(el('findingsList').innerHTML);"))
        tiles = run_js(TEAM, base + "renderTiles(); out(el('teamTiles').innerHTML);")
        self.assertIn("1 not covered by these weeks", tiles)

    def test_all_counts_the_workdays_and_calendar_days_of_the_exports_only(self):
        cells = self._people("2026-08-03", "2026-10-05")
        self.assertIn("40%", cells[4])                    # 8 active days of 20 workdays, not of 50
        self.assertNotIn("≈", cells[4])
        want = "$" + f"{round(300 / (365.25 / 12) * 28):,}"      # 28 calendar days, not 70
        self.assertEqual(re.sub(r"<.*?>", "", cells[10]), want)

    def test_the_run_rate_ignores_weeks_in_the_gap(self):
        limits = [{"person": "a@x.com", "limit_type": "weekly", "hit_at": f"{day}T10:00:00Z",
                   "locked_out": False, "blocked_hours": 0, "usage_after_hit_usd": 30}
                  for day in ("2026-08-05", "2026-08-12", "2026-09-29")]
        got = run_js(TEAM, _setup(people=[self.PERSON], weekly=self.WEEKLY, sources=self.SOURCES, limits=limits) +
                     "todayUTC = () => '2026-10-15'; state.from = '2026-08-03'; state.to = '2026-09-28';"
                     "renderOverage(); out({note: el('forecastNote').textContent, charts: charts_made});")
        self.assertIn("$30.00/week", got["note"])         # 90 over the 3 covered complete weeks, not / 4
        self.assertIn("3 complete week", got["note"])
        bars = got["charts"][-1]["data"]["datasets"][0]["data"]
        weeks = ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07",
                 "2026-09-14", "2026-09-21", "2026-09-28"]
        self.assertEqual([b is None for b in bars], [w in weeks[2:8] for w in weeks])


class TestLimitsTableCoverage(unittest.TestCase):
    """A person the selected weeks don't reach has no hits to count (F3)."""

    COVERED = {"person": "c@x.com", "display_name": "Cy", "window_from": "2026-08-03", "window_to": "2026-08-30"}
    LATE = {"person": "a@x.com", "display_name": "Ann", "window_from": "2026-09-07", "window_to": "2026-09-27"}

    def _rows(self, people):
        html = run_js(TEAM, _setup(people=people) + "state.from = '2026-08-03'; state.to = '2026-08-24'; renderLimits();"
                      "out(el('#limitsTable tbody').innerHTML);")
        return [re.findall(r"<td[^>]*>.*?</td>", r, re.S) for r in html.split("<tr>")[1:]]

    def test_an_uncovered_person_has_dashes_not_zeros(self):
        late = next(r for r in self._rows([self.LATE, self.COVERED]) if "Ann" in r[0])
        for i in (2, 3, 4, 5):
            self.assertIn("—", late[i], i)
            self.assertIn('title="Their export doesn&#39;t cover the selected weeks"', late[i], i)

    def test_a_covered_person_with_no_hits_still_shows_zero(self):
        covered = next(r for r in self._rows([self.LATE, self.COVERED]) if "Cy" in r[0])
        self.assertIn(">0<", covered[2])
        self.assertIn(">0<", covered[3])

    def test_uncovered_people_sort_last(self):
        rows = self._rows([self.LATE, self.COVERED])
        self.assertIn("Cy", rows[0][0])
        self.assertIn("Ann", rows[1][0])


if __name__ == "__main__":
    unittest.main()
