"""Phase 4b fixes to the team dashboard (T1-T10 from the re-check): the merge in
claude_usage/team.py and the page's logic under node against a stub DOM
(static/team.js, static/team-findings.js, static/team.html)."""
import datetime as dt
import json
import re
import unittest

from claude_usage import team
from tests.jsrun import run_js
from tests.test_p3_team import P3Case, week_row
from tests.test_p3_team_ui import F, HTML, TEAM, WEEK, WINDOW, row, setup

DOCS = (team.os.path.dirname(team.os.path.dirname(team.__file__)))


def doc(name):
    with open(team.os.path.join(DOCS, "docs", name), encoding="utf-8") as fh:
        return fh.read()


def page(fn, element, **payload):
    return run_js(TEAM, setup(**payload) + f"{fn}(); out(el({element!r}).innerHTML);")


def charts(fn, **payload):
    return run_js(TEAM, setup(**payload) + f"{fn}(); out(charts_made.map(c => ({{labels: c.data.labels, "
                  "sets: c.data.datasets.map(d => ({label: d.label, data: d.data, color: d.backgroundColor}))})));")


class TestT1NoLogsBothSchemas(unittest.TestCase):
    def person(self, days):
        return {"person": "k", "window_from": "2026-09-01", "window_to": "2026-09-27" if days > 1 else "2026-09-01",
                "window_days": days, "no_usage_window": True}

    def in_range(self, p):
        return run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], {{from: '2026-09-21', to: '2026-09-27'}}, null, "
                         "{allRows: []}));")

    def test_a_thirty_day_no_logs_export_is_not_idle_and_has_no_seat_use(self):
        res = self.in_range(self.person(30))
        self.assertTrue(res["noLogs"])
        self.assertFalse(res["idle"])
        self.assertIsNone(res["seatUse"])
        self.assertIsNone(res["value"])

    def test_the_badge_tooltip_follows_window_days(self):
        people = [dict(WINDOW, person="a", no_usage_window=True, window_days=30, machines=1)]
        html = page("renderPeople", "#peopleTable tbody", people=people)
        self.assertIn("covers only 30 days", html)
        self.assertNotIn("a single day", html)
        people = [dict(WINDOW, person="a", no_usage_window=True, window_days=1, machines=1)]
        self.assertIn("a single day", page("renderPeople", "#peopleTable tbody", people=people))

    def test_the_docs_say_either_length_is_not_idle(self):
        self.assertIn("either length", doc("team.md"))


class TestT2OutputFootnote(unittest.TestCase):
    def output(self, sources):
        weekly = [row("a", WEEK, "r", 10.0, lines=5, removed=1, with_lines=10.0), row("b", WEEK, "r", 10.0)]
        return page("renderOutput", "outputNotes", people=[{"person": "a"}, {"person": "b"}], weekly=weekly, sources=sources)

    def test_the_footnote_no_longer_says_lines_come_from_the_counter_alone(self):
        self.assertIn("counted per edit", HTML)
        self.assertIn("Exports before schema 13 took", HTML)

    def test_mixed_sources_are_named_and_the_two_measures_noted(self):
        html = self.output([{"person": "a", "schema_version": "12"}, {"person": "b", "schema_version": "13"}])
        self.assertIn("a", html)
        self.assertIn("counted per edit", html)
        self.assertIn("two different measures", html)

    def test_all_old_sources_note_the_old_measure_without_the_mix(self):
        html = self.output([{"person": "a", "schema_version": "12"}, {"person": "b", "schema_version": "12"}])
        self.assertIn("own counter", html)
        self.assertNotIn("two different measures", html)

    def test_all_new_sources_have_no_note(self):
        self.assertEqual(self.output([{"person": "a", "schema_version": "13"}, {"person": "b", "schema_version": "13"}]), "")


class TestT3NoLogsReport(P3Case):
    PRICES = {"currency": "USD", "per": "month", "prices": {"default": 100.0}, "source": "seat-prices.json"}

    def test_a_pre_13_one_day_no_logs_export_has_no_value_or_seat_use(self):
        self.folder.export("idle", "20261029", [week_row("2026-10-26", "", 0, requests="0", sessions_started="0",
                                                         sessions_active="0")], since="2026-10-29", until="2026-10-29",
                           person_active_days="0", window_workdays="1", last_active_date="", seat_idle="yes")
        merged = self.folder.merge(prices=self.PRICES)
        (p,) = merged["people"]
        self.assertTrue(p["no_usage_window"])
        self.assertIsNone(p["seat_use"])
        self.assertIsNone(p["value_multiple"])
        text = team.report_text(merged, today=dt.date(2026, 10, 29))
        self.assertNotIn("0.00x", text)

    def test_a_person_with_usage_keeps_both(self):
        self.folder.export("busy", "20260921", [week_row("2026-09-14", "x", 50.0)])
        (p,) = self.folder.merge(prices=self.PRICES)["people"]
        self.assertIsNotNone(p["seat_use"])
        self.assertIsNotNone(p["value_multiple"])


