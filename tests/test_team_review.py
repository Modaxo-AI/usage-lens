"""Regressions from the adversarial review of the team merge.

Each test writes small hand-made exports (loose CSVs, the columns the merge
reads) so the numbers in it can be checked by eye. The scenarios come from the
reviewer's reproductions: two machine ids for one person, a shared machine id,
the memory cost of a mostly empty file, seat value over a gap between exports,
"newest" across UTC offsets, and zone names that mean UTC.
"""
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from claude_usage import team

SUMMARY = ["person_email", "week_start", "project", "est_cost_usd", "sessions_active",
           "claude_account_email", "schema_version", "exported_at", "window_from", "window_to",
           "timezone", "bucket_zone", "machine_id", "person_active_days", "window_workdays",
           "rate_limit_tier", "seat_tier", "cli_history_truncated", "cli_history_from"]
SESSIONS = ["person_email", "session_id", "project", "started_at", "ended_at", "est_cost_usd",
            "requests", "claude_account_email", "schema_version", "exported_at", "window_from",
            "window_to", "timezone", "bucket_zone", "machine_id", "cli_history_truncated",
            "cli_history_from"]
P = "/Users/jane/proj"


def context(email="jane@x.com", **over):
    base = dict(person_email=email, claude_account_email=email, schema_version="11",
                exported_at="2026-10-20T18:00:00+00:00", window_from="2026-10-01",
                window_to="2026-10-20", timezone="+00:00", bucket_zone="UTC",
                machine_id="aaaaaaaaaaaa")
    base.update(over)
    return base


def week(start, cost, project=P, active="1"):
    return dict(week_start=start, project=project, est_cost_usd=str(cost), sessions_active=active)


def session(sid, started, ended, cost, project=P):
    return dict(session_id=sid, project=project, started_at=started, ended_at=ended,
                est_cost_usd=str(cost), requests="5")


