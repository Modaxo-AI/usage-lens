"""The team dashboard's DOM-free helpers (static/team-findings.js), under node."""
import json
import unittest

from tests.jsrun import run_js

F = ["static/team-findings.js"]


def js(expr):
    return run_js(F, f"out({expr});")


def week(person, w, repo, cost, days=1, lines=None, removed=None, with_lines=0.0, wdays=7):
    return [person, w, repo, cost, 0, 1, 1, days, False, False, lines, removed, with_lines, 1, wdays]


class TestLabels(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(js("tierLabel('default_claude_max_5x')"), "Max 5×")
        self.assertEqual(js("tierLabel('enterprise_tier_1')"), "Enterprise tier 1")
        self.assertEqual(js("tierLabel('something_new')"), "something_new")
        self.assertEqual(js("tierLabel(null)"), "—")

    def test_display_name_falls_back(self):
        self.assertEqual(js("displayName({person: 'a@x', display_name: 'A B'})"), "A B")
        self.assertEqual(js("displayName({person: 'a@x', person_name: 'Ann'})"), "Ann")
        self.assertEqual(js("displayName({person: 'a@x'})"), "a@x")

    def test_integrations(self):
        self.assertEqual(js("integrationLabel('built-in', 'Bash')"), "Bash")
        self.assertEqual(js("integrationLabel('claude_ai_Tracker', 'x')"), "claude_ai_Tracker (MCP)")
        self.assertEqual(js("integrationLabel('12345678-aaaa-4bbb-8ccc-1234567890ab', 'x')"),
                         "MCP connector (12345678…)")
        self.assertTrue(js("isClaudeCodeTool('built-in', 'TaskStop')"))
        self.assertFalse(js("isClaudeCodeTool('built-in', 'Bash')"))
        self.assertFalse(js("isClaudeCodeTool('srv', 'TaskStop')"))


class TestRepoSpend(unittest.TestCase):
    def test_top_n_then_other_with_people(self):
        rows = [week("a", "2026-09-14", f"r{i}", 10.0 + i) for i in range(10)]
        rows.append(week("b", "2026-09-14", "r9", 5.0))
        res = run_js(F, f"out(repoSpend({json.dumps(rows)}, 8));")
        names = [r["repo"] for r in res["repos"]]
        self.assertEqual(names[0], "r9")
        self.assertEqual(names[-1], "Other")
        self.assertEqual(len(names), 9)
        self.assertAlmostEqual(sum(r["cost"] for r in res["repos"]), res["total"])
        self.assertEqual([p for p, _ in res["repos"][0]["people"]], ["a", "b"])
        self.assertAlmostEqual(sum(r["share"] for r in res["repos"]), 1.0)

    def test_blank_repo_is_labelled(self):
        rows = [week("a", "2026-09-14", "", 3.0), week("a", "2026-09-21", "", 0.0)]
        res = run_js(F, f"out(repoSpend({json.dumps(rows)}));")
        self.assertEqual([r["repo"] for r in res["repos"]], ["(no project)"])


class TestNamesAreData(unittest.TestCase):
    """Repos, people and tools come from other people's files: a name that is
    also an Object.prototype member must be an ordinary name."""

    HOSTILE = ["toString", "constructor", "valueOf", "hasOwnProperty", "__proto__"]

    def test_repos_named_like_object_members_are_ordinary_repos(self):
        rows = [week("a", "2026-09-14", r, 10.0 + i) for i, r in enumerate(self.HOSTILE)]
        rows.append(week("__proto__", "2026-09-14", "toString", 5.0))
        res = run_js(F, f"out(repoSpend({json.dumps(rows)}, 8));")
        self.assertEqual(sorted(r["repo"] for r in res["repos"]), sorted(self.HOSTILE))
        by = {r["repo"]: r for r in res["repos"]}
        self.assertAlmostEqual(by["toString"]["cost"], 15.0)
        self.assertEqual([p for p, _ in by["toString"]["people"]], ["a", "__proto__"])
        self.assertAlmostEqual(by["__proto__"]["cost"], 14.0)

    def test_people_named_like_object_members_survive_per_day(self):
        rows = [week(p, "2026-09-14", "r", 10.0, days=2) for p in self.HOSTILE]
        res = run_js(F, f"out(perActiveDay({json.dumps(rows)}));")
        self.assertEqual(sorted(d["person"] for d in res), sorted(self.HOSTILE))
        self.assertTrue(all(d["days"] == 2 and d["cost"] == 10.0 for d in res))

    def test_output_by_person_keeps_hostile_names(self):
        rows = [week(p, "2026-09-14", "r", 10.0) for p in self.HOSTILE]
        res = run_js(F, f"const o = outputByPerson({json.dumps(rows)}, []); out(Object.keys(o).sort());")
        self.assertEqual(res, sorted(self.HOSTILE))

    def test_a_locked_out_person_called_constructor_is_named(self):
        people = [{"person": "constructor", "display_name": "Con"}]
        res = _findings(people=people, limits=[{"person": "constructor", "locked_out": True,
                                                "blocked_hours": 59, "limit_type": "spend_cap"}])
        self.assertEqual(len(res), 1)
        self.assertIn("Con (59.0 h)", res[0]["text"])
        self.assertNotIn("NaN", res[0]["text"])

    def test_hostile_tool_and_context_and_source_keys(self):
        tools = [["a", "2026-09-14", "toString", "built-in", 30, 9],
                 ["a", "2026-09-14", "x", "constructor", 30, 9]]
        health = [{"person": "valueOf", "very_long_context_cost_usd": 20.0, "est_cost_main_usd": 100.0}]
        sources = [{"person": "__proto__", "exported_at": "2020-01-01T00:00:00+00:00"}]
        res = _findings(people=[{"person": "valueOf", "display_name": "Val"}], tools=tools,
                        health=health, sources=sources)
        rules = {f["rule"]: f["text"] for f in res}
        self.assertIn("constructor (MCP)", rules["integration"])
        self.assertIn("Val (20%)", rules["context"])
        self.assertIn("__proto__", rules["stale"])


class TestActiveDays(unittest.TestCase):
    def test_summed_projects_are_capped_at_the_week(self):
        rows = [week("a", "2026-09-14", "r1", 1, days=5), week("a", "2026-09-14", "r2", 1, days=5)]
        self.assertEqual(run_js(F, f"out(activeDaysByPerson({json.dumps(rows)}));"), {"a": 7})

    def test_part_week_caps_lower(self):
        rows = [week("a", "2026-09-14", "r1", 1, days=3, wdays=2)]
        self.assertEqual(run_js(F, f"out(activeDaysByPerson({json.dumps(rows)}));"), {"a": 2})

    def test_a_narrow_window_on_one_machine_does_not_cap_the_week(self):
        # Summary rows are per machine: machine A saw 2 days of the week, B all 7.
        rows = [week("a", "2026-09-14", "r1", 1, days=2, wdays=2),
                week("a", "2026-09-14", "r2", 1, days=3, wdays=7)]
        self.assertEqual(run_js(F, f"out(activeDaysByPerson({json.dumps(rows)}));"), {"a": 5})

    def test_a_cap_above_seven_or_missing_is_seven(self):
        rows = [week("a", "2026-09-14", "r1", 1, days=9, wdays=9), week("a", "2026-09-21", "r1", 1, days=9, wdays=None)]
        self.assertEqual(run_js(F, f"out(activeDaysByPerson({json.dumps(rows)}));"), {"a": 14})

    def test_no_active_days_is_a_dash(self):
        rows = [week("a", "2026-09-14", "r1", 50.0, days=0)]
        res = run_js(F, f"out(perActiveDay({json.dumps(rows)}));")
        # (P3 TO-1/TO-6: the row also carries the days' range and the per-day range.)
        self.assertEqual([{k: r[k] for k in ("person", "cost", "days", "perDay")} for r in res],
                         [{"person": "a", "cost": 50.0, "days": 0, "perDay": None}])


class TestOutput(unittest.TestCase):
    def test_per_person_output(self):
        rows = [week("a", "2026-09-14", "r", 100.0, lines=900.0, removed=100.0, with_lines=60.0)]
        prs = [{"person": "a", "session_cost_usd": 30.0, "session_prs": 2},
               {"person": "a", "session_cost_usd": 30.0, "session_prs": 2}]
        # The share of spend with line data is over the whole cost of the sessions that
        # started in the weeks (P3 TM-1), passed as [person, week, cost].
        costs = [["a", "2026-09-14", 100.0]]
        res = run_js(F, f"out(outputByPerson({json.dumps(rows)}, {json.dumps(prs)}, {json.dumps(costs)}));")["a"]
        self.assertEqual(res["prs"], 2)
        self.assertAlmostEqual(res["costPerPr"], 15.0)
        self.assertEqual((res["linesAdded"], res["linesRemoved"]), (900, 100))
        self.assertAlmostEqual(res["perKLines"], 60.0)
        self.assertAlmostEqual(res["lineCoverage"], 0.6)

    def test_no_line_data_is_null_not_zero(self):
        rows = [week("a", "2026-09-14", "r", 100.0)]
        res = run_js(F, f"out(outputByPerson({json.dumps(rows)}, []));")["a"]
        self.assertIsNone(res["linesAdded"])
        self.assertIsNone(res["perKLines"])
        self.assertIsNone(res["costPerPr"])


def _input(**kw):
    base = {"people": [], "weekly": [], "limits": [], "tools": [], "health": [], "sources": []}
    base.update(kw)
    return json.dumps(base)


def _findings(today="2026-10-14", **kw):
    return run_js(F, f"out(findings({_input(**kw)}, '{today}'));")


class TestFindings(unittest.TestCase):
    PEOPLE = [{"person": "a", "display_name": "Ann"}, {"person": "b", "display_name": "Bo"}]

    def test_nothing_stands_out(self):
        self.assertEqual(_findings(people=self.PEOPLE), [])

    def test_lock_out(self):
        res = _findings(people=self.PEOPLE, limits=[{"person": "a", "locked_out": True, "blocked_hours": 59,
                                                     "limit_type": "spend_cap"}])
        self.assertEqual(res[0]["rule"], "lockout")
        self.assertIn("Ann", res[0]["text"])
        self.assertIn("59.0 h", res[0]["text"])
        self.assertEqual(res[0]["tab"], "seats")

    def test_concentration_needs_two_repos(self):
        one = [week("a", "2026-09-14", "r1", 100.0, days=7)]  # ~$14/day: below the per-day finding
        self.assertEqual(_findings(people=self.PEOPLE, weekly=one), [])
        two = one + [week("b", "2026-09-14", "r2", 50.0)]
        rules = [f["rule"] for f in _findings(people=self.PEOPLE, weekly=two)]
        self.assertIn("concentration", rules)

    def test_concentration_threshold(self):
        rows = [week("a", "2026-09-14", "r1", 39.0), week("a", "2026-09-14", "r2", 31.0),
                week("a", "2026-09-14", "r3", 30.0)]
        self.assertNotIn("concentration", [f["rule"] for f in _findings(people=self.PEOPLE, weekly=rows)])

    def test_lock_outs_are_one_finding_worst_first(self):
        people = [{"person": f"p{i}", "display_name": f"P{i}"} for i in range(7)]
        limits = [{"person": f"p{i}", "locked_out": True, "blocked_hours": 10 + i, "limit_type": "spend_cap"}
                  for i in range(7)]
        res = [f for f in _findings(people=people, limits=limits, ) if f["rule"] == "lockout"]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["text"], "P6 (16.0 h), P5 (15.0 h), P4 (14.0 h) and 4 more "
                                         "were locked out until a limit reset.")

    def test_two_lock_outs_read_as_a_list(self):
        res = _findings(people=self.PEOPLE, limits=[
            {"person": "a", "locked_out": True, "blocked_hours": 59, "limit_type": "x"},
            {"person": "b", "locked_out": True, "blocked_hours": 3, "limit_type": "x"}])
        self.assertEqual(res[0]["text"], "Ann (59.0 h), Bo (3.0 h) were locked out until a limit reset.")

    def test_short_lock_out_is_in_minutes(self):
        res = _findings(people=self.PEOPLE, limits=[
            {"person": "a", "locked_out": True, "blocked_hours": 0.5, "limit_type": "x"}])
        self.assertEqual(res[0]["text"], "Ann (30 min) was locked out until a limit reset.")

    def test_a_built_in_tool_is_not_a_failing_integration(self):
        tools = [["a", "2026-09-14", "Glob", "built-in", 36, 6],
                 ["a", "2026-09-14", "Bash", "built-in", 40, 20]]
        self.assertEqual([f for f in _findings(people=self.PEOPLE, tools=tools) if f["rule"] == "integration"], [])
        tools = [["a", "2026-09-14", "x", "claude_ai_Tracker", 45, 13]]
        self.assertEqual(len([f for f in _findings(people=self.PEOPLE, tools=tools)
                              if f["rule"] == "integration"]), 1)

    def test_a_stale_second_machine_is_named(self):
        sources = [{"person": "a", "machine_id": "A", "exported_at": "2026-10-13T09:00:00+00:00"},
                   {"person": "a", "machine_id": "B", "exported_at": "2026-09-14T09:00:00+00:00"},
                   {"person": "b", "machine_id": "C", "exported_at": "2026-10-13T09:00:00+00:00"}]
        res = [f for f in _findings(people=self.PEOPLE, sources=sources) if f["rule"] == "stale"]
        self.assertEqual(len(res), 1)
        self.assertIn("Ann", res[0]["text"])
        self.assertNotIn("Bo", res[0]["text"])
        self.assertEqual(res[0]["text"].count("Ann"), 1)

    def test_stale_uses_whole_days_from_utc_midnight(self):
        # 2026-10-06T23:00Z is 7.04 days before 2026-10-14T00:00Z -> stale; same-day export is not.
        sources = [{"person": "a", "machine_id": "A", "exported_at": "2026-10-06T23:00:00+00:00"}]
        self.assertEqual(len([f for f in _findings(people=self.PEOPLE, sources=sources)
                              if f["rule"] == "stale"]), 1)
        sources = [{"person": "a", "machine_id": "A", "exported_at": "2026-10-07T01:00:00+00:00"}]
        self.assertEqual([f for f in _findings(people=self.PEOPLE, sources=sources)
                          if f["rule"] == "stale"], [])

    def test_failing_integration_excludes_claude_code_tools(self):
        tools = [["a", "2026-09-14", "x", "claude_ai_Tracker", 45, 13],
                 ["a", "2026-09-14", "TaskStop", "built-in", 40, 10],
                 ["a", "2026-09-14", "Bash", "built-in", 19, 10]]
        res = [f for f in _findings(people=self.PEOPLE, tools=tools) if f["rule"] == "integration"]
        self.assertEqual(len(res), 1)
        self.assertIn("claude_ai_Tracker", res[0]["text"])

    def test_high_spend_per_active_day(self):
        # 250 over 10 project-days is $25/day; capped at the week's 7 days it is $35.7/day.
        rows = [week("a", "2026-09-14", "r1", 250.0, days=10, wdays=7)]
        res = [f for f in _findings(people=self.PEOPLE, weekly=rows) if f["rule"] == "per_day"]
        self.assertEqual(len(res), 1)
        self.assertIn("Ann", res[0]["text"])
        self.assertIn("$36/day", res[0]["text"])

    def test_context_tax_needs_a_measured_value(self):
        health = [{"person": "a", "very_long_context_cost_usd": None, "est_cost_main_usd": 100.0},
                  {"person": "b", "very_long_context_cost_usd": 20.0, "est_cost_main_usd": 100.0}]
        res = [f for f in _findings(people=self.PEOPLE, health=health) if f["rule"] == "context"]
        self.assertEqual(len(res), 1)
        self.assertIn("Bo", res[0]["text"])
        self.assertNotIn("Ann", res[0]["text"])

    def test_idle_and_on_pace(self):
        people = [{"person": "a", "display_name": "Ann", "idle_in_range": True},
                  {"person": "b", "display_name": "Bo", "allowance_status": "on_pace_to_hit", "snapshot_in_range": True}]
        rules = [f["rule"] for f in _findings(people=people)]
        self.assertIn("idle", rules)
        self.assertIn("on_pace", rules)

    def test_on_pace_is_omitted_when_the_snapshot_is_outside_the_range(self):
        people = [{"person": "b", "display_name": "Bo", "allowance_status": "over", "snapshot_in_range": False},
                  {"person": "c", "display_name": "Cy", "allowance_status": "over"}]
        self.assertNotIn("on_pace", [f["rule"] for f in _findings(people=people)])

    def test_stale_export(self):
        sources = [{"person": "a", "exported_at": "2026-10-01T09:00:00+00:00"},
                   {"person": "b", "exported_at": "2026-10-13T09:00:00+00:00"}]
        res = [f for f in _findings(people=self.PEOPLE, sources=sources) if f["rule"] == "stale"]
        self.assertEqual(len(res), 1)
        self.assertIn("Ann", res[0]["text"])

    def test_order_follows_the_spec(self):
        people = [{"person": "a", "display_name": "Ann", "idle_in_range": True,
                   "allowance_status": "over", "snapshot_in_range": True}]
        res = _findings(people=people, limits=[{"person": "a", "locked_out": True, "blocked_hours": 2}])
        self.assertEqual([f["rule"] for f in res], ["lockout", "on_pace", "idle"])


