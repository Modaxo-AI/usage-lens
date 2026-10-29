"""Phase 3 backend fixes from the full review (see each test's finding id):
what the parser keeps (permission modes, refusals, edit diffs, calling models,
slash commands), what the aggregate does with it, and what the export and the
redacted build say about it."""
import datetime
import json
import os
import shutil
import tempfile
import unittest

import collect
from claude_usage import aggregate, context as context_mod, export as export_mod, health, parser
from claude_usage.cache import FileCache
from claude_usage.parser import parse_file
from claude_usage.scan import scan
from tests.test_health import SENTINEL, _prompt, _response, _tool_result, _tool_use, _ts, _write

UTC = datetime.timezone.utc
PERSON = ("someone@example.com", "Some One")
CWD = "/Users/x/proj"


def edit_result(uuid, ts, tuid, added=0, removed=0, **tur):
    """The user record that answers an edit call: its own diff carries `added`
    '+' lines and `removed` '-' lines (and context), plus SENTINEL text that
    must never be kept."""
    lines = [" " + SENTINEL] + ["+" + SENTINEL] * added + ["-" + SENTINEL] * removed
    result = {"filePath": CWD + "/a.py", "oldString": SENTINEL, "newString": SENTINEL,
              "structuredPatch": [{"oldStart": 1, "newStart": 1, "oldLines": 1, "newLines": 1,
                                   "lines": lines}]}
    result.update(tur)
    return {"type": "user", "uuid": uuid, "timestamp": ts, "cwd": CWD,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tuid, "content": SENTINEL}]},
            "toolUseResult": result}


def edit_pair(n, ts, added=0, removed=0, name="Edit", **tur):
    """(Edit call, its result) with ids derived from `n`."""
    call = _tool_use(f"em{n}", ts, f"et{n}", name, {"file_path": CWD + "/a.py"})
    call["cwd"] = CWD
    return [call, edit_result(f"er{n}", ts, f"et{n}", added, removed, **tur)]


class _Scanned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.n = 0

    def scan(self, files, subagents=None, tz=UTC):
        """files: {session id: records}; subagents: {(session id, agent id): records}."""
        root = os.path.join(self.tmp, "projects")
        self.n += 1
        proj = os.path.join(root, "-Users-x-proj")
        for sid, records in files.items():
            _write(os.path.join(proj, sid + ".jsonl"), records)
        for (sid, agent), records in (subagents or {}).items():
            _write(os.path.join(proj, sid, "subagents", f"agent-{agent}.jsonl"), records)
        self.cache = FileCache(os.path.join(self.tmp, f"c{self.n}.json"))
        return scan(root, cache=self.cache, tz=tz)

    def parse(self, records, kind="main"):
        path = os.path.join(self.tmp, f"p{len(os.listdir(self.tmp))}.jsonl")
        _write(path, records)
        return parse_file(path, kind)

    def session(self, payload, sid):
        return next(s for s in payload["sessions"] if s["id"] == sid)


class TestPermissionModes(_Scanned):
    def test_oe1_modes_come_from_user_records_and_permission_mode_records(self):
        facts = self.parse([
            _prompt("u1", _ts(0), mode="auto"), _prompt("u2", _ts(1), mode="auto"),
            _prompt("u3", _ts(2), mode="plan"),
            {"type": "permission-mode", "permissionMode": "acceptEdits", "timestamp": _ts(3)},
            {"type": "mode", "mode": "normal", "timestamp": _ts(4)},
        ])
        self.assertEqual(dict(facts.permission_modes), {"auto": 2, "plan": 1, "acceptEdits": 1})

    def test_oe1_the_export_column_is_the_dominant_real_mode(self):
        payload = self.scan({"s": [_prompt("u1", _ts(0), mode="auto"), _response("m1", _ts(1)),
                                   {"type": "mode", "mode": "normal", "timestamp": _ts(2)}]})
        self.assertEqual(self.session(payload, "s")["permission_modes"], {"auto": 1})
        sheet = export_mod.session_rows(payload, PERSON)
        self.assertEqual(dict(zip(export_mod.SESSION_COLUMNS, sheet[0]))["permission_mode"], "auto")


