"""Discover Claude Code log files and turn them into the dashboard payload."""
from __future__ import annotations

import os
import sys
import time

from .aggregate import build_payload
from .cache import FileCache
from .context import history_coverage, now_in
from .parser import parse_file, parse_meta

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")

# Resolved against the repo, not the caller's cwd, so `collect.py` run from
# anywhere shares one cache with `serve.py` instead of cold-starting every time.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE_PATH = os.path.join(_REPO_ROOT, ".cache", "parsed.json")

# Per-project sibling directories that are not session transcripts. These sit
# *inside* a project folder (e.g. `<project>/memory/`), not beside it, so they
# have to be skipped in the inner walk.
IGNORED_SUBDIRS = {"memory"}


def default_cache() -> FileCache:
    return FileCache(DEFAULT_CACHE_PATH)


def discover(root=DEFAULT_ROOT):
    """
    main_files: [(session_id, folder_name, abspath)]
    sub_files:  [(session_id, folder_name, agent_id, abspath, meta_path_or_None)]
    """
    main_files = []
    sub_files = []
    if not os.path.isdir(root):
        return main_files, sub_files

    try:
        folder_names = sorted(os.listdir(root))
    except OSError:
        return main_files, sub_files
    for folder_name in folder_names:
        folder_path = os.path.join(root, folder_name)
        if not os.path.isdir(folder_path):
            continue
        try:
            entries = sorted(os.listdir(folder_path))
        except OSError:
            continue  # one unreadable project folder must not hide the others
        for entry in entries:
            entry_path = os.path.join(folder_path, entry)
            if entry.endswith(".jsonl") and os.path.isfile(entry_path):
                main_files.append((entry[: -len(".jsonl")], folder_name, entry_path))
            elif os.path.isdir(entry_path) and entry not in IGNORED_SUBDIRS:
                sub_dir = os.path.join(entry_path, "subagents")
                if not os.path.isdir(sub_dir):
                    continue
                session_id = entry
                # Walked, not listed: newer Claude Code writes Workflow-agent
                # transcripts in a subfolder (subagents/workflows/), and reading
                # only the top level silently dropped their usage. Any .jsonl
                # is a transcript; its meta file sits beside it.
                for dirpath, dirnames, filenames in os.walk(sub_dir):
                    dirnames.sort()
                    for sub_entry in sorted(filenames):
                        if not sub_entry.endswith(".jsonl"):
                            continue
                        stem = sub_entry[: -len(".jsonl")]
                        agent_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
                        meta_path = os.path.join(dirpath, f"{stem}.meta.json")
                        sub_files.append((
                            session_id, folder_name, agent_id, os.path.join(dirpath, sub_entry),
                            meta_path if os.path.isfile(meta_path) else None,
                        ))
    return main_files, sub_files


def _load(cache: FileCache, path: str, kind: str, force: bool):
    """(facts, was_cached), or (None, False) for a file that cannot be loaded.

    A backstop: the parser sanitises what it reads, so an exception here is
    unexpected, and losing one file beats losing the whole scan."""
    try:
        if force:
            cache.forget(path)
        return cache.get_or_parse(path, kind, parse_file)
    except Exception as exc:  # noqa: BLE001
        print(f"skipped {path}: {exc}", file=sys.stderr)
        return None, False


def _gather(root, cache: FileCache, force: bool):
    """Load every transcript (from the cache when it is unchanged) and its
    subagent meta. Returns (main_sessions, sub_agents, files_parsed, files_cached)."""
    main_files, sub_files = discover(root)
    live_paths = {p for _, _, p in main_files} | {p for _, _, _, p, _ in sub_files}

    # The same session id can sit in two project folders (a copied or moved
    # project). The first keeps the bare id, so a log set with no repeat is
    # keyed exactly as before; a later one gets "<id>@<folder>" so it does not
    # overwrite the first. `key_of` ties a subagent to the parent in its own
    # folder, and each entry keeps the real id for display.
    main_sessions = {}
    key_of = {}  # (folder, session id) -> key
    taken = set()  # every key handed out, so a generated one is never reused

    def claim(session_id, folder_name):
        key = session_id
        if key in taken:
            key = f"{session_id}@{folder_name}"
            n = 1
            while key in taken:  # a transcript may be literally named like a key
                n += 1
                key = f"{session_id}@{folder_name}#{n}"
        taken.add(key)
        return key

    files_parsed = files_cached = 0
    for session_id, folder_name, path in main_files:
        facts, was_cached = _load(cache, path, "main", force)
        if facts is None:
            continue
        files_cached += 1 if was_cached else 0
        files_parsed += 0 if was_cached else 1
        key = claim(session_id, folder_name)
        key_of[(folder_name, session_id)] = key
        main_sessions[key] = {"facts": facts, "folder": folder_name, "session_id": session_id}

    sub_agents = []
    for session_id, folder_name, agent_id, path, meta_path in sub_files:
        facts, was_cached = _load(cache, path, "sub", force)
        if facts is None:
            continue
        files_cached += 1 if was_cached else 0
        files_parsed += 0 if was_cached else 1
        try:
            meta = parse_meta(meta_path) if meta_path else {}
        except Exception as exc:  # noqa: BLE001
            print(f"skipped {meta_path}: {exc}", file=sys.stderr)
            meta = {}
        parent = key_of.get((folder_name, session_id))
        if parent is None:
            # No transcript for the parent in this folder. Give it a key of its
            # own, so it stays off another folder's session sharing the id.
            parent = key_of[(folder_name, session_id)] = claim(session_id, folder_name)
        sub_agents.append({"session_id": parent, "real_session_id": session_id, "folder": folder_name, "agent_id": agent_id, "facts": facts, "meta": meta})

    cache.prune(live_paths)
    cache.save()
    return main_sessions, sub_agents, files_parsed, files_cached


def scan(root=DEFAULT_ROOT, cache: FileCache = None, force: bool = False, tz=None) -> dict:
    """Read every transcript and build the payload.

    `tz` is the zone days and buckets are read in: None (the default) is this
    machine's zone, for the personal dashboard; an export passes its export
    zone. The parse cache holds facts, never buckets, so scanning again in
    another zone re-reads nothing.
    """
    start = time.time()
    if cache is None:
        cache = default_cache()

    main_sessions, sub_agents, files_parsed, files_cached = _gather(root, cache, force)
    now_iso = now_in(tz).isoformat()
    try:
        payload = build_payload(main_sessions, sub_agents, now_iso, tz)
    except Exception as exc:  # noqa: BLE001
        # The cache can hold facts the parser would no longer write (a hand
        # edit, a damaged file): they pass the version check but not the
        # aggregation. Re-read every file, replacing the cache, and try once more.
        print(f"cached facts could not be aggregated ({exc.__class__.__name__}: {exc}); "
              "re-parsing every file", file=sys.stderr)
        main_sessions, sub_agents, files_parsed, files_cached = _gather(root, cache, True)
        payload = build_payload(main_sessions, sub_agents, now_iso, tz)
    payload["scan_ms"] = round((time.time() - start) * 1000)
    payload["files_parsed"] = files_parsed
    payload["files_cached"] = files_cached
    payload["history"] = history_coverage(payload, tz=tz)
    return payload
