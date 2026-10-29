"""Checks on combining many people's exports (collect.py --merge / serve.py --team).

The point of the team view is that a manager can stack files without being
misled, so these tests concentrate on the ways stacking goes wrong: the same
person under two identities, the same week exported twice, files from
different versions, and a breakdown that no longer adds up to its summary.
"""
import contextlib
import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from collections import defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from claude_usage import aggregate
from claude_usage import context
from claude_usage import export as export_mod
from claude_usage import team
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.test_health import MODEL, _prompt, _response, _ts, _write

REPO = Path(__file__).resolve().parent.parent
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
LIMIT_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "limits", "projects")


def _scan(root=FIXTURES, tz=None):
    return scan(root, cache=FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json")), tz=tz)


def _on_another_machine(payload):
    """The same kind of work done on a second machine: same projects, weeks and
    costs, but its own sessions (session ids are per machine)."""
    ids = {s["id"] for s in payload["sessions"]}

    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value + "-m2" if isinstance(value, str) and value in ids else value
    return walk(json.loads(json.dumps(payload)))


class TeamFolder:
    """Writes exports into a temp folder as different people would."""

    def __init__(self, tz=None):
        """tz: the zone the payloads were scanned in and the exports read their
        days in. None is the machine's own zone; tests whose windows name dates
        pass UTC so they mean the same days on every machine."""
        self.tz = tz
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._orig = context.ACCOUNT_FILES
        self._orig_machine = context.MACHINE_ID_FILE

    def close(self):
        context.ACCOUNT_FILES = self._orig
        context.MACHINE_ID_FILE = self._orig_machine
        self.tmp.cleanup()

    def _account(self, email):
        path = os.path.join(self.dir, ".acct-" + (email or "none") + ".json")
        account = {"oauthAccount": {"emailAddress": email, "seatTier": "enterprise_tier_1",
                                    "userRateLimitTier": "default_claude_max_5x",
                                    "hasExtraUsageEnabled": True}} if email else {}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(account, fh)
        context.ACCOUNT_FILES = [path]

    def zip(self, payload, claude_email, git_email, stamp, since=None, until=None, name=None,
            machine="laptop", bucket_zone=""):
        """machine names the machine exported from (each gets its own random
        id); None writes no machine id, as schema 5 and older did. bucket_zone
        is the label written in the `bucket_zone` column ("" for an export
        that predates it); the days themselves stay machine-local here."""
        self._account(claude_email)
        context.MACHINE_ID_FILE = os.path.join(self.dir, f".machine-{machine}")
        person = (git_email, git_email.split("@")[0])
        # These payloads are scanned in the machine's own zone, so they are
        # exported the way every export was before the export zone: days read
        # in that zone and no bucket_zone (zone label "") unless one is given.
        with mock.patch.object(context, "machine_id", return_value=None) if machine is None \
                else contextlib.nullcontext():
            data = export_mod.zip_bytes(payload, person, since=since, until=until, stamp=stamp,
                                        zone=(self.tz, bucket_zone))
        path = os.path.join(self.dir, name or export_mod.zip_filename_for(person, stamp))
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def schema1_summary(self, payload, git_email, stamp):
        """A file as the first release wrote it: 35 summary columns, no context."""
        self._account(None)
        cols = export_mod.SUMMARY_COLUMNS[:35]
        rows = export_mod.summary_rows(payload, (git_email, "Old"))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            if r[3]:  # schema 1 had no zero-week rows
                w.writerow(r[:35])
        path = os.path.join(self.dir, f"claude-usage-summary-{git_email}-{stamp}.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
        return path


def _as_schema7(path):
    """Rewrite an export zip the way a schema 7 tool wrote it: no `bucket_zone`
    column, schema_version 7."""
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i.filename).decode("utf-8")) for i in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members:
            rows = list(csv.reader(io.StringIO(text)))
            drop = rows[0].index("bucket_zone")
            out = io.StringIO()
            writer = csv.writer(out)
            for n, row in enumerate(rows):
                row = row[:drop] + row[drop + 1:]
                if n:
                    row[rows[0].index("schema_version")] = "7"
                writer.writerow(row)
            archive.writestr(name, out.getvalue())


def _cost(rows, person=None):
    return sum(float(r["est_cost_usd"] or 0) for r in rows if person is None or r["person"] == person)


