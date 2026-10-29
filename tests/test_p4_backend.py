"""Phase 4b backend fixes: what the re-check found after the Phase 3 fixes.
Each test names its item (B1-B10)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import collect
from claude_usage import export as export_mod, health, parser, scan as scan_mod
from claude_usage.parser import parse_file
from tests.test_health import SENTINEL, _prompt, _response, _tool_result, _tool_use, _ts, _write
from tests.test_p3_backend import CWD, PERSON, _Scanned, edit_pair


def _result(uuid, ts, tuid, is_error=False, **extra):
    """A tool result with no `toolUseResult`, as a subagent's is."""
    rec = _tool_result(uuid, ts, tuid, is_error=is_error)
    rec.update(extra)
    return rec


def _call(n, ts, name, inp, cwd=CWD):
    rec = _tool_use(f"cm{n}", ts, f"ct{n}", name, inp)
    rec["cwd"] = cwd
    return rec


# --------------------------------------------------------------------------- #
# B1: a subagent's edits are counted from the call's own input
# --------------------------------------------------------------------------- #

class TestB1SubagentEdits(_Scanned):
    def counted(self, name, inp, is_error=False, **extra):
        facts = self.parse([_call(1, _ts(1), name, inp), _result("r1", _ts(2), "ct1", is_error, **extra)],
                           kind="sub")
        return [row[2:] for row in facts.edit_lines]

    def test_b1_edit_counts_inserted_and_deleted_lines(self):
        old = "a\nb\nc\nd\n"
        new = "a\nB\nB2\nc\n"       # b -> B, B2 (1 removed, 2 added); d removed
        self.assertEqual(self.counted("Edit", {"file_path": CWD + "/a.py", "old_string": old,
                                               "new_string": new}), [[2, 2]])

    def test_b1_edit_with_an_empty_old_string_adds_every_line(self):
        self.assertEqual(self.counted("Edit", {"file_path": CWD + "/a.py", "old_string": "",
                                               "new_string": "x\ny\nz"}), [[3, 0]])

    def test_b1_a_huge_edit_still_finishes_quickly(self):
        old = "\n".join("old%d" % i for i in range(20000))
        new = "\n".join("new%d" % i for i in range(20000))
        start = time.time()
        got = self.counted("Edit", {"file_path": CWD + "/a.py", "old_string": old, "new_string": new})
        self.assertLess(time.time() - start, 0.5)
        self.assertEqual(got, [[20000, 20000]])

    def test_b1_multiedit_sums_its_edits(self):
        edits = [{"old_string": "a", "new_string": "a\nb"}, {"old_string": "x\ny", "new_string": "x"}]
        self.assertEqual(self.counted("MultiEdit", {"file_path": CWD + "/a.py", "edits": edits}), [[1, 1]])

    def test_b1_write_counts_its_content_lines(self):
        self.assertEqual(self.counted("Write", {"file_path": CWD + "/a.py", "content": "1\n2\n3\n"}),
                         [[3, 0]])

    def test_b1_notebook_edit_counts_the_new_source(self):
        self.assertEqual(self.counted("NotebookEdit", {"notebook_path": CWD + "/n.ipynb",
                                                       "new_source": "a\nb", "edit_mode": "replace"}),
                         [[2, 0]])
        self.assertEqual(self.counted("NotebookEdit", {"notebook_path": CWD + "/n.ipynb",
                                                       "new_source": "", "edit_mode": "delete"}), [])

    def test_b1_an_error_or_a_refusal_counts_nothing(self):
        inp = {"file_path": CWD + "/a.py", "old_string": "a", "new_string": "b\nc"}
        self.assertEqual(self.counted("Edit", inp, is_error=True), [])
        self.assertEqual(self.counted("Edit", inp, is_error=True, toolDenialKind="user_rejected"), [])

    def test_b1_the_result_diff_wins_when_there_is_one(self):
        records = edit_pair(1, _ts(1), added=5, removed=2)
        records[0]["message"]["content"][0]["input"].update(old_string="a", new_string="b")
        facts = self.parse(records, kind="sub")
        self.assertEqual([r[2:] for r in facts.edit_lines], [[5, 2]])

    def test_b1_two_results_in_one_record_are_each_counted_from_their_calls(self):
        calls = [_call(1, _ts(1), "Write", {"file_path": CWD + "/a.py", "content": "a\nb"}),
                 _call(2, _ts(2), "Write", {"file_path": CWD + "/b.py", "content": "c"})]
        both = {"type": "user", "uuid": "r", "timestamp": _ts(3), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ct1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "ct2", "content": "ok"}]}}
        facts = self.parse(calls + [both], kind="sub")
        self.assertEqual(sorted(r[0:1] + r[2:] for r in facts.edit_lines), [["ct1", 2, 0], ["ct2", 1, 0]])

    def test_b1_a_subagents_edit_reaches_the_session_and_only_numbers_are_kept(self):
        sub = [_call(1, _ts(2), "Edit", {"file_path": CWD + "/a.py", "old_string": SENTINEL,
                                         "new_string": SENTINEL + "\n" + SENTINEL}),
               _result("r1", _ts(3), "ct1")]
        payload = self.scan({"s": [_response("m1", _ts(0))]}, subagents={("s", "a1"): sub})
        s = self.session(payload, "s")
        self.assertEqual((s["lines_added"], s["lines_removed"]), (1, 0))
        self.assertEqual(payload["lines"][0][2:], [1, 0])
        self.assertNotIn(SENTINEL, json.dumps(payload))
        self.cache.save()
        with open(self.cache.path, encoding="utf-8") as fh:
            self.assertNotIn(SENTINEL, fh.read())

    def test_b1_the_same_call_copied_into_two_files_counts_once(self):
        sub = [_call(1, _ts(2), "Write", {"file_path": CWD + "/a.py", "content": "a\nb\nc"}),
               _result("r1", _ts(3), "ct1")]
        payload = self.scan({"p1": [_response("m1", _ts(0))], "p2": [_response("m2", _ts(1))]},
                            subagents={("p1", "a1"): sub, ("p2", "a1"): sub})
        self.assertEqual(sum(r[2] for r in payload["lines"]), 3)


