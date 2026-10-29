"""Checks on usage-limit tracking and friction.

On a seat plan the allowance is the binding constraint, and Claude Code's own
limit notices are the only record of it running out. These tests guard the
three things that make the numbers trustworthy: notices are parsed to the right
instant, repeated notices and resume copies are counted once, and "blocked"
means locked out -- not "went home for the night".
"""
import datetime as dt
import json
import os
import tempfile
import time
import unittest

from claude_usage import export as export_mod
from claude_usage import limits
from claude_usage.cache import FileCache
from claude_usage.scan import scan

LIMITS_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "limits", "projects")
SENTINEL = "SECRET_PROMPT_XYZ"
PERSON = ("someone@example.com", "Some One")


def _scan():
    # In the export zone (UTC by default): the export tests below read it as one.
    return scan(LIMITS_ROOT, cache=FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json")),
                tz=dt.timezone.utc)


def _utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _has_zone_db():
    # Windows Python has no IANA database unless the tzdata package is
    # installed; the parser then falls back to local time, so tests that pin a
    # named zone only mean something where the database exists.
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        return True
    except Exception:
        return False


needs_zone_db = unittest.skipUnless(_has_zone_db(), "no IANA time-zone database")


class TestNoticeParsing(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "You've hit your weekly limit · resets Oct 2 at 4am (America/New_York)": limits.WEEKLY,
            "You've hit your individual spend limit · ask your admin for a higher limit · "
            "your weekly limit resets Oct 2 at 1am (America/Vancouver)": limits.SPEND_CAP,
            "You've hit your session limit · resets 3pm (Europe/London)": limits.FIVE_HOUR,
            "5-hour limit reached ∙ resets 11pm": limits.FIVE_HOUR,
            "You've hit your Opus limit · resets Oct 2 at 4am": limits.OPUS,
            "Rate limited": limits.OTHER,
        }
        for text, kind in cases.items():
            self.assertEqual(limits.classify(text), kind, text)

    @needs_zone_db
    def test_one_instant_named_in_two_zones(self):
        ny = limits.parse_reset("resets Oct 2 at 4am (America/New_York)", "2026-09-29T00:32:47Z")
        van = limits.parse_reset("resets Oct 2 at 1am (America/Vancouver)", "2026-09-29T00:51:08Z")
        self.assertEqual(ny, "2026-10-02T08:00:00Z")
        self.assertEqual(ny, van)

    @needs_zone_db
    def test_time_only_rolls_to_the_next_day(self):
        # 15:40Z is 16:40 in London (BST), so "3:30pm" is tomorrow's.
        self.assertEqual(limits.parse_reset("resets 3:30pm (Europe/London)", "2026-10-01T15:40:00Z"),
                         "2026-10-02T14:30:00Z")

    def test_year_rolls_over(self):
        self.assertEqual(limits.parse_reset("resets Jan 2 at 4am (UTC)", "2026-12-30T10:00:00Z"),
                         "2027-01-02T04:00:00Z")

    def test_noon_and_midnight(self):
        self.assertEqual(limits.parse_reset("resets Mar 6 at 12pm (UTC)", "2026-03-02T00:00:00Z"),
                         "2026-03-06T12:00:00Z")
        self.assertEqual(limits.parse_reset("resets Mar 6 at 12am (UTC)", "2026-03-02T00:00:00Z"),
                         "2026-03-06T00:00:00Z")

    def test_unknown_zone_does_not_crash(self):
        self.assertIsNotNone(limits.parse_reset("resets Mar 6 at 9am (Not/AZone)", "2026-03-02T00:00:00Z"))

    def test_no_reset_time(self):
        self.assertIsNone(limits.parse_reset("You've hit your weekly limit", "2026-03-02T00:00:00Z"))
        self.assertIsNone(limits.parse_reset("resets Mar 6 at 9am", None))


class _LocalZone:
    """Pin the machine's local zone for the duration of a test."""

    def pin(self, name):
        if name is None:  # no tzset here (Windows): the machine's own zone
            return
        old = os.environ.get("TZ")
        os.environ["TZ"] = name
        time.tzset()

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)


