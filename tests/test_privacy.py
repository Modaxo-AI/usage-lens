import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_usage import export as export_mod
from claude_usage import paths, price_check
from claude_usage.scan import scan

REPO = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
WIDE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "privacy_wide", "projects")
SENTINEL = "SECRET_PROMPT_XYZ"

# One distinct sentinel per place in the wide fixture, so a failure names the
# field that leaked. Each is "PRIVSENT_<name>"; test_every_placement_is_planted
# keeps this list and the fixture files in step.
WIDE_PLACEMENTS = (
    "AGENT_DESCRIPTION", "AGENT_PROMPT", "AGENT_RESULT_PROMPT", "AGENT_RESULT_TEXT",
    "AGENT_TOOL_RESULT", "API_ERROR_TEXT", "ASSISTANT_TEXT", "BASH_COMMAND",
    "BASH_DESCRIPTION", "BASH_STDERR", "BASH_STDOUT", "BASH_TOOL_RESULT",
    "COMPACT_BOUNDARY_CONTENT", "COMPACT_SUMMARY_CONTENT", "EDIT_FILE_PATH",
    "EDIT_NEW_STRING", "EDIT_OLD_STRING", "EDIT_RESULT_NEW", "EDIT_RESULT_OLD",
    "EDIT_RESULT_ORIGINAL_FILE", "EDIT_RESULT_PATCH_LINE", "EDIT_RESULT_PATH",
    "EDIT_TOOL_RESULT", "FILE_HISTORY_BACKUP", "FILE_HISTORY_PATH",
    "HOOK_ATTACHMENT_COMMAND", "HOOK_ATTACHMENT_CONTENT", "HOOK_ATTACHMENT_STDOUT",
    "HOOK_COMMAND", "HOOK_ERROR", "HOOK_SYSTEM_CONTENT", "INSTRUCTIONS_CONTENT",
    "INSTRUCTIONS_PATH", "LAST_PROMPT", "LIMIT_NOTICE_AFTER", "LIMIT_NOTICE_BEFORE",
    "MCP_INPUT", "MCP_NESTED_INPUT", "MCP_RESULT_LIST", "MCP_TOOL_RESULT",
    "QUEUED_COMMAND_PROMPT", "QUEUE_OPERATION_CONTENT", "REDACTED_THINKING_DATA",
    "SKILL_ARGS", "SUBAGENT_DESCRIPTION_HEAD", "SUBAGENT_DESCRIPTION_TAIL",
    "SUBAGENT_META_INITIAL_PROMPT", "SUBAGENT_META_PROMPT", "SUB_ASSISTANT_TEXT",
    "SUB_EDIT_FILE_PATH", "SUB_EDIT_NEW_STRING", "SUB_EDIT_OLD_STRING", "SUB_MCP_INPUT",
    "SUB_THINKING_SIGNATURE", "SUB_THINKING_TEXT", "SUB_TOOL_RESULT",
    "SUB_TOOL_USE_RESULT", "SUB_USER_PROMPT", "SUMMARY_RECORD",
    "SYSTEM_INFORMATIONAL_CONTENT", "SYSTEM_LOCAL_COMMAND_CONTENT",
    "THINKING_SIGNATURE", "THINKING_TEXT", "USER_PROMPT", "USER_STRING_PROMPT",
    "WRITE_CONTENT", "WRITE_TOOL_RESULT",
)
WIDE_SENTINELS = tuple("PRIVSENT_" + name for name in WIDE_PLACEMENTS)

# The one sentinel that may be kept: a subagent's meta `description` is a short
# label the tool shows on purpose, cut to 120 characters, and --redact blanks it.
# The part of the description past the cut is a sentinel of its own.
DESCRIPTION_KEPT = "PRIVSENT_SUBAGENT_DESCRIPTION_HEAD"
DESCRIPTION_CUT = "PRIVSENT_SUBAGENT_DESCRIPTION_TAIL"


class TestPrivacy(unittest.TestCase):
    def test_sentinel_absent_from_full_payload(self):
        # The fixtures embed SENTINEL in user text, assistant text, tool_use
        # input, and tool_result content -- none of that may ever reach the
        # payload sent to the browser.
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        dumped = json.dumps(payload)
        self.assertNotIn(SENTINEL, dumped)

    def test_sentinel_absent_from_the_csv_export(self):
        # The export is the one output meant to leave your machine, so the same
        # guarantee has to hold there: it carries titles, paths and PR links by
        # design, but never prompts, replies, tool inputs or tool results.
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        person = ("someone@example.com", "Some One")
        with tempfile.TemporaryDirectory() as tmp:
            written = export_mod.export(payload, out_dir=tmp, person=person)
            for path, _ in written:
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn(SENTINEL, fh.read(), path)

    def test_export_rows_carry_no_message_content(self):
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        person = ("someone@example.com", "Some One")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(export_mod.summary_rows(payload, person))
        writer.writerows(export_mod.session_rows(payload, person))
        self.assertNotIn(SENTINEL, buf.getvalue())

    def test_sentinel_present_in_source_fixture(self):
        # Sanity check that the fixture actually contains the sentinel, so a
        # bug that made parsing silently skip the file wouldn't give a false pass.
        path = os.path.join(FIXTURES_ROOT, "-Users-x-proj1", "sess-main-1.jsonl")
        with open(path, encoding="utf-8") as fh:
            self.assertIn(SENTINEL, fh.read())

