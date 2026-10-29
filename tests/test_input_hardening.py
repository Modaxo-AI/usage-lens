"""One wrong-typed leaf field in a transcript line must cost at most that line,
never the whole scan (BUG-02). Each case writes one good request plus one
record with a single field of the wrong type, then runs the full scan."""
import contextlib
import copy
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_usage import aggregate, parser
from claude_usage.cache import FileCache
from claude_usage.parser import parse_file
from claude_usage.scan import scan

_TS = "2026-01-01T10:00:00.000Z"


def _good():
    return {"type": "assistant", "timestamp": _TS, "requestId": "req-good",
            "message": {"id": "msg-good", "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": 20}}}


def _bad(**overrides):
    """An assistant record that is a request in its own right, carrying a Skill
    tool call so the tool-id and name paths are exercised too."""
    rec = {"type": "assistant", "timestamp": _TS, "requestId": "req-bad",
           "message": {"id": "msg-bad", "model": "claude-sonnet-5",
                       "usage": {"input_tokens": 10, "output_tokens": 20,
                                 "cache_read_input_tokens": 5,
                                 "cache_creation": {"ephemeral_5m_input_tokens": 5},
                                 "output_tokens_details": {"thinking_tokens": 3},
                                 "server_tool_use": {"web_search_requests": 1}},
                       "content": [{"type": "tool_use", "id": "tu-1", "name": "Skill",
                                    "input": {"skill": "dataviz"}}]}}
    for path, value in overrides.items():
        target = rec
        keys = path.split(".")
        for k in keys[:-1]:
            target = target[k]
        if value is _DELETE:
            del target[keys[-1]]
        else:
            target[keys[-1]] = value
    return rec


_DELETE = object()


def _tool_block(**block):
    rec = _bad()
    rec["message"]["content"] = [{"type": "tool_use", "id": "tu-1", "name": "Edit", **block}]
    return [rec]


# label -> (bad records, requests the scan should count in total)
_CASES = {
    "message.id dict": ([_bad(**{"message.id": {"x": 1}})], 2),
    "message.id gone, requestId dict": (
        [_bad(**{"message.id": _DELETE, "requestId": {"x": 1}})], 1),
    "tool_use id list": (_tool_block(id=["a"]), 2),
    "tool_use name list": (_tool_block(name=["Edit"]), 2),
    "tool_use name dict": (_tool_block(name={"n": 1}), 2),
    "tool_use skill list": ([_bad(**{"message.content": [
        {"type": "tool_use", "id": "tu-1", "name": "Skill", "input": {"skill": ["a"]}}]})], 2),
    "tool_result tool_use_id list": ([
        {"type": "user", "timestamp": _TS,
         "message": {"content": [{"type": "tool_result", "tool_use_id": ["a"], "is_error": True}]}}], 1),
    "input_tokens str": ([_bad(**{"message.usage.input_tokens": "20"})], 2),
    "output_tokens str": ([_bad(**{"message.usage.output_tokens": "20"})], 2),
    "cache_read str": ([_bad(**{"message.usage.cache_read_input_tokens": "5"})], 2),
    "cache_5m str": ([_bad(**{"message.usage.cache_creation.ephemeral_5m_input_tokens": "5"})], 2),
    "thinking_tokens str": ([_bad(**{"message.usage.output_tokens_details.thinking_tokens": "3"})], 2),
    "web_search_requests str": ([_bad(**{"message.usage.server_tool_use.web_search_requests": "1"})], 2),
    "output_tokens NaN": ([_bad(**{"message.usage.output_tokens": float("nan")})], 2),
    "output_tokens Infinity": ([_bad(**{"message.usage.output_tokens": float("inf")})], 2),
    "model list": ([_bad(**{"message.model": ["claude-opus-5-5"]})], 1),
    "model int": ([_bad(**{"message.model": 5})], 1),
    "attributionSkill list": ([_bad(attributionSkill=["x"])], 2),
    "permissionMode list": ([{"type": "permission-mode", "timestamp": _TS, "permissionMode": ["x"]}], 1),
    "mode list": ([{"type": "mode", "timestamp": _TS, "mode": ["x"]}], 1),
    "attachment files str": ([{"type": "attachment", "timestamp": _TS,
                               "attachment": {"type": "instructions", "files": "notalist"}}], 1),
    "system uuid dict": ([{"type": "system", "subtype": "api_error", "timestamp": _TS,
                           "uuid": {"x": 1}, "retryInMs": 5}], 1),
    "user prompt uuid list": ([{"type": "user", "timestamp": _TS, "uuid": ["u"],
                                "permissionMode": ["x"], "message": {"content": "hi"}}], 1),
}