class TestResetFormats(_LocalZone, unittest.TestCase):
    """Formats Claude Code has not been seen to write, but a notice could take.
    Each must give the same instant on any machine. Where the local zone can be
    pinned (not Windows) that is checked on two machines; elsewhere once, as
    these name their own zone."""

    MACHINES = (("America/Los_Angeles", "Asia/Karachi") if hasattr(time, "tzset")
                else (None,))

    def check(self, cases):
        for machine in self.MACHINES:
            self.pin(machine)
            for text, event, want in cases:
                with self.subTest(machine=machine, text=text, event=event):
                    self.assertEqual(limits.parse_reset(text, event), want)

    @needs_zone_db
    def test_the_real_format_still_parses(self):
        self.check([
            ("You've hit your weekly limit · resets Oct 2 at 4am (America/New_York)",
             "2026-09-29T00:32:47.000Z", "2026-10-02T08:00:00Z"),
            ("your weekly limit resets Oct 2 at 1am (America/Vancouver)",
             "2026-09-29T00:51:08.000Z", "2026-10-02T08:00:00Z"),
        ])

    @needs_zone_db
    def test_time_only_with_at_and_an_iana_zone(self):
        # 18:00Z is 14:00 in New York (EDT), so 4pm is the same day: 20:00Z.
        self.check([("resets at 4pm (America/New_York)", "2026-10-13T18:00:00.000Z",
                     "2026-10-13T20:00:00Z")])

    @needs_zone_db
    def test_time_only_rolls_to_tomorrow_when_already_past(self):
        # 21:00Z is 5pm in New York (EDT): 4pm has gone, so it means tomorrow's.
        self.check([("resets at 4pm (America/New_York)", "2026-10-13T21:00:00.000Z",
                     "2026-10-14T20:00:00Z")])

    def test_comma_between_date_and_time(self):
        self.check([("resets Oct 14, 4am (UTC)", "2026-10-13T10:00:00.000Z",
                     "2026-10-14T04:00:00Z")])

    def test_tomorrow_is_the_notice_date_plus_one(self):
        self.check([("resets tomorrow at 4am (UTC)", "2026-10-13T10:00:00.000Z",
                     "2026-10-14T04:00:00Z"),
                    ("resets tomorrow 4am (UTC)", "2026-12-31T23:00:00.000Z",
                     "2027-01-01T04:00:00Z")])

    @needs_zone_db
    def test_tomorrow_is_the_day_in_the_named_zone(self):
        # 20:00Z is already the 14th in Tokyo, so tomorrow there is the 15th.
        self.check([("resets tomorrow at 4am (Asia/Tokyo)", "2026-10-13T20:00:00.000Z",
                     "2026-10-14T19:00:00Z")])

    def test_ordinal_day_suffix(self):
        self.check([("resets Oct 14th at 4am (UTC)", "2026-10-13T10:00:00.000Z",
                     "2026-10-14T04:00:00Z"),
                    ("resets Oct 2nd at 4am (UTC)", "2026-10-01T10:00:00.000Z",
                     "2026-10-02T04:00:00Z"),
                    ("resets Oct 3rd at 4am (UTC)", "2026-10-01T10:00:00.000Z",
                     "2026-10-03T04:00:00Z")])

    def test_an_hour_outside_the_12_hour_clock_is_rejected(self):
        self.check([("resets 13pm (UTC)", "2026-10-13T10:00:00.000Z", None),
                    ("resets 0am (UTC)", "2026-10-13T10:00:00.000Z", None),
                    ("resets Oct 14 at 0pm (UTC)", "2026-10-13T10:00:00.000Z", None),
                    ("resets 12am (UTC)", "2026-10-13T10:00:00.000Z", "2026-10-14T00:00:00Z")])

    @needs_zone_db
    def test_us_abbreviations_follow_daylight_saving(self):
        # ZoneInfo("EST") is a fixed UTC-5, an hour out all summer.
        self.check([
            ("resets Jul 4 at 4am (EST)", "2026-07-01T00:00:00.000Z", "2026-07-04T08:00:00Z"),
            ("resets Jul 4 at 4am (EDT)", "2026-07-01T00:00:00.000Z", "2026-07-04T08:00:00Z"),
            ("resets Dec 4 at 4am (EDT)", "2026-12-01T00:00:00.000Z", "2026-12-04T09:00:00Z"),
            ("resets Nov 3 at 4am (PST)", "2026-11-01T00:00:00.000Z", "2026-11-03T12:00:00Z"),
            ("resets Jul 4 at 4am (PDT)", "2026-07-01T00:00:00.000Z", "2026-07-04T11:00:00Z"),
            ("resets Jul 4 at 4am (CST)", "2026-07-01T00:00:00.000Z", "2026-07-04T09:00:00Z"),
            ("resets Jul 4 at 4am (CDT)", "2026-07-01T00:00:00.000Z", "2026-07-04T09:00:00Z"),
            ("resets Jul 4 at 4am (MST)", "2026-07-01T00:00:00.000Z", "2026-07-04T10:00:00Z"),
            ("resets Jul 4 at 4am (MDT)", "2026-07-01T00:00:00.000Z", "2026-07-04T10:00:00Z"),
        ])

    @needs_zone_db
    def test_est_in_summer_equals_new_york(self):
        for machine in self.MACHINES:
            self.pin(machine)
            self.assertEqual(
                limits.parse_reset("resets Jul 4 at 4am (EST)", "2026-07-01T00:00:00.000Z"),
                limits.parse_reset("resets Jul 4 at 4am (America/New_York)", "2026-07-01T00:00:00.000Z"))

    def test_a_reset_before_the_notice_is_not_pushed_a_year_out(self):
        self.check([
            # three days earlier: a stale or garbled notice, not next October
            ("resets Oct 10 at 4am (UTC)", "2026-10-13T10:00:00.000Z", None),
            # more than a day earlier used to roll a year; within a day it stayed before the notice
            ("resets Oct 13 at 4am (UTC)", "2026-10-13T10:00:00.000Z", None),
            # a weekly window is 7 days, so a date a week or so ahead is fine
            ("resets Oct 20 at 4am (UTC)", "2026-10-13T10:00:00.000Z", "2026-10-20T04:00:00Z"),
            # more than 8 days ahead is not a limit window
            ("resets Oct 30 at 4am (UTC)", "2026-10-13T10:00:00.000Z", None),
            # the year still rolls over
            ("resets Jan 2 at 4am (UTC)", "2026-12-30T10:00:00.000Z", "2027-01-02T04:00:00Z"),
        ])

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the local zone")
    def test_no_zone_means_the_machines_local_zone_with_its_daylight_saving(self):
        # A fixed offset taken from "now" would be wrong for a date on the other
        # side of a clock change. The offset must be the one in force on the date.
        cases = {
            "America/Los_Angeles": [
                ("resets Jul 4 at 4am", "2026-07-01T00:00:00.000Z", "2026-07-04T11:00:00Z"),
                ("resets Dec 3 at 4am", "2026-12-01T00:00:00.000Z", "2026-12-03T12:00:00Z"),
                # across the November change: PDT now, PST at the reset
                ("resets Nov 3 at 4am", "2026-10-31T00:00:00.000Z", "2026-11-03T12:00:00Z"),
                ("resets 3pm", "2026-07-01T00:00:00.000Z", "2026-07-01T22:00:00Z"),
            ],
            "Asia/Karachi": [
                ("resets Jul 4 at 4am", "2026-07-01T00:00:00.000Z", "2026-07-03T23:00:00Z"),
                ("resets Dec 3 at 4am", "2026-12-01T00:00:00.000Z", "2026-12-02T23:00:00Z"),
            ],
        }
        for machine, rows in cases.items():
            self.pin(machine)
            for text, event, want in rows:
                with self.subTest(machine=machine, text=text):
                    self.assertEqual(limits.parse_reset(text, event), want)

    @needs_zone_db
    def test_a_zone_that_is_an_abbreviation_needs_no_machine_zone(self):
        # "(PST)" used to fall back to the machine's fixed offset: 13 hours out
        # on a machine in Karachi.
        for machine in self.MACHINES:
            self.pin(machine)
            self.assertEqual(
                limits.parse_reset("resets Nov 3 at 4am (PST)", "2026-11-02T00:00:00.000Z"),
                "2026-11-03T12:00:00Z")


