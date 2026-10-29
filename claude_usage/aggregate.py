"""Pure aggregation: turns parsed per-file facts into the dashboard payload.

Takes FileFacts objects (from parser.py) for every main session file, every
subagent file (+ its optional meta), and produces the normalized JSON payload
served to the browser. Contains no file I/O.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

from . import health as health_mod
from . import limits as limits_mod
from . import pricing
from .sanitize import (counter_key as _counter_key, ms as _ms, num as _num, sid as _sid,
                       tok as _tok)
from .parser import active_ms_of
from .pricing import cost_for, normalize_model, rates_for

HOME = os.path.expanduser("~")
# Windows records a cwd with backslashes, so both separators are accepted and
# the name is normalised to forward slashes for display.
_WORKTREE_RE = re.compile(r"[/\\]\.claude[/\\]worktrees[/\\][^/\\]+.*$")
_TEMP_E2E_RE = re.compile(r"^/private/var/folders/")


def project_name_for(cwd: str | None, folder_name: str | None = None) -> str:
    """Best-effort human-readable project name for a session.

    Prefers the session's first `cwd`; falls back to v1's lossy folder-name
    decode (marked `(decoded)`) when no cwd was ever recorded.
    """
    if cwd:
        if _TEMP_E2E_RE.match(cwd):
            return "(temp e2e)"
        name = _WORKTREE_RE.sub("", cwd)
        # Only HOME itself or a path below it: "<HOME>X/p" is another directory.
        # On Windows HOME has backslashes and a cwd may use either separator.
        slashes = (lambda p: p.replace("\\", "/")) if os.sep == "\\" else (lambda p: p)
        home, probe = slashes(HOME.rstrip("/\\")), slashes(name)
        if home and (probe == home or probe.startswith(home + "/")):
            name = "~" + probe[len(home):]
        return name.replace("\\", "/") or cwd

    if folder_name:
        decoded = folder_name[1:] if folder_name.startswith("-") else folder_name
        return "/" + decoded.replace("-", "/") + " (decoded)"

    return "(unknown project)"


# The finest bucket the payload carries. The browser rolls these up into any
# coarser grain the user picks, so this is the floor on chart granularity.
# Requests cluster heavily inside a session, so 5-minute buckets cost very
# little: on a ~500-file log set they produce ~3.9k rows against ~330 for daily.
BASE_BUCKET_MINUTES = 5


def _local_date(ts: str, tz=None) -> Optional[str]:
    """Calendar-day bucket for an ISO8601 UTC timestamp string, or None when
    there is no usable one. `tz` is the zone the day is read in; None is this
    machine's zone (the personal dashboard), an export passes its export zone."""
    if not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return None