class _Scanned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.root, "proj"))
        self.cache_path = os.path.join(self.tmp.name, "cache.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, name="s1.jsonl"):
        with open(os.path.join(self.root, "proj", name), "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")  # NaN / inf go out as bare literals

    def scan(self):
        return scan(self.root, cache=FileCache(self.cache_path))


def _total_requests(payload):
    return sum(s["requests"] for s in payload["sessions"])


class TestOneBadFieldCostsOneLine(_Scanned):
    def test_each_case_survives_and_counts_the_good_request(self):
        for label, (bad, expected) in _CASES.items():
            with self.subTest(label):
                self.write([_good(), *copy.deepcopy(bad)])
                payload = self.scan()
                self.assertEqual(_total_requests(payload), expected)
                self.assertIn("msg-good", {r["response_id"] for r in
                                           parse_file(os.path.join(self.root, "proj", "s1.jsonl"),
                                                      "main").requests})
                if os.path.exists(self.cache_path):
                    os.remove(self.cache_path)  # each case parses cold

    def test_an_integral_float_is_counted_as_that_many_tokens(self):
        self.write([_bad(**{"message.usage.output_tokens": 20.0})])
        rec, = parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main").requests
        self.assertEqual(rec["output_tokens"], 20)
        self.assertIsInstance(rec["output_tokens"], int)
        self.assertEqual(_total_requests(self.scan()), 1)

    def test_a_bad_token_field_counts_as_zero(self):
        self.write([_bad(**{"message.usage.output_tokens": "20",
                            "message.usage.input_tokens": float("nan")})])
        rec, = parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main").requests
        self.assertEqual((rec["input_tokens"], rec["output_tokens"]), (0, 0))


class TestStaleCacheIsDiscarded(_Scanned):
    def test_a_cache_from_the_previous_facts_version_is_reparsed(self):
        self.write([_good()])
        # A cache as the previous build wrote it: version 8, and a token count
        # that build let through as a string.
        with mock.patch.object(parser, "FACTS_VERSION", 8), \
             mock.patch("claude_usage.cache.FACTS_VERSION", 8):
            self.scan()
        with open(self.cache_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["facts_version"], 8)
        for entry in saved["entries"].values():
            for rec in entry["facts"]["requests"]:
                rec["output_tokens"] = "20"
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)

        payload = self.scan()  # must discard it and re-parse, not price "20" / 1e6
        self.assertEqual(_total_requests(payload), 1)
        self.assertEqual(payload["files_parsed"], 1)
        self.assertEqual(payload["files_cached"], 0)


def _cost_state(**overrides):
    rec = {"type": "cost-state", "startTime": "2026-01-01T09:00:00.000Z",
           "totalCostUSD": 1.5, "totalLinesAdded": 3, "totalLinesRemoved": 1,
           "modelUsage": {"claude-sonnet-5": {"inputTokens": 10, "outputTokens": 20,
                                              "costUSD": 0.2}}}
    for path, value in overrides.items():
        target = rec
        keys = path.split(".")
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return rec


def _pr(**overrides):
    return {"type": "pr-link", "timestamp": _TS, "prNumber": 7, "prRepository": "o/r",
            "prUrl": "https://github.com/o/r/pull/7", **overrides}


_LIMIT_NOTICE = {"type": "assistant", "timestamp": _TS, "isApiErrorMessage": True,
                 "error": "rate_limit",
                 "message": {"content": [{"type": "text", "text": "You've hit your limit"}]}}