def _ev(ts, kind, reset):
    return {"ts": ts, "kind": kind, "resets_at": reset, "session_id": "s"}


class TestEpisodes(unittest.TestCase):
    RESET = "2026-03-06T09:00:00Z"
    NOW = _utc("2026-03-10T00:00:00Z")

    def test_repeated_notices_are_one_episode(self):
        eps = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.WEEKLY, self.RESET),
             _ev("2026-03-02T10:05:00Z", limits.WEEKLY, self.RESET),
             _ev("2026-03-02T10:09:00Z", limits.WEEKLY, self.RESET)], [], now=self.NOW)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["notices"], 3)

    def test_extra_usage_after_the_weekly_limit_is_not_a_lockout(self):
        # Hit at 10:00, last notice 10:06, a request got through at 10:30 and
        # then nothing until next morning. Blocked for the six minutes of
        # notices, not overnight.
        requests = [("2026-02-28T12:00:00Z", 5.0), ("2026-03-02T09:00:00Z", 7.0),
                    ("2026-03-02T10:30:00Z", 2.0), ("2026-03-03T09:00:00Z", 1.0),
                    ("2026-03-07T09:00:00Z", 99.0)]
        ep, = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.WEEKLY, self.RESET),
             _ev("2026-03-02T10:06:00Z", limits.WEEKLY, self.RESET)], requests, now=self.NOW)
        self.assertFalse(ep["locked_out"])
        self.assertEqual(ep["blocked_hours"], 0.1)
        self.assertEqual(ep["resumed_at"], "2026-03-02T10:30:00Z")
        # Before: the week ending at the reset, up to the hit.
        self.assertAlmostEqual(ep["usage_before_hit_usd"], 12.0)
        # After: hit to reset -- extra usage. The 99 is after the reset.
        self.assertAlmostEqual(ep["usage_after_hit_usd"], 3.0)
        self.assertEqual(ep["requests_after_hit"], 2)

    def test_nothing_getting_through_until_the_reset_is_a_lockout(self):
        requests = [("2026-03-02T10:30:00Z", 2.0), ("2026-03-06T10:00:00Z", 1.0)]
        ep, = limits.build_episodes(
            [_ev("2026-03-02T11:00:00Z", limits.SPEND_CAP, self.RESET)], requests, now=self.NOW)
        self.assertTrue(ep["locked_out"])
        self.assertEqual(ep["blocked_hours"], 94.0)
        self.assertIsNone(ep["resumed_at"], "answered only after the reset")

    def test_a_lockout_still_in_force_runs_to_now(self):
        now = _utc("2026-03-03T11:00:00Z")
        ep, = limits.build_episodes(
            [_ev("2026-03-02T11:00:00Z", limits.SPEND_CAP, self.RESET)], [], now=now)
        self.assertTrue(ep["ongoing"])
        self.assertEqual(ep["blocked_hours"], 24.0)

    def test_different_resets_are_different_episodes(self):
        eps = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.WEEKLY, self.RESET),
             _ev("2026-03-09T10:00:00Z", limits.WEEKLY, "2026-03-13T09:00:00Z")], [], now=self.NOW)
        self.assertEqual(len(eps), 2)

    def test_an_interleaved_notice_of_another_kind_does_not_split_an_episode(self):
        # Weekly limit, then the spend cap while on extra usage, then the
        # weekly notice again: still one weekly episode, plus the spend cap.
        eps = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.WEEKLY, self.RESET),
             _ev("2026-03-02T12:00:00Z", limits.SPEND_CAP, self.RESET),
             _ev("2026-03-02T13:00:00Z", limits.WEEKLY, self.RESET)], [], now=self.NOW)
        self.assertEqual([(e["kind"], e["notices"]) for e in eps],
                         [(limits.WEEKLY, 2), (limits.SPEND_CAP, 1)])
        self.assertEqual(eps[0]["last_notice_at"], "2026-03-02T13:00:00Z")

    def test_interleaving_without_reset_times_still_groups_by_kind(self):
        eps = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.FIVE_HOUR, None),
             _ev("2026-03-02T10:30:00Z", limits.OTHER, None),
             _ev("2026-03-02T11:00:00Z", limits.FIVE_HOUR, None)], [], now=self.NOW)
        self.assertEqual([(e["kind"], e["notices"]) for e in eps],
                         [(limits.FIVE_HOUR, 2), (limits.OTHER, 1)])

    def test_a_notice_without_a_timestamp_is_skipped_not_a_crash(self):
        eps = limits.build_episodes(
            [_ev("2026-03-02T10:00:00Z", limits.WEEKLY, self.RESET),
             _ev(None, limits.WEEKLY, self.RESET)], [], now=self.NOW)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["notices"], 1)


