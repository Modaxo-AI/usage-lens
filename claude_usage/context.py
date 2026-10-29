"""Facts about *where an export came from*, so files from many people can be
combined without silently misleading whoever combines them.

Nothing here reads a transcript. It reads:

* a whitelist of plan fields from Claude Code's own account cache
  (``~/.claude.json`` -> ``oauthAccount``) -- never tokens, ids or org names;
* ``git`` metadata (this tool's commit, and each project's ``origin`` remote,
  stripped of any credentials embedded in the URL);
* the payload that has already been built.
"""
from __future__ import annotations

import datetime
import functools
import hashlib
import json
import os
import re
import secrets
import subprocess
from collections import Counter
from pathlib import Path

from . import paths, pricing

# Bumped whenever an export column or file is added, removed or changes
# meaning. Files written before this column existed are schema 1; schema 2 added
# provenance and seat columns; schema 3 added the breakdown file; schema 4
# added the limits file and the friction and allowance columns; schema 5 added
# the PRs file and the sessions sheet's pr_repos; schema 6 added machine_id;
# schema 7 added the usage-health columns and the tools file; schema 8 added
# bucket_zone and moved every day and week from the machine's zone to the export
# zone (see export_zone); schema 9 added output tokens imputed for subagent
# responses logged with a placeholder count (which output_tokens and every
# est_cost_* column now include) and est_cost_unlogged_usd; schema 10 added
# very_long_context_cost_usd (main-conversation spend on requests over 500k
# tokens) to the summary and sessions sheets; schema 11 added week_active_days
# (the person's distinct active days in each week) to the summary sheet.
EXPORT_SCHEMA_VERSION = 11

# The team-wide zone exports bucket their days and weeks in: `--tz`, else this
# variable, else UTC. Never the machine's own zone, so the same logs exported
# from anywhere give the same buckets and everyone's weeks line up.
EXPORT_ZONE_ENV = "CLAUDE_USAGE_EXPORT_TZ"
_UTC_NAMES = ("UTC", "Z", "ETC/UTC")

# Claude Code's default `cleanupPeriodDays`: CLI transcripts untouched for this
# long are deleted. Desktop/Cowork transcripts have their own, longer setting.
DEFAULT_CLI_RETENTION_DAYS = 30
# Deletion leaves the oldest surviving CLI transcript close to the retention
# age; with sparse use it can be a few days younger. History that starts more
# recently than this many days inside the window is a new user, not deletion.
CLI_RETENTION_SLACK_DAYS = 7

# A seat with no recorded request in this many days before the export is idle.
IDLE_AFTER_DAYS = 14

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where Claude Code caches the signed-in account. CLAUDE_CONFIG_DIR relocates it.
def _account_files():
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        yield Path(config_dir) / ".claude.json"
    yield Path.home() / ".claude.json"


ACCOUNT_FILES = None  # tests point this at a fixture; None means the real locations

# Only these leave the machine. Everything else in oauthAccount -- account and
# organisation UUIDs, organisation name, trial dates -- is deliberately ignored.
_ACCOUNT_FIELDS = {
    "claude_account_email": "emailAddress",
    "seat_tier": "seatTier",
    "rate_limit_tier": "userRateLimitTier",
    "extra_usage_enabled": "hasExtraUsageEnabled",
}


# --------------------------------------------------------------------------- #
# account and versions
# --------------------------------------------------------------------------- #

def account_info():
    """Plan fields for the Claude account signed in on this machine. Every key
    is present; a value is None when Claude Code has not cached it."""
    info = {col: None for col in _ACCOUNT_FIELDS}
    for path in (ACCOUNT_FILES if ACCOUNT_FILES is not None else _account_files()):
        try:
            with open(path, encoding="utf-8") as fh:
                account = (json.load(fh) or {}).get("oauthAccount") or {}
        except (OSError, ValueError):
            continue
        for col, key in _ACCOUNT_FIELDS.items():
            value = account.get(key)
            if isinstance(value, bool):
                value = "yes" if value else "no"
            info[col] = value if value not in ("", None) else None
        break
    return info


def _git(*args, cwd=None):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


@functools.lru_cache(maxsize=1)
def tool_version():
    """This tool's commit. Read from git rather than a constant, because a
    constant is the thing someone forgets to bump."""
    return _git("rev-parse", "--short", "HEAD", cwd=REPO_ROOT) or "unknown"


@functools.lru_cache(maxsize=1)
def pricing_version():
    """A fingerprint of the whole dated rate table. Each request is priced by the
    row in force on its date, so a file made before a new row was added priced
    the days since at the old rate -- this makes that visible without anyone
    maintaining a version number."""
    table = {"rates": {model: [[day, rates, long] for day, rates, long in zip(*sched)]
                       for model, sched in pricing._SCHEDULE.items()},
             "fast": pricing.FAST_MODE_MULTIPLIER,
             "fast_models": sorted(pricing.FAST_MODE_MODELS)}
    blob = json.dumps(table, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:8]


