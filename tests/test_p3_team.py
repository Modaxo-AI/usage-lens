"""Phase 3 fixes to combining people's exports (claude_usage/team.py), each test named
for the finding it covers (TO-, SP-, TM- ids from the review ledger). Exports here are
hand-made loose CSVs, so each test says exactly what is in them."""
import csv
import datetime as dt
import json
import os
import tempfile
import unittest
import zipfile

from claude_usage import team

SUMMARY_DEFAULTS = {"project": "", "est_cost_usd": "0", "requests": "0", "sessions_started": "0",
                    "sessions_active": "0", "active_days": "0"}


class Folder:
    """A temp folder of loose export CSVs, written the way the exporter names them."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def close(self):
        self.tmp.cleanup()

    def write(self, kind, slug, stamp, rows, delimiter=","):
        columns = []
        for r in rows:
            columns += [c for c in r if c not in columns]
        path = os.path.join(self.dir, f"claude-usage-{kind}-{slug}-{stamp}.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter=delimiter)
            w.writerow(columns)
            w.writerows([r.get(c, "") for c in columns] for r in rows)
        return path

    def export(self, slug, stamp, summary, sessions=None, machine="m1", email=None, since="2026-09-14",
               until="2026-09-20", **ctx):
        """One person's export: summary rows (dicts) plus the context columns every row carries."""
        context = {"schema_version": "12", "exported_at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}T12:00:00+00:00",
                   "window_from": since, "window_to": until, "machine_id": machine, "bucket_zone": "UTC",
                   "timezone": "+00:00", "claude_account_email": email or f"{slug}@example.com",
                   "person_email": f"{slug}@dev.io", "person_active_days": "1", "window_workdays": "5",
                   "seat_idle": "no", "last_active_date": until, "cli_history_truncated": "no"}
        context.update(ctx)
        rows = [dict(SUMMARY_DEFAULTS, **context, **r) for r in summary]
        self.write("summary", slug, stamp, rows)
        if sessions is not None:
            self.write("sessions", slug, stamp, [dict(context, **s) for s in sessions])

    def merge(self, prices=None):
        exports, problems = team.read_exports([self.dir])
        return team.merge(exports, problems, prices)

    def messages(self, **kw):
        return [w["message"] for w in self.merge(**kw)["warnings"]]


def week_row(week, project, cost, wad=None, wdiw=None, partial=None, **extra):
    row = {"week_start": week, "project": project, "est_cost_usd": str(cost), "requests": "5",
           "sessions_started": "1", "sessions_active": "1", "active_days": "1"}
    if wad is not None:
        row["week_active_days"] = str(wad)
    if wdiw is not None:
        row["week_days_in_window"] = str(wdiw)
        row["partial_week"] = partial or ("yes" if wdiw < 7 else "no")
    row.update(extra)
    return row


class P3Case(unittest.TestCase):
    def setUp(self):
        self.folder = Folder()
        self.addCleanup(self.folder.close)


