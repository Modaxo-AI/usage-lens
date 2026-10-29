"""Checks on the business-review CSV export.

The headline property is reconciliation: a manager's spreadsheet must not
disagree with the dashboard. Everything else here guards a decision that is easy
to regress -- blank vs zero, fork handling, and the attribution rule.
"""
import copy
import csv
import datetime
import io
import json
import os
import tempfile
import time
import unittest
from collections import defaultdict

from claude_usage import export as export_mod
from claude_usage.cache import FileCache
from claude_usage.scan import scan

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
PERSON = ("someone@example.com", "Some One")


def _payload(tz=datetime.timezone.utc):
    """The fixture, scanned in the zone it will be exported in (an export's
    payload is always scanned in its export zone; the default one is UTC)."""
    with tempfile.TemporaryDirectory() as tmp:
        return scan(FIXTURES_ROOT, cache=FileCache(os.path.join(tmp, "parsed.json")), tz=tz)


# The last released layouts without a schema_version column. Frozen: edit only
# by appending a new SCHEMA_n list, never by changing these.
SUMMARY_SCHEMA_1 = [
    "person_email", "person_name", "week_start", "project",
    "sessions_started", "sessions_active", "requests",
    "est_cost_usd", "est_cost_subagent_usd", "subagent_cost_share",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "total_tokens", "thinking_tokens",
    "web_search_requests", "web_fetch_requests",
    "tool_calls", "tool_errors",
    "main_active_hours", "prs_opened", "lines_added", "lines_removed",
    "cli_reported_cost_usd",
    "models_used", "skills_used",
    "sessions_with_line_data", "sessions_with_pr_data", "sessions_with_cli_cost",
    "cost_with_line_data_usd", "cost_with_pr_data_usd",
    "cache_saved_usd", "est_cost_main_usd", "cost_basis",
]
SESSION_SCHEMA_1 = [
    "person_email", "person_name", "session_id", "title", "project",
    "started_at", "ended_at", "main_active_minutes",
    "models", "skills", "requests",
    "est_cost_usd", "est_cost_main_usd", "est_cost_subagent_usd",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "total_tokens", "thinking_tokens",
    "subagents", "tool_calls", "tool_errors",
    "lines_added", "lines_removed", "prs_opened", "pr_urls",
    "cli_reported_cost_usd",
    "entrypoint", "cli_version", "git_branch", "permission_mode",
    "cache_saved_usd", "is_fork_copy", "cost_basis",
]

# Schema 2 appended these (unchanged in schema 3); schema 3 added the breakdown.
CONTEXT_SCHEMA_2 = [
    "claude_account_email", "seat_tier", "rate_limit_tier", "extra_usage_enabled",
    "person_active_days", "window_workdays", "last_active_date", "seat_idle",
    "cli_history_from", "cli_history_truncated",
    "schema_version", "exported_at", "window_from", "window_to", "timezone",
    "tool_version", "pricing_version",
]
SUMMARY_SCHEMA_3 = SUMMARY_SCHEMA_1 + [
    "project_repo", "sessions_active_cli", "active_days",
    "week_days_in_window", "partial_week", "cli_history_complete",
] + CONTEXT_SCHEMA_2
SESSION_SCHEMA_3 = SESSION_SCHEMA_1 + ["project_repo"] + CONTEXT_SCHEMA_2
BREAKDOWN_SCHEMA_3 = [
    "person_email", "person_name", "week_start", "project", "project_repo",
    "model", "model_family", "thread", "subagent_type",
    "requests",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_tokens",
    "total_tokens", "thinking_tokens",
    "est_cost_usd", "cache_saved_usd", "cost_basis",
] + CONTEXT_SCHEMA_2

# Schema 4: friction and allowance columns, and the limits file.
FRICTION_SCHEMA_4 = ["api_retries", "retry_wait_minutes", "turns", "long_turns",
                     "context_compactions", "limit_hits", "blocked_hours"]
ALLOWANCE_SCHEMA_4 = ["allowance_est_usd", "allowance_week_start", "allowance_week_used_usd",
                      "allowance_week_used_pct", "allowance_status", "projected_limit_hit_at"]
SUMMARY_SCHEMA_4 = SUMMARY_SCHEMA_3 + FRICTION_SCHEMA_4 + ["turn_hours"] + ALLOWANCE_SCHEMA_4
SESSION_SCHEMA_4 = SESSION_SCHEMA_3 + FRICTION_SCHEMA_4 + ["turn_minutes"] + ALLOWANCE_SCHEMA_4
BREAKDOWN_SCHEMA_4 = BREAKDOWN_SCHEMA_3 + ALLOWANCE_SCHEMA_4
LIMIT_SCHEMA_4 = [
    "person_email", "person_name", "limit_type", "hit_at", "last_notice_at",
    "resets_at", "notices", "locked_out", "blocked_hours", "resumed_at",
    "usage_before_hit_usd", "usage_after_hit_usd", "requests_after_hit",
    "project", "project_repo", "session_id", "cost_basis",
] + CONTEXT_SCHEMA_2 + ALLOWANCE_SCHEMA_4

# Schema 5: the PRs file, and the sessions sheet's pr_repos.
SUMMARY_SCHEMA_5 = SUMMARY_SCHEMA_4
SESSION_SCHEMA_5 = SESSION_SCHEMA_4 + ["pr_repos"]
BREAKDOWN_SCHEMA_5 = BREAKDOWN_SCHEMA_4
LIMIT_SCHEMA_5 = LIMIT_SCHEMA_4
PR_SCHEMA_5 = [
    "person_email", "person_name", "pr_url", "pr_repo", "pr_number", "opened_on",
    "session_id", "project", "project_repo", "in_project_repo",
    "session_cost_usd", "session_prs", "cost_basis",
] + CONTEXT_SCHEMA_2 + ALLOWANCE_SCHEMA_4


