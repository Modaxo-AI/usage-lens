"""Cross-platform file handling.

Claude Code writes UTF-8 logs. Python's `open()` without an explicit encoding
uses the *platform* default, which is UTF-8 on macOS and Linux and cp1252 on
Windows — so the code read fine for months and then died on the first non-ASCII
byte for every Windows user:

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x90

The static check below is the one that would have caught it on any machine.
"""
import ast
import os
import tempfile
import unittest

from claude_usage.aggregate import project_name_for
from claude_usage.parser import parse_file, parse_meta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = ["serve.py", "collect.py"] + [
    os.path.join("claude_usage", f)
    for f in sorted(os.listdir(os.path.join(REPO, "claude_usage")))
    if f.endswith(".py")
] + [
    # The suite itself has to run on Windows too, or nobody there can verify a
    # fix even once the shipped code is correct.
    os.path.join("tests", f)
    for f in sorted(os.listdir(os.path.join(REPO, "tests")))
    if f.endswith(".py")
]


class TestEveryTextFileDeclaresItsEncoding(unittest.TestCase):
    """A platform-dependent default encoding is a bug waiting for a Windows
    user, so every text-mode read and write names utf-8 explicitly."""

    def _offenders(self, path):
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {k.arg for k in node.keywords}
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "open" and getattr(fn, "id", None) == "open":
                mode = ""
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value or ""
                if "b" not in mode and "encoding" not in kwargs:
                    bad.append(f"{path}:{node.lineno} open() without encoding")
            elif name in ("read_text", "write_text") and "encoding" not in kwargs:
                bad.append(f"{path}:{node.lineno} {name}() without encoding")
        return bad

    def test_every_module_declares_encoding(self):
        offenders = [o for p in SHIPPED for o in self._offenders(p)]
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestNonAsciiLogs(unittest.TestCase):
    """Transcripts routinely contain non-ASCII — em dashes and accented names in
    session titles, project paths and branch names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_parses_a_transcript_with_non_ascii(self):
        title = "Café — naïve 日本語 ✨"
        path = self._write("s.jsonl", "\n".join([
            '{"type":"custom-title","timestamp":"2026-01-01T10:00:00.000Z",'
            f'"customTitle":"{title}"}}',
            '{"type":"assistant","timestamp":"2026-01-01T10:00:01.000Z","requestId":"r1",'
            '"gitBranch":"feature/café","message":{"id":"m1","role":"assistant",'
            '"model":"claude-opus-5-5","usage":{"input_tokens":5,"output_tokens":5}}}',
        ]))
        facts = parse_file(path, "main")
        self.assertEqual(facts.custom_title, title)
        self.assertIn("feature/café", facts.branches)
        self.assertEqual(len(facts.requests), 1)

    def test_parses_subagent_meta_with_non_ascii(self):
        path = self._write("m.json", '{"agentType":"général","description":"résumé ✨"}')
        meta = parse_meta(path)
        self.assertEqual(meta["agent_type"], "général")
        self.assertIn("résumé", meta["description"])

    def test_a_damaged_byte_costs_one_line_not_the_file(self):
        # errors="replace" means a corrupt byte degrades its own line rather
        # than aborting the scan for every other session too.
        path = os.path.join(self.tmp.name, "bad.jsonl")
        with open(path, "wb") as fh:
            fh.write(b'{"type":"assistant","timestamp":"2026-01-01T10:00:00.000Z",'
                     b'"requestId":"r1","message":{"id":"m","role":"assistant",'
                     b'"model":"claude-opus-5-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
            fh.write(b'{"bad":"\x90\x90"}\n')
            fh.write(b'{"type":"assistant","timestamp":"2026-01-01T10:00:02.000Z",'
                     b'"requestId":"r2","message":{"id":"m2","role":"assistant",'
                     b'"model":"claude-opus-5-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
        facts = parse_file(path, "main")
        self.assertEqual({r["request_id"] for r in facts.requests}, {"r1", "r2"})


class TestWindowsPaths(unittest.TestCase):
    """A Windows cwd uses backslashes, which the worktree and home-directory
    handling has to recognise."""

    def test_backslash_path_is_normalised(self):
        self.assertEqual(project_name_for(r"C:\Users\someone\code\myproject"),
                         "C:/Users/someone/code/myproject")

    def test_windows_worktree_collapses_to_its_project(self):
        self.assertEqual(
            project_name_for(r"C:\Users\someone\code\myproject\.claude\worktrees\feature-x"),
            "C:/Users/someone/code/myproject")

    def test_posix_behaviour_is_unchanged(self):
        self.assertEqual(project_name_for("/Users/x/code/p/.claude/worktrees/wt"),
                         "/Users/x/code/p")


if __name__ == "__main__":
    unittest.main()