class TestPolish(unittest.TestCase):
    """Deferred review items: wording, thresholds in the text, the Other bucket."""

    PEOPLE = [{"person": "a", "display_name": "Ann"}, {"person": "b", "display_name": "Bo"}]

    def test_a_zero_hour_lock_out_is_under_a_minute(self):
        res = _findings(people=self.PEOPLE, limits=[{"person": "a", "locked_out": True, "blocked_hours": 0}])
        self.assertIn("Ann (under a minute)", res[0]["text"])
        self.assertNotIn("0 min", res[0]["text"])

    def test_finding_text_follows_the_thresholds(self):
        health = [{"person": "b", "very_long_context_cost_usd": 30.0, "est_cost_main_usd": 100.0}]
        people = [{"person": "a", "display_name": "Ann"}, {"person": "b", "display_name": "Bo"}]
        snippet = ("FINDING.contextShare = 0.25; FINDING.contextTokens = 300000;"
                   f"out(findings({_input(people=people, health=health)}, '2026-10-14'));")
        text = " ".join(f["text"] for f in run_js(F, snippet))
        self.assertIn("25% or more", text)
        self.assertIn("over 300k tokens", text)

    def test_unattributed_spend_does_not_drive_concentration(self):
        rows = [week("a", "2026-09-14", "", 70.0, days=7), week("a", "2026-09-14", "r1", 20.0, days=7),
                week("a", "2026-09-14", "r2", 10.0, days=7)]
        self.assertNotIn("concentration", [f["rule"] for f in _findings(people=self.PEOPLE, weekly=rows)])
        rows = [week("a", "2026-09-14", "", 50.0, days=7), week("a", "2026-09-14", "r1", 40.0, days=7),
                week("a", "2026-09-14", "r2", 10.0, days=7)]
        res = [f for f in _findings(people=self.PEOPLE, weekly=rows) if f["rule"] == "concentration"]
        self.assertEqual(len(res), 1)
        self.assertIn("is in r1", res[0]["text"])

    def test_the_other_bucket_is_marked_and_counted(self):
        rows = [week("a", "2026-09-14", f"r{i}", 100.0 - i) for i in range(10)]
        rows.append(week("a", "2026-09-14", "Other", 95.5))  # a real repo named Other, in the top 8
        res = run_js(F, f"out(repoSpend({json.dumps(rows)}, 8));")["repos"]
        real = [r for r in res if r["repo"] == "Other" and not r.get("other")]
        bucket = [r for r in res if r.get("other")]
        self.assertEqual(len(real), 1)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]["count"], 3)
        self.assertEqual(bucket[0]["repo"], "Other")