# --------------------------------------------------------------------------- #
# B2: a slash command's record starts with <command-message>
# --------------------------------------------------------------------------- #

class TestB2CommandShape(_Scanned):
    def name(self, text):
        facts = self.parse([{"type": "user", "uuid": "u", "timestamp": _ts(0),
                             "message": {"role": "user", "content": text}}])
        return [c["name"] for c in facts.command_invocations]

    def test_b2_the_real_shape_is_read_and_the_arguments_are_not(self):
        text = (f"<command-message>next-task</command-message>\n<command-name>/next-task</command-name>"
                f"\n<command-args>{SENTINEL}</command-args>")
        self.assertEqual(self.name(text), ["next-task"])
        facts = self.parse([{"type": "user", "uuid": "u", "timestamp": _ts(0),
                             "message": {"role": "user", "content": text}}])
        self.assertNotIn(SENTINEL, json.dumps(facts.command_invocations))

    def test_b2_the_older_shape_and_a_block_list_still_work(self):
        self.assertEqual(self.name("<command-name>/model</command-name>"), ["model"])
        self.assertEqual(self.name([{"type": "text", "text":
                                     "<command-message>x</command-message><command-name>/x</command-name>"}]),
                         ["x"])

    def test_b2_text_that_only_mentions_a_command_is_not_one(self):
        self.assertEqual(self.name("please run <command-name>/x</command-name>"), [])
        self.assertEqual(self.name("<command-message>" + "y" * 400 + "</command-message><command-name>/x"
                                   "</command-name>"), [])

    def test_b2_the_real_shape_makes_a_skill_invocation(self):
        cmd = {"type": "user", "uuid": "c1", "timestamp": _ts(0), "message": {"role": "user", "content":
               "<command-message>next-task</command-message>\n<command-name>/next-task</command-name>"}}
        reply = _response("m1", _ts(1))
        reply["attributionSkill"] = "next-task"
        payload = self.scan({"s": [cmd, reply]})
        self.assertEqual([(r[2], r[5]) for r in payload["skillCalls"]], [("next-task", "command")])


