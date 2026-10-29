"""CSV export for business review: what Claude Code usage cost, and what came out of it.

Produces six files from an already-built payload:

* ``...-summary-...csv``   — one row per person x project x week
* ``...-sessions-...csv``  — one row per session
* ``...-breakdown-...csv`` — one row per person x week x project x model x
  thread (main / subagent type): the long-format fact table for pivoting
* ``...-limits-...csv``    — one row per usage-limit episode
* ``...-prs-...csv``       — one row per pull request, with the repo it was
  opened in (which is often not the project folder's own repo)
* ``...-tools-...csv``     — one row per person x week x project x tool: calls
  and errors, with the MCP integration a tool belongs to

Every row carries the person, so a manager can concatenate several people's
files (Excel, Power BI, ``cat``) and get a team view without any central
collection. The tool only ever reads one machine's logs, so the per-person file
is the unit that travels.

Three conventions the numbers depend on -- see docs/export.md:

1. **Missing is blank, not zero.** ``lines_added`` is empty when the session
   made no edit that left a diff in its transcript (it is counted from each
   edit's own result). A zero would read as
   "delivered nothing" and silently drag down any average computed over it. The
   ``sessions_with_*`` coverage columns say how many sessions actually carried
   each signal, so a denominator is never accidentally incomplete.
2. **Fork/resume copies are excluded**, from both files. They are duplicate
   transcripts of work already counted; including them would double-count cost
   for anyone who sums the session sheet.
3. **Session-level facts land in the week the session started.** Cost and tokens
   are per-request and split across weeks naturally, but active time and the
   session's own totals (lines changed, the PRs it opened) belong to a session
   that may span weeks. Tool calls and errors, PRs and the health counts are
   dated by the call, PR or event itself.

Every sheet names the zone its days are in (``bucket_zone``): the export zone
(``--tz``, else ``CLAUDE_USAGE_EXPORT_TZ``, else UTC), not the machine's. The
dashboard's date range is read in the machine's zone, so its totals equal an
export's only for the same days in the same zone.
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import re
import subprocess
import zipfile
from collections import Counter, defaultdict

from . import context as context_mod
from . import health as health_mod

SUMMARY_COLUMNS = [
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
    # Coverage: how many of this row's sessions actually carried each signal.
    "sessions_with_line_data", "sessions_with_pr_data", "sessions_with_cli_cost",
    # Coverage by *spend*, not just by session count. On a real log set only 30%
    # of sessions report lines changed, but those sessions are 66% of cost --
    # so the count alone makes a cost-per-line ratio look far less usable than
    # it is. Whole-session cost, attributed to the week the session started,
    # matching how lines and PRs are attributed.
    "cost_with_line_data_usd", "cost_with_pr_data_usd",
    # Appended rather than placed beside the cost columns: this schema is
    # already in use, and inserting a column mid-row silently breaks anyone
    # reading by position.
    "cache_saved_usd",
    # The sessions sheet always had the main-thread share; without it here a
    # leader had to derive it as est_cost_usd - est_cost_subagent_usd.
    "est_cost_main_usd",
    "cost_basis",
    # schema 2 -- see docs/export.md
    "project_repo", "sessions_active_cli", "active_days",
    "week_days_in_window", "partial_week", "cli_history_complete",
]

SESSION_COLUMNS = [
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
    "cache_saved_usd", "is_fork_copy",
    "cost_basis",
    # schema 2
    "project_repo",
]

# The same on every row of both sheets: who, on what plan, and where the file
# came from. Repeated per row rather than written once in a header block so
# that rows stay self-describing after files from many people are stacked.
CONTEXT_COLUMNS = [
    "claude_account_email", "seat_tier", "rate_limit_tier", "extra_usage_enabled",
    "person_active_days", "window_workdays", "last_active_date", "seat_idle",
    "cli_history_from", "cli_history_truncated",
    "schema_version", "exported_at", "window_from", "window_to", "timezone",
    "tool_version", "pricing_version",
]
SUMMARY_COLUMNS += CONTEXT_COLUMNS
SESSION_COLUMNS += CONTEXT_COLUMNS

# schema 4: friction and limits. Appended after the schema-2 context block so
# no released column moves.
FRICTION_COLUMNS = [
    "api_retries", "retry_wait_minutes", "turns", "long_turns",
    "context_compactions", "limit_hits", "blocked_hours",
]
SUMMARY_COLUMNS += FRICTION_COLUMNS + ["turn_hours"]
SESSION_COLUMNS += FRICTION_COLUMNS + ["turn_minutes"]

# Where this person's weekly allowance stands at export time; see
# limits.allowance_snapshot. Blank when the weekly limit has never been hit,
# because there is nothing to calibrate the estimate against.
ALLOWANCE_COLUMNS = [
    "allowance_est_usd", "allowance_week_start", "allowance_week_used_usd",
    "allowance_week_used_pct", "allowance_status", "projected_limit_hit_at",
]
SUMMARY_COLUMNS += ALLOWANCE_COLUMNS
SESSION_COLUMNS += ALLOWANCE_COLUMNS
# schema 5: the repos a session's PRs went to, as repo keys.
SESSION_COLUMNS += ["pr_repos"]
CONTEXT_COLUMNS = CONTEXT_COLUMNS + ALLOWANCE_COLUMNS

# A turn this long is worth knowing about: someone waited, or walked away.
LONG_TURN_MS = 10 * 60 * 1000

# Long format: one row per combination that had usage, every measure a column.
# This is the shape Excel pivot tables and Power BI want, and it answers the
# "by model" and "main vs subagent" questions without a column per model --
# a new model or subagent type becomes rows, not a schema change.
BREAKDOWN_COLUMNS = [
    "person_email", "person_name", "week_start", "project", "project_repo",
    "model", "model_family", "thread", "subagent_type",
    "requests",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_tokens",
    "total_tokens", "thinking_tokens",
    "est_cost_usd", "cache_saved_usd", "cost_basis",
] + CONTEXT_COLUMNS

# One row per limit episode: every notice of one limit with one reset time.
LIMIT_COLUMNS = [
    "person_email", "person_name", "limit_type", "hit_at", "last_notice_at",
    "resets_at", "notices", "locked_out", "blocked_hours", "resumed_at",
    "usage_before_hit_usd", "usage_after_hit_usd", "requests_after_hit",
    "project", "project_repo", "session_id", "cost_basis",
] + CONTEXT_COLUMNS

# One row per pull request. `pr_repo` is the repo the PR was opened in, from its
# own URL. A session started in one folder regularly opens PRs elsewhere -- on
# a real log set, PRs from one folder went to three different repos -- so
# grouping PRs by `project` alone misattributes them.
PR_COLUMNS = [
    "person_email", "person_name", "pr_url", "pr_repo", "pr_number", "opened_on",
    "session_id", "project", "project_repo", "in_project_repo",
    "session_cost_usd", "session_prs", "cost_basis",
] + CONTEXT_COLUMNS

# schema 6: which machine the export came from, so the team merge adds a
# person's machines together instead of treating the second as a re-export.
# Last on every sheet, so no released column moves.
MACHINE_COLUMNS = ["machine_id"]
for _columns in (SUMMARY_COLUMNS, SESSION_COLUMNS, BREAKDOWN_COLUMNS, LIMIT_COLUMNS, PR_COLUMNS):
    _columns += MACHINE_COLUMNS
CONTEXT_COLUMNS = CONTEXT_COLUMNS + MACHINE_COLUMNS

# schema 7: usage health (docs/usage-health.md) -- counts and dollars only,
# never a prompt, command or file. Appended last, so no released column moves.
# Per-request measures land in the week they happened; the sessions_* ones in
# the week the session started, like lines and PRs.
HEALTH_SUMMARY_COLUMNS = [
    "long_context_cost_usd", "cold_cache_usd", "cache_miss_usd",
    "prompts", "interrupts", "auto_compactions", "manual_compactions", "tool_refusals",
    "error_streaks", "test_runs", "lint_runs", "build_runs", "commits",
    "sessions_edited", "sessions_checked_after_edit", "sessions_plan_mode",
    "sessions_with_subagents", "sessions_instructions_recorded",
    "sessions_with_project_claude_md",
]
HEALTH_SESSION_COLUMNS = [
    "peak_context_tokens", "long_context_cost_usd", "cold_cache_usd", "prompts",
    "interrupts", "auto_compactions", "tool_refusals", "edits", "checked_after_last_edit",
    "max_error_streak", "plan_mode", "project_claude_md", "ended_on_interrupt",
]
SUMMARY_COLUMNS += HEALTH_SUMMARY_COLUMNS
SESSION_COLUMNS += HEALTH_SESSION_COLUMNS

# schema 7: tool calls and errors by tool, so a team can see which integration
# keeps failing for everyone. `integration` is the MCP server a tool belongs
# to, or "built-in".
TOOL_COLUMNS = [
    "person_email", "person_name", "week_start", "project", "project_repo",
    "tool", "integration", "calls", "errors", "cost_basis",
] + CONTEXT_COLUMNS

# schema 8: the zone every day and week in the file was bucketed in -- the
# export zone (--tz, else CLAUDE_USAGE_EXPORT_TZ, else UTC), not the machine's.
# Blank in older files, which were bucketed in the machine's own zone. Last on
# every sheet, so no released column moves.
ZONE_COLUMNS = ["bucket_zone"]
for _columns in (SUMMARY_COLUMNS, SESSION_COLUMNS, BREAKDOWN_COLUMNS, LIMIT_COLUMNS,
                 PR_COLUMNS, TOOL_COLUMNS):
    _columns += ZONE_COLUMNS
CONTEXT_COLUMNS = CONTEXT_COLUMNS + ZONE_COLUMNS

# schema 9: subagent output imputed where the transcript kept only a placeholder
# count (already inside output_tokens and est_cost_*; this says how much), and
# what Claude Code's own counter recorded that no transcript did -- blank
# without a counter, and never inside est_cost_usd. Last, so no released
# column moves.
SUMMARY_COLUMNS += ["output_tokens_imputed", "est_cost_unlogged_usd"]
SESSION_COLUMNS += ["output_tokens_imputed", "est_cost_unlogged_usd"]
BREAKDOWN_COLUMNS += ["output_tokens_imputed"]

# schema 10: context tax is flagged on requests over 500k tokens
# (health.VERY_LONG_CONTEXT_TOKENS); long_context_cost_usd keeps its 200k meaning.
SUMMARY_COLUMNS += ["very_long_context_cost_usd"]
SESSION_COLUMNS += ["very_long_context_cost_usd"]

# schema 11: the person's distinct active days in each week, across projects,
# on every summary row of the week -- so seat use can be measured for any range.
SUMMARY_COLUMNS += ["week_active_days"]

_MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")

# What every est_cost_* and cache_saved_usd figure is priced at: public API
# list rates, recomputed from token counts. Teams on a contracted seat plan
# aren't billed these rates, so they are a measure of how much was used -- not an invoice line.
# Carried as a column rather than left to the docs because the CSV travels
# without them.
COST_BASIS = "api_list_price"


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# A spreadsheet runs a cell that starts with one of these as a formula. Titles,
# branches, folder names and git names are the person's own text, and a
# manager opens their CSV in Excel; OWASP's guidance is a leading quote.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# ...except numbers (a negative cost) and UTC offsets ("-04:00"), which only
# look like formulas.
_HARMLESS = re.compile(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?|[-+]\d\d:\d\d")


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

def _git_config(key: str):
    try:
        out = subprocess.run(["git", "config", "--get", key],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def resolve_identity(override: str | None = None):
    """(email, name) for whoever is running the export.

    Prefers git's configured identity, since that is what the person's commits
    and PRs are attributed to and therefore what a leader can line this up
    against. Falls back to the OS account name.
    """
    if override:
        return override, _git_config("user.name") or override
    email = _git_config("user.email")
    name = _git_config("user.name")
    if not email:
        email = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return email, name or email


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _day(value):
    """Date from a payload bucket ("2026-10-02T14:35") or a date. The payload's
    buckets are already in the export zone, so this needs no zone of its own."""
    return str(value)[:10] if value else None


def _week_start(day: str):
    """Monday of the ISO week containing `day`."""
    if not day:
        return None
    d = datetime.date.fromisoformat(day)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _in_range(day, since, until):
    if not day:
        return False
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def csv_safe(value):
    """`value`, made safe to open in a spreadsheet: text that would run as a
    formula gets a leading single quote. Numbers and blanks pass through."""
    if isinstance(value, str) and value.startswith(_FORMULA_START) and not _HARMLESS.fullmatch(value):
        return "'" + value
    return value


def _safe_rows(rows):
    return ([csv_safe(v) for v in row] for row in rows)


def _local_time(ts, tz=None):
    """A transcript timestamp ("...Z") as time in the export zone with its
    offset, matching every other date in the export (tz None: machine-local)."""
    if not ts:
        return ts
    try:
        moment = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ts
    if moment.tzinfo is None:
        return ts
    return moment.astimezone(tz).isoformat(timespec="seconds")


def _join(values):
    return "; ".join(sorted(v for v in values if v))


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

def build_context(payload, person, since=None, until=None, now=None, zone=None):
    """Everything that is the same on every row of one export, computed once so
    the two sheets in a zip can never disagree about it (e.g. `exported_at`).

    `zone` is the (tzinfo, label) of the zone the export's days and weeks are in
    -- context.export_zone() when not given -- and the payload must have been
    scanned in it. `now`, the today cap, `since`/`until` and every time written
    to the file are read in that zone, never the machine's.

    Keys starting with "_" are for the row builders, not for output.
    """
    tz, label = zone if zone is not None else context_mod.export_zone()
    now = now.astimezone(tz) if now else context_mod.now_in(tz)
    today = now.date()
    days = sorted({_day(r[0]) for r in payload.get("usage") or [] if r[0]})
    # Capped at today: a file cannot vouch for weeks that have not happened, and
    # an uncapped window would pad an export for "2030" with empty weeks.
    window_to = min(until or today.isoformat(), today.isoformat())
    # The window the file claims to cover: what was asked for, else what the
    # logs on this machine actually reach back to. With no logs at all (Claude
    # Code deletes CLI transcripts after 30 days by default, so a person who has
    # been away that long has none) the seat was idle for at least the retention
    # period, so the window is that long, ending today -- not the one day that
    # would price the seat at a day's cost and call every earlier week "not
    # covered". Such a file has person_active_days 0 and no last_active_date;
    # pass --since to say how far back the person really was a seat holder.
    window_from = since or (days[0] if days else (
        datetime.date.fromisoformat(window_to)
        - datetime.timedelta(days=context_mod.NO_LOGS_WINDOW_DAYS - 1)).isoformat())
    in_window = [d for d in days if _in_range(d, window_from, window_to)]
    past = [d for d in days if d <= today.isoformat()]
    last_active = past[-1] if past else None
    idle = "yes"  # no usage in the logs at all is an idle seat
    if last_active:
        quiet = (today - datetime.date.fromisoformat(last_active)).days
        idle = "yes" if quiet > context_mod.IDLE_AFTER_DAYS else "no"
    history = context_mod.history_coverage(payload, today, tz)

    ctx = dict(context_mod.account_info())
    ctx.update({
        "person_active_days": len(in_window),
        "window_workdays": context_mod.workdays_between(window_from, window_to),
        "last_active_date": last_active,
        "seat_idle": idle,
        "cli_history_from": history["cli_history_from"],
        "cli_history_truncated": "yes" if history["cli_history_truncated"] else "no",
        "schema_version": context_mod.EXPORT_SCHEMA_VERSION,
        "exported_at": now.isoformat(timespec="seconds"),
        "window_from": window_from,
        "window_to": window_to,
        # The zone's offset at export time, so it still means "the zone of the
        # dates in this file"; `bucket_zone` names it.
        "timezone": context_mod.utc_offset(now),
        "bucket_zone": label,
        "tool_version": context_mod.tool_version(),
        "pricing_version": context_mod.pricing_version(),
        "machine_id": context_mod.machine_id(),
        **_allowance_context(payload.get("allowance")),
        "_history": history,
        "_tz": tz,
        "_repos": context_mod.project_repos(payload),
    })
    return ctx


def _allowance_context(snap):
    if not snap:
        return {c: None for c in ALLOWANCE_COLUMNS}
    return {
        "allowance_est_usd": snap.get("allowance_est_usd"),
        "allowance_week_start": snap.get("week_start"),
        "allowance_week_used_usd": snap.get("used_usd"),
        "allowance_week_used_pct": snap.get("used_pct"),
        "allowance_status": snap.get("status"),
        "projected_limit_hit_at": snap.get("projected_hit_at"),
    }


def _friction_by(payload, key_of):
    """Friction totals grouped by key_of(bucket_day, session_id) (None = skip)."""
    acc = defaultdict(lambda: {"api_retries": 0, "retry_ms": 0, "turns": 0,
                               "turn_ms": 0, "long_turns": 0, "context_compactions": 0})
    for bucket_key, sid, kind, value in payload.get("friction") or []:
        key = key_of(_day(bucket_key), sid)
        if key is None:
            continue
        a = acc[key]
        if kind == "retry":
            a["api_retries"] += 1
            a["retry_ms"] += value or 0
        elif kind == "turn":
            a["turns"] += 1
            a["turn_ms"] += value or 0
            if (value or 0) >= LONG_TURN_MS:
                a["long_turns"] += 1
        elif kind == "compact":
            a["context_compactions"] += 1
    return acc


def _limits_by(payload, key_of, tz=None):
    acc = defaultdict(lambda: {"limit_hits": 0, "blocked_hours": 0.0})
    for ep in payload.get("limits") or []:
        key = key_of(context_mod._local_day(ep.get("hit_at"), tz), ep.get("session_id"))
        if key is None:
            continue
        acc[key]["limit_hits"] += 1
        acc[key]["blocked_hours"] += ep.get("blocked_hours") or 0
    return acc


def _friction_values(f, lim, turn_col, turn_div):
    f = f or {}
    lim = lim or {}
    return {
        "api_retries": f.get("api_retries", 0),
        # Enough precision that summing either sheet gives the same total; a
        # tenth of a minute per row drifted by 0.1 over a few dozen rows.
        "retry_wait_minutes": round(f.get("retry_ms", 0) / 60_000, 4),
        "turns": f.get("turns", 0),
        "long_turns": f.get("long_turns", 0),
        "context_compactions": f.get("context_compactions", 0),
        "limit_hits": lim.get("limit_hits", 0),
        "blocked_hours": round(lim.get("blocked_hours", 0.0), 4),
        turn_col: round(f.get("turn_ms", 0) / turn_div, 4),
    }


def _health_by(payload, key_of):
    """Usage-health counts and dollars from payload["health"], bucketed by
    `key_of(day, session_id)` (None skips the row), as _friction_by does."""
    out = defaultdict(lambda: defaultdict(float))
    h = payload.get("health")
    if not h:
        return out
    t = h["thresholds"]
    long_band = t["context_bands"].index(t["long_context_tokens"]) + 1
    flag = t.get("flag_context_tokens")
    very_long_band = t["context_bands"].index(flag) + 1 if flag in t["context_bands"] else None
    for bucket, sid, _model, band, _n, cost in h.get("context", ()):
        key = key_of(_day(bucket), sid)
        if key is None:
            continue
        if band >= long_band:
            out[key]["long_context_cost_usd"] += cost
        if very_long_band is not None and band >= very_long_band:
            out[key]["very_long_context_cost_usd"] += cost
    for bucket, sid, _model, reason, _n, extra, _tokens in h.get("cache_misses", ()):
        key = key_of(_day(bucket), sid)
        if key is None:
            continue
        out[key]["cache_miss_usd"] += extra
        if reason == health_mod.EXPIRED_MISS:
            out[key]["cold_cache_usd"] += extra
    events = {"prompt": "prompts", "interrupt": "interrupts", "denial": "tool_refusals"}
    for bucket, sid, kind, value, n in h.get("events", ()):
        key = key_of(_day(bucket), sid)
        if key is None:
            continue
        if kind == "compact":
            out[key]["manual_compactions" if value == "manual" else "auto_compactions"] += n
        elif kind in events:
            out[key][events[kind]] += n
    checks = {"test": "test_runs", "lint": "lint_runs", "build": "build_runs", "commit": "commits"}
    for bucket, sid, category, n in h.get("checks", ()):
        key = key_of(_day(bucket), sid)
        if key is not None and category in checks:
            out[key][checks[category]] += n
    for bucket, sid, length, n in h.get("error_streaks", ()):
        key = key_of(_day(bucket), sid)
        if key is not None and length >= t["error_streak"]:
            out[key]["error_streaks"] += n
    return out


def _health_values(measures, b):
    """The schema-7 summary columns for one row."""
    m = measures or {}
    dollars = ("long_context_cost_usd", "cold_cache_usd", "cache_miss_usd",
               "very_long_context_cost_usd")
    out = {c: round(m.get(c, 0.0), 6) for c in dollars}
    for c in HEALTH_SUMMARY_COLUMNS:
        if c not in out and not c.startswith("sessions_"):
            out[c] = int(m.get(c, 0))
    out.update({
        "sessions_edited": b["h_edited"],
        "sessions_checked_after_edit": b["h_checked"],
        "sessions_plan_mode": b["h_plan"],
        "sessions_with_subagents": b["h_subagents"],
        "sessions_instructions_recorded": b["h_instructions"],
        "sessions_with_project_claude_md": b["h_project_md"],
    })
    return out


def _yes_no(value):
    return None if value is None else ("yes" if value else "no")


def _session_health(s):
    """The schema-7 session columns. Blank where the session has no health
    block, or its Claude Code version doesn't record the measure."""
    h = s.get("health")
    if not h:
        return {c: None for c in HEALTH_SESSION_COLUMNS + ["very_long_context_cost_usd"]}
    recorded = h.get("recorded") or {}
    instructions = h.get("instructions")
    return {
        "peak_context_tokens": h.get("peak_context"),
        "long_context_cost_usd": round(h.get("long_context_cost") or 0.0, 6),
        "cold_cache_usd": round(h.get("cold_cache_cost") or 0.0, 6),
        "very_long_context_cost_usd": round(h.get("very_long_context_cost") or 0.0, 6),
        "prompts": h.get("prompts"),
        "interrupts": h.get("interrupts"),
        "auto_compactions": h.get("compactions_auto") if recorded.get("compactions", True) else None,
        "tool_refusals": (sum((h.get("denials") or {}).values())
                          if recorded.get("denials", True) else None),
        "edits": h.get("edits"),
        "checked_after_last_edit": _yes_no(h.get("checked_after_last_edit")),
        "max_error_streak": h.get("max_error_streak"),
        "plan_mode": _yes_no(h.get("plan_mode")),
        "project_claude_md": None if instructions is None else _yes_no("Project" in instructions),
        "ended_on_interrupt": _yes_no(h.get("ended_on_interrupt")),
    }