class TestModelLabels(unittest.TestCase):
    def test_readable_names(self):
        cases = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5": "Sonnet 5", "claude-haiku-4-5": "Haiku 4.5",
                 "claude-fable-5-1": "Fable 5.1", "claude-opus-4-1-20250805": "Opus 4.1",
                 "claude-opus-5-5[1m]": "Opus 5.5", "some-other-model": "some-other-model", "": "(unknown)"}
        for raw, want in cases.items():
            self.assertEqual(js(f"modelLabel({json.dumps(raw)})"), want, raw)


def wk(person, w, cost, active=1, wdays=7, exact=None, sub=0.0, retries=0, sessions=1, repo="r"):
    """An 18-field weekly row (team.team_payload's layout)."""
    return [person, w, repo, cost, 0, 1, 1, active, False, False, None, None, 0.0, sessions, wdays,
            sub, retries, exact]


def in_range(person, rows, rng, window=("2026-09-15", "2026-10-14"), price=None, limits=(), prs=(), prices=None):
    p = {"person": person, "window_from": window[0], "window_to": window[1], "seat_price": price}
    return run_js(F, f"out(personInRange({json.dumps(p)}, {json.dumps(rows)}, {json.dumps(list(limits))}, "
                     f"{json.dumps(list(prs))}, {json.dumps(rng)}, {json.dumps(prices)}));")


