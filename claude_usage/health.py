"""Usage-health measures: habits that cost money or quality, priced and counted.

See docs/usage-health.md for the research behind each measure. Everything here
is computed from metadata the parser already extracted, de-duplicated across
fork/resume copies the same way as the rest of the payload: a record repeated
by a copy is counted once, for the original session.

The one input that is not plain metadata is the *category* of a shell command
(test / lint / build / commit / pr), decided in the parser by `classify_command`
and never stored as text. That is what lets the panel say whether a session
that edited code ever ran a check on it.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .pricing import rates_for

# Anthropic, "Manage costs effectively" (code.claude.com/docs/en/costs):
# about $13 per developer per active day, under $30 for 90% of users.
COSTS_DOC = "https://code.claude.com/docs/en/costs"
BEST_PRACTICES_DOC = "https://code.claude.com/docs/en/best-practices"
# Why a long context is worth keeping small: "recall decreases as tokens grow",
# "a performance gradient rather than a hard cliff".
CONTEXT_ENGINEERING_DOC = (
    "https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents")
BENCHMARKS = {
    "cost_per_active_day": {"average": 13.0, "p90": 30.0, "source": COSTS_DOC},
}

# `/usage` flags a behaviour (long context, cache misses) once it accounts for
# 10% or more of recent usage -- the same source as above. The panel applies
# that line to this tool's own measures of those habits, and says so: it is not
# Anthropic's verdict on them.
FLAG_SHARE = 0.10

# "Long context" is a request carrying more than 200k tokens. The line is ours,
# and labelled as such in the panel: Anthropic's pricing has no tier at 200k
# (Claude 4.6 and later include the 1M window at standard pricing). The other
# bands only split the long tail for display.
LONG_CONTEXT_TOKENS = 200_000
CONTEXT_BANDS = (100_000, LONG_CONTEXT_TOKENS, 500_000)

# Context tax is flagged on requests over this many tokens. 200k (above) no
# longer separates people on 1M-context models -- everyone was over 85% -- so
# the flag uses the top band edge. Ours, like 200k; labelled as such.
VERY_LONG_CONTEXT_TOKENS = CONTEXT_BANDS[2]

# Consecutive failed tool calls that count as a streak worth flagging. Ours.
ERROR_STREAK = 3

# Health rows are bucketed by the hour (see aggregate.build_payload).
BUCKET_MINUTES = 60

# Claude Code's label for a miss caused by the cache having expired between
# requests -- the "came back to a big session after a break" case.
EXPIRED_MISS = "previous_message_not_found"

EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
PLAN_TOOLS = frozenset({"EnterPlanMode", "ExitPlanMode"})

# The first Claude Code version in which this tool's author saw the record a
# measure depends on in their own logs. It is where *this tool* first saw it,
# not a date Anthropic published: Claude Code may have written the record
# earlier, so a session older than this is shown as "not recorded" (rather than
# zero) with that caveat -- see FIRST_RECORDED_NOTE, carried in the payload.
FIRST_RECORDED = {
    "denials": "2.1.246",       # user.toolDenialKind
    "compactions": "2.1.237",   # system/compact_boundary
}

FIRST_RECORDED_NOTE = (
    "Versions are the first this tool's author saw the record in their own logs, not "
    "dates Anthropic published; Claude Code may have written it earlier.")

CHECK_CATEGORIES = ("test", "lint", "build", "commit", "pr")
VERIFY_CATEGORIES = frozenset({"test", "lint", "build"})


# --------------------------------------------------------------------------- #
# Shell-command categories
# --------------------------------------------------------------------------- #
#
# The parser stores what classify_command returns, and the parse cache keeps it
# until parser.FACTS_VERSION changes. Any edit to the patterns below changes
# FileFacts content, so it needs a FACTS_VERSION bump, or cached files keep the
# old categories.

_ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
# Wrappers that run another command; stripped so "uv run pytest" is a test.
# Options a wrapper takes before the command go with it.
_WRAPPERS = re.compile(
    r"^(?:sudo|time|nice|env|command|exec|npx|bunx|pnpx|uvx|"
    r"poetry run|pipenv run|pdm run|hatch run|rye run|bundle exec|"
    r"dotenv(?: --)?|cross-env"
    r"|uv run(?:\s+(?:--(?:with|with-requirements|python|project|directory|package|extra|"
    r"group|env-file|index|index-url)(?:=|\s+)\S+|-p\s+\S+|--[\w-]+))*"
    r"|timeout(?:\s+(?:-[ks]\s*\S+|--[\w-]+(?:=\S+)?|-\w+))*\s+\d[\w.]*"
    rf"|xvfb-run(?:\s+(?:-[sfenlp]\s+{_ARG}|--[\w-]+(?:={_ARG})?|-\w+))*"
    r")\s+"
)
# A value may be quoted, a $(...) or `...` substitution (VAR=$(mktemp -d) cmd),
# or plain.
_ENV_ASSIGN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\$\([^)]*\)|`[^`]*`|\$(?!\()|[^\s'\"$`])*\s+")
# Shell keywords that open a command: "do pytest", "then make", "if pytest".
_KEYWORD = re.compile(r"^(?:do|then|else|elif|if|while|until|!|\{)\s+")
# A Python on its own, or named by a variable ("$PY", "${VENV}/bin/python").
_PYBIN = r"(?:python[0-9.]*|py|\"?\$\{?\w+\}?[\w./-]*\"?)"
# "./.venv/bin/pytest" is pytest, "./node_modules/.bin/jest" is jest.
_LEADING_PATH = re.compile(r"^[^\s'\"=]*/(?=[^\s/])")
# Options that come before the subcommand, dropped so the subcommand is next.
_PRE_OPTIONS = (
    (re.compile(r"^(g?make)(?:\s+(?:-[CfIo]\s*\S+|-j\s*\d*|--?[\w-]+(?:=\S+)?|[A-Za-z_]\w*=\S*))+"),
     r"\1"),
    (re.compile(r"^(npm|pnpm|yarn|bun)(?:\s+(?:(?:--prefix|--cwd|--dir|--filter|-C|-F)(?:=|\s+)\S+"
                r"|--?[\w-]+))+"), r"\1"),
    (re.compile(r"^git(?:\s+(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?))+"), "git"),
)
_JS_RUNNER = r"(?:npm|pnpm|yarn|bun)(?:\s+run(?:\s+--?[\w-]+)*)?"
_JVM = r"(?:mvnw?|gradlew?)"
# A dev server or watcher runs until stopped; it isn't a build.
_NOT_WATCHING = r"(?!.*\s(?:--watch\b|-\w*w\b|serve\b))"

_PATTERNS = (
    ("test", re.compile(
        r"^(?:pytest|py\.test|tox|nox|jest|vitest|mocha|ava|karma|rspec|phpunit|ctest)\b"
        rf"|^{_PYBIN}\s+-m\s+(?:pytest|unittest)\b"
        r"|^(?:python[0-9.]*\s+)?(?:\S*/)?manage\.py\s+test\b"
        r"|^node\b.*\s--test(?![\w-])"
        rf"|^{_JS_RUNNER}\s+test\b|^{_JS_RUNNER}\s+test:"
        # pnpm/yarn/bun run a package's own binary without `run`.
        r"|^(?:pnpm|yarn|bun)\s+(?:jest|vitest|mocha|ava|playwright\s+test|cypress\s+run)\b"
        r"|^(?:go|cargo|deno|dotnet|swift|mix|sbt|lein|bazel|flutter|dart)\s+test\b"
        r"|^cargo\s+nextest\b|^deno\s+task\s+test\b"
        r"|^(?:rails|rake|bin/rails|bin/rake)\s+(?:test|spec)\b"
        rf"|^{_JVM}\b.*\btest\b"
        r"|^make\s+(?:test|check)\b"
        r"|^(?:playwright|cypress)\s+(?:test|run)\b"
        r"|^xcodebuild\b.*\btest\b"
    )),
    ("lint", re.compile(
        r"^(?:eslint|flake8|pylint|mypy|pyright|tsc|vue-tsc|golangci-lint|rubocop|"
        r"shellcheck|biome|stylelint|swiftlint|ktlint|oxlint|hadolint|yamllint|markdownlint|"
        r"staticcheck|bandit|pre-commit|lefthook)\b"
        r"|^ruff\b(?!\s+format\b)|^ruff\s+format\b.*\s--check\b"
        # Formatters rewrite files; only their check modes are a check.
        r"|^(?:black|isort)\b.*\s--(?:check|check-only|diff)\b"
        rf"|^{_PYBIN}\s+-m\s+(?:mypy|ruff|flake8|pylint)\b"
        rf"|^{_PYBIN}\s+-m\s+black\b.*\s--(?:check|diff)\b"
        rf"|^{_JS_RUNNER}\s+(?:lint|typecheck|type-check|check|format:check)\b"
        r"|^cargo\s+(?:clippy|check)\b|^cargo\s+fmt\b.*\s--check\b"
        r"|^go\s+vet\b|^prettier\b.*--check|^next\s+lint\b|^deno\s+(?:lint|check)\b"
        r"|^make\s+lint\b"
    )),
    ("build", re.compile(
        rf"^{_JS_RUNNER}\s+build\b"
        r"|^(?:go|cargo|dotnet|swift|bazel|flutter)\s+build\b"
        rf"|^{_JVM}\b.*\b(?:package|compile|build|install|assemble|verify)\b"
        r"|^make(?:\s+(?:all|build))?\s*$|^make\s+(?:all|build)\b"
        r"|^(?:vite|next)\s+build\b"
        rf"|^(?:tsup|esbuild|rollup|webpack)(?!\S){_NOT_WATCHING}"
        r"|^(?:xcodebuild|docker\s+build|docker\s+compose\s+build|docker\s+buildx\s+build)\b"
        r"|^cmake\s+--build\b|^ninja\b(?!\s+-t\b)|^(?:turbo|nx)\s+(?:run\s+)?build\b"
        rf"|^(?:poetry|uv|pdm|hatch)\s+build\b|^{_PYBIN}\s+-m\s+build\b"
    )),
    ("commit", re.compile(r"^git\s+commit\b")),
    ("pr", re.compile(r"^gh\s+pr\s+create\b")),
)

# Longest segment looked at, and longest command: a single command is never this
# long, but a pasted script can be, and every pattern above is anchored at the
# start. The cap is per segment, so a long command is read to its end -- a test
# run after a 5,000-character heredoc still counts.
_MAX_SEGMENT = 1000
_MAX_COMMAND = 200_000

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
# A heredoc fed to one of these is a script, so its body is commands.
_SHELLS = re.compile(r"^(?:ba|z|da|k)?sh\b")
# `bash -c "..."`: the quoted string is itself a command line.
_SHELL_C = re.compile(r"^(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\s+(\"(?:[^\"\\]|\\.)*\"|'[^']*')")
_VERSION_QUERY = re.compile(r"\s--version(?:\s|$)")
_WATCH_OFF = ("false", "0", "no")


def _split_segments(command: str) -> list:
    """The command split at `&&`, `||`, `;`, `|` and newlines that are not inside
    quotes: `git commit -m "fix; make build pass"` is one segment, not a commit
    and a build. A quote with no partner is an ordinary character (an apostrophe
    in plain text), so it can't swallow the rest of the command."""
    out, start, i, n = [], 0, 0, len(command)
    while i < n:
        c = command[i]
        if c in "\"'":
            j = i + 1
            while j < n and command[j] != c:
                j += 2 if (c == '"' and command[j] == "\\") else 1
            if j < n:
                i = j + 1
                continue
        elif c == "\\":
            i += 2
            continue
        if c in ";|\n":
            out.append(command[start:i])
            i += 2 if command.startswith(("||",), i) else 1
            start = i
            continue
        if c == "&" and command.startswith("&&", i):
            out.append(command[start:i])
            i += 2
            start = i
            continue
        i += 1
    out.append(command[start:])
    return out