class TestT4OlderToolsFiles(unittest.TestCase):
    def notes(self, sources):
        people = [{"person": "old", "display_name": "Olga"}, {"person": "new", "display_name": "Nia"}]
        weekly = [row("old", WEEK, "r", 10.0), row("new", WEEK, "r", 10.0)]
        return page("renderFriction", "frictionNotes", people=people, weekly=weekly, sources=sources)

    def test_a_pre_13_tools_file_gets_a_note_naming_the_person(self):
        html = self.notes([{"person": "old", "files": ["summary", "tools"], "schema_version": "12"},
                           {"person": "new", "files": ["summary", "tools"], "schema_version": "13"}])
        self.assertIn("Tool errors from Olga", html)
        self.assertIn("count refused calls", html)
        self.assertNotIn("Nia", html)

    def test_no_note_when_every_tools_file_is_13(self):
        html = self.notes([{"person": "old", "files": ["summary", "tools"], "schema_version": "13"}])
        self.assertNotIn("refused", html)

    def test_page_and_docs_say_what_old_exports_do(self):
        self.assertIn("exports before schema 13 can", HTML)
        self.assertIn("before schema 13", doc("team.md"))


class TestT5ToolNameWording(unittest.TestCase):
    def test_real_but_undocumented_tools_are_not_made_up(self):
        for name in ("SearchSkills", "DesignSync", "PublishPlugin", "ListSkills", "ListPlugins"):
            res = run_js(F, f"out(inventedTool('built-in', {json.dumps(name)}, {{calls: 4, errors: 4}}));")
            self.assertFalse(res, name)
        self.assertTrue(run_js(F, "out(inventedTool('built-in', 'bash', {calls: 4, errors: 4}));"))

    def test_the_wording_says_documented_tool_list(self):
        self.assertIn("isn&rsquo;t in Claude Code&rsquo;s documented tool list", HTML)
        self.assertNotIn("isn&rsquo;t a Claude Code built-in", HTML)
        tools = [["p", WEEK, "bash", "built-in", 4, 4]]
        notes = page("renderFriction", "frictionNotes", people=[{"person": "p"}], tools=tools)
        self.assertIn("documented tool list", notes)
        self.assertNotIn("built-ins", notes)
        self.assertNotIn("isn't a Claude Code built-in", doc("team.md"))


class TestT6NinthColour(unittest.TestCase):
    def test_nine_distinct_series_variables_and_the_ninth_is_defined(self):
        vs = run_js(TEAM, "out(SERIES_VARS);")
        self.assertEqual(len(set(vs)), 9)
        self.assertIn("--series-9:", HTML)
        with open(team.os.path.join(DOCS, "static", "app.css"), encoding="utf-8") as fh:
            css = fh.read()
        light = re.search(r"--series-9:\s*(#\w+)", HTML).group(1).lower()
        self.assertNotIn(light, css.lower())

    def test_a_ninth_named_model_does_not_reuse_the_first_colour(self):
        models = [["a", WEEK, "x", "main", 100.0 - i, 100.0, 0.0, f"claude-opus-5-{i}"] for i in range(9)]
        used = run_js(TEAM, setup(people=[{"person": "a"}], models=models) +
                      "const seen = []; getComputedStyle = () => ({ getPropertyValue: n => n }); "
                      "renderMix(); out(charts_made[0].data.datasets.map(d => d.backgroundColor));")
        self.assertEqual(len(used), 9)
        self.assertEqual(len(set(used)), 9)


class TestT7TwoMachineFloors(unittest.TestCase):
    def two_machines(self):
        people = [dict(WINDOW, person="a", machines=2, window_workdays=20, window_days=28)]
        weekly = [row("a", WEEK, "r", 5.0, days=3, exact=3, machine="m1"), row("a", WEEK, "r", 5.0, days=3, exact=3, machine="m2")]
        return people, weekly

    def test_the_median_tile_carries_a_floor_mark(self):
        people, weekly = self.two_machines()
        html = page("renderTiles", "teamTiles", people=people, weekly=weekly,
                    sources=[{"person": "a", **WINDOW, "schema_version": "12"}])
        text = re.sub(r"<[^>]+>", " ", html)
        self.assertRegex(text, r"≥\s*\d+%")
        self.assertIn("a floor", text)

    def test_one_machine_has_no_floor_mark(self):
        people = [dict(WINDOW, person="a", machines=1, window_workdays=20, window_days=28)]
        weekly = [row("a", WEEK, "r", 5.0, days=3, wdays=7)]
        html = page("renderTiles", "teamTiles", people=people, weekly=weekly, sources=[{"person": "a", **WINDOW}])
        self.assertNotIn("≥", html)

    def test_the_heatmap_tooltip_gives_the_range(self):
        people, weekly = self.two_machines()
        html = page("renderHeatmap", "heatmap", people=people, weekly=weekly,
                    sources=[{"person": "a", **WINDOW, "schema_version": "12"}])
        self.assertIn("between 3 and 6", html)
        self.assertIn("≈", html)