class TestSeatPrices(P3Case):
    def prices(self, **raw):
        body = {"per": "month", "prices": {"default": 100}}
        body.update(raw)
        with open(os.path.join(self.folder.dir, "seat-prices.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        return team.load_seat_prices([self.folder.dir])

    def test_sp1_a_price_in_another_currency_is_rejected_with_a_clear_message(self):
        prices, problems = self.prices(currency="EUR")
        self.assertIsNone(prices)
        self.assertEqual(len(problems), 1)
        self.assertIn("prices must be in USD, the currency usage is priced in", problems[0])
        self.assertIn("EUR", problems[0])

    def test_sp1_usd_in_any_case_or_no_currency_is_accepted(self):
        for currency in ("USD", "usd", " Usd "):
            prices, problems = self.prices(currency=currency)
            self.assertEqual((problems, prices["currency"]), ([], "USD"))
        body = {"per": "month", "prices": {"default": 100}}
        with open(os.path.join(self.folder.dir, "seat-prices.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        self.assertIsNotNone(team.load_seat_prices([self.folder.dir])[0])

    def test_sp1_a_non_string_currency_is_rejected_too(self):
        prices, problems = self.prices(currency=["USD"])
        self.assertIsNone(prices)
        self.assertIn("must be in USD", problems[0])

    def test_sp12_a_rejected_file_is_not_followed_by_no_file_found(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "r", 5)])
        self.prices(currency="EUR")
        merged = team._load([self.folder.dir])
        text = " | ".join(w["message"] for w in merged["warnings"])
        self.assertIn("must be in USD", text)
        self.assertNotIn("No seat-prices.json found", text)
        self.assertIn("blank until the seat-prices.json problem above is fixed", text)

    def test_sp12_no_file_still_says_so(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "r", 5)])
        text = " | ".join(self.folder.messages())
        self.assertIn("No seat-prices.json found", text)

    def test_sp11_an_enterprise_seat_is_priced_by_its_seat_tier(self):
        prices = {"per": "month", "prices": {"default_claude_max_5x": 100, "enterprise_tier_1": 40}}
        person = {"rate_limit_tier": "default_claude_max_5x", "seat_tier": "enterprise_tier_1"}
        self.assertEqual(team._seat_price(person, prices), 40)
        # Anything that is not an Enterprise seat keeps the rate-limit tier first.
        self.assertEqual(team._seat_price({"rate_limit_tier": "default_claude_max_5x", "seat_tier": "team"}, prices), 100)

    def test_sp11_an_enterprise_seat_with_no_enterprise_price_falls_back(self):
        prices = {"per": "month", "prices": {"default_claude_max_5x": 100}}
        person = {"rate_limit_tier": "default_claude_max_5x", "seat_tier": "enterprise_tier_1"}
        self.assertEqual(team._seat_price(person, prices), 100)

    def test_sp13_the_unpriced_note_names_the_tiers_that_need_a_price(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "r", 5)], seat_tier="enterprise_tier_2",
                           rate_limit_tier="default_claude_max_20x")
        prices = {"currency": "USD", "per": "month", "prices": {"other": 10.0}, "source": "seat-prices.json"}
        (msg,) = [m for m in self.folder.messages(prices=prices) if "no price" in m]
        self.assertIn("default_claude_max_20x", msg)
        self.assertIn("enterprise_tier_2", msg)
        self.assertIn("A (a@example.com)", msg)