class TestAllowance(unittest.TestCase):
    EPISODE = {"kind": limits.WEEKLY, "hit_at": "2026-03-04T12:00:00Z",
               "resets_at": "2026-03-06T09:00:00Z", "usage_before_hit_usd": 100.0}

    def test_none_without_a_weekly_hit(self):
        self.assertIsNone(limits.allowance_snapshot([], [], now=_utc("2026-03-10T00:00:00Z")))

    def test_steps_to_the_window_containing_now(self):
        snap = limits.allowance_snapshot([self.EPISODE], [("2026-03-14T09:00:00Z", 30.0)],
                                         now=_utc("2026-03-15T09:00:00Z"))
        self.assertEqual(snap["week_start"], "2026-03-13T09:00:00Z")
        self.assertEqual(snap["week_end"], "2026-03-20T09:00:00Z")
        self.assertEqual(snap["used_usd"], 30.0)
        self.assertEqual(snap["used_pct"], 0.3)

    def test_on_pace_to_hit_gives_a_projection(self):
        # $60 in the first two days of a seven-day window: $100 by day 3.3.
        snap = limits.allowance_snapshot([self.EPISODE], [("2026-03-14T09:00:00Z", 60.0)],
                                         now=_utc("2026-03-15T09:00:00Z"))
        self.assertEqual(snap["status"], "on_pace_to_hit")
        self.assertEqual(snap["projected_hit_at"], "2026-03-16T17:00:00Z")

    def test_slow_week_is_on_track(self):
        snap = limits.allowance_snapshot([self.EPISODE], [("2026-03-14T09:00:00Z", 5.0)],
                                         now=_utc("2026-03-15T09:00:00Z"))
        self.assertEqual(snap["status"], "on_track")
        self.assertIsNone(snap["projected_hit_at"])

    def test_already_over(self):
        snap = limits.allowance_snapshot([self.EPISODE], [("2026-03-14T09:00:00Z", 150.0)],
                                         now=_utc("2026-03-15T09:00:00Z"))
        self.assertEqual(snap["status"], "over")