def _as_dicts(columns, rows):
    return [dict(zip(columns, r)) for r in rows]


def _real_sessions(payload):
    return [s for s in payload["sessions"] if not s["derived"]]


class TestReconciliation(unittest.TestCase):
    """The export is derived from the same payload the dashboard renders, so
    every total has to agree with it exactly."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                                export_mod.summary_rows(cls.payload, PERSON))

    def _sum(self, column, cast=float):
        return sum(cast(r[column]) for r in self.summary if r[column] not in (None, ""))

    def test_cost_matches_payload(self):
        self.assertAlmostEqual(self._sum("est_cost_usd"),
                               sum(r[13] for r in self.payload["usage"]), places=2)

    def test_tokens_match_payload(self):
        expected = sum(r[5] + r[6] + r[7] + r[8] + r[9] for r in self.payload["usage"])
        self.assertEqual(self._sum("total_tokens", int), expected)

    def test_requests_match_payload(self):
        self.assertEqual(self._sum("requests", int),
                         sum(r[12] for r in self.payload["usage"]))

    def test_tool_calls_match_payload(self):
        self.assertEqual(self._sum("tool_calls", int),
                         sum(r[3] for r in self.payload["tools"]))

    def test_pr_count_matches_payload(self):
        # PRs are counted from the deduped URL list: some survive only inside a
        # fork transcript, and counting via sessions would drop them.
        self.assertEqual(self._sum("prs_opened", int), len(self.payload["prs"]))

    def test_cost_weighted_coverage_never_exceeds_total_cost(self):
        # A coverage figure larger than the cost it qualifies would be nonsense.
        for col in ("cost_with_line_data_usd", "cost_with_pr_data_usd"):
            self.assertLessEqual(round(self._sum(col), 4),
                                 round(self._sum("est_cost_usd"), 4) + 0.01, col)

    def test_cost_weighted_coverage_is_zero_without_the_signal(self):
        for r in self.summary:
            if int(r["sessions_with_line_data"]) == 0:
                self.assertEqual(float(r["cost_with_line_data_usd"]), 0.0)
            if int(r["sessions_with_pr_data"]) == 0:
                self.assertEqual(float(r["cost_with_pr_data_usd"]), 0.0)

    def test_cache_savings_match_payload(self):
        self.assertAlmostEqual(self._sum("cache_saved_usd"),
                               sum(r[15] for r in self.payload["usage"]), places=6)

    def test_new_columns_are_appended_not_inserted(self):
        # Inserting a column mid-row silently breaks anyone reading the CSV by
        # position, and files from different versions get stacked together. So
        # every column a released schema had stays exactly where it was; new
        # ones only ever go on the end.
        for cols, released in ((export_mod.SUMMARY_COLUMNS, SUMMARY_SCHEMA_5),
                               (export_mod.SESSION_COLUMNS, SESSION_SCHEMA_5),
                               (export_mod.BREAKDOWN_COLUMNS, BREAKDOWN_SCHEMA_5),
                               (export_mod.LIMIT_COLUMNS, LIMIT_SCHEMA_5),
                               (export_mod.PR_COLUMNS, PR_SCHEMA_5)):
            self.assertEqual(cols[:len(released)], released)
            self.assertEqual(len(cols), len(set(cols)), "duplicate column name")

    def test_session_sheet_carries_savings_too(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON))
        total = sum(float(r["cache_saved_usd"]) for r in rows)
        self.assertAlmostEqual(total, sum(r[15] for r in self.payload["usage"]), places=6)

    def test_sessions_started_matches_non_fork_sessions(self):
        real = [s for s in self.payload["sessions"] if not s["derived"]]
        self.assertEqual(self._sum("sessions_started", int), len(real))


class TestSheetParity(unittest.TestCase):
    """The two sheets describe the same work. A leader who sums either one must
    get the same answer, or the export cannot be trusted at all."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                                export_mod.summary_rows(cls.payload, PERSON))
        cls.sessions = _as_dicts(export_mod.SESSION_COLUMNS,
                                 export_mod.session_rows(cls.payload, PERSON))

    def _both(self, summary_col, session_col=None):
        session_col = session_col or summary_col
        f = lambda rows, c: sum(float(r[c]) for r in rows if r[c] not in ("", None))
        return f(self.summary, summary_col), f(self.sessions, session_col)

    def test_main_cost_agrees_across_sheets(self):
        a, b = self._both("est_cost_main_usd")
        self.assertAlmostEqual(a, b, places=6)

    def test_cost_agrees_across_sheets(self):
        a, b = self._both("est_cost_usd")
        self.assertAlmostEqual(a, b, places=6)

    def test_requests_agree_across_sheets(self):
        a, b = self._both("requests")
        self.assertEqual(a, b)

    def test_cache_savings_agree_across_sheets(self):
        a, b = self._both("cache_saved_usd")
        self.assertAlmostEqual(a, b, places=6)

    def test_a_fork_copy_owning_work_is_kept_and_flagged(self):
        # Excluding these made the session sheet total less than the summary
        # sheet, with nothing in either file to explain the gap.
        owners = set()
        for r in self.payload["usage"]:
            owners.add(r[1])
        derived_owners = {s["id"] for s in self.payload["sessions"]
                          if s["derived"] and s["id"] in owners}
        listed = {r["session_id"]: r for r in self.sessions}
        for sid in derived_owners:
            if any(r[1] == sid and r[12] for r in self.payload["usage"]):
                self.assertIn(sid, listed)
                self.assertEqual(listed[sid]["is_fork_copy"], "yes")

    def test_ordinary_sessions_are_not_flagged(self):
        for r in self.sessions:
            if r["is_fork_copy"] == "no":
                continue
            self.assertEqual(r["is_fork_copy"], "yes")

    def test_flag_column_is_appended(self):
        cols = export_mod.SESSION_COLUMNS
        self.assertGreater(cols.index("is_fork_copy"), cols.index("permission_mode"))