# Records that are not requests: each must survive the whole scan and leave a
# payload the server can serialise as strict JSON.
_RECORD_CASES = {
    "prUrl list": [_pr(prUrl=["x"])],
    "prNumber list": [_pr(prNumber=["x"])],
    "prRepository dict": [_pr(prRepository={"x": 1})],
    "startTime list": [_cost_state(startTime=["x"])],
    "totalCostUSD str": [_cost_state(totalCostUSD="1.5")],
    "totalCostUSD NaN": [_cost_state(totalCostUSD=float("nan"))],
    "totalCostUSD Infinity": [_cost_state(totalCostUSD=float("inf"))],
    "totalLinesAdded str": [_cost_state(totalLinesAdded="3")],
    "totalAPIDuration str": [_cost_state(totalAPIDuration="9")],
    "modelUsage costUSD str": [_cost_state(**{"modelUsage.claude-sonnet-5.costUSD": "0.2"})],
    "modelUsage costUSD NaN": [_cost_state(**{"modelUsage.claude-sonnet-5.costUSD": float("nan")})],
    "modelUsage inputTokens str": [_cost_state(**{"modelUsage.claude-sonnet-5.inputTokens": "10"})],
    "api_error uuid list": [{"type": "system", "subtype": "api_error", "timestamp": _TS,
                             "uuid": ["x"], "retryInMs": 5}],
    "api_error retryInMs str": [{"type": "system", "subtype": "api_error", "timestamp": _TS,
                                 "uuid": "u1", "retryInMs": "5"}],
    "api_error retryInMs NaN": [{"type": "system", "subtype": "api_error", "timestamp": _TS,
                                 "uuid": "u1", "retryInMs": float("nan")}],
    "turn_duration durationMs str": [{"type": "system", "subtype": "turn_duration",
                                      "timestamp": _TS, "uuid": "u1", "durationMs": "5"}],
    "compact preTokens str": [{"type": "system", "subtype": "compact_boundary",
                               "timestamp": _TS, "uuid": "u1",
                               "compactMetadata": {"preTokens": "5", "trigger": "auto"}}],
    "limit notice uuid list": [dict(_LIMIT_NOTICE, uuid=["x"])],
    "user prompt uuid list": [{"type": "user", "timestamp": _TS, "uuid": ["u"],
                               "message": {"content": "hi"}}],
    "customTitle list": [{"type": "custom-title", "customTitle": ["t"]}],
    "aiTitle dict": [{"type": "ai-title", "aiTitle": {"t": 1}}],
    "agentName int": [{"type": "agent-name", "agentName": 5}],
    "continuedInSessionId list": [{"type": "continued-in", "continuedInSessionId": ["s"]}],
}


class TestNonRequestRecords(_Scanned):
    def test_each_case_survives_and_serialises_as_strict_json(self):
        for label, bad in _RECORD_CASES.items():
            with self.subTest(label):
                self.write([_good(), *copy.deepcopy(bad)])
                payload = self.scan()
                self.assertEqual(_total_requests(payload), 1)
                json.dumps(payload, allow_nan=False)
                if os.path.exists(self.cache_path):
                    os.remove(self.cache_path)

    def test_the_parser_stores_only_clean_values(self):
        self.write([
            _cost_state(totalCostUSD=float("nan"), totalLinesAdded="3",
                        **{"modelUsage.claude-sonnet-5.costUSD": "0.2"}),
            {"type": "system", "subtype": "api_error", "timestamp": _TS, "uuid": "u1",
             "retryInMs": "5"},
            _pr(prUrl=["x"]),
        ])
        facts = parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main")
        cs, = facts.cost_states
        self.assertEqual((cs["totalCostUSD"], cs["totalLinesAdded"]), (0.0, 0))
        self.assertEqual(cs["modelUsage"]["claude-sonnet-5"]["cost"], 0.0)
        self.assertEqual(facts.friction, [["u1", _TS, "retry", 0]])
        self.assertEqual(facts.pr_links, [])

    def test_a_numeric_start_time_is_kept_as_the_counter_id(self):
        # Real cost-state records write startTime as a number.
        self.write([_cost_state(startTime=1767261600000)])
        facts = parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main")
        self.assertEqual(facts.cost_states[0]["startTime"], 1767261600000)

    def test_the_parser_ignores_wrong_typed_titles_and_continuation(self):
        self.write([
            {"type": "custom-title", "customTitle": ["t"]},
            {"type": "ai-title", "aiTitle": {"t": 1}},
            {"type": "agent-name", "agentName": 5},
            {"type": "continued-in", "continuedInSessionId": ["s"]},
        ])
        facts = parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main")
        self.assertEqual((facts.custom_title, facts.ai_title, facts.agent_name), (None, None, None))
        self.assertIsNone(facts.continued_in)


