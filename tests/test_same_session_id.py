"""BUG-07: one session id can appear in two project folders (a copied or moved
project, a synced home directory). Both transcripts are real work, so neither
may overwrite the other, and each subagent stays with its own folder's parent."""
import json
import os
import shutil
import tempfile
import unittest

import collect
from claude_usage import export
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from claude_usage import team
from tests.test_health import _prompt, _response, _ts, _tool_use, _write
from tests.test_team import TeamFolder, _cost

FOLDER_A, FOLDER_B = "-Users-x-projA", "-Users-x-projB"


def _meta(path, agent_type):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"agentType": agent_type, "description": agent_type}, fh)


def _write_pair(root, folder, tag, mains, subs):
    """dup.jsonl with `mains` requests and one subagent with `subs` requests."""
    proj = os.path.join(root, folder)
    _write(os.path.join(proj, "dup.jsonl"),
           [_response(f"{tag}-m{i}", _ts(i)) for i in range(mains)])
    sub_dir = os.path.join(proj, "dup", "subagents")
    _write(os.path.join(sub_dir, f"agent-{tag}.jsonl"),
           [_response(f"{tag}-s{i}", _ts(30 + i)) for i in range(subs)])
    _meta(os.path.join(sub_dir, f"agent-{tag}.meta.json"), f"type-{tag}")


class TestSameSessionIdInTwoFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "projects")
        _write_pair(self.root, FOLDER_A, "a", mains=1, subs=1)
        _write_pair(self.root, FOLDER_B, "b", mains=2, subs=3)
        self.payload = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c.json")))

    def _sessions_by_project(self):
        return {s["project"]: s for s in self.payload["sessions"]}

    def test_both_transcripts_count_toward_the_total(self):
        self.assertEqual(sum(r[12] for r in self.payload["usage"]), 1 + 1 + 2 + 3)
        self.assertEqual(self.payload["summary"]["total_transcripts"], 2)
        self.assertEqual(self.payload["summary"]["total_sessions"], 2)
        self.assertEqual(self.payload["files_parsed"], 4)

    def test_both_projects_appear(self):
        self.assertEqual(len(self.payload["dims"]["projects"]), 2)
        self.assertEqual(len(self.payload["sessions"]), 2)

    def test_each_session_keeps_its_own_requests_and_subagents(self):
        by_project = self._sessions_by_project()
        a = by_project["/Users/x/projA (decoded)"]
        b = by_project["/Users/x/projB (decoded)"]
        self.assertEqual((a["requests"], a["n_subagents"]), (2, 1))
        self.assertEqual((b["requests"], b["n_subagents"]), (5, 1))
        self.assertGreater(a["est_cost_sub"], 0)
        self.assertGreater(b["est_cost_sub"], a["est_cost_sub"])

    def test_each_subagent_is_attached_to_its_own_folders_parent(self):
        by_project = self._sessions_by_project()
        owner_of = {a["agent_type"]: a["session_id"] for a in self.payload["agents"]}
        self.assertEqual(owner_of["type-a"], by_project["/Users/x/projA (decoded)"]["id"])
        self.assertEqual(owner_of["type-b"], by_project["/Users/x/projB (decoded)"]["id"])

    def test_rows_join_to_distinct_session_keys(self):
        keys = [s["id"] for s in self.payload["sessions"]]
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual({r[1] for r in self.payload["usage"]}, set(keys))

    def test_the_payload_shows_the_real_session_id(self):
        self.assertEqual([s["session_id"] for s in self.payload["sessions"]], ["dup", "dup"])
        self.assertEqual({s["title"] for s in self.payload["sessions"]}, {"dup"})

    def test_a_session_id_seen_once_keeps_its_plain_key(self):
        # The first folder (alphabetically) keeps the bare id, so ids are stable
        # for everyone who has no collision.
        self.assertIn("dup", {s["id"] for s in self.payload["sessions"]})

    def test_the_session_sheet_shows_the_real_id_and_loses_no_session(self):
        rows = export.session_rows(self.payload, ("a@example.com", "A"))
        col = {name: i for i, name in enumerate(export.SESSION_COLUMNS)}
        self.assertEqual([r[col["session_id"]] for r in rows], ["dup", "dup#2"])
        self.assertEqual(sorted(r[col["requests"]] for r in rows), [2, 5])

    def test_redacting_keeps_the_rows_joined_and_drops_the_folder_name(self):
        red = collect._redact(self.payload)
        self.assertNotIn("projB", json.dumps(red))
        self.assertNotIn("-Users-x", json.dumps(red))
        keys = [s["id"] for s in red["sessions"]]
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual({r[1] for r in red["usage"]}, set(keys))
        self.assertEqual({a["session_id"] for a in red["agents"]}, set(keys))
        self.assertEqual({s["title"] for s in red["sessions"]}, {"dup"})

    def test_scan_is_repeatable_from_a_warm_cache(self):
        again = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c.json")))
        self.assertEqual(again["files_cached"], 4)
        self.assertEqual(sorted(s["id"] for s in again["sessions"]),
                         sorted(s["id"] for s in self.payload["sessions"]))
        self.assertEqual(sum(r[12] for r in again["usage"]), 7)