def write(folder, slug, stamp, common, weeks, sessions):
    os.makedirs(folder, exist_ok=True)
    for kind, columns, rows in (("summary", SUMMARY, weeks), ("sessions", SESSIONS, sessions)):
        with open(os.path.join(folder, f"claude-usage-{kind}-{slug}-{stamp}.csv"), "w",
                  newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({**{c: common.get(c, "") for c in columns}, **row})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def export(self, slug, stamp, weeks, sessions, **over):
        write(self.dir, slug, stamp, context(**over), weeks, sessions)

    def merge(self):
        return team._load([self.dir])

    @staticmethod
    def warns(merged, level="warn"):
        return [w["message"] for w in merged["warnings"] if w["level"] == level]

    @staticmethod
    def cost(merged):
        return sum(float(r["est_cost_usd"]) for r in merged["files"]["summary"][1])


class TestTwoMachineIdsForOnePerson(Base):
    """h1: a deleted data folder gives one machine a new id. The earlier export
    holds a long session's cost on days the later export covers, and the later
    one can't list it. The files can't say that from two machines, so the
    merge warns."""

    def long_session(self, later_weeks=None, later_sessions=None):
        self.export("jane-x.com", "20261020",
                    [week("2026-10-05", 10), week("2026-10-12", 20)],
                    [session("S1", "2026-10-10T09:00:00+00:00", "2026-10-17T10:00:00+00:00", 30)])
        self.export("jane-x.com", "20261031",
                    later_weeks or [week("2026-10-12", 12), week("2026-10-19", 5)],
                    later_sessions if later_sessions is not None else
                    [session("S2", "2026-10-25T09:00:00+00:00", "2026-10-25T10:00:00+00:00", 5)],
                    exported_at="2026-10-31T18:00:00+00:00", window_from="2026-10-15",
                    window_to="2026-10-31", machine_id="bbbbbbbbbbbb")

    def test_it_warns_naming_the_person_and_the_weeks(self):
        self.long_session()
        m = self.merge()
        notes = [w for w in self.warns(m) if "different machine ids" in w]
        self.assertEqual(len(notes), 1, m["warnings"])
        self.assertIn("jane@x.com", notes[0])
        self.assertIn("2026-10-12", notes[0])
        self.assertNotIn("2026-10-05", notes[0])  # only the later export's window overlaps
        self.assertIn("double count", notes[0])
        self.assertIn("same machine", notes[0])

    def test_the_machines_are_still_added(self):
        # Dropping one would lose a real second machine's work: the totals stay a sum.
        self.long_session()
        self.assertAlmostEqual(self.cost(self.merge()), 47.0)

    def test_windows_that_do_not_meet_do_not_warn(self):
        self.long_session()
        os.remove(os.path.join(self.dir, "claude-usage-summary-jane-x.com-20261020.csv"))
        os.remove(os.path.join(self.dir, "claude-usage-sessions-jane-x.com-20261020.csv"))
        self.export("jane-x.com", "20261020", [week("2026-10-05", 10)], [],
                    window_to="2026-10-14")
        self.assertEqual([w for w in self.warns(self.merge()) if "machine ids" in w], [])

    def test_no_cost_in_the_overlap_does_not_warn(self):
        # Nothing for the later export's weeks to repeat.
        self.long_session(later_weeks=[week("2026-10-12", 0), week("2026-10-19", 5)])
        self.assertEqual([w for w in self.warns(self.merge()) if "machine ids" in w], [])

    def test_sessions_that_show_two_machines_do_not_warn(self):
        # The older lists a session on days both cover that the later export, made
        # after it, lacks: different logs, so adding them is right and there is
        # nothing to caution about.
        self.export("jane-x.com", "20261020",
                    [week("2026-10-05", 10), week("2026-10-12", 20)],
                    [session("S0", "2026-10-16T09:00:00+00:00", "2026-10-16T10:00:00+00:00", 20)])
        self.export("jane-x.com", "20261031", [week("2026-10-12", 12), week("2026-10-19", 5)], [],
                    exported_at="2026-10-31T18:00:00+00:00", window_from="2026-10-15",
                    window_to="2026-10-31", machine_id="bbbbbbbbbbbb")
        self.assertEqual([w for w in self.warns(self.merge()) if "machine ids" in w], [])

    def test_a_shared_session_joins_them_with_no_warning(self):
        self.long_session(later_sessions=[
            session("S1", "2026-10-10T09:00:00+00:00", "2026-10-17T10:00:00+00:00", 30)])
        self.assertEqual([w for w in self.warns(self.merge()) if "machine ids" in w], [])


class TestSharedMachineIdWithACompleteNewerExport(Base):
    """h5: a copied data folder. The newer export has complete history over the
    days both cover but lacks the older export's sessions (it is another
    machine's), and the older lists nothing of the newer's."""

    def exports(self, **newer):
        self.export("jane-x.com", "20261020",
                    [week("2026-09-28", 5), week("2026-10-19", 7)],
                    [session("O1", "2026-10-02T09:00:00+00:00", "2026-10-02T10:00:00+00:00", 5),
                     session("O2", "2026-10-19T09:00:00+00:00", "2026-10-19T10:00:00+00:00", 7)])
        self.export("jane-x.com", "20261031",
                    [week("2026-10-19", 40), week("2026-10-26", 30)],
                    [session("N1", "2026-10-22T09:00:00+00:00", "2026-10-22T10:00:00+00:00", 40),
                     session("N2", "2026-10-27T09:00:00+00:00", "2026-10-27T10:00:00+00:00", 30)],
                    exported_at="2026-10-31T18:00:00+00:00", window_from="2026-10-01",
                    window_to="2026-10-31", **newer)

    def test_the_newer_exports_work_is_not_dropped(self):
        self.exports()
        m = self.merge()
        self.assertEqual(m["people"][0]["machines"], 2)
        self.assertAlmostEqual(self.cost(m), 82.0)

    def test_the_warning_names_the_shared_id_not_the_window(self):
        self.exports()
        notes = [w for w in self.warns(self.merge()) if "same machine id" in w]
        self.assertEqual(len(notes), 1)
        self.assertIn("jane@x.com", notes[0])
        self.assertIn("machine-id", notes[0])
        self.assertNotIn("window runs Monday to Sunday", notes[0])
        # and no second warning blaming the window
        self.assertFalse([w for w in self.warns(self.merge()) if "without one covering all" in w])

    def test_cleaned_up_history_in_the_newer_export_is_still_one_machine(self):
        # Claude Code's 30-day cleanup deleted the early sessions before the second
        # export, which says so: not covered with complete history, so not another machine.
        self.exports(cli_history_truncated="yes", cli_history_from="2026-10-20")
        m = self.merge()
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertEqual([w for w in self.warns(m) if "same machine id" in w], [])

    def test_a_session_only_the_newer_export_has_is_not_another_machine(self):
        # The older export was made in the morning of its last day, before this
        # session began; the newer one rightly has it.
        self.export("jane-x.com", "20261020", [week("2026-10-12", 5)],
                    [session("A", "2026-10-13T09:00:00+00:00", "2026-10-13T10:00:00+00:00", 5)],
                    exported_at="2026-10-20T10:00:00+00:00", window_to="2026-10-20")
        self.export("jane-x.com", "20261031", [week("2026-10-12", 9)],
                    [session("A", "2026-10-13T09:00:00+00:00", "2026-10-13T10:00:00+00:00", 5),
                     session("B", "2026-10-20T15:00:00+00:00", "2026-10-20T16:00:00+00:00", 4)],
                    exported_at="2026-10-31T18:00:00+00:00", window_to="2026-10-31")
        self.assertEqual(self.merge()["people"][0]["machines"], 1)

    def test_a_session_the_newer_export_lacks_is_another_machine_whatever_the_newer_adds(self):
        self.export("jane-x.com", "20261020", [week("2026-10-12", 5)],
                    [session("A", "2026-10-13T09:00:00+00:00", "2026-10-13T10:00:00+00:00", 5)],
                    exported_at="2026-10-20T10:00:00+00:00", window_to="2026-10-20")
        self.export("jane-x.com", "20261031", [week("2026-10-12", 9)], [],
                    exported_at="2026-10-31T18:00:00+00:00", window_to="2026-10-31")
        self.assertEqual(self.merge()["people"][0]["machines"], 2)


    def test_a_newer_export_with_no_sessions_file_says_nothing(self):
        self.export("jane-x.com", "20261020", [week("2026-10-12", 5)],
                    [session("A", "2026-10-13T09:00:00+00:00", "2026-10-13T10:00:00+00:00", 5)],
                    exported_at="2026-10-20T10:00:00+00:00", window_to="2026-10-20")
        self.export("jane-x.com", "20261031", [week("2026-10-12", 9)], [],
                    exported_at="2026-10-31T18:00:00+00:00", window_to="2026-10-31")
        os.remove(os.path.join(self.dir, "claude-usage-sessions-jane-x.com-20261031.csv"))
        self.assertEqual(self.merge()["people"][0]["machines"], 1)


class TestWarningTextNamesTheDataFolder(Base):
    def test_the_copied_folder_warning_says_to_delete_machine_id_in_the_data_folder(self):
        TestSharedMachineIdWithACompleteNewerExport.exports(self)
        (note,) = [w for w in self.warns(self.merge()) if "same machine id" in w]
        self.assertIn("`machine-id`", note)
        self.assertIn("~/.usage-lens", note)
        self.assertIn("CLAUDE_USAGE_DATA_DIR", note)
        self.assertIn("--data-dir", note)
        self.assertIn("all but one", note)
        self.assertNotIn(".cache", note)


class TestSeatValueCoversOnlyTheExportedDays(Base):
    """h8: two exports a gap apart. Seat cost is for the days the windows cover,
    as the dashboard counts them, not for min(from)..max(to)."""

    def setUp(self):
        super().setUp()
        self.export("jane-x.com", "20260131", [week("2026-01-05", 100)], [],
                    exported_at="2026-01-31T12:00:00+00:00", window_from="2026-01-01",
                    window_to="2026-01-31", seat_tier="t")
        self.export("jane-x.com", "20261031", [week("2026-10-05", 100)], [],
                    exported_at="2026-10-31T12:00:00+00:00", window_from="2026-10-01",
                    window_to="2026-10-31", seat_tier="t")
        with open(os.path.join(self.dir, "seat-prices.json"), "w", encoding="utf-8") as fh:
            json.dump({"currency": "USD", "per": "month", "prices": {"default": 100}}, fh)

    def test_the_gap_between_windows_is_not_paid_for(self):
        person = self.merge()["people"][0]
        days = 31 + 31
        self.assertEqual(person["window_days"], days)
        self.assertAlmostEqual(person["seat_cost_in_window"], round(100 / (365.25 / 12) * days, 2))
        self.assertAlmostEqual(person["value_multiple"], 0.98, places=2)

    def test_the_report_and_people_csv_say_so(self):
        merged = self.merge()
        text = team.report_text(merged)
        self.assertIn("0.98x", text)
        self.assertIn("$204 seat", text)
        rows = list(csv.DictReader(io.StringIO(team.merged_files(merged)["team-people.csv"])))
        self.assertEqual(rows[0]["value_multiple"], "0.98")

    def test_overlapping_windows_count_each_day_once(self):
        self.assertEqual(team._covered_days([("2026-01-01", "2026-01-10"),
                                             ("2026-01-05", "2026-01-20"),
                                             ("2026-01-21", "2026-01-22")]), 22)
        self.assertEqual(team._covered_days([("2026-01-01", "2026-01-10"),
                                             ("2026-01-03", "2026-01-04")]), 10)
        self.assertEqual(team._covered_days([("2026-02-01", "2026-01-01"), (None, None)]), 0)


class TestNewestExportAcrossUtcOffsets(Base):
    """h4: 08:00+09:00 is 23:00 UTC the day before; as text it sorts after
    01:00+00:00, but it is the older export."""

    def setUp(self):
        super().setUp()
        # Two exports of one name: one folder each, as one machine's would be.
        write(os.path.join(self.dir, "a"), "jane-x.com", "20261020",
              context(exported_at="2026-10-20T08:00:00+09:00", window_from="2026-10-12",
                      timezone="+09:00", rate_limit_tier="old_tier", seat_tier="old_seat"),
              [week("2026-10-19", 10)],
              [session("S", "2026-10-19T09:00:00+09:00", "2026-10-19T10:00:00+09:00", 10)])
        write(os.path.join(self.dir, "b"), "jane-x.com", "20261020",
              context(exported_at="2026-10-20T01:00:00+00:00", window_from="2026-10-12",
                      rate_limit_tier="new_tier", seat_tier="new_seat"),
              [week("2026-10-19", 15)],
              [session("S", "2026-10-19T00:00:00+00:00", "2026-10-20T00:30:00+00:00", 15)])

    def test_a_persons_facts_come_from_the_newest_instant(self):
        person = self.merge()["people"][0]
        self.assertEqual(person["rate_limit_tier"], "new_tier")
        self.assertEqual(person["seat_tier"], "new_seat")
        self.assertEqual(person["latest_export_at"], "2026-10-20T01:00:00+00:00")

    def test_the_same_session_in_both_is_kept_from_the_newest(self):
        (row,) = self.merge()["files"]["sessions"][1]
        self.assertEqual(row["est_cost_usd"], "15")

    def test_recency_is_the_instant_not_the_text(self):
        exports, _ = team.read_exports([self.dir])
        self.assertEqual(max(exports, key=team._recency).exported_at, "2026-10-20T01:00:00+00:00")

    def test_the_sources_are_listed_oldest_first(self):
        stamps = [s["exported_at"] for s in self.merge()["sources"]]
        self.assertEqual(stamps, ["2026-10-20T08:00:00+09:00", "2026-10-20T01:00:00+00:00"])


class TestMemoryIsBoundedByCells(Base):
    """bomb: bytes don't bound a parsed file's cost. 200 columns of empty cells
    are a byte each on disk, eight in memory."""

    def wide(self, name, rows, columns=40):
        header = SUMMARY + [f"x{i}" for i in range(columns - len(SUMMARY))]
        path = os.path.join(self.dir, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for n in range(rows):
                row = [""] * len(header)
                row[0], row[1], row[2] = "a@x.com", "2026-10-05", f"/p{n}"
                row[3] = "1"
                writer.writerow(row)
        return path

    def test_a_file_past_the_cell_cap_is_skipped_and_named(self):
        self.wide("claude-usage-summary-big-20261020.csv", 100, columns=40)  # 4,000 cells
        with mock.patch.object(team, "_MAX_TOTAL_CELLS", 3_000):
            exports, problems = team.read_exports([self.dir])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("claude-usage-summary-big-20261020.csv", problems[0])
        self.assertIn("cells", problems[0])
        self.assertIn("skipped", problems[0])

    def test_the_cap_is_for_the_whole_run_not_each_file(self):
        for who in "abc":
            self.wide(f"claude-usage-summary-{who}-20261020.csv", 40, columns=40)  # 1,600 each
        with mock.patch.object(team, "_MAX_TOTAL_CELLS", 3_500):
            exports, problems = team.read_exports([self.dir])
        self.assertEqual(len(exports), 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("-c-", problems[0])  # name order: the third is the one that no longer fits

    def test_a_skipped_file_gives_its_cells_back(self):
        self.wide("claude-usage-summary-a-20261020.csv", 60, columns=40)  # 2,400: too many alone
        self.wide("claude-usage-summary-b-20261020.csv", 10, columns=40)  # 400
        with mock.patch.object(team, "_MAX_TOTAL_CELLS", 2_000):
            exports, problems = team.read_exports([self.dir])
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(problems), 1)

    def test_a_zip_of_mostly_empty_columns_is_stopped_at_the_cap(self):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(SUMMARY + [f"x{i}" for i in range(181)])  # 200 columns
        for n in range(300):
            writer.writerow(["a@x.com", "2026-10-05", f"/p{n}", "1"] + [""] * 196)
        path = os.path.join(self.dir, "claude-usage-export-p0-20261020.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("claude-usage-summary-p0-20261020.csv", buf.getvalue())
        with mock.patch.object(team, "_MAX_TOTAL_CELLS", 50_000):  # 300 x 200 = 60,000
            exports, problems = team.read_exports([self.dir])
        self.assertEqual(exports, [])
        self.assertIn("claude-usage-export-p0-20261020.zip", problems[0])
        self.assertIn("claude-usage-summary-p0-20261020.csv", problems[0])

    def test_one_column_rows_still_cost_a_row(self):
        path = os.path.join(self.dir, "claude-usage-prs-p-20261020.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("pr_url\n" + "u\n" * 100)
        with mock.patch.object(team, "_MAX_TOTAL_CELLS", 100 * team._MIN_ROW_CELLS - 1):
            exports, problems = team.read_exports([self.dir])
        self.assertEqual(exports, [])
        self.assertTrue(problems)

    def test_real_sized_exports_are_nowhere_near_it(self):
        TestSharedMachineIdWithACompleteNewerExport.exports(self)
        exports, problems = team.read_exports([self.dir])
        self.assertEqual((len(exports), problems), (2, []))


class TestUtcAliasesAreOneZone(Base):
    """GMT, Etc/GMT, UCT and Zulu cut days at the same midnight as UTC."""

    def zones(self, *names):
        for n, zone in enumerate(names):
            who = f"p{n}"
            write(self.dir, f"{who}-x.com", "20261020",
                  context(email=f"{who}@x.com", bucket_zone=zone, machine_id=str(n) * 12),
                  [week("2026-10-05", 1)], [])

    def test_people_in_utc_and_its_aliases_do_not_warn_of_different_zones(self):
        self.zones("UTC", "GMT", "Etc/GMT", "UCT", "Zulu")
        m = self.merge()
        self.assertEqual([w for w in self.warns(m) if "time zone" in w], [])

    def test_a_real_second_zone_still_warns(self):
        self.zones("UTC", "GMT", "Europe/Berlin")
        (note,) = [w for w in self.warns(self.merge()) if "time zones" in w]
        self.assertIn("UTC (2 people)", note)
        self.assertIn("Europe/Berlin (1 person)", note)

    def test_one_person_exporting_in_utc_and_gmt_does_not_warn(self):
        write(self.dir, "jane-x.com", "20261020", context(bucket_zone="UTC"),
              [week("2026-10-05", 1)], [])
        write(self.dir, "jane-x.com", "20261021", context(bucket_zone="GMT"),
              [week("2026-10-05", 1)], [])
        self.assertEqual([w for w in self.warns(self.merge()) if "time zone" in w or "both" in w], [])


class TestDocsMatchTheCode(unittest.TestCase):
    DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "team.md")

    @classmethod
    def setUpClass(cls):
        with open(cls.DOC, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_deleting_the_data_folder_is_not_called_safe(self):
        self.assertNotIn("shared sessions keep that from double-counting", self.text)
        self.assertIn("may double count", self.text)

    def test_the_copied_folder_is_the_data_folder(self):
        self.assertNotIn("`.cache` folder was copied", self.text)
        self.assertIn("`machine-id`", self.text)

    def test_no_stale_column_count(self):
        self.assertNotIn("at most 92 columns", self.text)

    def test_the_cell_cap_is_documented(self):
        self.assertIn(f"{team._MAX_TOTAL_CELLS:,} cells", self.text)


if __name__ == "__main__":
    unittest.main()