# --------------------------------------------------------------------------- #
# B3: edits inside a Claude Code worktree are code
# --------------------------------------------------------------------------- #

class TestB3Worktrees(unittest.TestCase):
    def test_b3_a_worktree_checkout_is_code(self):
        repo = "/Users/x/proj"
        for path in (repo + "/.claude/worktrees/feat/src/a.py", repo + "/.claude/worktrees/feat/index.html"):
            self.assertFalse(health.is_note_edit(path, (repo,)), path)
            self.assertFalse(health.is_note_edit(path, (repo + "/.claude/worktrees/feat",)), path)

    def test_b3_claude_code_own_files_are_still_notes(self):
        repo = "/Users/x/proj"
        for path in (repo + "/.claude/settings.json", repo + "/.claude/worktrees/feat/.claude/launch.json",
                     repo + "/.claude/worktrees/feat/notes.md", "/Users/x/.claude/projects/p/memory.md"):
            self.assertTrue(health.is_note_edit(path, (repo,)), path)

    def test_b3_a_worktree_edit_in_a_session_log_is_not_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            call = _tool_use("m1", _ts(1), "t1", "Edit", {"file_path": CWD + "/.claude/worktrees/w/a.py"})
            call["cwd"] = CWD
            path = os.path.join(tmp, "s.jsonl")
            _write(path, [_response("m0", _ts(0)), call])
            self.assertEqual(parse_file(path, "main").note_edits, [])


# --------------------------------------------------------------------------- #
# B4: the export's summary lines follow the edit's day
# --------------------------------------------------------------------------- #

class TestB4SummaryLinesByEditWeek(_Scanned):
    def test_b4_lines_land_in_the_week_of_the_edit_not_the_session_start(self):
        records = ([_prompt("p", _ts(0, day=2)), _response("m1", _ts(1, day=2))]
                   + edit_pair(1, _ts(2, day=2), added=3, removed=1)
                   + [_response("m2", _ts(1, day=20))]
                   + edit_pair(2, _ts(2, day=20), added=10, removed=0))
        payload = self.scan({"s": records})
        rows = [dict(zip(export_mod.SUMMARY_COLUMNS, r)) for r in export_mod.summary_rows(payload, PERSON)]
        by_week = {r["week_start"]: r for r in rows if r["lines_added"] not in (None, "")}
        self.assertEqual({w: (r["lines_added"], r["lines_removed"]) for w, r in by_week.items()},
                         {"2026-02-02": (3, 1), "2026-02-16": (10, 0)})
        # one session-week each, with that week's cost beside the lines
        for r in by_week.values():
            self.assertEqual(r["sessions_with_line_data"], 1)
            self.assertGreater(r["cost_with_line_data_usd"], 0)
        # the sessions sheet keeps the whole session
        sheet = dict(zip(export_mod.SESSION_COLUMNS, export_mod.session_rows(payload, PERSON)[0]))
        self.assertEqual(sheet["lines_added"], 13)

    def test_b4_a_date_range_gets_only_the_lines_written_inside_it(self):
        records = ([_prompt("p", _ts(0, day=2)), _response("m1", _ts(1, day=2))]
                   + edit_pair(1, _ts(2, day=2), added=3)
                   + [_response("m2", _ts(1, day=20))] + edit_pair(2, _ts(2, day=20), added=10))
        payload = self.scan({"s": records})
        rows = [dict(zip(export_mod.SUMMARY_COLUMNS, r))
                for r in export_mod.summary_rows(payload, PERSON, since="2026-02-15", until="2026-02-28")]
        self.assertEqual(sum(r["lines_added"] or 0 for r in rows), 10)


# --------------------------------------------------------------------------- #
# B5: a link is a copy only when the same (PR, time) is held by an earlier file
# --------------------------------------------------------------------------- #