RANGE = {"from": "2026-09-14", "to": "2026-10-04"}


class TestRangeDates(unittest.TestCase):
    def test_overlap_counts_days_and_workdays(self):
        self.assertEqual(js("overlap('2026-09-14','2026-09-20','2026-09-16','2026-10-01')"),
                         {"from": "2026-09-16", "to": "2026-09-20", "days": 5, "workdays": 3})
        self.assertIsNone(js("overlap('2026-09-14','2026-09-20','2026-09-21','2026-10-01')"))

    def test_monday_and_add_days(self):
        self.assertEqual(js("mondayOfDay('2026-10-11')"), "2026-10-05")
        self.assertEqual(js("mondayOfDay('2026-10-12')"), "2026-10-12")
        self.assertEqual(js("isoAddDays('2026-02-28', 1)"), "2026-03-01")

    def test_snapshot_in_range(self):
        self.assertTrue(js("snapshotInRange({window_to:'2026-10-14'}, {from:'2026-10-12', to:'2026-10-18'})"))
        self.assertFalse(js("snapshotInRange({window_to:'2026-10-14'}, {from:'2026-10-05', to:'2026-10-11'})"))


def run_rate(limits, weeks, coverage_to, today="2026-10-15", coverage_from=None):
    return js(f"rangeRunRate({json.dumps(limits)}, {json.dumps(weeks)}, {json.dumps(coverage_to)}, "
              f"{json.dumps(today)}, {json.dumps(coverage_from)})")