def _without_heredocs(command: str) -> str:
    """The command with heredoc bodies removed: they are text fed to a
    program, not commands, and often name commands ("run pytest")."""
    kept, terminator = [], None
    for line in command.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            segment = _split_segments(line[:match.start()])[-1]
            if not _SHELLS.match(_segment_head(segment)):
                terminator = match.group(2)
    return "\n".join(kept)


def _segment_head(segment: str) -> str:
    seg = segment[:_MAX_SEGMENT].strip().lstrip("(").strip()
    for _ in range(6):  # a few layers: "cd x && FOO=1 timeout 60 uv run pytest"
        before = seg
        seg = _KEYWORD.sub("", seg)
        seg = _ENV_ASSIGN.sub("", seg)
        seg = _LEADING_PATH.sub("", seg)
        seg = _WRAPPERS.sub("", seg)
        if seg == before:
            break
    for pattern, repl in _PRE_OPTIONS:
        seg = pattern.sub(repl, seg)
    return seg


def _is_watch(head: str) -> bool:
    """Is a test command a watcher (it runs until stopped, so it is no check)?
    `--watch=false` is not."""
    if re.match(r"^vitest\s+watch\b", head):
        return True
    for token in head.split():
        if token in ("--watch", "--watchAll", "--watchman"):
            return True
        if token.startswith("--watch=") and token.split("=", 1)[1].lower() not in _WATCH_OFF:
            return True
    return False


