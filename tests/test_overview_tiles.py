"""The Overview's pinned tiles: each figure says what it is, and none claims more
than the data supports. Found by an independent recomputation on real logs."""
import json
import os
import tempfile
import unittest

from claude_usage.parser import parse_file
from tests.jsrun import run_js

DASHBOARD = ["static/filters.js", "static/habits.js", "static/app.js"]


def _row(day, sid, cost, cache_read=0, thinking=0, thinking_unlogged=0):
    # date, session, model, skill, agent type, input, output, cache read,
    # 5m, 1h, thinking, web search, requests, cost, web fetch, cache saved,
    # imputed output, responses without a thinking count
    return [f"{day}T10:00", sid, "claude-opus-5-5", "(none)", -1, 10, 100, cache_read,
            0, 0, thinking, 0, 1, cost, 0, 0, 0, thinking_unlogged]


def _session(sid, start, end, requests=1, unlogged=None):
    return {"id": sid, "project": "p", "models": ["claude-opus-5-5"], "derived": False,
            "start": f"{start}T09:00:00Z", "end": f"{end}T18:00:00Z",
            "start_day": start, "end_day": end, "requests": requests,
            "est_cost_unlogged": unlogged, "title": sid, "entrypoints": {}, "versions": {},
            "branches": {}, "permission_modes": {}}


def _tiles(rows, sessions, frm="2026-10-25", to="2026-10-29"):
    payload = {"summary": {"date_max": to}, "usage": rows, "tools": [], "sessions": sessions,
               "lines": [], "prs": []}
    snippet = (f"PAYLOAD = {json.dumps(payload)};"
               "sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));"
               f"state.from = '{frm}'; state.to = '{to}'; renderGlobalStats();"
               "out(el('statTiles').innerHTML)")
    return run_js(DASHBOARD, snippet)


class TestTiles(unittest.TestCase):
    def test_a_transcript_with_no_requests_is_not_a_session(self):
        html = _tiles([_row("2026-10-26", "a", 1.0)],
                      [_session("a", "2026-10-26", "2026-10-26"),
                       _session("empty", "2026-10-26", "2026-10-26", requests=0)])
        self.assertRegex(html, r"Sessions</div>\s*<div[^>]*>1<")

    def test_concentration_waits_for_more_than_five_sessions(self):
        ids = list("abcde")
        html = _tiles([_row("2026-10-26", s, 1.0) for s in ids],
                      [_session(s, "2026-10-26", "2026-10-26") for s in ids])
        self.assertIn("needs more than 5 sessions with spend (5 here)", html)
        self.assertNotIn("100.0%", html)

    def test_concentration_shows_with_six(self):
        ids = list("abcdef")
        html = _tiles([_row("2026-10-26", s, 1.0) for s in ids],
                      [_session(s, "2026-10-26", "2026-10-26") for s in ids])
        self.assertIn("top 5 of 6 sessions with spend", html)

    def test_a_counter_running_past_the_dates_is_not_credited_to_them(self):
        # "long" ran from September; its whole shortfall can't be put on the
        # last five days.
        html = _tiles([_row("2026-10-26", "short", 1.0), _row("2026-10-27", "long", 1.0)],
                      [_session("short", "2026-10-26", "2026-10-26", unlogged=0.5),
                       _session("long", "2026-09-23", "2026-10-27", unlogged=4.4)])
        self.assertIn("+ $0.50 not in transcripts (1 session with a counter; 1 more run past these dates)",
                      html)

    def test_thinking_is_a_floor_when_responses_did_not_log_it(self):
        html = _tiles([_row("2026-10-26", "a", 1.0, thinking=500, thinking_unlogged=3)],
                      [_session("a", "2026-10-26", "2026-10-26")])
        self.assertIn("at least · 3 responses didn&#39;t log it", html.replace("didn't", "didn&#39;t"))

    def test_billions_have_their_own_unit(self):
        html = _tiles([_row("2026-10-26", "a", 1.0, cache_read=12_534_520_000)],
                      [_session("a", "2026-10-26", "2026-10-26")])
        self.assertIn("12.53B", html)
        self.assertNotIn("12534", html)

    def test_total_tokens_says_how_much_is_not_cache_reads(self):
        html = _tiles([_row("2026-10-26", "a", 1.0, cache_read=1_000_000)],
                      [_session("a", "2026-10-26", "2026-10-26")])
        self.assertIn("110 excl. cache reads", html)


class TestToolCallTimes(unittest.TestCase):
    def test_a_timestamp_less_repeat_keeps_the_calls_real_time(self):
        # Claude Code's api-request-blob records repeat a turn's tool calls with
        # no timestamp; the call must keep the time of the reply that made it.
        call = {"type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {}}
        reply = {"type": "assistant", "timestamp": "2026-10-28T10:00:00.000Z", "uuid": "u1",
                 "requestId": "req-1", "message": {"id": "m1", "model": "claude-opus-5-5",
                 "content": [call], "usage": {"input_tokens": 1, "output_tokens": 1}}}
        blob = {"type": "api-request-blob", "message": {"role": "assistant", "content": [call]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(reply) + "\n" + json.dumps(blob) + "\n")
            facts = parse_file(path, "main")
        self.assertEqual(facts.tool_ts["toolu_1"], "2026-10-28T10:00:00.000Z")

    def test_a_response_without_a_thinking_count_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            recs = [{"type": "assistant", "timestamp": "2026-10-28T10:00:00.000Z", "uuid": f"u{i}",
                     "requestId": f"r{i}", "message": {"id": f"m{i}", "model": "claude-opus-5-5",
                     "content": [], "usage": {"input_tokens": 1, "output_tokens": 5, **extra}}}
                    for i, extra in enumerate([{"output_tokens_details": {"thinking_tokens": 2}}, {}])]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
            facts = parse_file(path, "main")
        self.assertEqual([r["thinking_logged"] for r in facts.requests], [True, False])


if __name__ == "__main__":
    unittest.main()