def weekly_limit(day, usd, limit_type="weekly"):
    return {"person": "a", "limit_type": limit_type, "hit_at": day + "T10:00:00Z", "usage_after_hit_usd": usd}


class TestRangeRunRate(unittest.TestCase):
    WEEKS = ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]

    def test_three_basis_episodes_average_the_basis_weeks(self):
        got = run_rate([weekly_limit("2026-09-08", 60), weekly_limit("2026-09-15", 30), weekly_limit("2026-09-22", 30)],
                       self.WEEKS, "2026-10-01")
        # complete: Aug 31 .. Sep 21 (Sep 28 ends after the export); basis = all four
        self.assertEqual(got["basisWeeks"], 4)
        self.assertEqual(got["episodes"], 3)
        self.assertAlmostEqual(got["rate"], 30.0)
        self.assertEqual(got["weekly"][1], ["2026-09-07", 60])
        self.assertEqual(got["weekly"][4], ["2026-09-28", 0])

    def test_weeks_before_the_exports_begin_are_unknown_not_zero(self):
        # Ann's export begins on Monday Sep 14; the range starts on Aug 31.
        got = run_rate([weekly_limit("2026-09-15", 60), weekly_limit("2026-09-22", 30)],
                       self.WEEKS, "2026-10-04", coverage_from="2026-09-14")
        self.assertEqual(got["basisWeeks"], 3)       # Sep 14, Sep 21, Sep 28
        self.assertAlmostEqual(got["rate"], 30.0)    # (60 + 30 + 0) / 3, not over Aug 31 / Sep 7 as $0
        self.assertEqual([w for w, _ in got["weekly"]], self.WEEKS[2:])
        got = run_rate([weekly_limit("2026-09-15", 60), weekly_limit("2026-09-22", 30)],
                       ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"], "2026-10-04", coverage_from="2026-09-14")
        self.assertEqual(got["basisWeeks"], 2)
        self.assertAlmostEqual(got["rate"], 45.0)    # (60 + 30) / 2

    def test_the_basis_is_the_last_four_complete_weeks(self):
        got = run_rate([weekly_limit("2026-09-01", 1000), weekly_limit("2026-09-08", 8)],
                       self.WEEKS, "2026-10-04")
        self.assertEqual(got["basisWeeks"], 4)       # Sep 7 .. Sep 28
        self.assertEqual(got["episodes"], 1)         # the Aug 31 week is not in the basis
        self.assertAlmostEqual(got["rate"], 2.0)

    def test_a_week_after_today_or_the_exports_is_not_complete(self):
        self.assertEqual(run_rate([], self.WEEKS, "2026-10-04", today="2026-09-20")["basisWeeks"], 2)
        self.assertEqual(run_rate([], self.WEEKS, "2026-09-13")["basisWeeks"], 2)
        got = run_rate([], self.WEEKS, None)
        self.assertEqual((got["basisWeeks"], got["rate"], got["episodes"]), (0, None, 0))

    def test_only_weekly_limits_with_extra_usage_are_episodes(self):
        got = run_rate([weekly_limit("2026-09-08", 0), weekly_limit("2026-09-09", 5, "session"),
                        weekly_limit("2026-09-15", 7)], self.WEEKS, "2026-10-04")
        self.assertEqual(got["episodes"], 1)
        self.assertEqual(got["weekly"][1], ["2026-09-07", 0])

    def test_hit_at_is_placed_by_its_utc_date(self):
        got = run_rate([weekly_limit("2026-09-14", 9)], self.WEEKS, "2026-10-04")
        self.assertEqual(got["weekly"][2], ["2026-09-14", 9])


class TestPersonInRange(unittest.TestCase):
    def test_exact_seat_use(self):
        rows = [wk("a", "2026-09-14", 10, exact=4), wk("a", "2026-09-21", 10, exact=5),
                wk("a", "2026-09-28", 10, exact=2)]
        r = in_range("a", rows, RANGE)
        self.assertEqual(r["activeDays"], 11)
        self.assertEqual(r["workdays"], 14)  # Tue Sep 15 .. Sun Oct 4: 4 + 5 + 5 weekdays
        self.assertAlmostEqual(r["seatUse"], 11 / 14)
        self.assertFalse(r["approx"])
        self.assertEqual(r["sessions"], 3)
        self.assertAlmostEqual(r["cost"], 30)
        self.assertEqual(r["lastActiveWeek"], "2026-09-28")
        self.assertFalse(r["idle"])

    def test_mixed_exact_and_approx(self):
        rows = [wk("a", "2026-09-14", 5, exact=3), wk("a", "2026-09-21", 5, active=9, wdays=7)]
        r = in_range("a", rows, RANGE)
        self.assertEqual(r["activeDays"], 10)  # 3 exact + min(9, 7)
        self.assertTrue(r["approx"])

    def test_two_machines_take_the_larger(self):
        rows = [wk("a", "2026-09-14", 5, exact=2, repo="x"), wk("a", "2026-09-14", 5, exact=4, repo="y")]
        self.assertEqual(in_range("a", rows, RANGE)["activeDays"], 4)

    def test_one_week_exact_on_one_machine_and_not_the_other(self):
        rows = [wk("a", "2026-09-14", 5, active=5, exact=2, repo="x"),
                wk("a", "2026-09-14", 5, active=5, exact=None, repo="y")]
        r = in_range("a", rows, RANGE)
        self.assertEqual(r["activeDays"], 5)  # the larger of exact 2 and the fallback min(5, 7)
        self.assertTrue(r["approx"])

    def test_exact_beats_a_smaller_fallback(self):
        rows = [wk("a", "2026-09-14", 5, active=2, exact=4, repo="x"),
                wk("a", "2026-09-14", 5, active=2, exact=None, repo="y")]
        r = in_range("a", rows, RANGE)
        self.assertEqual(r["activeDays"], 4)
        self.assertFalse(r["approx"])

    def test_seat_cost_per_year(self):
        rng = {"from": "2026-09-14", "to": "2026-09-20"}
        r = in_range("a", [wk("a", "2026-09-14", 50, exact=3)], rng, price=2400,
                     window=("2026-09-01", "2026-09-30"), prices={"per": "year", "prices": {}})
        self.assertAlmostEqual(r["seatCost"], 2400 / 365.25 * 7)

    def test_range_outside_window(self):
        rng = {"from": "2026-08-03", "to": "2026-08-30"}
        r = in_range("a", [], rng, price=200, prices={"per": "month", "prices": {}})
        self.assertFalse(r["covered"])
        self.assertEqual(r["workdays"], 0)
        for key in ("idle", "sessions", "cost", "subCost", "subShare", "prs", "retries", "limitHits",
                    "lockouts", "extra", "activeDays", "lastActiveWeek", "seatUse", "seatCost", "value"):
            self.assertIsNone(r[key], key)

    def test_a_covered_range_is_marked_covered(self):
        r = in_range("a", [wk("a", "2026-09-14", 10, exact=1)], RANGE)
        self.assertTrue(r["covered"])

    def test_a_person_without_a_window_is_not_covered(self):
        p = {"person": "a"}
        r = run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], {json.dumps(RANGE)}, null));")
        self.assertFalse(r["covered"])
        self.assertIsNone(r["cost"])

    def test_seat_cost_and_value(self):
        rows = [wk("a", "2026-09-14", 100, exact=3)]
        limits = [{"limit_type": "weekly", "usage_after_hit_usd": 20.0},
                  {"limit_type": "session", "usage_after_hit_usd": 99.0}]
        r = in_range("a", rows, RANGE, price=200, limits=limits, prices={"per": "month", "prices": {}})
        seat = 200 / (365.25 / 12) * 20  # Sep 15 .. Oct 4 is 20 days
        self.assertAlmostEqual(r["seatCost"], seat)
        self.assertEqual(r["extra"], 20.0)
        self.assertAlmostEqual(r["value"], 100 / (seat + 20))

    def test_no_price_means_no_seat_cost(self):
        r = in_range("a", [wk("a", "2026-09-14", 100, exact=3)], RANGE, prices=None)
        self.assertIsNone(r["seatCost"])
        self.assertIsNone(r["value"])

    def test_limits_and_retries_and_subagents(self):
        limits = [{"limit_type": "weekly", "locked_out": True, "blocked_hours": 2.5},
                  {"limit_type": "session", "locked_out": True, "blocked_hours": 1.0},
                  {"limit_type": "session", "locked_out": False, "blocked_hours": 0}]
        rows = [wk("a", "2026-09-14", 40, sub=10, retries=3), wk("a", "2026-09-21", 0.0, retries=2)]
        r = in_range("a", rows, RANGE, limits=limits, prs=[{}, {}])
        self.assertEqual(r["limitHits"], 3)
        self.assertEqual(r["lockouts"], 2)
        self.assertAlmostEqual(r["lockedHours"], 3.5)
        self.assertEqual(r["prs"], 2)
        self.assertEqual(r["retries"], 5)
        self.assertAlmostEqual(r["subShare"], 0.25)
        self.assertEqual(r["lastActiveWeek"], "2026-09-14")

    def test_idle_when_no_cost(self):
        r = in_range("a", [wk("a", "2026-09-14", 0.0)], RANGE)
        self.assertTrue(r["idle"])
        self.assertIsNone(r["subShare"])
        self.assertIsNone(r["lastActiveWeek"])