class TestT8UncoveredWeeksAreBlank(unittest.TestCase):
    """Two people whose exports leave the weeks of 09-07 and 09-14 uncovered: no
    selected person's export touches them, so they are unknown, not zero."""

    def setUp(self):
        self.people = [{"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-06"},
                       {"person": "b", "window_from": "2026-09-21", "window_to": "2026-09-27"}]
        self.sources = [dict(p) for p in self.people]
        self.weekly = [row("a", "2026-08-31", "r", 5.0), row("b", "2026-09-21", "r", 7.0)]

    def models(self):
        return [["a", "2026-08-31", "x", "main", 5.0, 100.0, 0.0, "claude-opus-5-5"],
                ["b", "2026-09-21", "x", "main", 7.0, 100.0, 0.0, "claude-sonnet-5-5"]]

    def test_model_trend_leaves_uncovered_weeks_blank_and_covered_ones_zero(self):
        (chart,) = charts("renderModelTrend", people=self.people, sources=self.sources, models=self.models(),
                          weekly=self.weekly)
        by = {s["label"]: s["data"] for s in chart["sets"]}
        self.assertEqual(len(chart["labels"]), 4)
        self.assertEqual(by["Opus 5.5"], [5.0, None, None, 0])      # covered week without use is 0
        self.assertEqual(by["Sonnet 5.5"], [0, None, None, 7.0])

    def test_repo_trend_leaves_uncovered_weeks_blank(self):
        (chart,) = charts("renderRepoTrend", people=self.people, sources=self.sources, weekly=self.weekly)
        self.assertEqual([s["data"] for s in chart["sets"]], [[5.0, None, None, 7.0]])


class TestT9EnterpriseSeatPrice(unittest.TestCase):
    def test_the_rate_limit_tier_is_skipped_for_an_enterprise_seat(self):
        person = {"rate_limit_tier": "default_claude_max_5x", "seat_tier": "enterprise_tier_1"}
        self.assertIsNone(team._seat_price(person, {"per": "month", "prices": {"default_claude_max_5x": 100}}))
        self.assertEqual(team._seat_price(person, {"per": "month", "prices": {"default_claude_max_5x": 100, "default": 70}}), 70)
        self.assertEqual(team._seat_price(person, {"per": "month", "prices": {"enterprise_tier_1": 40, "default": 70}}), 40)

    def test_a_consumer_seat_still_uses_the_rate_limit_tier_first(self):
        person = {"rate_limit_tier": "default_claude_max_5x", "seat_tier": "team"}
        self.assertEqual(team._seat_price(person, {"per": "month", "prices": {"default_claude_max_5x": 100, "team": 25}}), 100)


class TestT10RunRateRounding(unittest.TestCase):
    def test_the_rate_averages_unrounded_weeks_like_the_page(self):
        limits = [{"limit_type": "weekly", "hit_at": f"{day}T10:00:00+00:00", "usage_after_hit_usd": str(usd)}
                  for day, usd in (("2026-09-01", 0.004), ("2026-09-08", 0.004), ("2026-09-15", 0.004), ("2026-09-22", 20.014))]
        merged = {"files": {"limits": ([], limits)}, "warnings": [], "prices": None,
                  "people": [{"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-27",
                              "latest_export_at": "2026-09-28T10:00:00+00:00"}],
                  "sources": [{"person": "a", "window_from": "2026-08-31", "window_to": "2026-09-27"}]}
        f = team.overage_forecast(merged, today=dt.date(2026, 10, 29))
        self.assertEqual(f["basis_weeks"], 4)
        self.assertEqual(f["weekly_run_rate_usd"], round(20.026 / 4, 2))   # 5.01, not 5.00 from cent-rounded weeks
        js = run_js(F, "out(rangeRunRate(%s, ['2026-08-31', '2026-09-07', '2026-09-14', '2026-09-21'], '2026-09-27', '2026-10-29'))"
                    % json.dumps([{"limit_type": "weekly", "hit_at": l["hit_at"], "usage_after_hit_usd": float(l["usage_after_hit_usd"])}
                                  for l in limits]))
        self.assertEqual(round(js["rate"], 2), f["weekly_run_rate_usd"])


if __name__ == "__main__":
    unittest.main()