class TestLinesPerEdit(_Scanned):
    def test_oe2_lines_are_counted_from_each_edits_diff_and_dated_by_the_edit(self):
        records = ([_prompt("p", _ts(0, day=1))]
                   + edit_pair(1, _ts(1, day=1), added=3, removed=1)
                   + edit_pair(2, _ts(1, day=5), added=10, removed=2))
        payload = self.scan({"s": records})
        self.assertEqual(sorted((r[0][:10], r[2], r[3]) for r in payload["lines"]),
                         [("2026-02-01", 3, 1), ("2026-02-05", 10, 2)])
        s = self.session(payload, "s")
        self.assertEqual((s["lines_added"], s["lines_removed"], s["lines_source"]), (13, 3, "edits"))

    def test_oe2_a_created_file_counts_its_content_lines(self):
        records = edit_pair(1, _ts(1), name="Write", type="create",
                            content="a\nb\nc\n", structuredPatch=[])
        records += edit_pair(2, _ts(2), name="Write", type="create", content="x\ny",
                             structuredPatch=[])
        payload = self.scan({"s": records})
        self.assertEqual(self.session(payload, "s")["lines_added"], 5)

    def test_oe2_a_failed_edit_and_a_session_without_edits_have_no_lines(self):
        failed = edit_pair(1, _ts(1), added=4)
        failed[1]["message"]["content"][0]["is_error"] = True
        payload = self.scan({"s": failed, "t": [_response("mt", _ts(3))]})
        for sid in ("s", "t"):
            s = self.session(payload, sid)
            self.assertIsNone(s["lines_added"])
            self.assertIsNone(s["lines_removed"])
        self.assertEqual(payload["lines"], [])

    def test_oe2_an_edit_repeated_by_a_fork_copy_counts_once(self):
        prefix = edit_pair(1, _ts(1), added=7)
        original = prefix + [_response("m9", _ts(5))]
        payload = self.scan({"orig": original, "copy": list(prefix)})
        self.assertEqual(sum(r[2] for r in payload["lines"]), 7)

    def test_oe2_a_subagents_edits_belong_to_its_parent_session(self):
        payload = self.scan({"s": [_response("m1", _ts(0))]},
                            subagents={("s", "a1"): edit_pair(1, _ts(2), added=6)})
        self.assertEqual(self.session(payload, "s")["lines_added"], 6)

    def test_oe2_only_the_two_numbers_are_kept(self):
        facts = self.parse(edit_pair(1, _ts(1), added=2, removed=1))
        self.assertEqual(facts.edit_lines, [["et1", _ts(1), 2, 1]])
        self.cache.save() if hasattr(self, "cache") else None
        payload = self.scan({"s": edit_pair(1, _ts(1), added=2)})
        self.cache.save()
        with open(self.cache.path, encoding="utf-8") as fh:
            self.assertNotIn(SENTINEL, fh.read())
        self.assertNotIn(SENTINEL, json.dumps(payload))

    def test_ts1_a_counters_line_totals_are_no_longer_used(self):
        state = {"type": "cost-state", "totalCostUSD": 1.0, "totalLinesAdded": 999,
                 "totalLinesRemoved": 9, "startTime": 5}
        payload = self.scan({"s": [_response("m1", _ts(0)), state]})
        self.assertIsNone(self.session(payload, "s")["lines_added"])
        self.assertEqual(payload["lines"], [])

    def test_ts1_an_empty_counter_on_a_session_with_requests_is_no_counter(self):
        empty = {"type": "cost-state", "totalCostUSD": 0, "startTime": 5}
        payload = self.scan({"s": [_response("m1", _ts(0), inp=1000), empty]})
        s = self.session(payload, "s")
        self.assertIsNone(s["cli_cost"])
        self.assertIsNone(s["cost_diverges"])
        self.assertIsNone(s["est_cost_unlogged"])