class TestSplitWeek(P3Case):
    """SP-3 and TM-6: a week exported in two pieces by one machine."""

    def two_pieces(self, a_extra=None, b_extra=None):
        self.folder.export("a", "20260917", [week_row("2026-09-14", "x", 5, wad=3, wdiw=3, **(a_extra or {}))],
                           since="2026-09-14", until="2026-09-16")
        self.folder.export("a", "20260921", [week_row("2026-09-14", "y", 6, wad=4, wdiw=4, **(b_extra or {}))],
                           since="2026-09-17", until="2026-09-20")
        return self.folder.merge()["files"]["summary"][1]

    def test_sp3_pieces_with_different_projects_agree_on_the_weeks_days(self):
        rows = self.two_pieces()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["week_active_days"], "7")     # 3 + 4, on every row of the week
            self.assertEqual(r["week_days_in_window"], "7")  # Monday to Sunday between them
            self.assertEqual(r["partial_week"], "no")

    def test_sp3_a_piece_without_exact_days_leaves_the_week_unknown(self):
        self.folder.export("a", "20260917", [week_row("2026-09-14", "x", 5, wad=3, wdiw=3)],
                           since="2026-09-14", until="2026-09-16")
        self.folder.export("a", "20260921", [week_row("2026-09-14", "y", 6, wdiw=4)],
                           since="2026-09-17", until="2026-09-20")
        rows = self.folder.merge()["files"]["summary"][1]
        self.assertEqual({r["week_active_days"] for r in rows}, {""})

    def test_sp3_the_payload_row_carries_the_summed_days_and_the_machine(self):
        self.two_pieces()
        payload = team.team_payload([self.folder.dir])
        self.assertEqual({r[17] for r in payload["weekly"]}, {7})
        self.assertEqual({r[18] for r in payload["weekly"]}, {"m1"})

    def test_tm6_a_measure_one_piece_lacks_is_blank_not_the_other_pieces_figure(self):
        # Same project in both pieces, so the rows are combined into one.
        self.folder.export("a", "20260917", [week_row("2026-09-14", "x", 5, wad=3, wdiw=3,
                                                       very_long_context_cost_usd="2.0", prompts="10")],
                           since="2026-09-14", until="2026-09-16")
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 6, wad=4, wdiw=4)],
                           since="2026-09-17", until="2026-09-20")
        (row,) = self.folder.merge()["files"]["summary"][1]
        self.assertEqual(float(row["est_cost_usd"]), 11.0)
        self.assertEqual(row["very_long_context_cost_usd"], "")
        self.assertEqual(row["prompts"], "")

    def test_tm6_when_every_piece_has_it_the_pieces_add_up(self):
        self.folder.export("a", "20260917", [week_row("2026-09-14", "x", 5, wad=3, wdiw=3, very_long_context_cost_usd="2")],
                           since="2026-09-14", until="2026-09-16")
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 6, wad=4, wdiw=4, very_long_context_cost_usd="3")],
                           since="2026-09-17", until="2026-09-20")
        (row,) = self.folder.merge()["files"]["summary"][1]
        self.assertEqual(row["very_long_context_cost_usd"], "5")


class TestBlankProjectRow(P3Case):
    def test_to15_a_blank_project_row_that_has_cost_is_kept(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "", 3.0), week_row("2026-09-14", "x", 5.0),
                                              week_row("2026-09-07", "", 0, requests="0")])
        rows = self.folder.merge()["files"]["summary"][1]
        by = {(r["week_start"], r["project"]): float(r["est_cost_usd"]) for r in rows}
        self.assertEqual(by[("2026-09-14", "")], 3.0)
        self.assertEqual(sum(by.values()), 8.0)

    def test_to15_an_empty_blank_project_row_next_to_work_is_still_dropped(self):
        empty = dict(week_row("2026-09-14", "", 0), requests="0", sessions_started="0", sessions_active="0")
        self.folder.export("a", "20260921", [empty, week_row("2026-09-14", "x", 5.0)])
        rows = self.folder.merge()["files"]["summary"][1]
        self.assertEqual([r["project"] for r in rows], ["x"])