def export_zone(name=None):
    """(tzinfo, label) for the zone an export buckets its days and weeks in.

    From `name` (the CLI's --tz), else the CLAUDE_USAGE_EXPORT_TZ variable, else
    UTC. "UTC", "Z" and "Etc/UTC" (any case) are datetime.timezone.utc and need
    no zone data; anything else is an IANA name for zoneinfo, and is its own
    label. A name that can't be loaded is a ValueError saying what to do.

    Always a tzinfo, never a change to the process's TZ: that works on Windows,
    which has no time.tzset, and leaves the personal dashboard in local time.
    """
    source = "--tz"
    raw = (name or "").strip()
    if not raw:
        source = EXPORT_ZONE_ENV
        raw = os.environ.get(EXPORT_ZONE_ENV, "").strip()
    if not raw:
        return datetime.timezone.utc, "UTC"
    if raw.upper() in _UTC_NAMES:
        return datetime.timezone.utc, "UTC"
    hint = ("Use an IANA name such as Europe/London or America/New_York, or UTC. "
            "If the name is right, this machine has no time zone data "
            "(typical on Windows): use UTC, or run: pip install tzdata")
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        raise ValueError(f"cannot use time zone {raw!r} (from {source}): "
                         f"this Python has no zoneinfo. {hint}") from None
    name = _canonical_zone_name(raw)
    try:
        return ZoneInfo(name), name
    except (KeyError, ValueError, OSError):
        raise ValueError(f"unknown time zone {raw!r} (from {source}). {hint}") from None


@functools.lru_cache(maxsize=1)
def _known_zones():
    """{lower-case name: IANA name} for every zone the zone database has. Listing
    them walks the whole database (~25 ms) and export_zone runs per export and
    per /api/data request, so it is done once. Raises (and so is not cached)
    when there is no zone data, so a later call can still succeed."""
    from zoneinfo import available_timezones
    return {known.lower(): known for known in available_timezones()}


def _canonical_zone_name(raw):
    """`raw` spelled as the IANA database spells it ("asia/tokyo" -> "Asia/Tokyo"),
    or unchanged when it isn't a known name. The label travels in the file and
    people's labels are compared, so two spellings of one zone must read the same."""
    try:
        return _known_zones().get(raw.lower(), raw)
    except Exception:  # noqa: BLE001 -- no zone data: ZoneInfo(raw) says so
        return raw