class TestCostDivergesInTheCountersWindow(_Scanned):
    def _state(self, cost, start_ms, ts):
        return {"type": "cost-state", "timestamp": ts, "totalCostUSD": cost,
                "startTime": start_ms, "modelUsage": {"claude-sonnet-5": {
                    "inputTokens": 100, "outputTokens": 10, "costUSD": cost}}}

    def test_ts5_a_counter_that_started_at_a_resume_is_compared_with_its_own_window(self):
        # Two requests, two hours apart. The counter started between them, so it
        # saw only the second: its cost is that request's estimate and must not
        # be flagged against the whole session (twice that).
        start_ms = int(datetime.datetime(2026, 2, 1, 11, 30, tzinfo=UTC).timestamp() * 1000)
        first = _response("m1", _ts(0, hour=10), inp=100, out=10)
        second = _response("m2", _ts(0, hour=12), inp=100, out=10)
        one_request = self.scan({"plain": [first, second]})["usage"][0][13]
        state = self._state(round(one_request, 6), start_ms, _ts(5, hour=12))
        s = self.session(self.scan({"s": [first, second, state]}), "s")
        self.assertFalse(s["cost_diverges"], (s["cli_cost"], s["cli_models"]))

    def test_ts5_a_counter_far_from_its_window_is_flagged(self):
        payload = self.scan({"s": [_response("m1", _ts(0), inp=100, out=10),
                                   self._state(50.0, 1, _ts(5))]})
        self.assertTrue(self.session(payload, "s")["cost_diverges"])


class TestRefusals(_Scanned):
    def _refused(self):
        denied = _tool_result("d1", _ts(3), "t-bash", is_error=True)
        denied["toolDenialKind"] = "user_rejected"
        return [_tool_use("m1", _ts(2), "t-bash", "Bash", {"command": "git commit -m x"}), denied,
                _tool_use("m2", _ts(4), "t-real", "Bash", {"command": "false"}),
                _tool_result("d2", _ts(5), "t-real", is_error=True)]

    def test_hb14_a_refusal_is_not_an_error_and_not_a_streak(self):
        facts = self.parse(self._refused())
        self.assertEqual(facts.tool_errors, {"t-bash": False, "t-real": True})
        self.assertEqual(facts.tool_refused, ["t-bash"])
        self.assertEqual(facts.error_streaks, [])  # one real error, not two

    def test_hb14_the_payload_counts_the_call_but_not_an_error(self):
        payload = self.scan({"s": self._refused()})
        rows = {tuple(r[2:5]) for r in payload["tools"]}
        self.assertEqual(rows, {("Bash", 2, 1)})
        self.assertEqual(self.session(payload, "s")["tool_errors"], 1)
        # the refused commit is not a commit
        self.assertEqual(payload["health"]["checks"], [])

    def test_hb7_a_failed_commit_is_not_a_commit_but_a_failed_test_is_a_check(self):
        records = [
            _tool_use("m1", _ts(1), "t-c", "Bash", {"command": "git commit -m x"}),
            _tool_result("r1", _ts(2), "t-c", is_error=True),
            _tool_use("m2", _ts(3), "t-t", "Bash", {"command": "pytest"}),
            _tool_result("r2", _ts(4), "t-t", is_error=True),
        ]
        payload = self.scan({"s": records})
        self.assertEqual([c[2] for c in payload["health"]["checks"]], ["test"])