class TestAggregateGuardsAgainstBadFacts(unittest.TestCase):
    """The cache can hold facts the parser would no longer produce (a hand edit,
    another build); build_payload must not be the thing that crashes on them."""

    def _facts(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s1.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec) + "\n")
            return parse_file(path, "main")

    def _build(self, facts):
        payload = aggregate.build_payload({"s1": {"facts": facts, "folder": "proj"}}, [],
                                          "2026-01-02T00:00:00+00:00")
        json.dumps(payload, allow_nan=False)
        return payload

    def test_cost_state_numbers(self):
        for label, change in {
            "totalCostUSD str": {"totalCostUSD": "1.5"},
            "totalCostUSD NaN": {"totalCostUSD": float("nan")},
            "totalCostUSD inf": {"totalCostUSD": float("inf")},
            "totalLinesAdded str": {"totalLinesAdded": "3"},
            "totalLinesRemoved NaN": {"totalLinesRemoved": float("nan")},
            "startTime list": {"startTime": ["x"]},
            "modelUsage costUSD str": {"modelUsage": {"claude-sonnet-5": {
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "cost": "0.2"}}},
            "modelUsage costUSD NaN": {"modelUsage": {"claude-sonnet-5": {
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "cost": float("nan")}}},
        }.items():
            with self.subTest(label):
                facts = self._facts([_good(), _cost_state()])
                facts.cost_states[0].update(change)
                self._build(facts)

    def test_pr_link_fields(self):
        for label, change in {"prUrl list": {"prUrl": ["x"]},
                              "timestamp list": {"timestamp": ["x"]}}.items():
            with self.subTest(label):
                facts = self._facts([_good(), _pr()])
                facts.pr_links[0].update(change)
                self._build(facts)

    def test_friction_and_health_rows(self):
        facts = self._facts([_good()])
        facts.friction = [[["u"], _TS, "retry", "5"], ["u2", _TS, "turn", float("nan")]]
        facts.health_events = [[["u"], _TS, "denial", ["x"]], ["u3", _TS, "prompt", {"a": 1}]]
        self._build(facts)

    def test_mixed_type_tool_and_skill_names_still_sort(self):
        facts = self._facts([_good()])
        facts.tool_calls = {"t1": 5, "t2": "Edit"}
        facts.skill_invocations = [{"id": "k1", "skill": 5, "ts": _TS},
                                   {"id": "k2", "skill": "dataviz", "ts": _TS}]
        self._build(facts)

    def test_num(self):
        n = aggregate._num
        self.assertEqual([n(5), n(1.5), n(0)], [5.0, 1.5, 0.0])
        for bad in (True, "1.5", None, float("nan"), float("inf"), -float("inf"), [1], {}):
            self.assertEqual(n(bad), 0.0, repr(bad))
        self.assertIsInstance(n(5), float)