class TestMergeCorrectness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.total = sum(r[13] for r in cls.payload["usage"])

    def setUp(self):
        self.folder = TeamFolder()

    def tearDown(self):
        self.folder.close()

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    def test_one_export_round_trips_exactly(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        self.assertAlmostEqual(_cost(m["files"]["sessions"][1]), self.total, places=5)
        self.assertAlmostEqual(_cost(m["files"]["breakdown"][1]), self.total, places=5)
        self.assertEqual([p["person"] for p in m["people"]], ["a@example.com"])

    def test_overlapping_re_export_is_not_double_counted(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212",
                        name="claude-usage-export-a-again-20260212.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        self.assertEqual(len(m["people"]), 1)
        self.assertEqual(m["people"][0]["exports"], 2)
        self.assertTrue(any("overlapping" in w["message"] for w in m["warnings"]))

    def test_a_newer_partial_export_does_not_replace_a_fuller_week(self):
        # The newer export starts mid-week (Feb 4), so its row for the week of
        # Feb 2 covers less of it than the older full export's row.
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", since="2026-02-04",
                        name="claude-usage-export-a-partial-20260212.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        week = [r for r in m["files"]["summary"][1] if r["week_start"] == "2026-02-02" and r["project"]]
        self.assertTrue(week)
        self.assertTrue(all("partial" not in r["merged_from"] for r in week))

    def test_breakdown_always_sums_to_its_summary_row(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", since="2026-02-04",
                        name="claude-usage-export-a-partial-20260212.zip")
        m = self._merge()
        s, b = defaultdict(float), defaultdict(float)
        for r in m["files"]["summary"][1]:
            s[(r["person"], r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        for r in m["files"]["breakdown"][1]:
            b[(r["person"], r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        for key in b:
            self.assertAlmostEqual(b[key], s[key], places=5, msg=key)

    def test_git_email_is_linked_to_the_claude_account(self):
        # Old file: git email only. New file: both. Same person.
        self.folder.schema1_summary(self.payload, "a@dev.io", "20260101")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        m = self._merge()
        self.assertEqual([p["person"] for p in m["people"]], ["a@example.com"])

    def test_identity_is_case_insensitive(self):
        self.folder.zip(self.payload, "A@Example.com", "a@dev.io", "20260210")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212",
                        name="claude-usage-export-a-2-20260212.zip")
        self.assertEqual(len(self._merge()["people"]), 1)

    def test_schema_mix_is_reported_and_columns_align(self):
        self.folder.schema1_summary(self.payload, "old@dev.io", "20260101")
        self.folder.zip(self.payload, "b@example.com", "b@dev.io", "20260210")
        m = self._merge()
        current = context.EXPORT_SCHEMA_VERSION
        self.assertTrue(any(f"schema versions 1, {current}" in w["message"] for w in m["warnings"]))
        columns, rows = m["files"]["summary"]
        self.assertEqual(columns[0], "person")
        self.assertEqual(columns[-1], "merged_from")
        self.assertIn("schema_version", columns)
        old = [r for r in rows if r["person"] == "old@dev.io"]
        self.assertTrue(old)
        self.assertTrue(all(r["schema_version"] == "" for r in old), "missing columns read blank")
        self.assertTrue(any("predate" in w["message"] for w in m["warnings"]))

    def test_the_same_logs_under_two_identities_is_flagged(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        self.folder.zip(self.payload, "b@example.com", "b@dev.io", "20260210")
        m = self._merge()
        self.assertTrue(any("more than one person" in w["message"] for w in m["warnings"]))

    def test_unreadable_and_unrelated_files_are_reported_not_fatal(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        with open(os.path.join(self.folder.dir, "notes.zip"), "wb") as fh:
            fh.write(b"not a zip")
        with open(os.path.join(self.folder.dir, "budget.csv"), "w", encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n")
        m = self._merge()
        messages = " ".join(w["message"] for w in m["warnings"])
        self.assertIn("notes.zip", messages)
        self.assertIn("budget.csv", messages)
        self.assertEqual(len(m["people"]), 1)


class TestTwoMachines(unittest.TestCase):
    """One person, two machines: the two add up, while a re-export from either
    one still de-duplicates."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.total = sum(r[13] for r in cls.payload["usage"])
        cls.sessions = len([s for s in cls.payload["sessions"] if not s["derived"]])

    def setUp(self):
        self.folder = TeamFolder()

    def tearDown(self):
        self.folder.close()

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    def _two_machines(self, payload=None, desktop_payload=None, **kw):
        payload = payload or self.payload
        self.folder.zip(payload, "a@example.com", "a@dev.io", "20260210", machine="laptop", **kw)
        self.folder.zip(desktop_payload or _on_another_machine(payload), "a@example.com", "a@dev.io",
                        "20260210", machine="desktop", name="claude-usage-export-a-desktop-20260210.zip",
                        **kw)

    def test_two_machines_add_up(self):
        self._two_machines()
        m = self._merge()
        for kind in ("summary", "breakdown", "sessions"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), 2 * self.total, places=5, msg=kind)
        p = m["people"][0]
        self.assertEqual((len(m["people"]), p["exports"], p["machines"]), (1, 2, 2))
        self.assertAlmostEqual(p["est_cost_usd"], round(2 * self.total, 2), places=2)
        self.assertEqual(p["sessions"], 2 * self.sessions)
        self.assertTrue(any("2 machines" in w["message"] for w in m["warnings"]))
        self.assertFalse(any("overlapping" in w["message"] for w in m["warnings"]))

    def test_a_joined_exports_unique_sessions_are_dropped_with_a_warning(self):
        # Joined through the session they share, the laptop and desktop count as
        # one machine; a week comes from one export, so the other's own sessions
        # are missing from the summary and breakdown. The report must say so.
        desktop = json.loads(json.dumps(_on_another_machine(self.payload))
                             .replace("sess-main-1-m2", "sess-main-1"))
        self._two_machines(desktop_payload=desktop)
        m = self._merge()
        self.assertEqual(m["people"][0]["machines"], 1)
        summary, sessions = _cost(m["files"]["summary"][1]), _cost(m["files"]["sessions"][1])
        self.assertGreater(sessions, summary)  # the two files disagree
        warns = [w["message"] for w in m["warnings"] if w["level"] == "warn"]
        dropped = [w for w in warns if "only one of two joined exports holds" in w]
        self.assertEqual(len(dropped), 1, warns)
        # The cost is the gap between the files, to the cent (0.0058 -> $0.01).
        self.assertIn(f"est. ${sessions - summary:,.2f}) only one", dropped[0])
        self.assertIn("2 sessions", dropped[0])
        self.assertIn("1 in the week of 2025-12-29", dropped[0])
        self.assertIn("1 in the week of 2026-01-26", dropped[0])

    def test_a_plain_re_export_of_one_machine_does_not_warn_of_dropped_sessions(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="laptop")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", machine="laptop",
                        since="2026-01-05", name="claude-usage-export-a-again-20260212.zip")
        m = self._merge()
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertEqual([w for w in m["warnings"] if w["level"] == "warn"], [])

    def test_breakdown_still_sums_to_summary_across_machines(self):
        self._two_machines()
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", since="2026-02-04",
                        machine="laptop", name="claude-usage-export-a-partial-20260212.zip")
        m = self._merge()
        s, b = defaultdict(float), defaultdict(float)
        for r in m["files"]["summary"][1]:
            s[(r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        for r in m["files"]["breakdown"][1]:
            b[(r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        for key in b:
            self.assertAlmostEqual(b[key], s[key], places=5, msg=key)
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), 2 * self.total, places=5)

    def test_re_export_from_one_machine_still_counts_once(self):
        self._two_machines()
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", machine="laptop",
                        name="claude-usage-export-a-laptop-again-20260212.zip")
        m = self._merge()
        for kind in ("summary", "breakdown", "sessions"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), 2 * self.total, places=5, msg=kind)
        self.assertEqual((m["people"][0]["exports"], m["people"][0]["machines"]), (3, 2))
        self.assertTrue(any("from one machine with overlapping" in w["message"] for w in m["warnings"]))

    def test_same_logs_under_a_new_machine_id_count_once(self):
        # The tool was re-cloned, so the same machine got a new id; the shared
        # sessions give it away.
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="old-clone")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", machine="new-clone",
                        name="claude-usage-export-a-again-20260212.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertTrue(any("share sessions" in w["message"] for w in m["warnings"]))

    def test_exports_without_machine_ids_merge_as_before_and_say_so(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine=None)
        self.folder.zip(_on_another_machine(self.payload), "a@example.com", "a@dev.io", "20260210",
                        machine=None, name="claude-usage-export-a-desktop-20260210.zip")
        m = self._merge()
        # Today's behaviour: one machine's rows per week and project survive.
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        self.assertAlmostEqual(_cost(m["files"]["sessions"][1]), 2 * self.total, places=5)
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertTrue(any("no machine id" in w["message"] for w in m["warnings"]))
        self.assertIn("no machine id", team.report_text(m))

    def test_an_old_export_joins_the_machine_whose_sessions_it_shares(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260201", machine=None,
                        name="claude-usage-export-a-old-20260201.zip")
        self._two_machines()
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), 2 * self.total, places=5)
        self.assertEqual(m["people"][0]["machines"], 2)
        self.assertFalse(any("no machine id" in w["message"] for w in m["warnings"]))

    def test_a_limit_episode_seen_on_two_machines_counts_once(self):
        limits = _scan(LIMIT_FIXTURES)
        self._two_machines(payload=limits)
        m = self._merge()
        p = m["people"][0]
        self.assertEqual(p["machines"], 2)
        self.assertEqual((p["limit_hits"], p["lockouts"]), (2, 1))
        rows = m["files"]["limits"][1]
        self.assertEqual(len(rows), 2)
        one = [e for e in limits["limits"] if e["resets_at"]]
        for row in rows:
            ep = next(e for e in one if e["kind"] == row["limit_type"] and e["resets_at"] == row["resets_at"])
            self.assertEqual(row["hit_at"], ep["hit_at"])
            self.assertEqual(int(row["notices"]), 2 * ep["notices"])
            if ep["usage_after_hit_usd"] is not None:
                # Each machine's usage after the hit was real extra usage.
                self.assertAlmostEqual(float(row["usage_after_hit_usd"]), 2 * ep["usage_after_hit_usd"],
                                       places=5)
            self.assertEqual(len(row["machine_id"].split("; ")), 2)
            self.assertEqual(len(row["merged_from"].split("; ")), 2)
        single = TeamFolder()
        try:
            single.zip(limits, "a@example.com", "a@dev.io", "20260210")
            alone = team._load([single.dir])["people"][0]
        finally:
            single.close()
        self.assertAlmostEqual(p["extra_usage_usd"], 2 * alone["extra_usage_usd"], places=2)

    def test_a_limit_hit_later_on_the_second_machine_is_still_the_same_episode(self):
        limits = _scan(LIMIT_FIXTURES)
        later = _on_another_machine(limits)
        later["limits"][0]["hit_at"] = "2026-03-02T23:59:00Z"  # noticed later on this machine
        self._two_machines(payload=limits, desktop_payload=later)
        m = self._merge()
        self.assertEqual(m["people"][0]["limit_hits"], 2)
        first = next(r for r in m["files"]["limits"][1]
                     if r["resets_at"] == limits["limits"][0]["resets_at"]
                     and r["limit_type"] == limits["limits"][0]["kind"])
        self.assertEqual(first["hit_at"], limits["limits"][0]["hit_at"], "the earliest hit wins")

    def test_activity_combines_across_machines(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="laptop")
        quiet = _on_another_machine(self.payload)
        quiet["usage"] = [r for r in quiet["usage"] if r[0] < "2026-01-05"]
        self.folder.zip(quiet, "a@example.com", "a@dev.io", "20260211", machine="desktop",
                        name="claude-usage-export-a-desktop-20260211.zip")
        exports, _ = team.read_exports([self.folder.dir])
        busy = next(e for e in exports if "desktop" not in e.source)
        p = team.merge(exports)["people"][0]
        # The busier machine's active days, not the newer export's.
        self.assertEqual(p["person_active_days"], int(busy.context("person_active_days")))
        self.assertEqual(p["last_active_date"], busy.context("last_active_date"))


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the local zone")
class TestJoinedExportsWarnByCost(unittest.TestCase):
    """BUG-04 (review): when two exports joined by a shared session each supply
    part of a week, the warning is about cost the summary lost, judged by
    comparing the exports' weekly cost -- not by session ids, which a resumed
    session changes, nor by the week a session started in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = TeamFolder()
        self.addCleanup(self.folder.close)
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _logs(self, name, files):
        root = os.path.join(self.tmp, name)
        for fn, records in files.items():
            _write(os.path.join(root, "-Users-x-p", fn + ".jsonl"), records)
        return scan(root, cache=FileCache(os.path.join(self.tmp, name + ".json")))

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    @staticmethod
    def _warns(m):
        return [w["message"] for w in m["warnings"] if w["level"] == "warn"]

    def test_resuming_a_session_between_exports_is_not_a_loss(self):
        # xxxx is exported, then resumed as yyyy (a copy plus new requests), then
        # exported again. The work moved to yyyy's id; none of it is missing.
        x = [_response(f"x{i}", _ts(i, day=2), inp=1_000_000) for i in range(5)]
        z = [_response(f"z{i}", _ts(i, day=1)) for i in range(2)]  # held by both exports
        first = self._logs("one", {"xxxx": x, "zzzz": z})
        resumed = self._logs("two", {"xxxx": x, "zzzz": z,
                                     "yyyy": x + [_response(f"y{i}", _ts(i, day=4)) for i in range(3)]})
        self.folder.zip(first, "a@example.com", "a@dev.io", "20260203", until="2026-02-03", machine="laptop",
                        name="e1.zip")
        self.folder.zip(resumed, "a@example.com", "a@dev.io", "20260205", until="2026-02-05", machine="laptop",
                        name="e2.zip")
        m = self._merge()
        truth = sum(r[13] for r in resumed["usage"])
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), truth, places=5)
        self.assertEqual(self._warns(m), [])

    def test_resuming_after_the_machine_changed_time_zone_is_still_one_machine(self):
        # The same session instant is written in the exporting machine's local
        # offset, so a laptop that travels between exports writes "...-08:00"
        # and then "...-05:00" for it. Matching on the raw string splits one
        # machine in two and counts the shared work twice.
        def zoned(hours):
            tz = dt.timezone(dt.timedelta(hours=hours))

            def local(ts, _tz=None):
                if not ts:
                    return ts
                moment = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                return moment.astimezone(tz).isoformat(timespec="seconds")
            return mock.patch.object(export_mod, "_local_time", local)

        x = [_response(f"x{i}", _ts(i, day=2), inp=1_000_000) for i in range(5)]
        first = self._logs("one", {"xxxx": x})
        resumed = self._logs("two", {"yyyy": x + [_response(f"y{i}", _ts(i, day=4)) for i in range(3)]})
        with zoned(-8):
            self.folder.zip(first, "a@example.com", "a@dev.io", "20260203", until="2026-02-03",
                            machine="laptop", name="e1.zip")
        with zoned(-5):
            self.folder.zip(resumed, "a@example.com", "a@dev.io", "20260205", until="2026-02-05",
                            machine="laptop", name="e2.zip")
        m = self._merge()
        truth = sum(r[13] for r in resumed["usage"])
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), truth, places=5)
        self.assertEqual(self._warns(m), [])

    def test_a_session_spanning_a_week_boundary_is_not_dropped_silently(self):
        # Desktop's session S starts Sunday 02-01 and has its big request on
        # Monday 02-02. It is a desktop-only session, so the week of 02-02 is
        # taken from the laptop export and S's Monday cost is dropped.
        def at(day, hour):
            return f"2026-{day}T{hour:02d}:00:00.000Z"
        shared = [_response("z0", at("01-28", 10), inp=1_000_000)]
        lap = self._logs("lap", {"zzzz": shared, "tttt": [_response("t0", at("02-03", 10), inp=1_000_000)]})
        desk = self._logs("desk", {"zzzz": shared, "ssss": [
            _response("s0", at("02-01", 23), inp=1_000_000), _response("s1", at("02-02", 1), inp=3_000_000)]})
        for payload, machine, name in ((lap, "laptop", "lap.zip"), (desk, "desktop", "desk.zip")):
            self.folder.zip(payload, "a@example.com", "a@dev.io", "20260209", since="2026-01-26",
                            until="2026-02-08", machine=machine, name=name)
        m = self._merge()
        self.assertEqual(m["people"][0]["machines"], 1)
        warns = [w for w in self._warns(m) if "only one of two joined exports holds" in w]
        self.assertEqual(len(warns), 1, self._warns(m))
        self.assertIn("a@example.com", warns[0])
        self.assertIn("1 session (est. $6.00)", warns[0])  # ssss's Monday request, not the $4 weekly gap
        self.assertIn("1 in the week of 2026-02-02", warns[0])

    def test_a_longer_export_of_the_same_sessions_is_not_a_loss(self):
        # The second export runs further and holds every session of the first
        # under the same ids (nothing was resumed): the first adds nothing.
        x = [_response(f"x{i}", _ts(i, day=2), inp=1_000_000) for i in range(3)]
        z = [_response(f"z{i}", _ts(i, day=1)) for i in range(2)]
        short = self._logs("one", {"xxxx": x, "zzzz": z})
        longer = self._logs("two", {"xxxx": x + [_response("x9", _ts(9, day=4))], "zzzz": z})
        self.folder.zip(short, "a@example.com", "a@dev.io", "20260203", until="2026-02-03", machine="laptop", name="e1.zip")
        self.folder.zip(longer, "a@example.com", "a@dev.io", "20260205", until="2026-02-05", machine="laptop", name="e2.zip")
        self.assertEqual(self._warns(self._merge()), [])

    def test_a_copy_that_only_resembles_a_session_is_not_taken_for_its_continuation(self):
        # Same start and project is how a resumed session's continuation looks, but
        # a continuation also runs on past the original. A second machine's own
        # session that merely starts at the same moment does not.
        z = [_response("z0", _ts(0, day=1))]
        lap = self._logs("lap", {"zzzz": z, "aaaa": [_response("a0", _ts(5, day=2), inp=1_000_000)]})
        desk = self._logs("desk", {"zzzz": z, "bbbb": [_response("b0", _ts(5, day=2), inp=1_000_000)]})
        self.folder.zip(lap, "a@example.com", "a@dev.io", "20260209", machine="laptop", name="lap.zip")
        self.folder.zip(desk, "a@example.com", "a@dev.io", "20260209", machine="desktop", name="desk.zip")
        warns = [w for w in self._warns(self._merge()) if "only one of two joined exports holds" in w]
        self.assertEqual(len(warns), 1)
        self.assertIn("1 session", warns[0])


    # ---- fix round 2 ------------------------------------------------------ #

    @staticmethod
    def _at(day, hour, minute=0):
        return f"2026-{day}T{hour:02d}:{minute:02d}:00.000Z"

    def _join(self, exports):
        """exports: [(payload, machine, until, name, since)] -> merged."""
        for payload, machine, until, name, since in exports:
            self.folder.zip(payload, "a@example.com", "a@dev.io", "20260301", since=since, until=until,
                            machine=machine, name=name)
        return self._merge()

    def _loss(self, m):
        return [w for w in self._warns(m) if "joined exports" in w and "missing from the summary" in w]

    def test_a_session_lost_in_three_weeks_is_credited_in_each(self):
        at = self._at
        z = [_response("z0", at("01-28", 10), inp=1000)]
        s3 = [_response("s0", at("02-01", 23), inp=1_000_000), _response("s1", at("02-04", 1), inp=3_000_000),
              _response("s2", at("02-11", 1), inp=2_000_000)]
        lap = self._logs("lap", {"zzzz": z, "tttt": [_response("t0", at("02-03", 10), inp=1_000_000)],
                                 "uuuu": [_response("u0", at("02-10", 10), inp=2_000_000)]})
        desk = self._logs("desk", {"zzzz": z, "ssss": s3})
        m = self._join([(lap, "laptop", "2026-02-15", "lap.zip", "2026-01-26"),
                        (desk, "desktop", "2026-02-15", "desk.zip", "2026-01-26")])
        (warn,) = self._loss(m)
        self.assertIn("1 session (est. $10.00)", warn)  # $6 of the week of 02-02 and $4 of 02-09
        self.assertIn("1 in the week of 2026-02-02", warn)
        self.assertIn("est. $4.00 in the week of 2026-02-09", warn)

    def test_an_exact_copy_that_owns_the_requests_is_not_a_loss(self):
        # The continuation is a byte copy (another id, no later record). It sorts
        # first so it owns the requests, and the original is not even exported.
        at = self._at
        x = [_response(f"x{i}", at("02-02", 10, i), inp=1_000_000) for i in range(5)]
        z = [_response("z0", at("02-01", 10), inp=1000)]
        one = self._logs("s1", {"xxxx": x, "zzzz": z})
        two = self._logs("s2", {"xxxx": x, "aaaa": x, "zzzz": z})
        m = self._join([(one, "laptop", "2026-02-03", "e1.zip", None), (two, "laptop", "2026-02-05", "e2.zip", None)])
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), sum(r[13] for r in two["usage"]), places=5)
        self.assertEqual(self._loss(m), [])

    def test_a_re_export_from_another_timezone_is_not_a_loss(self):
        # The same logs fall in different weeks by the local day, so the two
        # exports' weekly costs differ -- but they hold the same sessions.
        at = self._at
        files = {"aaaa": [_response("a0", at("02-04", 10), inp=1_000_000), _response("a1", at("02-09", 3), inp=1_000_000)],
                 "bbbb": [_response("b0", at("02-10", 10), inp=1_000_000)]}
        payloads = {}
        for tz in ("UTC", "America/Los_Angeles"):
            os.environ["TZ"] = tz
            time.tzset()
            payloads[tz] = self._logs("p-" + tz.replace("/", "-"), files)
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.folder.zip(payloads["UTC"], "a@example.com", "a@dev.io", "20260216", since="2026-02-02",
                        until="2026-02-15", machine="laptop", name="e1.zip")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.folder.zip(payloads["America/Los_Angeles"], "a@example.com", "a@dev.io", "20260217", since="2026-02-02",
                        until="2026-02-15", machine="laptop", name="e2.zip")
        m = self._merge()
        self.assertEqual(self._warns(m), [])

    def test_a_dropped_session_with_no_cost_still_warns(self):
        at = self._at
        z = [_response("z0", at("02-02", 9), inp=1_000_000)]
        t = [_response("t0", at("02-03", 10), inp=1_000_000)]
        # Each export has one session with no requests that the other lacks.
        lap = self._logs("lap", {"zzzz": z, "tttt": t, "vvvv": [_prompt("p1", at("02-03", 12))]})
        desk = self._logs("desk", {"zzzz": z, "tttt": t, "ssss": [_prompt("p0", at("02-03", 11))]})
        m = self._join([(lap, "laptop", "2026-02-08", "lap.zip", "2026-02-02"),
                        (desk, "desktop", "2026-02-08", "desk.zip", "2026-02-02")])
        (warn,) = self._loss(m)
        self.assertIn("1 session (est. $0.00)", warn)
        self.assertIn("1 in the week of 2026-02-02", warn)

    def test_three_joined_machines_are_not_called_two(self):
        at = self._at
        z = [_response("z0", at("02-02", 9), inp=1000)]
        logs = [self._logs(n, {"zzzz": z, n * 4: [_response(n + "0", at("02-03", 10 + i), inp=(i + 1) * 1_000_000)]})
                for i, n in enumerate("abc")]
        m = self._join([(p, f"m{i}", "2026-02-08", f"{i}.zip", "2026-02-02") for i, p in enumerate(logs)])
        (warn,) = self._loss(m)
        self.assertNotIn("one of two", warn)
        self.assertIn("3 joined exports", warn)
        self.assertIn("2 sessions", warn)
        self.assertRegex(warn, r"\) .* are missing")

    def test_one_missing_session_is_singular(self):
        at = self._at
        z = [_response("z0", at("02-02", 9), inp=1000)]
        lap = self._logs("lap", {"zzzz": z, "tttt": [_response("t0", at("02-03", 10), inp=1_000_000)]})
        desk = self._logs("desk", {"zzzz": z, "ssss": [_response("s0", at("02-03", 11), inp=1_000_000)]})
        m = self._join([(lap, "laptop", "2026-02-08", "lap.zip", "2026-02-02"),
                        (desk, "desktop", "2026-02-08", "desk.zip", "2026-02-02")])
        (warn,) = self._loss(m)
        self.assertIn("1 session (est. $", warn)
        self.assertIn("only one of two joined exports holds is missing", warn)


class TestPeopleAndForecast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = TeamFolder()
        cls.folder.zip(_scan(LIMIT_FIXTURES), "lim@example.com", "lim@dev.io", "20260310")
        cls.folder.zip(_scan(), "calm@example.com", "calm@dev.io", "20260310")
        exports, problems = team.read_exports([cls.folder.dir])
        cls.merged = team.merge(exports, problems)
        cls.people = {p["person"]: p for p in cls.merged["people"]}

    @classmethod
    def tearDownClass(cls):
        cls.folder.close()

    def test_limit_totals_per_person(self):
        p = self.people["lim@example.com"]
        self.assertEqual((p["limit_hits"], p["lockouts"]), (2, 1))
        self.assertGreater(p["locked_out_hours"], 0)
        self.assertGreater(p["extra_usage_usd"], 0)
        self.assertEqual(self.people["calm@example.com"]["limit_hits"], 0)

    def test_seat_fields_come_through(self):
        p = self.people["calm@example.com"]
        self.assertEqual(p["seat_tier"], "enterprise_tier_1")
        self.assertIsNotNone(p["seat_use"])
        self.assertLessEqual(p["seat_use"], 1.0)

    def test_forecast_is_a_stated_run_rate(self):
        f = team.overage_forecast(self.merged, today=dt.date(2026, 3, 20))
        weekly = dict(f["weekly"])
        self.assertGreater(weekly["2026-03-02"], 0)
        self.assertEqual(f["basis_weeks"], 4)
        self.assertAlmostEqual(f["next_4_weeks_usd"], f["weekly_run_rate_usd"] * 4, places=1)

    def test_written_files(self):
        with tempfile.TemporaryDirectory() as out:
            _, written = team.write_merged([self.folder.dir], out)
            names = sorted(os.path.basename(p) for p in written)
            self.assertEqual(names, ["merge-report.txt", "team-breakdown.csv", "team-limits.csv",
                                     "team-people.csv", "team-prs.csv", "team-sessions.csv",
                                     "team-summary.csv", "team-tools.csv"])
            with open(os.path.join(out, "team-people.csv"), encoding="utf-8-sig") as fh:
                header = next(csv.reader(fh))
            self.assertEqual(header, team.PEOPLE_COLUMNS)

    def test_merged_csvs_carry_a_bom_for_excel_but_the_report_does_not(self):
        bom = b"\xef\xbb\xbf"
        with tempfile.TemporaryDirectory() as out:
            _, written = team.write_merged([self.folder.dir], out)
            for path in written:
                with open(path, "rb") as fh:
                    head = fh.read(3)
                if path.endswith(".csv"):
                    self.assertEqual(head, bom, path)
                else:
                    self.assertNotEqual(head, bom, path)
        with zipfile.ZipFile(io.BytesIO(team.zip_merged([self.folder.dir]))) as archive:
            for name in archive.namelist():
                self.assertEqual(archive.read(name).startswith(bom), name.endswith(".csv"), name)

    def test_a_merged_csv_reads_back_without_the_bom_in_its_header(self):
        with tempfile.TemporaryDirectory() as out:
            team.write_merged([self.folder.dir], out)
            with open(os.path.join(out, "team-summary.csv"), "rb") as fh:
                data = fh.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        columns, rows = team._read_csv("summary", team._text(io.BytesIO(data)))
        self.assertEqual(columns[0], "person")
        self.assertTrue(rows)

    def test_per_person_zip_members_stay_bom_free_so_cat_joins_work(self):
        folder = TeamFolder()
        self.addCleanup(folder.close)
        path = folder.zip(_scan(), "x@example.com", "x@dev.io", "20260301")
        with zipfile.ZipFile(path) as archive:
            self.assertTrue(archive.namelist())
            for name in archive.namelist():
                self.assertFalse(archive.read(name).startswith(b"\xef\xbb\xbf"), name)

    def test_collect_merge_cli(self):
        with tempfile.TemporaryDirectory() as out:
            ok = subprocess.run([sys.executable, "collect.py", "--merge", self.folder.dir, "--out", out],
                                cwd=REPO, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("2 people", ok.stdout)
            self.assertTrue(os.path.exists(os.path.join(out, "team-summary.csv")))
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as out:
            none = subprocess.run([sys.executable, "collect.py", "--merge", empty, "--out", out],
                                  cwd=REPO, capture_output=True, text=True)
            self.assertEqual(none.returncode, 1)
            self.assertIn("No exports found", none.stdout)


class TestTeamServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import serve
        cls.serve = serve
        cls.folder = TeamFolder()
        cls.folder.zip(_scan(), "a@example.com", "a@dev.io", "20260210")
        serve.Handler.team_paths = [cls.folder.dir]
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_port
        serve.Handler.port = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.serve.Handler.team_paths = None
        cls.folder.close()

    def _get(self, path, host=None, site="same-origin"):
        # The dashboard's own page sends Sec-Fetch-Site: same-origin on its fetches.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if site:
            headers["Sec-Fetch-Site"] = site
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_root_is_the_team_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"team.js", body)

    def test_team_api(self):
        status, body = self._get("/api/team")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([p["person"] for p in payload["people"]], ["a@example.com"])
        for key in ("weekly", "models", "limits", "sources", "warnings", "forecast"):
            self.assertIn(key, payload)

    def test_merged_zip(self):
        status, body = self._get("/api/team/export.zip")
        self.assertEqual(status, 200)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        self.assertIn("team-people.csv", names)
        self.assertIn("merge-report.txt", names)

    def test_personal_routes_are_off_in_team_mode(self):
        self.assertEqual(self._get("/api/data")[0], 404)
        self.assertEqual(self._get("/api/export/all.zip")[0], 404)

    def test_host_check_still_applies(self):
        self.assertEqual(self._get("/api/team", host="evil.example.com")[0], 403)

    def test_team_api_needs_an_origin_signal(self):
        self.assertEqual(self._get("/api/team", site=None)[0], 403)
        self.assertEqual(self._get("/api/team/export.zip", site=None)[0], 403)
        self.assertEqual(self._get("/api/team", site="cross-site")[0], 403)
        self.assertEqual(self._get("/", site=None)[0], 200)

    def test_team_script_is_served(self):
        self.assertEqual(self._get("/static/team.js")[0], 200)


class TestTeamPageEscaping(unittest.TestCase):
    """Everything on the team page comes from other people's files."""

    UNTRUSTED = ("person", "person_name", "source", "project", "limit_type", "files", "seat_tier",
                 "rate_limit_tier", "window_from", "window_to", "last_active_date",
                 "cli_history_from", "message", "folder", "extra_usage_enabled", "machine_id")

    def test_every_untrusted_field_is_escaped(self):
        # Inside every ${...} interpolation, each reference to an untrusted
        # field must sit directly inside esc(...) or a fmt*(...) formatter.
        src = (REPO / "static" / "team.js").read_text(encoding="utf-8")
        field = "|".join(self.UNTRUSTED)
        checked = 0
        for start in [m.end() for m in re.finditer(r"\$\{", src)]:
            expr = src[start:start + 200].split("\n", 1)[0]
            # A field only compared (=== 'yes', && ...) never reaches the page.
            for ref in re.finditer(r"(\w+\()?\b[a-z]\w*\.(%s)\b(?!\s*(===|!==|&&|\|\|))" % field, expr):
                wrapper = ref.group(1) or ""
                checked += 1
                self.assertTrue(wrapper == "esc(" or wrapper.startswith("fmt"),
                                f"{ref.group(0)!r} in ${{{expr[:60]}...}} must go through esc()")
        self.assertGreater(checked, 10, "the check matched nothing -- the regex is broken")

    def test_template_scripts_are_served(self):
        import serve
        html = (REPO / "static" / "team.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:src|href)="static/([^"]+)"', html))
        self.assertFalse(referenced - serve.ALLOWED_STATIC_FILES)



class TestSeatValue(unittest.TestCase):
    def setUp(self):
        self.folder = TeamFolder()
        self.folder.zip(_scan(), "a@example.com", "a@dev.io", "20260210")

    def tearDown(self):
        self.folder.close()

    def _prices(self, body, name=team.SEAT_PRICE_FILE):
        path = os.path.join(self.folder.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))
        return path

    def test_no_prices_means_blank_value_and_a_note(self):
        m = team._load([self.folder.dir])
        self.assertIsNone(m["people"][0]["value_multiple"])
        self.assertTrue(any(team.SEAT_PRICE_FILE in w["message"] for w in m["warnings"]))

    def test_prices_found_next_to_the_exports(self):
        self._prices({"per": "month", "prices": {"default": 30}})
        p = team._load([self.folder.dir])["people"][0]
        self.assertEqual(p["seat_price"], 30)
        self.assertIsNotNone(p["value_multiple"])

    def test_rate_limit_tier_beats_seat_tier_beats_default(self):
        prices = {"per": "month", "prices": {"default": 1, "enterprise_tier_1": 2,
                                             "default_claude_max_5x": 3}}
        person = {"rate_limit_tier": "default_claude_max_5x", "seat_tier": "enterprise_tier_1"}
        self.assertEqual(team._seat_price(person, prices), 3)
        self.assertEqual(team._seat_price({"seat_tier": "enterprise_tier_1"}, prices), 2)
        self.assertEqual(team._seat_price({"seat_tier": "other"}, prices), 1)
        self.assertIsNone(team._seat_price({"seat_tier": "other"}, {"per": "month", "prices": {"x": 1}}))

    def test_value_arithmetic(self):
        # 30 days at a monthly price equal to 5 x (days per month): $150 of seat.
        # $450 of list-price usage, no extra usage -> 3.0x.
        people = [{"rate_limit_tier": "t", "window_from": "2026-01-01", "window_to": "2026-01-30",
                   "est_cost_usd": 450.0, "extra_usage_usd": 0.0}]
        team.apply_seat_value(people, {"per": "month", "prices": {"t": 365.25 / 12 * 5}})
        self.assertAlmostEqual(people[0]["seat_cost_in_window"], 150.0, places=2)
        self.assertEqual(people[0]["value_multiple"], 3.0)
        # Yearly price: 365 days at $365.25/year is $365 of seat. Extra usage is
        # part of what was paid: $1,095 of usage / ($365 + $0) = 3x, but with
        # $365 of extra usage it halves.
        people = [{"seat_tier": "s", "window_from": "2025-01-01", "window_to": "2025-12-31",
                   "est_cost_usd": 1095.0, "extra_usage_usd": 365.0}]
        team.apply_seat_value(people, {"per": "year", "prices": {"s": 365.25}})
        self.assertAlmostEqual(people[0]["seat_cost_in_window"], 365.0, places=2)
        self.assertEqual(people[0]["value_multiple"], 1.5)

    def test_bad_price_files_are_reported(self):
        for body in ("{not json", {"per": "week", "prices": {"default": 1}},
                     {"prices": {"default": -5}}, {"prices": {}},
                     {"prices": {"default": 0}}):
            self._prices(body)
            m = team._load([self.folder.dir])
            self.assertIsNone(m["prices"], body)
            self.assertTrue(any("could not be used" in w["message"] for w in m["warnings"]), body)

    def test_non_finite_prices_are_rejected_like_non_positive_ones(self):
        # "Infinity" and "1e400" parse as float('inf'), "NaN" as nan, and nan
        # compares false to everything -- so `<= 0` alone let all three through.
        for body in ('{"prices": {"default": "Infinity"}}', '{"prices": {"default": "NaN"}}',
                     '{"prices": {"default": "1e400"}}', '{"prices": {"default": 1e400}}',
                     '{"prices": {"default": NaN}}', '{"prices": {"default": Infinity}}',
                     '{"prices": {"default": 30, "enterprise_tier_1": "-Infinity"}}'):
            self._prices(body)
            m = team._load([self.folder.dir])
            self.assertIsNone(m["prices"], body)
            self.assertTrue(any("could not be used" in w["message"] for w in m["warnings"]), body)

    def test_prices_that_are_not_a_real_amount_are_rejected(self):
        # A 401-digit int overflows float(); `true` would read as $1; 1e-320 is
        # not a price. Each is reported, not raised.
        for body in ('{"prices": {"default": %s}}' % ("9" * 401),
                     '{"prices": {"default": true}}',
                     '{"prices": {"default": 30, "enterprise_tier_1": false}}',
                     '{"prices": {"default": 1e-320}}',
                     '{"prices": {"default": 0.009}}'):
            with self.subTest(body[:60]):
                self._prices(body)
                m = team._load([self.folder.dir])
                self.assertIsNone(m["prices"])
                self.assertTrue(any("could not be used" in w["message"] for w in m["warnings"]))

    def test_the_smallest_accepted_price_is_a_cent(self):
        self._prices({"prices": {"default": 0.01}})
        self.assertEqual(team._load([self.folder.dir])["prices"]["prices"], {"default": 0.01})

    def test_explicit_price_file(self):
        elsewhere = os.path.join(self.folder.dir, "sub")
        os.makedirs(elsewhere)
        path = os.path.join(elsewhere, "prices.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"prices": {"default": 10}}, fh)
        self.assertEqual(team._load([self.folder.dir], path)["prices"]["prices"], {"default": 10.0})
        missing = team._load([self.folder.dir], path + ".nope")
        self.assertTrue(any("not found" in w["message"] for w in missing["warnings"]))

    def test_value_reaches_people_file_and_report(self):
        self._prices({"prices": {"default": 30}})
        with tempfile.TemporaryDirectory() as out:
            team.write_merged([self.folder.dir], out)
            with open(os.path.join(out, "team-people.csv"), encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
            self.assertTrue(row["value_multiple"])
            with open(os.path.join(out, "merge-report.txt"), encoding="utf-8") as fh:
                self.assertIn("Value per seat", fh.read())


class TestPrMerge(unittest.TestCase):
    def test_prs_merge_once_per_url_and_reach_the_payload(self):
        folder = TeamFolder()
        try:
            payload = _scan()
            folder.zip(payload, "a@example.com", "a@dev.io", "20260210")
            folder.zip(payload, "a@example.com", "a@dev.io", "20260212",
                       name="claude-usage-export-a-again-20260212.zip")
            m = team._load([folder.dir])
            self.assertEqual(len(m["files"]["prs"][1]), len(payload["prs"]))
            self.assertEqual(m["people"][0]["prs"], len(payload["prs"]))
            tp = team.team_payload([folder.dir])
            self.assertEqual(len(tp["prs"]), len(payload["prs"]))
            self.assertEqual(tp["prs"][0]["pr_repo"], "github.com/x/proj1")
        finally:
            folder.close()


def _with_session_on(payload, day, sid="sess-extra", tz=None):
    """`payload` plus a one-request session on `day` (proj1), so a test can put
    work on both sides of a window boundary. Its record and its request agree,
    so the summary and sessions files still reconcile."""
    out = json.loads(json.dumps(payload))
    base = next(s for s in out["sessions"] if s["id"] == "sess-main-2")
    out["sessions"].append({**base, "id": sid, "session_id": sid, "title": sid,
                            "start": f"{day}T18:00:00.000Z", "end": f"{day}T18:00:01.000Z"})
    local = dt.datetime.fromisoformat(f"{day}T18:00:00+00:00").astimezone(tz)
    for r in [r for r in out["usage"] if r[1] == "sess-main-2"]:
        out["usage"].append([local.strftime("%Y-%m-%dT%H:00"), sid] + r[2:])
    return out


def _set_member_method(data, member, method):
    """The zip `data` with `member`'s compression method set to `method` in
    both its local header and the central directory."""
    import struct
    b = bytearray(data)
    name = member.encode()
    i = 0
    while (i := b.find(b"PK\x01\x02", i)) != -1:
        n = struct.unpack("<H", b[i + 28:i + 30])[0]
        if b[i + 46:i + 46 + n] == name:
            b[i + 10:i + 12] = struct.pack("<H", method)
            local = struct.unpack("<I", b[i + 42:i + 46])[0]
            b[local + 8:local + 10] = struct.pack("<H", method)
        i += 4
    return bytes(b)


class TestWindowsThatSplitAWeek(unittest.TestCase):
    """One machine exported in pieces. Complementary windows add up; windows
    that overlap still count each day once."""

    @classmethod
    def setUpClass(cls):
        # Read in UTC and exported in UTC, so "2026-02-03" is the same day on
        # every machine (the fixtures' hours sit near local midnight in some).
        cls.payload = _with_session_on(_scan(tz=dt.timezone.utc), "2026-02-03", tz=dt.timezone.utc)
        cls.total = sum(r[13] for r in cls.payload["usage"])

    def setUp(self):
        self.folder = TeamFolder(tz=dt.timezone.utc)

    def tearDown(self):
        self.folder.close()

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    def test_complementary_windows_combine(self):
        # Monthly exports: one up to Wednesday Feb 4, the next from Thursday.
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-04",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-05",
                        name="claude-usage-export-a-feb-20260302.zip")
        m = self._merge()
        for kind in ("summary", "breakdown", "sessions"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), self.total, places=6, msg=kind)
        week = [r for r in m["files"]["summary"][1] if r["week_start"] == "2026-02-02"]
        self.assertEqual(len(week), 1, "one row for the week and project, not one per export")
        self.assertEqual(week[0]["week_days_in_window"], "7")
        self.assertEqual(week[0]["partial_week"], "no")
        self.assertEqual(len(week[0]["merged_from"].split("; ")), 2)
        self.assertAlmostEqual(m["people"][0]["est_cost_usd"], round(self.total, 2), places=2)

    def test_a_split_week_with_one_old_piece_has_no_exact_active_days(self):
        # Only the newer export knows week_active_days; adding its part to a
        # part the older export can't count would show a partial figure as exact.
        old = self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-04",
                              name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-05",
                        name="claude-usage-export-a-feb-20260302.zip")
        _drop_column(old, "week_active_days")
        m = self._merge()
        week = [r for r in m["files"]["summary"][1] if r["week_start"] == "2026-02-02"]
        self.assertTrue(week)
        self.assertTrue(all(r["week_active_days"] == "" for r in week), week)

    def test_a_split_week_with_both_pieces_exact_keeps_the_days(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-04",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-05",
                        name="claude-usage-export-a-feb-20260302.zip")
        week = [r for r in self._merge()["files"]["summary"][1] if r["week_start"] == "2026-02-02"]
        self.assertTrue(week)
        self.assertTrue(all(r["week_active_days"] != "" for r in week), week)

    def test_combined_breakdown_still_sums_to_the_summary(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-04",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-05",
                        name="claude-usage-export-a-feb-20260302.zip")
        m = self._merge()
        s, b = defaultdict(float), defaultdict(float)
        for r in m["files"]["summary"][1]:
            s[(r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        for r in m["files"]["breakdown"][1]:
            b[(r["week_start"], r["project"])] += float(r["est_cost_usd"] or 0)
        self.assertTrue(b)
        for key in b:
            self.assertAlmostEqual(b[key], s[key], places=6, msg=key)

    def test_overlapping_windows_count_each_day_once(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-04",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-03",
                        name="claude-usage-export-a-feb-20260302.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=6)

    def test_days_only_a_dropped_export_had_are_reported(self):
        # Both cover Tuesday Feb 3, so only one can be kept for that week; the
        # Monday only the shorter one had is lost, and the report says so.
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-02-03",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-02-03",
                        name="claude-usage-export-a-feb-20260302.zip")
        m = self._merge()
        self.assertTrue(any("2026-02-02" in w["message"] and "overlap" in w["message"]
                            for w in m["warnings"]), m["warnings"])

    def test_a_re_export_after_cli_cleanup_does_not_replace_complete_weeks(self):
        # The later export was made after Claude Code deleted sess-main-1's CLI
        # transcript: its January weeks are marked cli_history_complete = no.
        later = json.loads(json.dumps(self.payload))
        gone = "sess-main-1"
        later["usage"] = [r for r in later["usage"] if r[1] != gone]
        later["tools"] = [r for r in later["tools"] if r[1] != gone]
        later["sessions"] = [s for s in later["sessions"] if s["id"] != gone]
        # Named so the later export is read first: both are made within the
        # same second, so this is the order a tie on everything else falls to.
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210",
                        name="claude-usage-export-a-pre-20260210.zip")
        truncated = {"cli_sessions": 1, "cli_history_from": "2026-02-02",
                     "cli_history_truncated": True, "retention_days_default": 30}
        with mock.patch.object(context, "history_coverage", return_value=truncated):
            self.folder.zip(later, "a@example.com", "a@dev.io", "20260301",
                            name="claude-usage-export-a-cleaned-20260301.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=6)
        self.assertAlmostEqual(_cost(m["files"]["sessions"][1]), self.total, places=6)
        january = [r for r in m["files"]["summary"][1] if r["week_start"] < "2026-02-02"]
        self.assertTrue(january)
        self.assertTrue(all(r["cli_history_complete"] == "yes" for r in january))

    def test_a_re_export_missing_sessions_does_not_replace_the_week_that_has_them(self):
        # Same windows, but the later export lost a transcript without the
        # cleanup being detected: the sessions it lacks give it away.
        later = json.loads(json.dumps(self.payload))
        gone = "sess-main-1"
        later["usage"] = [r for r in later["usage"] if r[1] != gone]
        later["tools"] = [r for r in later["tools"] if r[1] != gone]
        later["sessions"] = [s for s in later["sessions"] if s["id"] != gone]
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210",
                        name="claude-usage-export-a-pre-20260210.zip")
        self.folder.zip(later, "a@example.com", "a@dev.io", "20260301",
                        name="claude-usage-export-a-cleaned-20260301.zip")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=6)
        self.assertAlmostEqual(_cost(m["files"]["sessions"][1]), self.total, places=6)


class TestReadingRobustness(unittest.TestCase):
    """Managers open zips from colleagues: a broken or hostile zip is listed
    and skipped, never fatal, and never half-merged."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.total = sum(r[13] for r in cls.payload["usage"])

    def setUp(self):
        self.folder = TeamFolder()
        self.good = self.folder.zip(self.payload, "b@example.com", "b@dev.io", "20260210")

    def tearDown(self):
        self.folder.close()

    def _bad(self, data, name="claude-usage-export-a-20260210.zip"):
        with open(os.path.join(self.folder.dir, name), "wb") as fh:
            fh.write(data)

    def _a_zip(self):
        path = self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        with open(path, "rb") as fh:
            data = fh.read()
        os.remove(path)
        return data

    def _assert_skipped(self, name="claude-usage-export-a-20260210.zip"):
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual([p["person"] for p in m["people"]], ["b@example.com"])
        self.assertTrue(any(name in p for p in problems), problems)
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=6)
        return problems

    def _members(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return [i.filename for i in archive.infolist()]

    def test_unsupported_compression_is_skipped(self):
        data = self._a_zip()
        self._bad(_set_member_method(data, self._members(data)[0], 99))
        self._assert_skipped()

    def test_encrypted_member_is_skipped(self):
        import struct
        b = bytearray(self._a_zip())
        for sig, flag_at in ((b"PK\x01\x02", 8), (b"PK\x03\x04", 6)):
            i = b.find(sig)
            flags = struct.unpack("<H", b[i + flag_at:i + flag_at + 2])[0] | 1
            b[i + flag_at:i + flag_at + 2] = struct.pack("<H", flags)
        self._bad(bytes(b))
        self._assert_skipped()

    def test_corrupt_last_member_skips_the_whole_export(self):
        import struct
        data = self._a_zip()
        b = bytearray(data)
        j = b.rfind(b"PK\x03\x04")  # the last member's local header
        n, extra = struct.unpack("<HH", b[j + 26:j + 30])
        start = j + 30 + n + extra
        for k in range(start, start + 16):
            b[k] ^= 0xFF
        self._bad(bytes(b))
        self._assert_skipped()

    def test_members_not_named_like_an_export_are_never_decompressed(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self._a_zip())) as src, \
                zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for info in src.infolist():
                out.writestr(info.filename, src.read(info))
            out.writestr("readme.bin", "x" * 1000)
        self._bad(_set_member_method(buf.getvalue(), "readme.bin", 99))
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])
        self.assertEqual(len(exports), 2)

    def test_an_export_too_large_to_be_real_is_skipped(self):
        self._bad(self._a_zip())
        with mock.patch.object(team, "_MAX_EXPORT_BYTES", 2000):
            exports, problems = team.read_exports([self.folder.dir])
        self.assertFalse(exports)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("too large" in p for p in problems), problems)

    def test_a_zip_with_too_many_members_is_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self._a_zip())) as src, \
                zipfile.ZipFile(buf, "w") as out:
            for info in src.infolist():
                out.writestr(info.filename, src.read(info))
            for i in range(30):
                out.writestr(f"junk-{i}.txt", "")
        self._bad(buf.getvalue())
        with mock.patch.object(team, "_MAX_MEMBERS", 20):
            problems = self._assert_skipped()
        self.assertTrue(any("entries" in p for p in problems), problems)

    def test_same_file_name_in_two_folders_is_two_exports(self):
        a = self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", machine="laptop")
        b = self.folder.zip(_on_another_machine(self.payload), "a@example.com", "a@dev.io", "20260301",
                            machine="desktop", name="desktop.zip")
        for sub, path in (("laptop", a), ("desktop", b)):
            os.makedirs(os.path.join(self.folder.dir, sub))
            shutil.move(path, os.path.join(self.folder.dir, sub, os.path.basename(a)))
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual(len(exports), 3)
        a_rows = [r for r in m["files"]["summary"][1] if r["person"] == "a@example.com"]
        self.assertAlmostEqual(_cost(a_rows), 2 * self.total, places=6)
        self.assertEqual(len({r["merged_from"] for r in a_rows}), 2, "sources stay distinguishable")

    # ---- files that unzip fine but must not be merged as they are --------- #

    def _rezipped(self, members):
        """A zip of [(member name, bytes)]."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for name, data in members:
                out.writestr(name, data)
        return buf.getvalue()

    def _a_members(self, payload=None):
        path = self.folder.zip(payload or self.payload, "a@example.com", "a@dev.io", "20260210",
                               machine="laptop" if payload is None else "desktop")
        with zipfile.ZipFile(path) as archive:
            members = [(i.filename, archive.read(i)) for i in archive.infolist()]
        os.remove(path)
        return members

    def _edited(self, kind, edit):
        """The a@ zip with its `kind` file's rows passed through `edit`
        (rows, columns) -> (rows, columns)."""
        members = []
        for name, data in self._a_members():
            if f"-{kind}-" in name:
                reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
                rows, columns = edit(list(reader), list(reader.fieldnames))
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                data = buf.getvalue().encode("utf-8")
            members.append((name, data))
        return self._rezipped(members)

    def _with_cell(self, kind, column, value):
        def edit(rows, columns):
            if not rows:  # (the fixture has no limit episodes)
                rows = [{"limit_type": "weekly", "resets_at": "2026-02-09T00:00:00Z",
                         "hit_at": "2026-02-05T10:00:00Z"}]
            rows[0][column] = value
            return rows, columns
        return self._edited(kind, edit)

    def _a_cost(self, merged):
        return _cost(r for r in merged["files"]["summary"][1] if r["person"] == "a@example.com")

    def test_finder_resource_forks_are_ignored(self):
        # Compress in macOS Finder: a folder, plus an AppleDouble file per
        # member under __MACOSX/ named like the member with "._" in front.
        members = []
        for name, data in self._a_members():
            members.append((f"claude-usage-export-a-20260210/{name}", data))
            members.append((f"__MACOSX/claude-usage-export-a-20260210/._{name}",
                            b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 40))
        self._bad(self._rezipped(members))
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual(problems, [])
        self.assertEqual(len(exports), 2)
        self.assertAlmostEqual(self._a_cost(m), self.total, places=6)
        a = next(p for p in m["people"] if p["person"] == "a@example.com")
        self.assertGreater(a["sessions"], 0)

    def test_two_machines_exported_into_one_zip_both_count(self):
        members = [(f"laptop/{n}", d) for n, d in self._a_members()]
        members += [(f"desktop/{n}", d) for n, d in self._a_members(_on_another_machine(self.payload))]
        self._bad(self._rezipped(members))
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual(problems, [])
        self.assertEqual(len(exports), 3)
        self.assertAlmostEqual(self._a_cost(m), 2 * self.total, places=6)
        self.assertEqual(len({s["source"] for s in m["sources"]}), 3, "sources stay distinguishable")

    def test_a_semicolon_delimited_file_is_skipped(self):
        # European Excel re-saves with ";": one column, nothing to merge on.
        members = [(n, d.replace(b",", b";") if b"-summary-" in n.encode() else d)
                   for n, d in self._a_members()]
        self._bad(self._rezipped(members))
        problems = self._assert_skipped()
        self.assertTrue(any("columns" in p for p in problems), problems)

    def test_a_loose_csv_with_the_wrong_columns_is_not_a_person(self):
        with open(os.path.join(self.folder.dir, "claude-usage-summary-x-20260210.csv"), "w",
                  encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n")
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual([p["person"] for p in m["people"]], ["b@example.com"])
        self.assertTrue(any("claude-usage-summary-x-20260210.csv" in p for p in problems), problems)

    def test_a_bad_loose_csv_skips_the_rest_of_its_export(self):
        # Loose CSVs of one export: a good sessions file and a summary
        # re-saved by Excel. Half of it would be a person with sessions and $0.
        for name, data in self._a_members():
            if "-summary-" in name:
                data = data.replace(b"2026-01-05,", b"1/5/2026,")
            with open(os.path.join(self.folder.dir, name), "wb") as fh:
                fh.write(data)
        exports, problems = team.read_exports([self.folder.dir])
        m = team.merge(exports, problems)
        self.assertEqual([p["person"] for p in m["people"]], ["b@example.com"])
        self.assertTrue(any("half-merged" in p for p in problems), problems)

    def test_dates_that_are_not_iso_skip_the_file(self):
        prices = os.path.join(self.folder.dir, team.SEAT_PRICE_FILE)
        with open(prices, "w", encoding="utf-8") as fh:
            json.dump({"per": "month", "prices": {"default": 100}}, fh)
        for kind, column, value in (("summary", "week_start", "9/30/2026"),
                                    ("summary", "window_from", "garbage"),
                                    ("sessions", "window_to", "30/09/2026"),
                                    ("breakdown", "week_start", "zzzz"),
                                    ("tools", "week_start", "!bad"),
                                    ("summary", "week_start", "2026-02-30"),
                                    ("limits", "hit_at", "!bad")):
            with self.subTest(kind=kind, column=column, value=value):
                self._bad(self._with_cell(kind, column, value))
                problems = self._assert_skipped()
                self.assertTrue(any(column in p for p in problems), problems)
                json.dumps(team.team_payload([self.folder.dir]), allow_nan=False)

    def test_non_finite_and_absurd_numbers_skip_the_file(self):
        for value in ("nan", "inf", "-Infinity", "1e308", "9" * 5000):
            with self.subTest(value=value[:10]):
                self._bad(self._with_cell("summary", "total_tokens", value))
                problems = self._assert_skipped()
                self.assertTrue(any("total_tokens" in p for p in problems), problems)
                json.dumps(team.team_payload([self.folder.dir]), allow_nan=False)

    def test_impossible_values_skip_the_file(self):
        # Costs, tokens and counts only ever add up from zero; a negative one is
        # a hand-edit or a bug that would quietly subtract from the team total.
        # One bad value skips the whole file, as an unreadable number does.
        for kind, column, value in (("summary", "est_cost_usd", "-500"),
                                    ("summary", "total_tokens", "-1"),
                                    ("breakdown", "est_cost_usd", "-0.01"),
                                    ("sessions", "est_cost_usd", "-500"),
                                    ("tools", "calls", "-3")):
            with self.subTest(kind=kind, column=column, value=value):
                self._bad(self._with_cell(kind, column, value))
                problems = self._assert_skipped()
                self.assertTrue(any(column in p and "negative" in p for p in problems), problems)

    def test_cache_saved_may_be_negative(self):
        # A cache write costs more than it saves until it is read again, so
        # cache_saved_usd is legitimately below zero (the tool's own fixture
        # export has such rows).
        for kind in ("summary", "breakdown", "sessions"):
            with self.subTest(kind=kind):
                self._bad(self._with_cell(kind, "cache_saved_usd", "-0.001"))
                exports, problems = team.read_exports([self.folder.dir])
                self.assertEqual(problems, [])
                m = team.merge(exports, problems)
                self.assertEqual(sorted(p["person"] for p in m["people"]),
                                 ["a@example.com", "b@example.com"])

    def test_negative_zero_is_not_negative(self):
        self._bad(self._with_cell("summary", "est_cost_usd", "-0.0"))
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])

    def test_the_tools_own_export_passes_validation_untouched(self):
        self.folder.close()
        self.folder = TeamFolder()
        path = self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210")
        with zipfile.ZipFile(path) as archive:
            saved = [float(r["cache_saved_usd"])
                     for n in archive.namelist() if "-summary-" in n
                     for r in csv.DictReader(io.StringIO(archive.read(n).decode("utf-8-sig")))
                     if r["cache_saved_usd"]]
        self.assertTrue(any(v < 0 for v in saved), "the fixture should exercise a negative saving")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])
        self.assertEqual(len(exports), 1)

    def test_a_week_that_does_not_start_on_monday_skips_the_file(self):
        for kind in ("summary", "breakdown", "tools"):
            with self.subTest(kind=kind):
                self._bad(self._with_cell(kind, "week_start", "2026-02-04"))  # a Wednesday
                problems = self._assert_skipped()
                self.assertTrue(any("week_start" in p and "Monday" in p for p in problems), problems)

    def test_a_schema_newer_than_the_tool_is_warned_about(self):
        newer = context.EXPORT_SCHEMA_VERSION + 992
        self._bad(self._with_cell("summary", "schema_version", str(newer)))
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])
        warnings = team.merge(exports, problems)["warnings"]
        newer_notes = [w for w in warnings if w["level"] == "warn" and "newer" in w["message"]]
        self.assertEqual(len(newer_notes), 1, warnings)
        self.assertIn(str(newer), newer_notes[0]["message"])
        self.assertIn("update", newer_notes[0]["message"])
        # The current-version file it is mixed with is not told to re-export.
        for w in warnings:
            self.assertNotRegex(w["message"], rf"version {context.EXPORT_SCHEMA_VERSION} to re-export")
        self.assertFalse([w for w in warnings if "re-export" in w["message"]], warnings)

    def test_a_newer_schema_and_an_older_one_each_get_their_own_advice(self):
        self.folder.schema1_summary(self.payload, "old@dev.io", "20260101")
        self._bad(self._with_cell("summary", "schema_version", "999"))
        exports, problems = team.read_exports([self.folder.dir])
        messages = [w["message"] for w in team.merge(exports, problems)["warnings"]]
        self.assertTrue(any("version 1 to re-export" in m for m in messages), messages)
        self.assertTrue(any("newer" in m and "999" in m for m in messages), messages)

    def test_a_corrupt_transcript_still_exports_a_file_the_merge_accepts(self):
        # Negative durations and CLI-reported costs in a damaged transcript must
        # not become negative columns, which the merge would reject: that would
        # drop the whole person from the team report.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _write(os.path.join(root, "-Users-x-corrupt", "s1.jsonl"), [
            _prompt("p1", _ts(0)),
            _response("m1", _ts(1)),
            {"type": "system", "subtype": "api_error", "uuid": "e1", "timestamp": _ts(2),
             "retryInMs": -5000},
            {"type": "system", "subtype": "turn_duration", "uuid": "t1", "timestamp": _ts(3),
             "durationMs": -120000},
            {"type": "cost-state", "startTime": "2026-02-01T09:00:00.000Z", "totalCostUSD": -3.5,
             "totalAPIDuration": -10, "totalToolDuration": -10, "totalDuration": -10,
             "modelUsage": {MODEL: {"inputTokens": 10, "outputTokens": 20, "costUSD": -0.2}}},
        ])
        self.folder.zip(_scan(root), "c@example.com", "c@dev.io", "20260210")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])
        self.assertEqual(len(exports), 2)  # b@ from setUp, and the corrupt one
        negative = [(kind, c, r[c]) for e in exports for kind, rows in e.files.items()
                    for r in rows for c in r if c in team._NUMBERS and c != "cache_saved_usd"
                    and r[c] not in (None, "") and float(r[c]) < 0]
        self.assertEqual(negative, [])
        # ...and the corrupt values were read as zero, not dropped with the file.
        retry = [r["retry_wait_minutes"] for e in exports for r in e.files["summary"]
                 if r["retry_wait_minutes"]]
        self.assertTrue(all(float(v) == 0 for v in retry), retry)

    def test_num_never_returns_a_non_finite_value(self):
        for value in ("nan", "inf", "-inf", "1e999"):
            self.assertEqual(team._num(value), 0.0, value)
            self.assertIsNone(team._num(value, None), value)

    def test_a_member_inflating_past_its_declared_size_is_stopped_early(self):
        import struct
        import tracemalloc
        name = "claude-usage-summary-a-dev.io-20260210.csv"
        data = bytearray(self._rezipped([(name, b"0" * (48 * 1024 * 1024))]))
        # Declare it as 1 kB in both headers: the inflated 48 MB is a lie.
        c = data.find(b"PK\x01\x02")
        data[c + 24:c + 28] = struct.pack("<I", 1024)
        data[22:26] = struct.pack("<I", 1024)  # the local header's
        self._bad(bytes(data))
        tracemalloc.start()
        try:
            self._assert_skipped()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024 * 1024, f"{peak / 1e6:.0f} MB held while reading")

    def test_hostile_column_names_are_neutralised_in_merged_files(self):
        def edit(rows, columns):
            for r in rows:
                r["=HYPERLINK(\"http://x\")"] = "1"
            return rows, columns + ["=HYPERLINK(\"http://x\")"]
        self._bad(self._edited("summary", edit))
        files = team.merged_files(team._load([self.folder.dir]))
        header = next(csv.reader(io.StringIO(files["team-summary.csv"])))
        self.assertFalse([h for h in header if h[:1] in ("=", "+", "-", "@")], header)


