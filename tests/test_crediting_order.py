"""BUG-01: a resumed or forked session's transcript starts with a copy of the
original's records. Tool calls, edits, prompts and PR links must be credited to
the same file that owns the requests (the one holding more of them), whatever
the discovery order -- which is alphabetical by file name."""
import os
import shutil
import tempfile
import unittest

from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.test_health import _prompt, _response, _tool_result, _tool_use, _ts, _write

PR_URL = "https://github.com/acme/widgets/pull/7"


def _pr_link(ts):
    return {"type": "pr-link", "timestamp": ts, "prNumber": 7, "prUrl": PR_URL,
            "prRepository": "acme/widgets"}


def _shared_prefix():
    """Five requests (each an Edit), three prompts and a PR link."""
    records = [_prompt("p1", _ts(0))]
    for i in range(1, 6):
        if i == 3:
            records.append(_prompt("p2", _ts(i * 2 - 1)))
        if i == 5:
            records.append(_prompt("p3", _ts(i * 2 - 1)))
        records += [_tool_use(f"m{i}", _ts(i * 2), f"t{i}", "Edit", {"file_path": "src/app.py"}),
                    _tool_result(f"r{i}", _ts(i * 2 + 1), f"t{i}")]
    records.append(_pr_link(_ts(11)))
    return records


def _original():
    return _shared_prefix() + [
        _tool_use("m6", _ts(20), "t6", "Bash", {"command": "pytest"}),
        _tool_result("r6", _ts(21), "t6"),
        _response("m7", _ts(22)),
        _response("m8", _ts(23)),
    ]


def _continuation():
    return _shared_prefix() + [_response("c1", _ts(30))]


class TestCreditingFollowsRequestOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _scan(self, continuation_name):
        root = os.path.join(self.tmp, continuation_name)
        proj = os.path.join(root, "-Users-x-resume")
        _write(os.path.join(proj, "bbbb.jsonl"), _original())
        _write(os.path.join(proj, continuation_name + ".jsonl"), _continuation())
        payload = scan(root, cache=FileCache(os.path.join(self.tmp, continuation_name + ".json")))
        return {s["id"]: s for s in payload["sessions"]}, payload

    def _check(self, name):
        sessions, _ = self._scan(name)
        orig, cont = sessions["bbbb"], sessions[name]
        self.assertEqual(orig["tool_calls"], 6)  # five edits and the pytest run
        self.assertEqual(orig["health"]["edits"], 5)
        self.assertIs(orig["health"]["checked_after_last_edit"], True)
        self.assertEqual(orig["health"]["prompts"], 3)
        self.assertEqual(cont["tool_calls"], 0)
        self.assertEqual(cont["health"]["edits"], 0)
        self.assertEqual(cont["health"]["prompts"], 0)
        self.assertEqual([p["url"] for p in orig["prs"]], [PR_URL])
        self.assertEqual(cont["prs"], [])
        return sessions

    def test_the_session_with_more_requests_is_credited_whichever_sorts_first(self):
        before = self._check("aaaa")  # the continuation is discovered first
        after = self._check("zzzz")   # ...and last
        for key in ("tool_calls", "tool_errors", "prs"):
            self.assertEqual(before["bbbb"][key], after["bbbb"][key])
        self.assertEqual(before["bbbb"]["health"], after["bbbb"]["health"])
        self.assertEqual(before["aaaa"]["health"], after["zzzz"]["health"])

    def test_grand_totals_do_not_depend_on_the_name(self):
        _, a = self._scan("aaaa")
        _, z = self._scan("zzzz")
        self.assertEqual(sum(r[12] for r in a["usage"]), 9)  # 8 + the continuation's own 1
        # Column 1 is the session id, which carries the name; the rest is counts and cost.
        self.assertEqual(sorted([r[0]] + r[2:] for r in a["usage"]),
                         sorted([r[0]] + r[2:] for r in z["usage"]))
        self.assertEqual(sum(r[3] for r in a["tools"]), sum(r[3] for r in z["tools"]))


if __name__ == "__main__":
    unittest.main()