class TestOneActiveDaysRule(unittest.TestCase):
    """personInRange, activeDaysByPerson and the heatmap share weekActiveDays."""

    def test_week_active_days_helper(self):
        rows = [wk("a", "2026-09-14", 1, active=5, exact=2, repo="x"),
                wk("a", "2026-09-14", 1, active=3, exact=None, repo="y")]
        self.assertEqual(js(f"weekActiveDays({json.dumps(rows)})"), {"days": 3, "approx": True})
        rows = [wk("a", "2026-09-14", 1, active=2, exact=4, repo="x"),
                wk("a", "2026-09-14", 1, active=2, exact=None, repo="y")]
        self.assertEqual(js(f"weekActiveDays({json.dumps(rows)})"), {"days": 4, "approx": False})
        self.assertEqual(js("weekActiveDays([])"), {"days": 0, "approx": False})

    def test_exact_days_win_in_active_days_by_person(self):
        # Two projects of 5 days each would sum to 10 and cap at 7; the exact count is 4.
        rows = [wk("a", "2026-09-14", 10, active=5, exact=4, repo="x"),
                wk("a", "2026-09-14", 10, active=5, exact=4, repo="y")]
        self.assertEqual(run_js(F, f"out(activeDaysByPerson({json.dumps(rows)}));"), {"a": 4})
        res = run_js(F, f"out(perActiveDay({json.dumps(rows)}));")
        self.assertEqual(res[0]["days"], 4)
        self.assertAlmostEqual(res[0]["perDay"], 5.0)

    def test_idle_in_range_drives_the_idle_finding(self):
        people = [{"person": "a", "display_name": "Ann", "idle_in_range": True, "seat_idle": "no"},
                  {"person": "b", "display_name": "Bo", "idle_in_range": False, "seat_idle": "yes"}]
        res = [f for f in _findings(people=people) if f["rule"] == "idle"]
        self.assertEqual(len(res), 1)
        self.assertIn("Ann", res[0]["text"])
        self.assertNotIn("Bo", res[0]["text"])
        self.assertIn("no Claude Code activity in these weeks", res[0]["text"])

    def test_no_idle_finding_for_an_uncovered_person(self):
        # team.js passes idle_in_range only when the person is covered and idle.
        people = [{"person": "a", "display_name": "Ann", "idle_in_range": False}]
        self.assertEqual([f for f in _findings(people=people) if f["rule"] == "idle"], [])