class TestCallingModelAndSkills(_Scanned):
    def _call(self, mid, ts, tuid, model, name="Read", inp=None):
        rec = _tool_use(mid, ts, tuid, name, inp or {"file_path": "x"})
        rec["message"]["model"] = model
        return rec

    def test_ts2_tool_rows_carry_the_model_that_made_the_call(self):
        records = [self._call("m1", _ts(1), "t1", "claude-opus-5"),
                   self._call("m2", _ts(2), "t2", "claude-haiku-5"),
                   self._call("m3", _ts(3), "t3", "claude-haiku-5")]
        payload = self.scan({"s": records})
        got = {(r[2], r[6]): r[3] for r in payload["tools"]}
        self.assertEqual(got, {("Read", "claude-opus-5"): 1, ("Read", "claude-haiku-5"): 2})
        self.assertEqual(len(payload["tools"][0]), 7)

    def test_ts2_skill_calls_carry_the_model_and_how_they_started(self):
        skill = self._call("m1", _ts(1), "t1", "claude-opus-5", "Skill", {"skill": "dataviz"})
        payload = self.scan({"s": [skill]})
        row, = payload["skillCalls"]
        self.assertEqual(row[2:], ["dataviz", 1, "claude-opus-5", "tool"])

    def test_sa8_an_errored_or_refused_skill_call_is_not_an_invocation(self):
        failed = self._call("m1", _ts(1), "t1", "claude-opus-5", "Skill", {"skill": "dataviz"})
        refused_call = self._call("m2", _ts(3), "t2", "claude-opus-5", "Skill", {"skill": "pdf"})
        refused = _tool_result("r2", _ts(4), "t2", is_error=True)
        refused["toolDenialKind"] = "user_rejected"
        ok = self._call("m3", _ts(5), "t3", "claude-opus-5", "Skill", {"skill": "docx"})
        payload = self.scan({"s": [failed, _tool_result("r1", _ts(2), "t1", is_error=True),
                                   refused_call, refused, ok]})
        self.assertEqual([r[2] for r in payload["skillCalls"]], ["docx"])

    def test_sa3_a_slash_command_that_names_a_known_skill_is_an_invocation(self):
        def command(uuid, ts, name):
            return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {
                "role": "user", "content":
                # The shape Claude Code writes: the message element, then the name.
                f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>\n"
                f"<command-args>{SENTINEL}</command-args>"}}
        attributed = _response("m2", _ts(2))
        attributed["attributionSkill"] = "next-task"
        answer = _response("m1", _ts(1))
        records = [command("c1", _ts(0), "next-task"), answer, attributed,
                   command("c2", _ts(3), "model"), _response("m3", _ts(4))]
        payload = self.scan({"s": records})
        rows = [r for r in payload["skillCalls"] if r[5] == "command"]
        self.assertEqual([(r[2], r[3], r[4]) for r in rows], [("next-task", 1, "claude-sonnet-5")])
        self.assertNotIn(SENTINEL, json.dumps(payload))
        self.cache.save()
        with open(self.cache.path, encoding="utf-8") as fh:
            self.assertNotIn(SENTINEL, fh.read())

    def test_sa3_a_command_copied_into_a_fork_counts_once(self):
        cmd = {"type": "user", "uuid": "c1", "timestamp": _ts(0),
               "message": {"role": "user", "content": "<command-name>/next-task</command-name>"}}
        reply = _response("m1", _ts(1))
        reply["attributionSkill"] = "next-task"
        original = [cmd, reply, _response("m2", _ts(2))]
        payload = self.scan({"orig": original, "fork": [cmd, reply]})
        self.assertEqual(sum(r[3] for r in payload["skillCalls"] if r[5] == "command"), 1)


class TestSubagents(_Scanned):
    def test_sa5_a_subagent_copied_under_two_parents_is_one_subagent(self):
        sub = [_response("sub-1", _ts(2), out=30)]
        files = {"p1": [_response("m1", _ts(0))], "p2": [_response("m2", _ts(1))]}
        payload = self.scan(files, subagents={("p1", "aaa"): sub, ("p2", "aaa"): sub,
                                              ("p1", "bbb"): [_response("sub-2", _ts(3))]})
        self.assertEqual(payload["summary"]["total_subagents"], 2)
        self.assertEqual(len(payload["agents"]), 2)
        self.assertEqual(sum(s["n_subagents"] for s in payload["sessions"]), 2)
        copy, = [a for a in payload["agents"] if a["agent_id"].endswith("aaa")]
        self.assertEqual(copy["requests"], 1)  # the copy that owns the request is kept

    def test_ts7_tool_calls_include_subagents_and_main_is_separate(self):
        main = [_tool_use("m1", _ts(0), "t1", "Read", {"file_path": "x"})]
        sub = [_tool_use("s1", _ts(1), "t2", "Read", {"file_path": "x"}),
               _tool_use("s2", _ts(2), "t3", "Read", {"file_path": "x"})]
        payload = self.scan({"s": main}, subagents={("s", "a1"): sub})
        s = self.session(payload, "s")
        self.assertEqual((s["tool_calls"], s["main_tool_calls"]), (3, 1))
        # the export's sessions sheet agrees with the summary and the payload
        sheet = dict(zip(export_mod.SESSION_COLUMNS, export_mod.session_rows(payload, PERSON)[0]))
        self.assertEqual(sheet["tool_calls"], 3)
        total = sum(dict(zip(export_mod.SUMMARY_COLUMNS, r))["tool_calls"]
                    for r in export_mod.summary_rows(payload, PERSON))
        self.assertEqual(total, 3)


