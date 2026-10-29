"""A shared static dashboard is built in one timezone and opened in another.

Usage rows are bucketed by the BUILD machine's local day; the Sessions table,
habits, limits and the weekly version chart used to decide a session's day in
the VIEWER's timezone, so the cost tiles and the tables disagreed. The payload
now carries the build machine's day on sessions and limit events
(`start_day`, `end_day`, `hit_day`), and the page filters on those.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.jsrun import REPO, run_js

DASHBOARD = ["static/habits.js", "static/app.js"]
BUILD_TZ = "America/Los_Angeles"   # UTC-7 on the dates below
VIEW_TZ = "Asia/Tokyo"             # UTC+9
DAY = "2026-10-06"                 # the local day in the build zone that is asked for

# Three sessions straddling local midnight on 2026-10-06 in Los Angeles.
#   sess-a  LA 10-05 23:30 .. 10-06 00:30   (starts the day before, ends on DAY)
#   sess-b  LA 10-06 23:30 .. 10-07 00:30   (starts on DAY, ends the day after)
#   sess-c  LA 10-06 13:00                  (all inside DAY)
# In Tokyo, sess-a starts on 10-06 but sess-b and sess-c both start on 10-07.
LOGS = {
    "sess-a": ["2026-10-06T06:30:00.000Z", "2026-10-06T07:30:00.000Z"],
    "sess-b": ["2026-10-07T06:30:00.000Z", "2026-10-07T07:30:00.000Z"],
    "sess-c": ["2026-10-06T20:00:00.000Z"],
}
# A weekly limit hit at LA 10-06 23:45 (Tokyo 10-07 15:45), inside sess-b.
LIMIT_TS = "2026-10-07T06:45:00.000Z"

_BUILD = r"""
import json, os, sys, tempfile
from claude_usage.cache import FileCache
from claude_usage.scan import scan
root = sys.argv[1]
print(json.dumps(scan(root, cache=FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json")))))
"""


def _response(sid, n, ts, out):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/tz",
            "entrypoint": "cli", "requestId": f"r-{sid}-{n}",
            "message": {"id": f"m-{sid}-{n}", "role": "assistant", "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 1000, "output_tokens": out}}}


def _write_logs(root):
    proj = os.path.join(root, "-Users-x-tz")
    os.makedirs(proj)
    for i, (sid, stamps) in enumerate(LOGS.items()):
        records = [_response(sid, n, ts, 100000 * (i + 1) * (n + 1)) for n, ts in enumerate(stamps)]
        if sid == "sess-b":
            records.append({
                "type": "assistant", "sessionId": sid, "timestamp": LIMIT_TS, "cwd": "/Users/x/tz",
                "uuid": "n-weekly", "isApiErrorMessage": True, "error": "rate_limit",
                "apiErrorStatus": 429,
                "message": {"id": "n-n-weekly", "role": "assistant", "model": "<synthetic>",
                            "content": [{"type": "text",
                                         "text": "You've hit your weekly limit · resets Oct 13 at 9am (UTC)"}]}})
        records.sort(key=lambda r: r["timestamp"])
        with open(os.path.join(proj, sid + ".jsonl"), "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")


def build_payload(tz=BUILD_TZ):
    root = tempfile.mkdtemp()
    _write_logs(root)
    proc = subprocess.run([sys.executable, "-c", _BUILD, root], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", timeout=120, env={**os.environ, "TZ": tz})
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


_VIEW = """
PAYLOAD = %s;
sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));
state.from = '%s'; state.to = '%s'; state.showForks = true;
const rows = usageRows();
const w = {from: state.from, to: state.to};
const tableRows = sessionRowsWithCost();
out({
  tile: rows.reduce((s, r) => s + r[13], 0),
  table: tableRows.reduce((s, s2) => s + s2.est_total, 0),
  tableIds: tableRows.map(s => s.id).sort(),
  habitIds: habitSessions(w).map(s => s.id).sort(),
  limitDays: limitRows().map(e => e.hit_day || null),
  limitCount: limitRows().length,
  reconSessions: reconciliationRows().reduce((n, r) => n + r.sessions, 0),
});
"""


def view(payload, tz, day=DAY):
    snippet = _VIEW % (json.dumps(payload), day, day)
    with mock.patch.dict(os.environ, {"TZ": tz}):
        return run_js(DASHBOARD, snippet)


@unittest.skipUnless(hasattr(time, "tzset"), "needs TZ support to pin the local zone")
class TestStaticDashboardUsesBuildDays(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = build_payload()

    def _legacy(self):
        """The payload as an older build wrote it: no build-zone days."""
        legacy = json.loads(json.dumps(self.payload))
        for s in legacy["sessions"]:
            s.pop("start_day", None), s.pop("end_day", None)
        for e in legacy["limits"]:
            e.pop("hit_day", None)
        return legacy

    def test_payload_carries_the_build_machines_days(self):
        days = {s["session_id"]: (s["start_day"], s["end_day"]) for s in self.payload["sessions"]}
        self.assertEqual(days, {"sess-a": ("2026-10-05", "2026-10-06"),
                                "sess-b": ("2026-10-06", "2026-10-07"),
                                "sess-c": ("2026-10-06", "2026-10-06")})
        self.assertEqual([e["hit_day"] for e in self.payload["limits"]], ["2026-10-06"])

    def test_viewer_in_another_zone_sees_tile_and_table_agree(self):
        out = view(self.payload, VIEW_TZ)
        self.assertGreater(out["tile"], 0)
        self.assertAlmostEqual(out["table"], out["tile"], places=6)
        self.assertEqual(out["tableIds"], ["sess-a", "sess-b", "sess-c"])

    def test_viewer_in_another_zone_gets_the_same_habits_and_limits(self):
        out = view(self.payload, VIEW_TZ)
        # Habits count sessions by the day they ended: sess-a and sess-c end on DAY.
        self.assertEqual(out["habitIds"], ["sess-a", "sess-c"])
        self.assertEqual((out["limitCount"], out["limitDays"]), (1, ["2026-10-06"]))

    def test_same_zone_view_is_unchanged(self):
        # Review Focus 4: the numbers a viewer in the build zone gets must be
        # exactly what they were before the payload carried the days.
        legacy = self._legacy()
        now, before = view(self.payload, BUILD_TZ), view(legacy, BUILD_TZ)
        before["limitDays"] = now["limitDays"]
        self.assertEqual(now, before)
        self.assertAlmostEqual(now["table"], now["tile"], places=6)
        self.assertEqual(now["tableIds"], ["sess-a", "sess-b", "sess-c"])

    def test_an_older_payload_without_the_fields_still_renders(self):
        legacy = self._legacy()
        out = view(legacy, BUILD_TZ)
        self.assertEqual(out["habitIds"], ["sess-a", "sess-c"])
        self.assertEqual(out["limitCount"], 1)


@unittest.skipUnless(hasattr(time, "tzset"), "needs TZ support to pin the local zone")
class TestVersionChartWeek(unittest.TestCase):
    def test_a_session_lands_in_the_build_machines_week(self):
        # Sunday 10-04 in Los Angeles is already Monday 10-05 in Tokyo, which
        # would put it in the next week's bar.
        session = {"id": "s1", "project": "p", "models": ["m"], "derived": False, "title": "t",
                   "start": "2026-10-05T03:00:00.000Z", "end": "2026-10-05T04:00:00.000Z",
                   "start_day": "2026-10-04", "end_day": "2026-10-04",
                   "versions": {"2.1.300": 1}, "permission_modes": {}, "entrypoints": {}}
        setup = (f"PAYLOAD = {json.dumps({'sessions': [session], 'prs': [], 'usage': []})};"
                 "sessionById = {s1: PAYLOAD.sessions[0]}; state.from = '2026-09-28'; state.to = '2026-10-11';")
        with mock.patch.dict(os.environ, {"TZ": VIEW_TZ}):
            labels = run_js(DASHBOARD, setup + "renderEnv(); out(charts['versions'].data.labels)")
        # One bar per week of the range, empty ones included (P3 OE-5); the
        # session is in the build machine's week.
        self.assertEqual(labels, ["2026-09-28", "2026-10-05"])


if __name__ == "__main__":
    unittest.main()