def bounded(person_rows, rng, p_extra, intervals=None, window=("2026-09-14", "2026-09-27"), all_rows=None):
    """personInRange with the person's own total and every weekly row, as team.js passes them."""
    p = dict({"person": "a", "window_from": window[0], "window_to": window[1], "seat_price": None}, **p_extra)
    opts = {"allRows": all_rows if all_rows is not None else person_rows}
    if intervals is not None:
        opts["intervals"] = intervals
    return run_js(F, f"out(personInRange({json.dumps(p)}, {json.dumps(person_rows)}, [], [], "
                     f"{json.dumps(rng)}, null, {json.dumps(opts)}));")


# One machine, an export from before exact days: Sep 14 - Sep 27 has 10 workdays and
# 8 active days. Every day of both weeks had work in 3 projects (3 days each), so the
# summed project days cap at 7 a week (14) while the busiest project says 3 (6).
OLD_ONE_MACHINE = {"machines": 1, "person_active_days": 8, "window_workdays": 10}
THREE_PROJECTS = [wk("a", w, 5, active=3, repo=r) for w in ("2026-09-14", "2026-09-21") for r in "xyz"]
BOTH_WEEKS = {"from": "2026-09-14", "to": "2026-09-27"}


class TestSeatUseBounds(unittest.TestCase):
    """An export from before exact days still carries the person's own total for its
    window; with the busiest project's days and the capped sum as bounds per week,
    that pins or brackets any selected weeks (F1)."""

    def test_all_weeks_reproduce_the_exports_own_total_exactly(self):
        r = bounded(THREE_PROJECTS, BOTH_WEEKS, OLD_ONE_MACHINE)
        self.assertEqual(r["activeDays"], 8)
        self.assertFalse(r["approx"])
        self.assertAlmostEqual(r["seatUse"], 0.8)

    def test_a_partial_range_that_the_bounds_pin_is_exact(self):
        # Week 1 has one project of 4 days (the busiest and the sum agree); week 2 is
        # 3 projects of 3 days. Total 8: week 1 can only be 4.
        rows = [wk("a", "2026-09-14", 5, active=4, repo="x")] + [wk("a", "2026-09-21", 5, active=3, repo=r) for r in "xyz"]
        r = bounded([rows[0]], {"from": "2026-09-14", "to": "2026-09-20"}, OLD_ONE_MACHINE, all_rows=rows)
        self.assertEqual(r["activeDays"], 4)
        self.assertFalse(r["approx"])

    def test_one_week_is_bounded_and_marked_approximate(self):
        week1 = [r for r in THREE_PROJECTS if r[1] == "2026-09-14"]
        r = bounded(week1, {"from": "2026-09-14", "to": "2026-09-20"}, OLD_ONE_MACHINE, all_rows=THREE_PROJECTS)
        # lo = max(3, 8 - 7) = 3; hi = min(7, 8 - 3) = 5; the share-of-total estimate is 4.
        self.assertEqual((r["activeLo"], r["activeHi"]), (3, 5))
        self.assertEqual(r["activeDays"], 4)
        self.assertTrue(r["approx"])

    def test_a_week_the_total_pins_is_exact(self):
        # Week 1 summed 7, the busiest project 6; week 2 is one project of 1 day. E = 7
        # leaves week 2 at least 1 (and at most 1).
        rows = [wk("a", "2026-09-14", 5, active=6, repo="x"), wk("a", "2026-09-14", 5, active=2, repo="y"),
                wk("a", "2026-09-21", 5, active=1, repo="x")]
        week2 = [rows[2]]
        r = bounded(week2, {"from": "2026-09-21", "to": "2026-09-27"},
                    dict(OLD_ONE_MACHINE, person_active_days=7), all_rows=rows)
        self.assertEqual(r["activeDays"], 1)
        self.assertFalse(r["approx"])

    def test_two_machines_keep_the_capped_fallback(self):
        r = bounded(THREE_PROJECTS, BOTH_WEEKS, dict(OLD_ONE_MACHINE, machines=2))
        self.assertEqual(r["activeDays"], 14)
        self.assertTrue(r["approx"])
        self.assertIsNone(r["activeLo"])

    def test_a_total_that_does_not_cover_the_windows_keeps_the_fallback(self):
        r = bounded(THREE_PROJECTS, BOTH_WEEKS, dict(OLD_ONE_MACHINE, window_workdays=15))
        self.assertEqual(r["activeDays"], 14)
        self.assertTrue(r["approx"])

    def test_exact_weeks_count_exactly_and_the_rest_share_what_is_left(self):
        # Week 1 is exact (2 days); week 2 has 3 projects of 3 days. E = 8 leaves 6 for week 2.
        rows = [wk("a", "2026-09-14", 5, exact=2)] + [wk("a", "2026-09-21", 5, active=3, repo=r) for r in "xyz"]
        r = bounded(rows, BOTH_WEEKS, OLD_ONE_MACHINE)
        self.assertEqual(r["activeDays"], 8)
        self.assertFalse(r["approx"])
        r = bounded(rows[1:], {"from": "2026-09-21", "to": "2026-09-27"}, OLD_ONE_MACHINE, all_rows=rows)
        self.assertEqual(r["activeDays"], 6)
        self.assertFalse(r["approx"])

    def test_active_days_by_person_uses_the_same_estimates(self):
        week1 = [r for r in THREE_PROJECTS if r[1] == "2026-09-14"]
        p = dict({"person": "a", "window_from": "2026-09-14", "window_to": "2026-09-27"}, **OLD_ONE_MACHINE)
        got = js(f"(() => {{ const b = activeBasis([{json.dumps(p)}], {json.dumps(THREE_PROJECTS)}, {{}}); "
                 f"return [activeDaysByPerson({json.dumps(THREE_PROJECTS)}, b), "
                 f"activeDaysByPerson({json.dumps(week1)}, b), activeDaysByPerson({json.dumps(week1)})]; }})()")
        self.assertEqual(got, [{"a": 8}, {"a": 4}, {"a": 7}])   # without a basis: the capped sum

    def test_per_week_figure_is_marked_when_estimated(self):
        p = dict({"person": "a", "window_from": "2026-09-14", "window_to": "2026-09-27"}, **OLD_ONE_MACHINE)
        got = js(f"(() => {{ const b = activeBasis([{json.dumps(p)}], {json.dumps(THREE_PROJECTS)}, {{}}); "
                 f"return weekDaysFigure(b['a'], '2026-09-14', []); }})()")
        self.assertEqual((got["days"], got["approx"]), (4, True))
        self.assertLessEqual(got["lo"], 4)      # the range the cell's tooltip gives (T7)
        self.assertGreaterEqual(got["hi"], 4)


