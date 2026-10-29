"""Subagent responses written with only a placeholder output count, and calls
no transcript records.

From Claude Code 2.1.284 a subagent response is written one line per content
block, every line carrying the usage from the start of the stream (2-20 output
tokens), and the final count is never written. On one session that left
Sonnet's output at about 2% of Claude Code's own count, and the estimate 27%
below the counter.

The fixture (tests/fixtures/placeholder) has one session with a cost-state
counter whose subagent writes placeholder lines, and one desktop session with
no counter."""
import datetime
import json
import os
import tempfile
import unittest

from claude_usage import aggregate, parser, team
from claude_usage import export as export_mod
from claude_usage.cache import FileCache
from claude_usage.parser import parse_file
from claude_usage.pricing import cost_for
from claude_usage.scan import scan

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "placeholder", "projects")
PROJ = os.path.join(ROOT, "-Users-x-proj9")
SUB = os.path.join(PROJ, "sess-ph", "subagents", "agent-ph1.jsonl")
DESK_SUB = os.path.join(PROJ, "sess-desk", "subagents", "agent-desk1.jsonl")
PERSON = ("someone@example.com", "Some One")

# Response A: a 1000-character signature and 1000 characters of text, 3 tokens
# logged. Response B wrote its final line (400). The desktop subagent's one
# response is a Bash call: "Bash" plus its 515-character JSON input.
EST_A = round(aggregate.IMPUTE_CHAR_TOKENS * 1000 + aggregate.IMPUTE_SIGNATURE_TOKENS * 1000)
EST_DESK = round(aggregate.IMPUTE_CHAR_TOKENS * (4 + 515))


def _scan():
    tmp = tempfile.TemporaryDirectory()
    payload = scan(ROOT, cache=FileCache(os.path.join(tmp.name, "c.json")),
                   tz=datetime.timezone.utc)
    tmp.cleanup()
    return payload


class TestParserMarksPlaceholders(unittest.TestCase):
    def setUp(self):
        self.recs = {r["response_id"]: r for r in parse_file(SUB, "sub").requests}

    def test_response_without_a_final_line_is_not_final(self):
        self.assertFalse(self.recs["m-subA"]["final"])
        self.assertEqual(self.recs["m-subA"]["output_tokens"], 3, "kept as logged")

    def test_response_with_a_stop_reason_is_final(self):
        self.assertTrue(self.recs["m-subB"]["final"])
        self.assertEqual(self.recs["m-subB"]["output_tokens"], 400)

    def test_sizes_are_counted_once_per_line(self):
        # The text line is written twice under one uuid; it counts once.
        self.assertEqual(self.recs["m-subA"]["out_chars"], 1000)
        self.assertEqual(self.recs["m-subA"]["sig_chars"], 1000)

    def test_a_tool_call_counts_its_name_and_json_input(self):
        rec, = parse_file(DESK_SUB, "sub").requests
        self.assertEqual(rec["out_chars"], len("Bash") + len(json.dumps({"command": "z" * 500})))

    def test_only_sizes_are_kept_never_the_text(self):
        facts = parse_file(SUB, "sub")
        dumped = json.dumps(facts.__dict__, default=str)
        self.assertNotIn("xxxxxxxx", dumped)
        self.assertNotIn("SSSSSSSS", dumped)

    def test_facts_version_bumped_for_the_new_fields(self):
        self.assertGreaterEqual(parser.FACTS_VERSION, 16)


class TestImputedOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.s = {s["id"]: s for s in cls.payload["sessions"]}

    def _rows(self, sid, sub):
        return [r for r in self.payload["usage"] if r[1] == sid and (r[4] != -1) == sub]

    def test_placeholder_output_is_imputed_from_what_the_response_wrote(self):
        rows = self._rows("sess-ph", sub=True)
        self.assertEqual(sum(r[16] for r in rows), EST_A - 3)
        self.assertEqual(sum(r[6] for r in rows), EST_A + 400, "output column includes imputed")

    def test_cost_prices_the_imputed_output(self):
        rows = self._rows("sess-ph", sub=True)
        a = cost_for("claude-sonnet-5-5", {"input_tokens": 2, "output_tokens": EST_A,
                                           "cache_read_input_tokens": 5000,
                                           "cache_creation": {"ephemeral_5m_input_tokens": 100}},
                     ts="2026-10-10T10:01:00.000Z")
        b = cost_for("claude-sonnet-5-5", {"input_tokens": 2, "output_tokens": 400,
                                           "cache_read_input_tokens": 6000,
                                           "cache_creation": {"ephemeral_5m_input_tokens": 50}},
                     ts="2026-10-10T10:02:00.000Z")
        self.assertAlmostEqual(sum(r[13] for r in rows), a + b, places=6)
        self.assertAlmostEqual(self.s["sess-ph"]["est_cost_sub"], a + b, places=6)

    def test_main_thread_responses_are_never_imputed(self):
        self.assertEqual(sum(r[16] for r in self._rows("sess-ph", sub=False)), 0)

    def test_session_row_says_how_much_was_imputed(self):
        self.assertEqual(self.s["sess-ph"]["imputed_output_tokens"], EST_A - 3)
        self.assertEqual(self.s["sess-ph"]["placeholder_responses"], 1)
        self.assertEqual(self.s["sess-desk"]["imputed_output_tokens"], EST_DESK - 2)

    def test_agent_row_carries_the_imputed_cost(self):
        agent, = [a for a in self.payload["agents"] if a["session_id"] == "sess-ph"]
        self.assertAlmostEqual(agent["cost"], self.s["sess-ph"]["est_cost_sub"], places=6)

    def test_dedupe_still_ranks_by_logged_output(self):
        # A copy of a response that wrote its final line beats a placeholder
        # copy of the same response, whatever the placeholder's estimate.
        final = {"response_id": "r", "request_id": "r", "ts": "2026-10-10T10:00:00.000Z",
                 "model": "claude-sonnet-5-5", "output_tokens": 50, "final": True,
                 "out_chars": 0, "sig_chars": 0}
        ph = dict(final, output_tokens=3, final=False, out_chars=100000)

        class F:
            def __init__(self, kind, path, rec):
                self.kind, self.path, self.requests, self.first_ts = kind, path, [rec], rec["ts"]
        winners, _, _ = aggregate._dedupe_requests_across_files(
            [], [("s1", F("sub", "a", ph)), ("s2", F("sub", "b", final))])
        (rec, _, _, _), = winners
        self.assertTrue(rec["final"])


class TestUnloggedFromTheCounter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        cls.s = {s["id"]: s for s in cls.payload["sessions"]}

    def test_counter_shortfall_is_priced_at_the_tables_rates(self):
        # Opus: 40 input, 50 output, 1M cache read and 100 cache write (1h, as
        # the transcript's own Opus writes are) more than the transcript since
        # the counter started. Sonnet: 30 output more than logged + imputed;
        # its cache reads are *below* the transcript's, which is clamped to 0.
        opus = cost_for("claude-opus-5-5", {"input_tokens": 40, "output_tokens": 50,
                                            "cache_read_input_tokens": 1_000_000,
                                            "cache_creation": {"ephemeral_1h_input_tokens": 100}},
                        ts="2026-10-10T10:00:00.000Z")
        sonnet = cost_for("claude-sonnet-5-5", {"output_tokens": 30}, ts="2026-10-10T10:00:00.000Z")
        self.assertAlmostEqual(self.s["sess-ph"]["est_cost_unlogged"], opus + sonnet, places=6)

    def test_shortfall_is_listed_per_model_and_category(self):
        by_model = {m["model"]: m for m in self.s["sess-ph"]["unlogged_models"]}
        self.assertEqual(
            {k: by_model["claude-opus-5-5"][k] for k in ("input_tokens", "output_tokens",
             "cache_read_tokens", "cache_write_5m", "cache_write_1h")},
            {"input_tokens": 40, "output_tokens": 50, "cache_read_tokens": 1_000_000,
             "cache_write_5m": 0, "cache_write_1h": 100})
        self.assertEqual(by_model["claude-sonnet-5-5"]["output_tokens"], 30)
        self.assertEqual(by_model["claude-sonnet-5-5"]["cache_read_tokens"], 0, "clamped, not negative")

    def test_work_from_before_the_counter_started_is_not_compared(self):
        # m-main0 (1,000 Opus output) predates the counter's startTime. Counted,
        # it would hide the 50-token Opus output shortfall.
        by_model = {m["model"]: m for m in self.s["sess-ph"]["unlogged_models"]}
        self.assertEqual(by_model["claude-opus-5-5"]["output_tokens"], 50)

    def test_transcript_estimate_is_unchanged_by_the_unlogged_line(self):
        est = sum(r[13] for r in self.payload["usage"] if r[1] == "sess-ph")
        s = self.s["sess-ph"]
        self.assertAlmostEqual(s["est_cost_main"] + s["est_cost_sub"], est, places=6)

    def test_a_session_without_a_counter_is_blank_not_zero(self):
        self.assertIsNone(self.s["sess-desk"]["est_cost_unlogged"])
        self.assertEqual(self.s["sess-desk"]["unlogged_models"], [])