class TestForkActiveTime(_Scanned):
    def test_ts12_a_fork_copy_has_no_active_time_from_uuidless_records(self):
        original = [_prompt("p1", _ts(0)), _response("m1", _ts(1)), _response("m2", _ts(2))]
        queue = [{"type": "queue-operation", "timestamp": _ts(30 + i)} for i in range(3)]
        payload = self.scan({"orig": original, "copy": original[:2] + queue})
        copy = self.session(payload, "copy")
        self.assertTrue(copy["derived"] or copy["requests"] == 0)
        self.assertEqual(copy["active_ms"], 0)
        self.assertEqual(self.session(payload, "orig")["active_ms"], 120_000)


class TestPullRequests(_Scanned):
    def _link(self, ts, n=7):
        return {"type": "pr-link", "timestamp": ts, "prNumber": n, "prRepository": "o/r",
                "prUrl": f"https://github.com/o/r/pull/{n}"}

    def test_ts4_a_pr_is_dated_and_attributed_by_its_earliest_link(self):
        # `big` has more requests so it owns the PR under the old rule; `early`
        # linked it first.
        big = [_response(f"b{i}", _ts(i, day=3)) for i in range(4)] + [self._link(_ts(9, day=9))]
        early = [_response("e1", _ts(0, day=2)), self._link(_ts(5, day=2))]
        payload = self.scan({"big": big, "early": early})
        pr, = payload["prs"]
        self.assertEqual((pr["session_id"], pr["date"]), ("early", "2026-02-02"))
        # each session lists every PR it linked, and the row names the owner
        self.assertEqual([p["url"] for p in self.session(payload, "big")["prs"]], [pr["url"]])
        self.assertEqual([p["url"] for p in self.session(payload, "early")["prs"]], [pr["url"]])
        # ...but the export counts it once
        sheets = [dict(zip(export_mod.SESSION_COLUMNS, r))
                  for r in export_mod.session_rows(payload, PERSON)]
        self.assertEqual(sum(r["prs_opened"] for r in sheets), 1)

    def test_ts4_a_continuation_that_only_repeats_the_link_does_not_list_it(self):
        prefix = [_prompt("p1", _ts(0)), _response("m1", _ts(1)), self._link(_ts(2))]
        original = prefix + [_response("m2", _ts(3)), _response("m3", _ts(4))]
        cont = prefix + [_prompt("p2", _ts(30)), _response("c1", _ts(31))]
        payload = self.scan({"orig": original, "cont": cont})
        self.assertEqual(self.session(payload, "cont")["prs"], [])
        self.assertEqual(len(self.session(payload, "orig")["prs"]), 1)


class TestEditsOutsideTheSession(unittest.TestCase):
    def test_hb1_scratch_and_outside_paths_are_not_code_edits(self):
        cwds = (CWD, None)
        for path in ("/private/tmp/claude-502/x/scratchpad/plot.py", "/tmp/a.py",
                     "/Users/x/other-project/a.py", "/Users/x/proj/art/logo.svg",
                     "/Users/x/proj/notes.md", "/Users/x/.claude/projects/m/memory.md"):
            self.assertTrue(health.is_note_edit(path, cwds), path)
        for path in ("/Users/x/proj/src/a.py", "/Users/x/proj/index.html", "src/a.py"):
            self.assertFalse(health.is_note_edit(path, cwds), path)

    def test_hb1_with_no_folder_known_only_temp_locations_count(self):
        self.assertTrue(health.is_note_edit("/tmp/a.py"))
        self.assertTrue(health.is_note_edit("/private/var/folders/ab/x/T/a.py"))
        self.assertFalse(health.is_note_edit("/Users/x/proj/a.py"))

    def test_hb1_a_project_that_lives_in_tmp_still_has_code_edits(self):
        self.assertFalse(health.is_note_edit("/tmp/work/a.py", ("/tmp/work",)))

    def test_hb1_the_session_is_unchecked_only_for_code_inside_its_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            def session(path):
                call = _tool_use("m1", _ts(1), "t1", "Write", {"file_path": path})
                call["cwd"] = CWD
                path_ = os.path.join(tmp, "s.jsonl")
                _write(path_, [_response("m0", _ts(0)), call])
                return parse_file(path_, "main")
            self.assertEqual(session("/tmp/scratch.py").note_edits, ["t1"])
            self.assertEqual(session(CWD + "/a.py").note_edits, [])