def _csv_zip(path, member, columns, rows):
    """A zip holding one export CSV built from `columns` and an iterable of rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(member, buf.getvalue())


class TestMemoryIsBounded(unittest.TestCase):
    """A zip is parsed row by row and rejected past a column, row or byte
    limit, so a small file can't make the manager's machine run out of memory
    (a 146 KB zip of 2,000 empty columns once took 1.9 GB)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _zip(self, name, kind, columns, rows, stamp="20260210"):
        path = os.path.join(self.tmp, name)
        _csv_zip(path, f"claude-usage-{kind}-a-{stamp}.csv", columns, rows)
        return path

    def _summary(self, name="a.zip", n=3):
        columns = list(export_mod.SUMMARY_COLUMNS)
        rows = []
        for i in range(n):
            row = {"week_start": "2026-02-02", "project": f"p{i}", "est_cost_usd": "1.5"}
            rows.append([row.get(c, "") for c in columns])
        return self._zip(name, "summary", columns, rows)

    def test_a_file_with_too_many_columns_is_skipped(self):
        columns = ["week_start", "project", "est_cost_usd"] + [f"extra{i}" for i in range(497)]
        self._zip("wide.zip", "summary", columns, [["2026-02-02", "p", "1"] + [""] * 497])
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("500 columns", problems[0])
        self.assertIn(str(team._MAX_COLUMNS), problems[0])

    def test_a_file_with_too_many_rows_is_skipped(self):
        self._zip("long.zip", "sessions", ["session_id", "est_cost_usd"],
                  ([f"s{i}", "0.1"] for i in range(300_000)))
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("rows", problems[0])
        self.assertIn(f"{team._MAX_ROWS:,}", problems[0])

    def test_row_limit_is_exact(self):
        self._zip("rows.zip", "sessions", ["session_id", "est_cost_usd"],
                  ([f"s{i}", "0.1"] for i in range(6)))
        with mock.patch.object(team, "_MAX_ROWS", 6):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual((len(exports), problems), (1, []))
        with mock.patch.object(team, "_MAX_ROWS", 5):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual((exports, len(problems)), ([], 1))

    def test_a_row_with_more_fields_than_the_header_is_skipped(self):
        self._zip("ragged.zip", "sessions", ["session_id", "est_cost_usd"],
                  [["s1", "0.1"], ["s2", "0.1", "x", "y"]])
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("line 3", problems[0])
        self.assertIn("more fields", problems[0])

    def test_a_short_row_still_reads_with_its_missing_cells_blank(self):
        self._zip("short.zip", "sessions", ["session_id", "est_cost_usd", "project"],
                  [["s1", "0.1"]])
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(problems, [])
        row = exports[0].files["sessions"][0]
        self.assertEqual((row["session_id"], row.get("project"), row.get("nope", "d")),
                         ("s1", None, "d"))

    def test_a_loose_csv_is_held_to_the_same_limits(self):
        path = os.path.join(self.tmp, "claude-usage-sessions-a-20260210.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("session_id,est_cost_usd\n" + "".join(f"s{i},0.1\n" for i in range(10)))
        with mock.patch.object(team, "_MAX_ROWS", 5):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertTrue(problems and "rows" in problems[0], problems)

    def test_zips_in_a_folder_share_one_byte_budget(self):
        for name in ("a.zip", "b.zip", "c.zip"):
            self._summary(name, n=50)
        with zipfile.ZipFile(os.path.join(self.tmp, "a.zip")) as archive:
            one = archive.infolist()[0].file_size
        with mock.patch.object(team, "_MAX_TOTAL_BYTES", one * 2 + one // 2):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(len(exports), 2)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("c.zip", problems[0])
        self.assertIn("MiB", problems[0])

    def test_a_zip_that_fails_gives_its_bytes_back_to_the_budget(self):
        self._zip("a-bad.zip", "summary", ["week_start", "project", "est_cost_usd"],
                  [["not a date", "p", "1"]] * 50)
        self._summary("b-good.zip", n=50)
        with zipfile.ZipFile(os.path.join(self.tmp, "b-good.zip")) as archive:
            size = archive.infolist()[0].file_size
        with mock.patch.object(team, "_MAX_TOTAL_BYTES", size + 10):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("a-bad.zip", problems[0])

    def test_one_rejected_zip_leaves_the_others_merged(self):
        self._summary("a.zip", n=2)
        columns = ["week_start", "project", "est_cost_usd"] + [f"e{i}" for i in range(300)]
        self._zip("z-wide.zip", "summary", columns, [["2026-02-02", "p", "1"] + [""] * 300],
                  stamp="20260211")
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("z-wide.zip", problems[0])

    def _raw_zip(self, name, member, text):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr(member, text)
        return path

    def _peak_mb(self):
        """Peak RSS in MB of reading the folder in a fresh interpreter, and what it said.

        On Linux ru_maxrss survives fork+exec, so the child would report the
        whole test run's peak; VmHWM belongs to the new address space and
        starts fresh at exec, so it is read instead where /proc has it."""
        code = (
            "import resource, sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "from claude_usage import team\n"
            f"exports, problems = team.read_exports([{self.tmp!r}])\n"
            "try:\n"
            "    with open('/proc/self/status') as fh:\n"
            "        peak = next(int(l.split()[1]) for l in fh if l.startswith('VmHWM:')) / 1e3\n"
            "except (OSError, StopIteration):\n"
            "    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
            "    peak = peak / 1e6 if sys.platform == 'darwin' else peak / 1e3\n"
            "print(peak)\n"
            "print(len(exports)); print(*problems, sep='\\n')\n")
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                             check=True).stdout.splitlines()
        return float(out[0]), int(out[1]), out[2:]

    _HEADER = "week_start,project,est_cost_usd"
    _HUGE_HEADER = _HEADER + "," * 10_000_000 + "\n"
    _HUGE_ROW = _HEADER + "\n2026-02-02,p,1" + "," * 10_000_000 + "\n"

    def _assert_over_long(self, text):
        self._raw_zip("h.zip", "claude-usage-summary-a-20260210.csv", text)
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(f"{team._MAX_RECORD_CHARS:,} characters", problems[0])

    def test_one_huge_header_line_is_rejected_with_the_limit_named(self):
        self._assert_over_long(self._HUGE_HEADER)

    def test_one_huge_data_line_is_rejected_with_the_limit_named(self):
        self._assert_over_long(self._HUGE_ROW)

    @unittest.skipIf(sys.platform == "win32", "peak RSS needs the resource module")
    def test_one_huge_header_line_is_rejected_before_it_is_parsed(self):
        self._raw_zip("h.zip", "claude-usage-summary-a-20260210.csv", self._HUGE_HEADER)
        peak, exports, problems = self._peak_mb()
        self.assertEqual((exports, len(problems)), (0, 1))
        self.assertLess(peak, 100)

    @unittest.skipIf(sys.platform == "win32", "peak RSS needs the resource module")
    def test_one_huge_data_line_is_rejected_before_it_is_parsed(self):
        self._raw_zip("d.zip", "claude-usage-summary-a-20260210.csv", self._HUGE_ROW)
        peak, exports, problems = self._peak_mb()
        self.assertEqual((exports, len(problems)), (0, 1))
        self.assertLess(peak, 100)

    def test_a_record_spread_over_many_short_lines_is_held_to_the_same_limit(self):
        # Quotes opened and closed on every line keep one record going without
        # any one line, or field, being long.
        text = "session_id,est_cost_usd\ns1,\"\n" + '",,,,,,,,,,,,,,,,,,,,,,,,,,"\n' * 100
        self._raw_zip("q.zip", "claude-usage-sessions-a-20260210.csv", text)
        with mock.patch.object(team, "_MAX_RECORD_CHARS", 500):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(exports, [])
        self.assertTrue(problems and "500 characters" in problems[0], problems)

    def test_a_quoted_cell_over_several_lines_still_reads(self):
        text = 'session_id,est_cost_usd,project\ns1,0.1,"one\ntwo\r\nthree"\ns2,0.2,x\n'
        self._raw_zip("m.zip", "claude-usage-sessions-a-20260210.csv", text)
        with mock.patch.object(team, "_MAX_RECORD_CHARS", 60):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(problems, [])
        rows = exports[0].files["sessions"]
        self.assertEqual([r["project"] for r in rows], ["one\ntwo\r\nthree", "x"])

    def test_a_byte_order_mark_is_dropped_from_a_zip_member(self):
        path = os.path.join(self.tmp, "bom.zip")
        with zipfile.ZipFile(path, "w") as out:
            out.writestr("claude-usage-sessions-a-20260210.csv",
                         "session_id,est_cost_usd\ns1,0.1\n".encode("utf-8-sig"))
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(problems, [])
        self.assertEqual(exports[0].columns["sessions"], ["session_id", "est_cost_usd"])
        self.assertEqual(exports[0].files["sessions"][0]["session_id"], "s1")

    def test_a_byte_order_mark_is_dropped_from_a_loose_csv(self):
        path = os.path.join(self.tmp, "claude-usage-sessions-a-20260210.csv")
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("session_id,est_cost_usd\ns1,0.1\n")
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(problems, [])
        self.assertEqual(exports[0].columns["sessions"], ["session_id", "est_cost_usd"])
        self.assertEqual(exports[0].files["sessions"][0]["session_id"], "s1")

    def test_loose_csvs_are_charged_to_the_byte_budget(self):
        for who in ("a", "b"):
            with open(os.path.join(self.tmp, f"claude-usage-sessions-{who}-20260210.csv"), "w",
                      encoding="utf-8") as fh:
                fh.write("session_id,est_cost_usd\n" + "".join(f"s{i},0.1\n" for i in range(50)))
        size = os.path.getsize(os.path.join(self.tmp, "claude-usage-sessions-a-20260210.csv"))
        with mock.patch.object(team, "_MAX_TOTAL_BYTES", size + 10):
            exports, problems = team.read_exports([self.tmp])
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("-b-", problems[0])
        self.assertIn("MiB", problems[0])

    def test_a_real_size_export_still_merges(self):
        """97 columns by 150,000 rows is the biggest a real export gets."""
        self.assertEqual(len(export_mod.SUMMARY_COLUMNS), 97)
        columns = list(export_mod.SUMMARY_COLUMNS)
        blank = [""] * len(columns)
        at = {c: i for i, c in enumerate(columns)}

        def rows():
            for i in range(150_000):
                row = list(blank)
                row[at["week_start"]] = "2026-02-02"
                row[at["project"]] = f"p{i}"
                row[at["est_cost_usd"]] = "0.5"
                yield row
        self._zip("big.zip", "summary", columns, rows())
        exports, problems = team.read_exports([self.tmp])
        self.assertEqual(problems, [])
        self.assertEqual(len(exports[0].files["summary"]), 150_000)
        self.assertEqual(exports[0].files["summary"][7]["project"], "p7")
        merged = team.merge(exports, problems)
        self.assertEqual(len(merged["files"]["summary"][1]), 150_000)


class TestTeamPageWeeks(unittest.TestCase):
    def test_a_bad_week_start_cannot_hang_the_page(self):
        from tests.jsrun import run_js
        weeks = run_js(["static/team.js"],
                       "PAYLOAD = {weekly: [['a', '2026-09-21'], ['a', '9/30/2026'], ['a', 'garbage'],"
                       " ['a', '2026-10-05']]}; out(allWeeks())")
        self.assertEqual(weeks, ["2026-09-21", "2026-09-28", "2026-10-05"])


class TestMergedCsvFormulaInjection(unittest.TestCase):
    def test_merged_files_neutralise_formula_cells(self):
        folder = TeamFolder()
        try:
            payload = _scan()
            # A loose CSV written by hand (or an older tool): raw formula text.
            cols = export_mod.SUMMARY_COLUMNS[:35]
            rows = export_mod.summary_rows(payload, ("evil@dev.io", "=cmd|' /C calc'!A0"))
            path = os.path.join(folder.dir, "claude-usage-summary-evil-20260101.csv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for r in rows:
                    if r[3]:
                        r = list(r)
                        r[3] = "@SUM(1+1)"
                        w.writerow(r[:35])
            files = team.merged_files(team._load([folder.dir]))
        finally:
            folder.close()
        summary = list(csv.DictReader(io.StringIO(files["team-summary.csv"])))
        self.assertTrue(summary)
        for r in summary:
            self.assertEqual(r["person_name"], "'=cmd|' /C calc'!A0")
            self.assertEqual(r["project"], "'@SUM(1+1)")
        people = list(csv.DictReader(io.StringIO(files["team-people.csv"])))
        self.assertEqual(people[0]["person_name"], "'=cmd|' /C calc'!A0")


class TestForecastCoverage(unittest.TestCase):
    """The run-rate is computed from weeks the exports actually cover."""

    MERGED = {
        "files": {"limits": ([], [{"limit_type": "weekly", "hit_at": f"2026-09-{d:02d}T10:00:00Z",
                                   "usage_after_hit_usd": "100"} for d in (1, 8, 15, 22, 29)])},
        "people": [{"person": "a", "window_from": "2026-07-01", "window_to": "2026-10-01"}],
    }

    def test_weeks_after_the_last_export_are_not_zero_weeks(self):
        for today in (dt.date(2026, 10, 2), dt.date(2026, 11, 3)):
            f = team.overage_forecast(self.MERGED, today=today)
            self.assertEqual(f["weekly_run_rate_usd"], 100.0, today)
            self.assertEqual(f["basis_weeks"], 4, today)
            self.assertEqual(f["weekly"][-1][0], "2026-09-28", "no weeks past the data")

    def test_the_exports_unfinished_week_is_not_a_complete_week(self):
        # The export ends Thursday Oct 1, so the week of Sep 28 is part-seen:
        # its $100 is shown but not averaged as a whole week.
        f = team.overage_forecast(self.MERGED, today=dt.date(2026, 11, 3))
        self.assertEqual(dict(f["weekly"])["2026-09-28"], 100.0)
        full = {**self.MERGED, "people": [{"person": "a", "window_from": "2026-07-01",
                                           "window_to": "2026-10-04"}]}
        f = team.overage_forecast(full, today=dt.date(2026, 11, 3))
        self.assertEqual(f["basis_weeks"], 4)
        self.assertEqual(f["weekly"][-1][0], "2026-09-28")


    @staticmethod
    def _merged(hits, to="2026-10-01"):
        return {"files": {"limits": ([], [{"limit_type": t, "hit_at": f"{d}T10:00:00Z",
                                           "usage_after_hit_usd": usd} for d, usd, t in hits])},
                "people": [{"person": "a", "window_from": "2026-07-01", "window_to": to}],
                "sources": [], "warnings": [], "prices": None}

    def test_basis_episodes_counts_only_episodes_in_the_basis_weeks(self):
        # Basis weeks are Aug 31 - Sep 21; the July hits and the Oct 1 hit
        # (week of Sep 28, part-seen) are outside them, a $0 hit and a spend
        # cap are not episodes.
        merged = self._merged([("2026-07-07", "50", "weekly"), ("2026-07-14", "50", "weekly"),
                               ("2026-09-15", "80", "weekly"), ("2026-09-16", "0", "weekly"),
                               ("2026-09-17", "30", "spend_cap"), ("2026-10-01", "900", "weekly")])
        f = team.overage_forecast(merged, today=dt.date(2026, 11, 3))
        self.assertEqual(f["basis_weeks"], 4)
        self.assertEqual(f["basis_episodes"], 1)

    def test_every_basis_week_episode_counts(self):
        merged = self._merged([(f"2026-09-{d:02d}", "100", "weekly") for d in (1, 8, 15, 22, 29)])
        f = team.overage_forecast(merged, today=dt.date(2026, 11, 3))
        self.assertEqual(f["basis_episodes"], 4)             # Sep 29 is the part-seen week

    def test_the_report_does_not_project_one_episode(self):
        merged = self._merged([("2026-07-07", "50", "weekly"), ("2026-07-14", "50", "weekly"),
                               ("2026-09-15", "80", "weekly")], to="2026-10-04")
        text = team.report_text(merged)
        self.assertIn("One episode so far: too little to project a run-rate.", text)
        self.assertNotIn("quarter", text)
        self.assertNotIn("per 4 weeks", text)

    def test_the_report_with_no_extra_usage_says_so(self):
        text = team.report_text(self._merged([("2026-07-07", "50", "weekly")], to="2026-10-04"))
        self.assertIn("No extra usage so far", text)
        self.assertNotIn("quarter", text)

    def test_the_report_projects_from_several_episodes(self):
        merged = self._merged([(f"2026-09-{d:02d}", "100", "weekly") for d in (1, 8, 15, 22)], to="2026-10-04")
        text = team.report_text(merged)
        self.assertIn("3 episodes", text)           # Sep 1 is the week before the basis
        self.assertIn("per quarter", text)


def _empty_payload():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "projects"))
        return _scan(os.path.join(tmp, "projects"))


