"""Checks on the --static build and on the frontend's HTML escaping.

The static build's whole purpose is being openable on its own, and the previous
version silently wasn't: it left the page pointing at sibling files under
static/, so a shared copy rendered with no stylesheet and no charting library.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import collect

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static" / "app.js").read_text(encoding="utf-8")

PAYLOAD = {
    "generated_at": "2026-01-01T00:00:00+00:00",
    "unknown_models": [],
    "dims": {"projects": ["/Users/someone/code/secret-client"], "models": [], "skills": [],
             "tools": [], "agentTypes": []},
    "summary": {"total_sessions": 1, "total_transcripts": 1, "derived_sessions": 0,
                "total_subagents": 0, "fast_mode_requests": 0,
                "date_min": "2026-01-01", "date_max": "2026-01-01"},
    "sessions": [{
        "id": "sess-1", "project": "/Users/someone/code/secret-client",
        "title": "Rework the Acme billing flow", "branches": {"feature/acme-billing": 3},
        "prs": [{"session_id": "sess-1", "date": "2026-01-01", "repo": "acme/private-repo",
                 "number": 7, "url": "https://github.com/acme/private-repo/pull/7"}],
    }],
    "usage": [], "tools": [], "skillCalls": [],
    "agents": [{"agent_id": "a1", "description": "Investigate the Acme outage"}],
    "prs": [{"session_id": "sess-1", "date": "2026-01-01", "repo": "acme/private-repo",
             "number": 7, "url": "https://github.com/acme/private-repo/pull/7"}],
    "reconciliation": [],
}


class TestStaticBuildIsSelfContained(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "dashboard.html"
        self._orig = collect.DASHBOARD_PATH
        collect.DASHBOARD_PATH = self.out

    def tearDown(self):
        collect.DASHBOARD_PATH = self._orig
        self.tmp.cleanup()

    def test_build_succeeds(self):
        self.assertTrue(collect._build_static(PAYLOAD))
        self.assertTrue(self.out.exists())

    def test_no_references_to_sibling_static_files(self):
        collect._build_static(PAYLOAD)
        html = self.out.read_text(encoding="utf-8")
        self.assertNotIn('href="static/', html)
        self.assertNotIn('src="static/', html)

    def test_css_and_chart_library_are_inlined(self):
        collect._build_static(PAYLOAD)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn("--series-1", html, "stylesheet was not inlined")
        self.assertIn("Chart", html, "charting library was not inlined")

    def test_export_button_downloads_one_zip(self):
        # Exactly one download per click: browsers refuse multiple automatic
        # downloads from a single gesture, so both CSVs travel in one archive.
        html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("exportBtn", html)
        self.assertIn("exportBtn", APP_JS)
        self.assertIn("/api/export/all.zip", APP_JS)
        # The per-file CSV routes stay available for scripting, but the button
        # must not try to fetch them -- that is the shape that lost a file.
        self.assertNotIn("/api/export/summary.csv", APP_JS)
        self.assertNotIn("/api/export/sessions.csv", APP_JS)

    def test_standalone_build_disables_the_export_button(self):
        # The button downloads from /api/export/*, which only the local server
        # provides; a standalone file offering it would just fail on click.
        self.assertIn("let SERVED_BY_DASHBOARD = true;", APP_JS)
        collect._build_static(PAYLOAD)
        html = self.out.read_text(encoding="utf-8")
        self.assertIn("let SERVED_BY_DASHBOARD = false;", html)
        self.assertNotIn("let SERVED_BY_DASHBOARD = true;", html)

    def test_every_script_the_template_references_is_inlined(self):
        # Adding a new static script means touching both serve.py's allow-list
        # and collect.py's inliner; forgetting the latter silently breaks the
        # standalone build, so assert the two stay in sync with the template.
        template = (REPO / "static" / "index.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'<script src="static/([^"]+)"></script>', template))
        self.assertIn("app.js", referenced)
        inlinable = set(collect.VENDOR_SCRIPTS) | {"app.js"}
        self.assertEqual(referenced, inlinable,
                         "index.html references a script collect.py does not inline")
        collect._build_static(PAYLOAD)
        self.assertNotIn("static/", self.out.read_text(encoding="utf-8"))

    def test_referenced_scripts_are_served_by_the_dev_server(self):
        import serve
        template = (REPO / "static" / "index.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:src|href)="static/([^"]+)"', template))
        missing = referenced - serve.ALLOWED_STATIC_FILES
        self.assertFalse(missing, f"serve.py would 404 these: {sorted(missing)}")

    def test_multiselect_component_is_present(self):
        # The filter panel depends on it; if the file is renamed the build must fail
        # loudly rather than ship a dashboard with no working filters.
        self.assertIn("filters.js", collect.VENDOR_SCRIPTS)
        src = (REPO / "static" / "filters.js").read_text(encoding="utf-8")
        self.assertIn("class MultiSelect", src)
        self.assertIn("global.MultiSelect = MultiSelect", src)

    def test_payload_is_baked_in_place_of_the_fetch(self):
        collect._build_static(PAYLOAD)
        html = self.out.read_text(encoding="utf-8")
        self.assertNotIn("apiFetch('/api/data')", html)
        self.assertIn("sess-1", html)

    def _build_with_title(self, title):
        payload = json.loads(json.dumps(PAYLOAD))
        payload["sessions"][0]["title"] = title
        self.assertTrue(collect._build_static(payload))
        return self.out.read_text(encoding="utf-8")

    def _baked_json(self, html):
        """The raw text of the payload baked into the page, and its decoded value."""
        start = html.index("Promise.resolve(") + len("Promise.resolve(")
        value, end = json.JSONDecoder().raw_decode(html, start)
        return html[start:end], value

    def _script_count(self):
        return (REPO / "static" / "index.html").read_text(encoding="utf-8").count("<script src=")

    def test_closing_script_tag_in_data_cannot_break_out(self):
        html = self._build_with_title("evil </script><script>alert(1)</script>")
        # Only the blocks the build injects may close a script tag.
        self.assertEqual(html.count("</script>"), self._script_count())
        raw, value = self._baked_json(html)
        self.assertEqual(value["sessions"][0]["title"], "evil </script><script>alert(1)</script>")
        self.assertNotIn("<", raw)

    def test_end_tag_in_any_case_or_spacing_cannot_break_out(self):
        # The HTML tokenizer ends a script block at "</script" in any case,
        # followed by whitespace, "/" or ">" -- not only at "</script>".
        for title in ("x </SCRIPT><img src=x onerror=alert(1)>",
                      "x </script ><b>injected</b>",
                      "x </ScRiPt\n>", "x </script/>"):
            with self.subTest(title=title):
                html = self._build_with_title(title)
                self.assertEqual(len(re.findall(r"</script", html, re.IGNORECASE)),
                                 self._script_count())
                raw, value = self._baked_json(html)
                self.assertEqual(value["sessions"][0]["title"], title)
                self.assertNotIn("<", raw)
                self.assertNotIn(">", raw)

    def test_comment_open_in_data_cannot_swallow_the_end_tag(self):
        # "<!--" then "<script>" inside a script block puts the parser in a
        # state where the real "</script>" no longer ends it, so the rest of
        # the page is eaten as script.
        html = self._build_with_title("<!--<script>")
        raw, value = self._baked_json(html)
        self.assertEqual(value["sessions"][0]["title"], "<!--<script>")
        self.assertNotIn("<!--", raw)
        self.assertNotIn("<", raw)

    def test_ampersand_and_line_separators_are_escaped(self):
        title = "a & b \u2028 c \u2029 d"
        html = self._build_with_title(title)
        raw, value = self._baked_json(html)
        self.assertEqual(value["sessions"][0]["title"], title)
        for ch in ("&", "\u2028", "\u2029"):
            self.assertNotIn(ch, raw)
        self.assertIn("\\u0026", raw)

    def test_payload_is_baked_in_once(self):
        # Boot and Refresh share one loader, so the snapshot is not doubled.
        html = self._build_with_title("only-once-marker")
        self.assertEqual(html.count("only-once-marker"), 1)
        self.assertEqual(APP_JS.count("apiFetch('/api/data').then(jsonOrThrow)"), 1)

    def test_inlined_js_cannot_close_or_derail_the_script_block(self):
        src = 'var a = "</SCRIPT >", b = "<!--<script>", c = /<\\/script>/;'
        inlined = collect._inline_script(src)
        body = inlined[len("<script>\n"):-len("\n</script>")]
        self.assertIsNone(re.search(r"</script|<!--", body, re.IGNORECASE))
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed; cannot check the JS still means the same")
        probe = "\nconsole.log(JSON.stringify([a, b, String(c)]));"
        before = subprocess.run([node, "-e", src + probe], capture_output=True, text=True)
        after = subprocess.run([node, "-e", body + probe], capture_output=True, text=True)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout, before.stdout)

    def test_build_fails_loudly_without_the_data_marker(self):
        orig = collect.APP_JS
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "app.js"
            fake.write_text("let SERVED_BY_DASHBOARD = true;\n", encoding="utf-8")
            collect.APP_JS = fake
            try:
                with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
                    self.assertFalse(collect._build_static(PAYLOAD))
            finally:
                collect.APP_JS = orig
        self.assertFalse(self.out.exists())


class TestRedaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.red = collect._redact(PAYLOAD)
        cls.dumped = json.dumps(cls.red)

    def test_titles_and_descriptions_stripped(self):
        self.assertNotIn("Acme billing flow", self.dumped)
        self.assertNotIn("Acme outage", self.dumped)

    def test_absolute_paths_stripped(self):
        self.assertNotIn("/Users/someone", self.dumped)
        self.assertNotIn("secret-client", self.dumped)

    def test_branch_names_stripped(self):
        self.assertNotIn("feature/acme-billing", self.dumped)

    def test_pr_repo_and_url_stripped(self):
        self.assertNotIn("private-repo", self.dumped)
        self.assertNotIn("github.com", self.dumped)

    def test_pr_number_is_kept(self):
        # The table should still render; only the identifying parts go.
        self.assertEqual(self.red["prs"][0]["number"], 7)

    def test_project_dimension_is_aliased_consistently(self):
        alias = self.red["sessions"][0]["project"]
        self.assertEqual(self.red["dims"]["projects"], [alias])

    def test_original_payload_not_mutated(self):
        self.assertEqual(PAYLOAD["sessions"][0]["title"], "Rework the Acme billing flow")


class TestFrontendEscaping(unittest.TestCase):
    """Regression guard: session titles, project paths, skill/tool names and
    branch names all come from the transcripts, so none of them may reach an
    innerHTML template unescaped."""

    RAW_INTERPOLATIONS = [
        "${s.title}", "${a.description}", "${a.agent_type}", "${a.models.join",
        "${p.repo", "${p.url}", "${sk}</div>", "<td>${name}</td>",
        "${shortProjectLabel(s.project)}", "${r[2]}</td>",
    ]

    def test_no_known_unescaped_interpolation(self):
        for frag in self.RAW_INTERPOLATIONS:
            self.assertNotIn(frag, APP_JS, f"{frag} must be wrapped in esc()")

    def test_numeric_headers_are_right_aligned_like_their_values(self):
        # The markup marks numeric `th`s with .num, but for a long time only
        # `td.num` was right-aligned -- so a header sat at the far left of a wide
        # column with its numbers at the far right, and the table read as broken.
        css = (REPO / "static" / "app.css").read_text(encoding="utf-8")
        html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="num"', html, "no numeric headers to align")
        rule = re.search(r"([^}]*)\{[^}]*text-align:\s*right", css)
        self.assertIsNotNone(rule, "nothing right-aligns numeric cells")
        selectors = rule.group(1)
        for needed in ("td.num", "th.num"):
            self.assertIn(needed, selectors,
                          f"{needed} must share the right-align rule, got: {selectors.strip()}")

    def test_escape_helper_exists_and_covers_the_five_entities(self):
        self.assertIn("function esc(", APP_JS)
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, APP_JS)

    def test_pr_hrefs_go_through_the_url_guard(self):
        self.assertIn("function safeUrl(", APP_JS)
        for href in re.findall(r'href="\$\{([^}]*)\}"', APP_JS):
            self.assertTrue(href.startswith("safeUrl("),
                            f'href="${{{href}}}" should use safeUrl()')

    def test_unknown_models_are_surfaced_in_the_ui(self):
        # The payload has always carried unknown_models; nothing rendered it, so
        # an unpriced model showed as $0 with no warning.
        self.assertIn("unknown_models", APP_JS)


class TestEntryPointsRun(unittest.TestCase):
    def test_collect_help_runs(self):
        out = subprocess.run([sys.executable, "collect.py", "--help"], cwd=REPO,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--redact", out.stdout)

    def _collect(self, *args):
        return subprocess.run([sys.executable, "collect.py", *args], cwd=REPO,
                              capture_output=True, text=True)

    def test_impossible_dates_are_rejected_up_front(self):
        # "2026-09-31" has the right shape; it used to pass validation and
        # crash later, mid-export.
        for flag, value in (("--since", "2026-09-31"), ("--until", "2026-02-30"),
                            ("--since", "2026-13-01"), ("--since", "20260101")):
            with self.subTest(value=value):
                out = self._collect("--export", "--offline", flag, value)
                self.assertEqual(out.returncode, 2, out.stdout)
                self.assertIn("YYYY-MM-DD", out.stderr)

    def test_since_after_until_is_rejected(self):
        out = self._collect("--export", "--offline", "--since", "2026-05-02", "--until", "2026-05-01")
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("after --until", out.stderr)


class TestStaticBuildExitCode(unittest.TestCase):
    """A failed --static build used to exit 0, so a script calling it carried on
    with no dashboard.html (or a stale one) and no sign anything went wrong."""

    def _run_main(self, build_ok):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = (collect.scan, collect.price_check.check, collect.OUT_PATH,
                 collect._build_static, sys.argv)

        def restore():
            (collect.scan, collect.price_check.check, collect.OUT_PATH,
             collect._build_static, sys.argv) = saved
        self.addCleanup(restore)
        collect.scan = lambda root, force=False: json.loads(json.dumps({
            **PAYLOAD, "usage": [], "files_parsed": 0, "files_cached": 0, "scan_ms": 0,
            "summary": {**PAYLOAD["summary"]}}))
        collect.price_check.check = lambda **kw: {"status": "offline"}
        collect.OUT_PATH = Path(tmp.name) / "data.json"
        collect._build_static = lambda payload: build_ok
        sys.argv = ["collect.py", "--static", "--offline"]
        with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stdout(devnull):
            return collect.main()

    def test_failed_static_build_exits_nonzero(self):
        self.assertEqual(self._run_main(build_ok=False), 1)

    def test_successful_static_build_exits_zero(self):
        self.assertEqual(self._run_main(build_ok=True), 0)


if __name__ == "__main__":
    unittest.main()