class TestForkHandling(unittest.TestCase):
    """Fork/resume copies must not add a session row, but their unique subagent
    cost still has to be counted -- the two are separate concerns."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_no_session_row_for_a_fork_that_owns_nothing(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON))
        ids = {r["session_id"] for r in rows}
        owns = {r[1] for r in self.payload["usage"] if r[12]}
        for s in self.payload["sessions"]:
            if s["derived"] and s["id"] not in owns:
                self.assertNotIn(s["id"], ids)

    def test_each_session_appears_once(self):
        rows = export_mod.session_rows(self.payload, PERSON)
        ids = [r[2] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_usage_from_a_fork_parent_is_still_counted(self):
        # Guards the bug where filtering usage rows by non-fork session dropped
        # real subagent cost belonging to a fork's parent.
        summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                            export_mod.summary_rows(self.payload, PERSON))
        self.assertAlmostEqual(sum(float(r["est_cost_usd"]) for r in summary),
                               sum(r[13] for r in self.payload["usage"]), places=6)


class TestMissingIsBlank(unittest.TestCase):
    """A zero would read as "delivered nothing" and drag down any average taken
    over it, so an unrecorded signal stays empty."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.rows = _as_dicts(export_mod.SESSION_COLUMNS,
                             export_mod.session_rows(cls.payload, PERSON))

    def test_absent_line_counts_are_none_not_zero(self):
        by_id = {s["id"]: s for s in _real_sessions(self.payload)}
        for r in self.rows:
            if by_id[r["session_id"]].get("lines_added") is None:
                self.assertIsNone(r["lines_added"], r["session_id"])

    def test_absent_cli_cost_is_none_not_zero(self):
        by_id = {s["id"]: s for s in _real_sessions(self.payload)}
        for r in self.rows:
            if by_id[r["session_id"]].get("cli_cost") is None:
                self.assertIsNone(r["cli_reported_cost_usd"], r["session_id"])

    def test_none_writes_as_an_empty_cell(self):
        buf = io.StringIO()
        csv.writer(buf).writerow([1, None, "x"])
        self.assertEqual(buf.getvalue().strip(), "1,,x")


class TestDateFiltering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_since_excludes_earlier_sessions(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, since="2026-02-01"))
        self.assertTrue(rows)
        for r in rows:
            self.assertGreaterEqual(r["started_at"][:10], "2026-02-01")

    def test_until_excludes_later_sessions(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, until="2026-01-31"))
        for r in rows:
            self.assertLessEqual(r["started_at"][:10], "2026-01-31")

    def test_impossible_window_yields_no_rows(self):
        self.assertEqual(export_mod.session_rows(self.payload, PERSON,
                                                 since="2030-01-01"), [])
        self.assertEqual(export_mod.summary_rows(self.payload, PERSON,
                                                 since="2030-01-01"), [])


class TestIdentityAndTitles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_every_row_carries_the_person(self):
        # This is what lets a leader concatenate several people's files.
        for rows, cols in ((export_mod.summary_rows(self.payload, PERSON), export_mod.SUMMARY_COLUMNS),
                           (export_mod.session_rows(self.payload, PERSON), export_mod.SESSION_COLUMNS)):
            self.assertTrue(rows)
            for r in _as_dicts(cols, rows):
                self.assertEqual(r["person_email"], PERSON[0])
                self.assertEqual(r["person_name"], PERSON[1])

    def test_override_wins_over_git_config(self):
        email, _ = export_mod.resolve_identity("override@example.com")
        self.assertEqual(email, "override@example.com")

    def test_identity_is_never_empty(self):
        email, name = export_mod.resolve_identity()
        self.assertTrue(email)
        self.assertTrue(name)

    def test_no_titles_blanks_the_column(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, include_titles=False))
        self.assertTrue(rows)
        self.assertTrue(all(r["title"] == "" for r in rows))

    def test_titles_present_by_default(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON))
        self.assertTrue(any(r["title"] for r in rows))


class TestWrittenFiles(unittest.TestCase):
    def test_export_writes_every_kind_as_a_readable_csv(self):
        payload = _payload()
        with tempfile.TemporaryDirectory() as tmp:
            written = export_mod.export(payload, out_dir=tmp, person=PERSON, stamp="20260101")
            self.assertEqual(len(written), 6)
            names = sorted(os.path.basename(p) for p, _ in written)
            self.assertEqual(names, [
                "claude-usage-breakdown-someone-example.com-20260101.csv",
                "claude-usage-limits-someone-example.com-20260101.csv",
                "claude-usage-prs-someone-example.com-20260101.csv",
                "claude-usage-sessions-someone-example.com-20260101.csv",
                "claude-usage-summary-someone-example.com-20260101.csv",
                "claude-usage-tools-someone-example.com-20260101.csv",
            ])
            for path, count in written:
                with open(path, newline="", encoding="utf-8") as fh:
                    rows = list(csv.reader(fh))
                self.assertEqual(len(rows) - 1, count, path)  # header + rows
                self.assertTrue(all(len(r) == len(rows[0]) for r in rows), path)

    def test_headers_match_the_declared_columns(self):
        payload = _payload()
        with tempfile.TemporaryDirectory() as tmp:
            written = export_mod.export(payload, out_dir=tmp, person=PERSON, stamp="20260101")
            expected = {kind: cols for kind, (cols, _) in export_mod.KINDS.items()}
            self.assertEqual(len(written), len(expected))
            for path, _ in written:
                kind = os.path.basename(path).split("-")[2]
                with open(path, newline="", encoding="utf-8") as fh:
                    header = next(csv.reader(fh))
                self.assertEqual(header, expected[kind])

    def test_identity_is_sanitised_for_the_filename(self):
        self.assertEqual(export_mod._slug("a b/c@d.com"), "a-b-c-d.com")
        self.assertEqual(export_mod._slug("///"), "unknown")


