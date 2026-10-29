"""Checks on export provenance: account fields, repo keys, history coverage.

These columns exist so that files from many people can be stacked without
misleading whoever stacks them. Two of them could leak something if written
carelessly -- the account cache holds ids, and a git remote can embed a token --
so the whitelist and the credential stripping are tested directly.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from claude_usage import context


class TestNormalizeRemote(unittest.TestCase):
    def test_https_and_ssh_forms_of_one_repo_agree(self):
        forms = [
            "https://github.com/example-org/example-repo.git",
            "https://github.com/example-org/example-repo",
            "git@github.com:example-org/example-repo.git",
            "ssh://git@github.com:22/example-org/example-repo.git",
        ]
        keys = {context.normalize_remote(f) for f in forms}
        self.assertEqual(keys, {"github.com/example-org/example-repo"})

    def test_embedded_credentials_never_survive(self):
        key = context.normalize_remote("https://someone:ghp_SECRETTOKEN@github.com/acme/repo.git")
        self.assertEqual(key, "github.com/acme/repo")
        self.assertNotIn("SECRET", key.upper())
        self.assertNotIn("someone", key)

    def test_azure_devops_path_is_kept_whole(self):
        key = context.normalize_remote("https://contoso@dev.azure.com/contoso/Platform/_git/Billing")
        self.assertEqual(key, "dev.azure.com/contoso/platform/_git/billing")

    def test_local_paths_are_not_repositories(self):
        for url in ("/srv/git/repo.git", "file:///srv/git/repo.git", "", None, "../sibling"):
            self.assertIsNone(context.normalize_remote(url), url)


class TestAccountInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ".claude.json")
        self._orig = context.ACCOUNT_FILES
        context.ACCOUNT_FILES = [self.path]

    def tearDown(self):
        context.ACCOUNT_FILES = self._orig
        self.tmp.cleanup()

    def _write(self, account):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": account, "userID": "abc123"}, fh)

    def test_only_whitelisted_fields_are_returned(self):
        self._write({
            "emailAddress": "someone@example.com", "seatTier": "enterprise_tier_1",
            "userRateLimitTier": "default_claude_max_5x", "hasExtraUsageEnabled": True,
            "accountUuid": "uuid-must-not-leak", "organizationUuid": "org-uuid-must-not-leak",
            "organizationName": "Org Name Must Not Leak",
        })
        info = context.account_info()
        self.assertEqual(info, {
            "claude_account_email": "someone@example.com",
            "seat_tier": "enterprise_tier_1",
            "rate_limit_tier": "default_claude_max_5x",
            "extra_usage_enabled": "yes",
        })
        self.assertNotIn("leak", json.dumps(info).lower())

    def test_missing_file_gives_blank_fields_not_an_error(self):
        info = context.account_info()
        self.assertEqual(set(info), {"claude_account_email", "seat_tier",
                                     "rate_limit_tier", "extra_usage_enabled"})
        self.assertTrue(all(v is None for v in info.values()))

    def test_unreadable_file_gives_blank_fields(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertTrue(all(v is None for v in context.account_info().values()))

    def test_extra_usage_off_is_no(self):
        self._write({"hasExtraUsageEnabled": False})
        self.assertEqual(context.account_info()["extra_usage_enabled"], "no")


class TestVersions(unittest.TestCase):
    def test_pricing_version_is_a_short_stable_fingerprint(self):
        v = context.pricing_version()
        self.assertRegex(v, r"^[0-9a-f]{8}$")
        context.pricing_version.cache_clear()
        self.assertEqual(context.pricing_version(), v)

    def test_pricing_version_changes_when_a_dated_row_is_added(self):
        from unittest import mock
        from claude_usage import pricing
        before = context.pricing_version()
        history = {m: list(rows) for m, rows in pricing.RATE_HISTORY.items()}
        history["claude-sonnet-5"].append(("2099-01-01", {"input": 1.0, "output": 5.0,
                                                          "cache_read": 0.1, "cache_write_5m": 1.25}))
        try:
            with mock.patch.object(pricing, "_SCHEDULE", pricing._build_schedule(history)):
                context.pricing_version.cache_clear()
                after = context.pricing_version()
        finally:
            context.pricing_version.cache_clear()
        self.assertNotEqual(after, before)
        self.assertEqual(context.pricing_version(), before)

    def test_tool_version_is_never_empty(self):
        self.assertTrue(context.tool_version())

    def test_utc_offset_format(self):
        tz = datetime.timezone(datetime.timedelta(hours=-4))
        moment = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=tz)
        self.assertEqual(context.utc_offset(moment), "-04:00")
        self.assertRegex(context.utc_offset(), r"^[+-]\d\d:\d\d$")


class TestWorkdays(unittest.TestCase):
    def test_a_full_week_has_five(self):
        self.assertEqual(context.workdays_between("2026-10-05", "2026-10-11"), 5)

    def test_weekend_only_has_none(self):
        self.assertEqual(context.workdays_between("2026-10-10", "2026-10-11"), 0)

    def test_partial_span(self):
        # Thu..Tue = Thu, Fri, Mon, Tue
        self.assertEqual(context.workdays_between("2026-10-01", "2026-10-06"), 4)

    def test_empty_or_reversed(self):
        self.assertEqual(context.workdays_between("2026-10-06", "2026-10-01"), 0)
        self.assertEqual(context.workdays_between(None, "2026-10-01"), 0)


def _session(sid, end, entry, derived=False):
    return {"id": sid, "start": end, "end": end, "entrypoints": {entry: 3},
            "derived": derived, "project": "/p"}


class TestHistoryCoverage(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 7)

    def test_recent_oldest_cli_session_means_truncated(self):
        payload = {"sessions": [_session("a", "2026-09-08T17:00:00Z", "cli"),
                                _session("b", "2026-07-01T10:00:00Z", "claude-desktop")]}
        h = context.history_coverage(payload, self.TODAY)
        self.assertTrue(h["cli_history_truncated"])
        self.assertEqual(h["cli_history_from"], context._local_day("2026-09-08T17:00:00Z"))
        self.assertEqual(h["cli_sessions"], 1)

    def test_a_new_cli_user_is_not_truncated(self):
        # First CLI session five days ago: nothing has been old enough to delete.
        payload = {"sessions": [_session("a", "2026-10-02T12:00:00Z", "cli")]}
        self.assertFalse(context.history_coverage(payload, self.TODAY)["cli_history_truncated"])

    def test_truncation_needs_history_near_the_retention_age(self):
        # Deletion leaves the oldest survivor close to 30 days old; sparse use
        # can leave it a few days younger, so a week of slack is allowed.
        def flagged(days_ago):
            end = (self.TODAY - datetime.timedelta(days=days_ago)).isoformat() + "T12:00:00"
            payload = {"sessions": [_session("a", end, "cli")]}
            return context.history_coverage(payload, self.TODAY)["cli_history_truncated"]
        self.assertFalse(flagged(22))
        self.assertTrue(flagged(23))
        self.assertTrue(flagged(31))
        self.assertFalse(flagged(32))

    def test_old_cli_history_means_retention_was_raised(self):
        payload = {"sessions": [_session("a", "2026-03-01T12:00:00Z", "cli")]}
        self.assertFalse(context.history_coverage(payload, self.TODAY)["cli_history_truncated"])

    def test_desktop_only_has_nothing_to_flag(self):
        payload = {"sessions": [_session("a", "2026-10-01T12:00:00Z", "claude-desktop")]}
        h = context.history_coverage(payload, self.TODAY)
        self.assertFalse(h["cli_history_truncated"])
        self.assertIsNone(h["cli_history_from"])

    def test_fork_copies_do_not_count(self):
        payload = {"sessions": [_session("a", "2026-03-01T12:00:00Z", "cli"),
                                _session("b", "2026-01-01T12:00:00Z", "cli", derived=True)]}
        h = context.history_coverage(payload, self.TODAY)
        self.assertEqual(h["cli_sessions"], 1)
        self.assertEqual(h["cli_history_from"], context._local_day("2026-03-01T12:00:00Z"))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestProjectRepos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        context._cached_repo_from_disk.cache_clear()

    def tearDown(self):
        self.tmp.cleanup()
        context._cached_repo_from_disk.cache_clear()

    def _repo(self, name, remote):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "-q", path], check=True)
        subprocess.run(["git", "-C", path, "remote", "add", "origin", remote], check=True)
        return path

    def _payload(self, projects, prs=()):
        sessions = [{"id": f"s{i}", "project": p} for i, p in enumerate(projects)]
        return {"sessions": sessions, "prs": list(prs)}

    def test_reads_the_folders_origin_without_credentials(self):
        path = self._repo("app", "https://bot:ghp_TOKEN@github.com/Acme/App.git")
        repos = context.project_repos(self._payload([path]))
        self.assertEqual(repos[path], "github.com/acme/app")

    def test_deleted_worktree_resolves_through_its_parent(self):
        path = self._repo("app", "git@github.com:acme/app.git")
        gone = os.path.join(path, ".claude", "worktrees", "feature-x")
        repos = context.project_repos(self._payload([gone]))
        self.assertEqual(repos[gone], "github.com/acme/app")

    def test_pr_fallback_only_for_a_folder_that_no_longer_exists(self):
        gone = os.path.join(self.tmp.name, "nowhere", "proj")
        here = os.path.join(self.tmp.name, "plain-folder")
        os.makedirs(here)
        prs = [{"session_id": "s0", "url": "https://github.com/acme/gone/pull/3"},
               {"session_id": "s1", "url": "https://github.com/acme/elsewhere/pull/9"}]
        repos = context.project_repos(self._payload([gone, here], prs))
        self.assertEqual(repos[gone], "github.com/acme/gone")
        # An existing non-repo folder stays blank: its session opened a PR in
        # some other repo, and naming that repo would misattribute the project.
        self.assertIsNone(repos[here])

    def test_non_path_projects_are_blank(self):
        repos = context.project_repos(self._payload(["(temp e2e)"]))
        self.assertIsNone(repos["(temp e2e)"])


if __name__ == "__main__":
    unittest.main()