class TestWideFixtureIsPlanted(unittest.TestCase):
    """The privacy checks below only mean something if the sentinels really are
    in the transcripts, and in the shapes Claude Code writes them in."""

    def test_every_placement_is_planted(self):
        found = set()
        for dirpath, _, files in os.walk(WIDE_ROOT):
            for name in files:
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    found.update(re.findall(r"PRIVSENT_[A-Z_]+", fh.read()))
        self.assertEqual(found, set(WIDE_SENTINELS))

    def test_the_description_is_longer_than_the_label_cap(self):
        meta = _wide_meta()
        self.assertGreater(len(meta["description"]), 120)
        self.assertTrue(meta["description"].startswith(DESCRIPTION_KEPT))
        self.assertNotIn(DESCRIPTION_CUT, meta["description"][:120])
        self.assertIn(DESCRIPTION_CUT, meta["description"][120:])


class TestPrivacyOfEveryOutput(unittest.TestCase):
    """Runs the tool over the wide fixture (and the original one) and looks for
    every sentinel in everything it wrote: the dashboard payload, the CSVs with
    and without titles, the parse cache, data.json and the standalone
    dashboard.html, plain and --redact."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        home = Path(cls.tmp) / "home"
        projects = home / ".claude" / "projects"
        shutil.copytree(FIXTURES_ROOT, projects)
        shutil.copytree(WIDE_ROOT, projects, dirs_exist_ok=True)
        cls.sentinels = (SENTINEL,) + WIDE_SENTINELS

        # In-process: the payload as the server would hand it to the browser,
        # and the parse cache that scan saves beside it.
        cls.cache_path = os.path.join(cls.tmp, "inproc", "parsed.json")
        from claude_usage.cache import FileCache
        cls.payload = scan(str(projects), cache=FileCache(cls.cache_path))
        cls.payload_json = json.dumps(cls.payload)

        # collect.py as a user runs it, HOME on a temp folder, nothing online.
        cls.runs = {}
        for name, flags in (("plain", ["--static"]),
                            ("redacted", ["--static", "--redact", "--no-titles"])):
            data, out = Path(cls.tmp) / ("data-" + name), Path(cls.tmp) / ("export-" + name)
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                   paths.DATA_DIR_ENV: str(data), price_check.OFFLINE_ENV: "1"}
            done = subprocess.run(
                [sys.executable, str(REPO / "collect.py"), *flags, "--export", str(out),
                 "--as", "someone@example.com"],
                cwd=cls.tmp, env=env, capture_output=True, text=True, timeout=180)
            assert done.returncode == 0, done.stderr
            cls.runs[name] = {"data": data, "out": out, "stdout": done.stdout, "stderr": done.stderr}

    def _leaks(self, text, allowed=()):
        return sorted(s for s in self.sentinels if s in text and s not in allowed)

    def _assert_clean(self, label, text, allowed=()):
        self.assertEqual(self._leaks(text, allowed), [], label)

    def _csvs(self, name):
        files = sorted(self.runs[name]["out"].glob("*.csv"))
        self.assertGreaterEqual(len(files), 2, "the export wrote no CSVs")
        return files

    def _read(self, path):
        return Path(path).read_text(encoding="utf-8")

    # -- the sanity checks: the outputs hold the fixture's real data -----------

    def test_the_wide_session_is_in_the_payload(self):
        ids = {s["id"] for s in self.payload["sessions"]}
        self.assertIn("sess-wide-1", ids)
        agents = [a for a in self.payload["agents"] if a["session_id"] == "sess-wide-1"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_type"], "general-purpose")
        # The tools' names are metadata and are kept; their inputs are not.
        names = {row[2] for row in self.payload["tools"] if row[1] == "sess-wide-1"}
        self.assertTrue({"Agent", "Edit", "mcp__wide_server__wide_tool"} <= names, names)

    def test_the_rate_limit_notice_still_gives_its_limit_and_reset(self):
        # Only the limit type and the reset time are read from a notice; if the
        # notice were skipped, its sentinels would be absent for the wrong reason.
        events = [e for e in self.payload["limits"] if e["session_id"] == "sess-wide-1"]
        self.assertEqual([(e["kind"], e["resets_at"]) for e in events],
                         [("weekly", "2026-01-09T04:00:00Z")])

    def test_the_cache_holds_the_wide_transcripts(self):
        raw = json.loads(self._read(self.cache_path))
        keys = " ".join(raw["entries"])
        self.assertIn("sess-wide-1.jsonl", keys)
        self.assertIn("agent-wide1.jsonl", keys)

    # -- no sentinel in any output ----------------------------------------------

    def test_payload(self):
        self._assert_clean("dashboard payload", self.payload_json, allowed=(DESCRIPTION_KEPT,))

    def test_csvs_with_titles(self):
        for path in self._csvs("plain"):
            self._assert_clean(path.name, self._read(path))

    def test_csvs_without_titles(self):
        for path in self._csvs("redacted"):
            self._assert_clean(path.name + " (--no-titles)", self._read(path))

    def test_csvs_from_the_payload_with_and_without_titles(self):
        # The same through the library, so it holds whatever collect.py does.
        for include_titles in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                written = export_mod.export(self.payload, out_dir=tmp, include_titles=include_titles,
                                            person=("someone@example.com", "Some One"))
                for path, _ in written:
                    self._assert_clean("%s (titles=%s)" % (os.path.basename(path), include_titles),
                                       self._read(path))

    def test_parse_cache(self):
        self._assert_clean("parse cache (in-process)", self._read(self.cache_path))
        for name, run in self.runs.items():
            cache = run["data"] / "cache" / "parsed.json"
            self.assertTrue(cache.exists(), name)
            self._assert_clean("parse cache (%s run)" % name, self._read(cache))

    def test_data_json(self):
        self._assert_clean("data.json", self._read(self.runs["plain"]["data"] / "data.json"),
                           allowed=(DESCRIPTION_KEPT,))
        # data.json is never redacted, but it is no more revealing for that.
        self._assert_clean("data.json (--redact run)",
                           self._read(self.runs["redacted"]["data"] / "data.json"),
                           allowed=(DESCRIPTION_KEPT,))

    def test_static_dashboard(self):
        html = self._read(self.runs["plain"]["data"] / "dashboard.html")
        self.assertIn("sess-wide-1", html, "the payload was not baked in")
        self._assert_clean("dashboard.html", html, allowed=(DESCRIPTION_KEPT,))

    def test_static_dashboard_redacted(self):
        html = self._read(self.runs["redacted"]["data"] / "dashboard.html")
        self.assertIn("sess-wide-1", html, "the payload was not baked in")
        self._assert_clean("dashboard.html --redact", html)

    def test_nothing_else_written_or_printed_holds_one(self):
        # Every file in either data folder and either export folder, and what
        # the run said: nothing the tool leaves behind is overlooked.
        for name, run in self.runs.items():
            for folder in (run["data"], run["out"]):
                for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                    # The label is kept in data.json (never redacted) and, in
                    # a plain run, in the standalone dashboard.
                    allowed = (DESCRIPTION_KEPT,) if (
                        path.name == "data.json" or (name == "plain" and path.suffix == ".html")) else ()
                    self._assert_clean("%s run: %s" % (name, path.relative_to(self.tmp)),
                                       self._read(path), allowed)
            self._assert_clean(name + " stdout", run["stdout"])
            self._assert_clean(name + " stderr", run["stderr"])

    # -- the subagent description: the one kept label ---------------------------

    def test_description_appears_only_cut_to_120_characters(self):
        agent = next(a for a in self.payload["agents"] if a["session_id"] == "sess-wide-1")
        description = agent["description"]
        self.assertTrue(description.startswith(DESCRIPTION_KEPT), description)
        self.assertEqual(len(description), 120)
        self.assertNotIn(DESCRIPTION_CUT, self.payload_json)
        self.assertNotIn(DESCRIPTION_CUT, self._read(self.runs["plain"]["data"] / "data.json"))
        self.assertIn(DESCRIPTION_KEPT, self._read(self.runs["plain"]["data"] / "dashboard.html"))

    def test_description_is_blanked_by_redact(self):
        redacted = self._read(self.runs["redacted"]["data"] / "dashboard.html")
        self.assertNotIn(DESCRIPTION_KEPT, redacted)
        self.assertNotIn(DESCRIPTION_CUT, redacted)


def _wide_meta():
    path = os.path.join(WIDE_ROOT, "-Users-x-wide", "sess-wide-1", "subagents",
                        "agent-wide1.meta.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _fresh_cache():
    from claude_usage.cache import FileCache
    return FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json"))


if __name__ == "__main__":
    unittest.main()