class TestB5PrLinks(_Scanned):
    def _link(self, ts, n=7):
        return {"type": "pr-link", "timestamp": ts, "prNumber": n, "prRepository": "o/r",
                "prUrl": f"https://github.com/o/r/pull/{n}"}

    def test_b5_a_session_that_linked_the_pr_itself_keeps_it(self):
        # `late` starts after `early`'s link, and links the same PR at its own time.
        early = [_response("e1", _ts(0, day=2)), self._link(_ts(5, day=2)), _response("e2", _ts(9, day=2))]
        late = [_response("l1", _ts(0, day=4)), self._link(_ts(5, day=4))]
        # `earlier` started before everyone but linked at another time: still its own link.
        earlier = [_response("x0", _ts(0, day=1)), _response("x1", _ts(1, day=9)),
                   self._link(_ts(2, day=9))]
        payload = self.scan({"early": early, "late": late, "earlier": earlier})
        url = "https://github.com/o/r/pull/7"
        for sid in ("early", "late", "earlier"):
            self.assertEqual([p["url"] for p in self.session(payload, sid)["prs"]], [url], sid)

    def test_b5_a_fork_that_repeats_the_same_record_does_not_list_it(self):
        prefix = [_prompt("p1", _ts(0)), _response("m1", _ts(1)), self._link(_ts(2))]
        original = prefix + [_response("m2", _ts(3)), _response("m3", _ts(4))]
        fork = prefix + [_prompt("p2", _ts(30)), _response("f1", _ts(31))]
        payload = self.scan({"orig": original, "fork": fork})
        self.assertEqual(self.session(payload, "fork")["prs"], [])
        self.assertEqual(len(self.session(payload, "orig")["prs"]), 1)


# --------------------------------------------------------------------------- #
# B6: no catastrophic backtracking in the command classifier
# --------------------------------------------------------------------------- #

class TestB6Timing(unittest.TestCase):
    def fast(self, command):
        start = time.time()
        health.classify_command(command)
        self.assertLess(time.time() - start, 0.5, command[:40])

    def test_b6_a_timeout_with_many_options_returns(self):
        self.fast("timeout" + " -s1" * 40)
        self.fast("timeout" + " -s1" * 40 + " 30 pytest")
        self.fast(";".join(["timeout" + " -k" * 300] * 100))
        self.fast("timeout" + " -s1" * 30000)

    def test_b6_a_long_variable_path_is_not_quadratic(self):
        self.fast("$" + "a" * 100_000)
        self.fast('"$' + "a" * 100_000)
        self.fast("$a" + "." * 100_000 + " -m")
        self.fast(";".join(['"${' + "a" * 500 + "}" + "/" * 490] * 100))

    def test_b6_python_options_do_not_backtrack(self):
        self.fast("python" + " -Xa" * 30000)
        self.fast("python3" + " -I" * 30000)

    def test_b6_the_commands_still_classify(self):
        self.assertEqual(health.classify_command("timeout -s1 30 pytest"), ["test"])
        self.assertEqual(health.classify_command("timeout --signal=KILL -k 5 60 make test"), ["test"])
        self.assertEqual(health.classify_command("$PY -m pytest"), ["test"])
        self.assertEqual(health.classify_command('"${VENV}/bin/python" -m pytest'), ["test"])


# --------------------------------------------------------------------------- #
# B7: a refused or failed edit is not an edit
# --------------------------------------------------------------------------- #