class TestWarnings(P3Case):
    def test_tm7_each_schema_version_names_only_its_own_people(self):
        self.folder.export("old", "20260921", [week_row("2026-09-14", "x", 1)], schema_version="6")
        self.folder.export("mid", "20260921", [week_row("2026-09-14", "x", 1)], schema_version="9")
        self.folder.export("cur", "20260921", [week_row("2026-09-14", "x", 1)], schema_version=str(team.context.EXPORT_SCHEMA_VERSION))
        (msg,) = [m for m in self.folder.messages() if "Exports use schema versions" in m]
        six = msg.split("version 6 to re-export:")[1].split(". Ask")[0]
        self.assertIn("old@example.com", six)
        self.assertNotIn("mid@example.com", six)
        nine = msg.split("version 9 to re-export:")[1]
        self.assertIn("mid@example.com", nine)
        self.assertNotIn("old@example.com", nine)
        self.assertNotIn("cur@example.com", msg)

    def test_sp10_an_excel_saved_export_is_one_notice_and_one_skipped_entry(self):
        for kind in ("summary", "sessions", "breakdown", "limits", "prs", "tools"):
            self.folder.write(kind, "ann", "20260921", [{"week_start": "9/14/2026", "project": "x", "est_cost_usd": "1"}],
                              delimiter=";")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("ann", problems[0])
        self.assertIn("skipped", problems[0])
        self.assertIn("comma-separated UTF-8", problems[0])
        self.assertIn("5 more of its files failed too", problems[0])
        self.assertEqual([(k["person"], k["source"]) for k in problems.skipped][0][0], "ann")
        merged = team._load([self.folder.dir])
        self.assertEqual(len(merged["skipped"]), 1)
        self.assertEqual(len([w for w in merged["warnings"] if "ann" in w["message"]]), 1)
        self.assertEqual(team.team_payload([self.folder.dir])["skipped"][0]["person"], "ann")

    def test_sp10_a_bad_zip_is_listed_as_skipped_with_the_fix(self):
        path = os.path.join(self.folder.dir, "claude-usage-export-bo-20260921.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("claude-usage-summary-bo-20260921.csv", "a;b\n1;2\n")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(len(problems), 1)
        self.assertIn("re-export", problems[0])
        self.assertEqual(problems.skipped[0]["source"], "claude-usage-export-bo-20260921.zip")

    def test_sp2_an_export_with_no_usage_and_a_day_window_is_named_with_the_fix(self):
        # What a machine with no logs wrote before schema 13: a window of today only.
        self.folder.export("idle", "20261029", [week_row("2026-10-26", "", 0, requests="0", sessions_started="0",
                                                         sessions_active="0")], since="2026-10-29", until="2026-10-29",
                           person_active_days="0", last_active_date="", seat_idle="yes")
        merged = self.folder.merge()
        (p,) = merged["people"]
        self.assertTrue(p["no_usage_window"])
        (msg,) = [m for m in self.folder.messages() if "holds no usage" in m]
        self.assertIn("Idle (idle@example.com)", msg)
        self.assertIn("--since", msg)

    def test_sp2_a_person_with_usage_is_not_flagged(self):
        self.folder.export("busy", "20260921", [week_row("2026-09-14", "x", 5.0)])
        (p,) = self.folder.merge()["people"]
        self.assertFalse(p["no_usage_window"])

    def test_sp2_a_longer_window_with_no_usage_is_not_flagged(self):
        self.folder.export("idle", "20261029", [week_row("2026-10-26", "", 0, requests="0")], since="2026-06-01",
                           until="2026-10-29", person_active_days="0", last_active_date="")
        (p,) = self.folder.merge()["people"]
        self.assertFalse(p["no_usage_window"])


class TestWindowsAndGaps(P3Case):
    def two_windows(self):
        self.folder.export("a", "20260907", [week_row("2026-08-31", "x", 5.0)], since="2026-08-31", until="2026-09-06")
        self.folder.export("a", "20260928", [week_row("2026-09-21", "x", 5.0)], since="2026-09-21", until="2026-09-27")
        return self.folder.merge()

    def test_sp9_people_carry_their_windows_and_the_gap(self):
        (p,) = self.two_windows()["people"]
        self.assertEqual(p["windows"], [["2026-08-31", "2026-09-06"], ["2026-09-21", "2026-09-27"]])
        self.assertEqual(p["gaps"], [["2026-09-07", "2026-09-20"]])
        self.assertEqual((p["window_days"], p["gap_days"]), (14, 14))

    def test_sp9_a_note_names_the_gap(self):
        (msg,) = [w["message"] for w in self.two_windows()["warnings"] if "leave out" in w["message"]]
        self.assertIn("2026-09-07 .. 2026-09-20", msg)
        self.assertIn("14 day(s)", msg)
        self.assertIn("A (a@example.com)", msg)

    def test_sp9_touching_windows_have_no_gap(self):
        self.folder.export("a", "20260907", [week_row("2026-08-31", "x", 5.0)], since="2026-08-31", until="2026-09-06")
        self.folder.export("a", "20260914", [week_row("2026-09-07", "x", 5.0)], since="2026-09-07", until="2026-09-13")
        (p,) = self.folder.merge()["people"]
        self.assertEqual((p["gaps"], p["gap_days"], p["window_days"]), ([], 0, 14))
        self.assertFalse([m for m in self.folder.messages() if "leave out" in m])

    def test_sp9_the_people_csv_has_window_days_and_gap_days(self):
        merged = self.two_windows()
        self.assertIn("window_days", team.PEOPLE_COLUMNS)
        self.assertIn("gap_days", team.PEOPLE_COLUMNS)
        text = team.merged_files(merged)["team-people.csv"]
        header = text.lstrip("﻿").splitlines()[0].split(",")
        row = dict(zip(header, text.splitlines()[1].split(",")))
        self.assertEqual((row["window_days"], row["gap_days"]), ("14", "14"))

    def test_sp6_two_machines_mark_seat_use_as_a_floor_in_the_csv(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 5.0)], machine="m1")
        self.folder.export("a", "20260922", [week_row("2026-09-14", "x", 5.0)], machine="m2")
        merged = self.folder.merge()
        (p,) = merged["people"]
        self.assertEqual((p["machines"], p["seat_use_is_floor"]), (2, "yes"))
        self.assertIn("seat_use_is_floor", team.PEOPLE_COLUMNS)

    def test_sp6_one_machine_is_not_a_floor(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 5.0)])
        (p,) = self.folder.merge()["people"]
        self.assertEqual(p["seat_use_is_floor"], "no")