class TestWeekBucketing(unittest.TestCase):
    def test_week_start_is_monday(self):
        # 2026-02-05 is a Thursday.
        self.assertEqual(export_mod._week_start("2026-02-05"), "2026-02-02")
        self.assertEqual(export_mod._week_start("2026-02-02"), "2026-02-02")

    def test_week_start_accepts_a_bucket_key(self):
        self.assertEqual(export_mod._week_start(export_mod._day("2026-02-05T14:35")),
                         "2026-02-02")


class TestCostColumns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                                export_mod.summary_rows(cls.payload, PERSON))
        cls.sessions = _as_dicts(export_mod.SESSION_COLUMNS,
                                 export_mod.session_rows(cls.payload, PERSON))

    def test_main_plus_subagent_is_the_total_on_every_summary_row(self):
        self.assertTrue(self.summary)
        for r in self.summary:
            self.assertAlmostEqual(r["est_cost_main_usd"] + r["est_cost_subagent_usd"],
                                   r["est_cost_usd"], places=5)

    def test_main_cost_matches_main_thread_usage(self):
        expected = sum(r[13] for r in self.payload["usage"] if r[4] == -1)
        self.assertAlmostEqual(sum(r["est_cost_main_usd"] for r in self.summary),
                               expected, places=4)

    def test_every_row_states_its_cost_basis(self):
        # The CSV travels without the docs, so a figure priced at API list rates
        # has to say so on the row -- otherwise it gets read as an invoice.
        for rows in (self.summary, self.sessions):
            self.assertTrue(rows)
            self.assertEqual({r["cost_basis"] for r in rows}, {"api_list_price"})


