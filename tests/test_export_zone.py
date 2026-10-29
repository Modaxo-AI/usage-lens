"""Exports bucket every day and week in one *export zone* -- a team-set zone
(`--tz`, else CLAUDE_USAGE_EXPORT_TZ, else UTC) -- not in the machine's local
zone, so re-exporting from anywhere gives the same buckets and everyone's weeks
line up. The personal dashboard (`/api/data`, data.json, `--static`) keeps the
machine's local days.

The zone is always passed as a tzinfo: nothing here changes the process TZ
(Windows has no `time.tzset`). Tests that need the *machine's* zone to differ
use tzset and are skipped where there is none.
"""
import contextlib
import copy
import csv
import datetime
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest import mock

import collect
import serve
from claude_usage import aggregate
from claude_usage import context as context_mod
from claude_usage import export as export_mod
from claude_usage import scan as scan_mod
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.jsrun import run_js

PERSON = ("someone@example.com", "Some One")
UTC = datetime.timezone.utc
# Fixed offsets, so most of this file runs without zone data (Windows has none
# unless the tzdata package is installed). Los Angeles is on daylight time for
# every instant used below.
TOKYO = datetime.timezone(datetime.timedelta(hours=9))
LOS_ANGELES = datetime.timezone(datetime.timedelta(hours=-7))


def _have_zone_data():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Tokyo"), ZoneInfo("America/Los_Angeles"), ZoneInfo("Pacific/Kiritimati")
        return True
    except Exception:  # noqa: BLE001
        return False


needs_zone_data = unittest.skipUnless(_have_zone_data(), "needs IANA zone data (pip install tzdata on Windows)")

# One session across a Sunday-to-Monday boundary in UTC.
#   2026-03-08T23:30Z  Sunday    UTC   Monday 08:30 Tokyo   Sunday 16:30 Los Angeles (PDT)
#   2026-03-09T00:30Z  Monday    UTC   Monday 09:30 Tokyo   Sunday 17:30 Los Angeles
# plus a second, later session so a week boundary is not the only difference.
STAMPS = {
    "sess-x": ["2026-03-08T23:30:00.000Z", "2026-03-09T00:30:00.000Z"],
    "sess-y": ["2026-03-12T03:00:00.000Z"],
}
NOW = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


def _write_logs(root):
    proj = os.path.join(root, "-Users-x-zone")
    os.makedirs(proj)
    for i, (sid, stamps) in enumerate(STAMPS.items()):
        records = []
        for n, ts in enumerate(stamps):
            content = ([{"type": "tool_use", "id": f"tu-{sid}", "name": "Bash", "input": {}}]
                       if n == 0 else [{"type": "text", "text": "ok"}])
            records.append({
                "type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/zone",
                "entrypoint": "cli", "requestId": f"r-{sid}-{n}",
                "message": {"id": f"m-{sid}-{n}", "role": "assistant", "model": "claude-sonnet-5",
                            "content": content,
                            "usage": {"input_tokens": 1000, "output_tokens": 100000 * (i + 1) * (n + 1)}}})
        if sid == "sess-x":
            # A PR and a weekly-limit notice, both inside the Sunday-night half hour.
            records.append({"type": "pr-link", "sessionId": sid, "timestamp": "2026-03-08T23:40:00.000Z",
                            "prNumber": 7, "prRepository": "o/r", "prUrl": "https://github.com/o/r/pull/7"})
            records.append({
                "type": "assistant", "sessionId": sid, "timestamp": "2026-03-08T23:45:00.000Z",
                "cwd": "/Users/x/zone", "uuid": "n-weekly", "isApiErrorMessage": True,
                "error": "rate_limit", "apiErrorStatus": 429,
                "message": {"id": "n-n-weekly", "role": "assistant", "model": "<synthetic>",
                            "content": [{"type": "text",
                                         "text": "You've hit your weekly limit \u00b7 resets Mar 12 at 9am (UTC)"}]}})
        records.sort(key=lambda r: r["timestamp"])
        with open(os.path.join(proj, sid + ".jsonl"), "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")


def _scan(root, tz=None, cache=None):
    cache = cache or FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json"))
    return scan(root, cache=cache, tz=tz)


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def _sheet(payload, kind, zone, now=NOW, since=None, until=None):
    ctx = export_mod.build_context(payload, PERSON, since, until, now=now, zone=zone)
    return _rows(export_mod.csv_text(payload, kind, PERSON, since, until, ctx=ctx))


class _Logs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        _write_logs(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)