class TestExportCarriesBoth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _scan()
        ctx = export_mod.build_context(cls.payload, PERSON)
        cls.summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                       for r in export_mod.summary_rows(cls.payload, PERSON, ctx=ctx)]
        cls.sessions = [dict(zip(export_mod.SESSION_COLUMNS, r))
                        for r in export_mod.session_rows(cls.payload, PERSON, ctx=ctx)]
        cls.breakdown = [dict(zip(export_mod.BREAKDOWN_COLUMNS, r))
                         for r in export_mod.breakdown_rows(cls.payload, PERSON, ctx=ctx)]

    def test_imputed_output_reconciles_across_sheets(self):
        want = sum(r[16] for r in self.payload["usage"])
        self.assertEqual(want, EST_A - 3 + EST_DESK - 2)
        for sheet in (self.summary, self.sessions, self.breakdown):
            self.assertEqual(sum(r["output_tokens_imputed"] or 0 for r in sheet), want)

    def test_unlogged_cost_reconciles_and_stays_out_of_the_estimate(self):
        want = sum(s["est_cost_unlogged"] or 0 for s in self.payload["sessions"])
        self.assertGreater(want, 0)
        for sheet in (self.summary, self.sessions):
            self.assertAlmostEqual(sum(r["est_cost_unlogged_usd"] or 0 for r in sheet), want, places=6)
            self.assertAlmostEqual(sum(r["est_cost_usd"] for r in sheet),
                                   sum(r[13] for r in self.payload["usage"]), places=6)

    def test_unlogged_is_blank_where_no_session_had_a_counter(self):
        desk, = [r for r in self.sessions if r["session_id"] == "sess-desk"]
        self.assertIsNone(desk["est_cost_unlogged_usd"])
        week_rows = [r for r in self.summary if r["sessions_started"] and not r["sessions_with_cli_cost"]]
        self.assertTrue(week_rows)
        self.assertTrue(all(r["est_cost_unlogged_usd"] is None for r in week_rows))

    def test_columns_are_appended_last_under_a_new_schema(self):
        self.assertGreaterEqual(export_mod.context_mod.EXPORT_SCHEMA_VERSION, 9)
        self.assertEqual(export_mod.SUMMARY_COLUMNS[-4:-2], ["output_tokens_imputed", "est_cost_unlogged_usd"])
        self.assertEqual(export_mod.SESSION_COLUMNS[-3:-1], ["output_tokens_imputed", "est_cost_unlogged_usd"])
        self.assertEqual(export_mod.BREAKDOWN_COLUMNS[-1], "output_tokens_imputed")

    def test_team_merge_adds_the_new_measures(self):
        self.assertIn("output_tokens_imputed", team._SUMMED["summary"])
        self.assertIn("est_cost_unlogged_usd", team._SUMMED["summary"])
        self.assertIn("output_tokens_imputed", team._SUMMED["breakdown"])


if __name__ == "__main__":
    unittest.main()