class TestWebLookups(unittest.TestCase):
    """Claude Code's WebSearch/WebFetch are client-side tools. The usage record's
    server_tool_use counters never move for them, so the export has to count
    the tool calls, as the dashboard does."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        tool_row = next(r for r in cls.payload["tools"]
                        if any(s["id"] == r[1] for s in cls.payload["sessions"]))
        cls.sid, cls.when = tool_row[1], tool_row[0]
        cls.payload["tools"] = cls.payload["tools"] + [
            [cls.when, cls.sid, "WebSearch", 3, 0, False],
            [cls.when, cls.sid, "WebFetch", 5, 1, True],
        ]
        cls.summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                                export_mod.summary_rows(cls.payload, PERSON))

    def test_web_tool_calls_are_counted(self):
        self.assertEqual(sum(r["web_search_requests"] for r in self.summary), 3)
        self.assertEqual(sum(r["web_fetch_requests"] for r in self.summary), 5)

    def test_server_side_counters_are_not_what_is_read(self):
        # Even if a future build populates the server-side counters, the column
        # must keep meaning "lookups Claude Code made" and not double-count.
        for r in self.payload["usage"]:
            r[11] = 100
            if len(r) > 14:
                r[14] = 100
        rows = _as_dicts(export_mod.SUMMARY_COLUMNS,
                         export_mod.summary_rows(self.payload, PERSON))
        self.assertEqual(sum(r["web_search_requests"] for r in rows), 3)
        self.assertEqual(sum(r["web_fetch_requests"] for r in rows), 5)



class TestSchema2(unittest.TestCase):
    """Provenance, seat and coverage columns. Fixture data runs 2026-01-01 to
    2026-02-05; "now" is pinned to Sunday 2026-02-08 so the window is fixed."""

    NOW = datetime.datetime(2026, 2, 8, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))

    # NOW's own fixed offset as the export zone, and the payload scanned in it,
    # as an export's always is.
    ZONE = (NOW.tzinfo, "UTC-05:00")

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload(tz=cls.ZONE[0])

    def _ctx(self, since=None, until=None, **history):
        ctx = export_mod.build_context(self.payload, PERSON, since, until, now=self.NOW, zone=self.ZONE)
        if history:
            ctx["_history"] = {**ctx["_history"], **history}
        return ctx

    def _summary(self, since=None, until=None, ctx=None):
        ctx = ctx or self._ctx(since, until)
        return _as_dicts(export_mod.SUMMARY_COLUMNS,
                         export_mod.summary_rows(self.payload, PERSON, since, until, ctx=ctx))

    def _sessions(self, ctx=None):
        ctx = ctx or self._ctx()
        return _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, ctx=ctx))

    def test_private_context_keys_are_never_columns(self):
        for cols in (export_mod.SUMMARY_COLUMNS, export_mod.SESSION_COLUMNS):
            self.assertFalse([c for c in cols if c.startswith("_")])

    def test_both_sheets_carry_identical_context(self):
        ctx = self._ctx()
        for rows in (self._summary(ctx=ctx), self._sessions(ctx=ctx)):
            for r in rows:
                for c in export_mod.CONTEXT_COLUMNS:
                    self.assertEqual(r[c], ctx[c], c)

    def test_zip_sheets_share_one_export_timestamp(self):
        import zipfile
        archive = zipfile.ZipFile(io.BytesIO(export_mod.zip_bytes(self.payload, PERSON)))
        stamps = set()
        for name in archive.namelist():
            for r in csv.DictReader(io.StringIO(archive.read(name).decode("utf-8"))):
                stamps.add(r["exported_at"])
        self.assertEqual(len(stamps), 1)

    def test_machine_id_is_random_stable_and_on_every_sheet(self):
        orig = export_mod.context_mod.MACHINE_ID_FILE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                export_mod.context_mod.MACHINE_ID_FILE = os.path.join(tmp, "a", "machine-id")
                first = export_mod.context_mod.machine_id()
                self.assertRegex(first, r"^[0-9a-f]{12}$")
                self.assertEqual(export_mod.context_mod.machine_id(), first, "kept, not remade")
                # Schema 6 put machine_id last; later schemas only append after
                # it, so it keeps its position and only schema-7+ columns follow
                # (the schema-8 bucket_zone, then schema 9's, 10's and 11's columns).
                later = {"summary": export_mod.HEALTH_SUMMARY_COLUMNS,
                         "sessions": export_mod.HEALTH_SESSION_COLUMNS}
                after_zone = {"summary": ["output_tokens_imputed", "est_cost_unlogged_usd",
                                       "very_long_context_cost_usd", "week_active_days"],
                           "sessions": ["output_tokens_imputed", "est_cost_unlogged_usd",
                                        "very_long_context_cost_usd"],
                           "breakdown": ["output_tokens_imputed"]}
                for kind, (columns, _) in export_mod.KINDS.items():
                    after = columns[columns.index("machine_id") + 1:]
                    self.assertEqual(after, later.get(kind, []) + ["bucket_zone"] + after_zone.get(kind, []),
                                     kind)
                self.assertEqual(self._ctx()["machine_id"], first)
                # Another machine (another cache folder) gets its own.
                export_mod.context_mod.MACHINE_ID_FILE = os.path.join(tmp, "b", "machine-id")
                self.assertNotEqual(export_mod.context_mod.machine_id(), first)
                # A damaged file is replaced rather than exported as-is.
                with open(os.path.join(tmp, "b", "machine-id"), "w", encoding="utf-8") as fh:
                    fh.write("my-laptop.local\n")
                self.assertRegex(export_mod.context_mod.machine_id(), r"^[0-9a-f]{12}$")
            finally:
                export_mod.context_mod.MACHINE_ID_FILE = orig

    def test_window_defaults_to_the_data_and_today(self):
        ctx = self._ctx()
        self.assertEqual(ctx["window_from"], "2026-01-01")
        self.assertEqual(ctx["window_to"], "2026-02-08")
        self.assertEqual(ctx["schema_version"], export_mod.context_mod.EXPORT_SCHEMA_VERSION)
        self.assertEqual(ctx["timezone"], "-05:00")

    def test_window_never_extends_past_today(self):
        self.assertEqual(self._ctx(until="2030-01-01")["window_to"], "2026-02-08")

    def test_every_week_in_the_window_has_a_row(self):
        rows = self._summary()
        weeks = {r["week_start"] for r in rows}
        self.assertEqual(weeks, {"2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19",
                                 "2026-01-26", "2026-02-02"})

    def test_quiet_weeks_are_zero_rows_without_a_project(self):
        quiet = [r for r in self._summary() if r["week_start"] in
                 ("2026-01-05", "2026-01-12", "2026-01-19")]
        self.assertEqual(len(quiet), 3)
        for r in quiet:
            self.assertEqual(r["project"], "")
            self.assertEqual(r["est_cost_usd"], 0)
            self.assertEqual(r["requests"], 0)
            self.assertIsNone(r["lines_added"], "unrecorded stays blank, not zero")

    def test_zero_rows_do_not_change_totals(self):
        self.assertAlmostEqual(sum(r["est_cost_usd"] for r in self._summary()),
                               sum(r[13] for r in self.payload["usage"]), places=6)

    def test_partial_weeks_are_flagged_with_their_day_count(self):
        by_week = {r["week_start"]: r for r in self._summary()}
        # Window starts Thursday 2026-01-01: Thu-Sun is 4 days.
        self.assertEqual(by_week["2025-12-29"]["week_days_in_window"], 4)
        self.assertEqual(by_week["2025-12-29"]["partial_week"], "yes")
        self.assertEqual(by_week["2026-01-12"]["week_days_in_window"], 7)
        self.assertEqual(by_week["2026-01-12"]["partial_week"], "no")

    def test_active_days_count_distinct_request_days(self):
        rows = [r for r in self._summary() if r["project"]]
        expected = {}
        for u in self.payload["usage"]:
            day = u[0][:10]
            week = export_mod._week_start(day)
            expected.setdefault(week, set()).add(day)
        for r in rows:
            self.assertLessEqual(r["active_days"], len(expected.get(r["week_start"], ())))
        self.assertEqual(self._ctx()["person_active_days"],
                         len({u[0][:10] for u in self.payload["usage"]}))

    def test_seat_use_columns(self):
        ctx = self._ctx()
        self.assertEqual(ctx["last_active_date"], "2026-02-05")
        self.assertEqual(ctx["seat_idle"], "no")
        # Thu 2026-01-01 .. Sun 2026-02-08 = 2 + 25 = 27 weekdays
        self.assertEqual(ctx["window_workdays"], 27)
        later = export_mod.build_context(self.payload, PERSON, now=self.NOW + datetime.timedelta(days=30))
        self.assertEqual(later["seat_idle"], "yes")

    def test_a_seat_with_no_usage_is_idle_and_still_has_a_row(self):
        # No usage and no date range: the file must still say whose it is and
        # that the seat is idle, or the team view counts it as active.
        empty = {**self.payload, "usage": [], "sessions": [], "tools": [], "friction": [],
                 "limits": [], "prs": [], "agents": []}
        ctx = export_mod.build_context(empty, PERSON, now=self.NOW)
        self.assertEqual(ctx["seat_idle"], "yes")
        self.assertEqual((ctx["window_from"], ctx["window_to"]), ("2026-02-08", "2026-02-08"))
        self.assertEqual(ctx["person_active_days"], 0)
        rows = _as_dicts(export_mod.SUMMARY_COLUMNS,
                         export_mod.summary_rows(empty, PERSON, ctx=ctx))
        self.assertEqual([r["week_start"] for r in rows], ["2026-02-02"])
        self.assertEqual(rows[0]["seat_idle"], "yes")

    def test_weeks_before_deleted_cli_history_are_marked_incomplete(self):
        ctx = self._ctx(cli_history_from="2026-01-20", cli_history_truncated=True)
        by_week = {r["week_start"]: r["cli_history_complete"] for r in self._summary(ctx=ctx)}
        self.assertEqual(by_week["2026-01-12"], "no")
        self.assertEqual(by_week["2026-01-26"], "yes")

    def test_untruncated_history_is_complete_everywhere(self):
        self.assertEqual({r["cli_history_complete"] for r in self._summary()}, {"yes"})

    def test_cli_sessions_are_counted_per_row(self):
        rows = self._summary()
        self.assertEqual(sum(r["sessions_active_cli"] for r in rows), 1)

    def test_project_repo_falls_back_to_pr_repo_for_a_missing_folder(self):
        # /Users/x/proj1 does not exist; its PR was opened against x/proj1.
        rows = self._sessions()
        self.assertEqual({r["project_repo"] for r in rows}, {"github.com/x/proj1"})



class TestBreakdown(unittest.TestCase):
    """The long-format file. Its whole value is that it can be pivoted any way
    and still agree with the other two sheets, so that is what is tested."""

    MEASURES = {"requests": "requests", "input_tokens": "input_tokens",
                "output_tokens": "output_tokens", "cache_read_tokens": "cache_read_tokens",
                "cache_write_tokens": "cache_write_tokens", "total_tokens": "total_tokens",
                "thinking_tokens": "thinking_tokens", "est_cost_usd": "est_cost_usd",
                "cache_saved_usd": "cache_saved_usd"}

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        ctx = export_mod.build_context(cls.payload, PERSON)
        cls.rows = _as_dicts(export_mod.BREAKDOWN_COLUMNS,
                             export_mod.breakdown_rows(cls.payload, PERSON, ctx=ctx))
        cls.summary = _as_dicts(export_mod.SUMMARY_COLUMNS,
                                export_mod.summary_rows(cls.payload, PERSON, ctx=ctx))
        cls.sessions = _as_dicts(export_mod.SESSION_COLUMNS,
                                 export_mod.session_rows(cls.payload, PERSON, ctx=ctx))

    def test_has_rows_for_both_threads(self):
        self.assertEqual({r["thread"] for r in self.rows}, {"main", "subagent"})

    def test_every_week_and_project_sums_to_its_summary_row(self):
        summary = {(r["week_start"], r["project"]): r for r in self.summary if r["project"]}
        sums = {}
        for r in self.rows:
            key = (r["week_start"], r["project"])
            agg = sums.setdefault(key, {m: 0 for m in self.MEASURES})
            for m in self.MEASURES:
                agg[m] += r[m]
        self.assertTrue(sums)
        for key, agg in sums.items():
            for m, col in self.MEASURES.items():
                if col == "cache_saved_usd" or col == "est_cost_usd":
                    self.assertAlmostEqual(agg[m], summary[key][col], places=5, msg=(key, m))
                else:
                    self.assertEqual(agg[m], summary[key][col], (key, m))

    def test_thread_split_matches_the_cost_split(self):
        main = sum(r["est_cost_usd"] for r in self.rows if r["thread"] == "main")
        sub = sum(r["est_cost_usd"] for r in self.rows if r["thread"] == "subagent")
        self.assertAlmostEqual(main, sum(r["est_cost_main_usd"] for r in self.summary), places=5)
        self.assertAlmostEqual(sub, sum(r["est_cost_subagent_usd"] for r in self.summary), places=5)

    def test_total_cost_agrees_with_the_session_sheet(self):
        self.assertAlmostEqual(sum(r["est_cost_usd"] for r in self.rows),
                               sum(r["est_cost_usd"] for r in self.sessions), places=5)

    def test_cache_write_is_the_sum_of_its_two_lifetimes(self):
        for r in self.rows:
            self.assertEqual(r["cache_write_tokens"],
                             r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"])

    def test_main_rows_have_no_subagent_type_and_subagent_rows_do(self):
        for r in self.rows:
            if r["thread"] == "main":
                self.assertIsNone(r["subagent_type"])
            else:
                self.assertTrue(r["subagent_type"])

    def test_no_empty_rows(self):
        for r in self.rows:
            self.assertGreater(r["requests"], 0)

    def test_carries_the_shared_context(self):
        for r in self.rows:
            self.assertEqual(r["cost_basis"], "api_list_price")
            self.assertEqual(r["schema_version"], export_mod.context_mod.EXPORT_SCHEMA_VERSION)

    def test_model_family(self):
        self.assertEqual(export_mod.model_family("claude-opus-5-5"), "opus")
        self.assertEqual(export_mod.model_family("claude-haiku-4-5-20251001"), "haiku")
        self.assertEqual(export_mod.model_family("claude-sonnet-5[1m]"), "sonnet")
        self.assertEqual(export_mod.model_family("<synthetic>"), "other")
        self.assertEqual(export_mod.model_family(None), "other")

    def test_date_range_cuts_like_the_summary(self):
        since, until = "2026-02-02", "2026-02-03"
        rows = export_mod.breakdown_rows(self.payload, PERSON, since, until)
        summ = export_mod.summary_rows(self.payload, PERSON, since, until)
        cost = export_mod.BREAKDOWN_COLUMNS.index("est_cost_usd")
        scost = export_mod.SUMMARY_COLUMNS.index("est_cost_usd")
        self.assertAlmostEqual(sum(r[cost] for r in rows), sum(r[scost] for r in summ), places=6)



class TestPrFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()
        cls.ctx = export_mod.build_context(cls.payload, PERSON)
        cls.rows = _as_dicts(export_mod.PR_COLUMNS,
                             export_mod.pr_rows(cls.payload, PERSON, ctx=cls.ctx))

    def test_one_row_per_pr(self):
        self.assertEqual(len(self.rows), len(self.payload["prs"]))
        self.assertEqual(sorted(r["pr_url"] for r in self.rows),
                         sorted(p["url"] for p in self.payload["prs"]))

    def test_repo_comes_from_the_prs_own_url(self):
        r = self.rows[0]
        self.assertEqual(r["pr_repo"], "github.com/x/proj1")
        self.assertEqual(r["in_project_repo"], "yes")
        self.assertEqual(r["session_prs"], 1)
        self.assertGreater(r["session_cost_usd"], 0)

    def test_a_pr_in_another_repo_is_marked(self):
        payload = json.loads(json.dumps(self.payload))
        payload["prs"].append({"session_id": payload["prs"][0]["session_id"], "date": "2026-01-01",
                               "repo": "acme/elsewhere", "number": 9,
                               "url": "https://github.com/Acme/Elsewhere/pull/9"})
        rows = _as_dicts(export_mod.PR_COLUMNS, export_mod.pr_rows(payload, PERSON, ctx=self.ctx))
        other = next(r for r in rows if r["pr_number"] == 9)
        self.assertEqual(other["pr_repo"], "github.com/acme/elsewhere")
        self.assertEqual(other["in_project_repo"], "no")
        self.assertEqual({r["session_prs"] for r in rows}, {2}, "two PRs share one session")

    def test_date_range(self):
        self.assertEqual(export_mod.pr_rows(self.payload, PERSON, since="2026-01-02", ctx=self.ctx), [])

    def test_sessions_sheet_lists_pr_repos(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, ctx=self.ctx))
        with_pr = [r for r in rows if r["prs_opened"]]
        self.assertTrue(with_pr)
        self.assertEqual({r["pr_repos"] for r in with_pr}, {"github.com/x/proj1"})


class TestLocalSessionDates(unittest.TestCase):
    """Session start and end are UTC in the logs; the export writes them in the
    export zone. A session at 22:00 Sunday in New York starts at 03:00 Monday
    UTC, and belongs to Sunday -- the day, and the week, its own requests are
    in. The zone is passed explicitly: nothing here depends on the machine's."""

    # A fixed -05:00 (New York in winter), so this needs no zone data.
    ZONE = (datetime.timezone(datetime.timedelta(hours=-5)), "America/New_York")
    NOW = datetime.datetime(2026, 2, 9, 12, 0, tzinfo=datetime.timezone.utc)

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def setUp(self):
        self.payload = copy.deepcopy(self.payload)
        s = next(x for x in self.payload["sessions"] if not x.get("derived"))
        self.sid = s["id"]
        s["start"], s["end"] = "2026-02-02T03:00:00.000Z", "2026-02-02T03:30:00.000Z"
        for r in self.payload["usage"]:
            if r[1] == self.sid:
                r[0] = "2026-02-01T22:00"
        self.ctx = export_mod.build_context(self.payload, PERSON, now=self.NOW, zone=self.ZONE)

    def test_started_and_ended_at_are_in_the_export_zone(self):
        rows = _as_dicts(export_mod.SESSION_COLUMNS,
                         export_mod.session_rows(self.payload, PERSON, ctx=self.ctx))
        row = next(r for r in rows if r["session_id"] == self.sid)
        self.assertEqual(row["started_at"], "2026-02-01T22:00:00-05:00")
        self.assertEqual(row["ended_at"], "2026-02-01T22:30:00-05:00")

    def test_date_filter_uses_the_export_zone_day(self):
        def ids(since, until):
            ctx = export_mod.build_context(self.payload, PERSON, since, until, now=self.NOW, zone=self.ZONE)
            return [r[2] for r in export_mod.session_rows(self.payload, PERSON, since=since, until=until, ctx=ctx)]
        self.assertIn(self.sid, ids("2026-02-01", "2026-02-01"))
        self.assertNotIn(self.sid, ids("2026-02-02", "2026-02-08"))

    def test_session_facts_land_in_the_export_zone_week(self):
        ctx = export_mod.build_context(self.payload, PERSON, "2026-02-02", "2026-02-08",
                                       now=self.NOW, zone=self.ZONE)
        rows = _as_dicts(export_mod.SUMMARY_COLUMNS,
                         export_mod.summary_rows(self.payload, PERSON, since="2026-02-02",
                                                 until="2026-02-08", ctx=ctx))

        def zone_day(ts):
            moment = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return moment.astimezone(self.ZONE[0]).date().isoformat()
        expected = sum(1 for x in self.payload["sessions"] if not x.get("derived")
                       and "2026-02-02" <= zone_day(x["start"]) <= "2026-02-08")
        self.assertEqual(sum(int(r["sessions_started"]) for r in rows), expected)