def _local_bucket(ts: str, minutes: int = BASE_BUCKET_MINUTES, tz=None) -> Optional[str]:
    """Wall-clock bucket in `tz` (None: this machine's zone), e.g.
    "2026-10-02T14:35", or None when `ts` is missing or unreadable -- callers
    fall back to the file's first day.

    Slots align to the hour (…:00, :05, :10) so a coarser roll-up built from
    these -- 8 hours, say -- lands on 00:00/08:00/16:00 rather than drifting
    from whenever the data happened to start. The first 10 characters are the
    local date, which is what the date-range filter compares on.
    """
    if not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
    except (ValueError, OverflowError, OSError):
        return None
    slot = ((dt.hour * 60 + dt.minute) // minutes) * minutes
    return "{}T{:02d}:{:02d}".format(dt.strftime("%Y-%m-%d"), slot // 60, slot % 60)


def _effective_cache_writes(rec: dict):
    """Cache-write tokens split into (5m, 1h).

    When a record carries no `cache_creation` breakdown, the flat
    `cache_creation_input_tokens` total is billed as 5m -- so it has to be
    counted as 5m for *tokens* too, or the token columns silently drop writes
    that the cost column charges for.
    """
    cw_5m = _tok(rec.get("cache_write_5m"))
    cw_1h = _tok(rec.get("cache_write_1h"))
    if not cw_5m and not cw_1h:
        return _tok(rec.get("cache_write_fallback")), 0
    return cw_5m, cw_1h


# Output tokens per character a response wrote, and per character of its
# thinking signature (the thinking itself is redacted). Fitted on 26,000
# subagent responses whose final count was logged: within 4% in aggregate for
# Opus 5, Opus 5.5 and Sonnet 5, 8-22% per response.
IMPUTE_CHAR_TOKENS = 0.39
IMPUTE_SIGNATURE_TOKENS = 0.28


def imputed_output(rec: dict) -> int:
    """Output tokens to add to a subagent response whose transcript never
    received its final line (Claude Code 2.1.284 on writes only the placeholder
    count from the start of the stream). Estimated from how much the response
    wrote, less what was logged; 0 for any response whose final count was
    logged, and for main-thread responses, which always have one."""
    if not rec.get("is_sub") or rec.get("final", True):
        return 0
    estimate = round(IMPUTE_CHAR_TOKENS * _tok(rec.get("out_chars"))
                     + IMPUTE_SIGNATURE_TOKENS * _tok(rec.get("sig_chars")))
    return max(0, estimate - _tok(rec["output_tokens"]))


def _effective_output(rec: dict) -> int:
    """Output tokens as priced and shown: logged plus imputed. The record's own
    output_tokens stays as logged, so the dedupe ranks copies as before."""
    return _tok(rec["output_tokens"]) + imputed_output(rec)


def _request_cost(rec: dict, unknown_models: set) -> float:
    cw_5m, cw_1h = _effective_cache_writes(rec)
    usage = {
        "input_tokens": _tok(rec["input_tokens"]),
        "output_tokens": _effective_output(rec),
        "cache_read_input_tokens": _tok(rec["cache_read_tokens"]),
        "cache_creation": {
            "ephemeral_5m_input_tokens": cw_5m,
            "ephemeral_1h_input_tokens": cw_1h,
        },
        "speed": rec.get("speed"),
        "service_tier": rec.get("service_tier"),
    }
    return cost_for(rec["model"], usage, unknown_models, ts=rec["ts"])


def _request_cache_saving(rec: dict) -> float:
    """Dollars prompt caching saved on this request.

    What the cache-read tokens would have cost as fresh input, *less* the
    premium paid to write the cache in the first place (a 5m write is 1.25x
    input, a 1h write 2x). Netting the premium off keeps the figure from
    overstating the benefit -- it is small in practice, but a "savings" number
    that ignores its own cost is the kind of number nobody trusts twice.
    """
    cw_5m, cw_1h = _effective_cache_writes(rec)
    cache_read = _tok(rec["cache_read_tokens"])
    prompt = _tok(rec.get("input_tokens")) + cache_read + cw_5m + cw_1h
    rates = rates_for(rec["model"], rec.get("speed"), ts=rec["ts"], prompt_tokens=prompt)
    if not rates:
        return 0.0
    saved = (
        cache_read / 1_000_000 * (rates["input"] - rates["cache_read"])
        - cw_5m / 1_000_000 * (rates["cache_write_5m"] - rates["input"])
        - cw_1h / 1_000_000 * (rates["cache_write_1h"] - rates["input"])
    )
    if rec.get("service_tier") == "batch":
        saved *= 0.5
    return saved


def _request_tokens(rec: dict) -> int:
    """Total billed tokens for one request: input + output + cache read + cache writes.
    Excludes thinking_tokens (already counted inside output_tokens) and the
    server-tool request counts (not token counts), matching the frontend's
    tokenTotal()."""
    cw_5m, cw_1h = _effective_cache_writes(rec)
    return (
        _tok(rec["input_tokens"]) + _effective_output(rec) + _tok(rec["cache_read_tokens"])
        + cw_5m + cw_1h
    )


def _counter_start(v) -> Optional[str]:
    """A cost-state startTime (epoch ms, as a number or a string) as a
    canonical UTC timestamp, comparable with a request's `ts`; None when it
    can't be read."""
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        dt = datetime.fromtimestamp(v / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def _logged_in_window(requests: list, state: dict, unknown_models: set) -> dict:
    """Per normalized model, {"cost", "tokens"} of the requests a CLI counter
    could have seen: those at or after its startTime and, when the snapshot's
    time is known, at or before it. Priced at this tool's rates, request by
    request, as everywhere else. A request with no time is kept: nothing says it
    falls outside."""
    since = _counter_start(state.get("startTime"))
    until = state.get("ts")
    out = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "cw_5m": 0, "cw_1h": 0})
    for rec in requests:
        ts = rec["ts"]
        if ts and ((since and ts < since) or (until and ts > until)):
            continue
        acc = out[normalize_model(rec["model"])]
        acc["cost"] += _request_cost(rec, unknown_models)
        acc["tokens"] += _request_tokens(rec)
        cw_5m, cw_1h = _effective_cache_writes(rec)
        acc["cw_5m"] += cw_5m
        acc["cw_1h"] += cw_1h
    return out


def _counter_repriced(model: str, cli: dict, state: dict, unknown_models: set,
                      split=(0, 0)) -> float:
    """What a counter's own tokens for one model cost at this tool's rates on
    the day of its last snapshot. The counter keeps no 5m/1h split of cache
    writes, so they take the transcripts' own split for the model in the same
    window, `split` = (5m, 1h) tokens (all 5m when they have none, as
    _unlogged does). Pricing them all as 5m would read low wherever Claude Code
    wrote 1-hour cache, which costs more, and look like a rate difference.
    Summed tokens, so no prompt-length tier."""
    t_5m, t_1h = split
    writes = cli["cache_write_tokens"]
    cw_1h = round(writes * t_1h / (t_5m + t_1h)) if t_5m + t_1h else 0
    return cost_for(model, {
        "input_tokens": cli["input_tokens"], "output_tokens": cli["output_tokens"],
        "cache_read_input_tokens": cli["cache_read_tokens"],
        "cache_creation": {"ephemeral_5m_input_tokens": writes - cw_1h,
                           "ephemeral_1h_input_tokens": cw_1h},
    }, unknown_models, ts=state.get("ts"), summed=True)


def _unlogged(cli_by_model: dict, requests: list, state: dict, unknown_models: set):
    """(cost, per-model rows) of what a CLI counter recorded and the transcripts
    did not: per model and token category, the counter's tokens less the
    group's requests (output as logged plus imputed), priced at this tool's
    rates on the day of the counter's last snapshot -- not at the counter's own
    costUSD, so a rate the counter disagrees with is not imported.

    Only requests made since the counter started are compared: a resumed
    session's counter restarts from zero, and its earlier work would hide a
    shortfall. A category where the transcripts hold more is 0, not negative.
    Cache writes have no 5m/1h split in the counter, so the shortfall takes
    the transcript's own split for that model (all 5m when it has none, as for
    an unsplit total in a transcript)."""
    since = _counter_start(state.get("startTime"))
    logged = defaultdict(lambda: [0, 0, 0, 0, 0])  # input, output, cache read, 5m, 1h
    for rec in requests:
        if since and rec["ts"] and rec["ts"] < since:
            continue
        cw_5m, cw_1h = _effective_cache_writes(rec)
        acc = logged[normalize_model(rec["model"])]
        acc[0] += _tok(rec["input_tokens"])
        acc[1] += _effective_output(rec)
        acc[2] += _tok(rec["cache_read_tokens"])
        acc[3] += cw_5m
        acc[4] += cw_1h
    total, rows = 0.0, []
    for model, cli in sorted(cli_by_model.items()):
        t_in, t_out, t_read, t_5m, t_1h = logged.get(model, (0, 0, 0, 0, 0))
        short_write = max(0, cli["cache_write_tokens"] - t_5m - t_1h)
        cw_1h = round(short_write * t_1h / (t_5m + t_1h)) if t_5m + t_1h else 0
        row = {
            "model": model,
            "input_tokens": max(0, cli["input_tokens"] - t_in),
            "output_tokens": max(0, cli["output_tokens"] - t_out),
            "cache_read_tokens": max(0, cli["cache_read_tokens"] - t_read),
            "cache_write_5m": short_write - cw_1h,
            "cache_write_1h": cw_1h,
        }
        if not any(v for k, v in row.items() if k != "model"):
            continue
        cost = cost_for(model, {
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cache_read_input_tokens": row["cache_read_tokens"],
            "cache_creation": {"ephemeral_5m_input_tokens": row["cache_write_5m"],
                               "ephemeral_1h_input_tokens": row["cache_write_1h"]},
        # Many calls' tokens added together: their sum can pass a prompt-length
        # threshold that no single call did, so the base rates apply. A call
        # that really was over it is undercounted; the logs can't say which.
        }, unknown_models, ts=state.get("ts"), summed=True)
        row["cost"] = round(cost, 6)
        rows.append(row)
        total += cost
    return total, rows


def _response_key(rec: dict) -> str:
    """The id one API response is counted under: message.id, falling back to
    requestId (see parser.py). Records cached before response_id existed are
    discarded by FACTS_VERSION, so the fallback is only belt and braces."""
    return rec.get("response_id") or rec["request_id"]


def _beats(cand, incumbent) -> bool:
    """Should `cand` replace `incumbent` as the owner of a request id?

    Each tuple is (output_tokens, kind, n_requests_in_session, first_ts, sid),
    where the count is per session: its main transcript plus its subagents.
    """
    c_out, c_kind, c_n, c_start, c_sid = cand
    i_out, i_kind, i_n, i_start, i_sid = incumbent
    if c_out != i_out:
        # Streaming writes several lines per request and only the last carries the
        # final output_tokens, so the largest is the complete record.
        return c_out > i_out
    if c_kind != i_kind:
        return c_kind == "main"  # a main transcript outranks a subagent one
    if c_n != i_n:
        # Resume/fork writes a new transcript that is a prefix-copy of an existing
        # one, so the file holding *more* requests is the original and the other is
        # the copy. Giving the request to the superset keeps per-session cost
        # attached to the session that actually ran it.
        return c_n > i_n
    if c_start != i_start:
        return (c_start or "") < (i_start or "")
    return c_sid < i_sid


def _dedupe_requests_across_files(main_facts: list, sub_facts: list):
    """One global winner per response (message.id). Returns (winners, owned_by_path,
    ordered): winners is a flat list of (record, session_id, kind, path) tuples,
    owned_by_path maps a file's path -> how many of its requests it kept, and
    ordered is every (session_id, facts) pair ranked as _beats ranks owners --
    mains first, then the session with more requests, the earlier start, the
    smaller id. Anything else repeated across fork/resume copies (tool calls,
    prompts, PR links) is credited to the first file in `ordered` that holds it,
    so it lands on the same session as the requests.

    `ordered` cannot see the per-request output_tokens that _beats compares
    first. When a request's owner is chosen on that tiebreak (a later copy
    holds the final, larger count), a tool call can be credited to a file that
    doesn't own its request. That case is rare."""
    session_start = {}
    file_requests = Counter()
    for sid, facts in main_facts + sub_facts:
        file_requests[sid] += len(facts.requests)
        if facts.first_ts and (sid not in session_start or facts.first_ts < session_start[sid]):
            session_start[sid] = facts.first_ts

    winners = {}  # request_id -> (record, session_id, kind, owning file path)
    ranks = {}    # request_id -> comparison tuple for the current winner
    for sid, facts in main_facts + sub_facts:
        for rec in facts.requests:
            rid = _response_key(rec)
            rank = (rec["output_tokens"], facts.kind, file_requests[sid],
                    session_start.get(sid), sid)
            if rid not in winners or _beats(rank, ranks[rid]):
                winners[rid] = (rec, sid, facts.kind, facts.path)
                ranks[rid] = rank

    # Ownership is tracked per *file*, not per session: fork detection asks
    # whether a main transcript kept any of its own requests, and a subagent
    # request that is itself only a copy must not answer that on its behalf.
    owned_by_path = Counter()
    for _, _, _, path in winners.values():
        owned_by_path[path] += 1
    ordered = sorted(main_facts + sub_facts,
                     key=lambda p: (p[1].kind != "main", -file_requests[p[0]],
                                    session_start.get(p[0]) or "", p[0]))
    return list(winners.values()), owned_by_path, ordered


def _session_activity(ordered: list) -> dict:
    """{session key: (active_ms, active_start)} for every main transcript.

    A fork or resume starts with a byte-copy of the original's records, uuids
    included, so measuring each file whole counts that history twice. Files are
    visited in `ordered` (originals first, as for request ownership) and a
    record whose uuid an earlier file already holds is a copy. A file is still
    walked whole, so the gap into the first record it wrote itself (the user
    typing a prompt) is kept, but a gap counts only when it ends on a record
    the file kept. Every gap is thereby counted by exactly one file: the
    earliest file in `ordered` that holds the ending record. `active_start` is
    the first record the file wrote itself.

    Only an earlier *file's* uuids make a record a copy, so a uuid repeated
    inside one file still counts, and a record with no uuid can't be matched
    and stays in its own file. Subagent transcripts are not sessions here."""
    seen = set()
    out = {}
    for sid, facts in ordered:
        if facts.kind != "main":
            continue
        activity = getattr(facts, "activity", None) or []
        kept = [uuid is None or uuid not in seen for uuid, _ in activity]
        seen.update(uuid for uuid, _ in activity if uuid is not None)
        out[sid] = (active_ms_of((ts for _, ts in activity), kept),
                    min((ts for (_, ts), k in zip(activity, kept) if k), default=None))
    return out


def build_payload(main_sessions: dict, sub_agents: list, now_iso: str, tz=None) -> dict:
    """
    main_sessions: {key: {"facts": FileFacts, "folder": str, "session_id": str (optional)}}
    sub_agents: [{"session_id": parent_key, "agent_id": str, "facts": FileFacts, "meta": dict,
                  "real_session_id": str (optional), "folder": str (optional)}]

    `key` identifies a session throughout the payload (it is the join column of
    every table). It is the session id, except when the same id turns up in a
    second project folder; scan.py then keys that one "<id>@<folder>". The real
    id is kept on each session row as `session_id`; `id` is the key.

    `tz` is the zone every day and bucket is read in: None is this machine's
    zone, which is what the personal dashboard shows; an export passes its
    export zone. Nothing but the label of a day or bucket depends on it.
    """
    unknown_models = set()

    main_facts = [(sid, info["facts"]) for sid, info in main_sessions.items()]
    sub_facts = [(a["session_id"], a["facts"]) for a in sub_agents]
    all_winners, owned_by_path, ordered = _dedupe_requests_across_files(main_facts, sub_facts)

    # Every response each session's files hold (main transcript and subagents),
    # whoever won it, for _unlogged: a counter saw the requests in its own
    # files even when a fork copied them and won them.
    winner_by_key = {_response_key(rec): rec for rec, _, _, _ in all_winners}
    session_held = defaultdict(set)
    for sid, facts in main_facts + sub_facts:
        session_held[sid].update(_response_key(rec) for rec in facts.requests)

    # A main transcript whose every request was won by another file is a
    # resume/fork copy of that file, not a session of its own. Counting it would
    # double-count the session (and its active time) in every total.
    #
    # Unless its subagents ran requests no other file holds: then it is the
    # original whose main thread was copied wholesale into a continuation (the
    # superset, so it wins the shared requests). Hiding it would drop those
    # subagents' cost from every per-session view while Total cost keeps it --
    # on a real log set, a few hundred requests.
    sub_owners = {sid for _, sid, kind, _ in all_winners if kind == "sub"}
    derived_sessions = {
        sid for sid, info in main_sessions.items()
        if len(info["facts"].requests) > 0 and owned_by_path.get(info["facts"].path, 0) == 0
        and sid not in sub_owners
    }

    projects = {}  # session_id -> project name
    for sid, info in main_sessions.items():
        projects[sid] = project_name_for(info["facts"].first_cwd, info["folder"])

    # A subagent whose parent transcript is gone has no session row to take a
    # project from, yet its cost is in every total. It takes the project of its
    # own cwd (else its folder), so a project filter still finds it.
    orphan_origin = {}  # parent key -> [first cwd, folder]
    for a in sub_agents:
        key = a["session_id"]
        if key in projects:
            continue
        origin = orphan_origin.setdefault(key, [None, a.get("folder")])
        origin[0] = origin[0] or a["facts"].first_cwd
    agent_projects = {key: project_name_for(cwd, folder) for key, (cwd, folder) in orphan_origin.items()}
    agent_projects.update(projects)

    # request_id -> agent_type, built once so the main loop below never has to
    # scan sub_agents/requests per record (O(1) lookup instead of O(n*m)).
    request_agent_type = {}
    for a in sub_agents:
        agent_type_name = (a.get("meta") or {}).get("agent_type", "unknown")
        for r in a["facts"].requests:
            request_agent_type[_response_key(r)] = agent_type_name

    usage_buckets = defaultdict(lambda: defaultdict(float))
    usage_counts = defaultdict(int)
    skill_set, tool_set, model_set, agent_type_set = set(), set(), set(), set()

    session_cost = defaultdict(float)       # main-thread cost per session
    session_sub_cost = defaultdict(float)   # subagent cost per session
    session_requests = defaultdict(int)
    session_models = defaultdict(set)
    session_imputed = defaultdict(int)       # imputed output tokens per session
    session_placeholders = defaultdict(int)  # responses whose output was imputed
    # Cost of requests won by a subagent file, keyed by that file's path. Keying
    # on agent_id would double-count the rare agent whose transcript is copied
    # under two parent session directories.
    agent_cost = defaultdict(float)
    agent_tokens = defaultdict(int)
    agent_requests = defaultdict(int)

    fast_requests = 0
    request_times = []  # (ts, cost) of every successful request, for limit episodes
    priced_winners = []  # (record, session_id, kind, cost), for the health measures
    for rec, sid, kind, owner_path in all_winners:
        cost = _request_cost(rec, unknown_models)
        request_times.append((rec["ts"], cost))
        priced_winners.append((rec, sid, kind, cost))
        tokens = _request_tokens(rec)
        # Finest bucket the payload carries; the browser rolls it up coarser.
        date = _local_bucket(rec["ts"], tz=tz)
        skill = rec["skill"] or "(none)"
        model = rec["model"]
        model_set.add(model)
        skill_set.add(skill)
        session_requests[sid] += 1
        session_models[sid].add(model)
        imputed = imputed_output(rec)
        if imputed:
            session_imputed[sid] += imputed
            session_placeholders[sid] += 1
        if rec.get("speed") == "fast":
            fast_requests += 1

        if kind == "main":
            session_cost[sid] += cost
        else:
            session_sub_cost[sid] += cost
            agent_cost[owner_path] += cost
            agent_tokens[owner_path] += tokens
            agent_requests[owner_path] += 1

        agent_type = -1
        if kind == "sub":
            agent_type = request_agent_type.get(_response_key(rec), "unknown")
            agent_type_set.add(agent_type)

        cw_5m, cw_1h = _effective_cache_writes(rec)
        key = (date, sid, model, skill, agent_type)
        b = usage_buckets[key]
        b["input_tokens"] += _tok(rec["input_tokens"])
        b["output_tokens"] += _effective_output(rec)
        b["imputed_output"] += imputed
        b["cache_read_tokens"] += _tok(rec["cache_read_tokens"])
        b["cache_write_5m"] += cw_5m
        b["cache_write_1h"] += cw_1h
        b["thinking_tokens"] += _tok(rec["thinking_tokens"])
        if not rec.get("thinking_logged", True):
            b["thinking_unlogged"] += 1
        b["web_search_requests"] += _tok(rec["web_search_requests"])
        b["web_fetch_requests"] += _tok(rec.get("web_fetch_requests"))
        b["cost"] += cost
        b["cache_saved"] += _request_cache_saving(rec)
        usage_counts[key] += 1

    usage_rows = []
    for (date, sid, model, skill, agent_type), b in usage_buckets.items():
        usage_rows.append([
            date, sid, model, skill, agent_type,
            round(b["input_tokens"]), round(b["output_tokens"]), round(b["cache_read_tokens"]),
            round(b["cache_write_5m"]), round(b["cache_write_1h"]), round(b["thinking_tokens"]),
            round(b["web_search_requests"]), usage_counts[(date, sid, model, skill, agent_type)],
            round(b["cost"], 6), round(b["web_fetch_requests"]),
            round(b["cache_saved"], 6),
            # Of the output in column 6, how much was imputed (imputed_output).
            round(b["imputed_output"]),
            # Responses that logged no thinking count (column 10 holds zero for
            # them, though they may have thought).
            int(b["thinking_unlogged"]),
        ])

    # Hourly buckets: a habits panel reads days and weeks, and the finer
    # 5-minute grid would triple the payload for no visible difference.
    session_activity = _session_activity(ordered)

    health, session_health = health_mod.build(
        main_facts, sub_facts, derived_sessions, priced_winners,
        lambda ts: _local_bucket(ts, health_mod.BUCKET_MINUTES, tz), ordered=ordered)

    # ---- Tools (main + sub) ----
    tool_rows_map = defaultdict(lambda: {"calls": 0, "errors": 0})
    skill_call_map = defaultdict(int)
    session_main_tool_totals = defaultdict(lambda: {"calls": 0, "errors": 0})
    # A fork/resume copy repeats the original's tool calls, and the same subagent
    # transcript can appear under two parent sessions. tool_use_ids are globally
    # unique, so the first file to claim one wins and later repeats are skipped.
    # Files are visited in request-ownership order (`ordered`), so the credit
    # lands on the session that owns the requests rather than on a copy of it.
    seen_tool_ids = set()
    seen_skill_ids = set()  # Skill calls are tool calls: the same rule applies
    for sid, facts in ordered:
        is_sub = facts.kind != "main"
        fallback_date = _local_bucket(facts.first_ts, tz=tz)
        tool_ts = getattr(facts, "tool_ts", None) or {}

        def _record(tuid, name, is_error):
            if tuid in seen_tool_ids:
                return
            seen_tool_ids.add(tuid)
            # Bucket by when the call happened, not when the session started --
            # sessions routinely span days, and a session-start date would pile a
            # month of tool calls onto day one.
            date = _local_bucket(tool_ts.get(tuid), tz=tz) or fallback_date
            key = (date, sid, name, is_sub)
            tool_rows_map[key]["calls"] += 1
            if is_error:
                tool_rows_map[key]["errors"] += 1
            if not is_sub:
                session_main_tool_totals[sid]["calls"] += 1
                if is_error:
                    session_main_tool_totals[sid]["errors"] += 1

        for tuid, name in facts.tool_calls.items():
            tool_set.add(name)
            _record(tuid, name, bool(facts.tool_errors.get(tuid)))
        for tuid, is_error in facts.tool_errors.items():
            if tuid not in facts.tool_calls:
                tool_set.add("(unknown)")
                _record(tuid, "(unknown)", is_error)
        for s in facts.skill_invocations:
            if s.get("id"):
                if s["id"] in seen_skill_ids:
                    continue
                seen_skill_ids.add(s["id"])
            skill_call_map[(_local_bucket(s["ts"], tz=tz) or fallback_date, sid, s["skill"])] += 1
            skill_set.add(s["skill"])

    tools_rows = [[d, sid, name, v["calls"], v["errors"], is_sub]
                  for (d, sid, name, is_sub), v in tool_rows_map.items()]
    skill_calls_rows = [[d, sid, skill, n] for (d, sid, skill), n in skill_call_map.items()]

    # ---- Agents ----
    agents_rows = []
    for a in sub_agents:
        facts = a["facts"]
        meta = a.get("meta") or {}
        path = facts.path
        agents_rows.append({
            "session_id": a["session_id"],
            "real_session_id": a.get("real_session_id") or a["session_id"],
            "project": agent_projects[a["session_id"]],
            "agent_id": a["agent_id"],
            "date": _local_date(facts.first_ts, tz),
            "agent_type": meta.get("agent_type", "unknown"),
            "spawn_depth": meta.get("spawn_depth", 1),
            "description": meta.get("description", ""),
            # Taken from the global dedupe, not a raw per-file sum, so the Agents
            # tab and the Overview's subagent share can't disagree.
            "cost": round(agent_cost.get(path, 0.0), 6),
            "tokens": agent_tokens.get(path, 0),
            "requests": agent_requests.get(path, 0),
            "models": sorted({r["model"] for r in facts.requests}),
        })
        agent_type_set.add(meta.get("agent_type", "unknown"))

    # ---- PRs (deduped by URL) ----
    prs_by_url = {}
    pr_ts = {}
    # `ordered` puts the owning session first; the first claim on a URL stands.
    for sid, facts in ordered:
        if facts.kind != "main":
            continue
        for pr in facts.pr_links:
            if not _sid(pr.get("prUrl")):
                continue
            pr = {**pr, "timestamp": _sid(pr.get("timestamp")),
                  "prRepository": _sid(pr.get("prRepository"))}
            # A fork/resume copy repeats the original's pr-link entries, so the
            # same URL can arrive from several transcripts; the owner got there
            # first.
            if pr["prUrl"] in prs_by_url:
                continue
            prs_by_url[pr["prUrl"]] = {
                "session_id": sid,
                "real_session_id": main_sessions[sid].get("session_id") or sid,
                "date": _local_date(pr["timestamp"], tz),
                "repo": pr["prRepository"], "number": pr["prNumber"], "url": pr["prUrl"],
            }
            pr_ts[pr["prUrl"]] = pr["timestamp"] or ""
    # Newest first, by the time each was opened, so same-day PRs keep their order.
    prs_rows = sorted(prs_by_url.values(), key=lambda r: pr_ts[r["url"]], reverse=True)
    prs_by_session = defaultdict(list)
    for pr in prs_rows:
        prs_by_session[pr["session_id"]].append(pr)

    subagents_by_session = Counter(a["session_id"] for a in sub_agents)

    # ---- CLI counters -------------------------------------------------- #
    # The CLI's cost-state is a running counter, identified by its startTime. A
    # branched or resumed transcript inherits the counter it came from -- on a
    # real log set a 76-request branch carried its parent's whole counter -- so
    # taking each transcript's snapshot at face value counts one counter once
    # per copy. Transcripts sharing a counter are grouped; the group's figures
    # come from its latest snapshot and are shown once, on the session that did
    # most of the work, and compared against the group's combined estimate.
    def _est(s):
        return session_cost.get(s, 0.0) + session_sub_cost.get(s, 0.0)

    best_states = {}
    counters = defaultdict(list)
    for sid, info in main_sessions.items():
        states = info["facts"].cost_states
        if not states:
            continue
        best_states[sid] = max(states, key=lambda cs: _num(cs["totalCostUSD"]))
        counters[_counter_key(best_states[sid].get("startTime")) or ("session", sid)].append(sid)
    counter_of = {}
    for members in counters.values():
        latest = max(members, key=lambda s: (_num(best_states[s]["totalCostUSD"]), s))
        owner = max(members, key=lambda s: (s not in derived_sessions, _est(s), s))
        group = {"owner": owner, "members": sorted(members), "latest": latest,
                 "states": [cs for m in members for cs in main_sessions[m]["facts"].cost_states]}
        for m in members:
            counter_of[m] = group

    # Lines added/removed are running totals for the whole counter, so a
    # session's final figure says nothing about *when* the lines were written.
    # Each rise in the counter is put on the time it was recorded, on the
    # session that shows the counter; the steps add up to its final figure.
    # [bucket, session_id, lines_added, lines_removed]
    lines_rows = []
    for group in {id(g): g for g in counter_of.values()}.values():
        owner = group["owner"]
        fallback = main_sessions[owner]["facts"].first_ts
        top_added = top_removed = 0
        for cs in sorted(group["states"], key=lambda cs: (cs.get("ts") or fallback or "",
                                                          _tok(cs["totalLinesAdded"]))):
            added = max(0, _tok(cs["totalLinesAdded"]) - top_added)
            removed = max(0, _tok(cs["totalLinesRemoved"]) - top_removed)
            top_added += added
            top_removed += removed
            if added or removed:
                lines_rows.append([_local_bucket(cs.get("ts") or fallback, tz=tz), owner, added, removed])

    # ---- Sessions table ----
    sessions_rows = []
    reconcile = defaultdict(lambda: {"est_cost": 0.0, "est_tokens": 0, "cli_cost": 0.0,
                                     "cli_tokens": 0, "cli_repriced": 0.0, "sessions": 0})
    for sid, info in main_sessions.items():
        facts = info["facts"]
        group = counter_of.get(sid)
        owns_counter = bool(group) and group["owner"] == sid
        # The richest snapshot of the counter wins: the CLI rewrites it as the
        # session grows, and a resumed session restarts it from zero. Only the
        # counter's owner carries it; another transcript on the same counter
        # shows nothing, so summing sessions never counts a counter twice.
        best_state = best_states[group["latest"]] if owns_counter else None
        states = group["states"] if owns_counter else []
        members = group["members"] if owns_counter else [sid]
        cli_cost = _num(best_state["totalCostUSD"]) if best_state else None
        cli_models = (best_state or {}).get("modelUsage") or {}
        lines_added = max((_tok(cs["totalLinesAdded"]) for cs in states), default=None)
        lines_removed = max((_tok(cs["totalLinesRemoved"]) for cs in states), default=None)
        api_ms = max((_ms(cs["totalAPIDuration"]) for cs in states), default=None)
        tool_ms = max((_ms(cs["totalToolDuration"]) for cs in states), default=None)

        real_id = info.get("session_id") or sid
        title = facts.custom_title or facts.ai_title or facts.agent_name or real_id
        est_main = round(session_cost.get(sid, 0.0), 6)
        est_sub = round(session_sub_cost.get(sid, 0.0), 6)
        counter_est = sum(_est(m) for m in members)
        diff_flag = None
        if cli_cost is not None and counter_est > 0:
            diff_flag = abs(cli_cost - counter_est) / counter_est > 0.15

        # Per-model estimate vs the CLI's own modelUsage, for the models the CLI
        # reported on. Only sessions carrying modelUsage contribute.
        #
        # The CLI splits the 1M-context mode into its own "<model>[1m]" bucket
        # while the transcripts record the bare model name, so both sides are
        # normalized before comparing -- otherwise every "[1m]" row reads as a
        # 100% shortfall. The split is labelling only, not pricing: the CLI's own
        # figure for claude-opus-5[1m] reproduces exactly at standard Opus 5
        # rates.
        cli_by_model = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0,
                                            "cache_read_tokens": 0,
                                            "cache_write_tokens": 0, "cost": 0.0})
        for model, cli in cli_models.items():
            acc = cli_by_model[normalize_model(model)]
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens"):
                acc[k] += _tok(cli[k])
            acc["cost"] += _num(cli["cost"])

        # The transcript side is the requests the counter could have seen: every
        # response the counter's files hold, whoever won it in the dedupe (a
        # continuation that copied the session wins its requests, but the
        # counter saw them in the original), from the counter's start to its
        # last snapshot. Counting everything the session won, over its whole
        # life, set a counter's window against a longer one.
        held = set()
        est_by_model = {}
        if best_state and cli_by_model:
            held = set().union(*(session_held.get(m, ()) for m in members))
            est_by_model = _logged_in_window(
                [winner_by_key[k] for k in held], best_state, unknown_models)

        model_rows = []
        for model, cli in cli_by_model.items():
            est = est_by_model.get(model, {"cost": 0.0, "tokens": 0})
            cli_tokens = (cli["input_tokens"] + cli["output_tokens"]
                          + cli["cache_read_tokens"] + cli["cache_write_tokens"])
            # The counter's own tokens at this tool's rates: against its costUSD
            # that tests the rate table; against `est`, nothing about rates.
            repriced = _counter_repriced(model, cli, best_state, unknown_models,
                                         (est.get("cw_5m", 0), est.get("cw_1h", 0)))
            model_rows.append({
                "model": model,
                "est_cost": round(est["cost"], 6), "est_tokens": est["tokens"],
                "cli_cost": round(cli["cost"], 6), "cli_tokens": cli_tokens,
                "cli_repriced": round(repriced, 6),
            })
            agg = reconcile[model]
            agg["est_cost"] += est["cost"]
            agg["est_tokens"] += est["tokens"]
            agg["cli_cost"] += cli["cost"]
            agg["cli_tokens"] += cli_tokens
            agg["cli_repriced"] += repriced
            agg["sessions"] += len(members)
        model_rows.sort(key=lambda r: -r["cli_cost"])

        # What the counter saw and no transcript did. Blank, not zero, without
        # a counter: nothing measured it.
        unlogged_cost, unlogged_models = None, []
        if best_state and cli_by_model:
            unlogged_cost, unlogged_models = _unlogged(
                cli_by_model, [winner_by_key[k] for k in held], best_state, unknown_models)
            unlogged_cost = round(unlogged_cost, 6)

        sessions_rows.append({
            "id": sid,
            "session_id": real_id,
            "project": projects[sid],
            "title": title,
            "start": facts.first_ts,
            "end": facts.last_ts,
            # The build machine's calendar days, so a dashboard opened in another
            # timezone filters sessions by the same days the usage rows carry.
            "start_day": _local_date(facts.first_ts, tz),
            "end_day": _local_date(facts.last_ts, tz),
            "active_ms": round(session_activity[sid][0]),
            # The first record this transcript wrote itself, which is later than
            # `start` for a fork (its first records are the original's, copied).
            # `start` is left as it was: the team merge recognises a continuation
            # by the original's (start, project) carried over to it.
            "active_start": session_activity[sid][1],
            "entrypoints": dict(facts.entrypoints),
            "versions": dict(facts.versions),
            "branches": dict(facts.branches),
            "permission_modes": dict(facts.permission_modes),
            "models": sorted(session_models.get(sid, [])),
            "requests": session_requests.get(sid, 0),
            "requests_in_file": len(facts.requests),
            "est_cost_main": est_main,
            "est_cost_sub": est_sub,
            "cli_cost": cli_cost,
            "cli_models": model_rows,
            "cost_diverges": diff_flag,
            # Calls the counter recorded and the transcripts did not, priced at
            # this tool's rates; kept apart from est_cost_main/est_cost_sub.
            "est_cost_unlogged": unlogged_cost,
            "unlogged_models": unlogged_models,
            "imputed_output_tokens": session_imputed.get(sid, 0),
            "placeholder_responses": session_placeholders.get(sid, 0),
            # Other transcripts on this session's CLI counter (owner only), or
            # the session whose row carries this one's counter (everyone else).
            "cli_counter_shared_with": [m for m in members if m != sid],
            "cli_counted_in": group["owner"] if group and not owns_counter else None,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "api_ms": api_ms,
            "tool_ms": tool_ms,
            "n_subagents": subagents_by_session.get(sid, 0),
            "tool_calls": session_main_tool_totals[sid]["calls"],
            "tool_errors": session_main_tool_totals[sid]["errors"],
            "prs": prs_by_session.get(sid, []),
            # True when every request in this transcript is also in another one
            # and it owns no subagent requests either, i.e. it is a resume/fork
            # copy rather than a distinct session.
            "derived": sid in derived_sessions,
            "health": session_health.get(sid),
            "continued_in": facts.continued_in,
        })

    # ---- limits and friction ------------------------------------------- #
    # Fork/resume copies repeat these records verbatim, so each is counted once
    # by its uuid -- on a real log set 289 retry records were 203 retries.
    # Files are read in request-ownership order, so a repeated record is
    # credited to the session that actually owns it -- a copy has no row of its
    # own in the session sheet.
    limit_events, seen_limits = [], set()
    friction_rows, seen_friction = [], set()
    for sid, facts in ordered:
        for ev in getattr(facts, "limit_events", None) or []:
            key = _sid(ev.get("uuid")) or (ev.get("ts"), ev.get("kind"))
            if key in seen_limits:
                continue
            seen_limits.add(key)
            limit_events.append({**ev, "session_id": sid})
        for uuid, ts, fkind, value in getattr(facts, "friction", None) or []:
            if not isinstance(ts, str) or not ts:
                continue
            value = _ms(value)
            key = _sid(uuid) or (ts, fkind, value, sid)
            if key in seen_friction:
                continue
            seen_friction.add(key)
            friction_rows.append([_local_bucket(ts, tz=tz), sid, fkind, value])
    friction_rows.sort(key=lambda r: (r[0] or "", r[1], r[2]))
    now_dt = limits_mod._parse_iso(now_iso)
    limit_episodes = limits_mod.build_episodes(limit_events, request_times, now=now_dt)
    for ep in limit_episodes:
        ep["hit_day"] = _local_date(ep.get("hit_at"), tz)
    allowance = limits_mod.allowance_snapshot(limit_episodes, request_times, now=now_dt)

    reconciliation = [
        {"model": model, **{k: (round(v, 6) if isinstance(v, float) else v)
                            for k, v in agg.items()}}
        for model, agg in sorted(reconcile.items(), key=lambda kv: -kv[1]["cli_cost"])
    ]

    # Column 0 is now a 5-minute bucket; the summary still reports whole days.
    dates = [r[0][:10] for r in usage_rows if r[0]]
    return {
        "generated_at": now_iso,
        "base_bucket_minutes": BASE_BUCKET_MINUTES,
        "unknown_models": sorted(unknown_models),
        # When the rate table was last checked against the published prices.
        "pricing": {
            "checked": pricing.PRICES_CHECKED,
            "age_days": pricing.prices_age_days(),
            "stale_after_days": pricing.PRICES_STALE_AFTER_DAYS,
            "stale": pricing.prices_stale(),
        },
        "dims": {
            "projects": sorted(set(agent_projects.values())),
            "models": sorted(model_set, key=str),
            "skills": sorted(skill_set, key=str),
            "tools": sorted(tool_set, key=str),
            "agentTypes": sorted(str(t) for t in agent_type_set),
        },
        "summary": {
            # Forks/resumes are excluded: they are copies of a transcript already
            # counted, so including them inflates the session count.
            # A transcript with no requests (a session opened and closed, or a
            # stub) did no work either, so it is not a session; it still counts
            # in total_transcripts.
            "total_sessions": sum(1 for sid, info in main_sessions.items()
                                  if info["facts"].requests and sid not in derived_sessions),
            "total_transcripts": len(main_sessions),
            "derived_sessions": len(derived_sessions),
            "total_subagents": len(sub_agents),
            "fast_mode_requests": fast_requests,
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
        },
        "sessions": sessions_rows,
        "usage": usage_rows,
        "tools": tools_rows,
        "skillCalls": skill_calls_rows,
        "agents": agents_rows,
        "prs": prs_rows,
        "lines": lines_rows,
        "reconciliation": reconciliation,
        # [bucket, session_id, "retry"|"turn"|"compact", value]; see parser.FileFacts.friction
        "friction": friction_rows,
        "limits": limit_episodes,
        "allowance": allowance,
        # Usage-health measures; see health.build and docs/usage-health.md.
        "health": health,
    }