@contextlib.contextmanager
def machine_zone(name):
    """Pin the machine's own zone (not available on Windows)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

class TestExportZoneResolution(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(context_mod.EXPORT_ZONE_ENV, None)

    def test_the_env_var_is_the_documented_name(self):
        self.assertEqual(context_mod.EXPORT_ZONE_ENV, "CLAUDE_USAGE_EXPORT_TZ")

    def test_default_is_utc_without_any_zone_data(self):
        with mock.patch.dict(sys.modules, {"zoneinfo": None}):  # no zoneinfo at all
            tz, label = context_mod.export_zone()
        self.assertIs(tz, UTC)
        self.assertEqual(label, "UTC")

    @needs_zone_data
    def test_env_var_is_used_when_there_is_no_argument(self):
        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Asia/Tokyo"
        tz, label = context_mod.export_zone()
        self.assertEqual(label, "Asia/Tokyo")
        self.assertEqual(tz.utcoffset(datetime.datetime(2026, 3, 9)), datetime.timedelta(hours=9))
        self.assertIsNot(tz, UTC)

    @needs_zone_data
    def test_argument_beats_env_var(self):
        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Asia/Tokyo"
        self.assertEqual(context_mod.export_zone("America/Los_Angeles")[1], "America/Los_Angeles")

    @needs_zone_data
    def test_blank_values_fall_through(self):
        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "  "
        self.assertEqual(context_mod.export_zone("")[1], "UTC")
        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Asia/Tokyo"
        self.assertEqual(context_mod.export_zone("  ")[1], "Asia/Tokyo")

    def test_utc_aliases_all_mean_the_utc_object(self):
        for name in ("UTC", "utc", "Utc", "Z", "z", "Etc/UTC", "etc/utc", " UTC "):
            with self.subTest(name):
                tz, label = context_mod.export_zone(name)
                self.assertIs(tz, UTC)
                self.assertEqual(label, "UTC")

    @needs_zone_data
    def test_an_iana_name_is_its_own_label_and_follows_dst(self):
        tz, label = context_mod.export_zone("America/Los_Angeles")
        self.assertEqual(label, "America/Los_Angeles")
        self.assertEqual(tz.utcoffset(datetime.datetime(2026, 1, 15)), datetime.timedelta(hours=-8))
        self.assertEqual(tz.utcoffset(datetime.datetime(2026, 7, 15)), datetime.timedelta(hours=-7))

    @needs_zone_data
    def test_the_label_is_the_canonical_name_whatever_the_case_typed(self):
        # Task B compares labels across people: asia/tokyo and Asia/Tokyo are one zone.
        for typed in ("asia/tokyo", "ASIA/TOKYO", "Asia/Tokyo"):
            with self.subTest(typed):
                tz, label = context_mod.export_zone(typed)
                self.assertEqual(label, "Asia/Tokyo")
                self.assertEqual(tz.utcoffset(datetime.datetime(2026, 3, 9)), datetime.timedelta(hours=9))
        self.assertEqual(context_mod.export_zone("america/NEW_york")[1], "America/New_York")

    @needs_zone_data
    def test_the_zone_database_is_listed_once_not_on_every_call(self):
        # export_zone runs per export and per /api/data request; listing every
        # zone takes ~25 ms each time.
        import zoneinfo
        real = zoneinfo.available_timezones
        calls = []
        context_mod._known_zones.cache_clear()
        self.addCleanup(context_mod._known_zones.cache_clear)
        with mock.patch.object(zoneinfo, "available_timezones",
                               lambda: calls.append(1) or real()):
            for typed in ("asia/tokyo", "Asia/Tokyo", "america/new_york", "Mars/Phobos"):
                try:
                    context_mod.export_zone(typed)
                except ValueError:
                    pass
        self.assertEqual(len(calls), 1)

    def test_unknown_name_is_a_value_error_naming_it(self):
        for name in ("Mars/Phobos", "Not A Zone", "../etc/passwd"):
            with self.subTest(name):
                with self.assertRaises(ValueError) as cm:
                    context_mod.export_zone(name)
                self.assertIn(name, str(cm.exception))

    def test_a_bad_env_var_is_a_value_error_too(self):
        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Mars/Phobos"
        with self.assertRaises(ValueError):
            context_mod.export_zone()

    def test_named_zone_without_zone_data_suggests_utc_or_tzdata(self):
        with mock.patch.dict(sys.modules, {"zoneinfo": None}):
            with self.assertRaises(ValueError) as cm:
                context_mod.export_zone("Asia/Tokyo")
        self.assertIn("UTC", str(cm.exception))
        self.assertIn("pip install tzdata", str(cm.exception))


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #

class TestBucketsWithAnExplicitZone(unittest.TestCase):
    TS = "2026-03-08T23:30:00.000Z"

    def test_local_date_in_a_given_zone(self):
        self.assertEqual(aggregate._local_date(self.TS, UTC), "2026-03-08")
        self.assertEqual(aggregate._local_date(self.TS, TOKYO), "2026-03-09")
        self.assertEqual(aggregate._local_date(self.TS, LOS_ANGELES), "2026-03-08")
        self.assertEqual(aggregate._local_date(self.TS, datetime.timezone(datetime.timedelta(hours=14))),
                         "2026-03-09")

    def test_local_bucket_in_a_given_zone(self):
        self.assertEqual(aggregate._local_bucket(self.TS, tz=UTC), "2026-03-08T23:30")
        self.assertEqual(aggregate._local_bucket(self.TS, tz=TOKYO), "2026-03-09T08:30")
        self.assertEqual(aggregate._local_bucket(self.TS, 60, tz=LOS_ANGELES), "2026-03-08T16:00")
        self.assertEqual(aggregate._local_bucket("2026-03-08T23:32:00.000Z", 5, TOKYO), "2026-03-09T08:30")

    def test_no_zone_is_the_machine_local_zone_as_before(self):
        local = datetime.datetime.fromisoformat(self.TS.replace("Z", "+00:00")).astimezone()
        self.assertEqual(aggregate._local_date(self.TS), local.strftime("%Y-%m-%d"))
        self.assertEqual(aggregate._local_bucket(self.TS)[:10], local.strftime("%Y-%m-%d"))
        self.assertEqual(aggregate._local_date(self.TS, None), aggregate._local_date(self.TS))

    def test_unreadable_timestamps_are_still_none(self):
        for ts in (None, "", "garbage"):
            self.assertIsNone(aggregate._local_date(ts, TOKYO))
            self.assertIsNone(aggregate._local_bucket(ts, tz=TOKYO))


class TestScanInAZone(_Logs):
    def test_every_day_bucket_follows_the_zone(self):
        for tz, first_day in ((UTC, "2026-03-08"), (TOKYO, "2026-03-09"), (LOS_ANGELES, "2026-03-08")):
            with self.subTest(tz=str(tz)):
                payload = _scan(self.root, tz)
                days = sorted({r[0][:10] for r in payload["usage"]})
                self.assertEqual(days[0], first_day)
                start_days = {s["session_id"]: s["start_day"] for s in payload["sessions"]}
                self.assertEqual(start_days["sess-x"], first_day)
                self.assertEqual(payload["summary"]["date_min"], first_day)

    def test_generated_at_is_in_the_zone(self):
        self.assertTrue(_scan(self.root, TOKYO)["generated_at"].endswith("+09:00"))
        self.assertTrue(_scan(self.root, UTC)["generated_at"].endswith("+00:00"))

    def test_grand_totals_do_not_depend_on_the_zone(self):
        def totals(p):
            return (round(sum(r[13] for r in p["usage"]), 6), sum(r[12] for r in p["usage"]),
                    sum(r[5] + r[6] for r in p["usage"]))
        base = totals(_scan(self.root, None))
        for tz in (UTC, TOKYO, LOS_ANGELES):
            self.assertEqual(totals(_scan(self.root, tz)), base)

    def test_a_second_scan_in_another_zone_reuses_the_parse_cache(self):
        # The cache stores facts, not buckets: no facts version bump, no re-parse.
        cache = FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json"))
        first = _scan(self.root, UTC, cache)
        second = _scan(self.root, TOKYO, cache)
        self.assertGreater(first["files_parsed"], 0)
        self.assertEqual(second["files_parsed"], 0)
        self.assertEqual(second["files_cached"], first["files_parsed"])
        self.assertNotEqual({r[0] for r in first["usage"]}, {r[0] for r in second["usage"]})

    def test_the_default_scan_is_unchanged(self):
        default = _scan(self.root)
        explicit = _scan(self.root, None)
        for p in (default, explicit):
            for k in ("scan_ms", "generated_at", "files_parsed", "files_cached"):
                p.pop(k)
        self.assertEqual(default, explicit)


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

class TestExportUsesTheZone(_Logs):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zones = {"UTC": (UTC, "UTC"), "Asia/Tokyo": (TOKYO, "Asia/Tokyo"),
                     "America/Los_Angeles": (LOS_ANGELES, "America/Los_Angeles")}
        cls.by_zone = {name: _scan(cls.root, tz) for name, (tz, _) in cls.zones.items()}

    def zone(self, name):
        return self.zones[name]

    def test_bucket_zone_is_a_column_on_every_sheet(self):
        self.assertGreaterEqual(context_mod.EXPORT_SCHEMA_VERSION, 8)
        # Appended last in schema 8, so no released column moved; only schema
        # 9's, 10's and 11's columns come after it.
        after_zone = {"output_tokens_imputed", "est_cost_unlogged_usd", "very_long_context_cost_usd",
                      "week_active_days"}
        for kind, (columns, _) in export_mod.KINDS.items():
            with self.subTest(kind):
                after = columns[columns.index("bucket_zone") + 1:]
                self.assertTrue(set(after) <= after_zone, after)
                self.assertEqual(columns.count("bucket_zone"), 1)
        self.assertEqual(export_mod.CONTEXT_COLUMNS[-1], "bucket_zone")

    def test_bucket_zone_and_timezone_describe_the_zone_not_the_machine(self):
        for name, offset in (("UTC", "+00:00"), ("Asia/Tokyo", "+09:00"), ("America/Los_Angeles", "-07:00")):
            with self.subTest(name):
                ctx = export_mod.build_context(self.by_zone[name], PERSON, now=NOW, zone=self.zone(name))
                self.assertEqual(ctx["bucket_zone"], name)
                self.assertEqual(ctx["timezone"], offset)
                self.assertEqual(ctx["schema_version"], context_mod.EXPORT_SCHEMA_VERSION)
                self.assertTrue(ctx["exported_at"].endswith(offset), ctx["exported_at"])

    @needs_zone_data
    def test_the_timezone_column_is_the_offset_at_export_time(self):
        winter = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        zone = context_mod.export_zone("America/Los_Angeles")
        ctx = export_mod.build_context(self.by_zone["America/Los_Angeles"], PERSON, now=winter, zone=zone)
        self.assertEqual((ctx["timezone"], ctx["bucket_zone"]), ("-08:00", "America/Los_Angeles"))

    def test_with_no_zone_given_exports_use_utc(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
            ctx = export_mod.build_context(self.by_zone["UTC"], PERSON, now=NOW)
        self.assertEqual((ctx["bucket_zone"], ctx["timezone"]), ("UTC", "+00:00"))

    def test_weeks_follow_the_export_zone(self):
        def weeks(name):
            rows = _sheet(self.by_zone[name], "summary", self.zone(name))
            return sorted({r["week_start"] for r in rows if r["project"]})
        self.assertEqual(weeks("UTC"), ["2026-03-02", "2026-03-09"])
        self.assertEqual(weeks("America/Los_Angeles"), ["2026-03-02", "2026-03-09"])
        # Monday 08:30 in Tokyo: the Sunday-night request is in the next week.
        self.assertEqual(weeks("Asia/Tokyo"), ["2026-03-09"])

    def test_the_breakdown_and_tools_sheets_agree(self):
        for kind in ("breakdown", "tools"):
            with self.subTest(kind):
                utc = {r["week_start"] for r in _sheet(self.by_zone["UTC"], kind, self.zone("UTC"))}
                tokyo = {r["week_start"] for r in _sheet(self.by_zone["Asia/Tokyo"], kind, self.zone("Asia/Tokyo"))}
                self.assertEqual(utc, {"2026-03-02", "2026-03-09"})
                self.assertEqual(tokyo, {"2026-03-09"})

    def test_session_times_are_written_in_the_zone(self):
        rows = {r["started_at"][:10]: r for r in
                _sheet(self.by_zone["Asia/Tokyo"], "sessions", self.zone("Asia/Tokyo"))}
        self.assertIn("2026-03-09T08:30:00+09:00", {r["started_at"] for r in rows.values()})
        self.assertIn("2026-03-09T09:30:00+09:00", {r["ended_at"] for r in rows.values()})
        la = _sheet(self.by_zone["America/Los_Angeles"], "sessions", self.zone("America/Los_Angeles"))
        self.assertIn("2026-03-08T16:30:00-07:00", {r["started_at"] for r in la})
        utc = _sheet(self.by_zone["UTC"], "sessions", self.zone("UTC"))
        self.assertIn("2026-03-08T23:30:00+00:00", {r["started_at"] for r in utc})

    def test_since_and_until_are_days_in_the_export_zone(self):
        def requests(name, since=None, until=None):
            rows = _sheet(self.by_zone[name], "summary", self.zone(name), since=since, until=until)
            return sum(int(r["requests"] or 0) for r in rows)
        self.assertEqual(requests("UTC"), 3)
        # 2026-03-09 starts at 15:00Z on the 8th in Tokyo, so the first request (23:30Z) is in.
        self.assertEqual(requests("Asia/Tokyo", since="2026-03-09", until="2026-03-09"), 2)
        # ...and in UTC the same days hold only the second one.
        self.assertEqual(requests("UTC", since="2026-03-09", until="2026-03-09"), 1)

    def test_prs_and_limits_are_dated_and_filtered_in_the_zone(self):
        for name, day in (("UTC", "2026-03-08"), ("Asia/Tokyo", "2026-03-09")):
            with self.subTest(name):
                zone, payload = self.zone(name), self.by_zone[name]
                self.assertEqual([r["opened_on"] for r in _sheet(payload, "prs", zone)], [day])
                # The limit was hit at 23:45Z on the 8th: that day in UTC, the 9th in Tokyo.
                self.assertEqual(len(_sheet(payload, "limits", zone, since=day, until=day)), 1)
                other = "2026-03-09" if day == "2026-03-08" else "2026-03-08"
                self.assertEqual(len(_sheet(payload, "limits", zone, since=other, until=other)), 0)
                # sess-x starts on `day` in this zone (sess-y on the 12th).
                self.assertEqual(len(_sheet(payload, "sessions", zone, since=day, until=day)), 1)
                self.assertEqual(len(_sheet(payload, "sessions", zone, since=other, until=other)), 0)

    def test_today_and_the_window_cap_are_the_zone_date(self):
        # 2026-03-08 20:00 in New York is already 2026-03-09 in Tokyo and UTC.
        late = datetime.datetime(2026, 3, 8, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
        for name, today in (("Asia/Tokyo", "2026-03-09"), ("America/Los_Angeles", "2026-03-08")):
            with self.subTest(name):
                ctx = export_mod.build_context(self.by_zone[name], PERSON, until="2030-01-01",
                                               now=late, zone=self.zone(name))
                self.assertEqual(ctx["window_to"], today)
        ctx = export_mod.build_context(self.by_zone["UTC"], PERSON, until="2030-01-01", now=late,
                                       zone=self.zone("UTC"))
        self.assertEqual(ctx["window_to"], "2026-03-09")

    def test_person_active_days_are_counted_in_the_zone(self):
        for name, days in (("UTC", 3), ("Asia/Tokyo", 2)):
            with self.subTest(name):
                ctx = export_mod.build_context(self.by_zone[name], PERSON, now=NOW, zone=self.zone(name))
                self.assertEqual(ctx["person_active_days"], days)

    def test_filenames_and_the_default_stamp_use_the_zone_date(self):
        stamp = export_mod.zip_filename_for(PERSON, zone=self.zone("Asia/Tokyo"), now=datetime.datetime(
            2026, 3, 8, 20, 0, tzinfo=UTC))
        self.assertTrue(stamp.endswith("20260309.zip"), stamp)

    def test_the_machines_zone_never_matters(self):
        # The same logs exported by a machine in Tokyo and one in Los Angeles,
        # both for zone UTC, are the same files (but for exported_at).
        if not hasattr(time, "tzset"):
            self.skipTest("needs time.tzset to move the machine's zone")
        # The named zone is read from the machine's own zone files too.
        outputs = []
        for machine in ("Asia/Tokyo", "America/Los_Angeles"):
            with machine_zone(machine):
                zone = context_mod.export_zone("UTC")
                payload = _scan(self.root, zone[0])
                out = tempfile.mkdtemp()
                export_mod.export(payload, out_dir=out, person=PERSON, stamp="20260320", zone=zone)
                files = {}
                for name in sorted(os.listdir(out)):
                    with open(os.path.join(out, name), newline="", encoding="utf-8") as fh:
                        rows = list(csv.reader(fh))
                    col = rows[0].index("exported_at")
                    for r in rows[1:]:
                        r[col] = "<now>"
                    files[name] = rows
                outputs.append(files)
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(outputs[0])

    def test_an_export_never_reads_the_machines_zone(self):
        # The same property as the tzset test, runnable everywhere (Windows has
        # no tzset): every machine-local conversion is made to fail, so scanning
        # and exporting for a zone can only have used that zone.
        def guard(real, pos):
            def wrapped(*args, **kwargs):
                tz = kwargs["tz"] if "tz" in kwargs else (args[pos] if len(args) > pos else None)
                if tz is None:
                    raise AssertionError(f"{real.__name__} read the machine's zone")
                return real(*args, **kwargs)
            return wrapped
        real_now = context_mod.now_in
        def now_in(tz=None):
            if tz is None:
                raise AssertionError("now_in read the machine's zone")
            return real_now(tz)
        with mock.patch.object(aggregate, "_local_date", guard(aggregate._local_date, 1)), \
                mock.patch.object(aggregate, "_local_bucket", guard(aggregate._local_bucket, 2)), \
                mock.patch.object(context_mod, "_local_day", guard(context_mod._local_day, 1)), \
                mock.patch.object(export_mod, "_local_time", guard(export_mod._local_time, 1)), \
                mock.patch.object(context_mod, "now_in", now_in), \
                mock.patch.object(scan_mod, "now_in", now_in):
            for name in ("UTC", "Asia/Tokyo"):
                zone = self.zone(name)
                payload = _scan(self.root, zone[0])
                ctx = export_mod.build_context(payload, PERSON, now=NOW, zone=zone)
                for kind in export_mod.KINDS:
                    export_mod.csv_text(payload, kind, PERSON, ctx=ctx)
                with tempfile.TemporaryDirectory() as out:
                    export_mod.export(payload, out_dir=out, person=PERSON, zone=zone, now=NOW)
        # ...and the output for one zone is one thing, however it was reached.
        first = _sheet(self.by_zone["Asia/Tokyo"], "summary", self.zone("Asia/Tokyo"))
        again = _sheet(_scan(self.root, TOKYO), "summary", self.zone("Asia/Tokyo"))
        self.assertEqual(first, again)

    @needs_zone_data
    def test_with_the_machines_own_zone_it_is_todays_export_plus_the_new_column(self):
        if not hasattr(time, "tzset"):
            self.skipTest("needs time.tzset to pin the machine's zone")
        with machine_zone("America/New_York"):
            legacy_payload = _scan(self.root, None)
            legacy_ctx = export_mod.build_context(legacy_payload, PERSON, now=NOW,
                                                  zone=(None, "machine-local"))
            zone = context_mod.export_zone("America/New_York")
            new_payload = _scan(self.root, zone[0])
            new_ctx = export_mod.build_context(new_payload, PERSON, now=NOW, zone=zone)
            for kind, (columns, _) in export_mod.KINDS.items():
                with self.subTest(kind):
                    old = _rows(export_mod.csv_text(legacy_payload, kind, PERSON, ctx=legacy_ctx))
                    new = _rows(export_mod.csv_text(new_payload, kind, PERSON, ctx=new_ctx))
                    self.assertTrue(new)
                    for rows in (old, new):
                        for r in rows:
                            r.pop("bucket_zone")
                    self.assertEqual(new, old)
        self.assertEqual(new_ctx["timezone"], "-04:00")


# --------------------------------------------------------------------------- #
# dashboard stays local
# --------------------------------------------------------------------------- #

class TestDashboardStaysLocal(_Logs):
    def test_the_export_zone_never_changes_the_dashboard_payload(self):
        def local():
            p = _scan(self.root)
            for k in ("scan_ms", "generated_at", "files_parsed", "files_cached"):
                p.pop(k)
            return p
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
            plain = local()
            os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Pacific/Kiritimati"
            self.assertEqual(local(), plain)

    def _run_collect(self, *args):
        out = tempfile.mkdtemp()
        data = os.path.join(tempfile.mkdtemp(), "data.json")
        cache_path = os.path.join(tempfile.mkdtemp(), "parsed.json")
        argv = ["collect.py", "--offline", *args]
        built = []
        with mock.patch.object(collect, "DEFAULT_ROOT", self.root), \
                mock.patch.object(collect, "OUT_PATH", __import__("pathlib").Path(data)), \
                mock.patch.object(scan_mod, "DEFAULT_CACHE_PATH", cache_path), \
                mock.patch.object(collect, "_build_static", side_effect=lambda p: built.append(p) or True), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            code = collect.main()
        with open(data, encoding="utf-8") as fh:
            return code, json.load(fh), built, out

    @needs_zone_data
    def test_collect_exports_in_the_zone_and_keeps_data_json_and_static_local(self):
        out = tempfile.mkdtemp()
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
            local = _scan(self.root)
            code, data, built, _ = self._run_collect("--export", out, "--tz", "Pacific/Kiritimati",
                                                     "--static", "--as", "a@b.c")
        self.assertEqual(code, 0)
        self.assertEqual(data["usage"], local["usage"])
        self.assertEqual([s["start_day"] for s in data["sessions"]], [s["start_day"] for s in local["sessions"]])
        self.assertEqual(built[0]["usage"], local["usage"])
        sheet = next(f for f in os.listdir(out) if "-sessions-" in f)
        with open(os.path.join(out, sheet), newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual({r["bucket_zone"] for r in rows}, {"Pacific/Kiritimati"})
        self.assertEqual({r["timezone"] for r in rows}, {"+14:00"})
        self.assertIn("2026-03-09T13:30:00+14:00", {r["started_at"] for r in rows})

    def test_collect_reads_the_env_var_and_defaults_to_utc(self):
        cases = [(None, "UTC")] + ([("Asia/Tokyo", "Asia/Tokyo")] if _have_zone_data() else [])
        for env, label in cases:
            with self.subTest(env=env):
                out = tempfile.mkdtemp()
                with mock.patch.dict(os.environ):
                    os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
                    if env:
                        os.environ["CLAUDE_USAGE_EXPORT_TZ"] = env
                    self._run_collect("--export", out, "--as", "a@b.c")
                sheet = next(f for f in os.listdir(out) if "-summary-" in f)
                with open(os.path.join(out, sheet), newline="", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
                self.assertEqual({r["bucket_zone"] for r in rows}, {label})

    def test_the_history_note_comes_from_the_payload_being_exported(self):
        # An export's cli_history_* is read in the export zone, so the note
        # collect.py prints about it must come from that payload, not the local one.
        real = collect.scan

        def fake(root, force=False, tz=None, **kw):
            payload = real(root, force=force, tz=tz, cache=FileCache(os.path.join(tempfile.mkdtemp(), "c.json")))
            payload["history"] = {"cli_sessions": 1, "retention_days_default": 30,
                                  "cli_history_truncated": tz is not None,
                                  "cli_history_from": "2026-02-01" if tz is not None else "2026-01-01"}
            return payload

        def run(*args):
            out = io.StringIO()
            with mock.patch.object(collect, "scan", fake), \
                    mock.patch.object(collect, "DEFAULT_ROOT", self.root), \
                    mock.patch.object(collect, "OUT_PATH", __import__("pathlib").Path(tempfile.mkdtemp()) / "d.json"), \
                    mock.patch.object(sys, "argv", ["collect.py", "--offline", *args]), \
                    contextlib.redirect_stdout(out):
                collect.main()
            return out.getvalue()
        exporting = run("--export", tempfile.mkdtemp(), "--as", "a@b.c")
        self.assertIn("CLI history starts 2026-02-01", exporting)
        # Not exporting: the dashboard's own (local) payload decides, as before.
        self.assertNotIn("CLI history starts", run())

    def test_a_bad_zone_is_a_clear_error_before_anything_is_scanned(self):
        for args in (["--export", "x", "--tz", "Mars/Phobos"],):
            with mock.patch.object(sys, "argv", ["collect.py", "--offline", *args]), \
                    mock.patch.object(collect, "scan", side_effect=AssertionError("scanned")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    collect.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Mars/Phobos", err.getvalue())

    def test_a_bad_env_var_is_a_clear_error_for_an_export(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Mars/Phobos"}), \
                mock.patch.object(sys, "argv", ["collect.py", "--offline", "--export", "x"]), \
                mock.patch.object(collect, "scan", side_effect=AssertionError("scanned")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                collect.main()
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", err.getvalue())

    def test_tz_without_export_is_an_error(self):
        with mock.patch.object(sys, "argv", ["collect.py", "--offline", "--tz", "UTC"]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                collect.main()
        self.assertIn("--export", err.getvalue())


class TestServeExport(_Logs):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._orig = (serve.DEFAULT_ROOT, serve._cache)
        serve.DEFAULT_ROOT = cls.root
        cls.tmpdir = tempfile.mkdtemp()
        serve._cache = FileCache(os.path.join(cls.tmpdir, "parsed.json"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_port
        serve.Handler.port = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        serve.DEFAULT_ROOT, serve._cache = cls._orig
        super().tearDownClass()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def _zip_rows(self, kind):
        status, body = self._get("/api/export/all.zip")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            name = next(n for n in archive.namelist() if f"-{kind}-" in n)
            return _rows(archive.read(name).decode("utf-8"))

    def test_the_zip_is_in_utc_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
            utc = self._zip_rows("summary")
        self.assertEqual({r["bucket_zone"] for r in utc}, {"UTC"})
        self.assertEqual({r["week_start"] for r in utc if r["project"]}, {"2026-03-02", "2026-03-09"})

    @needs_zone_data
    def test_the_zip_uses_the_export_zone(self):
        with mock.patch.dict(os.environ):
            os.environ["CLAUDE_USAGE_EXPORT_TZ"] = "Asia/Tokyo"
            tokyo = self._zip_rows("summary")
            tokyo_sessions = self._zip_rows("sessions")
        self.assertEqual({r["bucket_zone"] for r in tokyo}, {"Asia/Tokyo"})
        self.assertEqual({r["week_start"] for r in tokyo if r["project"]}, {"2026-03-09"})
        self.assertIn("2026-03-09T08:30:00+09:00", {r["started_at"] for r in tokyo_sessions})

    @needs_zone_data
    def test_a_single_csv_uses_it_too(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Asia/Tokyo"}):
            status, body = self._get("/api/export/summary.csv")
        self.assertEqual(status, 200)
        self.assertEqual({r["bucket_zone"] for r in _rows(body.decode("utf-8"))}, {"Asia/Tokyo"})

    def test_api_data_stays_machine_local_whatever_the_export_zone(self):
        local = _scan(self.root)
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Pacific/Kiritimati"}):  # never read here
            status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"], local["usage"])

    def test_api_data_says_which_zone_exports_use(self):
        # The dashboard shows local days but its Export button buckets in the
        # export zone; the page needs the label to say so next to the button.
        for env, label in ((None, "UTC"), ("Z", "UTC")):
            with self.subTest(env=env), mock.patch.dict(os.environ):
                os.environ.pop("CLAUDE_USAGE_EXPORT_TZ", None)
                if env:
                    os.environ["CLAUDE_USAGE_EXPORT_TZ"] = env
                status, body = self._get("/api/data")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["export_zone"], label)

    @needs_zone_data
    def test_api_data_names_a_set_zone(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "asia/tokyo"}):
            status, body = self._get("/api/data")
        self.assertEqual(json.loads(body)["export_zone"], "Asia/Tokyo")

    def test_api_data_still_loads_when_the_export_zone_is_bad(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Mars/Phobos"}):
            status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["export_zone"])

    def _keep_handler_state(self):
        # main() sets the handler's port and team folders; the other tests need the real ones.
        saved = (serve.Handler.port, serve.Handler.team_paths, serve.Handler.seat_prices)
        self.addCleanup(lambda: (setattr(serve.Handler, "port", saved[0]),
                                 setattr(serve.Handler, "team_paths", saved[1]),
                                 setattr(serve.Handler, "seat_prices", saved[2])))

    def test_a_bad_env_var_stops_the_server_at_startup(self):
        self._keep_handler_state()
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Mars/Phobos"}), \
                mock.patch.object(sys, "argv", ["serve.py", "--no-open", "--offline", "--port", "0"]), \
                mock.patch.object(serve, "ThreadingHTTPServer", side_effect=AssertionError("started")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                serve.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("Mars/Phobos", err.getvalue())
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", err.getvalue())

    def test_team_mode_does_not_care_about_the_export_zone(self):
        self._keep_handler_state()
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Mars/Phobos"}), \
                mock.patch.object(sys, "argv", ["serve.py", "--no-open", "--offline", "--port", "0",
                                                "--team", self.tmpdir]), \
                mock.patch.object(serve, "ThreadingHTTPServer", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                serve.main()

    def test_a_bad_env_var_answers_with_an_error_not_a_default(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_EXPORT_TZ": "Mars/Phobos"}), \
                contextlib.redirect_stderr(io.StringIO()):
            status, body = self._get("/api/export/all.zip")
        self.assertEqual(status, 500)
        self.assertIn("Mars/Phobos", body.decode("utf-8"))


class TestExportButtonSaysWhichZone(unittest.TestCase):
    """The dashboard shows machine-local days, but the Export button's files are
    bucketed in the export zone. The button has to say so."""

    def _run(self, payload):
        return run_js(["static/habits.js", "static/app.js"],
                      f"PAYLOAD = {json.dumps(payload)}; wireExport();"
                      "out({title: el('exportBtn').title, note: el('exportNote').innerHTML,"
                      " noteTitle: el('exportNote').title, hidden: el('exportNote').hidden});")

    def test_the_note_and_tooltip_name_the_zone(self):
        out = self._run({"export_zone": "Asia/Tokyo"})
        self.assertIn("Asia/Tokyo", out["note"])
        self.assertIn("Asia/Tokyo", out["title"])
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", out["title"])
        self.assertIn("your dashboard shows local days", out["title"])
        self.assertIn("CLAUDE_USAGE_EXPORT_TZ", out["noteTitle"])
        self.assertFalse(out["hidden"])

    def test_the_zone_is_escaped(self):
        out = self._run({"export_zone": "<img src=x onerror=alert(1)>"})
        self.assertNotIn("<img", out["note"])
        self.assertIn("&lt;img", out["note"])

    def test_no_zone_means_no_claim(self):
        # A server whose export zone is bad (or an older one) reports none.
        out = self._run({"export_zone": None})
        self.assertNotIn("local days", out["title"])
        self.assertTrue(out["hidden"])

    def test_the_standalone_build_has_no_export_button(self):
        out = run_js(["static/habits.js", "static/app.js"], "SERVED_BY_DASHBOARD = false; PAYLOAD = {export_zone: 'UTC'}; wireExport();"
                                       "out({hidden: el('exportBtn').hidden});")
        self.assertTrue(out["hidden"])


if __name__ == "__main__":
    unittest.main()