def classify_command(command, _depth=0) -> list:
    """Categories of the checks a shell command runs, e.g. ["commit", "test"].

    Called by the parser on a Bash tool call's command, in memory; only the
    returned categories are kept, never the command.
    """
    if not isinstance(command, str) or not command:
        return []
    found = set()
    command = command[:_MAX_COMMAND].replace("\\\n", " ")  # line continuations
    for segment in _split_segments(_without_heredocs(command)):
        head = _segment_head(segment)
        if not head or _VERSION_QUERY.search(head):
            continue
        inner = _SHELL_C.match(head)
        if inner and _depth < 2:
            found.update(classify_command(inner.group(1)[1:-1], _depth + 1))
            continue
        for category, pattern in _PATTERNS:
            if pattern.search(head):
                if category == "test" and _is_watch(head):
                    break
                found.add(category)
                break
    return sorted(found)


# Edits no test, lint or build checks: prose, Claude Code's own files (memory,
# plans, settings, launch.json), images, throwaway files in a temp or scratch
# folder, and files outside the folder the session runs in. Writing one of these
# after the last test doesn't leave the session's code unchecked. Like the
# command patterns above, a change here changes FileFacts and needs a
# FACTS_VERSION bump.
_NOTE_SUFFIXES = (".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".svg")
# Temp and scratch folders: used only when the session's own folder isn't known
# (inside it, a file is the session's work even if the folder is under /tmp).
_SCRATCH = re.compile(
    r"^/(?:private/)?(?:tmp|var/tmp)/|^/(?:private/)?var/folders/|/scratchpad/|"
    r"/appdata/local/temp/|^[a-z]:/(?:temp|tmp)/")


def _inside(path: str, folder) -> bool:
    if not isinstance(folder, str) or not folder:
        return False
    root = folder.replace("\\", "/").lower().rstrip("/")
    return bool(root) and (path == root or path.startswith(root + "/"))


def is_note_edit(path, cwds=()) -> bool:
    """Is an edited file a note rather than code? Called by the parser on an
    edit tool call's path, in memory; only the answer is kept.

    `cwds` are the folders the session ran in (the record's and the session's
    first). A file under one of them is the session's own work. An absolute path
    outside all of them -- a scratch script in /tmp, a file in another project --
    is not checked by this session's tests, so it is a note; with no folder
    known, only the temp and scratch locations are."""
    if not isinstance(path, str) or not path:
        return False
    p = path.replace("\\", "/").lower()
    if p.endswith(_NOTE_SUFFIXES) or "/.claude/" in p or p.startswith(".claude/"):
        return True
    absolute = p.startswith("/") or re.match(r"^[a-z]:/", p) is not None
    if not absolute:
        return False
    known = [c for c in cwds if isinstance(c, str) and c]
    if any(_inside(p, c) for c in known):
        return False
    return bool(known) or bool(_SCRATCH.search(p))


# --------------------------------------------------------------------------- #
# Measures
# --------------------------------------------------------------------------- #

def _version_tuple(version):
    parts = []
    for piece in str(version).split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def _recorded(versions, since: str) -> bool:
    """Did any Claude Code version this session ran on write the record?"""
    floor = _version_tuple(since)
    return any(_version_tuple(v) >= floor for v in (versions or {}))


def _context_tokens(rec: dict) -> int:
    """Tokens the request sent: everything it was charged input for. The flat
    cache-write total stands in when a record carries no 5m/1h breakdown."""
    writes = ((rec.get("cache_write_5m") or 0) + (rec.get("cache_write_1h") or 0)
              or rec.get("cache_write_fallback") or 0)
    return (rec.get("input_tokens") or 0) + (rec.get("cache_read_tokens") or 0) + writes


def _band(tokens: int) -> int:
    """Index of the band a request falls in. Each edge belongs to the band
    below it, so band >= the long band means "more than 200k", as the
    per-session figure counts it."""
    for i, edge in enumerate(CONTEXT_BANDS):
        if tokens <= edge:
            return i
    return len(CONTEXT_BANDS)


def _miss_cost(rec: dict) -> tuple:
    """(extra dollars, tokens) a labelled cache miss cost over reading the same
    tokens from a warm cache. Uses Claude Code's own count of missed tokens when
    present, else the request's cache writes. A model priced by prompt length
    is read at the rates its prompt size was billed at; writes split across the
    5m and 1h tiers are priced at the blend of the two rates."""
    rates = rates_for(rec["model"], rec.get("speed"), ts=rec.get("ts"),
                      prompt_tokens=_context_tokens(rec))
    cw_5m = rec.get("cache_write_5m") or 0
    cw_1h = rec.get("cache_write_1h") or 0
    written = cw_5m + cw_1h or rec.get("cache_write_fallback") or 0
    missed = rec.get("cache_missed_tokens") or written
    if not rates or not missed:
        return 0.0, missed
    # Each tier at its own rate, by its share of the writes. A request with no
    # 5m/1h breakdown is priced as 5m, as pricing.cost_for does.
    split = cw_5m + cw_1h
    write_rate = ((cw_5m * rates["cache_write_5m"] + cw_1h * rates["cache_write_1h"]) / split
                  if split else rates["cache_write_5m"])
    extra = missed / 1_000_000 * max(write_rate - rates["cache_read"], 0.0)
    if rec.get("service_tier") == "batch":
        extra *= 0.5
    return extra, missed


def _first_after_compaction(priced_winners, main_facts, sub_facts, ordered) -> set:
    """ids of the requests that are the first main-conversation request of their
    session after a compact boundary."""
    boundaries = defaultdict(set)  # session -> compaction timestamps
    for sid, facts in (ordered if ordered is not None else main_facts + sub_facts):
        for _uuid, ts, ekind, _value in getattr(facts, "health_events", None) or []:
            if ekind == "compact" and isinstance(ts, str) and ts:
                boundaries[sid].add(ts)
    if not boundaries:
        return set()
    by_session = defaultdict(list)
    for rec, sid, kind, _cost in priced_winners:
        if kind == "main" and sid in boundaries and rec.get("ts"):
            by_session[sid].append(rec)
    first = set()
    for sid, recs in by_session.items():
        recs.sort(key=lambda r: r["ts"])
        for boundary in boundaries[sid]:
            nxt = next((r for r in recs if r["ts"] > boundary), None)
            if nxt is not None:
                first.add(id(nxt))
    return first


def build(main_facts, sub_facts, derived_sessions, priced_winners, bucket, ordered=None) -> tuple:
    """Return (health payload, {session_id: per-session health}).

    main_facts / sub_facts: [(session_id, FileFacts)]
    priced_winners: [(record, session_id, kind, cost)] -- the global request
        dedupe from aggregate, with each request's estimated cost.
    bucket: the payload's time-bucketing function (local 5-minute buckets).
    ordered: every (session_id, FileFacts) in request-ownership order (see
        aggregate._dedupe_requests_across_files); a record repeated across
        resume/fork copies is credited to the first file holding it. Without
        it, mains come before subagents and non-derived sessions first.
    """
    per = defaultdict(lambda: {
        "main_cost": 0.0, "long_context_cost": 0.0, "very_long_context_cost": 0.0,
        "peak_context": 0,
        "long_context_requests": 0, "main_requests": 0,
        "cache_miss_cost": 0.0, "cold_cache_cost": 0.0,
        "compactions_auto": 0, "compactions_manual": 0,
        "prompts": 0, "plan_prompts": 0, "interrupts": 0, "queued_prompts": 0,
        "away": 0, "denials": {}, "edits": 0, "checks": {}, "max_error_streak": 0,
    })

    # ---- requests: context size and cache misses ----------------------- #
    context_rows = defaultdict(lambda: [0, 0.0])
    miss_rows = defaultdict(lambda: [0, 0.0, 0])
    # The first main-conversation request after a compaction has to write the
    # whole summarised context to the cache afresh. That is expected, not a miss
    # worth flagging (Claude Code's /usage doesn't count it as one either).
    compacted = _first_after_compaction(priced_winners, main_facts, sub_facts, ordered)
    for rec, sid, kind, cost in priced_winners:
        b = bucket(rec.get("ts"))
        if kind == "main":
            tokens = _context_tokens(rec)
            band = _band(tokens)
            acc = context_rows[(b, sid, rec["model"], band)]
            acc[0] += 1
            acc[1] += cost
            s = per[sid]
            s["main_cost"] += cost
            s["main_requests"] += 1
            s["peak_context"] = max(s["peak_context"], tokens)
            if tokens > LONG_CONTEXT_TOKENS:
                s["long_context_cost"] += cost
                s["long_context_requests"] += 1
            if tokens > VERY_LONG_CONTEXT_TOKENS:
                s["very_long_context_cost"] += cost
        reason = rec.get("cache_miss")
        if reason and id(rec) not in compacted:
            extra, missed = _miss_cost(rec)
            acc = miss_rows[(b, sid, rec["model"], reason)]
            acc[0] += 1
            acc[1] += extra
            acc[2] += missed
            per[sid]["cache_miss_cost"] += extra
            if reason == EXPIRED_MISS:
                per[sid]["cold_cache_cost"] += extra

    # ---- per-record events, once each across copies --------------------- #
    # In request-ownership order, so a repeated record is credited to the
    # session that owns it (as for friction and limits in aggregate).
    if ordered is None:
        ordered = (sorted(main_facts, key=lambda sf: sf[0] in derived_sessions)
                   + sorted(sub_facts, key=lambda sf: sf[0] in derived_sessions))
    event_rows = defaultdict(int)
    seen_events = set()
    # Prompts, interrupts and the rest belong to the main conversation; a
    # subagent's refused tool call is still a refusal the session ran into.
    for sid, facts in ordered:
        is_sub = facts.kind != "main"
        for uuid, ts, ekind, value in getattr(facts, "health_events", None) or []:
            if is_sub and ekind != "denial":
                continue
            # One record can be two events (a refusal by interrupt is also an
            # interrupt), so the kind is part of the key.
            if not isinstance(ts, str) or not ts:
                continue
            if not isinstance(value, (str, type(None))):
                value = None  # a wrong-typed value is also unhashable in the row key
            key = (uuid, ekind) if isinstance(uuid, str) and uuid else (ts, ekind, value, sid)
            if key in seen_events:
                continue
            seen_events.add(key)
            event_rows[(bucket(ts), sid, ekind, value)] += 1
            s = per[sid]
            if ekind == "prompt":
                s["prompts"] += 1
                if value == "plan":
                    s["plan_prompts"] += 1
            elif ekind == "interrupt":
                s["interrupts"] += 1
            elif ekind == "queued":
                s["queued_prompts"] += 1
            elif ekind == "away":
                s["away"] += 1
            elif ekind == "denial":
                s["denials"][value] = s["denials"].get(value, 0) + 1
            elif ekind == "compact":
                field = "compactions_manual" if value == "manual" else "compactions_auto"
                s[field] += 1

    # ---- tool calls: edits, checks, error streaks ---------------------- #
    # Subagent files count too: a subagent that edits or runs the tests is
    # doing it for its parent session.
    check_rows = defaultdict(int)
    streak_rows = defaultdict(int)
    seen_tools, seen_streaks = set(), set()
    last_edit, last_check = {}, {}
    plan_tool = set()
    for sid, facts in ordered:
        tool_ts = getattr(facts, "tool_ts", None) or {}
        checks = getattr(facts, "tool_checks", None) or {}
        notes = set(getattr(facts, "note_edits", None) or ())
        refused = set(getattr(facts, "tool_refused", None) or ())
        errors = getattr(facts, "tool_errors", None) or {}
        for tuid, name in (facts.tool_calls or {}).items():
            if tuid in seen_tools:
                continue
            seen_tools.add(tuid)
            ts = tool_ts.get(tuid)
            # Only code edits need checking: a memory file or doc written after
            # the last test run doesn't make the session "unchecked".
            if name in EDIT_TOOLS and tuid not in notes:
                per[sid]["edits"] += 1
                if ts and ts > last_edit.get(sid, ""):
                    last_edit[sid] = ts
            elif name in PLAN_TOOLS:
                plan_tool.add(sid)
            for category in checks.get(tuid) or ():
                # A refused command never ran, and a commit or PR that failed
                # made nothing. A failed test, lint or build still ran a check.
                if tuid in refused or (category in ("commit", "pr") and errors.get(tuid)):
                    continue
                check_rows[(bucket(ts), sid, category)] += 1
                s_checks = per[sid]["checks"]
                s_checks[category] = s_checks.get(category, 0) + 1
                if category in VERIFY_CATEGORIES and ts and ts > last_check.get(sid, ""):
                    last_check[sid] = ts
        for first_tuid, ts, length in getattr(facts, "error_streaks", None) or []:
            if first_tuid in seen_streaks:
                continue
            seen_streaks.add(first_tuid)
            streak_rows[(bucket(ts), sid, length)] += 1
            per[sid]["max_error_streak"] = max(per[sid]["max_error_streak"], length)

    # ---- per-session flags from the main transcript --------------------- #
    sessions = {}
    for sid, facts in main_facts:
        s = per[sid]
        modes = getattr(facts, "permission_modes", None) or {}
        instructions = getattr(facts, "instruction_types", None)
        sessions[sid] = {
            **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in s.items()},
            "long_context_share": (round(s["long_context_cost"] / s["main_cost"], 4)
                                   if s["main_cost"] else None),
            "very_long_context_share": (round(s["very_long_context_cost"] / s["main_cost"], 4)
                                        if s["main_cost"] else None),
            "ended_on_interrupt": bool(getattr(facts, "ended_on_interrupt", False)),
            "plan_mode": bool(s["plan_prompts"] or sid in plan_tool or modes.get("plan")),
            # None: this Claude Code version doesn't record which instruction
            # files it loaded -- not the same as "none were loaded".
            "instructions": sorted(instructions) if instructions is not None else None,
            "stop_hooks": bool(getattr(facts, "stop_hook_runs", 0)),
            "recorded": {name: _recorded(getattr(facts, "versions", None), since)
                         for name, since in FIRST_RECORDED.items()},
            # None when the session made no edits: there was nothing to check.
            "checked_after_last_edit": (
                (last_check.get(sid, "") >= last_edit[sid]) if sid in last_edit else None),
        }

    payload = {
        "bucket_minutes": BUCKET_MINUTES,
        "thresholds": {
            "long_context_tokens": LONG_CONTEXT_TOKENS,
            "context_bands": list(CONTEXT_BANDS),
            "flag_share": FLAG_SHARE,
            "flag_context_tokens": VERY_LONG_CONTEXT_TOKENS,
            "error_streak": ERROR_STREAK,
        },
        "benchmarks": BENCHMARKS,
        # Where `recorded` comes from: see FIRST_RECORDED.
        "first_recorded": {**FIRST_RECORDED, "note": FIRST_RECORDED_NOTE},
        "sources": {"costs": COSTS_DOC, "best_practices": BEST_PRACTICES_DOC,
                    "context_engineering": CONTEXT_ENGINEERING_DOC},
        # [bucket, session_id, model, band, requests, cost]; main thread only.
        # band indexes the gaps between context_bands (0 = under the first).
        "context": [[b, sid, model, band, n, round(cost, 6)]
                    for (b, sid, model, band), (n, cost) in context_rows.items()],
        # [bucket, session_id, model, reason, requests, extra_cost, missed_tokens]
        "cache_misses": [[b, sid, model, reason, n, round(extra, 6), tokens]
                         for (b, sid, model, reason), (n, extra, tokens) in miss_rows.items()],
        # [bucket, session_id, kind, value, count]; kinds: prompt (value: the
        # permission mode it was sent in), interrupt, queued, away, denial
        # (value: why the tool call was refused), compact (value: auto|manual).
        "events": [[b, sid, kind, value, n]
                   for (b, sid, kind, value), n in event_rows.items()],
        # [bucket, session_id, category, count]; category in CHECK_CATEGORIES.
        "checks": [[b, sid, category, n] for (b, sid, category), n in check_rows.items()],
        # [bucket, session_id, length, count]; runs of 2+ failed tool calls.
        "error_streaks": [[b, sid, length, n]
                          for (b, sid, length), n in streak_rows.items()],
    }
    for key in ("context", "cache_misses", "events", "checks", "error_streaks"):
        payload[key].sort(key=lambda r: (r[0] or "", r[1], str(r[2])))
    return payload, sessions