class TestForecastAndReport(unittest.TestCase):
    def merged(self, **extra):
        limits = [{"limit_type": "weekly", "hit_at": f"{day}T10:00:00+00:00", "usage_after_hit_usd": str(usd)}
                  for day, usd in (("2026-08-04", 10), ("2026-09-08", 20), ("2026-09-15", 30))]
        base = {"files": {"limits": ([], limits)},
                "people": [{"person": "a", "window_from": "2026-08-03", "window_to": "2026-09-20",
                            "latest_export_at": "2026-09-21T10:00:00+00:00"}],
                "sources": [{"person": "a", "window_from": "2026-08-03", "window_to": "2026-08-16"},
                            {"person": "a", "window_from": "2026-09-07", "window_to": "2026-09-20"}],
                "warnings": [], "prices": None}
        base.update(extra)
        return base

    def test_sp4_weeks_in_the_gap_between_exports_are_not_zero_weeks(self):
        f = team.overage_forecast(self.merged(), today=dt.date(2026, 10, 1))
        self.assertEqual([w for w, _ in f["weekly"]], ["2026-08-03", "2026-08-10", "2026-09-07", "2026-09-14"])
        self.assertEqual(f["basis_weeks"], 4)
        self.assertEqual(f["weekly_run_rate_usd"], 15.0)  # (10 + 0 + 20 + 30) / 4; the gap weeks aren't averaged as $0

    def test_sp7_the_report_does_not_claim_exports_were_current(self):
        text = team.report_text(self.merged(files={}, people=[], sources=[]), today=dt.date(2026, 10, 1))
        self.assertIn("Notes: none.", text)
        self.assertNotIn("current", text)

    def snapshot_people(self):
        week = {"window_from": "2026-10-19", "window_to": "2026-10-29", "latest_export_at": "2026-10-29T08:00:00+00:00"}
        return [dict(week, person="over@x", allowance_status="over"),
                dict(week, person="pace@x", allowance_status="on_pace_to_hit"),
                dict(week, person="fine@x", allowance_status="on_track"),
                {"person": "old@x", "window_from": "2026-09-01", "window_to": "2026-09-27",
                 "allowance_status": "on_pace_to_hit", "latest_export_at": "2026-09-28T08:00:00+00:00"}]

    def test_sp8_only_snapshots_of_the_current_week_count_and_over_is_separate(self):
        merged = self.merged(people=self.snapshot_people(), sources=[], files={})
        f = team.overage_forecast(merged, today=dt.date(2026, 10, 29))
        self.assertEqual((f["people_on_pace_to_hit"], f["people_over"]), (["pace@x"], ["over@x"]))
        text = team.report_text(merged, today=dt.date(2026, 10, 29))
        self.assertIn("Already over the weekly limit this week: over@x (as of their export, 2026-10-29)", text)
        self.assertIn("On pace to hit the weekly limit this week: pace@x (as of their export, 2026-10-29)", text)
        self.assertNotIn("old@x", text)

    def test_sp8_a_week_later_nobody_is_on_pace(self):
        merged = self.merged(people=self.snapshot_people(), sources=[], files={})
        f = team.overage_forecast(merged, today=dt.date(2026, 11, 9))
        self.assertEqual((f["people_on_pace_to_hit"], f["people_over"]), ([], []))

    def test_sp1_the_value_lines_say_usd(self):
        people = [{"person": "a", "value_multiple": 2.0, "est_cost_usd": 100.0, "seat_cost_in_window": 50.0,
                   "extra_usage_usd": 0.0}]
        text = team.report_text(self.merged(people=people, files={}, sources=[], prices={"source": "seat-prices.json"}),
                                today=dt.date(2026, 10, 1))
        self.assertIn("seat-prices.json, USD", text)


