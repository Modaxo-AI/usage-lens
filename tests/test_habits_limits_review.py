"""Review fixes for the Overview's Habits strip and its "Usage limits & friction"
panel: every figure correct and honestly labelled. Each class is one finding
from an adversarial review on real logs.

JS runs under node against a stub DOM (tests/jsrun.py); the allowance and the
payload are plain Python.
"""
import datetime as dt
import json
import os
import re
import unittest

from claude_usage import limits
from tests.jsrun import REPO, run_js
from tests.test_health_ui import DASHBOARD, DAY, _dashboard, _session, _usage


def _read(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


def _strip(setup):
    return run_js(DASHBOARD, setup + "renderHabitStrip(); out(el('habitsStrip').innerHTML)")


def _cards(setup):
    snippet = (setup + f"const w = {{from: '{DAY}', to: '{DAY}'}};"
               "const r = habitMeasures(w), b = habitMeasures(habitWindows().baseline);"
               "out({r: {worstTool: r.worstTool, sessions: r.sessions.length, withSubagents: r.withSubagents},"
               " cards: habitCards(r, b)});")
    return run_js(DASHBOARD, snippet)


def _card(cards, label):
    return next(c for c in cards["cards"] if c["label"] == label)


def _tile(html, label):
    """(value html, sub text) of the stat tile called `label`."""
    part = html.split(f'<div class="label">{label}</div><div class="value">')[1]
    value, rest = part.split("</div>", 1)
    sub = rest.split('<div class="sub">')[1].split("</div>")[0] if rest.startswith('<div class="sub">') else ""
    return value, sub


# --------------------------------------------------------------------------- #
# 1. Friction: the worst tool
# --------------------------------------------------------------------------- #

class TestFrictionWorstTool(unittest.TestCase):
    def _tools(self, rows):
        return [[f"{DAY}T10:00", "s1", name, calls, errors] for name, calls, errors in rows]

    def _setup(self, rows):
        return _dashboard(sessions=[_session("s1")], usage=[_usage(DAY, "s1", 5.0)], tools=self._tools(rows))

    def test_a_tool_name_that_never_succeeded_is_not_the_worst_tool(self):
        # Real logs: a lowercase `bash` the model invented, 17 calls, all failing,
        # against the real Bash with 79% of the errors.
        res = _cards(self._setup([("bash", 17, 17), ("Bash", 1000, 60), ("Read", 100, 2)]))
        self.assertEqual(res["r"]["worstTool"]["name"], "Bash")

    def test_ranked_by_error_count_not_by_rate(self):
        # Edit fails more often, but Bash accounts for far more of the errors.
        res = _cards(self._setup([("Edit", 20, 8), ("Bash", 1000, 60)]))
        self.assertEqual(res["r"]["worstTool"]["name"], "Bash")

    def test_only_a_tool_that_never_succeeded_leaves_no_worst_tool(self):
        res = _cards(self._setup([("bash", 17, 17), ("Read", 100, 0)]))
        self.assertIsNone(res["r"]["worstTool"])
        self.assertNotIn("bash", _card(res, "Friction")["sub"])

    def test_the_callout_names_the_real_tool_and_its_share_of_errors(self):
        res = _cards(self._setup([("bash", 17, 17), ("Bash", 1000, 60), ("Read", 100, 2)]))
        sub = _card(res, "Friction")["sub"]
        self.assertIn("Bash", sub)
        self.assertNotIn("bash fails", sub)
        self.assertNotIn("100.0%", sub)
        self.assertRegex(sub, r"\d+(?:\.\d)?% of the errors")

    def test_the_fix_does_not_present_our_guess_as_a_fact(self):
        res = _cards(self._setup([("Bash", 1000, 60)]))
        fix = _card(res, "Friction")["fix"]
        self.assertNotIn("usually an integration or permission to fix, not a habit", fix)
        self.assertRegex(fix, r"(?i)our suggestion|may|might")


# --------------------------------------------------------------------------- #
# 2. Habit denominators count sessions that did work
# --------------------------------------------------------------------------- #

class TestHabitDenominators(unittest.TestCase):
    def _sessions(self):
        with_work = _session("s1")
        with_work["requests"] = 5
        with_work["n_subagents"] = 1
        stub = _session("s2")
        stub["requests"] = 0            # opened and closed: not a session
        other = _session("s3")
        other["requests"] = 2
        return [with_work, stub, other]

    def test_a_transcript_with_no_requests_is_not_a_session(self):
        res = _cards(_dashboard(sessions=self._sessions(), usage=[_usage(DAY, "s1", 5.0)]))
        self.assertEqual(res["r"]["sessions"], 2)
        value = _card(res, "Workflow")["value"]
        self.assertIn("of 2 used subagents", value)

    def test_the_baseline_denominator_follows_the_same_rule(self):
        base = "2026-02-09"
        old_ok = _session("b1", day=base)
        old_ok["requests"] = 3
        old_ok["n_subagents"] = 1
        old_stub = _session("b2", day=base)
        old_stub["requests"] = 0
        setup = _dashboard(sessions=[old_ok, old_stub], usage=[_usage(base, "b1", 5.0)])
        res = run_js(DASHBOARD, setup + "out(habitMeasures(habitWindows().baseline).sessions.length)")
        self.assertEqual(res, 1)

    def test_recent_sessions_skip_a_transcript_that_did_no_work(self):
        html = run_js(DASHBOARD, _dashboard(sessions=self._sessions()) +
                      "renderRecentSessions(); out(el('habitRecent').innerHTML)")
        self.assertEqual(html.count('class="recent-session"'), 2)


# --------------------------------------------------------------------------- #
# 3 and 4. The panel's tiles
# --------------------------------------------------------------------------- #

def _limits_setup(friction=(), compacts=(), date_max=DAY, frm=DAY, to=DAY):
    base = _dashboard(sessions=[_session("s1")], usage=[_usage(DAY, "s1", 5.0)])
    events = [[f"{DAY}T10:00", "s1", "compact", trig, n] for trig, n in compacts]
    return (base + f"PAYLOAD.friction = {json.dumps(list(friction))}; PAYLOAD.limits = [];"
            f"PAYLOAD.health.events = {json.dumps(events)}; PAYLOAD.summary.date_max = '{date_max}';"
            f"state.from = '{frm}'; state.to = '{to}';")


def _limit_tiles(setup):
    return run_js(DASHBOARD, setup + "renderLimits(); out(el('limitTiles').innerHTML)")


class TestCompactionsTile(unittest.TestCase):
    def test_shows_automatic_and_manual_separately(self):
        friction = [[f"{DAY}T10:00", "s1", "compact", 900000]] * 3
        html = _limit_tiles(_limits_setup(friction=friction, compacts=[("auto", 2), ("manual", 1)]))
        value, sub = _tile(html, "Context compactions")
        self.assertEqual(value, "3")
        self.assertEqual(sub, "2 automatic · 1 manual")

    def test_the_old_blanket_description_is_gone(self):
        friction = [[f"{DAY}T10:00", "s1", "compact", 900000]]
        html = _limit_tiles(_limits_setup(friction=friction, compacts=[("manual", 1)]))
        self.assertNotIn("context filled and was summarised", html)

    def test_the_habits_card_count_matches_the_tile(self):
        friction = [[f"{DAY}T10:00", "s1", "compact", 900000]] * 3
        setup = _limits_setup(friction=friction, compacts=[("auto", 2), ("manual", 1)])
        res = run_js(DASHBOARD, setup + "out(habitMeasures({from: '%s', to: '%s'}).ev)" % (DAY, DAY))
        self.assertEqual((res["auto"], res["manual"]), (2, 1))


class TestMedianTurnTile(unittest.TestCase):
    def _turns(self, ms, day=DAY):
        return [[f"{day}T10:{i % 60:02d}", "s1", "turn", v] for i, v in enumerate(ms)]

    def test_under_a_minute_shows_seconds(self):
        self.assertEqual(run_js(DASHBOARD, "out([fmtDuration(40000), fmtDuration(59000), fmtDuration(90000)])"),
                         ["40s", "59s", "2m"])

    def test_the_tile_shows_the_real_median_and_its_sample(self):
        html = _limit_tiles(_limits_setup(friction=self._turns([40000] * 25)))
        value, sub = _tile(html, "Median turn")
        self.assertEqual(value, "40s")
        self.assertIn("from 25 turns", sub)

    def test_a_thin_sample_is_hidden(self):
        html = _limit_tiles(_limits_setup(friction=self._turns([40000] * 3)))
        value, sub = _tile(html, "Median turn")
        self.assertEqual(value, "—")
        self.assertIn("only 3 turns", sub)

    def test_a_stale_sample_says_when_the_records_stop(self):
        # Turn records stop on 2026-02-01; the range and the activity run on to the 28th.
        turns = self._turns([40000] * 25, day="2026-02-01")
        setup = _limits_setup(friction=turns, date_max="2026-02-28", frm="2026-02-01", to="2026-02-28")
        value, sub = _tile(_limit_tiles(setup), "Median turn")
        # The date is in the reader's locale ("Feb 1" in the US, "1 Feb" in the
        # UK, "2月1日" in Japan), so the expected text is formatted the same way.
        expected = run_js([], "out(new Date('2026-02-01T12:00:00').toLocaleDateString("
                              "undefined, { month: 'short', day: 'numeric' }))")
        self.assertIn(f"none after {expected}", sub)

    def test_a_sample_up_to_the_end_of_the_range_is_not_stale(self):
        turns = self._turns([40000] * 25)
        _, sub = _tile(_limit_tiles(_limits_setup(friction=turns)), "Median turn")
        self.assertNotIn("none after", sub)


# --------------------------------------------------------------------------- #
# 5. Allowance
# --------------------------------------------------------------------------- #

def _utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _has_zone_db():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        return True
    except Exception:
        return False


HIT = {"kind": limits.WEEKLY, "hit_at": "2026-03-04T12:00:00Z",
       "resets_at": "2026-03-06T09:00:00Z", "usage_before_hit_usd": 100.0}


class TestAllowanceEstimate(unittest.TestCase):
    NOW = _utc("2026-03-24T09:00:00Z")      # the week of 03-20 .. 03-27

    def _snap(self, requests, episodes=(HIT,)):
        # Logs begin before the weeks examined: a week begun before the first
        # request is only partly seen and is left out.
        requests = [("2026-02-20T09:00:00Z", 1.0)] + list(requests)
        return limits.allowance_snapshot(list(episodes), requests, now=self.NOW, tz=dt.timezone.utc)

    def test_says_how_many_hits_it_rests_on(self):
        self.assertEqual(self._snap([("2026-03-21T09:00:00Z", 5.0)])["weekly_hits"], 1)

    def test_a_completed_week_without_a_hit_that_used_more_widens_it_to_a_range(self):
        # 03-13 .. 03-20 had no limit notice and used $160, against $100 at the hit.
        snap = self._snap([("2026-03-14T09:00:00Z", 160.0), ("2026-03-21T09:00:00Z", 5.0)])
        self.assertEqual(snap["allowance_est_usd"], 100.0)
        self.assertEqual(snap["allowance_high_usd"], 160.0)
        self.assertTrue(snap["contradicted"])
        self.assertEqual(snap["high_week_start"], "2026-03-13T09:00:00Z")

    def test_no_range_when_every_clear_week_used_less(self):
        snap = self._snap([("2026-03-14T09:00:00Z", 60.0), ("2026-03-21T09:00:00Z", 5.0)])
        self.assertIsNone(snap["allowance_high_usd"])
        self.assertFalse(snap["contradicted"])

    def test_the_week_the_limit_was_hit_is_not_a_week_without_a_hit(self):
        # 03-06 is the reset of the hit week (02-27 .. 03-06): it used $100 and hit.
        snap = self._snap([("2026-03-01T09:00:00Z", 100.0), ("2026-03-21T09:00:00Z", 5.0)])
        self.assertIsNone(snap["allowance_high_usd"])

    def test_the_week_in_progress_is_never_the_upper_bound(self):
        snap = self._snap([("2026-03-21T09:00:00Z", 900.0)])
        self.assertIsNone(snap["allowance_high_usd"])

    def test_current_use_is_also_given_against_the_upper_bound(self):
        snap = self._snap([("2026-03-14T09:00:00Z", 200.0), ("2026-03-21T09:00:00Z", 50.0)])
        self.assertEqual(snap["used_pct"], 0.5)
        self.assertEqual(snap["used_pct_of_high"], 0.25)

    def test_existing_fields_are_unchanged(self):
        snap = self._snap([("2026-03-21T09:00:00Z", 5.0)])
        for key in ("allowance_est_usd", "calibrated_from", "week_start", "week_end",
                    "used_usd", "used_pct", "projected_hit_at", "status"):
            self.assertIn(key, snap)


@unittest.skipUnless(_has_zone_db(), "needs the IANA zone database")
class TestWeekBoundariesFollowTheClock(unittest.TestCase):
    def test_a_week_across_a_daylight_saving_change_keeps_its_wall_clock_time(self):
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        # Reset 4am New York on 6 March (EST, 09:00Z). Clocks go forward on 8 March,
        # so the next reset is 4am EDT, 08:00Z -- not 09:00Z, 168 hours later.
        episode = {**HIT, "resets_at": "2026-03-06T09:00:00Z"}
        snap = limits.allowance_snapshot([episode], [("2026-03-14T09:00:00Z", 5.0)],
                                         now=_utc("2026-03-15T12:00:00Z"), tz=ny)
        self.assertEqual(snap["week_start"], "2026-03-13T08:00:00Z")
        self.assertEqual(snap["week_end"], "2026-03-20T08:00:00Z")

    def test_a_zone_without_a_change_is_the_same_168_hours(self):
        snap = limits.allowance_snapshot([HIT], [("2026-03-14T09:00:00Z", 5.0)],
                                         now=_utc("2026-03-15T09:00:00Z"), tz=dt.timezone.utc)
        self.assertEqual(snap["week_start"], "2026-03-13T09:00:00Z")


class TestAllowanceDisplay(unittest.TestCase):
    A = {"week_start": "2026-02-07T12:00:00Z", "week_end": "2026-02-14T12:00:00Z",
         "used_usd": 59.68, "allowance_est_usd": 1237.73, "used_pct": 0.0482,
         "status": "on_track", "projected_hit_at": None, "calibrated_from": "2026-01-01T12:00:00Z",
         "weekly_hits": 1, "allowance_high_usd": None, "contradicted": False,
         "used_pct_of_high": None, "high_week_start": None}

    def _html(self, **over):
        a = {**self.A, **over}
        return run_js(DASHBOARD, _dashboard() + f"PAYLOAD.allowance = {json.dumps(a)};"
                      "renderAllowance(); out(el('allowanceWrap').innerHTML)")

    def test_no_false_precision(self):
        html = self._html()
        self.assertNotIn("1,237.73", html)
        self.assertNotIn("59.68", html)
        self.assertIn("$1.2k", html)

    def test_presented_as_a_rough_estimate_with_its_basis(self):
        html = self._html()
        self.assertRegex(html, r"(?i)rough estimate")
        self.assertIn("from 1 weekly-limit hit", html)

    def test_on_track_is_a_statement_of_the_share_used(self):
        html = self._html()
        self.assertNotIn("On track", html)
        self.assertRegex(html, r"(?i)about 5% of the estimate")

    def test_a_contradicting_week_widens_the_estimate_to_a_range_and_says_so(self):
        html = self._html(allowance_high_usd=1608.4, contradicted=True, used_pct_of_high=0.0371,
                          high_week_start="2026-01-31T12:00:00Z")
        self.assertIn("$1.2k", html)
        self.assertIn("$1.6k", html)
        self.assertRegex(html, r"(?i)without a (?:limit )?(?:notice|hit)")
        self.assertRegex(html, r"(?i)list-price dollars")

    def test_older_payloads_without_the_new_fields_still_render(self):
        a = {k: self.A[k] for k in ("week_start", "week_end", "used_usd", "allowance_est_usd",
                                    "used_pct", "status", "projected_hit_at", "calibrated_from")}
        html = run_js(DASHBOARD, _dashboard() + f"PAYLOAD.allowance = {json.dumps(a)};"
                      "renderAllowance(); out(el('allowanceWrap').innerHTML)")
        self.assertIn("meter", html)

    def test_the_daylight_saving_assumption_is_noted(self):
        self.assertRegex(self._html(), r"(?i)daylight|clock time")


# --------------------------------------------------------------------------- #
# 6. Badges and baselines
# --------------------------------------------------------------------------- #

class TestBadges(unittest.TestCase):
    def _html(self, context=(), usage=None, base=None):
        usage = usage or [_usage(DAY, "s1", 100.0)]
        return _strip(_dashboard(sessions=[_session("s1")], usage=usage, context=context))

    def test_no_badge_says_healthy_or_worth_a_look(self):
        for context in ([[f"{DAY}T10:00", "s1", "m", 0, 1, 50.0]],
                        [[f"{DAY}T10:00", "s1", "m", 3, 1, 50.0]]):
            html = self._html(context=context)
            self.assertNotIn("Healthy", html)
            self.assertNotIn("Worth a look", html)

    def test_the_share_badges_name_the_ten_percent_line(self):
        low = self._html(context=[[f"{DAY}T10:00", "s1", "m", 0, 1, 50.0]])
        high = self._html(context=[[f"{DAY}T10:00", "s1", "m", 3, 1, 50.0]])
        self.assertIn("10%", re.search(r'habit-status[^>]*>([^<]*)<', low).group(1))
        self.assertIn("10%", re.search(r'habit-status[^>]*>([^<]*)<', high).group(1))

    def test_a_badge_says_it_is_this_tool_applying_anthropics_line(self):
        html = self._html(context=[[f"{DAY}T10:00", "s1", "m", 3, 1, 50.0]])
        self.assertRegex(html, r"(?i)this tool applies Anthropic")

    def test_spend_at_or_under_the_average_is_not_called_healthy(self):
        html = self._html(usage=[_usage(DAY, "s1", 5.0)])
        self.assertNotIn("Healthy", html)
        self.assertRegex(html, r"average")

    def test_an_empty_earlier_window_says_there_is_no_data_before_the_range(self):
        html = self._html()
        self.assertIn("no data before this range", html)
        self.assertNotIn("no baseline yet", html)

    def test_the_summary_line_does_not_call_habits_worth_a_look(self):
        text = run_js(DASHBOARD, _dashboard(sessions=[_session("s1")], usage=[_usage(DAY, "s1", 100.0)],
                                            context=[[f"{DAY}T10:00", "s1", "m", 3, 1, 50.0]]) +
                      "renderHabitStrip(); out(el('habitsWindow').textContent)")
        self.assertNotIn("worth a look", text)
        self.assertIn("flagged", text)


# --------------------------------------------------------------------------- #
# 7. Footnotes, Why popovers and docs
# --------------------------------------------------------------------------- #

CONTEXT_POST = "https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents"


class TestSourcesAndWording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("static/index.html")
        cls.js = _read("static/habits.js")
        cls.doc = _read("docs/usage-health.md")
        cls.limitations = _read("docs/limitations.md")

    def test_200k_is_not_attributed_to_a_pricing_tier(self):
        self.assertNotIn("long-context tier in Anthropic", self.html)
        self.assertNotIn("long-context tier", self.js)
        self.assertNotIn("long-context pricing line", self.doc)
        self.assertNotIn("drew for the long-context tier", _read("claude_usage/health.py"))
        self.assertRegex(self.html, r"200k and 500k lines are (?:this tool's|our) own")

    def test_context_reliability_cites_the_context_engineering_post(self):
        self.assertIn("effective-context-engineering", self.doc)
        self.assertRegex(self.html, r"context-engineering")
        self.assertRegex(self.html, r"gradient")

    def test_the_payload_carries_the_context_post_as_a_source(self):
        self.assertIn(CONTEXT_POST, _read("claude_usage/health.py"))

    def test_the_best_practices_guide_is_not_said_to_rest_on_the_context_constraint(self):
        self.assertNotIn("is built on one constraint", self.doc)
        self.assertNotIn("context as the key constraint", self.doc)
        self.assertRegex(self.doc, r"(?i)what you can stop doing")

    def test_plan_mode_uses_anthropics_own_wording(self):
        self.assertNotIn("overhead for a one-line fix", self.html)
        self.assertNotIn("Plan mode for multi-file or unfamiliar work", self.js)
        self.assertIn("Use plan mode for complex tasks", self.html)
        self.assertIn("plan mode for complex tasks", self.js.lower())
        self.assertNotIn("not for one-line fixes", self.doc)

    def test_subagent_advice_follows_the_current_guidance(self):
        self.assertNotIn("subagents for research", self.js)
        self.assertRegex(self.js, r"parallel work")
        self.assertRegex(self.js, r"independent review")

    def test_metr_19_percent_is_marked_outdated(self):
        para = self.doc.split("METR's 2025 trial")[1][:400]
        self.assertRegex(para, r"(?i)outdated")
        self.assertIn("Feb 2026", para)

    def test_the_habits_footnote_says_whose_line_the_status_is(self):
        note = self.html.split('id="habitsStrip"')[1].split("</p>")[0]
        self.assertRegex(note, r"(?i)this tool applies")
        self.assertNotIn("Healthy", note)

    def test_limitations_entry_describes_the_range_and_the_basis(self):
        entry = self.limitations.split("The allowance is estimated, not reported.")[1].split("\n- **")[0]
        self.assertRegex(entry, r"(?i)range")
        self.assertRegex(entry, r"(?i)daylight|wall-clock")
        self.assertRegex(entry, r"(?i)one (?:data point|hit)|single")

    def test_status_wording_in_the_docs(self):
        self.assertNotIn('A status ("Healthy" / "Worth a look")', self.doc)


if __name__ == "__main__":
    unittest.main()