class TestFormulaInjection(unittest.TestCase):
    """Text a person controls -- a session title, a branch, a folder name, a
    git name -- must not run as a formula when a manager opens the CSV in
    Excel. OWASP: prefix such a cell with a single quote."""

    @classmethod
    def setUpClass(cls):
        cls.payload = _payload()

    def test_csv_safe(self):
        for raw in ("=HYPERLINK(\"http://x\")", "+1+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"):
            self.assertEqual(export_mod.csv_safe(raw), "'" + raw, repr(raw))
        for kept in ("plain", "-0.25", "+3", "-1e-05", "-04:00", "+05:30", "", None, -1.5, 7,
                     "'=already"):
            self.assertEqual(export_mod.csv_safe(kept), kept, repr(kept))

    def test_user_text_is_neutralised_in_every_written_file(self):
        payload = copy.deepcopy(self.payload)
        s = next(x for x in payload["sessions"] if not x.get("derived"))
        s["title"] = "=HYPERLINK(\"http://evil\",\"click\")"
        s["branches"] = {"@SUM(1+1)": 3}
        person = ("+evil@x.io", "-2+3")
        rows = list(csv.reader(io.StringIO(
            export_mod.csv_text(payload, "sessions", person))))
        row = next(dict(zip(rows[0], r)) for r in rows[1:] if r[2] == s["id"])
        self.assertEqual(row["title"], "'=HYPERLINK(\"http://evil\",\"click\")")
        self.assertEqual(row["git_branch"], "'@SUM(1+1)")
        self.assertEqual(row["person_email"], "'+evil@x.io")
        self.assertEqual(row["person_name"], "'-2+3")
        with tempfile.TemporaryDirectory() as out:
            for path, _ in export_mod.export(payload, out, person=person):
                with open(path, encoding="utf-8") as fh:
                    for line in list(csv.reader(fh))[1:]:
                        # Numbers and UTC offsets ("+00:00" on a UTC runner) only
                        # look like formulas; anything else must be quoted.
                        risky = [c for c in line if c[:1] and c[:1] in "=+-@\t\r"
                                 and not export_mod._HARMLESS.fullmatch(c)]
                        self.assertEqual(risky, [], path)