class TestExportKeepsEachRepeatedSessionId(unittest.TestCase):
    """BUG-07 (review): the team merge keys sessions on session_id alone, so an
    export must not write one id on three rows. The first occurrence keeps the
    real id, later ones get "<id>#2", "<id>#3" -- never the folder."""

    PERSON = ("a@example.com", "A")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "projects")
        for folder, tag, n in (("-Users-x-projA", "a", 1), ("-Users-x-projB", "b", 3), ("-Users-x-projC", "c", 2)):
            _write(os.path.join(self.root, folder, "dup.jsonl"),
                   [_prompt(f"{tag}-p", _ts(0, day=3))]
                   + [_tool_use(f"{tag}-m{i}", _ts(i + 1, day=3), f"{tag}-t{i}", "Bash", {"command": "ls"})
                      for i in range(n)]
                   + [{"type": "pr-link", "timestamp": _ts(9, day=3), "prNumber": 1,
                       "prUrl": f"https://github.com/o/{tag}/pull/1", "prRepository": f"o/{tag}"}])
        self.payload = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c.json")))

    def _column(self, rows, columns, name):
        return [r[columns.index(name)] for r in rows]

    def test_the_sessions_sheet_has_a_distinct_id_per_session(self):
        rows = export.session_rows(self.payload, self.PERSON)
        ids = self._column(rows, export.SESSION_COLUMNS, "session_id")
        self.assertEqual(ids, ["dup", "dup#2", "dup#3"])
        self.assertEqual(sorted(self._column(rows, export.SESSION_COLUMNS, "requests")), [1, 2, 3])

    def test_the_folder_never_reaches_an_export_id(self):
        sessions = export.session_rows(self.payload, self.PERSON)
        prs = export.pr_rows(self.payload, self.PERSON)
        ids = (self._column(sessions, export.SESSION_COLUMNS, "session_id")
               + self._column(prs, export.PR_COLUMNS, "session_id"))
        for sid in ids:
            self.assertRegex(sid, r"^dup(#\d+)?$")

    def test_the_prs_sheet_names_the_same_session_ids(self):
        rows = export.pr_rows(self.payload, self.PERSON)
        ids = self._column(rows, export.PR_COLUMNS, "session_id")
        self.assertEqual(sorted(ids), ["dup", "dup#2", "dup#3"])
        sessions = export.session_rows(self.payload, self.PERSON)
        cost = dict(zip(self._column(sessions, export.SESSION_COLUMNS, "session_id"),
                        self._column(sessions, export.SESSION_COLUMNS, "est_cost_usd")))
        for row in rows:
            self.assertEqual(row[export.PR_COLUMNS.index("session_cost_usd")],
                             cost[row[export.PR_COLUMNS.index("session_id")]])

    def test_ids_are_the_same_when_exported_again(self):
        again = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c.json")))
        self.assertEqual(self._column(export.session_rows(again, self.PERSON), export.SESSION_COLUMNS, "session_id"),
                         self._column(export.session_rows(self.payload, self.PERSON), export.SESSION_COLUMNS,
                                      "session_id"))

    def test_a_session_literally_named_like_a_generated_id_is_not_overwritten(self):
        _write(os.path.join(self.root, "-Users-x-projA", "dup#2.jsonl"), [_response("n", _ts(7, day=3))])
        payload = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c2.json")))
        ids = self._column(export.session_rows(payload, self.PERSON), export.SESSION_COLUMNS, "session_id")
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertIn("dup#2", ids)  # the real one keeps its name

    def test_a_derived_copy_that_is_not_exported_does_not_take_a_number(self):
        # Folder A's dup is a copy of folder B's (same requests, fewer of them), so
        # the scan marks A derived and the sheet skips it: the one row is "dup".
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            reqs = [_response(f"m{i}", _ts(i)) for i in range(4)]
            _write(os.path.join(root, FOLDER_A, "dup.jsonl"), reqs[:2])
            _write(os.path.join(root, FOLDER_B, "dup.jsonl"), reqs)
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            self.assertEqual([s["derived"] for s in payload["sessions"]], [True, False])
            rows = export.session_rows(payload, self.PERSON)
            self.assertEqual(self._column(rows, export.SESSION_COLUMNS, "session_id"), ["dup"])
            # A row that points at the skipped copy still shows a real id.
            self.assertEqual(export._export_ids(payload)[payload["sessions"][0]["id"]], "dup")

    def test_a_key_with_no_session_row_shows_its_real_id_not_its_folder(self):
        payload = {"sessions": [{"id": "dup", "session_id": "dup"}], "usage": [],
                   "agents": [{"session_id": "dup@-Users-x-projD", "real_session_id": "dup"}]}
        ids = export._export_ids(payload)
        self.assertEqual(ids["dup@-Users-x-projD"], "dup")

    def test_unique_ids_are_exported_exactly_as_they_were(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            for folder, sid in ((FOLDER_A, "one"), (FOLDER_B, "two")):
                _write(os.path.join(root, folder, sid + ".jsonl"), [_response(sid, _ts(1))])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            ids = self._column(export.session_rows(payload, self.PERSON), export.SESSION_COLUMNS, "session_id")
            self.assertEqual(sorted(ids), ["one", "two"])

    def test_the_team_merge_keeps_all_three_sessions_and_their_cost(self):
        folder = TeamFolder()
        self.addCleanup(folder.close)
        folder.zip(self.payload, "a@example.com", "a@dev.io", "20260210", machine="laptop")
        exports, problems = team.read_exports([folder.dir])
        m = team.merge(exports, problems)
        summary = _cost(m["files"]["summary"][1])
        self.assertEqual(len(m["files"]["sessions"][1]), 3)
        self.assertAlmostEqual(_cost(m["files"]["sessions"][1]), summary, places=5)
        self.assertAlmostEqual(summary, sum(r[13] for r in self.payload["usage"]), places=5)
        self.assertEqual(m["people"][0]["sessions"], 3)


class TestRedactedKeysAreUniqueAndFolderFree(unittest.TestCase):
    """BUG-07 (review): --redact swapped "<id>@<folder>" for "<id>@<n>" with n
    counted from 1, so a transcript literally named "dup@1" shared its key with
    a generated one, and a real id containing a folder name was left as it was."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "projects")
        for folder, tag, n in (("-Users-x-secretA", "a", 1), ("-Users-x-secretB", "b", 3), ("-Users-x-secretC", "c", 2)):
            proj = os.path.join(self.root, folder)
            _write(os.path.join(proj, "dup.jsonl"), [_response(f"{tag}-m{i}", _ts(i, day=3)) for i in range(n)])
            sub_dir = os.path.join(proj, "dup", "subagents")
            _write(os.path.join(sub_dir, f"agent-{tag}.jsonl"), [_response(f"{tag}-s0", _ts(30, day=3))])
            _meta(os.path.join(sub_dir, f"agent-{tag}.meta.json"), f"t{tag}")
        # A subagent whose parent transcript is missing: keyed "dup@<folder>" too.
        sub_dir = os.path.join(self.root, "-Users-x-secretD", "dup", "subagents")
        _write(os.path.join(sub_dir, "agent-d.jsonl"), [_response("d-s0", _ts(41, day=3))])
        _meta(os.path.join(sub_dir, "agent-d.meta.json"), "td")
        # Files named like a redacted key, and like a generated key.
        _write(os.path.join(self.root, "-Users-x-secretE", "dup@1.jsonl"), [_response("e-m0", _ts(50, day=3))])
        _write(os.path.join(self.root, "-Users-x-secretE", "dup@-Users-x-secretB.jsonl"),
               [_response("f-m0", _ts(51, day=3))])
        self.payload = scan(self.root, cache=FileCache(os.path.join(self.tmp, "c.json")))
        self.red = collect._redact(self.payload)

    def test_every_redacted_key_is_unique(self):
        ids = [s["id"] for s in self.red["sessions"]]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(len(ids), len(self.payload["sessions"]))

    def test_no_folder_name_survives_anywhere(self):
        text = json.dumps(self.red)
        for needle in ("secret", "-Users-", "Users"):
            self.assertNotIn(needle, text)

    def test_rows_stay_joined_and_totals_do_not_change(self):
        ids = {s["id"] for s in self.red["sessions"]}
        usage_keys = {r[1] for r in self.red["usage"]}
        self.assertTrue(usage_keys <= ids | {a["session_id"] for a in self.red["agents"]})
        self.assertEqual(sum(r[12] for r in self.red["usage"]), sum(r[12] for r in self.payload["usage"]))
        # Each session still owns the requests it had.
        before = {s["id"]: s["requests"] for s in self.payload["sessions"]}
        self.assertEqual(sorted(s["requests"] for s in self.red["sessions"]), sorted(before.values()))

    def test_a_session_id_without_a_repeat_or_an_at_sign_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            _write(os.path.join(root, FOLDER_A, "plain.jsonl"), [_response("p", _ts(0))])
            red = collect._redact(scan(root, cache=FileCache(os.path.join(tmp, "c.json"))))
            self.assertEqual([(s["id"], s["session_id"]) for s in red["sessions"]], [("plain", "plain")])


class TestSubagentWithoutItsParentTranscript(unittest.TestCase):
    def test_orphan_does_not_attach_to_another_folders_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            _write(os.path.join(root, FOLDER_A, "dup.jsonl"), [_response("a-m0", _ts(0))])
            sub_dir = os.path.join(root, FOLDER_B, "dup", "subagents")
            _write(os.path.join(sub_dir, "agent-o.jsonl"), [_response("o-s0", _ts(5))])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            (session,) = payload["sessions"]
            self.assertEqual(session["n_subagents"], 0)
            self.assertEqual(session["requests"], 1)
            self.assertEqual(sum(r[12] for r in payload["usage"]), 2)

    def test_orphan_keeps_its_real_id_and_redacts_without_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            _write(os.path.join(root, FOLDER_A, "dup.jsonl"), [_response("a-m0", _ts(0))])
            sub_dir = os.path.join(root, FOLDER_B, "dup", "subagents")
            _write(os.path.join(sub_dir, "agent-o.jsonl"), [_response("o-s0", _ts(5))])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            (agent,) = payload["agents"]
            self.assertEqual(agent["real_session_id"], "dup")
            self.assertNotEqual(agent["session_id"], "dup")  # the internal key

            red = collect._redact(payload)
            text = json.dumps(red)
            self.assertNotIn("-Users-x", text)
            self.assertNotIn("projB", text)
            (red_agent,) = red["agents"]
            self.assertEqual(red_agent["real_session_id"], "dup")
            usage_keys = {r[1] for r in red["usage"]}
            self.assertIn(red_agent["session_id"], usage_keys)
            self.assertEqual(len(usage_keys), 2)

    def test_two_orphan_folders_do_not_share_a_parent_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            for folder, tag in ((FOLDER_A, "a"), (FOLDER_B, "b")):
                _write(os.path.join(root, folder, "dup", "subagents", f"agent-{tag}.jsonl"),
                       [_response(f"{tag}-s0", _ts(5))])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            self.assertEqual(len({a["session_id"] for a in payload["agents"]}), 2)


class TestGeneratedKeysAreUnique(unittest.TestCase):
    def test_a_transcript_named_like_a_generated_key_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "projects")
            _write(os.path.join(root, FOLDER_A, "dup.jsonl"), [_response("a", _ts(0))])
            # Named exactly what projB's dup is about to be keyed.
            _write(os.path.join(root, FOLDER_A, f"dup@{FOLDER_B}.jsonl"), [_response("c", _ts(2))])
            _write(os.path.join(root, FOLDER_B, "dup.jsonl"), [_response("b", _ts(1))])
            payload = scan(root, cache=FileCache(os.path.join(tmp, "c.json")))
            self.assertEqual(len({s["id"] for s in payload["sessions"]}), 3)
            self.assertEqual(sum(r[12] for r in payload["usage"]), 3)
            self.assertEqual(payload["summary"]["total_transcripts"], 3)


if __name__ == "__main__":
    unittest.main()
