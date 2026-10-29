import os
import unittest
from unittest import mock

from claude_usage import aggregate
from claude_usage.aggregate import HOME, project_name_for


class TestProjectNaming(unittest.TestCase):
    def test_plain_cwd_home_replaced_with_tilde(self):
        import os
        home = os.path.expanduser("~")
        self.assertEqual(project_name_for(f"{home}/code/myrepo"), "~/code/myrepo")

    def test_worktree_suffix_stripped(self):
        import os
        home = os.path.expanduser("~")
        cwd = f"{home}/code/myrepo/.claude/worktrees/feature-branch-abc123"
        self.assertEqual(project_name_for(cwd), "~/code/myrepo")

    def test_worktree_suffix_stripped_with_trailing_subpath(self):
        import os
        home = os.path.expanduser("~")
        cwd = f"{home}/code/myrepo/.claude/worktrees/feature-x/nested/dir"
        self.assertEqual(project_name_for(cwd), "~/code/myrepo")

    def test_temp_e2e_grouping(self):
        cwd = "/private/var/folders/ab/cdefgh/T/e2e-claude-abc123"
        self.assertEqual(project_name_for(cwd), "(temp e2e)")

    def test_fallback_decode_when_no_cwd(self):
        name = project_name_for(None, "-Users-x-code-nocwd-project")
        self.assertEqual(name, "/Users/x/code/nocwd/project (decoded)")

    def test_fallback_decode_strips_leading_dash(self):
        name = project_name_for(None, "-Users-x-proj")
        self.assertTrue(name.startswith("/Users/x/proj"))

    def test_no_cwd_no_folder(self):
        self.assertEqual(project_name_for(None, None), "(unknown project)")

    def test_non_home_cwd_unchanged_besides_worktree_strip(self):
        self.assertEqual(project_name_for("/opt/other/path"), "/opt/other/path")

    def _homes(self):
        """The real HOME, then a Windows one (backslashes, os.sep patched), so
        the home-prefix tests also run the Windows branch on any OS."""
        yield HOME
        with mock.patch.object(aggregate, "HOME", "C:\\Users\\runneradmin"), \
                mock.patch.object(aggregate.os, "sep", "\\"), \
                mock.patch.object(aggregate.os.path, "sep", "\\"):
            yield "C:\\Users\\runneradmin"

    @staticmethod
    def _shown(path):
        # The name is always shown with forward slashes, whatever HOME uses.
        return path.replace("\\", "/")

    def test_a_path_that_only_starts_like_home_is_not_shortened(self):
        # HOME + "X/p" is another user's directory, not "~X/p".
        for home in self._homes():
            with self.subTest(home=home):
                self.assertEqual(project_name_for(home + "X/p"), self._shown(home + "X/p"))
                self.assertEqual(project_name_for(home + "-old/p"), self._shown(home + "-old/p"))

    def test_home_itself_and_paths_below_it_are_shortened(self):
        for home in self._homes():
            with self.subTest(home=home):
                self.assertEqual(project_name_for(home), "~")
                self.assertEqual(project_name_for(home + "/p"), "~/p")
                self.assertEqual(project_name_for(home + "/"), "~/")

    def test_a_trailing_separator_on_home_is_ignored(self):
        for home in self._homes():
            with self.subTest(home=home), mock.patch.object(aggregate, "HOME", home + "/"):
                self.assertEqual(project_name_for(home + "/p"), "~/p")
                self.assertEqual(project_name_for(home + "X/p"), self._shown(home + "X/p"))

    def test_windows_home_matches_with_either_separator(self):
        home = "C:\\Users\\me"
        with mock.patch.object(aggregate, "HOME", home), mock.patch.object(aggregate.os, "sep", "\\"):
            self.assertEqual(project_name_for(home + "\\code\\p"), "~/code/p")
            self.assertEqual(project_name_for("C:/Users/me/code/p"), "~/code/p")
            self.assertEqual(project_name_for(home), "~")
            self.assertEqual(project_name_for("C:\\Users\\meX\\p"), "C:/Users/meX/p")
            self.assertEqual(project_name_for("C:/Users/me-old/p"), "C:/Users/me-old/p")


if __name__ == "__main__":
    unittest.main()