class TestB7FailedEdits(_Scanned):
    def _health(self, records):
        payload = self.scan({"s": records})
        return self.session(payload, "s")["health"]

    def test_b7_a_failed_or_refused_edit_after_the_last_test_leaves_nothing_unchecked(self):
        refused = _result("r3", _ts(7), "ct3", is_error=True, toolDenialKind="user_rejected")
        records = [_prompt("p", _ts(0)),
                   _call(1, _ts(1), "Edit", {"file_path": CWD + "/a.py"}), _result("r1", _ts(2), "ct1"),
                   _tool_use("t", _ts(3), "tu-test", "Bash", {"command": "pytest"}),
                   _result("rt", _ts(4), "tu-test"),
                   _call(2, _ts(5), "Edit", {"file_path": CWD + "/a.py"}),
                   _result("r2", _ts(6), "ct2", is_error=True),
                   _call(3, _ts(6), "Write", {"file_path": CWD + "/b.py"}), refused]
        h = self._health(records)
        self.assertEqual(h["edits"], 1)
        self.assertTrue(h["checked_after_last_edit"])

    def test_b7_a_session_whose_only_edits_failed_made_no_edit(self):
        h = self._health([_prompt("p", _ts(0)), _call(1, _ts(1), "Edit", {"file_path": CWD + "/a.py"}),
                          _result("r1", _ts(2), "ct1", is_error=True)])
        self.assertEqual(h["edits"], 0)
        self.assertIsNone(h["checked_after_last_edit"])


# --------------------------------------------------------------------------- #
# B8: more runners; a watching build is no build
# --------------------------------------------------------------------------- #

class TestB8Classifier(unittest.TestCase):
    def check(self, cases):
        for command, expected in cases.items():
            self.assertEqual(health.classify_command(command), expected, command)

    def test_b8_interpreter_options_before_dash_m(self):
        self.check({
            "python3 -I -m unittest discover -s tests": ["test"],
            "python -X dev -m pytest -q": ["test"],
            "python -W error -m pytest": ["test"],
            "python3 -I -X utf8 -m mypy src": ["lint"],
            "python -O -m build": ["build"],
            "python -m pytest": ["test"],
            "python3 -c 'import pytest'": [],
            "python -I script.py": [],
        })

    def test_b8_npm_short_test_names(self):
        self.check({"npm t": ["test"], "npm tst": ["test"], "pnpm t": ["test"], "npm t -- -u": ["test"],
                    "npm run t": [], "npm tree": [], "npm t-x": []})

    def test_b8_a_watching_script_build_is_not_a_build(self):
        self.check({
            "npm run build -- --watch": [],
            "npm run build --watch": [],
            "npm run build:watch": [],
            "pnpm run build -- --watch=true": [],
            "npm run build": ["build"],
            "npm run build -w packages/app": ["build"],
            "npm run build -- --watch=false": ["build"],
            "yarn build": ["build"],
        })


# --------------------------------------------------------------------------- #
# B9, B10: what the redacted build and the export message say
# --------------------------------------------------------------------------- #

def _doc(name):
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", name), encoding="utf-8") as fh:
        return fh.read()


class TestB9B10Disclosure(unittest.TestCase):
    def test_b9_privacy_says_the_redacted_times_still_show_the_zone(self):
        text = " ".join(_doc("privacy.md").split())
        self.assertIn("work out your time zone", text)

    def test_b9_privacy_says_what_slash_command_names_are_kept(self):
        text = " ".join(_doc("privacy.md").split())
        self.assertIn("the parse cache keeps the name of every slash command you ran", text)

    def test_b10_the_export_message_names_what_the_files_carry(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "projects", "-Users-x-proj")
        _write(os.path.join(root, "s.jsonl"), [_prompt("p", _ts(0)), _response("m", _ts(1))])
        out, data = os.path.join(tmp, "out"), os.path.join(tmp, "data.json")
        buf = io.StringIO()
        with mock.patch.object(collect, "DEFAULT_ROOT", os.path.join(tmp, "projects")), \
                mock.patch.object(collect, "OUT_PATH", __import__("pathlib").Path(data)), \
                mock.patch.object(scan_mod, "DEFAULT_CACHE_PATH", os.path.join(tmp, "c.json")), \
                mock.patch.object(sys, "argv", ["collect.py", "--offline", "--export", out, "--as", "a@b.c"]), \
                contextlib.redirect_stdout(buf):
            collect.main()
        said = " ".join(buf.getvalue().split())
        for word in ("skills", "MCP servers", "tools", "subagent types"):
            self.assertIn(word, said)


if __name__ == "__main__":
    unittest.main()