class TestPayload(P3Case):
    def test_to16_two_peoples_unrelated_folders_with_the_same_path_stay_apart(self):
        self.folder.export("ann", "20260921", [week_row("2026-09-14", "~/work/app", 5.0)])
        self.folder.export("bo", "20260921", [week_row("2026-09-14", "~/work/app", 7.0),
                                               week_row("2026-09-14", "(unknown project)", 1.0),
                                               week_row("2026-09-14", "x", 2.0, project_repo="github.com/o/r")])
        payload = team.team_payload([self.folder.dir])
        repos = {r[0]: set() for r in payload["weekly"]}
        for r in payload["weekly"]:
            repos[r[0]].add(r[2])
        self.assertEqual(repos["ann@example.com"], {"~/work/app (folder, Ann)"})
        self.assertEqual(repos["bo@example.com"], {"~/work/app (folder, Bo)", "(unknown project)", "github.com/o/r"})

    def test_tm1_the_payload_carries_what_each_weeks_started_sessions_cost(self):
        sessions = [{"session_id": "s1", "started_at": "2026-09-14T10:00:00+00:00", "est_cost_usd": "100"},
                    {"session_id": "s2", "started_at": "2026-09-16T10:00:00+00:00", "est_cost_usd": "50"},
                    {"session_id": "s3", "started_at": "2026-09-21T10:00:00+00:00", "est_cost_usd": "7"}]
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 5.0)], sessions=sessions)
        payload = team.team_payload([self.folder.dir])
        self.assertEqual(payload["session_costs"], [["a@example.com", "2026-09-14", 150.0],
                                                    ["a@example.com", "2026-09-21", 7.0]])

    def test_tm9_a_blank_session_cost_stays_blank_in_the_prs(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 5.0)])
        self.folder.write("prs", "a", "20260921", [
            {"pr_url": "https://github.com/o/r/pull/1", "opened_on": "2026-09-15", "session_cost_usd": "", "session_prs": "1",
             "machine_id": "m1", "claude_account_email": "a@example.com"},
            {"pr_url": "https://github.com/o/r/pull/2", "opened_on": "2026-09-15", "session_cost_usd": "12.5", "session_prs": "1",
             "machine_id": "m1", "claude_account_email": "a@example.com"}])
        prs = sorted(team.team_payload([self.folder.dir])["prs"], key=lambda r: r["pr_url"])
        self.assertEqual([r["session_cost_usd"] for r in prs], [None, 12.5])

    def test_to1_weekly_rows_end_with_the_machine(self):
        self.folder.export("a", "20260921", [week_row("2026-09-14", "x", 5.0)], machine="laptop-1")
        (row,) = team.team_payload([self.folder.dir])["weekly"]
        self.assertEqual(len(row), 19)
        self.assertEqual(row[18], "laptop-1")


if __name__ == "__main__":
    unittest.main()