class TestClassifier(unittest.TestCase):
    def check(self, cases):
        for command, expected in cases.items():
            self.assertEqual(health.classify_command(command), expected, command)

    def test_hb2_commands_the_old_classifier_missed(self):
        self.check({
            "for f in a b; do pytest $f; done": ["test"],
            '"$PY" -m pytest tests': ["test"],
            "$PY -m unittest discover": ["test"],
            "VAR=$(mktemp -d) python3 -m unittest discover -s tests": ["test"],
            "CLAUDE_USAGE_DATA_DIR=$(mktemp -d) python3 -m unittest discover": ["test"],
            "if pytest -q; then echo ok; fi": ["test"],
        })

    def test_hb2_a_long_command_is_read_to_its_end(self):
        pad = "echo " + "x" * 6000
        self.assertEqual(health.classify_command(pad + " && pytest -q"), ["test"])
        self.assertEqual(health.classify_command(pad + "\n" + pad + "; gh pr create --fill"), ["pr"])

    def test_hb7_quoted_operators_do_not_split_a_command(self):
        self.check({
            'git commit -m "fix; make build pass"': ["commit"],
            "git commit -m 'run pytest && npm run build'": ["commit"],
            'echo "a | b"; pytest': ["test"],
            "echo don't; pytest": ["test"],  # an unpaired quote is just a character
        })

    def test_hb7_more_runners(self):
        self.check({
            'bash -c "cd web && pytest -q"': ["test"],
            "sh -c 'npm run build'": ["build"],
            "pnpm vitest run": ["test"],
            "yarn jest --ci": ["test"],
            "cargo nextest run": ["test"],
            "bin/rails test": ["test"],
            "py -m pytest": ["test"],
            "vue-tsc --noEmit": ["lint"],
            "pre-commit run --all-files": ["lint"],
            "cmake --build build": ["build"],
        })

    def test_hb7_watch_mode_is_not_a_check(self):
        self.check({"jest --watch": [], "npx vitest watch": [], "jest --watchAll": [],
                    "npm run test -- --watch=false": ["test"], "jest --ci": ["test"]})


class TestCompactionRebuilds(_Scanned):
    def test_hb8_the_first_request_after_a_compaction_is_not_a_cache_miss(self):
        records = [_response("m1", _ts(0), inp=10, read=1000),
                   {"type": "system", "subtype": "compact_boundary", "uuid": "cb", "timestamp": _ts(5),
                    "compactMetadata": {"preTokens": 5000, "trigger": "auto"}},
                   _response("m2", _ts(6), inp=10, w5=500_000, miss={"type": "previous_message_not_found"}),
                   _response("m3", _ts(7), inp=10, w5=400_000, miss={"type": "previous_message_not_found"})]
        payload = self.scan({"s": records})
        misses = payload["health"]["cache_misses"]
        self.assertEqual(sum(r[4] for r in misses), 1)  # m3 only


class TestFirstRecordedNote(_Scanned):
    def test_hb9_the_payload_says_where_the_versions_come_from(self):
        payload = self.scan({"s": [_response("m1", _ts(0))]})
        first = payload["health"]["first_recorded"]
        self.assertEqual(first["denials"], health.FIRST_RECORDED["denials"])
        self.assertIn("first this tool's author saw", first["note"])