class TestOrphanSubagentCostIsExported(unittest.TestCase):
    """A subagent whose parent transcript is gone has no session row, but its
    cost is in the dashboard total, so every cost-bearing sheet must carry it
    -- under the project it was placed in -- or a team summary built from the
    exports under-counts by exactly that spend."""

    def setUp(self):
        from tests.test_health import _response, _ts, _tool_use, _write
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "projects")
        _write(os.path.join(root, "-Users-x-secretA", "main.jsonl"),
               [_response("m1", _ts(0)), _response("m2", _ts(1))])
        sub = os.path.join(root, "-Users-x-secretB", "gone", "subagents", "agent-o.jsonl")
        _write(sub, [_tool_use("o1", _ts(5), "tu-1", "Read", {"file_path": "f"}),
                     _response("o2", _ts(6))])
        self.payload = scan(root, cache=FileCache(os.path.join(self.tmp.name, "c.json")))
        (self.orphan,) = self.payload["agents"]
        self.total = sum(r[13] for r in self.payload["usage"])

    def _rows(self, kind, payload=None):
        text = export_mod.csv_text(payload or self.payload, kind, PERSON)
        return list(csv.DictReader(io.StringIO(text)))

    def test_the_orphan_has_no_session_row_and_real_cost(self):
        self.assertEqual(len(self.payload["sessions"]), 1)
        self.assertGreater(self.orphan["cost"], 0)

    def test_summary_total_equals_the_dashboard(self):
        rows = self._rows("summary")
        self.assertAlmostEqual(sum(float(r["est_cost_usd"]) for r in rows), self.total, places=5)
        self.assertEqual(sum(int(r["requests"]) for r in rows), 4)

    def test_breakdown_total_equals_the_dashboard(self):
        rows = self._rows("breakdown")
        self.assertAlmostEqual(sum(float(r["est_cost_usd"]) for r in rows), self.total, places=5)

    def test_the_orphans_cost_lands_under_its_own_project(self):
        for kind in ("summary", "breakdown"):
            by_project = {}
            for r in self._rows(kind):
                by_project[r["project"]] = by_project.get(r["project"], 0.0) + float(r["est_cost_usd"])
            self.assertAlmostEqual(by_project[self.orphan["project"]], self.orphan["cost"], places=5, msg=kind)

    def test_tool_calls_of_the_orphan_are_exported(self):
        rows = self._rows("tools")
        self.assertEqual(sum(int(r["calls"]) for r in rows), sum(r[3] for r in self.payload["tools"]))
        self.assertIn(self.orphan["project"], {r["project"] for r in rows})

    def test_sessions_sheet_still_has_no_row_for_it(self):
        rows = self._rows("sessions")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(sum(float(r["est_cost_usd"]) for r in rows),
                               self.total - self.orphan["cost"], places=5)

    def test_redacted_export_hides_the_folder_paths(self):
        import collect
        red = collect._redact(self.payload)
        for kind in export_mod.KINDS:
            text = export_mod.csv_text(red, kind, PERSON)
            for needle in ("secret", "Users"):
                self.assertNotIn(needle, text, kind)
        rows = self._rows("summary", red)
        self.assertAlmostEqual(sum(float(r["est_cost_usd"]) for r in rows), self.total, places=5)


class TestWeekActiveDays(unittest.TestCase):
    def test_distinct_days_per_week_across_projects(self):
        payload = _payload()
        cols = export_mod.SUMMARY_COLUMNS
        self.assertEqual(cols[-1], "week_active_days")
        rows = [dict(zip(cols, r)) for r in export_mod.summary_rows(payload, PERSON)]
        self.assertTrue(rows)
        days = defaultdict(set)
        for u in payload["usage"]:
            if u[0]:
                d = export_mod._day(u[0])
                days[export_mod._week_start(d)].add(d)
        self.assertTrue(days)
        by_week = defaultdict(set)
        for r in rows:
            by_week[r["week_start"]].add(r["week_active_days"])
        for week, values in by_week.items():
            self.assertEqual(values, {len(days.get(week, ()))}, week)
        self.assertEqual(sum(next(iter(v)) for v in by_week.values()), rows[0]["person_active_days"])


if __name__ == "__main__":
    unittest.main()