def _context_for(payload, person, since, until, ctx):
    if ctx is None:
        return build_context(payload, person, since, until)
    return ctx


def _render(columns, values, ctx):
    return [values[c] if c in values else ctx.get(c) for c in columns]


def _weeks(first, last):
    """Every Monday from the week containing `first` to the one containing `last`."""
    if not first or not last or first > last:
        return []
    week = datetime.date.fromisoformat(_week_start(first))
    end = datetime.date.fromisoformat(_week_start(last))
    out = []
    while week <= end:
        out.append(week.isoformat())
        week += datetime.timedelta(days=7)
    return out


def _days_in_window(week, window_from, window_to):
    start = datetime.date.fromisoformat(week)
    return sum(1 for i in range(7)
               if _in_range((start + datetime.timedelta(days=i)).isoformat(),
                            window_from, window_to))


def _opened_prs(session):
    """The PRs a session opened: those the payload attributes to it. A session
    row lists every PR it linked (a continuation repeats its parent's), but each
    PR is opened once, by the session with its earliest link, so summing this
    over sessions equals the PR count."""
    return [p for p in session.get("prs") or []
            if p.get("session_id", session["id"]) == session["id"]]


def summary_rows(payload, person, since=None, until=None, ctx=None):
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    # Session *records* exclude fork/resume copies, so a session is never listed
    # twice. Request- and tool-level rows are keyed by every session though:
    # the global dedupe already guarantees each request and each tool call
    # appears exactly once, and a fork's parent can still own unique subagent
    # work. Filtering those by session would drop real cost.
    sessions = {s["id"]: s for s in payload["sessions"] if not s.get("derived")}
    project_of = context_mod.project_by_key(payload)
    # The sessions sheet skips a fork copy that owns no request, so the summary's
    # health counts (prompts, interrupts...) skip it too, or the two sheets would
    # total different numbers (EX-4).
    owned = Counter()
    for r in payload["usage"]:
        owned[r[1]] += r[12]
    skipped = {s["id"] for s in payload["sessions"] if s.get("derived") and not owned[s["id"]]}
    is_cli = {s["id"]: context_mod.dominant_entrypoint(s) == "cli" for s in payload["sessions"]}

    acc = defaultdict(lambda: {
        "sessions": 0, "active_sids": set(), "active_days": set(),
        "requests": 0, "cost": 0.0, "sub_cost": 0.0,
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "thinking": 0, "web_search": 0, "web_fetch": 0,
        "tool_calls": 0, "tool_errors": 0, "active_ms": 0, "cache_saved": 0.0,
        "prs": 0, "lines_added": None, "lines_removed": None, "cli_cost": None,
        "imputed": 0, "unlogged": None,
        "cost_with_lines": 0.0, "cost_with_prs": 0.0,
        "models": set(), "skills": set(),
        "with_lines": 0, "with_prs": 0, "with_cli": 0,
        "h_edited": 0, "h_checked": 0, "h_plan": 0, "h_subagents": 0,
        "h_instructions": 0, "h_project_md": 0,
    })

    def bucket(day, project):
        return acc[(_week_start(day), project)]

    session_week_cost = defaultdict(float)  # (session, week) -> cost, for the lines below

    # ---- request-level: cost, tokens, models, skills -------------------- #
    for r in payload["usage"]:
        sid = r[1]
        if sid not in project_of:
            continue
        day = _day(r[0])
        if not _in_range(day, since, until):
            continue
        b = bucket(day, project_of[sid])
        b["active_sids"].add(sid)
        b["active_days"].add(day)
        b["requests"] += r[12]
        b["cost"] += r[13]
        session_week_cost[(sid, _week_start(day))] += r[13]
        if r[4] != -1:
            b["sub_cost"] += r[13]
        b["input"] += r[5]
        b["output"] += r[6]
        b["cache_read"] += r[7]
        b["cache_write"] += r[8] + r[9]
        b["thinking"] += r[10]
        # Net of what the cache cost to write; see aggregate._request_cache_saving.
        b["cache_saved"] += r[15] if len(r) > 15 else 0
        b["imputed"] += r[16] if len(r) > 16 else 0
        b["models"].add(r[2])
        if r[3] and r[3] != "(none)":
            b["skills"].add(r[3])

    # ---- tool-level: own timestamps, so their own week ------------------ #
    for r in payload["tools"]:
        sid = r[1]
        if sid not in project_of:
            continue
        day = _day(r[0])
        if not _in_range(day, since, until):
            continue
        b = bucket(day, project_of[sid])
        b["tool_calls"] += r[3]
        b["tool_errors"] += r[4]
        # Counted from tool calls, as the dashboard does. The usage record's
        # server_tool_use counters belong to the Messages API's server-side web
        # tool, which Claude Code's own WebSearch/WebFetch never touch, so
        # reading them exported a column of zeros next to hundreds of lookups.
        if r[2] == "WebSearch":
            b["web_search"] += r[3]
        elif r[2] == "WebFetch":
            b["web_fetch"] += r[3]

    # ---- session-level: attributed to the session's starting week ------- #
    for sid, s in sessions.items():
        day = context_mod._local_day(s.get("start"), ctx["_tz"])
        if not _in_range(day, since, until):
            continue
        b = bucket(day, s["project"])
        # A transcript that never got a reply is listed on the sessions sheet
        # but isn't a session (the dashboard's rule), so it isn't counted
        # (schema 12). A payload without the field counts, as before.
        counted = bool(s.get("requests", 1))
        if counted:
            b["sessions"] += 1
        b["active_ms"] += s.get("active_ms") or 0
        session_cost = (s.get("est_cost_main") or 0.0) + (s.get("est_cost_sub") or 0.0)
        if _opened_prs(s):
            b["with_prs"] += 1
            b["cost_with_prs"] += session_cost
        # Blank, not zero, when the signal was never recorded. (From the
        # per-edit rows below, by the day of each edit; a payload without them
        # falls back to the session's total, in its starting week.)
        if "lines" not in payload and s.get("lines_added") is not None:
            b["lines_added"] = (b["lines_added"] or 0) + s["lines_added"]
            b["lines_removed"] = (b["lines_removed"] or 0) + (s.get("lines_removed") or 0)
            b["with_lines"] += 1
            b["cost_with_lines"] += session_cost
        if s.get("cli_cost") is not None:
            b["cli_cost"] = (b["cli_cost"] or 0.0) + s["cli_cost"]
            b["with_cli"] += 1
        # Like cli_cost, a whole-counter figure, so it lands in the week the
        # session started.
        if s.get("est_cost_unlogged") is not None:
            b["unlogged"] = (b["unlogged"] or 0.0) + s["est_cost_unlogged"]
        # The numerators of the sessions_* shares count only sessions that
        # sessions_started counts, so a share never has a transcript that got no
        # reply on top (TM-8).
        h = (s.get("health") or {}) if counted else {}
        if h.get("edits"):
            b["h_edited"] += 1
            b["h_checked"] += 1 if h.get("checked_after_last_edit") else 0
        b["h_plan"] += 1 if h.get("plan_mode") else 0
        b["h_subagents"] += 1 if counted and s.get("n_subagents") else 0
        if h.get("instructions") is not None:
            b["h_instructions"] += 1
            b["h_project_md"] += 1 if "Project" in h["instructions"] else 0

    # Lines changed, by the day of each edit (like requests and tool calls), not
    # by the week the session started: a long session's lines land in the weeks
    # it wrote them. The cost beside them is the session's cost in that same
    # week, so lines per dollar compares like with like.
    line_weeks = set()
    for bucket_key, sid, added, removed in payload.get("lines") or []:
        day = _day(bucket_key)
        if sid not in project_of or not _in_range(day, since, until):
            continue
        b = bucket(day, project_of[sid])
        b["lines_added"] = (b["lines_added"] or 0) + (added or 0)
        b["lines_removed"] = (b["lines_removed"] or 0) + (removed or 0)
        if (sid, _week_start(day)) not in line_weeks:
            line_weeks.add((sid, _week_start(day)))
            b["with_lines"] += 1
            b["cost_with_lines"] += session_week_cost.get((sid, _week_start(day)), 0.0)

    # PRs are counted from the payload's URL-deduped list rather than from
    # session membership. A handful of PRs survive only inside a fork/resume
    # transcript, and counting via sessions would silently drop those.
    for pr in payload.get("prs") or []:
        day = _day(pr.get("date"))
        if not _in_range(day, since, until):
            continue
        project = project_of.get(pr.get("session_id"))
        if project is None:
            continue
        bucket(day, project)["prs"] += 1

    # ---- friction and limits: by event time, like requests -------------- #
    def week_project(day, sid):
        if sid not in project_of or not _in_range(day, since, until):
            return None
        return (_week_start(day), project_of[sid])
    friction = _friction_by(payload, week_project)
    limit_hits = _limits_by(payload, week_project, ctx["_tz"])
    health = _health_by(payload, lambda day, sid: None if sid in skipped else week_project(day, sid))
    for key in list(friction) + list(limit_hits) + list(health):
        acc[key]  # a week with only friction (e.g. all retries failed) still gets a row

    # ---- weeks with no activity at all ---------------------------------- #
    # Without these, averaging a person's weekly rows skips every quiet week and
    # overstates a typical week. One row per empty week, with no project, is
    # enough: a per-project average should divide by weeks in the window.
    window_from, window_to = ctx["window_from"], ctx["window_to"]
    active_weeks = {week for week, _ in acc}
    for week in _weeks(window_from, window_to):
        if week not in active_weeks:
            acc[(week, "")]  # materialise an empty bucket

    history = ctx["_history"]
    cli_from = history["cli_history_from"] if history["cli_history_truncated"] else None

    week_days = defaultdict(set)
    for (week, _project), b in acc.items():
        week_days[week] |= b["active_days"]

    rows = []
    for (week, project), b in sorted(acc.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        total_tokens = b["input"] + b["output"] + b["cache_read"] + b["cache_write"]
        in_window = _days_in_window(week, window_from, window_to) if week and window_from else None
        rows.append(_render(SUMMARY_COLUMNS, {
            "person_email": email, "person_name": name,
            "week_start": week, "project": project,
            "sessions_started": b["sessions"],
            "sessions_active": len(b["active_sids"]),
            "requests": b["requests"],
            # Six decimals matches the payload's own precision, so a leader
            # summing many rows gets the same total as the dashboard rather than
            # accumulated rounding drift.
            "est_cost_usd": round(b["cost"], 6),
            "est_cost_subagent_usd": round(b["sub_cost"], 6),
            "subagent_cost_share": round(b["sub_cost"] / b["cost"], 4) if b["cost"] else None,
            "input_tokens": b["input"], "output_tokens": b["output"],
            "cache_read_tokens": b["cache_read"], "cache_write_tokens": b["cache_write"],
            "total_tokens": total_tokens, "thinking_tokens": b["thinking"],
            "web_search_requests": b["web_search"], "web_fetch_requests": b["web_fetch"],
            "tool_calls": b["tool_calls"], "tool_errors": b["tool_errors"],
            "main_active_hours": round(b["active_ms"] / 3_600_000, 2),
            "prs_opened": b["prs"],
            "lines_added": b["lines_added"], "lines_removed": b["lines_removed"],
            "cli_reported_cost_usd": round(b["cli_cost"], 6) if b["cli_cost"] is not None else None,
            "models_used": _join(b["models"]), "skills_used": _join(b["skills"]),
            "sessions_with_line_data": b["with_lines"],
            "sessions_with_pr_data": b["with_prs"],
            "sessions_with_cli_cost": b["with_cli"],
            "cost_with_line_data_usd": round(b["cost_with_lines"], 6),
            "cost_with_pr_data_usd": round(b["cost_with_prs"], 6),
            "cache_saved_usd": round(b["cache_saved"], 6),
            "est_cost_main_usd": round(b["cost"] - b["sub_cost"], 6),
            "cost_basis": COST_BASIS,
            "project_repo": ctx["_repos"].get(project) if project else None,
            "sessions_active_cli": sum(1 for sid in b["active_sids"] if is_cli.get(sid)),
            "active_days": len(b["active_days"]),
            "week_days_in_window": in_window,
            "partial_week": None if in_window is None else ("yes" if in_window < 7 else "no"),
            # "no" = Claude Code has already deleted CLI transcripts from this
            # week, so CLI work is missing here and only Desktop work remains.
            "cli_history_complete": "no" if cli_from and week and week < cli_from else "yes",
            **_friction_values(friction.get((week, project)), limit_hits.get((week, project)),
                               "turn_hours", 3_600_000),
            **_health_values(health.get((week, project)), b),
            "output_tokens_imputed": b["imputed"],
            "est_cost_unlogged_usd": round(b["unlogged"], 6) if b["unlogged"] is not None else None,
            "week_active_days": len(week_days[week]),
        }, ctx))
    return rows


def session_rows(payload, person, since=None, until=None, include_titles=True, ctx=None):
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    sessions = payload["sessions"]

    per_session = defaultdict(lambda: {
        "requests": 0, "input": 0, "output": 0, "cache_read": 0,
        "cache_write": 0, "thinking": 0, "cache_saved": 0.0, "imputed": 0, "skills": set(),
    })
    for r in payload["usage"]:
        a = per_session[r[1]]
        a["requests"] += r[12]
        a["input"] += r[5]
        a["output"] += r[6]
        a["cache_read"] += r[7]
        a["cache_write"] += r[8] + r[9]
        a["thinking"] += r[10]
        a["cache_saved"] += r[15] if len(r) > 15 else 0
        a["imputed"] += r[16] if len(r) > 16 else 0
        if r[3] and r[3] != "(none)":
            a["skills"].add(r[3])

    def dominant(counter):
        if not counter:
            return None
        return max(counter.items(), key=lambda kv: kv[1])[0]

    # The session sheet keeps sessions whole, so its friction is too.
    friction = _friction_by(payload, lambda day, sid: sid)
    limit_hits = _limits_by(payload, lambda day, sid: sid, ctx["_tz"])

    # One row per transcript that owns work. A fork/resume copy normally owns
    # nothing -- its requests belong to the original -- so it is skipped and no
    # session is listed twice. A few do own unique subagent work, though, and
    # dropping those made this sheet total less than the summary sheet with
    # nothing to explain the gap. They are kept, and flagged.
    rows = []
    export_id = _export_ids(payload)
    for s in sorted(sessions, key=lambda s: s.get("start") or ""):
        owned = per_session.get(s["id"], {}).get("requests", 0)
        if s.get("derived") and not owned:
            continue
        day = context_mod._local_day(s.get("start"), ctx["_tz"])
        if not _in_range(day, since, until):
            continue
        a = per_session.get(s["id"], {})
        main = s.get("est_cost_main") or 0.0
        sub = s.get("est_cost_sub") or 0.0
        input_t = a.get("input", 0)
        output_t = a.get("output", 0)
        read_t = a.get("cache_read", 0)
        write_t = a.get("cache_write", 0)
        prs = _opened_prs(s)
        rows.append(_render(SESSION_COLUMNS, {
            "person_email": email, "person_name": name,
            "session_id": export_id[s["id"]],
            "title": (s.get("title") if include_titles else "") or "",
            "project": s.get("project"),
            "started_at": _local_time(s.get("start"), ctx["_tz"]),
            "ended_at": _local_time(s.get("end"), ctx["_tz"]),
            "main_active_minutes": round((s.get("active_ms") or 0) / 60_000, 1),
            "models": _join(s.get("models") or []), "skills": _join(a.get("skills", set())),
            "requests": a.get("requests", 0),
            "est_cost_usd": round(main + sub, 6),
            "est_cost_main_usd": round(main, 6),
            "est_cost_subagent_usd": round(sub, 6),
            "input_tokens": input_t, "output_tokens": output_t,
            "cache_read_tokens": read_t, "cache_write_tokens": write_t,
            "total_tokens": input_t + output_t + read_t + write_t,
            "thinking_tokens": a.get("thinking", 0),
            "subagents": s.get("n_subagents") or 0,
            "tool_calls": s.get("tool_calls") or 0, "tool_errors": s.get("tool_errors") or 0,
            "lines_added": s.get("lines_added"), "lines_removed": s.get("lines_removed"),
            "prs_opened": len(prs), "pr_urls": _join(p.get("url") for p in prs),
            "cli_reported_cost_usd": round(s["cli_cost"], 6) if s.get("cli_cost") is not None else None,
            "entrypoint": dominant(s.get("entrypoints")),
            "cli_version": dominant(s.get("versions")),
            "git_branch": dominant(s.get("branches")),
            "permission_mode": dominant(s.get("permission_modes")),
            "cache_saved_usd": round(a.get("cache_saved", 0.0), 6),
            "is_fork_copy": "yes" if s.get("derived") else "no",
            "cost_basis": COST_BASIS,
            "project_repo": ctx["_repos"].get(s.get("project")),
            **_friction_values(friction.get(s["id"]), limit_hits.get(s["id"]),
                               "turn_minutes", 60_000),
            "pr_repos": _join({_pr_repo(p) for p in prs}),
            **_session_health(s),
            "output_tokens_imputed": a.get("imputed", 0),
            "est_cost_unlogged_usd": (round(s["est_cost_unlogged"], 6)
                                      if s.get("est_cost_unlogged") is not None else None),
        }, ctx))
    return rows


def model_family(model):
    """"opus", "sonnet", ... -- the level people actually choose between."""
    name = (model or "").lower()
    for family in _MODEL_FAMILIES:
        if family in name:
            return family
    return "other"


def breakdown_rows(payload, person, since=None, until=None, ctx=None):
    """Tokens, requests and cost by week x project x model x thread.

    Dated by request, exactly like the summary sheet, so for any week and
    project the rows here sum to that summary row's tokens, requests and cost.
    Weeks with no usage have no rows: this is a fact table, and the summary
    sheet already carries the zero weeks.
    """
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    project_of = context_mod.project_by_key(payload)

    acc = defaultdict(lambda: {
        "requests": 0, "input": 0, "output": 0, "cache_read": 0,
        "cw5m": 0, "cw1h": 0, "thinking": 0, "cost": 0.0, "cache_saved": 0.0, "imputed": 0,
    })
    for r in payload["usage"]:
        sid = r[1]
        if sid not in project_of:
            continue
        day = _day(r[0])
        if not _in_range(day, since, until):
            continue
        subagent_type = None if r[4] == -1 else str(r[4])
        b = acc[(_week_start(day), project_of[sid], r[2], subagent_type)]
        b["requests"] += r[12]
        b["input"] += r[5]
        b["output"] += r[6]
        b["cache_read"] += r[7]
        b["cw5m"] += r[8]
        b["cw1h"] += r[9]
        b["thinking"] += r[10]
        b["cost"] += r[13]
        b["cache_saved"] += r[15] if len(r) > 15 else 0
        b["imputed"] += r[16] if len(r) > 16 else 0

    rows = []
    for (week, project, model, subagent_type), b in sorted(
            acc.items(), key=lambda kv: tuple(x or "" for x in kv[0])):
        write = b["cw5m"] + b["cw1h"]
        rows.append(_render(BREAKDOWN_COLUMNS, {
            "person_email": email, "person_name": name,
            "week_start": week, "project": project,
            "project_repo": ctx["_repos"].get(project),
            "model": model, "model_family": model_family(model),
            "thread": "main" if subagent_type is None else "subagent",
            "subagent_type": subagent_type,
            "requests": b["requests"],
            "input_tokens": b["input"], "output_tokens": b["output"],
            "cache_read_tokens": b["cache_read"],
            "cache_write_5m_tokens": b["cw5m"], "cache_write_1h_tokens": b["cw1h"],
            "cache_write_tokens": write,
            "total_tokens": b["input"] + b["output"] + b["cache_read"] + write,
            "thinking_tokens": b["thinking"],
            "est_cost_usd": round(b["cost"], 6),
            "cache_saved_usd": round(b["cache_saved"], 6),
            "cost_basis": COST_BASIS,
            "output_tokens_imputed": b["imputed"],
        }, ctx))
    return rows


def _export_ids(payload):
    """{session key: the session id the sheets show}. The payload joins its
    tables on a key that is only unique, not always the id: a session id found
    in two project folders is keyed "<id>@<folder>" for its second occurrence.
    The sheets show the id a person can look up -- the real one for the first
    occurrence, so a log set with no repeat is exported exactly as before --
    and "<id>#2", "<id>#3", ... for the later ones, in the order the scan keyed
    them. The merge keys sessions on this column, so it must be unique; and the
    folder is never part of it, as it is the project's path."""
    sessions = payload["sessions"]
    real = {s["id"]: s.get("session_id") or s["id"] for s in sessions}
    owned = defaultdict(int)
    for r in payload["usage"]:
        owned[r[1]] += r[12]
    # Numbered over the sessions that get a row (see session_rows), not over a
    # fork copy it skips, or the real session would be "<id>#2" for no reason.
    listed = [s for s in sessions if not (s.get("derived") and not owned[s["id"]])]
    taken = set(real.values())  # an id that really is named "dup#2" keeps it
    seen, out = set(), {}
    for s in listed:
        key, rid = s["id"], real[s["id"]]
        if rid not in seen:
            seen.add(rid)
            out[key] = rid
            continue
        n = 1
        while True:
            n += 1
            candidate = f"{rid}#{n}"
            if candidate not in taken:
                break
        taken.add(candidate)
        out[key] = candidate
    # A skipped copy has no row of its own; limit and PR rows that point at it
    # show its real id.
    for s in sessions:
        out.setdefault(s["id"], real[s["id"]])
    # A subagent whose parent transcript is missing has a key but no session
    # row; limit rows can still point at it. It shows its real id.
    for agent in payload.get("agents") or []:
        out.setdefault(agent.get("session_id"), agent.get("real_session_id") or agent.get("session_id"))
    return out


def limit_rows(payload, person, since=None, until=None, ctx=None):
    """One row per usage-limit episode, dated by when the limit was hit."""
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    project_of = context_mod.project_by_key(payload)
    real_id = _export_ids(payload)
    rows = []
    for ep in sorted(payload.get("limits") or [], key=lambda e: e.get("hit_at") or ""):
        if not _in_range(context_mod._local_day(ep.get("hit_at"), ctx["_tz"]), since, until):
            continue
        project = project_of.get(ep.get("session_id"))
        rows.append(_render(LIMIT_COLUMNS, {
            "person_email": email, "person_name": name,
            "limit_type": ep.get("kind"),
            "hit_at": ep.get("hit_at"), "last_notice_at": ep.get("last_notice_at"),
            "resets_at": ep.get("resets_at"), "notices": ep.get("notices"),
            "locked_out": "yes" if ep.get("locked_out") else "no",
            "blocked_hours": ep.get("blocked_hours"),
            "resumed_at": ep.get("resumed_at"),
            "usage_before_hit_usd": ep.get("usage_before_hit_usd"),
            "usage_after_hit_usd": ep.get("usage_after_hit_usd"),
            "requests_after_hit": ep.get("requests_after_hit"),
            "project": project, "project_repo": ctx["_repos"].get(project),
            "session_id": real_id.get(ep.get("session_id"), ep.get("session_id")),
            "cost_basis": COST_BASIS,
        }, ctx))
    return rows


def _pr_repo(pr):
    url = pr.get("url") or ""
    return context_mod.normalize_remote(url.split("/pull/")[0]) if "/pull/" in url else None


def pr_rows(payload, person, since=None, until=None, ctx=None):
    """One row per PR, dated by when it was opened.

    session_cost_usd is the whole cost of the session that opened the PR; a
    session that opened several carries the same figure on each, with
    session_prs saying how many share it. Divide by session_prs, don't sum.
    """
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    sessions = {s["id"]: s for s in payload["sessions"]}
    per_session = Counter(pr.get("session_id") for pr in payload.get("prs") or [])
    real_id = _export_ids(payload)
    rows = []
    for pr in sorted(payload.get("prs") or [], key=lambda p: (p.get("date") or "", p.get("url") or "")):
        if not _in_range(_day(pr.get("date")), since, until):
            continue
        s = sessions.get(pr.get("session_id")) or {}
        project = s.get("project")
        project_repo = ctx["_repos"].get(project) if project else None
        repo = _pr_repo(pr)
        same = None if not (repo and project_repo) else ("yes" if repo == project_repo else "no")
        cost = (s.get("est_cost_main") or 0.0) + (s.get("est_cost_sub") or 0.0)
        rows.append(_render(PR_COLUMNS, {
            "person_email": email, "person_name": name,
            "pr_url": pr.get("url"), "pr_repo": repo, "pr_number": pr.get("number"),
            "opened_on": pr.get("date"),
            "session_id": real_id.get(pr.get("session_id"), pr.get("session_id")),
            "project": project, "project_repo": project_repo, "in_project_repo": same,
            "session_cost_usd": round(cost, 6) if s else None,
            "session_prs": per_session.get(pr.get("session_id")),
            "cost_basis": COST_BASIS,
        }, ctx))
    return rows


def tool_integration(name):
    """The MCP server a tool belongs to ("mcp__tracker__getIssue" ->
    "tracker"), or "built-in" for Claude Code's own tools."""
    if name and name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3 and parts[1]:
            return parts[1]
    return "built-in"


def tool_rows(payload, person, since=None, until=None, ctx=None):
    """Calls and errors by week x project x tool, dated by the call."""
    email, name = person
    ctx = _context_for(payload, person, since, until, ctx)
    project_of = context_mod.project_by_key(payload)
    acc = defaultdict(lambda: [0, 0])
    for r in payload["tools"]:
        sid = r[1]
        day = _day(r[0])
        if sid not in project_of or not _in_range(day, since, until):
            continue
        a = acc[(_week_start(day), project_of[sid], r[2])]
        a[0] += r[3]
        a[1] += r[4]
    rows = []
    for (week, project, tool), (calls, errors) in sorted(acc.items()):
        rows.append(_render(TOOL_COLUMNS, {
            "person_email": email, "person_name": name,
            "week_start": week, "project": project,
            "project_repo": ctx["_repos"].get(project) if project else None,
            "tool": tool, "integration": tool_integration(tool),
            "calls": calls, "errors": errors, "cost_basis": COST_BASIS,
        }, ctx))
    return rows


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

KINDS = {
    "summary": (SUMMARY_COLUMNS, "summary"),
    "sessions": (SESSION_COLUMNS, "sessions"),
    "breakdown": (BREAKDOWN_COLUMNS, "breakdown"),
    "limits": (LIMIT_COLUMNS, "limits"),
    "prs": (PR_COLUMNS, "prs"),
    "tools": (TOOL_COLUMNS, "tools"),
}


def _stamp(stamp=None, zone=None, now=None):
    """The date in a file name: `stamp`, else today in the export zone, so it
    agrees with `exported_at` inside the file."""
    if stamp:
        return stamp
    tz = (zone if zone is not None else context_mod.export_zone())[0]
    moment = now.astimezone(tz) if now else context_mod.now_in(tz)
    return moment.date().isoformat().replace("-", "")


def filename_for(kind: str, person, stamp=None, zone=None, now=None) -> str:
    stamp = _stamp(stamp, zone, now)
    return f"claude-usage-{KINDS[kind][1]}-{_slug(person[0])}-{stamp}.csv"


def zip_filename_for(person, stamp=None, zone=None, now=None) -> str:
    stamp = _stamp(stamp, zone, now)
    return f"claude-usage-export-{_slug(person[0])}-{stamp}.zip"


def zip_bytes(payload, person, since=None, until=None, include_titles=True,
              stamp=None, zone=None, now=None) -> bytes:
    """Every CSV in one archive, so the dashboard can deliver them in a single
    click. A browser refuses multiple automatic downloads from one user gesture,
    so several files have to arrive as one download or as separate clicks."""
    ctx = build_context(payload, person, since, until, now=now, zone=zone)
    stamp = _stamp(stamp, zone, now)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for kind in KINDS:
            archive.writestr(
                filename_for(kind, person, stamp),
                csv_text(payload, kind, person, since, until, include_titles, ctx=ctx),
            )
    return buf.getvalue()


def csv_text(payload, kind: str, person, since=None, until=None,
             include_titles=True, ctx=None, zone=None, now=None) -> str:
    """One CSV as a string, for serving as a download. Same columns and same
    rows as the file the CLI writes -- there is one implementation of each."""
    columns = KINDS[kind][0]
    if ctx is None and (zone is not None or now is not None):
        ctx = build_context(payload, person, since, until, now=now, zone=zone)
    rows = _rows_for(kind, payload, person, since, until, include_titles, ctx)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(_safe_rows(rows))
    return buf.getvalue()


def _rows_for(kind, payload, person, since, until, include_titles, ctx):
    if kind == "summary":
        return summary_rows(payload, person, since, until, ctx=ctx)
    if kind == "sessions":
        return session_rows(payload, person, since, until, include_titles, ctx=ctx)
    if kind == "breakdown":
        return breakdown_rows(payload, person, since, until, ctx=ctx)
    if kind == "prs":
        return pr_rows(payload, person, since, until, ctx=ctx)
    if kind == "tools":
        return tool_rows(payload, person, since, until, ctx=ctx)
    return limit_rows(payload, person, since, until, ctx=ctx)


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(_safe_rows(rows))
    return path


def _slug(value):
    return _UNSAFE_FILENAME.sub("-", str(value)).strip("-") or "unknown"


def export(payload, out_dir=".", person=None, since=None, until=None,
           include_titles=True, stamp=None, zone=None, now=None):
    """Write every CSV into `out_dir`. Returns [(path, row_count), ...].

    `zone` is the (tzinfo, label) the payload was scanned in -- see
    build_context; context.export_zone() when not given."""
    person = person or resolve_identity()
    os.makedirs(out_dir, exist_ok=True)
    if zone is None:
        zone = context_mod.export_zone()
    stamp = _stamp(stamp, zone, now)

    ctx = build_context(payload, person, since, until, now=now, zone=zone)
    written = []
    for kind, (columns, _) in KINDS.items():
        rows = _rows_for(kind, payload, person, since, until, include_titles, ctx)
        path = _write_csv(os.path.join(out_dir, filename_for(kind, person, stamp)),
                          columns, rows)
        written.append((path, len(rows)))
    return written
