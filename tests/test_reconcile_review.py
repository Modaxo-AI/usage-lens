"""The estimate-vs-Claude-Code panel compares like with like: per counter, the
requests the counter could have seen (the session's files, whoever won them in
the dedupe, from the counter's start to its last snapshot), and a column that
prices the counter's own tokens at the table's rates."""
import json
import os
import tempfile
import unittest

from claude_usage.aggregate import build_payload
from claude_usage.parser import parse_file
from claude_usage.pricing import cost_for
from tests.jsrun import run_js
from tests.test_health_ui import DASHBOARD, _dashboard, _session

MODEL = "claude-opus-5-5"
START_MS = 1792490400000  # 2026-10-20T10:00:00Z
USAGE = {"inputTokens": 100, "outputTokens": 1000, "cacheReadInputTokens": 50000,
         "cacheCreationInputTokens": 20000, "costUSD": 0.5}


def _reply(mid, ts, model=MODEL, inp=10, out=100):
    return {"type": "assistant", "timestamp": ts, "uuid": mid + "-u", "requestId": "req-" + mid,
            "message": {"id": mid, "model": model, "stop_reason": "end_turn", "content": [],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def _counter(start_ms, cost, usage, ts=None, model=MODEL):
    rec = {"type": "cost-state", "totalCostUSD": cost, "startTime": start_ms, "modelUsage": {model: usage}}
    if ts:
        rec["timestamp"] = ts
    return rec


def _cost(model, **usage):
    return cost_for(model, usage, summed=True)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name, records):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return parse_file(path, "main")

    def _payload(self, **files):
        return build_payload({sid: {"facts": f, "folder": "proj"} for sid, f in files.items()},
                             [], "2026-10-29T00:00:00+00:00")


class TestDerivedOriginal(_Base):
    def test_an_original_whose_requests_a_continuation_won_is_not_zero(self):
        # A holds 5 replies and the counter. B, a continuation without a counter,
        # copies all 5 and adds 3, so it wins them. A's counter saw its own 5.
        a = [_reply(f"m{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(5)]
        a.append(_counter(START_MS, 0.01, {"inputTokens": 50, "outputTokens": 500, "cacheReadInputTokens": 0,
                                           "cacheCreationInputTokens": 0, "costUSD": 0.01}))
        b = ([_reply(f"m{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(5)]
             + [_reply(f"n{i}", f"2026-10-20T11:{i:02d}:00.000Z") for i in range(3)])
        payload = self._payload(A=self._file("A.jsonl", a), B=self._file("B.jsonl", b))
        row, = payload["reconciliation"]
        self.assertEqual(row["est_tokens"], 5 * 110)
        self.assertEqual(row["cli_tokens"], 550)
        self.assertGreater(row["est_cost"], 0)
        a_row, = {s["id"]: s for s in payload["sessions"]}["A"]["cli_models"]
        self.assertEqual(a_row["est_tokens"], 550)


class TestCounterWindow(_Base):
    def test_a_late_starting_counter_compares_only_requests_since_its_start(self):
        # The session's first 4 requests precede the counter's start (a resume
        # that reset it); the counter saw only the last 6.
        early = [_reply(f"e{i}", f"2026-10-20T09:{i:02d}:00.000Z") for i in range(4)]
        late = [_reply(f"l{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(1, 7)]
        recs = early + late + [_counter(START_MS, 0.01, {
            "inputTokens": 60, "outputTokens": 600, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.01})]
        row, = self._payload(A=self._file("A.jsonl", recs))["reconciliation"]
        self.assertEqual(row["est_tokens"], 6 * 110)
        self.assertEqual(row["cli_tokens"], 660)

    def test_requests_after_the_last_snapshot_are_left_out(self):
        recs = [_reply(f"m{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(1, 6)]
        recs.append(_counter(START_MS, 0.01, {
            "inputTokens": 30, "outputTokens": 300, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.01}, ts="2026-10-20T10:03:30.000Z"))
        recs += [_reply(f"z{i}", f"2026-10-20T10:{i:02d}:00.000Z") for i in range(10, 14)]
        row, = self._payload(A=self._file("A.jsonl", recs))["reconciliation"]
        self.assertEqual(row["est_tokens"], 3 * 110)

    def test_a_model_the_counter_never_saw_gets_no_row(self):
        recs = [_reply("m1", "2026-10-20T10:01:00.000Z"),
                _reply("h1", "2026-10-20T10:02:00.000Z", model="claude-haiku-5-5"),
                _counter(START_MS, 0.01, USAGE)]
        payload = self._payload(A=self._file("A.jsonl", recs))
        self.assertEqual([r["model"] for r in payload["reconciliation"]], [MODEL])


class TestRepricedCounter(_Base):
    def test_its_tokens_are_priced_at_the_tables_rates(self):
        recs = [_reply("m1", "2026-10-20T10:01:00.000Z"),
                _counter(START_MS, 0.5, USAGE, ts="2026-10-20T11:00:00.000Z")]
        payload = self._payload(A=self._file("A.jsonl", recs))
        want = _cost(MODEL, input_tokens=100, output_tokens=1000, cache_read_input_tokens=50000,
                     cache_creation_input_tokens=20000, ts="2026-10-20T11:00:00.000Z")
        row, = payload["reconciliation"]
        self.assertAlmostEqual(row["cli_repriced"], want, places=6)
        self.assertGreater(want, 0)
        session_row, = {s["id"]: s for s in payload["sessions"]}["A"]["cli_models"]
        self.assertAlmostEqual(session_row["cli_repriced"], want, places=6)
        self.assertEqual(session_row["cli_cost"], 0.5)

    def test_cache_writes_take_the_transcripts_one_hour_share(self):
        # The transcripts wrote 1-hour cache only; the counter's unsplit writes
        # are priced the same way, not as the cheaper 5-minute writes.
        reply = _reply("m1", "2026-10-20T10:01:00.000Z")
        reply["message"]["usage"].update(cache_creation_input_tokens=300, cache_creation={
            "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 300})
        recs = [reply, _counter(START_MS, 0.5, USAGE, ts="2026-10-20T11:00:00.000Z")]
        row, = self._payload(A=self._file("A.jsonl", recs))["reconciliation"]
        want = _cost(MODEL, input_tokens=100, output_tokens=1000, cache_read_input_tokens=50000,
                     cache_creation={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 20000},
                     ts="2026-10-20T11:00:00.000Z")
        self.assertAlmostEqual(row["cli_repriced"], want, places=6)

    def test_the_one_million_bucket_folds_into_the_base_model(self):
        recs = [_reply("m1", "2026-10-20T10:01:00.000Z"),
                _counter(START_MS, 0.5, USAGE, model=MODEL + "[1m]")]
        row, = self._payload(A=self._file("A.jsonl", recs))["reconciliation"]
        self.assertEqual(row["model"], MODEL)
        self.assertGreater(row["cli_repriced"], 0)

    def test_summed_tokens_do_not_take_a_prompt_length_tier(self):
        # Haiku 5.5 prices a prompt over 100K tokens at a higher rate; summed
        # tokens say nothing about any one prompt.
        usage = {"inputTokens": 4000, "outputTokens": 2000, "cacheReadInputTokens": 200000,
                 "cacheCreationInputTokens": 0, "costUSD": 0.0034}
        recs = [_reply("m1", "2026-10-20T10:01:00.000Z", model="claude-haiku-5-5"),
                _counter(START_MS, 0.0034, usage, model="claude-haiku-5-5")]
        row, = self._payload(A=self._file("A.jsonl", recs))["reconciliation"]
        want = _cost("claude-haiku-5-5", input_tokens=4000, output_tokens=2000,
                     cache_read_input_tokens=200000)
        self.assertAlmostEqual(row["cli_repriced"], want, places=6)


class TestPanel(unittest.TestCase):
    def _html(self):
        s = _session("s1")
        s["cli_models"] = [{"model": "m", "est_cost": 1.0, "est_tokens": 1000, "cli_cost": 2.0,
                            "cli_tokens": 2000, "cli_repriced": 2.2}]
        return run_js(DASHBOARD, _dashboard(sessions=[s]) +
                      "state.models = []; renderReconciliation(); out(el('reconcileWrap').innerHTML)")

    def test_the_panel_names_its_columns_and_sums_the_repriced_figure(self):
        html = self._html()
        for text in ("Logged here", "Claude Code counted", "Claude Code's $", "Its tokens at list price",
                     "$2.20", "+10.0%"):
            self.assertIn(text, html)

    def test_the_note_no_longer_makes_the_old_claims(self):
        html = self._html()
        self.assertNotIn("validates the rate table", html)
        self.assertNotIn("restarts from zero", html)
        self.assertIn("2.1.277", html)


if __name__ == "__main__":
    unittest.main()
