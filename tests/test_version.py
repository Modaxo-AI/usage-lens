"""One version, written in three places: the package, the plugin manifest and
the changelog. A release that bumps one and forgets another fails here."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import claude_usage
from claude_usage import context

REPO = Path(__file__).resolve().parent.parent


class TestVersion(unittest.TestCase):
    def test_is_a_release_number(self):
        self.assertRegex(claude_usage.__version__, r"^\d+\.\d+\.\d+$")

    def test_plugin_manifest_agrees(self):
        manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], claude_usage.__version__)

    def test_changelog_names_it_as_the_latest_release(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = re.findall(r"^## (\d+\.\d+\.\d+)\b", text, re.M)
        self.assertTrue(releases, "no release headings in CHANGELOG.md")
        self.assertEqual(releases[0], claude_usage.__version__)

    def test_tool_version_starts_with_the_release(self):
        self.assertRegex(context.tool_version(),
                         rf"^{re.escape(claude_usage.__version__)}(\+[0-9a-f]+)?$")

    def test_without_git_it_is_the_release(self):
        context.tool_version.cache_clear()
        self.addCleanup(context.tool_version.cache_clear)
        orig = context._git
        context._git = lambda *a, **k: None
        try:
            self.assertEqual(context.tool_version(), claude_usage.__version__)
        finally:
            context._git = orig

    def test_version_flags(self):
        for script in ("serve.py", "collect.py"):
            out = subprocess.run([sys.executable, str(REPO / script), "--version"],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(out.stdout.startswith(f"Usage Lens {claude_usage.__version__}"), out.stdout)


if __name__ == "__main__":
    unittest.main()