class TestSeatActivity(unittest.TestCase):
    """Seat use and idleness, as the team view reports them."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()

    def setUp(self):
        self.folder = TeamFolder()

    def tearDown(self):
        self.folder.close()

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    def _alone(self, **kw):
        single = TeamFolder()
        try:
            single.zip(self.payload, "a@example.com", "a@dev.io", "20260210", **kw)
            return team._load([single.dir])["people"][0]
        finally:
            single.close()

    def test_a_seat_with_no_usage_is_named_and_idle(self):
        self.folder.zip(_empty_payload(), "idle@example.com", "idle@dev.io", "20260210")
        p = self._merge()["people"]
        self.assertEqual([x["person"] for x in p], ["idle@example.com"])
        self.assertEqual(p[0]["seat_idle"], "yes")
        self.assertEqual(p[0]["person_active_days"], 0)

    def test_split_exports_from_one_machine_add_their_active_days(self):
        full = self._alone()
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", until="2026-01-20",
                        name="claude-usage-export-a-jan-20260301.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260302", since="2026-01-21",
                        name="claude-usage-export-a-feb-20260302.zip")
        p = self._merge()["people"][0]
        self.assertGreater(full["person_active_days"], 0)
        self.assertEqual(p["person_active_days"], full["person_active_days"])
        self.assertEqual(p["window_workdays"], full["window_workdays"])
        self.assertEqual(p["seat_use"], full["seat_use"])

    def test_a_later_shorter_re_export_does_not_shrink_seat_use(self):
        full = self._alone()
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210",
                        name="claude-usage-export-a-full-20260210.zip")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260301", since="2026-02-01",
                        until="2026-02-07", name="claude-usage-export-a-week-20260301.zip")
        p = self._merge()["people"][0]
        self.assertEqual(p["person_active_days"], full["person_active_days"])
        self.assertEqual(p["window_workdays"], full["window_workdays"])


class TestCopiedMachineId(unittest.TestCase):
    """Two machines that share a machine id because the tool's .cache folder
    was copied: their exports cover the same days but none of each other's
    sessions, so they are two machines, not a re-export."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.total = sum(r[13] for r in cls.payload["usage"])

    def setUp(self):
        self.folder = TeamFolder()

    def tearDown(self):
        self.folder.close()

    def test_both_machines_count_and_it_is_reported(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="laptop")
        self.folder.zip(_on_another_machine(self.payload), "a@example.com", "a@dev.io", "20260210",
                        machine="laptop", name="claude-usage-export-a-desktop-20260210.zip")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(len({e.machine_id for e in exports}), 1)
        m = team.merge(exports, problems)
        for kind in ("summary", "breakdown", "sessions"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), 2 * self.total, places=5, msg=kind)
        self.assertEqual(m["people"][0]["machines"], 2)
        self.assertTrue(any("same machine id" in w["message"] for w in m["warnings"]), m["warnings"])

    def test_a_re_export_of_the_same_machine_is_still_one_machine(self):
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="laptop")
        self.folder.zip(self.payload, "a@example.com", "a@dev.io", "20260212", machine="laptop",
                        name="claude-usage-export-a-again-20260212.zip")
        m = team._load([self.folder.dir])
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), self.total, places=5)
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertFalse(any("same machine id" in w["message"] for w in m["warnings"]))


