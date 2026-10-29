"""Lines added/removed come from the CLI's cost-state, a running counter for the
whole session. Counting a session's final total in every date range it touched
put a six-week session's 78k lines into the last 7 days as well as into 30, 90
and All. These tests pin each step of the counter to when it was recorded, so a
date range gets only the lines written inside it."""
import datetime
import json
import os
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.jsrun import run_js


def _req(sid, ts, rid):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "cwd": "/Users/x/proj",
            "requestId": rid, "message": {"id": "m-" + rid, "role": "assistant",
            "model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 20}}}


def _state(sid, start, cost, added, removed):
    # Real cost-state records carry no timestamp of their own.
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": cost,
            "totalLinesAdded": added, "totalLinesRemoved": removed, "startTime": start}


def _scan(files):
    tmp = tempfile.TemporaryDirectory()
    folder = os.path.join(tmp.name, "-Users-x-proj")
    os.makedirs(folder)
    for sid, lines in files.items():
        with open(os.path.join(folder, sid + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
    # UTC: the test is about which day a step lands on, not the machine's zone.
    payload = scan(tmp.name, cache=FileCache(os.path.join(tmp.name, "c.json")), tz=datetime.timezone.utc)
    tmp.cleanup()
    return payload


class TestLinesByDate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _scan({
            # One long session: 100 lines by Mar 2, 250 by Mar 20.
            "long": [_req("long", "2026-03-02T12:00:00.000Z", "a"),
                     _state("long", 111, 1.0, 100, 1),
                     _req("long", "2026-03-20T12:00:00.000Z", "b"),
                     _state("long", 111, 2.0, 250, 5)],
            # A branch that carried on the same counter: its first snapshot
            # repeats the parent's, then it adds 30 lines on Mar 21.
            "branch": [_req("branch", "2026-03-21T12:00:00.000Z", "c"),
                       _state("branch", 111, 2.0, 250, 5),
                       _state("branch", 111, 2.1, 280, 5)],
        })
        cls.lines = cls.payload["lines"]

    def by_day(self):
        out = {}
        for bucket, _sid, added, removed in self.lines:
            a, r = out.get(bucket[:10], (0, 0))
            out[bucket[:10]] = (a + added, r + removed)
        return out

    def test_each_step_lands_on_the_day_it_was_recorded(self):
        self.assertEqual(self.by_day(), {"2026-03-02": (100, 1), "2026-03-20": (150, 4),
                                         "2026-03-21": (30, 0)})

    def test_steps_add_up_to_the_sessions_total(self):
        session = {s["id"]: s for s in self.payload["sessions"]}["long"]
        self.assertEqual(session["lines_added"], 280)
        self.assertEqual(sum(r[2] for r in self.lines), 280)
        self.assertEqual(sum(r[3] for r in self.lines), 5)

    def test_steps_sit_on_the_session_that_shows_the_counter(self):
        self.assertEqual({r[1] for r in self.lines}, {"long"})

    def test_pull_requests_come_newest_first(self):
        def pr(sid, ts, n):
            return {"type": "pr-link", "sessionId": sid, "timestamp": ts, "prNumber": n,
                    "prUrl": f"https://github.com/o/r/pull/{n}", "prRepository": "o/r"}
        payload = _scan({"s": [_req("s", "2026-03-02T09:00:00.000Z", "a"),
                               pr("s", "2026-03-02T10:00:00.000Z", 1),
                               pr("s", "2026-03-05T10:00:00.000Z", 3),
                               pr("s", "2026-03-02T15:00:00.000Z", 2)]})
        self.assertEqual([p["number"] for p in payload["prs"]], [3, 2, 1])

    def test_no_counter_no_rows(self):
        self.assertEqual(_scan({"plain": [_req("plain", "2026-03-02T12:00:00.000Z", "z")]})["lines"], [])


DASHBOARD = ["static/filters.js", "static/habits.js", "static/app.js"]


def _env(lines, prs=(), frm="2026-03-15", to="2026-03-31"):
    session = {"id": "long", "project": "p", "models": ["m"], "start": "2026-03-02T12:00:00Z",
               "end": "2026-03-21T12:00:00Z", "derived": False, "title": "Long one",
               "lines_added": 280, "lines_removed": 5, "entrypoints": {}, "versions": {},
               "branches": {}, "permission_modes": {}}
    payload = {"summary": {"date_max": to}, "usage": [], "tools": [], "sessions": [session],
               "lines": lines, "prs": list(prs)}
    return (f"PAYLOAD = {json.dumps(payload)};"
            "sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));"
            f"state.from = '{frm}'; state.to = '{to}'; renderEnv();")


LINES = [["2026-03-02T05:00", "long", 100, 1], ["2026-03-20T05:00", "long", 150, 4],
         ["2026-03-21T05:00", "long", 30, 0]]


class TestEnvTab(unittest.TestCase):
    def test_tile_counts_only_lines_written_in_the_range(self):
        html = run_js(DASHBOARD, _env(LINES) + "out(el('envStatTiles').innerHTML)")
        self.assertIn("+180", html)
        self.assertIn("-4", html)

    def test_a_wider_range_adds_the_earlier_lines(self):
        html = run_js(DASHBOARD, _env(LINES, frm="2026-03-01") + "out(el('envStatTiles').innerHTML)")
        self.assertIn("+280", html)

    def test_tile_says_when_line_counts_start(self):
        # The date is written in the reader's own locale ("Mar 2, 2026" in the
        # US, "2 Mar 2026" in the UK), so the expected text is formatted the
        # same way rather than spelled out.
        html, first = run_js(DASHBOARD, _env(LINES, frm="2026-02-01")
                             + "out([el('envStatTiles').innerHTML, prettyDate('2026-03-02')])")
        self.assertIn("2026", first)
        self.assertIn(f"recorded from {first}", html)

    def test_pull_requests_newest_first(self):
        prs = [{"session_id": "long", "date": d, "repo": "o/r", "number": n, "url": f"https://x/{n}"}
               for d, n in (("2026-03-16", 1), ("2026-03-30", 3), ("2026-03-20", 2))]
        html = run_js(DASHBOARD, _env(LINES, prs) + "out(el('#prsTable tbody').innerHTML)")
        self.assertLess(html.index("#3"), html.index("#2"))
        self.assertLess(html.index("#2"), html.index("#1"))


if __name__ == "__main__":
    unittest.main()