class TestEndToEnd(unittest.TestCase):
    """The limits fixture: one session plus a resume copy repeating its records."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()

    def test_resume_copy_does_not_double_count(self):
        kinds = sorted(e["kind"] for e in self.payload["limits"])
        self.assertEqual(kinds, [limits.SPEND_CAP, limits.WEEKLY])
        retries = [r for r in self.payload["friction"] if r[2] == "retry"]
        self.assertEqual(len(retries), 1)

    def test_records_are_credited_to_the_original_session(self):
        self.assertEqual({e["session_id"] for e in self.payload["limits"]}, {"sess-limit"})
        self.assertEqual({r[1] for r in self.payload["friction"]}, {"sess-limit"})

    def test_text_a_person_typed_is_never_a_limit_event(self):
        # The fixture's user message mentions a Mar 9 reset; only flagged
        # notices count, and those all say Mar 6.
        self.assertEqual({e["resets_at"] for e in self.payload["limits"]}, {"2026-03-06T09:00:00Z"})

    def test_friction_kinds(self):
        by_kind = {}
        for _, _, kind, value in self.payload["friction"]:
            by_kind.setdefault(kind, []).append(value)
        self.assertEqual(sorted(by_kind["turn"]), [30000, 900000])
        self.assertEqual(by_kind["compact"], [900000])
        self.assertEqual(by_kind["retry"], [120000])

    def test_notice_text_never_reaches_the_payload_or_any_export(self):
        self.assertNotIn(SENTINEL, json.dumps(self.payload))
        with tempfile.TemporaryDirectory() as tmp:
            for path, _ in export_mod.export(self.payload, out_dir=tmp, person=PERSON):
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn(SENTINEL, fh.read(), path)

    def test_fixture_really_contains_the_sentinel(self):
        path = os.path.join(LIMITS_ROOT, "-Users-x-proj2", "sess-limit.jsonl")
        with open(path, encoding="utf-8") as fh:
            self.assertIn(SENTINEL, fh.read())

    def test_limits_file(self):
        rows = [dict(zip(export_mod.LIMIT_COLUMNS, r))
                for r in export_mod.limit_rows(self.payload, PERSON)]
        by_type = {r["limit_type"]: r for r in rows}
        self.assertEqual(by_type["weekly"]["locked_out"], "no")
        self.assertEqual(by_type["spend_cap"]["locked_out"], "yes")
        self.assertEqual(by_type["spend_cap"]["blocked_hours"], 94.0)
        self.assertEqual(by_type["weekly"]["project"], "/Users/x/proj2")

    def test_limits_file_respects_the_date_range(self):
        self.assertEqual(export_mod.limit_rows(self.payload, PERSON, since="2026-03-03"), [])

    def test_summary_and_session_friction_agree(self):
        ctx = export_mod.build_context(self.payload, PERSON)
        summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                   for r in export_mod.summary_rows(self.payload, PERSON, ctx=ctx)]
        sessions = [dict(zip(export_mod.SESSION_COLUMNS, r))
                    for r in export_mod.session_rows(self.payload, PERSON, ctx=ctx)]
        for col in ("api_retries", "turns", "long_turns", "context_compactions",
                    "limit_hits", "blocked_hours", "retry_wait_minutes"):
            self.assertAlmostEqual(sum(r[col] for r in summary),
                                   sum(r[col] for r in sessions), places=6, msg=col)
        s = next(r for r in sessions if r["session_id"] == "sess-limit")
        self.assertEqual((s["api_retries"], s["retry_wait_minutes"]), (1, 2.0))
        self.assertEqual((s["turns"], s["long_turns"], s["turn_minutes"]), (2, 1, 15.5))
        self.assertEqual((s["context_compactions"], s["limit_hits"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