class TestCoverageIntervals(unittest.TestCase):
    """A person's coverage is the union of their own exports' windows (F2)."""

    GAPPED = [("2026-08-03", "2026-08-16"), ("2026-09-28", "2026-10-11")]
    P = {"machines": 1, "person_active_days": 8, "window_workdays": 20}

    def _range(self, rng):
        iv = [{"from": a, "to": b} for a, b in self.GAPPED]
        return bounded([], rng, self.P, intervals=iv, window=("2026-08-03", "2026-10-11"), all_rows=[])

    def test_intervals_merge_overlapping_and_adjacent_windows(self):
        sources = [{"person": "a", "window_from": "2026-08-03", "window_to": "2026-08-16"},
                   {"person": "a", "window_from": "2026-08-17", "window_to": "2026-08-23"},
                   {"person": "a", "window_from": "2026-08-20", "window_to": "2026-08-30"},
                   {"person": "a", "window_from": "2026-09-28", "window_to": "2026-10-11"},
                   {"person": "b", "window_from": "2026-09-01", "window_to": "2026-09-07"},
                   {"person": "c", "window_from": None, "window_to": None}]
        got = js(f"coverageIntervals({json.dumps(sources)})")
        self.assertEqual(got["a"], [{"from": "2026-08-03", "to": "2026-08-30"},
                                    {"from": "2026-09-28", "to": "2026-10-11"}])
        self.assertEqual(got["b"], [{"from": "2026-09-01", "to": "2026-09-07"}])
        self.assertNotIn("c", got)

    def test_a_range_inside_the_gap_is_not_covered(self):
        r = self._range({"from": "2026-08-24", "to": "2026-09-20"})
        self.assertFalse(r["covered"])
        self.assertIsNone(r["seatUse"])
        self.assertIsNone(r["idle"])
        self.assertEqual(r["workdays"], 0)

    def test_all_counts_only_the_covered_days(self):
        iv = [{"from": a, "to": b} for a, b in self.GAPPED]
        p = dict({"person": "a", "window_from": "2026-08-03", "window_to": "2026-10-11", "seat_price": 300}, **self.P)
        r = run_js(F, f"out(personInRange({json.dumps(p)}, [], [], [], "
                      f"{json.dumps({'from': '2026-08-03', 'to': '2026-10-11'})}, "
                      f"{json.dumps({'per': 'month', 'prices': {}})}, {json.dumps({'intervals': iv, 'allRows': []})}));")
        self.assertTrue(r["covered"])
        self.assertEqual(r["workdays"], 20)
        self.assertAlmostEqual(r["seatCost"], 300 / (365.25 / 12) * 28)

    def test_without_intervals_the_persons_window_is_the_coverage(self):
        r = in_range("a", [wk("a", "2026-09-14", 10, exact=1)], RANGE)
        self.assertTrue(r["covered"])
        self.assertEqual(r["workdays"], 14)

    def test_a_week_is_covered_when_any_interval_touches_it(self):
        iv = [{"from": a, "to": b} for a, b in self.GAPPED]
        got = js(f"[coveredWeek({json.dumps(iv)}, '2026-08-10'), coveredWeek({json.dumps(iv)}, '2026-08-24'), "
                 f"coveredWeek({json.dumps(iv)}, '2026-09-21'), coveredWeek({json.dumps(iv)}, '2026-09-28')]")
        self.assertEqual(got, [True, False, False, True])


class TestTeamValue(unittest.TestCase):
    """Value per seat for the team: everyone's usage over everyone's seat cost
    plus extra usage, not the median of each person's multiple."""

    def test_sums_before_dividing(self):
        ranged = [{"cost": 900, "seatCost": 100, "extra": 0}, {"cost": 10, "seatCost": 100, "extra": 0},
                  {"cost": 20, "seatCost": 90, "extra": 10}]
        self.assertAlmostEqual(js(f"teamValue({json.dumps(ranged)})"), 930 / 300)

    def test_a_person_without_a_seat_cost_counts_on_neither_side(self):
        # unpriced, or not covered by the selected weeks
        ranged = [{"cost": 100, "seatCost": 50, "extra": 0}, {"cost": 999, "seatCost": None, "extra": 0},
                  {"cost": None, "seatCost": None, "extra": None}]
        self.assertAlmostEqual(js(f"teamValue({json.dumps(ranged)})"), 2)

    def test_nobody_with_a_seat_cost_has_no_value(self):
        self.assertIsNone(js("teamValue([])"))
        self.assertIsNone(js("teamValue([{cost: 5, seatCost: null, extra: 0}])"))