class TestResumeBetweenExportsIsOneMachine(unittest.TestCase):
    """One laptop exports, resumes a session (the continuation copies the
    original, which the sessions sheet then leaves out), and exports again. The
    exports share no session id, and each lists a session on days both cover
    that the other lacks -- but the original is explained by the continuation,
    so it is one machine, not two to add up."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = TeamFolder(tz=dt.timezone.utc)
        self.addCleanup(self.folder.close)

    def _logs(self, name, files):
        root = os.path.join(self.tmp, name)
        for fn, records in files.items():
            _write(os.path.join(root, "-Users-x-p", fn + ".jsonl"), records)
        # In UTC, like the export, so `until` names the same days on every machine.
        return scan(root, cache=FileCache(os.path.join(self.tmp, name + ".json")), tz=dt.timezone.utc)

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    def _export(self, payload, stamp, until, machine, name):
        self.folder.zip(payload, "a@example.com", "a@dev.io", stamp, until=until, machine=machine, name=name)

    @staticmethod
    def _warns(m):
        return [w["message"] for w in m["warnings"] if w["level"] == "warn"]

    @staticmethod
    def _x():
        return [_response(f"x{i}", _ts(i, day=2), inp=1_000_000) for i in range(5)]

    def test_a_resume_between_exports_with_no_other_shared_session_is_one_machine(self):
        x = self._x()
        first = self._logs("one", {"xxxx": x})
        resumed = self._logs("two", {"yyyy": x + [_response(f"y{i}", _ts(i, day=4)) for i in range(3)]})
        # The resume left xxxx a derived copy, out of the second export's sessions.
        self.assertEqual([s["id"] for s in resumed["sessions"] if not s["derived"]], ["yyyy"])
        self._export(first, "20260203", "2026-02-03", "laptop", "e1.zip")
        self._export(resumed, "20260205", "2026-02-05", "laptop", "e2.zip")
        m = self._merge()
        truth = sum(r[13] for r in resumed["usage"])
        self.assertEqual(m["people"][0]["machines"], 1)
        for kind in ("summary", "breakdown"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), truth, places=5, msg=kind)
        self.assertEqual(self._warns(m), [])

    def test_a_byte_copy_from_a_later_export_is_one_machine_too(self):
        # The continuation is another id with the same records and no more (an
        # exact copy that owns the requests), so the original is left out of the
        # second export's sessions sheet.
        x = self._x()
        first = self._logs("one", {"xxxx": x})
        copied = self._logs("two", {"xxxx": x, "aaaa": x})
        self._export(first, "20260203", "2026-02-03", "laptop", "e1.zip")
        self._export(copied, "20260205", "2026-02-05", "laptop", "e2.zip")
        m = self._merge()
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), sum(r[13] for r in copied["usage"]), places=5)
        self.assertEqual(self._warns(m), [])

    def test_two_machines_on_a_copied_id_with_their_own_sessions_are_still_two(self):
        # Same machine id (a copied .cache), sessions of their own on the same
        # days and no continuation relation between them.
        one = self._logs("one", {"aaaa": [_response("a0", _ts(5, day=2), inp=1_000_000)]})
        two = self._logs("two", {"bbbb": [_response("b0", _ts(9, day=2), inp=1_000_000)]})
        self._export(one, "20260203", "2026-02-03", "laptop", "e1.zip")
        self._export(two, "20260205", "2026-02-05", "laptop", "e2.zip")
        m = self._merge()
        self.assertEqual(m["people"][0]["machines"], 2)
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]),
                               sum(r[13] for r in one["usage"]) + sum(r[13] for r in two["usage"]), places=5)

    def test_sessions_that_merely_start_together_are_not_each_others_continuation(self):
        # Same start moment and project but neither runs on past the other, and
        # the exports end together (so neither is a later copy of the other).
        one = self._logs("one", {"aaaa": [_response("a0", _ts(5, day=2), inp=1_000_000)]})
        two = self._logs("two", {"bbbb": [_response("b0", _ts(5, day=2), inp=1_000_000)]})
        self._export(one, "20260205", "2026-02-05", "laptop", "e1.zip")
        self._export(two, "20260205", "2026-02-05", "laptop", "e2.zip")
        self.assertEqual(self._merge()["people"][0]["machines"], 2)

    def test_a_resume_does_not_explain_an_unrelated_session(self):
        # The first export also holds a session on a shared day that nothing in
        # the second continues (and the second's resumed session is nothing of
        # the first's): each has work the other lacks, so two machines.
        x = self._x()
        first = self._logs("one", {"xxxx": x, "oooo": [_response("o0", _ts(1, day=3), inp=1_000_000)]})
        resumed = self._logs("two", {"yyyy": x + [_response(f"y{i}", _ts(i, day=4)) for i in range(3)]})
        self._export(first, "20260203", "2026-02-03", "laptop", "e1.zip")
        self._export(resumed, "20260205", "2026-02-05", "laptop", "e2.zip")
        self.assertEqual(self._merge()["people"][0]["machines"], 2)


@contextlib.contextmanager
def _zone(hours, offset_cell=None, summer=None):
    """The exporting machine in a fixed UTC offset: every local-time step of the
    scan and the export (week and day buckets, session times, the `timezone`
    column) uses it, so no `tzset` is needed. `offset_cell` replaces the
    `timezone` column's value ("" for an export that predates it). `summer`,
    an offset in hours, applies from 2026-03-08 10:00Z on: one zone across a
    daylight-saving change (the `timezone` column is then the offset in force
    when the export was made, so pass it as `offset_cell`)."""
    tz = dt.timezone(dt.timedelta(hours=hours))
    summer_tz = dt.timezone(dt.timedelta(hours=summer)) if summer is not None else tz
    switch = dt.datetime(2026, 3, 8, 10, tzinfo=dt.timezone.utc)

    def local(ts):
        moment = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return moment.astimezone(summer_tz if moment >= switch else tz)

    def date(ts, tz=None):
        return local(ts).strftime("%Y-%m-%d") if ts else None

    def bucket(ts, minutes=5, tz=None):
        if not ts:
            return None
        moment = local(ts)
        slot = ((moment.hour * 60 + moment.minute) // minutes) * minutes
        return "{}T{:02d}:{:02d}".format(moment.strftime("%Y-%m-%d"), slot // 60, slot % 60)

    def local_time(ts, tz=None):
        return local(ts).isoformat(timespec="seconds") if ts else ts

    sign = "-" if hours < 0 else "+"
    cell = offset_cell if offset_cell is not None else f"{sign}{abs(hours):02d}:00"
    with mock.patch.object(aggregate, "_local_date", date), \
            mock.patch.object(aggregate, "_local_bucket", bucket), \
            mock.patch.object(context, "_local_day", lambda ts, tz=None: date(ts) if ts else None), \
            mock.patch.object(export_mod, "_local_time", local_time), \
            mock.patch.object(context, "utc_offset", lambda moment=None: cell):
        yield


class TestMixedTimeZoneReExport(unittest.TestCase):
    """A person exports, changes time zone, and exports the same machine again.
    Each export puts a week's work in the week of its own local days, so work
    near a Sunday/Monday midnight is in week W of one and W+1 of the other. The
    weekly rows are summed, so the merge can't re-bucket them: one export
    supplies the whole stretch both cover, and no week is taken from each.

    The session below is Monday 06:30Z: Sunday 22:30 at UTC-8, Monday 01:30 at
    UTC-5."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = TeamFolder()
        self.addCleanup(self.folder.close)

    def _export(self, hours, name, files, stamp, until, since=None, cell=None, machine="laptop",
                summer=None, truncated=None, bucket_zone=""):
        root = os.path.join(self.tmp, name)
        for fn, records in files.items():
            _write(os.path.join(root, "-Users-x-p", fn + ".jsonl"), records)
        history = mock.patch.object(context, "history_coverage", return_value={
            "cli_sessions": 1, "cli_history_from": truncated, "cli_history_truncated": True,
            "retention_days_default": 30}) if truncated else contextlib.nullcontext()
        with _zone(hours, cell, summer), history:
            payload = scan(root, cache=FileCache(os.path.join(self.tmp, name + ".json")))
            self.folder.zip(payload, "a@example.com", "a@dev.io", stamp, since=since, until=until,
                            machine=machine, name=name + ".zip", bucket_zone=bucket_zone)
        return payload

    def _everything(self, files):
        """The true total of the logs, whatever zone they are read in."""
        self._truths = getattr(self, "_truths", 0) + 1
        return self._truth(self._scan_only(f"truth{self._truths}", files))

    def _scan_only(self, name, files):
        root = os.path.join(self.tmp, name)
        for fn, records in files.items():
            _write(os.path.join(root, "-Users-x-p", fn + ".jsonl"), records)
        return scan(root, cache=FileCache(os.path.join(self.tmp, name + ".json")))

    def _assert_total(self, m, truth):
        for kind in ("summary", "breakdown"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), truth, places=5, msg=kind)

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    @staticmethod
    def _notes(m):
        return [w["message"] for w in m["warnings"] if w["level"] == "info" and "time zone" in w["message"]]

    @staticmethod
    def _warns(m):
        return [w["message"] for w in m["warnings"] if w["level"] == "warn"]

    @staticmethod
    def _x():
        return [_response(f"x{i}", f"2026-02-02T06:{30 + i:02d}:00.000Z", inp=1_000_000) for i in range(5)]

    @staticmethod
    def _truth(payload):
        return sum(r[13] for r in payload["usage"])

    def _assert_one_count(self, m, payload):
        for kind in ("summary", "breakdown"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), self._truth(payload), places=5, msg=kind)
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertEqual(self._warns(m), [])

    def _assert_noted(self, m):
        (note,) = self._notes(m)
        self.assertIn("a@example.com", note)
        self.assertIn("-08:00", note)
        self.assertIn("-05:00", note)
        self.assertIn("2026-02-02", note)
        self.assertIn("e2", note)

    def test_a_resume_between_exports_from_two_zones_counts_once(self):
        x = self._x()
        self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04")
        second = self._export(-5, "e2", {"yyyy": x + [_response(f"y{i}", f"2026-02-05T10:0{i}:00.000Z")
                                                    for i in range(3)]}, "20260206", "2026-02-06")
        m = self._merge()
        self._assert_one_count(m, second)
        self._assert_noted(m)

    def test_a_plain_re_export_from_two_zones_counts_once(self):
        x = self._x()
        self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04")
        second = self._export(-5, "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06")
        m = self._merge()
        self._assert_one_count(m, second)
        self._assert_noted(m)

    def test_a_re_export_with_another_session_shared_counts_once(self):
        x = self._x()
        other = [_response("o1", "2026-02-03T18:00:00.000Z", inp=1000)]
        self._export(-8, "e1", {"xxxx": x, "oooo": other}, "20260204", "2026-02-04")
        second = self._export(-5, "e2", {"xxxx": x, "oooo": other,
                                         "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06")
        m = self._merge()
        self._assert_one_count(m, second)
        self._assert_noted(m)

    def test_the_newer_export_wins_whichever_zone_it_is_in(self):
        x = self._x()
        self._export(-5, "e1", {"xxxx": x}, "20260204", "2026-02-04")
        second = self._export(-8, "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06")
        m = self._merge()
        self._assert_one_count(m, second)
        (note,) = self._notes(m)
        self.assertIn("-08:00", note)
        self.assertIn("-05:00", note)

    def test_the_same_zone_twice_is_unchanged(self):
        x = self._x()
        self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04")
        second = self._export(-8, "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06")
        m = self._merge()
        self._assert_one_count(m, second)
        self.assertEqual(self._notes(m), [])

    # ---- bucket_zone: one named zone is one zone ---------------------------- #

    LA = "America/Los_Angeles"

    @staticmethod
    def _midweek():
        return [_response(f"x{i}", f"2026-02-03T18:0{i}:00.000Z", inp=1_000_000) for i in range(5)]

    def _re_export_pair(self, zone1, zone2, hours=(-8, -7)):
        """The same machine exported twice, the second with one more session.
        Mid-week work, so both offsets put every day in the same week and the
        totals are exact either way: only whether the merge sees a zone
        disagreement differs."""
        x = self._midweek()
        self._export(hours[0], "e1", {"xxxx": x}, "20260204", "2026-02-04", bucket_zone=zone1)
        return self._export(hours[1], "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                            "20260206", "2026-02-06", bucket_zone=zone2)

    def test_the_same_bucket_zone_is_never_a_disagreement_whatever_the_offsets_say(self):
        # A named zone's offset moves with daylight saving (or a rule change
        # between tz data versions), so the offsets two files show prove nothing
        # when both name the same zone.
        second = self._re_export_pair(self.LA, self.LA)
        m = self._merge()
        self._assert_one_count(m, second)
        self.assertEqual(self._notes(m), [])

    def test_without_a_bucket_zone_the_offsets_still_decide(self):
        # The same two files from before schema 8 (no bucket_zone): unchanged.
        second = self._re_export_pair("", "")
        m = self._merge()
        self._assert_one_count(m, second)
        self.assertEqual(len(self._notes(m)), 1)

    def test_a_legacy_export_against_a_new_one_keeps_todays_handling(self):
        for older, newer in (("", self.LA), (self.LA, "")):
            with self.subTest(older=older, newer=newer):
                self.setUp()
                second = self._re_export_pair(older, newer)
                m = self._merge()
                self._assert_one_count(m, second)
                self.assertEqual(len(self._notes(m)), 1)

    def test_two_different_bucket_zones_for_one_machine_are_compared_by_offset(self):
        second = self._re_export_pair(self.LA, "America/New_York")
        m = self._merge()
        for kind in ("summary", "breakdown"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), self._truth(second), places=5, msg=kind)
        self.assertEqual(m["people"][0]["machines"], 1)
        self.assertEqual(len(self._notes(m)), 1)
        # Two zones in one merge is also the team-wide advice, once.
        (warn,) = self._warns(m)
        self.assertIn("a@example.com exported in both America/Los_Angeles and America/New_York", warn)
        self.assertIn("--tz", warn)

    def test_timezone_cells_are_not_consulted_for_exports_naming_one_zone(self):
        # No session in common, so the cells would decide for legacy exports.
        one = {"aaaa": [_response("a1", "2026-02-03T10:00:00.000Z", inp=1_000_000)]}
        two = {"bbbb": [_response("b1", "2026-02-04T10:00:00.000Z", inp=1_000_000)]}
        for zone, disagree in ((self.LA, False), ("", True)):
            with self.subTest(bucket_zone=zone):
                self.setUp()
                self._export(-8, "e1", one, "20260205", "2026-02-05", since="2026-02-02", bucket_zone=zone)
                self._export(-7, "e2", two, "20260206", "2026-02-06", since="2026-02-02", bucket_zone=zone)
                older, newer = sorted(team.read_exports([self.folder.dir])[0], key=lambda e: e.stamp)
                self.assertEqual(team._zone_overlap(older, newer) is not None, disagree)

    def test_the_bucket_zone_is_read_blank_or_absent_as_legacy(self):
        x = self._midweek()
        self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04", bucket_zone=self.LA)
        self._export(-8, "e2", {"xxxx": x}, "20260206", "2026-02-06", bucket_zone="")
        self._export(-8, "e3", {"xxxx": x}, "20260208", "2026-02-08", bucket_zone="UTC")
        exports = sorted(team.read_exports([self.folder.dir])[0], key=lambda e: e.stamp)
        self.assertEqual([e.bucket_zone for e in exports], [self.LA, None, "UTC"])
        _as_schema7(os.path.join(self.folder.dir, "e3.zip"))
        exports = sorted(team.read_exports([self.folder.dir])[0], key=lambda e: e.stamp)
        self.assertEqual([e.bucket_zone for e in exports], [self.LA, None, None])

    def test_exports_with_windows_that_do_not_meet_are_unchanged(self):
        files = {"aaaa": [_response("a0", "2026-01-28T10:00:00.000Z", inp=1_000_000)],
                 "bbbb": [_response("b0", "2026-02-04T10:00:00.000Z", inp=2_000_000)]}
        self._export(-8, "e1", files, "20260202", "2026-02-01", since="2026-01-26")
        everything = self._export(-5, "e2", files, "20260209", "2026-02-08", since="2026-02-02")
        m = self._merge()
        rows = {r["week_start"]: float(r["est_cost_usd"]) for r in m["files"]["summary"][1]}
        self.assertEqual(sorted(rows), ["2026-01-26", "2026-02-02"])
        self.assertAlmostEqual(sum(rows.values()), self._truth(everything), places=5)
        self.assertEqual(self._notes(m), [])
        self.assertEqual(self._warns(m), [])

    def test_an_older_export_with_no_timezone_is_not_a_disagreement(self):
        # Unknown against a known zone: nothing says the weeks were bucketed
        # differently, so the merge picks week by week as it always did, and the
        # boundary work stays in both.
        x = self._x()
        first = self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04", cell="")
        second = self._export(-5, "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06")
        m = self._merge()
        self.assertEqual(self._notes(m), [])
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]),
                               self._truth(second) + self._truth(first), places=5)

    def test_a_week_with_days_before_the_other_export_began_is_kept(self):
        # The older export also holds work from days before the newer one's
        # window starts; dropping its week for the newer export would lose them.
        files = {"xxxx": self._x(), "jjjj": [_response("j0", "2026-01-28T10:00:00.000Z", inp=3_000_000)]}
        self._export(-8, "e1", files, "20260204", "2026-02-04", since="2026-01-26")
        self._export(-5, "e2", files, "20260206", "2026-02-06", since="2026-02-02")
        m = self._merge()
        rows = {r["week_start"]: float(r["est_cost_usd"]) for r in m["files"]["summary"][1]}
        self.assertGreater(rows["2026-01-26"], 3.0)
        self.assertEqual(self._warns(m), [])


    # ---- a week is left out only when nothing is lost ---------------------- #

    def test_work_inside_the_older_window_but_outside_the_newer_is_kept(self):
        # Identical Monday-to-Sunday windows. Sunday 22:00 at UTC-8 is Monday
        # 01:00 at UTC-5, past the newer export's window: only the older holds it.
        x = [_response(f"x{i}", f"2026-02-03T18:0{i}:00.000Z", inp=1_000_000) for i in range(3)]
        late = [_response("l1", "2026-02-09T06:00:00.000Z", inp=2_000_000)]
        files = {"xxxx": x, "llll": late}
        self._export(-8, "e1", files, "20260209", "2026-02-08", since="2026-02-02")
        self._export(-5, "e2", files, "20260210", "2026-02-08", since="2026-02-02")
        self._assert_total(self._merge(), self._everything(files))

    def test_work_inside_the_newer_window_but_outside_the_older_is_kept(self):
        early = [_response("e1", "2026-02-02T06:00:00.000Z", inp=2_000_000)]  # Sun 22:00 at -8
        x = [_response(f"x{i}", f"2026-02-03T18:0{i}:00.000Z", inp=1_000_000) for i in range(3)]
        files = {"xxxx": x, "eeee": early}
        self._export(-5, "e1", files, "20260209", "2026-02-08", since="2026-02-02")
        self._export(-8, "e2", files, "20260210", "2026-02-08", since="2026-02-02")
        self._assert_total(self._merge(), self._everything(files))

    def test_a_newer_window_inside_the_older_one_loses_nothing(self):
        files = {"aaaa": [_response("a1", "2026-01-08T18:00:00.000Z", inp=1_000_000)],
                 "mmmm": [_response("m1", "2026-01-22T18:00:00.000Z", inp=1_000_000)],
                 # Sunday 22:30 at UTC-8: in the older window, past the newer's end
                 "eeee": [_response("e1", "2026-02-02T06:30:00.000Z", inp=1_000_000)],
                 "tttt": [_response("t1", "2026-02-05T18:00:00.000Z", inp=1_000_000)]}
        self._export(-8, "e1", files, "20260210", "2026-02-10", since="2026-01-05")
        self._export(-5, "e2", files, "20260211", "2026-02-01", since="2026-01-19")
        m = self._merge()
        self._assert_total(m, self._everything(files))

    def _cleaned_up(self, newer, summer=None, cell=None, flagged=True):
        # The older export holds session A (January 13); the newer one lost it
        # to Claude Code's 30-day cleanup (flagged, or not).
        a = [_response(f"A{i}", f"2026-01-13T18:0{i}:00.000Z", inp=2_000_000) for i in range(4)]
        x = [_response(f"x{i}", f"2026-02-03T18:0{i}:00.000Z", inp=1_000_000) for i in range(2)]
        late = [_response("late", "2026-03-04T18:00:00.000Z", inp=1_000_000)]
        self._export(-8, "e1", {"AAAA": a, "xxxx": x}, "20260210", "2026-02-10", since="2026-01-05")
        self._export(newer, "e2", {"xxxx": x, "late": late}, "20260320", "2026-03-20", since="2026-01-05",
                     summer=summer, cell=cell, truncated="2026-02-18" if flagged else None)
        return self._everything({"AAAA": a, "xxxx": x, "late": late})

    def test_a_cleaned_up_re_export_from_another_zone_does_not_replace_the_weeks(self):
        for flagged in (True, False):
            with self.subTest(flagged=flagged):
                self.setUp()
                truth = self._cleaned_up(-5, flagged=flagged)
                self._assert_total(self._merge(), truth)

    def test_a_cleaned_up_re_export_in_the_same_zone_across_daylight_saving_is_unchanged(self):
        # PST then PDT: the `timezone` cells read -08:00 and -07:00, but the
        # shared session is written in -08:00 in both, so it is one zone.
        for flagged in (True, False):
            with self.subTest(flagged=flagged):
                self.setUp()
                truth = self._cleaned_up(-8, summer=-7, cell="-07:00", flagged=flagged)
                m = self._merge()
                self._assert_total(m, truth)
                self.assertEqual(self._notes(m), [])

    def test_one_zone_across_daylight_saving_is_not_two_zones(self):
        x = self._x()
        self._export(-8, "e1", {"xxxx": x}, "20260204", "2026-02-04")
        second = self._export(-8, "e2", {"xxxx": x, "zzzz": [_response("z1", "2026-02-05T10:00:00.000Z")]},
                              "20260206", "2026-02-06", summer=-7, cell="-07:00")
        m = self._merge()
        self._assert_one_count(m, second)
        self.assertEqual(self._notes(m), [])

    def test_exports_that_share_no_session_are_compared_by_their_timezone_column(self):
        # No session in common, so the cells decide; each export's own work is
        # not in the other, and all of it is kept.
        one = {"aaaa": [_response("a1", "2026-02-03T10:00:00.000Z", inp=1_000_000)]}
        two = {"bbbb": [_response("b1", "2026-02-04T10:00:00.000Z", inp=1_000_000)]}
        self._export(-8, "e1", one, "20260205", "2026-02-05", since="2026-02-02")
        self._export(-5, "e2", two, "20260206", "2026-02-06", since="2026-02-02")
        m = self._merge()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]),
                               self._everything(one) + self._everything(two), places=5)

    def test_an_export_ruled_out_of_a_week_is_still_checked_for_dropped_work(self):
        # `left_out` exports are never chosen but their sessions the chosen
        # export lacks still show up as dropped work (so the merge can warn).
        x = self._x()
        self._export(-8, "e1", {"xxxx": x, "oooo": [_response("o1", "2026-02-03T18:00:00.000Z", inp=1000)]},
                     "20260204", "2026-02-04")
        self._export(-8, "e2", {"xxxx": x}, "20260206", "2026-02-06")
        exports, _ = team.read_exports([self.folder.dir])
        older, newer = sorted(exports, key=lambda e: e.stamp)
        rows = lambda e: [r for r in e.files["summary"] if r["week_start"] == "2026-02-02"]
        chosen, _, dropped = team._choose_for_week("2026-02-02", {newer: rows(newer)}, {older: rows(older)})
        self.assertEqual(chosen, [newer])
        self.assertEqual(list(dropped["sessions"]), ["oooo"])

    def test_a_ruled_out_export_whose_session_the_chosen_window_cannot_list_is_dropped_work(self):
        # The older export (made in another zone, so ruled out) also runs past the
        # chosen one's last day, so no cost gap is compared. Its session `oooo` started
        # Sunday 02-08 local, before the chosen export's window, which therefore can't
        # list it; that is still not a reason to excuse it.
        x = [_response("x1", "2026-02-10T18:00:00.000Z", inp=1_000_000)]
        o = [_response("o1", "2026-02-09T02:00:00.000Z", inp=1_000_000),
             _response("o2", "2026-02-09T10:00:00.000Z", inp=3_000_000)]
        self._export(-8, "e1", {"xxxx": x, "oooo": o}, "20260213", "2026-02-14", since="2026-02-02")
        self._export(-5, "e2", {"xxxx": x}, "20260214", "2026-02-12", since="2026-02-09")
        exports, _ = team.read_exports([self.folder.dir])
        older, newer = sorted(exports, key=lambda e: e.stamp)
        rows = lambda e: [r for r in e.files["summary"] if r["week_start"] == "2026-02-09"]
        chosen, _, dropped = team._choose_for_week("2026-02-09", {newer: rows(newer)}, {older: rows(older)})
        self.assertEqual(chosen, [newer])
        self.assertEqual(list(dropped["sessions"]), ["oooo"])