class TestExport(_Scanned):
    def test_ex4_summary_and_sessions_sheets_agree_on_prompts_and_sessions_started(self):
        # A transcript that got no reply is not a session in either sheet; neither are its prompts.
        payload = self.scan({"s": [_prompt("p1", _ts(0)), _response("m1", _ts(1))],
                             "noreply": [_prompt("p9", _ts(5))]})
        summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                   for r in export_mod.summary_rows(payload, PERSON)]
        sessions = [dict(zip(export_mod.SESSION_COLUMNS, r))
                    for r in export_mod.session_rows(payload, PERSON)]
        self.assertEqual(sum(r["sessions_started"] for r in summary), 1)
        self.assertEqual(sum(r["sessions_plan_mode"] for r in summary), 0)

    def test_ex4_a_fork_copy_that_owns_nothing_is_left_out_of_both_sheets(self):
        original = [_prompt("p1", _ts(0)), _response("m1", _ts(1)), _response("m2", _ts(2))]
        copy = original[:2] + [_prompt("p2", _ts(20), mode="plan")]
        payload = self.scan({"orig": original, "copy": copy})
        summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                   for r in export_mod.summary_rows(payload, PERSON)]
        sessions = [dict(zip(export_mod.SESSION_COLUMNS, r))
                    for r in export_mod.session_rows(payload, PERSON)]
        self.assertEqual(sum(r["prompts"] or 0 for r in summary),
                         sum(r["prompts"] or 0 for r in sessions))

    def test_tm8_zero_request_transcripts_do_not_feed_the_sessions_shares(self):
        # A plan-mode prompt that never got a reply: no request, so not a session.
        payload = self.scan({"s": [_prompt("p1", _ts(0)), _response("m1", _ts(1))],
                             "noreply": [_prompt("pp", _ts(5), mode="plan")]})
        self.assertEqual(self.session(payload, "noreply")["requests"], 0)
        self.assertTrue(self.session(payload, "noreply")["health"]["plan_mode"])
        summary = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                   for r in export_mod.summary_rows(payload, PERSON)]
        self.assertEqual(sum(r["sessions_started"] for r in summary), 1)
        self.assertEqual(sum(r["sessions_plan_mode"] for r in summary), 0)

    def test_sp2_an_export_with_no_logs_covers_thirty_days(self):
        empty = {"usage": [], "sessions": [], "tools": [], "friction": [], "limits": [],
                 "prs": [], "agents": []}
        now = datetime.datetime(2026, 3, 31, 12, tzinfo=UTC)
        ctx = export_mod.build_context(empty, PERSON, now=now)
        self.assertEqual((ctx["window_from"], ctx["window_to"]), ("2026-03-02", "2026-03-31"))
        self.assertEqual(ctx["person_active_days"], 0)
        self.assertIsNone(ctx["last_active_date"])
        self.assertEqual(ctx["seat_idle"], "yes")
        # an explicit --since still wins
        ctx = export_mod.build_context(empty, PERSON, since="2026-03-20", now=now)
        self.assertEqual(ctx["window_from"], "2026-03-20")

    def test_schema_version_is_13(self):
        self.assertEqual(context_mod.EXPORT_SCHEMA_VERSION, 13)
        self.assertGreaterEqual(parser.FACTS_VERSION, 19)


class TestRedact(unittest.TestCase):
    def test_oe15_head_stays_and_the_build_time_loses_its_offset(self):
        payload = {"generated_at": "2026-10-29T10:00:00-04:00",
                   "sessions": [{"id": "s", "session_id": "s", "project": "/p", "title": "t",
                                 "branches": {"HEAD": 3, "feature/secret": 2}, "prs": []}],
                   "agents": [], "prs": [], "dims": {"projects": ["/p"]}}
        out = collect._redact(payload)
        self.assertEqual(out["sessions"][0]["branches"], {"HEAD": 3, "branch-1": 2})
        self.assertEqual(out["generated_at"], "2026-10-29T14:00:00Z")


class TestCounterSpan(_Scanned):
    def test_rc1_the_counter_owner_carries_the_span_of_every_transcript_on_it(self):
        def state(cost):
            return {"type": "cost-state", "totalCostUSD": cost, "startTime": 5,
                    "modelUsage": {"claude-sonnet-5": {"inputTokens": 1, "costUSD": cost}}}
        owner = [_response(f"o{i}", _ts(i, day=1)) for i in range(3)] + [state(1.0)]
        branch = [_response("b1", _ts(0, day=9)), state(1.1)]
        payload = self.scan({"owner": owner, "branch": branch})
        s = self.session(payload, "owner")
        self.assertEqual(s["cli_counter_shared_with"], ["branch"])
        self.assertEqual(s["cli_counter_start"][:10], "2026-02-01")
        self.assertEqual(s["cli_counter_end"][:10], "2026-02-09")
        self.assertIsNone(self.session(payload, "branch")["cli_counter_start"])


class TestCacheWriteAuthority(unittest.TestCase):
    def test_ex6_the_five_minute_and_one_hour_split_is_authoritative_when_present(self):
        rec = {"cache_write_5m": 100_000, "cache_write_1h": 883_102,
               "cache_write_fallback": 631_768}
        self.assertEqual(aggregate._effective_cache_writes(rec), (100_000, 883_102))
        flat_only = {"cache_write_5m": 0, "cache_write_1h": 0, "cache_write_fallback": 631_768}
        self.assertEqual(aggregate._effective_cache_writes(flat_only), (631_768, 0))


if __name__ == "__main__":
    unittest.main()