class TestFolderAndFileBackstops(_Scanned):
    def _quiet_scan(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            payload = self.scan()
        return payload, err.getvalue()

    def test_an_unreadable_project_folder_costs_only_that_folder(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("chmod 000 does not block this user")
        bad = os.path.join(self.root, "aaa-locked")
        os.makedirs(bad)
        with open(os.path.join(bad, "s0.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_good()) + "\n")
        self.write([_good()])
        os.chmod(bad, 0)
        try:
            payload, _ = self._quiet_scan()
        finally:
            os.chmod(bad, 0o700)  # so the temp dir can be removed
        self.assertEqual(_total_requests(payload), 1)

    def test_a_file_that_cannot_be_loaded_is_skipped_and_named_on_stderr(self):
        self.write([_good()], name="s1.jsonl")
        self.write([_good()], name="s2.jsonl")
        real = parser.parse_file

        def flaky(path, kind):
            if path.endswith("s2.jsonl"):
                raise RuntimeError("boom")
            return real(path, kind)

        with mock.patch("claude_usage.scan.parse_file", flaky):
            payload, err = self._quiet_scan()
        self.assertEqual(_total_requests(payload), 1)
        self.assertEqual(payload["files_parsed"], 1)
        self.assertIn("skipped", err)
        self.assertIn("s2.jsonl", err)
        self.assertIn("boom", err)


class TestHelpers(unittest.TestCase):
    def test_tok(self):
        t = parser._tok
        self.assertEqual([t(5), t(0), t(7.0), t(0.0)], [5, 0, 7, 0])
        for bad in (True, False, "20", None, -1, -3.0, 2.5, float("nan"), float("inf"), [1], {}):
            self.assertEqual(t(bad), 0, repr(bad))

    def test_sid_and_name(self):
        for fn in (parser._sid, parser._name):
            self.assertEqual(fn("a"), "a")
            for bad in ("", None, 5, ["a"], {"a": 1}, True):
                self.assertIsNone(fn(bad), repr(bad))


# ---- Phase 1 gate: crash paths found by the adversarial fuzz ---------------- #

_HUGE_INT = 10 ** 400  # 401 digits: finite as an int, but not as a float


class _QuietScanned(_Scanned):
    def quiet_scan(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            payload = self.scan()
        return payload, err.getvalue()

    def write_raw(self, lines, name="s1.jsonl"):
        with open(os.path.join(self.root, "proj", name), "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")


class TestSubagentMetaFields(_QuietScanned):
    """agent-*.meta.json is another file this tool does not write; a wrong-typed
    or non-finite field in it must not take /api/data down."""

    def _write_subagent(self, meta):
        self.write([_good()])
        sub_dir = os.path.join(self.root, "proj", "s1", "subagents")
        os.makedirs(sub_dir, exist_ok=True)
        with open(os.path.join(sub_dir, "agent-a1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(_good(), requestId="req-sub",
                                     message=dict(_good()["message"], id="msg-sub"))) + "\n")
        with open(os.path.join(sub_dir, "agent-a1.meta.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta))  # NaN / Infinity go out as bare literals

    def test_each_bad_meta_field_still_counts_the_subagent(self):
        bad_values = (["general-purpose"], {"a": 1}, float("nan"), float("inf"), -float("inf"),
                      _HUGE_INT, True)
        for field in ("agentType", "description", "spawnDepth"):
            for bad in bad_values:
                with self.subTest(field=field, bad=repr(bad)[:30]):
                    meta = {"agentType": "general-purpose", "description": "d", "spawnDepth": 2}
                    meta[field] = bad
                    self._write_subagent(meta)
                    payload, err = self.quiet_scan()
                    json.dumps(payload, allow_nan=False)
                    self.assertEqual(err, "")
                    self.assertEqual(payload["summary"]["total_subagents"], 1)
                    agent, = payload["agents"]
                    self.assertEqual(agent["requests"], 1)
                    self.assertIsInstance(agent["agent_type"], str)
                    self.assertIsInstance(agent["description"], str)
                    self.assertIsInstance(agent["spawn_depth"], int)
                    self.assertEqual(_total_requests(payload), 2)  # the main thread and the subagent

    def test_good_meta_values_are_unchanged_and_defaults_kept(self):
        self._write_subagent({"agentType": "Explore", "description": "x" * 200, "spawnDepth": 3,
                              "toolUseId": "t"})
        meta = parser.parse_meta(os.path.join(self.root, "proj", "s1", "subagents",
                                              "agent-a1.meta.json"))
        self.assertEqual((meta["agent_type"], meta["spawn_depth"], len(meta["description"])),
                         ("Explore", 3, 120))
        self._write_subagent({})
        meta = parser.parse_meta(os.path.join(self.root, "proj", "s1", "subagents",
                                              "agent-a1.meta.json"))
        self.assertEqual((meta["agent_type"], meta["spawn_depth"], meta["description"]),
                         ("unknown", 1, ""))


class TestHugeFiniteNumbers(_QuietScanned):
    def test_helpers_treat_beyond_a_quadrillion_as_garbage(self):
        t, n = parser._tok, parser._num
        self.assertEqual((t(10 ** 15), t(1e15), n(10 ** 15), n(-1e15)), (10 ** 15, 10 ** 15, 1e15, -1e15))
        for huge in (10 ** 15 + 1, 1e16, 1.7e308, _HUGE_INT):
            self.assertEqual(t(huge), 0, repr(huge)[:20])
        for huge in (10 ** 15 + 1, 1.7e308, _HUGE_INT, -_HUGE_INT):
            self.assertEqual(n(huge), 0.0, repr(huge)[:20])
            self.assertIsInstance(n(huge), float)
        self.assertIsNone(sanitize_counter_key(_HUGE_INT))
        self.assertIsNone(sanitize_counter_key(1.7e308))
        self.assertEqual(sanitize_counter_key(1767261600000), 1767261600000)

    def _assert_scans_clean(self, bad_records, expected_requests=1):
        self.write([_good(), *copy.deepcopy(bad_records)])
        payload, err = self.quiet_scan()
        self.assertEqual(err, "", "a file was skipped")
        json.dumps(payload, allow_nan=False)
        self.assertEqual(_total_requests(payload), expected_requests)
        self.assertEqual(len(payload["sessions"]), 1)
        return payload

    def test_a_huge_token_count_costs_its_own_field(self):
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            for huge in (_HUGE_INT, 1.7e308):
                with self.subTest(field=field, huge=repr(huge)[:12]):
                    self._assert_scans_clean([_bad(**{"message.usage." + field: huge})], 2)
                    os.remove(self.cache_path)

    def test_two_huge_requests_in_one_bucket_do_not_overflow_the_sum(self):
        recs = [_bad(**{"message.usage.output_tokens": 1.7e308}),
                _bad(**{"message.id": "msg-bad2", "requestId": "req-bad2",
                        "message.usage.output_tokens": 1.7e308,
                        "message.content": []})]
        payload = self._assert_scans_clean(recs, 3)
        self.assertEqual(sum(r[6] for r in payload["usage"]), 20)  # only the good one

    def test_a_huge_int_in_a_non_request_record_does_not_drop_the_transcript(self):
        cases = {
            "totalCostUSD": [_cost_state(totalCostUSD=_HUGE_INT)],
            "totalAPIDuration": [_cost_state(totalAPIDuration=_HUGE_INT)],
            "totalToolDuration": [_cost_state(totalToolDuration=_HUGE_INT)],
            "startTime": [_cost_state(startTime=_HUGE_INT)],
            "totalLinesAdded": [_cost_state(totalLinesAdded=_HUGE_INT)],
            "modelUsage costUSD": [_cost_state(**{"modelUsage.claude-sonnet-5.costUSD": _HUGE_INT})],
            "modelUsage inputTokens": [_cost_state(
                **{"modelUsage.claude-sonnet-5.inputTokens": _HUGE_INT})],
            "retryInMs": [{"type": "system", "subtype": "api_error", "timestamp": _TS,
                           "uuid": "u1", "retryInMs": _HUGE_INT}],
            "durationMs": [{"type": "system", "subtype": "turn_duration", "timestamp": _TS,
                            "uuid": "u1", "durationMs": _HUGE_INT}],
            "preTokens": [{"type": "system", "subtype": "compact_boundary", "timestamp": _TS,
                           "uuid": "u1", "compactMetadata": {"preTokens": _HUGE_INT}}],
        }
        for label, bad in cases.items():
            with self.subTest(label):
                self._assert_scans_clean(bad)
                os.remove(self.cache_path)

    def test_two_huge_model_usage_costs_in_one_family_stay_finite(self):
        usage = {"claude-sonnet-5": {"costUSD": 1.7e308}, "claude-sonnet-5[1m]": {"costUSD": 1.7e308}}
        payload = self._assert_scans_clean([_cost_state(modelUsage=usage)])
        for row in payload["reconciliation"]:
            self.assertEqual(row["cli_cost"], 0.0)


from claude_usage.sanitize import counter_key as sanitize_counter_key  # noqa: E402


class TestNoticeTextOfAnyType(_QuietScanned):
    def test_a_rate_limit_notice_with_non_string_text_keeps_the_transcript(self):
        for bad in (["x"], 5, 1.5, {"a": 1}, True, float("nan")):
            with self.subTest(repr(bad)):
                notice = {"type": "assistant", "uuid": "n1", "timestamp": _TS,
                          "isApiErrorMessage": True, "error": "rate_limit",
                          "apiErrorStatus": 429,
                          "message": {"id": "nn", "model": "<synthetic>",
                                      "content": [{"type": "text", "text": bad}]}}
                self.write([_good(), notice])
                payload, err = self.quiet_scan()
                self.assertEqual(err, "", "the transcript was skipped")
                self.assertEqual(_total_requests(payload), 1)
                self.assertEqual(len(payload["sessions"]), 1)
                json.dumps(payload, allow_nan=False)
                os.remove(self.cache_path)

    def test_only_well_formed_text_blocks_are_read(self):
        text = parser._notice_text
        self.assertEqual(text({"content": "You've hit your limit"}), "You've hit your limit")
        self.assertEqual(text({"content": [
            {"type": "text", "text": ["x"]}, {"type": "image", "text": "no"},
            {"type": "text", "text": "You've hit"}, "junk", {"type": "text", "text": "your limit"},
            {"type": "text", "text": None}]}), "You've hit your limit")
        self.assertEqual(text({"content": 5}), "")


class TestUnparseableJsonLines(_QuietScanned):
    def test_a_line_json_cannot_load_costs_only_itself(self):
        good = json.dumps(_good())
        for label, line in {
            "4301-digit int": '{"type":"custom-title","customTitle":"t","n":' + "9" * 4301 + "}",
            "deep nesting": "[" * 100000,
            "deep nesting in a record": '{"type":"x","a":' + "[" * 100000,
        }.items():
            with self.subTest(label):
                self.write_raw([good, line, good.replace("msg-good", "msg-good2")
                                .replace("req-good", "req-good2")])
                payload, err = self.quiet_scan()
                self.assertEqual(err, "", "the transcript was skipped")
                self.assertEqual(_total_requests(payload), 2)
                os.remove(self.cache_path)


class TestCorruptedCacheAtTheCurrentVersion(_QuietScanned):
    def _corrupt(self, change):
        with open(self.cache_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for entry in raw["entries"].values():
            change(entry["facts"])
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_facts_build_payload_cannot_use_are_reparsed(self):
        changes = {
            "model list": lambda f: f["requests"][0].__setitem__("model", ["x"]),
            "ts int": lambda f: f["requests"][0].__setitem__("ts", 5),
            "response_id list": lambda f: f["requests"][0].__setitem__("response_id", ["x"]),
            "missing input_tokens": lambda f: f["requests"][0].pop("input_tokens"),
            "requests [1]": lambda f: f.__setitem__("requests", [1]),
        }
        for label, change in changes.items():
            with self.subTest(label):
                self.write([_good()])
                self.scan()  # a valid cache
                self._corrupt(change)
                payload, err = self.quiet_scan()
                self.assertEqual(_total_requests(payload), 1)
                self.assertEqual(payload["usage"][0][5], 10)  # input tokens, from the re-parse
                self.assertEqual(payload["files_parsed"], 1)
                self.assertEqual(payload["files_cached"], 0)
                self.assertIn("re-parsing", err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                with open(self.cache_path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                rec, = [r for e in saved["entries"].values() for r in e["facts"]["requests"]]
                self.assertEqual(rec["model"], "claude-sonnet-5")
                payload, err = self.quiet_scan()  # now served clean from the cache
                self.assertEqual((payload["files_cached"], err), (1, ""))

    def test_a_failure_that_survives_a_re_parse_is_raised(self):
        self.write([_good()])
        with mock.patch("claude_usage.scan.build_payload", side_effect=RuntimeError("bug")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(RuntimeError):
                self.scan()


class TestIntegersStayIntegers(_QuietScanned):
    def test_durations_and_token_counts_of_valid_input_are_ints(self):
        self.write([
            _good(),
            _cost_state(totalAPIDuration=1000, totalToolDuration=200, startTime=1767261600000),
            {"type": "system", "subtype": "api_error", "timestamp": _TS, "uuid": "u1",
             "retryInMs": 1000},
            {"type": "system", "subtype": "turn_duration", "timestamp": _TS, "uuid": "u2",
             "durationMs": 30000},
            {"type": "system", "subtype": "compact_boundary", "timestamp": _TS, "uuid": "u3",
             "compactMetadata": {"preTokens": 5000, "trigger": "auto"}},
        ])
        payload = self.scan()
        session, = payload["sessions"]
        self.assertIs(type(session["api_ms"]), int)
        self.assertEqual((session["api_ms"], session["tool_ms"]), (1000, 200))
        self.assertIs(type(session["tool_ms"]), int)
        self.assertIs(type(session["cli_models"][0]["cli_tokens"]), int)
        self.assertEqual(session["cli_models"][0]["cli_tokens"], 30)
        self.assertIs(type(payload["reconciliation"][0]["cli_tokens"]), int)
        # Lines changed come from edits (P3), not from the counter's totals.
        self.assertIsNone(session["lines_added"])
        self.assertEqual(len(payload["friction"]), 3)
        for row in payload["friction"]:
            self.assertIs(type(row[3]), int, row)
        self.assertEqual(sorted(r[3] for r in payload["friction"]), [1000, 5000, 30000])

    def test_a_fractional_duration_is_kept_not_zeroed(self):
        self.write([_good(), _cost_state(totalAPIDuration=1234.5),
                    {"type": "system", "subtype": "api_error", "timestamp": _TS, "uuid": "u1",
                     "retryInMs": 538.25}])
        payload = self.scan()
        self.assertEqual(payload["sessions"][0]["api_ms"], 1234.5)
        self.assertEqual(payload["friction"][0][3], 538.25)

    def test_money_stays_a_float(self):
        self.write([_good(), _cost_state(totalCostUSD=2)])
        self.assertIs(type(self.scan()["sessions"][0]["cli_cost"]), float)

    def test_negative_line_counts_clamp_to_zero(self):
        facts = parse_file(self._path(_cost_state(totalLinesAdded=-5)), "main")
        self.assertEqual(facts.cost_states[0]["totalLinesAdded"], 0)
        facts.cost_states[0]["totalLinesAdded"] = -5  # as a hand-edited cache could hold it
        payload = aggregate.build_payload({"s1": {"facts": facts, "folder": "proj"}}, [],
                                          "2026-01-02T00:00:00+00:00")
        # The counter's line totals are no longer shown; the session made no edit.
        self.assertIsNone(payload["sessions"][0]["lines_added"])

    def _path(self, *records):
        self.write([_good(), *records])
        return os.path.join(self.root, "proj", "s1.jsonl")


class TestSkillCallWithoutATimestamp(_QuietScanned):
    def test_it_is_bucketed_on_the_files_first_day_like_a_tool_call(self):
        skill_call = {"type": "assistant", "requestId": "req-s", "message": {
            "id": "msg-s", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "tu-s", "name": "Skill",
                         "input": {"skill": "dataviz"}}]}}  # no timestamp
        self.write([_good(), skill_call])
        payload = self.scan()
        row, = payload["skillCalls"]
        self.assertEqual(row[0], aggregate._local_bucket(_TS))
        self.assertEqual(row[2:], ["dataviz", 1, "claude-sonnet-5", "tool"])
        tool_row, = payload["tools"]
        self.assertEqual(tool_row[0], row[0])


class TestCollectWritesStrictJson(_QuietScanned):
    """A non-finite number that reaches the payload must be written as null,
    never as a bare NaN (a browser's JSON.parse refuses it) and never crash
    collect.py without writing data.json."""

    def test_finite_json_nulls_non_finite_numbers(self):
        from claude_usage.sanitize import finite_json
        nan, inf = float("nan"), float("inf")
        self.assertEqual(finite_json({"a": nan, "b": [1.5, inf, {"c": -inf}], "d": (nan, 2),
                                      "s": "nan", "n": None, "i": 3}),
                         {"a": None, "b": [1.5, None, {"c": None}], "d": [None, 2],
                          "s": "nan", "n": None, "i": 3})

    def test_script_safe_json_nulls_non_finite_numbers(self):
        import collect
        self.assertEqual(collect._script_safe_json({"a": float("nan"), "b": 1.5}),
                         '{"a":null,"b":1.5}')

    def _run_collect(self, price_cache=None, edit_facts=None):
        """collect.main() against a throwaway log root, parse cache, price cache
        and output file; returns the text it wrote to data.json."""
        import collect
        from claude_usage import price_check
        self.write([_good()])
        if edit_facts:
            self.scan()
            with open(self.cache_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for entry in raw["entries"].values():
                edit_facts(entry["facts"])
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
        out_path = os.path.join(self.tmp.name, "data.json")
        saved = (collect.scan, collect.OUT_PATH, price_check.CACHE_FILE, sys.argv,
                 os.environ.pop(price_check.OFFLINE_ENV, None))

        real_scan = collect.scan
        collect.scan = lambda root, force=False: real_scan(
            self.root, cache=FileCache(self.cache_path), force=force)
        collect.OUT_PATH = Path(out_path)
        price_check.CACHE_FILE = Path(self.tmp.name) / "price-check.json"
        if price_cache is not None:
            price_check.CACHE_FILE.write_text(price_cache, encoding="utf-8")
        sys.argv = ["collect.py"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                collect.main()
        finally:
            collect.scan, collect.OUT_PATH, price_check.CACHE_FILE, sys.argv = saved[:4]
            if saved[4] is not None:
                os.environ[price_check.OFFLINE_ENV] = saved[4]
        with open(out_path, encoding="utf-8") as fh:
            return fh.read()

    def _assert_strict(self, text):
        def refuse(token):
            raise AssertionError(f"non-JSON literal {token}")
        return json.loads(text, parse_constant=refuse)

    def test_a_non_finite_price_check_error_does_not_stop_data_json(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for field in ("error", "url"):
            with self.subTest(field):
                cache = json.dumps({"checked_at": now, "status": "failed", field: float("nan")})
                data = self._assert_strict(self._run_collect(price_cache=cache))
                self.assertEqual(data["price_check"]["status"], "failed")
                self.assertIsNone(data["price_check"]["error"] if field == "error" else None)
                self.assertIsInstance(data["price_check"]["url"], str)

    def test_a_non_finite_value_in_hand_edited_facts_does_not_stop_data_json(self):
        def edit(facts):
            facts["error_streaks"] = [["tu-bash-1", _TS, float("nan")]]
        data = self._assert_strict(self._run_collect(edit_facts=edit))
        self.assertEqual(data["summary"]["total_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
