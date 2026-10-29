"""mtime/size-keyed cache of parsed per-file facts, so a warm scan only
re-parses files that actually changed (e.g. the live session being written).

Stored as JSON rather than a pickle: a pickle cache makes a writable
`.cache/parsed.json` into arbitrary code execution on the next scan, which is a
poor trade for a tool whose whole premise is reading your own logs locally. The
payload is plain data, so JSON costs nothing but the Counter round-trip below.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import fields as dataclass_fields

from .parser import FACTS_VERSION, FileFacts

# Dataclass fields that are Counters on the way in and plain dicts in JSON.
_COUNTER_FIELDS = ("entrypoints", "versions", "branches", "permission_modes")
_FACTS_FIELDS = {f.name for f in dataclass_fields(FileFacts)}


def _facts_to_dict(facts: FileFacts) -> dict:
    return {f: getattr(facts, f) for f in _FACTS_FIELDS}


def _facts_from_dict(d: dict) -> FileFacts:
    kwargs = {k: v for k, v in d.items() if k in _FACTS_FIELDS}
    for name in _COUNTER_FIELDS:
        kwargs[name] = Counter(kwargs.get(name) or {})
    return FileFacts(**kwargs)


class FileCache:
    def __init__(self, path=".cache/parsed.json"):
        self.path = path
        self._store = {}  # abspath -> (mtime_ns, size, facts)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("facts_version") != FACTS_VERSION:
                self._store = {}  # written by an older build -- re-parse from scratch
                return
            self._store = {
                path: (entry["mtime_ns"], entry["size"], _facts_from_dict(entry["facts"]))
                for path, entry in raw["entries"].items()
            }
        except Exception:
            # Any read/decode failure (missing file, corrupt data, or a stale
            # cache written by a since-refactored module layout) just means a
            # cold start -- never let a bad cache file crash the scan.
            self._store = {}

    def save(self) -> bool:
        """Write the cache; False if it couldn't be. Never raises: a cache that
        didn't save only means the next scan re-parses more.

        serve.py and collect.py can save the same file at once, so each save
        writes its own temp file next to the cache and renames it into place --
        the rename is atomic, and the last one to land wins whole."""
        payload = {
            "facts_version": FACTS_VERSION,
            "entries": {
                path: {"mtime_ns": mtime_ns, "size": size, "facts": _facts_to_dict(facts)}
                for path, (mtime_ns, size, facts) in self._store.items()
            },
        }
        folder = os.path.dirname(self.path) or "."
        tmp = None
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(self.path) + ".",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            return True
        except OSError:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False

    def get(self, abspath: str, mtime_ns: int, size: int):
        entry = self._store.get(abspath)
        if entry and entry[0] == mtime_ns and entry[1] == size:
            return entry[2]
        return None

    def put(self, abspath: str, mtime_ns: int, size: int, facts):
        self._store[abspath] = (mtime_ns, size, facts)

    def forget(self, abspath: str):
        self._store.pop(abspath, None)

    def prune(self, live_paths: set):
        """Drop cache entries for files that no longer exist on disk."""
        for path in list(self._store.keys()):
            if path not in live_paths:
                del self._store[path]

    def get_or_parse(self, abspath: str, kind: str, parse_fn):
        try:
            st = os.stat(abspath)
        except OSError:
            return None, False
        cached = self.get(abspath, st.st_mtime_ns, st.st_size)
        if cached is not None:
            return cached, True
        facts = parse_fn(abspath, kind)
        if facts is None:
            # Unreadable right now. Storing it would pin "empty" to a valid
            # mtime/size that fixing the permissions doesn't change.
            return None, False
        self.put(abspath, st.st_mtime_ns, st.st_size, facts)
        return facts, False