def now_in(tz=None):
    """The current moment as an aware datetime in `tz` (None: this machine's zone)."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(tz)


def utc_offset(moment=None):
    """UTC offset of `moment` (default: now, machine-local), e.g. "-04:00". An
    export passes its own `now`, already in the export zone."""
    moment = moment or now_in()
    raw = moment.strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}"


# Where this machine's id is kept: the tool's data folder (see paths.py).
# Tests point this elsewhere; None means the real location.
MACHINE_ID_FILE = None
# Where versions before the data folder kept it, beside the code. Tests point
# this elsewhere too.
LEGACY_MACHINE_ID_FILE = None
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def machine_id():
    """A random id for this machine, made on first export and kept in the
    tool's data folder, so the team merge can tell a person's second machine
    from a re-export of the same one.

    Random rather than derived: a hash of the hostname or a MAC address would
    still identify the machine to anyone who guessed it; this id identifies
    nothing but itself. None when it can't be stored -- an id that changed on
    every export would make each re-export look like a new machine and double
    count, whereas a blank one merges exactly as before machine ids existed.
    """
    path = Path(MACHINE_ID_FILE) if MACHINE_ID_FILE else paths.data_dir() / "machine-id"
    for _ in range(2):
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = None
        if existing and _MACHINE_ID_RE.match(existing):
            return existing
        # A copy that kept its id beside the code keeps it here: a new id
        # would make this machine's next export look like a second machine.
        new = (_legacy_machine_id() if existing is None else None) or secrets.token_hex(6)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # "x" when there is no file, so a server and a CLI export starting
            # together can't each write a different id; a damaged file is
            # replaced.
            with open(path, "x" if existing is None else "w", encoding="utf-8") as fh:
                fh.write(new + "\n")
            return new
        except FileExistsError:
            continue  # someone else just made it: read theirs
        except OSError:
            return None
    return None


def _legacy_machine_id():
    path = (Path(LEGACY_MACHINE_ID_FILE) if LEGACY_MACHINE_ID_FILE
            else paths.LEGACY_CACHE_DIR / "machine-id")
    try:
        old = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return old if _MACHINE_ID_RE.match(old) else None


# --------------------------------------------------------------------------- #
# project -> repository
# --------------------------------------------------------------------------- #

_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")


def normalize_remote(url):
    """A git remote as a credential-free, case-folded ``host/path`` key.

    Two people who cloned the same repo into different folders, over https and
    ssh, get the same key. Any ``user:token@`` embedded in an https remote is
    dropped -- that is the one way this column could otherwise leak a secret.
    Local-path remotes return None: they identify a folder, not a repository.
    """
    if not url:
        return None
    url = url.strip()
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme == "file":
            return None
        authority, _, path = rest.partition("/")
        # rsplit: everything before the last "@" is user[:token]; after it,
        # host[:port].
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    else:
        m = _SCP_REMOTE.match(url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def _repo_from_disk(project):
    """The `origin` of the git repo a project folder belongs to, if the folder
    (or, for a deleted worktree, its nearest surviving parent) is still there."""
    is_path = project and (project.startswith(("~", "/"))
                           or re.match(r"^[A-Za-z]:[\\/]", project))
    if not is_path:
        return None
    home = Path.home()
    path = Path(os.path.expanduser(project))
    # A worktree under .claude/worktrees/ is often deleted after its PR merges;
    # its repo is the nearest folder that still exists. Never walk up as far as
    # home or the filesystem root -- those would name an unrelated repo.
    while not path.is_dir():
        parent = path.parent
        if parent in (path, home) or parent == Path(parent.anchor):
            return None
        path = parent
    top = _git("rev-parse", "--show-toplevel", cwd=path)
    if not top:
        return None
    # A dotfiles repo at ~ would otherwise claim every untracked folder in home.
    if Path(top) == home and path != home:
        return None
    return normalize_remote(_git("config", "--get", "remote.origin.url", cwd=path))


def project_by_key(payload):
    """{session key: project} for every key a usage/tool/friction row can carry:
    the session rows, plus the keys of subagents whose parent transcript is gone
    (no session row, so their agent row names the project). One definition, so
    every sheet places that spend in the same project."""
    out = {s["id"]: s.get("project") for s in payload.get("sessions") or []}
    for a in payload.get("agents") or []:
        if a.get("session_id") not in out and a.get("project"):
            out[a["session_id"]] = a["project"]
    return out


def project_repos(payload):
    """{project: repo key or None}.

    From the folder's own git remote when the folder still exists. Only when it
    is *gone* (typically a worktree removed after its PR merged) does this fall
    back to the repo the project's PRs were opened against. An existing folder
    that is not a git repo stays blank: a session there can open a PR in some
    other repo, and naming that repo would misattribute the whole project.
    """
    project_of = project_by_key(payload)
    from_prs = {}
    for pr in payload.get("prs") or []:
        project = project_of.get(pr.get("session_id"))
        repo = normalize_remote(pr.get("url", "").split("/pull/")[0]) if pr.get("url") else None
        if project and repo:
            from_prs.setdefault(project, Counter())[repo] += 1
    out = {}
    for project in sorted({p for p in project_of.values() if p}):
        repo = _cached_repo_from_disk(project)
        if repo is None and project in from_prs and not _folder_exists(project):
            repo = from_prs[project].most_common(1)[0][0]
        out[project] = repo
    return out


def _folder_exists(project):
    return Path(os.path.expanduser(project)).is_dir()


@functools.lru_cache(maxsize=512)
def _cached_repo_from_disk(project):
    return _repo_from_disk(project)


# --------------------------------------------------------------------------- #
# history coverage
# --------------------------------------------------------------------------- #

def _local_day(iso_ts, tz=None):
    """Calendar date of a transcript timestamp ("...Z" or with offset) in `tz`
    (None: this machine's zone)."""
    if not iso_ts:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_ts)[:10]
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return moment.date().isoformat()


def dominant_entrypoint(session):
    eps = session.get("entrypoints") or {}
    return max(eps.items(), key=lambda kv: kv[1])[0] if eps else None


def history_coverage(payload, today=None, tz=None):
    """Whether Claude Code has already deleted this person's older CLI history.

    Claude Code deletes CLI transcripts that have not been touched for
    `cleanupPeriodDays` (30 by default); Desktop transcripts are kept far
    longer. The effective setting can come from MDM or the claude.ai console,
    so it cannot be read reliably from disk -- but its effect can be seen in
    the data: if the oldest surviving CLI transcript was last active about 30
    days ago, everything older has been deleted. Much younger than that is
    someone who started using the CLI recently, whose history is complete.
    """
    today = today or now_in(tz).date()
    cli = [s for s in payload.get("sessions") or []
           if not s.get("derived") and dominant_entrypoint(s) == "cli"]
    ends = [d for d in (_local_day(s.get("end"), tz) for s in cli) if d]
    cli_from = min(ends) if ends else None
    truncated = False
    if cli_from:
        age = (today - datetime.date.fromisoformat(cli_from)).days
        truncated = (DEFAULT_CLI_RETENTION_DAYS - CLI_RETENTION_SLACK_DAYS
                     <= age <= DEFAULT_CLI_RETENTION_DAYS + 1)
    return {
        "cli_sessions": len(cli),
        "cli_history_from": cli_from,
        "cli_history_truncated": truncated,
        "retention_days_default": DEFAULT_CLI_RETENTION_DAYS,
    }


def workdays_between(first, last):
    """Monday-to-Friday days in [first, last], both ISO dates, inclusive."""
    if not first or not last or first > last:
        return 0
    a = datetime.date.fromisoformat(first)
    b = datetime.date.fromisoformat(last)
    full_weeks, extra = divmod((b - a).days + 1, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (a + datetime.timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count