class TestPeoplesExportZones(unittest.TestCase):
    """People who export in different zones have weeks that don't line up, and
    weekly rows can't be re-bucketed: the merge says so once, naming the zones,
    and asks for one team-wide --tz."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()

    def setUp(self):
        self.folder = TeamFolder()
        self.addCleanup(self.folder.close)

    def _person(self, who, zone, stamp="20260210", machine="laptop", name=None):
        return self.folder.zip(self.payload, f"{who}@example.com", f"{who}@dev.io", stamp,
                               machine=machine, bucket_zone=zone, name=name)

    def _merge(self):
        exports, problems = team.read_exports([self.folder.dir])
        return team.merge(exports, problems)

    @staticmethod
    def _zone_warns(m):
        return [w for w in m["warnings"] if "--tz" in w["message"]]

    def test_different_zones_across_people_warn_once_naming_each_with_its_count(self):
        for who in ("a", "b"):
            self._person(who, "America/Los_Angeles")
        self._person("c", "UTC")
        self._person("d", "")  # a legacy export: not a zone, not named
        warns = self._zone_warns(self._merge())
        self.assertEqual(len(warns), 1, warns)
        (warn,) = warns
        self.assertEqual(warn["level"], "warn")
        self.assertIn("America/Los_Angeles (2 people)", warn["message"])
        self.assertIn("UTC (1 person)", warn["message"])
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", warn["message"])
        self.assertIn("weeks", warn["message"])
        self.assertNotIn("legacy", warn["message"].lower())

    def test_a_person_with_two_exports_in_one_zone_counts_once(self):
        self._person("a", "UTC", stamp="20260203", name="a1.zip")
        self._person("a", "UTC", stamp="20260210", name="a2.zip")
        self._person("b", "Asia/Tokyo")
        (warn,) = self._zone_warns(self._merge())
        self.assertIn("UTC (1 person)", warn["message"])
        self.assertIn("Asia/Tokyo (1 person)", warn["message"])

    def test_one_person_in_two_zones_is_named_not_counted(self):
        self._person("a", "UTC", stamp="20260203", name="a1.zip")
        self._person("a", "Asia/Tokyo", stamp="20260210", name="a2.zip")
        (warn,) = self._zone_warns(self._merge())
        self.assertIn("a@example.com exported in both Asia/Tokyo and UTC", warn["message"])
        self.assertNotIn("(1 person)", warn["message"])
        self.assertNotIn("everyone", warn["message"])
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", warn["message"])

    def test_the_same_zone_for_everyone_does_not_warn(self):
        for who in ("a", "b", "c"):
            self._person(who, "Europe/London")
        self.assertEqual(self._zone_warns(self._merge()), [])

    def test_one_zone_next_to_legacy_exports_does_not_warn(self):
        self._person("a", "UTC")
        self._person("b", "")
        self._person("c", "")
        self.assertEqual(self._zone_warns(self._merge()), [])

    def test_legacy_only_does_not_warn(self):
        self._person("a", "")
        self._person("b", "")
        self.assertEqual(self._zone_warns(self._merge()), [])

    # ---- schema 7 against schema 8 ----------------------------------------- #

    def test_a_schema_7_export_next_to_a_current_one_gets_the_re_export_advice(self):
        old = self._person("a", "UTC")
        _as_schema7(old)
        self._person("b", "UTC")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual(problems, [])
        current = context.EXPORT_SCHEMA_VERSION
        self.assertEqual(sorted(e.schema_version for e in exports), [7, current])
        advice = [w for w in team.merge(exports, problems)["warnings"]
                  if w["level"] == "warn" and "re-export" in w["message"]]
        self.assertEqual(len(advice), 1, advice)
        self.assertIn(f"7, {current}", advice[0]["message"])
        self.assertIn("version 7", advice[0]["message"])
        self.assertIn("bucketed", advice[0]["message"])
        self.assertIn("zone", advice[0]["message"])
        # It names who: the older file's person and file, not the current one's.
        self.assertIn("a@example.com (", advice[0]["message"])
        self.assertIn(os.path.basename(old), advice[0]["message"])
        self.assertNotIn("b@example.com", advice[0]["message"])

    def test_the_re_export_advice_lists_five_people_then_counts_the_rest(self):
        for who in "abcdefg":
            _as_schema7(self._person(who, "UTC", name=f"{who}.zip"))
        self._person("h", "UTC")
        exports, problems = team.read_exports([self.folder.dir])
        (advice,) = [w["message"] for w in team.merge(exports, problems)["warnings"]
                     if w["level"] == "warn" and "re-export" in w["message"]]
        self.assertIn("e@example.com (e.zip)", advice)
        self.assertNotIn("f@example.com", advice)
        self.assertIn("2 more people", advice)

    def test_the_re_export_advice_caps_one_persons_files(self):
        for n in range(5):
            _as_schema7(self._person("a", "UTC", stamp=f"2026020{n + 1}", name=f"a{n}.zip"))
        self._person("b", "UTC")
        exports, problems = team.read_exports([self.folder.dir])
        (advice,) = [w["message"] for w in team.merge(exports, problems)["warnings"]
                     if w["level"] == "warn" and "re-export" in w["message"]]
        self.assertIn("a@example.com (a0.zip, a1.zip, a2.zip, +2 more files)", advice)

    def test_a_legacy_and_a_new_export_of_one_machine_get_the_re_export_advice(self):
        old = self._person("a", "UTC", stamp="20260203", name="a1.zip")
        _as_schema7(old)
        self._person("a", "UTC", stamp="20260210", name="a2.zip")
        exports, problems = team.read_exports([self.folder.dir])
        self.assertEqual([e.bucket_zone for e in sorted(exports, key=lambda e: e.stamp)], [None, "UTC"])
        messages = [w["message"] for w in team.merge(exports, problems)["warnings"] if w["level"] == "warn"]
        self.assertTrue(any("version 7 to re-export" in m for m in messages), messages)
        self.assertEqual([m for m in messages if "--tz" in m], [], "no zone warning for one zone and legacy")


class TestRollingMondayReExport(unittest.TestCase):
    """One person re-exports three times: two cumulative exports, then a rolling
    one whose window starts on a Monday. A session crosses Sunday midnight into
    that Monday. Every request is in some export, so the merge is exact and has
    nothing to warn about -- in UTC and in a zone east or west of it."""

    MONDAYS = ("2026-02-02", "2026-02-09", "2026-02-16")
    EXPORTS = (("2026-01-26", "2026-02-10"), ("2026-01-26", "2026-02-24"), ("2026-02-09", "2026-02-24"))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = None

    def _instants(self, zone):
        """{session id: [UTC instants]}: each Monday one session from local Sunday
        23:30 through Monday 00:30 to 08:00, and one on the Wednesday."""
        out = {}
        for monday in self.MONDAYS + ("2026-02-23",):
            day = dt.date.fromisoformat(monday)
            spec = {"cross": [(day - dt.timedelta(days=1), 23, 30), (day, 0, 30), (day, 8, 0)],
                    "mid": [(day + dt.timedelta(days=2), 14, 0)]}
            for kind, stamps in spec.items():
                out[f"s{monday}{kind}"] = [dt.datetime(d.year, d.month, d.day, h, m, tzinfo=zone)
                                           .astimezone(dt.timezone.utc) for d, h, m in stamps]
        return out

    def _logs(self, name, zone, until):
        cutoff = dt.datetime.fromisoformat(until + "T12:00").replace(tzinfo=zone)
        root = os.path.join(self.tmp, name)
        for sid, stamps in self._instants(zone).items():
            records = [_response(f"{sid}-{i}", t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), inp=1_000_000)
                       for i, t in enumerate(stamps) if t <= cutoff]
            if records:
                _write(os.path.join(root, "-Users-x-p", sid + ".jsonl"), records)
        return scan(root, cache=FileCache(os.path.join(self.tmp, name + ".json")), tz=zone)

    def _merged(self, hours, label, exports):
        zone = dt.timezone.utc if hours == 0 else dt.timezone(dt.timedelta(hours=hours))
        self.folder = TeamFolder(tz=zone)
        self.addCleanup(self.folder.close)
        for n, (since, until) in enumerate(exports):
            payload = self._logs(f"p{hours}-{n}", zone, until)
            self.folder.zip(payload, "a@example.com", "a@dev.io", f"2026{n:04d}", since=since, until=until,
                            machine="laptop", name=f"e{n}.zip", bucket_zone=label)
        everything = self._logs(f"truth{hours}", zone, exports[-1][1])
        exported, problems = team.read_exports([self.folder.dir])
        return team.merge(exported, problems), sum(r[13] for r in everything["usage"])

    def _check(self, hours, label, exports):
        m, truth = self._merged(hours, label, exports)
        for kind in ("summary", "breakdown"):
            self.assertAlmostEqual(_cost(m["files"][kind][1]), truth, places=5, msg=kind)
        self.assertEqual([w["message"] for w in m["warnings"] if w["level"] == "warn"], [])

    def test_a_rolling_export_from_a_monday_does_not_warn_in_utc(self):
        self._check(0, "UTC", self.EXPORTS)

    def test_nor_does_it_east_or_west_of_utc(self):
        for hours, label in ((9, "Fixed/+9"), (-8, "Fixed/-8")):
            with self.subTest(zone=label):
                self._check(hours, label, self.EXPORTS)

    def test_the_two_cumulative_exports_alone_do_not_warn(self):
        self._check(0, "UTC", self.EXPORTS[:2])


    def test_a_dropped_machines_session_that_crossed_into_the_monday_still_warns(self):
        # The same crossing, but the desktop's own session ssss (Sunday 23:00
        # into Monday 01:00) is in no export that supplies the week of 02-09:
        # the laptop's rolling export starts that Monday and can't list it, so
        # it is the weekly cost gap that shows the Monday part is missing.
        utc = dt.timezone.utc
        at = lambda day, hour: f"2026-{day}T{hour:02d}:00:00.000Z"
        shared = {"zzzz": [_response("z0", at("02-10", 9), inp=1_000_000)],
                  "tttt": [_response("t0", at("02-11", 9), inp=1_000_000)]}
        lap_root, desk_root = os.path.join(self.tmp, "lap"), os.path.join(self.tmp, "desk")
        for root, extra in ((lap_root, {}), (desk_root, {"ssss": [
                _response("s0", at("02-08", 23), inp=1_000_000), _response("s1", at("02-09", 1), inp=3_000_000)]})):
            for fn, records in {**shared, **extra}.items():
                _write(os.path.join(root, "-Users-x-p", fn + ".jsonl"), records)
        folder = TeamFolder(tz=utc)
        self.addCleanup(folder.close)
        for root, machine, stamp, since in ((desk_root, "desktop", "20260215", "2026-02-02"),
                                            (lap_root, "laptop", "20260216", "2026-02-09")):
            payload = scan(root, cache=FileCache(root + ".json"), tz=utc)
            folder.zip(payload, "a@example.com", "a@dev.io", stamp, since=since, until="2026-02-15",
                       machine=machine, name=machine + ".zip", bucket_zone="UTC")
        exported, problems = team.read_exports([folder.dir])
        warns = [w["message"] for w in team.merge(exported, problems)["warnings"] if w["level"] == "warn"]
        (warn,) = warns
        self.assertIn("1 session (est. $", warn)
        self.assertIn("1 in the week of 2026-02-09", warn)


class TestPreWindowSessionDrops(unittest.TestCase):
    """A session that started before the export supplying a week begins (Sunday
    23:00 into Monday 01:00, against a rolling export from that Monday) can't be
    on that export's sessions sheet, though its Monday work is in the weekly
    rows. Whether it is lost therefore can't be read off the sheet: the merge
    asks whether the weekly cost and the count of sessions with work that week
    leave room for it. All in UTC, S is the crossing session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = TeamFolder(tz=dt.timezone.utc)
        self.addCleanup(self.folder.close)
        self._n = 0

    def _req(self, day, hour, inp=1_000_000, mid=None):
        self._n += 1
        return _response(mid or f"m{self._n}", f"2026-{day}T{hour:02d}:00:00.000Z", inp=inp)

    def _s(self, monday_inp=3_000_000):
        return [self._req("02-08", 23, mid="s0"), self._req("02-09", 1, inp=monday_inp, mid="s1")]

    def _export(self, files, stamp, since, until, machine="laptop"):
        self._n += 1
        root = os.path.join(self.tmp, f"l{self._n}")
        for key, records in files.items():
            project, fn = key.split(":") if ":" in key else ("p", key)  # "x:s" is file s in project x
            _write(os.path.join(root, f"-Users-x-{project}", fn + ".jsonl"), records)
        utc = dt.timezone.utc
        payload = scan(root, cache=FileCache(root + ".json"), tz=utc)
        moment = dt.datetime.fromisoformat(until + "T12:00").replace(tzinfo=utc)
        original = export_mod.build_context

        def build(payload, person, since=None, until=None, now=None, zone=None):
            return original(payload, person, since, until, now=moment, zone=zone)
        with mock.patch.object(export_mod, "build_context", build):
            self.folder.zip(payload, "a@example.com", "a@dev.io", stamp, since=since, until=until,
                            machine=machine, name=f"e{stamp}.zip", bucket_zone="UTC")

    def _warns(self):
        exported, problems = team.read_exports([self.folder.dir])
        return [w["message"] for w in team.merge(exported, problems)["warnings"] if w["level"] == "warn"]

    def _lost(self):
        return [w for w in self._warns() if "missing from the summary" in w]

    def test_the_newer_rolling_export_with_more_later_work_still_warns(self):
        # The rolling export lacks S, but its Thursday work is worth more than S's
        # Monday part, so the weekly cost gap alone is hidden.
        w = [self._req("02-10", 9, mid="w0")]
        self._export({"s": self._s(), "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"w": w, "l": [self._req("02-12", 9, inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")
        (warn,) = self._lost()
        self.assertIn("1 session", warn)

    def test_the_same_when_both_exports_run_to_the_end_of_the_week(self):
        w = [self._req("02-10", 9, mid="w0")]
        self._export({"s": self._s(), "w": w}, "20260214", "2026-02-02", "2026-02-15")
        self._export({"w": w, "l": [self._req("02-12", 9, inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")
        self.assertEqual(len(self._lost()), 1, self._warns())

    def test_a_tiny_dropped_session_warns_though_it_costs_less_than_half_a_cent(self):
        tiny = [self._req("02-08", 23, inp=100, mid="s0"), self._req("02-09", 1, inp=100, mid="s1")]
        w = [self._req("02-10", 9, mid="w0")]
        self._export({"s": tiny, "w": w}, "20260214", "2026-02-02", "2026-02-15")
        self._export({"w": w}, "20260215", "2026-02-09", "2026-02-15")
        (warn,) = self._lost()
        self.assertIn("1 session", warn)

    def test_a_machine_with_only_the_crossing_session_in_that_week_still_warns(self):
        d = [self._req("02-03", 9, mid="d0")]
        self._export({"s": self._s(), "d": d}, "20260214", "2026-02-02", "2026-02-15", machine="same")
        self._export({"w": [self._req("02-10", 9, mid="w0")],
                      "l": [self._req("02-12", 9, inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15", machine="same")
        self.assertEqual(len(self._lost()), 1, self._warns())

    # ---- exact merges the sessions_active count must not mistake for drops ---- #

    def _rec(self, day, hm, inp=1_000_000, mid=None):
        self._n += 1
        return _response(mid or f"m{self._n}", f"2026-{day}T{hm}:00.000Z", inp=inp)

    def _prompt_at(self, day, hm, mid):
        return _prompt(mid, f"2026-{day}T{hm}:00.000Z")

    def test_a_prompt_just_after_sunday_midnight_with_no_reply_is_not_a_drop(self):
        # Session h's last record is a prompt after midnight: its span reaches the
        # new week but it has no request there.
        rec, prompt = self._rec, self._prompt_at
        w, h = [rec("02-10", "09:00", mid="w0")], [rec("02-15", "20:00", mid="h0"), prompt("02-16", "00:05", "h1")]
        self._export({"w": w, "h": h}, "20260217", "2026-02-02", "2026-02-17")
        self._export({"w": w, "h": h, "x": [rec("02-17", "09:00", mid="w2")]}, "20260222", "2026-02-16", "2026-02-22")
        self.assertEqual(self._warns(), [])

    def test_a_long_session_idle_for_a_whole_week_is_not_a_drop(self):
        rec = self._rec
        pl = [rec("02-08", "20:00", mid="pl0"), rec("02-09", "10:00", mid="pl1"), rec("02-17", "10:00", mid="pl2")]
        z = [rec("02-10", "11:00", mid="z0"), rec("02-25", "11:00", mid="z1")]
        self._export({"pl": pl, "z": z[:1]}, "20260222", "2026-02-02", "2026-02-22")
        self._export({"pl": pl, "z": z}, "20260228", "2026-02-09", "2026-02-28")
        self.assertEqual(self._warns(), [])

    def test_a_session_opened_midweek_with_work_only_the_next_week_is_not_a_drop(self):
        rec = self._rec
        p = [rec("02-08", "23:30", mid="p0"), rec("02-09", "00:30", mid="p1")]
        w = [rec("02-10", "09:00", mid="w0")]
        g = [self._prompt_at("02-10", "18:00", "g0"), rec("02-18", "10:00", mid="g1")]
        self._export({"p": p, "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"p": p, "w": w, "g": g}, "20260220", "2026-02-09", "2026-02-20")
        self.assertEqual(self._warns(), [])

    def test_a_prompt_before_midnight_answered_after_it_is_not_a_drop(self):
        rec = self._rec
        p = [rec("02-08", "23:30", mid="p0"), rec("02-09", "00:30", mid="p1")]
        w = [rec("02-10", "09:00", mid="w0")]
        f = [self._prompt_at("02-15", "23:58", "f0"), rec("02-16", "00:01", mid="f1")]
        self._export({"p": p, "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"p": p, "w": w, "f": f}, "20260216", "2026-02-09", "2026-02-15")
        self.assertEqual(self._warns(), [])

    def test_a_trailing_prompt_in_a_session_that_spans_into_the_rolling_week_is_not_a_drop(self):
        rec = self._rec
        p = [rec("02-08", "23:30", mid="p0"), rec("02-09", "00:30", mid="p1")]
        w = [rec("02-10", "09:00", mid="w0")]
        h = [rec("02-09", "10:00", mid="h0"), self._prompt_at("02-16", "00:05", "h1")]
        q = [rec("02-15", "23:30", mid="q0"), rec("02-16", "00:30", mid="q1")]
        files = {"p": p, "w": w, "h": h, "q": q}
        self._export(files, "20260217", "2026-02-02", "2026-02-17")
        self._export(files, "20260222", "2026-02-16", "2026-02-22")
        self.assertEqual(self._warns(), [])

    def test_many_pre_window_sessions_held_do_not_hide_one_dropped(self):
        rec = self._rec
        many = {f"m{i}": [rec("02-08", f"2{i % 4}:{i:02d}", mid=f"m{i}a"), rec("02-09", f"0{i % 9}:{i:02d}", mid=f"m{i}b")]
                for i in range(30)}
        w = [rec("02-10", "09:00", mid="w0")]
        self._export({**many, "s": self._s(), "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({**many, "w": w, "l": [rec("02-12", "09:00", inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")
        self.assertEqual(len(self._lost()), 1, self._warns())

    def test_an_exact_rolling_re_export_stays_silent(self):
        w = [self._req("02-10", 9, mid="w0")]
        self._export({"s": self._s(), "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"s": self._s(), "w": w, "l": [self._req("02-12", 9, inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")
        self.assertEqual(self._warns(), [])

    # ---- the count check is per project, and only where both sides have counts ---- #

    def test_only_the_sessions_of_the_short_project_are_reported(self):
        # S (project x, $8) is dropped; both exports hold the pre-window session Qy
        # (project y), so y is accounted for and only S is lost.
        rec = self._rec
        s = [rec("02-08", "23:00", mid="s0"), rec("02-09", "01:00", inp=3_000_000, mid="s1")]
        qy = [rec("02-08", "22:00", mid="q0"), rec("02-09", "02:00", mid="q1")]
        w = [rec("02-10", "09:00", mid="w0")]
        self._export({"x:s": s, "y:q": qy, "x:w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"y:q": qy, "x:w": w, "x:l": [rec("02-12", "09:00", inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")
        (warn,) = self._lost()
        self.assertIn("1 session (est. $8.00)", warn)
        self.assertIn("1 in the week of 2026-02-09", warn)

    def _rolling_pair(self):
        rec = self._rec
        p = [rec("02-08", "23:30", mid="p0"), rec("02-09", "00:30", mid="p1")]
        w = [rec("02-10", "09:00", mid="w0")]
        self._export({"p": p, "w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"p": p, "w": w, "l": [rec("02-12", "09:00", inp=5_000_000, mid="l0")]},
                     "20260215", "2026-02-09", "2026-02-15")

    def _edit_zip(self, name, edit):
        """Rewrite the zip `name` in the folder: edit(member name, bytes) -> bytes, or None to drop it."""
        path = os.path.join(self.folder.dir, name)
        with zipfile.ZipFile(path) as z:
            items = [(n, z.read(n)) for n in z.namelist()]
        with zipfile.ZipFile(path, "w") as z:
            for n, data in items:
                data = edit(n, data)
                if data is not None:
                    z.writestr(n, data)

    def _blank_active(self, name, project=None):
        """Blank sessions_active in zip `name`'s summary, for `project` only if given."""
        def blank(member, data):
            if "-summary-" not in member:
                return data
            rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
            at, who = rows[0].index("sessions_active"), rows[0].index("project")
            for row in rows[1:]:
                if project is None or row[who].rsplit("/", 1)[-1].split(" ")[0] == project:
                    row[at] = ""
            out = io.StringIO()
            csv.writer(out).writerows(rows)
            return out.getvalue().encode("utf-8")
        self._edit_zip(name, blank)

    def test_a_blank_sessions_active_in_the_chosen_export_is_not_a_drop(self):
        # Blank is missing, not zero: the count can't judge, so an exact merge is silent.
        self._rolling_pair()
        self._blank_active("e20260215.zip")
        self.assertEqual(self._warns(), [])

    def test_a_chosen_export_with_no_summary_file_is_not_a_drop(self):
        # Without a summary the breakdown rows are all the weeks have: no sessions_active.
        self._rolling_pair()
        self._edit_zip("e20260215.zip", lambda name, data: None if "-summary-" in name else data)
        self.assertEqual(self._warns(), [])

    # ---- the cost-gap path names only the short projects' sessions, when there are any ---- #

    def _two_projects(self, chosen_extra=None):
        """S (project x, $8) dropped; both exports hold the pre-window Qy (project
        y, $2). The chosen export has no later work unless `chosen_extra`, so the
        weekly cost gap shows the loss."""
        rec = self._rec
        s = [rec("02-08", "23:00", mid="s0"), rec("02-09", "01:00", inp=3_000_000, mid="s1")]
        qy = [rec("02-08", "22:00", mid="q0"), rec("02-09", "02:00", mid="q1")]
        w = [rec("02-10", "09:00", mid="w0")]
        self._export({"x:s": s, "y:q": qy, "x:w": w}, "20260210", "2026-02-02", "2026-02-10")
        self._export({"y:q": qy, "x:w": w, **(chosen_extra or {})}, "20260215", "2026-02-09", "2026-02-15")

    def test_a_cost_gap_names_only_the_sessions_of_the_short_project(self):
        self._two_projects()
        (warn,) = self._lost()
        self.assertIn("1 session (est. $8.00)", warn)

    def test_a_cost_gap_with_no_short_project_names_every_unlistable_session(self):
        # The chosen export's count for x is blank, so no project is judged short
        # and the cost gap alone says work is missing: both pre-window sessions
        # are named, as the cost gap can't tell which project lost it.
        self._two_projects()
        self._blank_active("e20260215.zip", project="x")
        (warn,) = self._lost()
        self.assertIn("2 sessions (est. $10.00)", warn)


class TestChoosingAWeekScales(unittest.TestCase):
    """Re-exports pile up; choosing a week's export must not cost
    weeks x exports^2 x sessions (six re-exports took /api/team to 37 s)."""

    def test_many_sessions_and_re_exports_stay_fast(self):
        import time
        first = dt.date(2024, 10, 7)
        weeks = [(first + dt.timedelta(weeks=i)).isoformat() for i in range(104)]
        sessions = [{"session_id": f"s{i}",
                     "started_at": (first + dt.timedelta(days=i % 728)).isoformat() + "T10:00:00"}
                    for i in range(20000)]
        def exports(count):  # the same machine re-exported `count` times
            out = []
            for n in range(count):
                e = team.Export(f"export-{n}", f"2026100{n}")
                e.files = {"summary": [{"week_start": w, "project": "p", "window_from": weeks[0],
                                        "window_to": "2026-10-04", "exported_at": f"2026-10-0{n + 1}"}
                                       for w in weeks],
                           "sessions": sessions}
                out.append(e)
            return out

        def seconds(chosen_from):
            started = time.perf_counter()
            for week in weeks:
                chosen = team._choose_for_week(week, {e: e.files["summary"] for e in chosen_from})[0]
                self.assertEqual(chosen, [chosen_from[-1]], "identical re-exports: the newest")
            return time.perf_counter() - started

        # How the time grows from 2 to 6 re-exports, not a time limit: CI
        # runners vary several-fold in speed, so a fixed limit was flaky. The
        # best of three runs filters out a runner's pauses. Growing linearly,
        # this is about 3x; the weeks x exports^2 x sessions version was 14x.
        two, six = exports(2), exports(6)
        growth = min(seconds(six) for _ in range(3)) / min(seconds(two) for _ in range(3))
        self.assertLess(growth, 7)


class TestSeatPricesNextToAZip(unittest.TestCase):
    def test_a_price_file_beside_a_zip_given_directly_is_used(self):
        folder = TeamFolder()
        try:
            path = folder.zip(_scan(), "a@example.com", "a@dev.io", "20260210")
            with open(os.path.join(folder.dir, team.SEAT_PRICE_FILE), "w", encoding="utf-8") as fh:
                json.dump({"per": "month", "prices": {"default": 30}}, fh)
            p = team._load([path])["people"][0]
            self.assertEqual(p["seat_price"], 30)
        finally:
            folder.close()


class TestAsKeepsTheAccount(unittest.TestCase):
    def test_exports_stamped_with_as_stay_under_the_claude_account(self):
        # --as changes the git email on the rows, not the Claude account the
        # merge keys people by: both exports are one person, and not flagged.
        folder = TeamFolder()
        try:
            payload = _scan()
            folder.zip(payload, "a@example.com", "a@dev.io", "20260210")
            folder.zip(payload, "a@example.com", "someone-else@dev.io", "20260211")
            m = team._load([folder.dir])
            self.assertEqual([p["person"] for p in m["people"]], ["a@example.com"])
            self.assertFalse(any("more than one person" in w["message"] for w in m["warnings"]))
        finally:
            folder.close()


class TestIdentityMerges(unittest.TestCase):
    """Exports with no Claude account email are told apart by git email alone,
    which two people can share; the merge says so rather than quietly joining."""

    def setUp(self):
        self.folder = TeamFolder()
        self.addCleanup(self.folder.close)
        self.payload = _scan()
        self.other = _on_another_machine(self.payload)  # not the same logs

    def _identity_warnings(self):
        m = team._load([self.folder.dir])
        notes = [w for w in m["warnings"] if w["level"] == "warn" and "--as" in w["message"]]
        return m, notes

    def _two(self, git_email, claude_email=None):
        for machine, stamp in (("laptop", "20260210"), ("desktop", "20260211")):
            self.folder.zip(self.payload if machine == "laptop" else self.other, claude_email, git_email, stamp, machine=machine,
                            name=f"claude-usage-export-x-{machine}-{stamp}.zip")

    def test_shared_git_email_without_claude_account_warns_with_the_flag(self):
        self._two("dev@team.io")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["dev@team.io"])
        self.assertEqual(len(notes), 1)
        self.assertIn("dev@team.io", notes[0]["message"])
        self.assertIn("collect.py --export --as", notes[0]["message"])

    def test_one_warning_per_identity_however_many_files(self):
        for i, machine in enumerate(("a", "b", "c")):
            self.folder.zip(self.payload if i == 0 else _on_another_machine(self.payload) if i == 1
                            else _on_another_machine(_on_another_machine(self.payload)),
                            None, "dev@team.io", f"2026021{i}", machine=machine,
                            name=f"claude-usage-export-x-{machine}.zip")
        self.assertEqual(len(self._identity_warnings()[1]), 1)

    def test_two_unknowns_warn(self):
        self._two("unknown")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["unknown"])
        self.assertEqual(len(notes), 1)
        self.assertIn("unknown", notes[0]["message"])
        self.assertIn("git config", notes[0]["message"])
        self.assertIn("collect.py --export --as", notes[0]["message"])

    def test_os_user_name_fallback_warns_like_unknown(self):
        # With no git user.email the export stamps $USER, which a lot of
        # machines share ("ubuntu", "vscode", "admin").
        self._two("ubuntu")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["ubuntu"])
        self.assertEqual(len(notes), 1)
        self.assertIn("OS user name", notes[0]["message"])
        self.assertIn("collect.py --export --as", notes[0]["message"])

    def test_a_lone_export_without_a_claude_account_is_not_flagged(self):
        self.folder.zip(self.payload, None, "dev@team.io", "20260210")
        self.assertEqual(self._identity_warnings()[1], [])

    def test_claude_accounts_are_never_flagged_as_merged_by_git_email(self):
        self._two("a@dev.io", claude_email="a@example.com")
        self.assertEqual(self._identity_warnings()[1], [])

    def test_blank_claude_email_is_not_aliased_to_a_claude_account(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.zip(self.other, None, "dev@team.io", "20260211", machine="server",
                        name="claude-usage-export-dev-server-20260211.zip")
        m, notes = self._identity_warnings()
        self.assertEqual(sorted(p["person"] for p in m["people"]), ["alice@example.com", "dev@team.io"])
        # One person with an OAuth machine and an API-key machine is the cost
        # of this: the warning tells them how to join the two.
        self.assertEqual(len(notes), 1)
        self.assertIn("alice@example.com", notes[0]["message"])
        self.assertIn("collect.py --export --as alice@example.com", notes[0]["message"])

    def test_as_with_the_claude_email_joins_an_api_key_machine(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.zip(self.other, None, "alice@example.com", "20260211", machine="server",
                        name="claude-usage-export-dev-server-20260211.zip")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])
        self.assertEqual(m["people"][0]["machines"], 2)
        self.assertEqual(notes, [])

    def test_schema_1_export_is_still_aliased_by_git_email(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.schema1_summary(self.payload, "dev@team.io", "20260101")
        exports, _ = team.read_exports([self.folder.dir])
        old = next(e for e in exports if e.schema_version == 1)
        self.assertFalse(old.has_claude_column)
        self.assertEqual(set(team._person_keys(exports).values()), {"alice@example.com"})
        self.assertEqual(self._identity_warnings()[1], [])

    def test_git_email_with_two_claude_accounts_warns_and_matches_neither(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.zip(self.other, "bob@example.com", "dev@team.io", "20260211", machine="bob",
                        name="claude-usage-export-bob-20260211.zip")
        self.folder.schema1_summary(self.payload, "dev@team.io", "20260101")
        m, notes = self._identity_warnings()
        self.assertEqual(len(notes), 1)
        self.assertIn("alice@example.com", notes[0]["message"])
        self.assertIn("bob@example.com", notes[0]["message"])
        self.assertEqual(sorted(p["person"] for p in m["people"]),
                         ["alice@example.com", "bob@example.com", "dev@team.io"])

    def test_limit_episodes_are_not_combined_across_an_unidentified_identity(self):
        limits = _scan(LIMIT_FIXTURES)
        self.folder.zip(limits, None, "dev@team.io", "20260210", machine="one")
        self.folder.zip(_on_another_machine(limits), None, "dev@team.io", "20260211",
                        machine="two", name="claude-usage-export-x-two-20260211.zip")
        m = team._load([self.folder.dir])
        resets = [e for e in limits["limits"] if e["resets_at"]]
        self.assertEqual(len(m["files"]["limits"][1]), 2 * len(resets))

    def test_re_exports_from_one_machine_are_not_flagged(self):
        # The same machine id is the same person by construction, whatever the
        # identity is: an API-key user exporting every week gets no warning.
        for ident in ("dev@team.io", "unknown", "ubuntu"):
            folder = TeamFolder()
            try:
                folder.zip(self.payload, None, ident, "20260210", machine="laptop")
                folder.zip(self.other, None, ident, "20260217", machine="laptop",
                           name="claude-usage-export-x-again-20260217.zip")
                m = team._load([folder.dir])
                self.assertEqual([p["person"] for p in m["people"]], [ident])
                self.assertEqual([w for w in m["warnings"] if "--as" in w["message"]
                                  and "more than one person" not in w["message"]], [], ident)
            finally:
                folder.close()

    def test_a_re_cloned_machine_is_one_machine_not_two_people(self):
        # Two machine ids over the same sessions are one machine (the tool was
        # re-cloned), so there is nothing to warn about.
        for ident in ("dev@team.io", "unknown"):
            folder = TeamFolder()
            try:
                folder.zip(self.payload, None, ident, "20260210", machine="a")
                folder.zip(self.payload, None, ident, "20260211", machine="b",
                           name="claude-usage-export-x-b-20260211.zip")
                m = team._load([folder.dir])
                self.assertEqual([p["person"] for p in m["people"]], [ident])
                self.assertEqual(m["people"][0]["machines"], 1)
                self.assertEqual([w for w in m["warnings"] if "counted as one person" in w["message"]],
                                 [], ident)
            finally:
                folder.close()

    def test_two_machines_with_different_sessions_still_warn_once(self):
        self._two("dev@team.io")
        self.assertEqual(len([w for w in self._identity_warnings()[1]
                              if "counted as one person" in w["message"]]), 1)

    def test_exports_without_a_machine_id_count_as_one_machine_among_themselves(self):
        self.folder.zip(self.payload, None, "dev@team.io", "20260210", machine=None)
        self.folder.zip(self.other, None, "dev@team.io", "20260217", machine=None,
                        name="claude-usage-export-x-again-20260217.zip")
        self.assertEqual(self._identity_warnings()[1], [])
        # ... but not next to an export that does name its machine.
        self.folder.zip(_on_another_machine(self.other), None, "dev@team.io", "20260218",
                        machine="server", name="claude-usage-export-x-server-20260218.zip")
        self.assertEqual(len(self._identity_warnings()[1]), 1)

    def test_blank_claude_export_sharing_nothing_with_the_account_is_split_and_flagged(self):
        # Another machine, no shared session: nothing says it is the same
        # person, so it stays apart (with the way to join it).
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.zip(self.other, None, "dev@team.io", "20260211", machine="server",
                        name="claude-usage-export-dev-apikey-20260211.zip")
        m, notes = self._identity_warnings()
        self.assertEqual(len(notes), 1)
        self.assertIn("--as alice@example.com", notes[0]["message"])

    def test_git_email_equal_to_the_claude_email_is_a_join_not_a_split(self):
        # (A) an API-key machine exported with --as <claude email>, next to the
        # OAuth export whose git email is the same address.
        self.folder.zip(self.payload, "alice@example.com", "alice@example.com", "20260210")
        self.folder.zip(self.other, None, "alice@example.com", "20260211", machine="server",
                        name="claude-usage-export-alice-server-20260211.zip")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])
        self.assertEqual(notes, [])
        self.assertFalse([w for w in m["warnings"] if "kept apart" in w["message"]])

    def test_schema_1_export_aliased_to_an_account_with_the_same_git_email_is_not_flagged(self):
        # (B) a pure join.
        self.folder.zip(self.payload, "alice@example.com", "alice@example.com", "20260210")
        self.folder.schema1_summary(self.payload, "alice@example.com", "20260101")
        m, _ = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])
        self.assertFalse([w for w in m["warnings"] if "kept apart" in w["message"]])

    def test_switching_one_machine_from_oauth_to_an_api_key_stays_one_person(self):
        # Same machine id and the same sessions: provably the same logs, so
        # they are not counted twice.
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
        self.folder.zip(self.payload, None, "dev@team.io", "20260217",
                        name="claude-usage-export-dev-apikey-20260217.zip")
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])
        self.assertEqual(notes, [])
        alone = TeamFolder()
        try:
            alone.zip(self.payload, "alice@example.com", "dev@team.io", "20260210")
            one = team._load([alone.dir])
        finally:
            alone.close()
        self.assertAlmostEqual(_cost(m["files"]["summary"][1]), _cost(one["files"]["summary"][1]),
                               places=5)

    def test_a_blank_claude_export_sharing_only_sessions_with_the_account_is_joined(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210", machine="a")
        self.folder.zip(self.payload, None, "dev@team.io", "20260217", machine="b",
                        name="claude-usage-export-dev-b-20260217.zip")
        m, _ = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])

    def test_a_blank_claude_export_on_the_accounts_machine_is_joined_without_shared_sessions(self):
        self.folder.zip(self.payload, "alice@example.com", "dev@team.io", "20260210", machine="a")
        self.folder.zip(self.other, None, "dev@team.io", "20260217", machine="a",
                        name="claude-usage-export-dev-a-20260217.zip")
        exports, _ = team.read_exports([self.folder.dir])
        self.assertTrue(exports[0].sessions_started.keys().isdisjoint(exports[1].sessions_started))
        m, notes = self._identity_warnings()
        self.assertEqual([p["person"] for p in m["people"]], ["alice@example.com"])
        self.assertEqual(notes, [])

    def test_a_non_email_identity_is_not_called_an_os_user_name_outright(self):
        self._two("alice")
        message = self._identity_warnings()[1][0]["message"]
        self.assertIn("a name passed with --as", message)
        self.assertIn("collect.py --export --as", message)
        self.assertNotIn("{", message)

    def test_the_machine_note_only_promises_one_episode_for_a_claude_account(self):
        self._two("dev@team.io")
        m = team._load([self.folder.dir])
        notes = [w["message"] for w in m["warnings"] if "machines" in w["message"] and w["level"] == "info"]
        self.assertTrue(notes)
        self.assertFalse([n for n in notes if "counted once" in n])
        other = TeamFolder()
        self.addCleanup(other.close)
        for machine, stamp, payload in (("laptop", "20260210", self.payload),
                                        ("desktop", "20260211", self.other)):
            other.zip(payload, "a@example.com", "a@dev.io", stamp, machine=machine,
                      name=f"claude-usage-export-a-{machine}-{stamp}.zip")
        notes = [w["message"] for w in team._load([other.dir])["warnings"] if "2 machines" in w["message"]]
        self.assertTrue([n for n in notes if "counted once" in n])


class TestDisplayName(unittest.TestCase):
    def test_a_real_git_name_is_used(self):
        self.assertEqual(team.display_name("a.b@x.com", "Sam Lee"), "Sam Lee")

    def test_a_handle_falls_back_to_the_email(self):
        self.assertEqual(team.display_name("jane.doe@example.com", "janedoe"),
                         "Jane Doe")

    def test_no_git_name(self):
        self.assertEqual(team.display_name("sam.lee@example.com", None), "Sam Lee")
        self.assertEqual(team.display_name("sam.lee@example.com", "sam.lee@example.com"),
                         "Sam Lee")

    def test_separators_and_case(self):
        self.assertEqual(team.display_name("JANE_DOE-SMITH@x.com", ""), "Jane Doe Smith")

    def test_a_key_with_no_at_is_kept(self):
        self.assertEqual(team.display_name("ubuntu", None), "ubuntu")


def _set_cell(path, column, value):
    """Rewrite one context column on every row of every CSV in an export zip."""
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i.filename).decode("utf-8")) for i in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members:
            rows = list(csv.reader(io.StringIO(text)))
            if column in rows[0]:
                i = rows[0].index(column)
                for row in rows[1:]:
                    row[i] = value
            out = io.StringIO()
            csv.writer(out).writerows(rows)
            archive.writestr(name, out.getvalue())


def _drop_column(path, column):
    """Rewrite every CSV in an export zip without `column`, as an older export lacks it."""
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i.filename).decode("utf-8")) for i in archive.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members:
            rows = list(csv.reader(io.StringIO(text)))
            if column in rows[0]:
                i = rows[0].index(column)
                rows = [row[:i] + row[i + 1:] for row in rows]
            out = io.StringIO()
            csv.writer(out).writerows(rows)
            archive.writestr(name, out.getvalue())


class TestWarningsNamePeople(unittest.TestCase):
    """A manager acts on a person, not a count: the notices say who."""

    def setUp(self):
        self.folder = TeamFolder()
        self.calm = self.folder.zip(_scan(), "calm@example.com", "calm@dev.io", "20260310")
        self.folder.zip(_scan(LIMIT_FIXTURES), "lim@example.com", "lim@dev.io", "20260310")

    def tearDown(self):
        self.folder.close()

    def _messages(self, prices=None):
        exports, problems = team.read_exports([self.folder.dir])
        return [w["message"] for w in team.merge(exports, problems, prices)["warnings"]]

    def test_short_cli_history_names_the_person(self):
        _set_cell(self.calm, "cli_history_truncated", "yes")
        (msg,) = [m for m in self._messages() if "cut short" in m]
        self.assertIn("Calm (calm@example.com)", msg)
        self.assertNotIn("Lim", msg)
        self.assertNotIn("1 of 2 people", msg)

    def test_unpriced_tier_names_the_people(self):
        prices = {"currency": "USD", "per": "month", "prices": {"some_other_tier": 10.0},
                  "source": "seat-prices.json"}
        (msg,) = [m for m in self._messages(prices) if "no price" in m]
        self.assertIn("Calm (calm@example.com)", msg)
        self.assertIn("Lim (lim@example.com)", msg)


class TestDisplayNamesAreUnique(unittest.TestCase):
    def test_a_collision_takes_the_email_domain(self):
        people = [{"person": "john.smith@a.com", "display_name": "John Smith"},
                  {"person": "john_smith@b.com", "display_name": "John Smith"},
                  {"person": "ann@a.com", "display_name": "Ann"}]
        team._unique_display_names(people)
        self.assertEqual([p["display_name"] for p in people],
                         ["John Smith (a.com)", "John Smith (b.com)", "Ann"])

    def test_same_domain_falls_back_to_the_key(self):
        people = [{"person": "john.smith@a.com", "display_name": "John Smith"},
                  {"person": "john_smith@a.com", "display_name": "John Smith"}]
        team._unique_display_names(people)
        self.assertEqual([p["display_name"] for p in people],
                         ["John Smith (john.smith@a.com)", "John Smith (john_smith@a.com)"])


class TestTeamPayloadForManagers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = TeamFolder()
        cls.folder.zip(_scan(), "calm@example.com", "calm@dev.io", "20260310")
        cls.payload = team.team_payload([cls.folder.dir])

    @classmethod
    def tearDownClass(cls):
        cls.folder.close()

    def test_weekly_rows_carry_output_and_window_fields(self):
        for row in self.payload["weekly"]:
            self.assertEqual(len(row), 18)
            self.assertTrue(row[10] is None or isinstance(row[10], float))
            self.assertTrue(row[11] is None or isinstance(row[11], float))
            self.assertIsInstance(row[12], float)
            self.assertIsInstance(row[13], float)
            self.assertIsInstance(row[15], float)
            self.assertIsInstance(row[16], float)

    def test_weekly_rows_carry_exact_active_days(self):
        rows = self.payload["weekly"]
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(r[17], float) for r in rows))
        self.assertIn("week_active_days", team._SUMMED["summary"])

    def test_an_export_without_the_column_reads_none(self):
        folder = TeamFolder()
        try:
            path = folder.zip(_scan(), "old@example.com", "old@dev.io", "20260310")
            _drop_column(path, "week_active_days")
            payload = team.team_payload([folder.dir])
            self.assertTrue(payload["weekly"])
            self.assertTrue(all(r[17] is None for r in payload["weekly"]))
        finally:
            folder.close()

    def test_blank_lines_stay_null(self):
        quiet = [r for r in self.payload["weekly"] if not r[2]]
        self.assertTrue(quiet)  # the zero-usage week rows
        self.assertTrue(all(r[10] is None and r[11] is None for r in quiet))

    def test_model_rows_carry_the_exact_model(self):
        rows = self.payload["models"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 8)
            self.assertTrue(row[7].startswith("claude-"), row[7])   # the exact id, e.g. claude-opus-5-5

    def test_people_have_display_names(self):
        (p,) = self.payload["people"]
        self.assertEqual(p["display_name"], "Calm")

    def test_sources_name_their_zone(self):
        self.assertIn("bucket_zone", self.payload["sources"][0])

    def test_health_rows_carry_the_500k_column(self):
        rows = self.payload["health"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(r["very_long_context_cost_usd"], float) for r in rows))

    def test_an_older_export_has_no_500k_figure(self):
        folder = TeamFolder()
        try:
            path = folder.zip(_scan(), "old@example.com", "old@dev.io", "20260310")
            with zipfile.ZipFile(path) as archive:
                members = [(i.filename, archive.read(i.filename).decode("utf-8"))
                           for i in archive.infolist()]
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, text in members:
                    rows = list(csv.reader(io.StringIO(text)))
                    if "very_long_context_cost_usd" in rows[0]:
                        drop = rows[0].index("very_long_context_cost_usd")
                        rows = [r[:drop] + r[drop + 1:] for r in rows]
                    out = io.StringIO()
                    csv.writer(out).writerows(rows)
                    archive.writestr(name, out.getvalue())
            payload = team.team_payload([folder.dir])
            self.assertTrue(payload["health"]["rows"])
            self.assertTrue(all(r["very_long_context_cost_usd"] is None for r in payload["health"]["rows"]))
        finally:
            folder.close()

    def test_the_merge_sums_the_new_column(self):
        self.assertIn("very_long_context_cost_usd", team._SUMMED["summary"])


if __name__ == "__main__":
    unittest.main()
