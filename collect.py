#!/usr/bin/env python3
"""CLI entry point: scan ~/.claude/projects and write data.json to the data
folder (~/.usage-lens; see claude_usage/paths.py).

With --static, also bakes the payload, the stylesheet and the charting library
into a single self-contained dashboard.html that needs no server and no sibling
files -- pass --redact for a version safe to hand to someone else.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from datetime import date
from pathlib import Path

from claude_usage import context as context_mod
from claude_usage import paths
from claude_usage import export as export_mod
from claude_usage import price_check
from claude_usage.sanitize import finite_json
from claude_usage.scan import DEFAULT_ROOT, scan

HERE = Path(__file__).parent
# Both written to the data folder; tests point them elsewhere.
OUT_PATH = None
STATIC_DIR = HERE / "static"
STATIC_TEMPLATE = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
APP_CSS = STATIC_DIR / "app.css"
CHART_JS = STATIC_DIR / "chart.umd.min.js"
DASHBOARD_PATH = None

# Every <script src="static/..."> in index.html, in load order. Scripts other
# than app.js are inlined verbatim; app.js additionally gets the data baked in.
VENDOR_SCRIPTS = ("chart.umd.min.js", "filters.js", "habits.js")

_PR_URL_RE = re.compile(r"^https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)")


def _redact(payload: dict) -> dict:
    """Replace the parts of the payload that carry your own content or identify
    your machine: session titles (swapped for the session id), subagent
    descriptions (blanked), project names (a project name is an absolute path;
    aliased to "project-N"), git branch names, PR repositories and URLs, and the
    "<id>@<folder>" keys that put a project folder inside a session id.

    Everything else is kept: dates, token counts, costs, models, and the skill,
    MCP server, tool and subagent-type names. The data is not anonymous.

    Titles alone are not enough -- a project name is an absolute path, and
    branch names and PR links routinely carry client or feature names.
    """
    payload = _without_folder_in_keys(copy.deepcopy(payload))

    projects = sorted({r.get("project") for r in payload.get("sessions", []) + payload.get("agents", [])
                       if r.get("project")})
    alias = {name: f"project-{i + 1}" for i, name in enumerate(projects)}
    branches = {}
    repos = {}

    def branch_alias(name):
        return branches.setdefault(name, f"branch-{len(branches) + 1}")

    def repo_alias(name):
        return repos.setdefault(name, f"org/repo-{len(repos) + 1}")

    for s in payload.get("sessions", []):
        s["title"] = s.get("session_id") or s["id"]
        if s.get("project"):
            s["project"] = alias.get(s["project"], "(project)")
        if s.get("branches"):
            s["branches"] = {branch_alias(b): n for b, n in s["branches"].items()}
        for pr in s.get("prs", []):
            _redact_pr(pr, repo_alias)
    for a in payload.get("agents", []):
        a["description"] = ""
        if a.get("project"):
            a["project"] = alias.get(a["project"], "(project)")
    for pr in payload.get("prs", []):
        _redact_pr(pr, repo_alias)

    dims = payload.get("dims") or {}
    if "projects" in dims:
        dims["projects"] = sorted({alias.get(p, "(project)") for p in dims["projects"]})
    return payload


def _without_folder_in_keys(payload: dict) -> dict:
    """A session id found in two project folders is keyed "<id>@<folder>" (so is
    a subagent whose parent transcript is missing), and the folder name is the
    project's path. So is a real id that happens to hold an "@" -- a file named
    after such a key. Swap each such string for "<id>@<n>" wherever it appears
    -- the key is the join column of every table -- so the rows stay joined.
    A new name is never one already in the payload, so no two sessions merge."""
    # Keys come from session rows and from subagents whose parent has no row.
    pairs = [(s.get("id"), s.get("session_id")) for s in payload.get("sessions", [])]
    pairs += [(a.get("session_id"), a.get("real_session_id")) for a in payload.get("agents", [])]
    in_use = {v for pair in pairs for v in pair if isinstance(v, str) and v}
    generated = {}  # string to swap -> the id it stands for
    for key, real in pairs:
        if not (key and real):
            continue
        if key != real:
            generated.setdefault(key, real)
        if "@" in real:
            generated.setdefault(real, real)
        if "@" in key:
            generated.setdefault(key, real)
    if not generated:
        return payload
    taken = set(in_use)  # a new name is no string the payload already has
    renamed, n = {}, 0
    for old, real in generated.items():
        stem = real.split("@", 1)[0]
        while True:
            n += 1
            new = f"{stem}@{n}"
            if new not in taken:
                break
        taken.add(new)
        renamed[old] = new
    # One pass, so a new name is never itself swapped again.
    quoted = {json.dumps(old): json.dumps(new) for old, new in renamed.items()}
    text = re.sub("|".join(re.escape(q) for q in quoted), lambda m: quoted[m.group(0)],
                  json.dumps(payload))
    return json.loads(text)


def _redact_pr(pr: dict, repo_alias):
    if pr.get("repo"):
        pr["repo"] = repo_alias(pr["repo"])
    url = pr.get("url") or ""
    m = _PR_URL_RE.match(url)
    # Keep the shape of the link (so the table still renders) but not the target.
    pr["url"] = f"https://example.invalid/{pr.get('repo') or 'org/repo'}/pull/{m.group(2) if m else '0'}"


def _stale_prices_message(prices: dict) -> str:
    return (f"the rate table was last checked against claude.com/pricing on {prices['checked']} "
            f"({prices['age_days']} days ago). Re-check it, and if any price changed, add a dated "
            f"row to claude_usage/pricing.py (never edit a past row) -- see docs/cost.md. "
            f"Then set PRICES_CHECKED to the day you checked.")


def _out_path() -> Path:
    return Path(OUT_PATH) if OUT_PATH else paths.data_dir() / "data.json"


def _dashboard_path() -> Path:
    return Path(DASHBOARD_PATH) if DASHBOARD_PATH else paths.data_dir() / "dashboard.html"


def _build_static(payload: dict) -> bool:
    """Inline the data, the CSS and Chart.js into one standalone HTML file.

    The previous build only rewrote the app.js <script src>, which left the page
    pointing at `static/app.css` and `static/chart.umd.min.js` -- so the file was
    only openable from inside the repo, and unusable once shared on its own.
    """
    required = [STATIC_TEMPLATE, APP_JS, APP_CSS] + [STATIC_DIR / n for n in VENDOR_SCRIPTS]
    for path in required:
        if not path.exists():
            print(f"{path} not found; skipping --static build")
            return False
    out = _dashboard_path()

    data_json = _script_safe_json(payload)
    app_js = APP_JS.read_text(encoding="utf-8")

    # app.js loads its data in exactly one place (boot and Refresh both go
    # through it), so the payload is baked in once rather than once per caller.
    marker = "apiFetch('/api/data').then(jsonOrThrow)"
    if app_js.count(marker) != 1:
        print(f"ERROR: expected exactly one {marker!r} in {APP_JS}; cannot bake the data in")
        return False
    app_js = app_js.replace(marker, f"Promise.resolve({data_json})")
    # No server behind a standalone file, so the export routes don't exist.
    served_marker = "let SERVED_BY_DASHBOARD = true;"
    if served_marker not in app_js:
        print(f"WARNING: {served_marker!r} not found in {APP_JS}; the standalone "
              f"build may offer an Export button that cannot work")
    app_js = app_js.replace(served_marker, "let SERVED_BY_DASHBOARD = false;")

    html = STATIC_TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="static/app.css">',
        "<style>\n" + APP_CSS.read_text(encoding="utf-8") + "\n</style>",
    )
    for name in VENDOR_SCRIPTS:
        html = html.replace(
            f'<script src="static/{name}"></script>',
            _inline_script((STATIC_DIR / name).read_text(encoding="utf-8")),
        )
    html = html.replace('<script src="static/app.js"></script>', _inline_script(app_js))

    for leftover in ('href="static/', 'src="static/'):
        if leftover in html:
            print(f"WARNING: {out.name} still references {leftover}... — not fully standalone")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    size_mb = out.stat().st_size / 1e6
    print(f"Built standalone dashboard: {out} ({size_mb:.1f} MB, no other files needed)")
    return True


# Characters that must never appear raw in JSON inlined into a <script>: "<"
# and ">" can open "</script>" (in any case, with any trailing whitespace) or
# the "<!--<script>" sequence that stops the real end tag from closing the
# block, "&" goes too as the conventional belt-and-braces, and U+2028/U+2029
# were line terminators inside JS strings before ES2019. Every one of them can
# only occur inside a JSON string, where the \uXXXX escape reads back as the
# same character.
_JSON_SCRIPT_ESCAPES = {
    "<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
    "\u2028": "\\u2028", "\u2029": "\\u2029",
}
_JSON_SCRIPT_UNSAFE = re.compile("[<>&\u2028\u2029]")


def _script_safe_json(payload) -> str:
    """JSON text that is safe to place inside an HTML <script> element.

    Session titles, branch names and paths come from your own transcripts, and
    this file is meant to be shared: a title containing "</SCRIPT>" must not be
    able to end the script block and inject markup.
    """
    text = json.dumps(finite_json(payload), separators=(",", ":"), allow_nan=False)
    return _JSON_SCRIPT_UNSAFE.sub(lambda m: _JSON_SCRIPT_ESCAPES[m.group(0)], text)


# The sequences that end or derail a <script> block in the HTML tokenizer:
# "</script" in any case (the end tag), and "<!--" (which, followed by
# "<script", makes the parser skip the real end tag).
_SCRIPT_BREAKERS = re.compile(r"<(?=/script|!--)", re.IGNORECASE)


def _inline_script(source: str) -> str:
    # In the JS sources these can only sit inside a string, a regex or a
    # comment, where "\x3C" means the same "<" -- so the code is unchanged but
    # the HTML parser never sees the sequence.
    return "<script>\n" + _SCRIPT_BREAKERS.sub(r"\\x3C", source) + "\n</script>"


def _merge(paths, out_dir, seat_prices=None):
    from claude_usage import team
    merged, written = team.write_merged(paths, out_dir, seat_prices)
    print(f"Merged {len(merged['sources'])} export(s) from {len(merged['people'])} people into {out_dir}:")
    for path in written:
        print(f"  {path}")
    for w in merged["warnings"]:
        print(f"{'WARNING' if w['level'] == 'warn' else 'Note'}: {w['message']}")
    if not merged["sources"]:
        print("No exports found -- expected claude-usage-export-*.zip or claude-usage-*.csv files.")
        return 1
    print("\nOpen the team dashboard on the same folder with:  python3 serve.py --team "
          + " ".join(paths))
    return 0


def _parse_day(value: str):
    """The date for a strict YYYY-MM-DD string, else None. The shape alone
    isn't enough: "2026-09-31" matches it and only fails later, mid-export."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"Usage Lens {context_mod.tool_version()}")
    parser.add_argument("--static", action="store_true", help="also build a standalone dashboard.html")
    parser.add_argument("--redact", "--redact-titles", dest="redact", action="store_true",
                        help="strip titles, descriptions, project paths, branches and PR links "
                             "from the --static output")
    parser.add_argument("--force", action="store_true", help="ignore the parse cache and re-read every file")
    parser.add_argument("--export", nargs="?", const=".", metavar="DIR",
                        help="write the business-review CSVs (summary + per-session) into DIR")
    parser.add_argument("--tz", metavar="ZONE",
                        help="with --export: the time zone the export's days and weeks are in, "
                             "as an IANA name (default: the CLAUDE_USAGE_EXPORT_TZ variable, "
                             "else UTC). Set the same zone for everyone whose exports are merged")
    parser.add_argument("--as", dest="person", metavar="EMAIL",
                        help="identity to stamp on exported rows (default: git user.email)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="earliest date to export")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="latest date to export")
    parser.add_argument("--merge", nargs="+", metavar="PATH",
                        help="team mode: combine people's export zips/CSVs (files or folders) "
                             "into de-duplicated team CSVs, instead of reading your own logs")
    parser.add_argument("--out", metavar="DIR", default="team-merged",
                        help="where --merge writes its files (default: ./team-merged)")
    parser.add_argument("--seat-prices", metavar="FILE",
                        help="seat price per tier, for value per seat (default: a "
                             "seat-prices.json in the --merge folder)")
    parser.add_argument("--no-titles", action="store_true",
                        help="leave session titles out of the export (paths, branches "
                             "and PR links are still included)")
    parser.add_argument("--check-prices", action="store_true",
                        help="check the rate table against Anthropic's published prices now "
                             "(ignoring today's cached check); exit 1 if they differ or the "
                             "check fails")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="where the tool keeps its cache, machine id, data.json and dashboard.html "
                             f"(default: {paths.DEFAULT_DATA_DIR}, or {paths.DATA_DIR_ENV})")
    parser.add_argument("--offline", action="store_true",
                        help="make no network request at all (skips the daily price check)")
    args = parser.parse_args()
    if args.data_dir:
        paths.set_data_dir(args.data_dir)
    if args.offline:
        os.environ[price_check.OFFLINE_ENV] = "1"
    for flag in ("since", "until"):
        value = getattr(args, flag)
        if value and _parse_day(value) is None:
            parser.error(f"--{flag} must be a real date as YYYY-MM-DD, got {value!r}")
    if args.since and args.until and args.since > args.until:
        parser.error(f"--since {args.since} is after --until {args.until}")

    if args.tz and args.export is None:
        parser.error("--tz only applies to --export (the dashboard always uses this machine's time zone)")
    zone = None
    if args.export is not None and not args.merge:
        try:
            zone = context_mod.export_zone(args.tz)
        except ValueError as exc:
            parser.error(str(exc))

    if args.merge:
        return _merge(args.merge, args.out, args.seat_prices)

    # The dashboard payload (data.json, --static) is in this machine's zone.
    payload = scan(DEFAULT_ROOT, force=args.force)
    # Daily, cached, and only ever reported -- see claude_usage/price_check.py.
    checked = price_check.check(used_models=payload["dims"]["models"], force=args.check_prices)
    payload["price_check"] = checked
    out_path = _out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(finite_json(payload), separators=(",", ":"), allow_nan=False),
                        encoding="utf-8")
    summary = payload["summary"]
    # The sessions an export lists: every transcript that is not a fork copy,
    # including ones that never got a reply (the dashboard's count leaves those out).
    listed = summary["total_transcripts"] - summary["derived_sessions"]
    print(f"Wrote {listed} sessions ({summary['total_sessions']} with requests, "
          f"+{summary['derived_sessions']} fork/resume copies), "
          f"{summary['total_subagents']} subagents, {len(payload['usage'])} usage rows to {out_path} "
          f"({payload['files_parsed']} parsed, {payload['files_cached']} cached, {payload['scan_ms']}ms)")
    if payload["unknown_models"]:
        print(f"WARNING: no pricing for models: {', '.join(payload['unknown_models'])} "
              f"-- their requests are counted at $0, so totals are understated")
    # An export's CLI history is read in the export zone, so its note comes from
    # that payload; the dashboard's own (local) one decides otherwise.
    export_payload = None
    if args.export is not None:
        # A separate scan: the export's days and weeks are in the export zone,
        # not this machine's. The parse cache holds facts, so this re-reads nothing.
        export_payload = scan(DEFAULT_ROOT, tz=zone[0])
    history = (export_payload or payload).get("history") or {}
    if history.get("cli_history_truncated"):
        print(f"Note: CLI history starts {history['cli_history_from']}. Claude Code deletes CLI "
              f"transcripts after {history['retention_days_default']} days by default, so older CLI "
              f"work is missing (Desktop sessions are kept longer). To keep it from now on, add "
              f'"cleanupPeriodDays": 365 to ~/.claude/settings.json.')
    if summary.get("fast_mode_requests"):
        print(f"Note: {summary['fast_mode_requests']} request(s) ran in fast mode (priced at 2x standard)")
    prices = payload.get("pricing") or {}
    for line in price_check.describe(checked):
        print(line)
    # A check that just succeeded is a better guarantee than the date someone
    # last looked by hand, so the age warning only applies without one.
    if prices.get("stale") and checked["status"] not in ("ok", "differs"):
        print(f"WARNING: {_stale_prices_message(prices)}")
    if args.check_prices:
        if checked["status"] == "ok":
            print(f"Prices match {checked['url']} (checked {checked['checked_at']}).")
        elif checked["status"] == "offline":
            print("--check-prices with --offline: nothing checked.")
    # Decided here, returned at the end: --check-prices alongside --export or
    # --static must still do those.
    exit_code = 1 if args.check_prices and checked["status"] != "ok" else 0

    if args.export is not None:
        person = export_mod.resolve_identity(args.person)
        written = export_mod.export(
            export_payload, out_dir=args.export, person=person,
            since=args.since, until=args.until,
            include_titles=not args.no_titles, zone=zone,
        )
        print(f"Exported as {person[0]}, with days and weeks in {zone[1]}:")
        for path, count in written:
            print(f"  {path}  ({count} rows)")
        # Said at the moment the file exists, not only in docs/export.md: this is the
        # one output of this tool meant to be handed to someone else.
        print("\nThese files identify you by email and describe your work:"
              " session titles\n(written by the model from your own conversations),"
              " full project paths, branch\nnames and PR links."
              " They contain no prompts, no replies, and no code.")
        if args.no_titles:
            print("Session titles were omitted (--no-titles).")
        else:
            print("Re-run with --no-titles to leave session titles out.")

    if args.static:
        if not _build_static(_redact(payload) if args.redact else payload):
            exit_code = 1
    elif args.redact:
        print("Note: --redact only affects the --static build; data.json is never redacted")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
