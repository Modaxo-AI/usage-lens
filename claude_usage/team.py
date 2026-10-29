"""Merge many people's exports into one team view.

Each person runs the tool on their own machine and sends their export zip. A
manager drops the zips (or loose CSVs) into one folder and either:

* ``python3 collect.py --merge ./exports``  -> combined CSVs + a merge report
* ``python3 serve.py --team ./exports``      -> the team dashboard

Combining is where the export's provenance columns pay off. People re-export
"to be safe", run different versions, and export different windows; stacking
the files naively double-counts and misaligns. So this module:

* identifies a person by Claude account email (falling back to git email, and
  learning the mapping between the two from any file that carries both);
* de-duplicates rows on their natural keys, but only among exports from the
  same machine: a person's two machines add up. Weekly rows come from the
  exports that cover a week: ones covering different days of it add up (an
  export to Wednesday, the next from Thursday), and of ones covering the same
  days the one with complete CLI history, then more of the week, then the
  newer, is kept;
* aligns columns across schema versions (missing columns read blank);
* reports everything it had to resolve, rather than resolving it silently.

Reads files in memory only; nothing in a zip is ever extracted to disk.
"""
from __future__ import annotations

import csv
import datetime as _dt
import functools
import io
import json
import math
import os
import re
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Mapping

from . import context
from .export import csv_safe

KINDS = ("summary", "sessions", "breakdown", "limits", "prs", "tools")
UNKNOWN_PROJECT = "(unknown project)"  # what the exporter writes for spend with no folder (aggregate.py)
_NAME_RE = re.compile(r"^claude-usage-(summary|sessions|breakdown|limits|prs|tools)-(.+)-(\d{8})\.csv$")
SEAT_PRICE_FILE = "seat-prices.json"
# Zips come from colleagues, so a hostile or broken one must cost little and
# stop nothing. Real exports are a few MB across five files.
_MAX_EXPORT_BYTES = 64 * 1024 * 1024  # all of one zip's export CSVs, uncompressed
_MAX_TOTAL_BYTES = 256 * 1024 * 1024  # every zip and CSV read in one run, uncompressed
_MAX_MEMBERS = 100                    # entries in one zip
_MAX_COLUMNS = 200                    # a real export's widest file has about a hundred
_MAX_ROWS = 200_000                   # in one CSV; a real one has a few thousand
# Bytes don't bound what a parsed file costs: a row of empty cells is a byte
# a cell on disk and eight bytes a cell in memory (and a zip squeezes it to
# nothing), so 200 columns by 200,000 rows of commas fits the byte caps in 40 MB
# and takes gigabytes once parsed. Parsed cells are therefore counted across the
# whole run. A real export is tens of thousands of cells, a heavy one a million or
# so, so this is room for a large team and about 200 MB at the most.
_MAX_TOTAL_CELLS = 20_000_000
# A row costs more than its cells (the row object and its tuple), so one counts for
# at least this many: a file of one-column rows can't get round the cap.
_MIN_ROW_CELLS = 16
# csv.reader parses a whole record before returning it, so a record with
# millions of commas would build millions of fields before the column check
# could run. A record is therefore cut off by length first. A real one is a
# few hundred characters (about a hundred columns; a project path is the longest cell), so 1 MiB
# is thousands of times that and still caps the fields at about a million.
_MAX_RECORD_CHARS = 1 << 20
_MAX_SHARED_CELLS = 100_000           # distinct short cells _read_csv keeps one copy of
# What a damaged zip raises: encrypted members (RuntimeError), unsupported
# compression (NotImplementedError), corrupt streams (zlib.error), and so on.
_READ_ERRORS = (OSError, EOFError, ValueError, RuntimeError, NotImplementedError,
                zipfile.BadZipFile, zlib.error, csv.Error)
_CHUNK = 64 * 1024  # a zip member is inflated this much at a time


# Zone names that all mean UTC (the tool itself folds "UTC", "Z" and "Etc/UTC"
# into "UTC"; --tz GMT is written as GMT).
_UTC_ZONE_NAMES = frozenset((
    "utc", "z", "etc/utc", "uct", "etc/uct", "zulu", "etc/zulu", "universal", "etc/universal",
    "gmt", "etc/gmt", "gmt0", "gmt+0", "gmt-0", "etc/gmt0", "etc/gmt+0", "etc/gmt-0",
    "greenwich", "etc/greenwich"))


class _Unusable(Exception):
    """A file that read fine but is not something to merge."""

# Natural key of a row in each file. Person is always prepended, and for the
# kinds in _PER_MACHINE so is the machine: two machines' rows for the same week
# and project are different work, not a re-export.
_ROW_KEYS = {
    "summary": ("week_start", "project"),
    "sessions": ("session_id",),
    "breakdown": ("week_start", "project", "model", "thread", "subagent_type"),
    "limits": ("limit_type", "resets_at", "hit_at"),
    "prs": ("pr_url",),
    "tools": ("week_start", "project", "tool"),
}
# Sessions and PRs need no machine: a session id, or a PR URL, seen in two
# exports is the same session or PR wherever it was exported from.
_PER_MACHINE = ("summary", "breakdown", "limits", "tools")

# What a file must hold to be merged. A file re-saved by Excel (dates turned
# into 9/30/2026, ";" for ","), cut down by hand, or made to cause trouble
# otherwise merges as a person with no usage, crashes the dashboard, or slips
# past the date checks that keep re-exports from double counting. So each file
# is checked as it is read, and one that fails is skipped whole.
_REQUIRED = {kind: keys + (("est_cost_usd",) if kind in ("summary", "sessions", "breakdown") else ())
             for kind, keys in _ROW_KEYS.items()}
_DATES = ("week_start", "window_from", "window_to", "last_active_date", "cli_history_from",
          "opened_on")
_TIMESTAMPS = ("exported_at", "started_at", "ended_at", "hit_at", "last_notice_at", "resets_at",
               "resumed_at", "allowance_week_start", "projected_limit_hit_at")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
                           re.ASCII)
_MAX_NUMBER = 1e15  # far past any real cost or token count, far short of overflowing a sum


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

class Export:
    """One person's export: the files from one zip, or one set of loose CSVs."""

    def __init__(self, source, stamp):
        self.source = source
        self.stamp = stamp
        self.files = {}    # kind -> [_Row]
        self.columns = {}  # kind -> [header]

    def _first(self, column):
        for kind in KINDS:
            for row in self.files.get(kind, ()):
                value = row.get(column)
                if value not in (None, ""):
                    return value
        return None

    @property
    def schema_version(self):
        value = self._first("schema_version")
        try:
            return int(value) if value else 1
        except ValueError:
            return 1

    @property
    def exported_at(self):
        # Schema 1 files have no timestamp; their filename's date is the best
        # available, and sorts correctly against full timestamps.
        return self._first("exported_at") or f"{self.stamp[:4]}-{self.stamp[4:6]}-{self.stamp[6:]}"

    @property
    def claude_email(self):
        return (self._first("claude_account_email") or "").strip().lower() or None

    @property
    def has_claude_column(self):
        """Whether this export could have recorded a Claude account email (schema 2
        and later). Blank there means no OAuth login; absent means it predates it."""
        return any("claude_account_email" in cols for cols in self.columns.values())

    @property
    def machine_id(self):
        return (self._first("machine_id") or "").strip() or None

    @property
    def bucket_zone(self):
        """The zone this export's days and weeks were bucketed in (schema 8's
        `bucket_zone`), or None for a legacy export: older files, and any with
        the cell blank, were bucketed in the exporting machine's own zone. The
        names the tz database keeps for UTC (GMT, Etc/GMT, UCT, Zulu...) read as
        "UTC": they cut days at the same midnight, and --tz GMT is no
        disagreement with the default."""
        zone = (self._first("bucket_zone") or "").strip() or None
        return "UTC" if zone and zone.lower() in _UTC_ZONE_NAMES else zone

    @property
    def git_email(self):
        return (self._first("person_email") or "").strip().lower() or None

    def context(self, column):
        return self._first(column)

    @functools.cached_property
    def sessions_started(self):
        """{session id: start day} from the sessions file."""
        return {r["session_id"]: (r.get("started_at") or "")[:10]
                for r in self.files.get("sessions", ()) if r.get("session_id")}

    @functools.cached_property
    def sessions_by_week(self):
        """{week start: {session id: start day}}, so a week's sessions can be
        compared without walking every session in the file."""
        out = defaultdict(dict)
        for sid, day in self.sessions_started.items():
            try:
                out[_monday(day)][sid] = day
            except ValueError:
                continue
        return out

    @functools.cached_property
    def sessions_by_active_week(self):
        """{week start: [session row]} for every week a session is active in,
        from its start to its end day -- a session can run across Sunday night."""
        out = defaultdict(list)
        for row in self.files.get("sessions", ()):
            start = (row.get("started_at") or "")[:10]
            end = (row.get("ended_at") or "")[:10] or start
            try:
                first, last = _monday(start), _monday(max(start, end))
            except ValueError:
                continue
            week = _dt.date.fromisoformat(first)
            while week.isoformat() <= last:
                out[week.isoformat()].append(row)
                week += _dt.timedelta(days=7)
        return out

    @functools.cached_property
    def sessions_by_start(self):
        """{(start instant, project): [session row]}: how a resumed, forked or
        copied session is found from its original. The start is keyed by its
        UTC instant (see `_instant`), since each export writes it in its own
        machine's local offset."""
        out = defaultdict(list)
        for row in self.files.get("sessions", ()):
            if row.get("started_at"):
                out[(_instant(row.get("started_at")), row.get("project"))].append(row)
        return out


def _instant(ts):
    """A timestamp as the UTC instant it names, so the same moment written in
    two machines' local offsets compares equal. A timestamp that doesn't parse
    stays as the raw string, which still matches itself; one with no offset
    is kept as written."""
    if not ts:
        return ts
    try:
        moment = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ts
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_dt.timezone.utc)


def _later(a, b):
    """Whether timestamp `a` is after `b` (ISO, any offset; unparseable counts
    as not later)."""
    a, b = _instant(a), _instant(b)
    if not (isinstance(a, _dt.datetime) and isinstance(b, _dt.datetime)):
        return False
    try:
        return a > b
    except TypeError:  # an offset on one side only
        return False


def _is_date(value, pattern):
    if not pattern.fullmatch(value):
        return False
    try:
        _dt.date.fromisoformat(value[:10])
    except ValueError:
        return False
    return True


def _is_number(value):
    try:
        number = float(value)
    except ValueError:
        return False
    return math.isfinite(number) and abs(number) <= _MAX_NUMBER


def _is_count_or_amount(value):
    """A number that is never below zero: a cost, a token count, a tally."""
    return _is_number(value) and float(value) >= 0


def _is_monday(value):
    """A YYYY-MM-DD date that is a Monday: every week_start the tool writes is."""
    return _is_date(value, _DATE_RE) and _dt.date.fromisoformat(value).weekday() == 0


class _Row(Mapping):
    """One CSV row, read-only like a dict. It holds its cells as a tuple and
    shares the file's header with every other row, so a row costs about what
    its cells do -- a dict per row would cost several times that. A cell the
    line left out is None, as csv.DictReader gives."""
    __slots__ = ("_index", "_cells")

    def __init__(self, index, cells):
        self._index = index  # {column: position}, the same dict for every row of a file
        self._cells = cells

    def __getitem__(self, column):
        return self._cells[self._index[column]]

    def get(self, column, default=None):
        position = self._index.get(column)
        return default if position is None else self._cells[position]

    def __iter__(self):
        return iter(self._index)

    def __len__(self):
        return len(self._index)


def _records(stream):
    """The cells of each CSV record in the text `stream`, read a line at a
    time. A record (a line, or several if a quoted cell holds newlines) over
    _MAX_RECORD_CHARS raises _Unusable before csv.reader parses it, and without
    reading more of it than that."""
    used = 0

    def lines():
        nonlocal used
        while True:
            room = _MAX_RECORD_CHARS - used
            line = stream.readline(room + 1)
            if not line:
                return
            used += len(line)
            if len(line) > room:
                raise _Unusable(f"a line or quoted cell over {_MAX_RECORD_CHARS:,} characters, "
                                "far more than an export has")
            yield line

    reader = csv.reader(lines())
    while True:
        used = 0
        try:
            yield next(reader)
        except StopIteration:
            return


def _read_csv(kind, stream, budget=None):
    """(columns, rows) of one export CSV of `kind`, read from the text `stream`
    a line at a time; _Unusable if it lacks the columns the merge keys on,
    holds a date or number it can't use, or is past the column, row or
    record-length limit, or (given a `budget`) takes the run past its cell limit."""
    reader = _records(stream)
    columns = next(reader, [])
    if len(columns) > _MAX_COLUMNS:
        raise _Unusable(f"{len(columns):,} columns, more than the {_MAX_COLUMNS} an export can have")
    missing = [c for c in _REQUIRED[kind] if c not in columns]
    if missing:
        raise _Unusable(f"lacks columns {', '.join(missing)} (not an export, or re-saved with "
                        "another delimiter)")
    index = {c: i for i, c in enumerate(columns)}
    checks = [(i, c, _is_monday, "not a Monday (YYYY-MM-DD)") if c == "week_start" else
              (i, c, lambda v: _is_date(v, _DATE_RE), "not a YYYY-MM-DD date")
              for c, i in index.items() if c in _DATES]
    checks += [(i, c, lambda v: _is_date(v, _TIMESTAMP_RE), "not an ISO date and time")
               for c, i in index.items() if c in _TIMESTAMPS]
    checks += [(i, c, _is_number, "not a number") if c in _SIGNED else
               (i, c, _is_count_or_amount, "negative or not a number")
               for c, i in index.items() if c in _NUMBERS]
    width = len(columns)
    row_cells = max(width, _MIN_ROW_CELLS)
    rows = []
    seen = {}  # cells already read: one copy of "0" or a project name serves every row
    for line, cells in enumerate(reader, start=2):  # line 1 is the header
        if not cells:  # a blank line
            continue
        if len(rows) >= _MAX_ROWS:
            raise _Unusable(f"more than {_MAX_ROWS:,} rows, far more than an export has")
        if len(cells) > width:
            raise _Unusable(f"line {line} has {len(cells)} fields under a header of {width} "
                            "(more fields than columns)")
        if budget is not None:
            budget.take_cells(row_cells)
        if len(cells) < width:
            cells += [None] * (width - len(cells))
        for i, column, ok, what in checks:
            value = cells[i] or ""
            if value == "" and column != "week_start":
                continue
            if not ok(value):
                raise _Unusable(f"{column} on line {line} is {value[:30]!r}, {what} "
                                "(re-saved in Excel, or edited?)")
        share = seen.setdefault if len(seen) < _MAX_SHARED_CELLS else seen.get
        cells = [share(c, c) if c is not None and len(c) <= 24 else c for c in cells]
        rows.append(_Row(index, tuple(cells)))
    return columns, rows


def _is_finder_metadata(name):
    """macOS Finder's "Compress" adds a __MACOSX/ folder of "._" files, one
    per member and named like it."""
    return "__MACOSX" in name.split("/") or os.path.basename(name).startswith("._")


class _Capped(io.RawIOBase):
    """A zip member's inflated bytes, read a chunk at a time and never past its
    declared size: a member that claims to be small is stopped as soon as it
    isn't, rather than inflated in one go."""

    def __init__(self, fh, info):
        self._fh, self._info, self._seen = fh, info, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self._fh.read(min(len(buffer), _CHUNK))
        self._seen += len(chunk)
        if self._seen > self._info.file_size:
            raise _Unusable(f"{self._info.filename} inflates past its declared size")
        buffer[:len(chunk)] = chunk
        return len(chunk)


class _Budget:
    """What a run may still read, across every zip and loose file, since what is
    parsed stays in memory until the merge is done: uncompressed bytes of export
    CSVs, and parsed cells (rows times columns), which bytes don't bound."""

    def __init__(self):
        self.left = _MAX_TOTAL_BYTES
        self.cells = _MAX_TOTAL_CELLS

    def take_cells(self, count):
        if count > self.cells:
            raise _Unusable(f"it would take the run past {_MAX_TOTAL_CELLS:,} cells (rows times "
                            "columns) in all the CSVs read at once, far more than exports have; "
                            "mostly empty columns cost memory as much as full ones")
        self.cells -= count

    def take(self, size):
        if size > self.left:
            raise _Unusable(f"{size / 2**20:,.0f} MiB more of CSVs would pass the "
                            f"{_MAX_TOTAL_BYTES // 2**20} MiB limit on everything read in one run")
        self.left -= size


def _text(stream):
    return io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline="")


def _zip_members(path, budget):
    """Yield (member name, text stream) for each export CSV in one zip, to be
    read before asking for the next. Members are checked by name and declared
    size, and the size is taken from `budget`, before anything is decompressed;
    a member never inflates past its declared size, so a small hostile zip can't
    use much memory. Nothing is held whole: the caller reads each stream a line
    at a time."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_MEMBERS:
            raise _Unusable(f"{len(infos)} entries, far more than an export has")
        wanted = [i for i in infos
                  if not i.is_dir() and not _is_finder_metadata(i.filename)
                  and _NAME_RE.match(os.path.basename(i.filename))]
        size = sum(i.file_size for i in wanted)
        if size > _MAX_EXPORT_BYTES:
            raise _Unusable(f"too large to be an export ({size / 1e6:,.0f} MB uncompressed)")
        budget.take(size)
        for info in wanted:
            with archive.open(info) as fh, _text(io.BufferedReader(_Capped(fh, info))) as stream:
                yield info.filename, stream


class _Problems(list):
    """The human-readable problem strings of a read, plus `skipped`: one
    {"source", "person", "reason"} per export that was left out whole, so the
    team page can list it next to the exports it merged."""

    def __init__(self, *args):
        super().__init__(*args)
        self.skipped = []


_FIX = ("re-export it with the current tool (or save the CSVs as comma-separated UTF-8, not "
        "semicolons or dates re-formatted by Excel)")


def read_exports(paths):
    """Every export found under `paths` (files or folders). Returns
    (exports, problems) where problems are human-readable strings (and
    `problems.skipped` lists the exports left out whole).

    An export with a file that can't be read, or fails the checks in
    _read_csv, is skipped whole, never half-merged. Files are parsed a line at
    a time, and what is read across all of them stays inside _MAX_TOTAL_BYTES
    of CSV and _MAX_TOTAL_CELLS cells, and _MAX_ROWS rows and _MAX_COLUMNS columns a
    file; one past a limit is skipped like any other bad file."""
    budget = _Budget()
    groups = {}
    problems = _Problems()
    rejected = {}           # loose CSVs' group key -> label, for a group with a bad file
    loose_files = Counter()  # loose CSVs' group key -> files seen
    loose_reasons = defaultdict(list)  # loose CSVs' group key -> why each bad file was skipped

    def key_of(source, member):
        # Grouped by the file's full path and its folder inside a zip: two
        # folders can each hold an export of the same name (one per machine),
        # and they are two exports.
        kind, slug, stamp = _NAME_RE.match(os.path.basename(member)).groups()
        return kind, (source, os.path.dirname(member), slug, stamp)

    def add(key, label, kind, columns, rows):
        export = groups.get(key)
        if export is None:
            export = groups[key] = Export(label, key[3])
        export.columns[kind] = columns
        export.files[kind] = rows

    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, names in os.walk(path):
                files.extend((os.path.join(root, n), os.path.relpath(os.path.join(root, n), path))
                             for n in sorted(names))
        elif os.path.exists(path):
            files.append((path, os.path.basename(path)))
        else:
            problems.append(f"Not found: {path}")

    for path, label in sorted(files):
        name = os.path.basename(path)
        loose = None
        before = (budget.left, budget.cells)  # a file that is skipped frees what it took
        try:
            if name.lower().endswith(".zip"):
                # Every member is read and checked before any is added.
                parsed = []
                for member, stream in _zip_members(path, budget):
                    kind, key = key_of(os.path.abspath(path), member)
                    try:
                        columns, rows = _read_csv(kind, stream, budget)
                    except _Unusable as exc:
                        raise _Unusable(f"{os.path.basename(member)}: {exc}") from None
                    folder = os.path.dirname(member)
                    parsed.append((key, f"{label} ({folder})" if folder else label, kind, columns, rows))
                if not parsed:
                    problems.append(f"{label}: no Claude Code usage CSVs inside, skipped")
                for item in parsed:
                    add(*item)
            elif name.lower().endswith(".csv"):
                if not _NAME_RE.match(name):
                    problems.append(f"{label}: not named like an export file, skipped")
                    continue
                folder = os.path.dirname(path) or "."
                kind, loose = key_of(os.path.abspath(folder), name)
                loose_files[loose] += 1
                size = os.path.getsize(path)
                if size > _MAX_EXPORT_BYTES:
                    raise _Unusable("too large to be an export")
                budget.take(size)
                with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
                    columns, rows = _read_csv(kind, fh, budget)
                if loose not in rejected:
                    add(loose, f"{folder} ({loose[2]}, {loose[3]})", kind, columns, rows)
            continue
        except _Unusable as exc:
            reason = f"{exc}"
            budget.left, budget.cells = before
        except _READ_ERRORS as exc:
            reason = f"could not be read ({type(exc).__name__}: {exc})"
            budget.left, budget.cells = before
        if loose:
            # One notice per export, not one per file: a person's six Excel-saved
            # CSVs are one problem. Reported after the loop, with the fix.
            rejected[loose] = f"{os.path.dirname(path) or '.'} ({loose[2]}, {loose[3]})"
            loose_reasons[loose].append(f"{name}: {reason}")
        else:
            problems.append(f"{label}: {reason}, skipped. The export was left out of the merge: {_FIX}.")
            problems.skipped.append({"source": label, "person": os.path.splitext(name)[0], "reason": reason})
    for key, label in rejected.items():
        groups.pop(key, None)
        reasons = loose_reasons[key]
        more = f" ({len(reasons) - 1} more of its files failed too)" if len(reasons) > 1 else ""
        other = loose_files[key] - len(reasons)
        rest = f"; its {other} other file(s) were left out with it so it isn't half-merged" if other > 0 else ""
        problems.append(f"{label}: skipped. {reasons[0]}{more}{rest}. The whole export was left out of the "
                        f"merge, so {key[2]} is missing from the team views: {_FIX}.")
        problems.skipped.append({"source": label, "person": key[2], "reason": reasons[0]})
    return list(groups.values()), problems


# --------------------------------------------------------------------------- #
# seat prices
# --------------------------------------------------------------------------- #

_PER_DAY = {"month": 365.25 / 12, "year": 365.25}


def load_seat_prices(paths, explicit=None):
    """Seat prices for value-per-seat, or (None, []) when none are configured.

    Read from `explicit`, else from a seat-prices.json in any folder given, or
    next to any file given.
    Prices are each organisation's contract terms, so they are never guessed: without the
    file, the value columns stay blank and the dashboard says why.

        {"currency": "USD", "per": "month",
         "prices": {"default_claude_max_5x": 100, "enterprise_tier_1": 50, "default": 50}}

    (Example prices; use your own.)

    A person is priced by their rate-limit tier, then their seat tier, then
    "default" -- the rate-limit tier is the more specific of the two. An
    Enterprise seat (a seat tier named enterprise_*) is the exception: its
    seat tier comes first, since the rate-limit tier that rides along with it
    (a consumer Max tier) is not what the organisation pays for.
    """
    candidates = [explicit] if explicit else list(dict.fromkeys(
        os.path.join(p if os.path.isdir(p) else os.path.dirname(os.path.abspath(p)), SEAT_PRICE_FILE)
        for p in paths))
    for path in candidates:
        if not path or not os.path.exists(path):
            if explicit:
                return None, [f"Seat price file not found: {explicit}"]
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            per = raw.get("per", "month")
            if per not in _PER_DAY:
                raise ValueError(f'"per" must be "month" or "year", got {per!r}')
            # `true` would read as $1, and a 401-digit int overflows float().
            if any(isinstance(v, bool) for v in (raw.get("prices") or {}).values()):
                raise ValueError('"prices" must map tiers to numbers, not true/false')
            # Usage is priced in USD, so a price in anything else would be divided
            # into dollars as if it were one: refused, not converted (no rate to trust).
            currency = raw.get("currency", "USD")
            if not isinstance(currency, str) or currency.strip().upper() != "USD":
                raise ValueError(f"prices must be in USD, the currency usage is priced in; the file says "
                                 f"{currency!r}. Convert them to USD and set \"currency\": \"USD\"")
            prices = {str(k): float(v) for k, v in (raw.get("prices") or {}).items()}
            # Zero is rejected too: it is what the documented template ships
            # with, and an unedited template must not yield a value multiple. So
            # is anything under a cent, which is not a price.
            if not prices or any(not (math.isfinite(v) and v >= 0.01) for v in prices.values()):
                raise ValueError('"prices" must map tiers to numbers of at least 0.01 -- '
                                 'replace the template\'s zeros with real prices')
            return {"currency": "USD", "per": per, "prices": prices,
                    "source": os.path.basename(path)}, []
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            return None, [f"{os.path.basename(path)}: could not be used ({exc})"]
    return None, []


def _seat_price(person, prices):
    if not prices:
        return None
    seat = person.get("seat_tier")
    keys = [person.get("rate_limit_tier"), seat, "default"]
    if seat and str(seat).startswith("enterprise"):
        # The rate-limit tier of an enterprise seat is a consumer plan's name: its
        # price is not what the enterprise seat costs, so it is never used for one.
        keys = [seat, "default"]
    for key in keys:
        if key and key in prices["prices"]:
            return prices["prices"][key]
    return None


def apply_seat_value(people, prices):
    """Value per seat: list-price usage against what the seat cost plus extra
    usage, over the days each person's exports cover. A multiple above 1 means the seat
    delivered more API-equivalent usage than it cost."""
    for p in people:
        price = _seat_price(p, prices)
        p["seat_price"] = price
        p["seat_cost_in_window"] = None
        p["value_multiple"] = None
        if price is None or not (p.get("window_from") and p.get("window_to")):
            continue
        if p.get("no_usage_window"):
            # An export from a machine with no logs holds no usage: 0.00x says nothing about the seat.
            days = p.get("window_days")
            if days is not None:
                p["seat_cost_in_window"] = round(price / _PER_DAY[prices["per"]] * max(days, 0), 2)
            continue
        # The days the exports cover, not min(from)..max(to): the days between two
        # exports are neither counted nor paid for (the dashboard does the same).
        days = p.get("window_days")
        if days is None:
            days = (_dt.date.fromisoformat(p["window_to"]) - _dt.date.fromisoformat(p["window_from"])).days + 1
        seat_cost = price / _PER_DAY[prices["per"]] * max(days, 0)
        paid = seat_cost + (p.get("extra_usage_usd") or 0.0)
        p["seat_cost_in_window"] = round(seat_cost, 2)
        p["value_multiple"] = round(p["est_cost_usd"] / paid, 2) if paid else None


# --------------------------------------------------------------------------- #
# merging
# --------------------------------------------------------------------------- #

def _num(value, default=0.0):
    try:
        number = float(value) if value not in (None, "") else default
    except ValueError:
        return default
    # NaN and infinity would break the sums, and the page's JSON.
    return default if number is not None and not math.isfinite(number) else number


def _person_keys(exports):
    """{export: person key}. Claude account email where known; a git email is
    mapped to the Claude email whenever any file shows the two together, so a
    person's old (schema 1) and new exports land on one person.

    An export that predates the Claude email column is matched this way. A
    current export with a blank Claude email was made without an OAuth login
    (API key, Bedrock, Vertex), and its git email may be shared by other
    people, so it is matched only when it provably holds the same logs: it
    shares a machine id, or a session, with that account's exports (someone
    who switched one machine to an API key). Otherwise it stays under its git
    email. A git email seen with two Claude accounts matches neither."""
    accounts = defaultdict(set)
    holders = defaultdict(list)
    for e in exports:
        if e.claude_email:
            holders[e.claude_email].append(e)
            if e.git_email:
                accounts[e.git_email].add(e.claude_email)

    def same_logs(e, account):
        return any((e.machine_id and e.machine_id == o.machine_id)
                   or not e.sessions_started.keys().isdisjoint(o.sessions_started)
                   for o in holders[account])

    keys = {}
    for e in exports:
        matched = None
        if not e.claude_email and len(accounts.get(e.git_email, ())) == 1:
            account = next(iter(accounts[e.git_email]))
            if not e.has_claude_column or same_logs(e, account):
                matched = account
        keys[id(e)] = e.claude_email or matched or e.git_email or e.source
    return keys


_AS_FLAG = "`python3 collect.py --export --as EMAIL`"


def _machine_count(group):
    """How many machines these exports come from: one per machine id, with the
    exports that have no id counted as one machine among themselves (they are
    not each their own), and ids that share a session counted as one -- the same
    logs under a new id, as in `_machine_groups`."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, e in enumerate(group):
        parent[find(("id", e.machine_id))] = find(("export", i))
        for sid in e.sessions_started:
            parent[find(("session", sid))] = find(("export", i))
    return len({find(("export", i)) for i in range(len(group))})


def _identity_notes(exports, keys):
    """Warnings for people the merge could not tell apart, or may have split.

    Without a Claude account email the only identity is the git email the
    export was stamped with. People on a team's common address share it, and
    with no git user.email the export falls back to the OS user name or
    "unknown" -- which it doesn't record, so a name with no "@" is taken to be
    that fallback. One note per identity, however many files it has, and only
    where the files come from more than one machine.
    """
    accounts = defaultdict(set)  # git email -> Claude accounts seen with it
    for e in exports:
        if e.claude_email and e.git_email:
            accounts[e.git_email].add(e.claude_email)
    claude_keys = {keys[id(e)] for e in exports if e.claude_email}
    groups = defaultdict(list)
    for e in exports:
        if not e.claude_email and keys[id(e)] not in claude_keys:
            groups[keys[id(e)]].append(e)
    notes = []
    for git_email, who in sorted(accounts.items()):
        if len(who) > 1:
            notes.append({"level": "warn", "message":
                f"Git email {git_email} appears with {len(who)} Claude accounts "
                f"({', '.join(sorted(who))}). They were kept as separate people, and an older "
                "export carrying only this git email was not matched to either. If any of them "
                f"are one person, re-export with {_AS_FLAG} (EMAIL being their Claude account "
                "email) so they join."})
    for key, group in sorted(groups.items()):
        # Kept apart from the one Claude account that used the git email (two
        # or more is the note above). An export under the account's own key
        # was joined to it, not kept apart.
        linked = sorted(accounts.get(key, ())) if len(accounts.get(key, ())) == 1 else []
        if not any(e.has_claude_column for e in group):
            linked = []
        # Re-exports from one machine are one person whatever the identity; it
        # takes exports from two machines (exports with no id count as one
        # machine among themselves) to be possibly two people.
        merged = _machine_count(group) > 1
        if merged and "@" not in key:
            notes.append({"level": "warn", "message":
                f"{len(group)} exports with no Claude account email were counted as one person, "
                f"\"{key}\". That isn't a Claude account email: it is an OS user name or "
                "\"unknown\" (what the export falls back to when git has no user.email) or a name "
                f"passed with --as, so different people can share it. Ask each to re-export "
                f"with {_AS_FLAG} (their own email), or to set "
                "`git config --global user.email`. Their limit episodes were not combined "
                "across machines."})
        elif merged:
            notes.append({"level": "warn", "message":
                f"{len(group)} exports with no Claude account email (an API key, Bedrock or "
                f"Vertex login) and the same git email, {key}, were counted as one person. "
                f"If they are different people, ask each to re-export with {_AS_FLAG} (their "
                "own email). Their limit episodes were not combined across machines."})
        if linked:
            # A split rather than a merge, so the machine doesn't matter.
            notes.append({"level": "warn", "message":
                f"{len(group)} export(s) with no Claude account email and git email {key} "
                f"were kept apart from the Claude account {linked[0]} that also used it, "
                "since a git email can be shared. If this is one person (an OAuth machine and "
                "an API-key machine), re-export on the API-key machine with "
                f"`python3 collect.py --export --as {linked[0]}` to join them."})
    return notes


def _machine_groups(exports, keys):
    """({export: machine label}, notes): which of a person's exports describe
    the same machine's logs, so re-exports de-duplicate and machines add up.

    Mostly the export's machine_id. Two refinements towards not double
    counting: exports that share a session are the same logs whatever their
    ids say (the tool was re-cloned, or ~/.claude is synced between machines);
    and an export with no machine id (schema 5 or older) that can't be tied to
    a machine by its sessions is merged with all of that person's exports, as
    every export was before machine ids existed. And one towards not losing
    work: exports with the same id where one lists sessions on days both cover
    that the other, a later export, lacks (see `_apart`) are two machines' logs (the tool's
    data folder was copied), so they add up rather than one replacing the other.
    A session the other export holds the continuation of (see `_continued`)
    doesn't count as one it lacks: resuming a session between two exports from
    one machine doesn't make them two machines.

    The reverse can't be settled from the files: exports with different ids and
    no shared session are added as two machines, but one machine whose data
    folder was deleted gets a new id and looks just like that, with its earlier
    export's weekly rows still holding work the later one's cover. Where the
    sessions don't show them to be different logs, the note says so (see
    `_possible_repeats`).
    """
    parent = {id(e): id(e) for e in exports}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_person = defaultdict(list)
    for e in exports:
        by_person[keys[id(e)]].append(e)
    notes = []
    for person, group in sorted(by_person.items()):
        seen = {}
        for e in group:
            for sid in e.sessions_started:
                if sid in seen:
                    union(id(e), seen[sid])
                else:
                    seen[sid] = id(e)
        for mid in sorted({e.machine_id for e in group if e.machine_id}):
            components = defaultdict(list)
            for e in group:
                components[find(id(e))].append(e)
            homes = []  # the machines found under this id
            for component in components.values():
                if not any(e.machine_id == mid for e in component):
                    continue
                home = next((h for h in homes
                             if not any(_apart(a, b) for a in h for b in component)), None)
                if home is None:
                    homes.append(component)
                else:
                    union(id(component[0]), id(home[0]))
                    home.extend(component)
            if len(homes) > 1:
                notes.append({"level": "warn", "message":
                    f"{person}: exports carrying the same machine id ({mid}) disagree about the "
                    "sessions on days they both cover (the older lists sessions that the newer, "
                    "covering those days, lacks), so they were counted as "
                    f"{len(homes)} machines and added together. The id is shared, so the tool's data "
                    "folder was probably copied or synced between machines: delete the `machine-id` "
                    "file in the data folder (~/.usage-lens, or wherever CLAUDE_USAGE_DATA_DIR or "
                    "--data-dir points) on all but one of them, then re-export, to give each its own id."})
        notes += _possible_repeats(person, group, find)
        ids_in = defaultdict(set)
        for e in group:
            if e.machine_id:
                ids_in[find(id(e))].add(e.machine_id)
        unplaced = [e for e in group if not ids_in.get(find(id(e)))]
        if unplaced and len(group) > 1:
            for e in group[1:]:
                union(id(e), id(group[0]))
            notes.append({"level": "warn", "message":
                f"{person}: {len(unplaced)} export(s) carry no machine id (made before schema 6), so "
                "all of this person's exports were de-duplicated against each other, as before machine "
                "ids existed. If they work on more than one machine, their summary and breakdown "
                "undercount until each machine re-exports with the current tool."})
        elif any(len(ids) > 1 for ids in ids_in.values()):
            notes.append({"level": "info", "message":
                f"{person}: exports with different machine ids share sessions, so they were treated "
                "as one machine (the tool was re-installed, or ~/.claude is synced between machines)."})

    members = defaultdict(set)
    for e in exports:
        members[find(id(e))].add(e.machine_id or "")
    labels = {id(e): "; ".join(sorted(m for m in members[find(id(e))] if m)) for e in exports}
    # Machines that share an id (a copied data folder) still need labels of their own.
    roots = defaultdict(list)
    for e in sorted(exports, key=lambda e: e.source):
        root = find(id(e))
        if root not in roots[(keys[id(e)], labels[id(e)])]:
            roots[(keys[id(e)], labels[id(e)])].append(root)
    for e in exports:
        same = roots[(keys[id(e)], labels[id(e)])]
        if len(same) > 1:
            labels[id(e)] += f" #{same.index(find(id(e))) + 1}"
    for person, group in sorted(by_person.items()):
        machines = {labels[id(e)] for e in group}
        if len(machines) > 1:
            notes.append({"level": "info", "message":
                f"{person}: exports from {len(machines)} machines. Their usage was added together"
                + ("; limit episodes seen on more than one machine were counted once."
                   if any(e.claude_email for e in group) else
                   ". With no Claude account, limit episodes were not combined across them.")})
    return labels, notes


def _weekly_cost(e):
    """{week: cost} from an export's summary, else its breakdown."""
    kind = "summary" if "summary" in e.files else "breakdown"
    out = defaultdict(float)
    for row in e.files.get(kind, ()):
        if row.get("week_start"):
            out[row["week_start"]] += _num(row.get("est_cost_usd"))
    return out


def _possible_repeats(person, group, find):
    """Notes for exports with different machine ids that share no session, whose
    windows overlap, and that both hold cost in a week they both cover days of,
    when the sessions don't show them to be different logs (see `_apart`).

    That is what two machines look like, and also what one machine looks like
    after its data folder was deleted (a new machine id) and a session that
    started before the later export's window is still running: the earlier
    export's weekly rows hold that session's cost on days the later one covers
    too. The weekly rows can't be split by day and the later export can't list
    the session, so the files can't say which it is. They are added (the other
    choice loses a machine's work), and the weeks are named so the manager can
    tell."""
    weeks, ids = set(), set()
    costs = {}
    for i, a in enumerate(group):
        for b in group[i + 1:]:
            if not (a.machine_id and b.machine_id) or a.machine_id == b.machine_id \
                    or find(id(a)) == find(id(b)):
                continue
            if not all(_window(a) + _window(b)) or not _overlaps(_window(a), _window(b)):
                continue
            costs.setdefault(id(a), _weekly_cost(a))
            costs.setdefault(id(b), _weekly_cost(b))
            shared = {week for week, cost in costs[id(a)].items()
                      if cost > _COST_TOLERANCE and costs[id(b)].get(week, 0.0) > _COST_TOLERANCE
                      and _days_of(week, _window(a)) & _days_of(week, _window(b))}
            if shared and not _apart(a, b):
                weeks |= shared
                ids |= {a.machine_id, b.machine_id}
    if not weeks:
        return []
    return [{"level": "warn", "message":
        f"{person}: exports with different machine ids ({', '.join(sorted(ids))}) cover overlapping "
        "days, share no session and are not shown to be different logs by their sessions, so they "
        f"were added together as separate machines, and the totals for week(s) {', '.join(sorted(weeks))} "
        "may double count if they are the same machine (deleting the tool's data folder gives a new "
        "machine id, and a session that began before the later export's window can't be matched). "
        "If so, drop the older export from the folder, or re-export from the day after the older "
        "one's window ends so the two don't overlap."}]


def _continued(row, e, c):
    """Whether export `c` holds the continuation of session `row` (from export
    `e`): another id with the original's start and project that either runs on
    past it (a resume or fork), or ends with it and costs at least as much,
    from a later export (a copy that owns the requests and so leaves the
    original unlisted). The original's work is then in `c` under that id."""
    for other in c.sessions_by_start.get((_instant(row.get("started_at")), row.get("project")), ()):
        if other.get("session_id") == row.get("session_id"):
            continue
        # The "ends later" test below can pair two unrelated sessions that
        # share a started_at (seconds precision) and project, and so merge two
        # exports with the same machine id into one machine. An exact
        # collision like that is accepted as negligible.
        if _later(other.get("ended_at"), row.get("ended_at")):
            return True
        if (_instant(other.get("ended_at")) == _instant(row.get("ended_at"))
                and _num(other.get("est_cost_usd")) >= _num(row.get("est_cost_usd"))
                and (c.context("window_to") or "") > (e.context("window_to") or "")):
            return True
    return False


def _apart(a, b):
    """Whether two exports are different logs: the older lists a session, started
    on days both windows cover, that the newer neither lists nor continues.
    (They share no session id, or they'd be grouped already.) An export made
    later that covers those days holds everything an earlier one listed there if
    it is the same logs, so a newer export with complete history that lacks the
    older one's sessions is from another machine, whether or not the older one
    lacks anything of the newer's -- and the work in it is not a re-export's to
    drop. Not the other way round: the morning's export lacks a session that
    began that afternoon, and the later export rightly has it. Two exports made
    at the same moment can't tell which is older, so each must lack one of the
    other's.

    A session the other export holds the continuation of isn't missing from it:
    one laptop exports, resumes a session (the original becomes a copy the
    sessions sheet leaves out) and exports again. Days before either's CLI
    history cut-off don't count -- missing sessions there were deleted, not
    never there. Exports in different time zones put a session near midnight on
    different days, so the first and last day both cover are left out for them."""
    (af, at), (bf, bt) = _window(a), _window(b)
    if not (af and at and bf and bt):
        return False
    lo = max([af, bf] + [e.context("cli_history_from") or "" for e in (a, b)
                         if e.context("cli_history_truncated") == "yes"])
    hi = min(at, bt)
    if _zones_differ(a, b):
        try:
            lo = (_dt.date.fromisoformat(lo) + _dt.timedelta(days=1)).isoformat()
            hi = (_dt.date.fromisoformat(hi) - _dt.timedelta(days=1)).isoformat()
        except ValueError:
            return False
    if lo > hi:
        return False

    def lacks(e, other):
        """Whether `e` lists a session `other` doesn't have. An export with no
        sessions file lists none, which says nothing."""
        return "sessions" in other.files and any(lo <= (row.get("started_at") or "")[:10] <= hi
                   and not _continued(row, e, other)
                   for row in e.files.get("sessions", ()) if row.get("session_id"))
    if _recency(a) == _recency(b):
        return lacks(a, b) and lacks(b, a)
    older, newer = sorted((a, b), key=_recency)
    return lacks(older, newer)


def _zones_differ(a, b):
    """Whether two exports cut their days in different zones, as far as they say:
    two named `bucket_zone`s that differ, or (without both names) `timezone`
    offsets that differ. An export that names neither is no disagreement."""
    if a.bucket_zone is not None and b.bucket_zone is not None:
        return a.bucket_zone != b.bucket_zone and _zone_offsets_differ(a, b)
    return _zone_offsets_differ(a, b)


def _zone_offsets_differ(a, b):
    offsets = [_offset_minutes(e.context("timezone")) for e in (a, b)]
    return None not in offsets and offsets[0] != offsets[1]


_EPISODE_SUMS = ("usage_before_hit_usd", "usage_after_hit_usd", "requests_after_hit")


def _combine_episode(options):
    """One limit episode seen on several machines, as one row.

    A limit belongs to the account, so the machines saw the same episode --
    each from its first notice there, and each measuring only its own usage.
    So: the earliest hit, the latest notice, and usage summed across machines
    (what ran before and after the hit on any of them). A lock-out only if no
    machine got a request through.
    """
    rows = sorted((row for _, _, row in options), key=lambda r: r.get("hit_at") or "")
    out = dict(rows[0])
    out["last_notice_at"] = max(r.get("last_notice_at") or "" for r in rows)
    out["notices"] = int(sum(_num(r.get("notices")) for r in rows))
    for c in _EPISODE_SUMS:
        values = [r.get(c) for r in rows if r.get(c) not in (None, "")]
        total = sum(_num(v) for v in values)
        out[c] = (int(total) if c == "requests_after_hit" else round(total, 6)) if values else ""
    locked = all(r.get("locked_out") == "yes" for r in rows)
    out["locked_out"] = "yes" if locked else "no"
    resumed = [r["resumed_at"] for r in rows if r.get("resumed_at")]
    out["resumed_at"] = min(resumed) if resumed else ""
    out["blocked_hours"] = max(_num(r.get("blocked_hours")) for r in rows
                               if locked or r.get("locked_out") != "yes")
    out["machine_id"] = "; ".join(sorted({r.get("machine_id") or "" for r in rows} - {""}))
    return out


def _window(e):
    return e.context("window_from"), e.context("window_to")


def _overlaps(a, b):
    (af, at), (bf, bt) = a, b
    if not (af and at and bf and bt):
        return True  # unknown windows: assume they might
    return af <= bt and bf <= at


_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})", re.ASCII)


def _offset_minutes(value):
    """Minutes east of UTC from a `timezone` cell ("-08:00"), or None when the
    export has none (older schema) or it doesn't read as an offset."""
    match = _OFFSET.fullmatch((value or "").strip())
    if not match:
        return None
    minutes = int(match[2]) * 60 + int(match[3])
    return -minutes if match[1] == "-" else minutes


def _recency(e):
    """Sort key for the newest export: its export instant, then its filename's
    date. Every "newest" and "newer" in this module goes through it: timestamps
    carry each machine's own UTC offset, so comparing them as text puts
    10:00+09:00 (01:00 UTC) after 02:00+00:00."""
    moment = _instant(e.exported_at)
    if not isinstance(moment, _dt.datetime):
        moment = _dt.datetime.min
    elif moment.tzinfo is not None:
        moment = moment.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return (moment, e.stamp)


def _session_offset(row):
    """Minutes east of UTC a session's `started_at` was written in, or None."""
    try:
        moment = _dt.datetime.fromisoformat(str(row.get("started_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = moment.utcoffset()
    return None if delta is None else int(delta.total_seconds() // 60)


def _zone_offsets(a, b):
    """(offset of a, offset of b) in minutes east of UTC, or None when either is
    unknown. An export's `timezone` cell is the offset when it was made, so one
    zone reads as two across a daylight-saving change; a session both exports
    list names the same instant in each one's own offset, which is what decides
    whether they bucketed alike. The cells are used when no session is shared."""
    cells = tuple(_offset_minutes(e.context("timezone")) for e in (a, b))
    if None in cells:
        return None  # an older export: no zone to disagree with
    for key in sorted(a.sessions_by_start.keys() & b.sessions_by_start.keys(), key=str):
        seen = (_session_offset(a.sessions_by_start[key][0]), _session_offset(b.sessions_by_start[key][0]))
        if None not in seen:
            return seen
    return cells


def _zone_overlap(a, b):
    """The stretch of time two exports from different time zones both cover, as
    (start, end, a's offset, b's offset) with naive UTC datetimes, or None: the
    days of each window are its own zone's local days, so the same instant can
    be a different date in each. Needs both zones and both windows; one with no
    `timezone` (an older schema) is not a disagreement, so gives None. Two
    exports naming the same `bucket_zone` are one zone whatever offsets they
    show (a named zone's offset moves with daylight saving), so give None too;
    legacy exports, which name none, are compared by offset as before."""
    if a.bucket_zone is not None and a.bucket_zone == b.bucket_zone:
        return None
    offsets = _zone_offsets(a, b)
    if offsets is None or offsets[0] == offsets[1]:
        return None
    # Windows whose dates don't meet are separate exports, however the two
    # zones' midnights happen to fall.
    if not _overlaps(_window(a), _window(b)):
        return None
    spans = []
    for e, offset in zip((a, b), offsets):
        since, until = _window(e)
        if not (since and until):
            return None
        try:
            first = _dt.datetime.combine(_dt.date.fromisoformat(since), _dt.time())
            last = _dt.datetime.combine(_dt.date.fromisoformat(until), _dt.time()) + _dt.timedelta(days=1)
            shift = _dt.timedelta(minutes=offset)
            spans.append((first - shift, last - shift))
        except (ValueError, OverflowError):
            return None
    start, end = max(spans[0][0], spans[1][0]), min(spans[0][1], spans[1][1])
    return (start, end) + offsets if start < end else None


def _flagged_weeks(e):
    """The weeks whose rows an export marks as missing CLI history."""
    kind = "summary" if "summary" in e.files else "breakdown"
    return {r.get("week_start") for r in e.files.get(kind, ()) if r.get("cli_history_complete") == "no"}


def _mixed_zones(group):
    """Clusters among one machine's exports that were made in different time
    zones over the same stretch of time, as dicts {"chosen", "zones",
    "leave_out"}.

    Each export puts a week's work in the week of its own local days, and its
    weekly rows are already summed, so work near a Sunday/Monday midnight sits in
    a different week of each. Choosing a week at a time then takes one week from
    one export and the next from the other, and counts that work twice. So
    exports linked by a stretch of time they both cover form a cluster, and the
    newest of the cluster supplies it: `leave_out` is {week: {exports}} whose
    row for that week is left out. That is only done when nothing is lost by it:
    every day of the week the export covers is inside the stretch the chosen
    export covers, the chosen export holds every session the export lists as
    active that week (or its continuation), and the chosen export's rows for the
    week aren't marked as missing CLI history. Otherwise the week is chosen as
    usual, so a re-export after Claude Code's cleanup can't replace a week that
    still had its work. Weeks no other zone's export reaches stay as they are."""
    parent = list(range(len(group)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    stretches = {}
    for i, a in enumerate(group):
        for j in range(i + 1, len(group)):
            stretch = _zone_overlap(a, group[j])
            if stretch:
                start, end, oa, ob = stretch
                stretches[(i, j)], stretches[(j, i)] = (start, end, oa), (start, end, ob)
                parent[find(i)] = find(j)
    clusters = defaultdict(list)
    for i in range(len(group)):
        if any((i, j) in stretches for j in range(len(group))):
            clusters[find(i)].append(i)

    out = []
    for members in clusters.values():
        chosen = max(members, key=lambda i: _recency(group[i]))
        c, flagged = group[chosen], _flagged_weeks(group[chosen])
        leave_out, zones = defaultdict(set), set()
        for i in members:
            if (i, chosen) not in stretches:
                continue
            e = group[i]
            start, end, offset = stretches[(i, chosen)]
            zones |= {offset, stretches[(chosen, i)][2]}
            shift = _dt.timedelta(minutes=offset)
            first = (start + shift).date().isoformat()
            last = (end - _dt.timedelta(microseconds=1) + shift).date()
            week = _dt.date.fromisoformat(_monday(first))
            while week <= last:
                wk = week.isoformat()
                week += _dt.timedelta(days=7)
                if wk in flagged or not all(first <= d <= last.isoformat() for d in _days_of(wk, _window(e))):
                    continue
                if any(row.get("session_id") and row["session_id"] not in c.sessions_started
                       and not _continued(row, e, c) for row in e.sessions_by_active_week.get(wk, ())):
                    continue
                leave_out[wk].add(e)
        if leave_out:
            out.append({"chosen": c, "leave_out": leave_out, "zones": sorted(zones)})
    return out


_SUMMED = {
    "summary": (
        "sessions_started", "sessions_active", "requests", "est_cost_usd", "est_cost_subagent_usd",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens",
        "thinking_tokens", "web_search_requests", "web_fetch_requests", "tool_calls", "tool_errors",
        "main_active_hours", "prs_opened", "lines_added", "lines_removed", "cli_reported_cost_usd",
        "sessions_with_line_data", "sessions_with_pr_data", "sessions_with_cli_cost",
        "cost_with_line_data_usd", "cost_with_pr_data_usd", "cache_saved_usd", "est_cost_main_usd",
        "sessions_active_cli", "active_days", "week_days_in_window", "api_retries",
        "retry_wait_minutes", "turns", "long_turns", "context_compactions", "limit_hits",
        "blocked_hours", "turn_hours",
        # schema 7: usage health
        "long_context_cost_usd", "cold_cache_usd", "cache_miss_usd", "prompts", "interrupts",
        "auto_compactions", "manual_compactions", "tool_refusals", "error_streaks", "test_runs",
        "lint_runs", "build_runs", "commits", "sessions_edited", "sessions_checked_after_edit",
        "sessions_plan_mode", "sessions_with_subagents", "sessions_instructions_recorded",
        "sessions_with_project_claude_md",
        # schema 9
        "output_tokens_imputed", "est_cost_unlogged_usd",
        # schema 10
        "very_long_context_cost_usd",
        # schema 11
        "week_active_days",
    ),
    "breakdown": (
        "requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens",
        "cache_write_1h_tokens", "cache_write_tokens", "total_tokens", "thinking_tokens",
        "est_cost_usd", "cache_saved_usd", "output_tokens_imputed",
    ),
    "tools": ("calls", "errors"),
}
_INTEGER = re.compile(r"-?\d+")
_SIGNED = frozenset(("cache_saved_usd",))  # the one _NUMBERS column that may be below zero
# Every column the merge does arithmetic on; _read_csv holds them to numbers.
_NUMBERS = frozenset().union(*_SUMMED.values(), _EPISODE_SUMS, (
    "notices", "session_cost_usd", "session_prs", "person_active_days", "window_workdays",
))


def _sum_cells(values):
    """Sum of CSV cells; blank if every one is blank (missing is not zero)."""
    present = [v for v in values if v not in (None, "")]
    if not present:
        return ""
    if all(_INTEGER.fullmatch(v) for v in present):
        return str(sum(int(v) for v in present))
    return repr(round(sum(_num(v) for v in present), 6))


# Columns that older schemas don't have (blank there, meaning "not measured", not
# zero): schema 7's usage health, 9's imputed tokens and 10's very-long-context
# cost. Summing a week's pieces when one piece lacks them would pass the other's
# figure for the whole week, next to costs that cover both.
_GATED = frozenset((
    "long_context_cost_usd", "cold_cache_usd", "cache_miss_usd", "prompts", "interrupts",
    "auto_compactions", "manual_compactions", "tool_refusals", "error_streaks", "test_runs",
    "lint_runs", "build_runs", "commits", "sessions_edited", "sessions_checked_after_edit",
    "sessions_plan_mode", "sessions_with_subagents", "sessions_instructions_recorded",
    "sessions_with_project_claude_md", "output_tokens_imputed", "est_cost_unlogged_usd",
    "very_long_context_cost_usd"))


def _has_measures(row):
    """Whether a summary row holds any usage at all (a cost, requests, tokens or
    sessions), rather than being the empty row a quiet week gets."""
    return any(_num(row.get(c)) for c in ("est_cost_usd", "requests", "total_tokens",
                                          "sessions_started", "sessions_active", "output_tokens",
                                          "tool_calls", "prs_opened"))


def _unify_week_pieces(picked, candidates):
    """A week exported in pieces (to Wednesday, then from Thursday) by one
    machine: rows with the same project are added up by `_combine_week_rows`, but
    a project only one piece has keeps that piece's `week_active_days`, and the
    page reads a week's days from any of its rows. The days belong to the week,
    not the project: the pieces cover different days, so the week's distinct
    days are the pieces' added up (blank if any piece lacks them), and the days
    of the week in the window are the pieces' windows together. Set the same on
    every row of the person's machine and week."""
    pieces = defaultdict(lambda: defaultdict(list))  # (person, machine, week) -> {id(export): [days]}
    owner = {}
    for key, options in candidates.items():
        for _rank, e, row in options:
            raw = row.get("week_active_days")
            owner[id(e)] = e
            pieces[key[:3]][id(e)].append(None if raw in (None, "") else int(_num(raw)))
    pieces = {wk: {i: (owner[i], None if None in vals else max(vals)) for i, vals in slot.items()}
              for wk, slot in pieces.items()}
    fixed = {}
    for week_key, slot in pieces.items():
        if len(slot) < 2:
            continue
        exports = [e for e, _ in slot.values()]
        counts = [d for _, d in slot.values()]
        active = "" if None in counts else str(sum(counts))
        windows = [_window(e) for e in exports]
        covered = set()
        for window in windows:
            covered |= _days_of(week_key[2], window)
        known = all(all(w) for w in windows)
        fixed[week_key] = (active, len(covered) if known else None)
    if not fixed:
        return picked
    out = {}
    for key, value in picked.items():
        change = fixed.get(key[:3])
        if change is None:
            out[key] = value
            continue
        rank, e, row = value
        row = dict(row)
        row["week_active_days"] = change[0]
        if change[1] is not None:
            row["week_days_in_window"] = str(change[1])
            row["partial_week"] = "yes" if change[1] < 7 else "no"
        out[key] = (rank, e, row)
    return out


def _combine_week_rows(kind, rows):
    """One row from the rows several exports hold for the same week and key,
    each covering different days of that week. Measures add up; the rest comes
    from the first (best-ranked) export. sessions_active can overcount a
    session active on both sides of the split -- the files carry counts, not
    the sessions."""
    out = dict(rows[0])
    for c in _SUMMED[kind]:
        out[c] = _sum_cells([r.get(c) for r in rows])
        if kind == "summary" and c in _GATED and any(r.get(c) in (None, "") for r in rows):
            out[c] = ""
    if kind == "summary":
        cost, sub = _num(out["est_cost_usd"]), _num(out["est_cost_subagent_usd"])
        out["subagent_cost_share"] = repr(round(sub / cost, 4)) if cost else ""
        days = _num(out["week_days_in_window"], None)
        out["partial_week"] = "" if days is None else ("yes" if days < 7 else "no")
        if any(r.get("cli_history_complete") == "no" for r in rows):
            out["cli_history_complete"] = "no"
        # Exact days of the week are known only if every piece of it has them: a
        # piece from an older export is days nobody counted, and the sum of the
        # rest would pass for the whole week.
        if any(r.get("week_active_days") in (None, "") for r in rows):
            out["week_active_days"] = ""
        for c in ("models_used", "skills_used"):
            out[c] = "; ".join(sorted({v.strip() for r in rows for v in (r.get(c) or "").split(";")
                                       if v.strip()}))
    return out


def _days_of(week, window):
    """The days of `week` an export's window covers; all seven if unknown."""
    try:
        start = _dt.date.fromisoformat(week)
    except (TypeError, ValueError):
        return {week}
    since, until = window
    days = {(start + _dt.timedelta(days=i)).isoformat() for i in range(7)}
    return {d for d in days if (not since or d >= since) and (not until or d <= until)}


_COST_TOLERANCE = 0.005  # half a cent: rows are rounded, and a warning needs a real gap


def _in_window(day, window):
    """Whether `day` is inside an export's (since, until); a missing end is open."""
    since, until = window
    return bool(day) and (not since or day >= since) and (not until or day <= until)


def _active_count(rows, project):
    """`project`'s total `sessions_active` over `rows`, or None when a row for it
    has no usable value (blank, or an export with no summary file, whose rows
    come from the breakdown sheet): a missing count is not zero."""
    total = 0.0
    for r in rows:
        if r.get("project") == project:
            count = _num(r.get("sessions_active"), None)
            if count is None:
                return None
            total += count
    return total


def _short_projects(week, unlistable, e, chosen, rows_by_export, own_rows):
    """The projects in which, per the weekly `sessions_active`, the chosen
    exports can't hold the `unlistable` (pre-window) sessions that export `e`
    lists; empty if none. A weekly row counts the sessions with a request that
    week, listed or not, while the sessions sheet lists them by their start..end
    span, so the two don't match session for session: a span can touch the week
    with no request in it (a trailing prompt, an idle week inside a long
    session). Both sides are therefore bounded so that such a session can only
    make the check pass.

    * need: `e`'s own `sessions_active` less every session it lists that week that
      isn't unlistable, at most the unlistable ones -- never more than those that
      really had requests that week.
    * room: the chosen exports' `sessions_active` less only the sessions they list
      that lie wholly inside the week with requests -- never less than the
      pre-window sessions they hold.

    Missing work is shown when need exceeds room. A project whose rows (in `e` or
    a chosen export) have no usable `sessions_active` can't be judged by count and
    is left out."""
    last = (_dt.date.fromisoformat(week) + _dt.timedelta(days=6)).isoformat()
    listed = e.sessions_by_active_week.get(week, ())
    unlisted = {r.get("session_id") for r in listed if r.get("session_id") in unlistable}
    short = set()
    for project in {r.get("project") for r in listed if r.get("session_id") in unlisted}:
        mine = [r for r in listed if r.get("project") == project]
        held = sum(1 for r in mine if r.get("session_id") not in unlisted)
        pre = len(mine) - held
        own = _active_count(own_rows, project)
        active = [_active_count(rows_by_export[c], project) for c in chosen]
        if own is None or None in active:
            continue
        need = min(own - held, pre)
        inside = sum(1 for c in chosen for r in c.sessions_by_active_week.get(week, ())
                     if r.get("project") == project and _num(r.get("requests")) > 0
                     and week <= (r.get("started_at") or "")[:10]
                     and (r.get("ended_at") or r.get("started_at") or "")[:10] <= last)
        if need > sum(active) - inside:
            short.add(project)
    return short


def _week_cost(rows):
    return sum(_num(r.get("est_cost_usd")) for r in rows)


def _choose_for_week(week, rows_by_export, left_out=None):
    """(chosen, lost, dropped): which of one machine's exports supply `week`,
    best first, whether some of the week's days are missing as a result, and
    what a dropped export has that the chosen ones lack, as {"excess": dollars,
    "sessions": {session id: (cost, started before the week)}, "cost": the
    dropped export's own cost for the week} (see below).

    Exports covering different days of the week are all used, so windows that
    split a week add up. Of exports covering the same days, the one with
    complete CLI history wins (a re-export after Claude Code's 30-day cleanup
    must not replace a complete week) -- flagged, or given away by sessions
    another export has on days this one covers and this one lacks -- then the
    one covering more of the week, then the newer. An export left out whose own days weren't all covered by
    the chosen ones, and which had work that week, means days are missing.

    `dropped` catches work an export left out holds on days the chosen exports
    do cover, two ways, either of which is enough to warn:

    * cost: weekly rows are the finest cost the export has (no per-day cost), so
      an export left out whose covered days are all covered by the chosen ones
      is compared with their weekly cost. The same days can't cost more in one
      export than in the others, so a surplus is work the chosen ones lack.
      The largest surplus is kept (re-exports of one machine repeat each other).
      It is a floor: work only the chosen exports hold hides an equal amount.
    * sessions: ones the left-out export lists as active on a covered day
      (start to end, not just the start week) that no chosen export holds by
      id, unless a chosen export holds the continuation of one -- the same start
      and project, running on past it, or ending with it at no less cost from a
      later export (a byte copy). Resuming a session moves its work to a new id,
      so ids alone would cry wolf; equal starts alone would excuse a second
      machine's own session that merely began at the same moment. When the
      export lists sessions for the week and every one is accounted for, the
      cost gap is ignored (an export from another timezone puts the same days in
      different weeks). Sessions the chosen exports couldn't have listed -- they
      started before every chosen window, as a rolling export from the Monday a
      session crossed into -- aren't evidence of a gap by themselves (the chosen
      export holds the work on the days it covers but not the sheet row). They
      count when the weekly cost gap shows work is missing, or when the chosen
      exports' weekly `sessions_active` (every session with work that week, listed
      or not) less the sessions they list leaves no room for them in their project
      (only those sessions then, the cost gap's too; a project with no usable
      `sessions_active` isn't judged by it, and a cost gap with no short project
      names every such session), or when the left-out export has days the chosen
      ones lack.

    `left_out` ({export: rows}) are exports the caller has ruled out of the
    choice (see `_mixed_zones`); they are never chosen, only checked for work
    they hold that the chosen exports lack."""

    left_out = left_out or {}
    days = {e: _days_of(week, _window(e)) for e in (*rows_by_export, *left_out)}

    # Only this week's sessions can show an export lacks some of the week.
    week_sessions = {}
    for e in rows_by_export:
        week_sessions.update(e.sessions_by_week.get(week, {}))

    def lacks_sessions(e):
        started = e.sessions_started
        return any(day in days[e] and sid not in started for sid, day in week_sessions.items())

    def rank(e):
        flagged = any(r.get("cli_history_complete") == "no" for r in rows_by_export[e])
        return (not flagged, not lacks_sessions(e), len(days[e]), _recency(e))

    chosen, covered = [], set()
    for e in sorted(rows_by_export, key=rank, reverse=True):
        if not days[e] & covered:
            chosen.append(e)
            covered |= days[e]
    lost = any(days[e] - covered and any(r.get("project") for r in rows_by_export[e])
               for e in rows_by_export if e not in chosen)
    chosen_cost = sum(_week_cost(rows_by_export[e]) for e in chosen)
    held = {sid for e in chosen for sid in e.sessions_started}

    excess, unmatched, dropped_cost = 0.0, {}, 0.0
    for e, rows in (*rows_by_export.items(), *left_out.items()):
        if e in chosen:
            continue
        cost = _week_cost(rows)
        dropped_cost = max(dropped_cost, cost)
        listed = e.sessions_by_active_week.get(week, ())
        lacking, unlistable = {}, {}
        for row in listed:
            sid = row.get("session_id")
            if not sid or sid in held or sid in unmatched \
                    or any(_continued(row, e, c) for c in chosen):
                continue
            start, end = (row.get("started_at") or "")[:10], (row.get("ended_at") or "")[:10]
            if any(start <= d <= max(start, end) for d in covered):
                found = (_num(row.get("est_cost_usd")), start < week)
                # The sessions sheet lists a session by the day it started, so a
                # chosen export whose window begins after that day (a rolling
                # export from the Monday a session crossed into) can't list it
                # though it holds the work on the days it covers: its absence
                # from the sheet says nothing.
                (lacking if any(_in_window(start, _window(c)) for c in chosen) else unlistable)[sid] = found
        # Weekly cost is only a hint: the same days fall in different weeks when an
        # export is made from another timezone. When every session the export lists
        # that week is accounted for, the gap isn't missing work. Sessions the
        # chosen exports couldn't have listed prove nothing by their absence, but
        # they aren't accounted for either: they are missing when the weekly cost
        # gap shows it, or the chosen exports' weekly `sessions_active` has no room
        # for them (extra work in the chosen export can hide the cost gap), or
        # the export has days the chosen ones lack (no gap to compare).
        if unlistable and not lacking:
            gap = cost - chosen_cost > _COST_TOLERANCE
            if not days[e] <= covered:
                lacking = unlistable
            else:
                # The count is per project: a project the chosen exports account
                # for lost nothing, so with a short project only its sessions are
                # named, on the cost-gap path too. The cost gap alone (no short
                # project) can't say which project lost the work: all are named.
                short = _short_projects(week, unlistable, e, chosen, rows_by_export, rows)
                if short:
                    projects = {r.get("session_id"): r.get("project") for r in listed}
                    lacking = {sid: v for sid, v in unlistable.items() if projects.get(sid) in short}
                elif gap:
                    lacking = unlistable
        unmatched.update(lacking)
        if days[e] <= covered and (lacking or not listed):
            excess = max(excess, cost - chosen_cost)
    dropped = {"excess": excess if excess > _COST_TOLERANCE else 0.0,
               "sessions": unmatched, "cost": dropped_cost, "exports": len(rows_by_export)}
    return chosen, lost, dropped


def merge(exports, problems=(), prices=None, skipped=None, prices_rejected=False):
    """Combine exports into one de-duplicated set.

    Returns {"files": {kind: (columns, rows)}, "people": [...], "sources": [...],
             "skipped": [...], "warnings": [{"level", "message"}]}.
    `skipped` lists the exports read_exports left out whole (default: the
    `skipped` of `problems`); `prices_rejected` says a seat-prices file was
    found and refused (its problem is already among `problems`).
    """
    if skipped is None:
        skipped = getattr(problems, "skipped", [])
    warnings = [{"level": "warn", "message": p} for p in problems]
    keys = _person_keys(exports)
    machines, machine_notes = _machine_groups(exports, keys)
    by_person = defaultdict(list)
    for e in exports:
        by_person[keys[id(e)]].append(e)

    # ---- version and overlap checks -------------------------------------- #
    versions = Counter(e.schema_version for e in exports)
    current = context.EXPORT_SCHEMA_VERSION
    newer = sorted(v for v in versions if v > current)
    if newer:
        warnings.append({"level": "warn", "message":
            f"Exports use schema version {', '.join(map(str, newer))}, newer than this tool's "
            f"{current}. Columns it doesn't know are ignored and any whose meaning changed may be "
            "misread -- update this tool to read them correctly."})
    if len(versions) > 1 and min(versions) < current:
        oldest = min(versions)
        # Each version's people, so "ask people on version 6" doesn't also name
        # the ones on 9 or 11.
        stale = defaultdict(lambda: defaultdict(list))
        for e in sorted(exports, key=lambda e: e.source):
            if e.schema_version < current:
                stale[e.schema_version][keys[id(e)]].append(e.source)
        parts = []
        for version, by_person_files in sorted(stale.items()):
            named = [f"{person} ({', '.join(files[:3] + ([f'+{len(files) - 3} more files'] if len(files) > 3 else []))})"
                     for person, files in sorted(by_person_files.items())]
            if len(named) > 5:
                named = named[:5] + [f"{len(named) - 5} more people"]
            parts.append(f"ask people on version {version} to re-export: {'; '.join(named)}")
        warnings.append({"level": "warn", "message":
            f"Exports use schema versions {', '.join(map(str, sorted(versions)))}. Columns an older "
            f"file doesn't have are blank -- with the current tool the data is complete. "
            f"{'. '.join(p[0].upper() + p[1:] for p in parts)}."
            + (" Version 8 also changed how days and weeks are bucketed (in an export zone, UTC "
               "by default, not the machine's), so older exports put work near a week's edge in "
               "different weeks than newer ones and should be re-exported." if oldest < 8 else "")})
    people_by_zone = defaultdict(set)
    for e in exports:
        if e.bucket_zone is not None:
            people_by_zone[e.bucket_zone].add(keys[id(e)])
    if len(people_by_zone) > 1:
        listed = ", ".join(f"{zone} ({len(people)} {'person' if len(people) == 1 else 'people'})"
                           for zone, people in sorted(people_by_zone.items(),
                                                      key=lambda item: (-len(item[1]), item[0])))
        everyone = set().union(*people_by_zone.values())
        if len(everyone) == 1:
            (who,) = everyone
            names = sorted(people_by_zone)
            first = f"{who} exported in both {names[0]} and {names[1]}" if len(names) == 2 \
                else f"{who} exported in {len(names)} zones ({', '.join(names)})"
            first += ". Each export cuts its days and weeks at midnight in its own zone and weekly " \
                     "rows can't be re-bucketed, so work near a week boundary lands in different " \
                     "weeks in different exports. Re-export with one zone: "
        else:
            first = (f"Exports were bucketed in different time zones: {listed}. Each export cuts "
                     "its days and weeks at midnight in its own zone and weekly rows can't be "
                     "re-bucketed, so work near a week boundary lands in different weeks for "
                     "different people. Ask everyone to export with the same zone: ")
        warnings.append({"level": "warn", "message":
            first + "pass --tz ZONE (or set CLAUDE_USAGE_EXPORT_TZ) so weeks line up."})
    pricing = {e.context("pricing_version") for e in exports if e.context("pricing_version")}
    if len(pricing) > 1:
        warnings.append({"level": "info", "message":
            f"Exports were priced with {len(pricing)} different rate tables; costs from different "
            "tool versions aren't exactly comparable."})
    zone_weeks = {}  # (person, machine, week) -> the mixed-zone cluster that settles it
    zone_clusters = []
    for person, group in sorted(by_person.items()):
        # Only one machine's exports can duplicate each other.
        same_machine = defaultdict(list)
        for e in group:
            same_machine[machines[id(e)]].append(e)
        for machine, exports_of_one in same_machine.items():
            for cluster in _mixed_zones(exports_of_one):
                cluster["person"], cluster["taken"] = person, set()
                zone_clusters.append(cluster)
                for week in cluster["leave_out"]:
                    zone_weeks[(person, machine, week)] = cluster
            overlapping = any(_overlaps(_window(a), _window(b))
                              for i, a in enumerate(exports_of_one) for b in exports_of_one[i + 1:])
            if overlapping:
                warnings.append({"level": "info", "message":
                    f"{person}: {len(exports_of_one)} exports from one machine with overlapping "
                    "windows. Each duplicate row was kept once: per week, from the export with "
                    "complete CLI history covering more of that week (then the newer one); exports "
                    "covering different days of a week were added together."})
    warnings += machine_notes
    warnings += _identity_notes(exports, keys)

    # ---- which exports supply each week ---------------------------------- #
    # Decided once per person, machine and week, from the summary (else the
    # breakdown), so the breakdown always comes from the same exports as the
    # summary and still sums to it.
    week_rows = defaultdict(lambda: defaultdict(list))
    for e in exports:
        kind = next((k for k in ("summary", "breakdown") if k in e.files), None)
        for row in e.files.get(kind, ()):
            week_rows[(keys[id(e)], machines[id(e)], row.get("week_start") or "")][e].append(row)
    week_sources, lost_weeks = {}, defaultdict(set)
    dropped_weeks = defaultdict(lambda: defaultdict(list))
    for week_key, rows_by_export in week_rows.items():
        cluster = zone_weeks.get(week_key)
        if cluster:
            # Exports made in another time zone put this week's work in other
            # weeks; the newest export supplies the stretch both cover.
            ruled_out = cluster["leave_out"][week_key[2]]
            left_out = {e: rows for e, rows in rows_by_export.items() if e in ruled_out}
            if left_out:
                cluster["taken"].add(week_key[2])
                rows_by_export = {e: rows for e, rows in rows_by_export.items() if e not in left_out}
        else:
            left_out = None
        week_sources[week_key], lost, dropped = _choose_for_week(week_key[2], rows_by_export, left_out)
        if lost:
            lost_weeks[week_key[0]].add(week_key[2])
        if dropped["excess"] or dropped["sessions"]:
            dropped_weeks[week_key[0]][week_key[2]].append(dropped)
    for cluster in zone_clusters:
        if not cluster["taken"]:
            continue
        zones = " and ".join("UTC{}{:02d}:{:02d}".format("-" if m < 0 else "+", *divmod(abs(m), 60))
                             for m in cluster["zones"])
        warnings.append({"level": "info", "message":
            f"{cluster['person']}: exports from one machine were made in different time zones "
            f"({zones}), so each puts the same work in its own local week. A week's rows can't be "
            f"re-bucketed, so for week(s) {', '.join(sorted(cluster['taken']))} the newest export "
            f"({cluster['chosen'].source}) was used instead of adding exports together, where it holds "
            "everything the older one lists. Work near a week's edge can still be counted in two "
            "weeks if an export's window starts or ends mid-week."})
    for person, weeks in sorted(lost_weeks.items()):
        warnings.append({"level": "warn", "message":
            f"{person}: exports from one machine overlap within week(s) {', '.join(sorted(weeks))} "
            "without one covering all the days the others do. A week's rows can't be split by "
            "day, so one export was used and days only the other covered are missing. An export "
            "whose window runs Monday to Sunday fixes it."})
    for person, weeks in sorted(dropped_weeks.items()):
        remaining, counted, details, total, joined = {}, set(), [], 0.0, 2
        for week, infos in sorted(weeks.items()):
            joined = max([joined] + [info["exports"] for info in infos])
            # A session is lost in every week it is active in. Its whole cost belongs
            # to the week it started in; one that came in from an earlier week is
            # credited, week by week, no more than the dropped export had that week
            # and no more than its cost not yet credited.
            live = {}
            for info in infos:
                for sid, (cost, spilled) in info["sessions"].items():
                    remaining.setdefault(sid, cost)
                    live[sid] = spilled
            live = {sid: sp for sid, sp in live.items() if remaining[sid] > 0 or sid not in counted}
            new = [sid for sid in live if sid not in counted]
            counted |= set(new)
            cap, credit = max(info["cost"] for info in infos), 0.0
            for sid in sorted(live, key=lambda sid: live[sid]):
                take = min(remaining[sid], max(0.0, cap - credit)) if live[sid] else remaining[sid]
                remaining[sid] -= take
                credit += take
            amount = max(sum(info["excess"] for info in infos), credit)
            if not new and amount <= _COST_TOLERANCE:
                continue
            total += amount
            details.append(f"{len(new)} in the week of {week}" if new
                           else f"est. ${amount:,.2f} in the week of {week}")
        if not counted and total <= _COST_TOLERANCE:
            continue
        # Sessions are a warning however little they cost: they are dropped work.
        money = "< $0.01" if 0 < total < _COST_TOLERANCE else f"${total:,.2f}"
        holders = ("only one of two joined exports holds" if joined == 2
                   else f"not all of the {joined} joined exports hold")
        if counted:
            plural = len(counted) != 1
            lead = (f"{len(counted)} session{'s' if plural else ''} (est. {money}) {holders} "
                    f"{'are' if plural else 'is'}")
        else:
            lead = f"est. {money} of work {holders} is"
        warnings.append({"level": "warn", "message":
            f"{person}: {lead} missing from the summary and breakdown, because a week is "
            f"taken from one export ({'; '.join(details)}). The sessions file still lists the "
            "sessions, so it can total more than the summary; the figure is an estimate (a "
            "session's whole cost, or the weekly cost gap). Exports that share a session count as "
            "one machine, so their weeks are not added together."})

    files = {}
    for kind in KINDS:
        holders = [e for e in exports if kind in e.files]
        if not holders:
            continue
        # `person` first -- the normalised identity to pivot on -- then the
        # newest schema's columns, then anything only older files had.
        holders_by_version = sorted(holders, key=lambda e: (e.schema_version, _recency(e)),
                                    reverse=True)
        columns = ["person"]
        for e in holders_by_version:
            columns += [c for c in e.columns.get(kind, []) if c not in columns]
        columns.append("merged_from")

        candidates = defaultdict(list)
        for e in holders:
            who = (keys[id(e)], machines[id(e)]) if kind in _PER_MACHINE else (keys[id(e)],)
            for row in e.files[kind]:
                natural = tuple(row.get(c, "") for c in _ROW_KEYS[kind])
                if kind in _SUMMED:
                    week = who + (row.get("week_start") or "",)
                    if e not in week_sources.get(week, ()):
                        continue
                    rank = -week_sources[week].index(e)
                else:
                    rank = _recency(e)
                if kind == "limits" and row.get("resets_at"):
                    # One machine has one episode per limit and reset time; the
                    # hit time is left out so the cross-machine step can match.
                    natural = natural[:2]
                candidates[who + natural].append((rank, e, row))

        sources = {}
        if kind in _SUMMED:
            # Every export chosen for a week supplies different days of it.
            picked = {}
            for key, options in candidates.items():
                # One row per export: a key repeated inside one file is not
                # more of the week.
                first = {}
                for option in options:
                    first.setdefault(id(option[1]), option)
                options = sorted(first.values(), key=lambda o: o[0], reverse=True)
                if len(options) == 1:
                    picked[key] = options[0]
                else:
                    picked[key] = (None, options[0][1], _combine_week_rows(kind, [o[2] for o in options]))
                    sources[key] = "; ".join(o[1].source for o in options)
            if kind == "summary":
                picked = _unify_week_pieces(picked, candidates)
                # A quiet week's empty row from one export, next to another's
                # work in the same week, is not a week of its own. A row with no
                # project that holds anything (cost, requests, tokens) is work,
                # not an empty row, whatever else the week has.
                busy = {key[:3] for key in picked if key[3]}
                picked = {key: v for key, v in picked.items()
                          if key[3] or key[:3] not in busy or _has_measures(v[2])}
        else:
            picked = {key: max(options, key=lambda o: o[0]) for key, options in candidates.items()}

        if kind == "limits":
            # A limit is the account's, not the machine's: the same episode
            # seen on two machines is one episode. Matched on limit and reset
            # time; an episode with no reset time can't be matched, so stays
            # per machine.
            # Only on a Claude account: an identity without one may be several
            # people, whose episodes at the same reset time are not one.
            claude_people = {keys[id(e)] for e in exports if e.claude_email}
            episodes = defaultdict(list)
            for (person, machine, limit_type, resets_at, *hit_at), option in picked.items():
                key = (person, limit_type, resets_at)
                episodes[key if resets_at and person in claude_people
                         else key + (machine, *hit_at)].append(option)
            picked = {}
            for key, options in episodes.items():
                if len(options) == 1:
                    picked[key] = options[0]
                else:
                    first = min(options, key=lambda o: o[2].get("hit_at") or "")
                    picked[key] = (first[0], first[1], _combine_episode(options))
                    sources[key] = "; ".join(sorted({o[1].source for o in options}))

        rows, kept = [], []
        for key in sorted(picked, key=lambda k: tuple(str(x) for x in k)):
            _, e, row = picked[key]
            out = {c: row.get(c, "") for c in columns}
            out["person"] = keys[id(e)]
            out["merged_from"] = sources.get(key, e.source)
            if kind in _PER_MACHINE:
                # Not a column of the CSV: which machine's row this is, so the
                # page can tell two machines' days of a week from one's.
                out["_machine"] = machines[id(e)]
            rows.append(out)
            kept.append((e, row))
        files[kind] = (columns, rows, kept)

    # ---- cross-person sanity -------------------------------------------- #
    if "sessions" in files:
        owners = defaultdict(set)
        for e, row in files["sessions"][2]:
            owners[row.get("session_id")].add(keys[id(e)])
        shared = [sid for sid, who in owners.items() if sid and len(who) > 1]
        if shared:
            warnings.append({"level": "warn", "message":
                f"{len(shared)} session(s) appear in more than one person's export -- the same logs "
                "were exported under two identities (a ~/.claude folder shared or copied between "
                "Claude accounts, or --as on an export with no Claude account email). They are "
                "counted under each."})

    missing = [k for k in ("breakdown", "limits") if any(k not in e.files for e in exports)]
    if missing and exports:
        warnings.append({"level": "info", "message":
            f"Some exports predate the {' and '.join(missing)} file(s); those people are missing "
            "from the views built on them."})

    people = _people(keys, machines, by_person, files)
    apply_seat_value(people, prices)
    if exports and not prices:
        if prices_rejected:
            warnings.append({"level": "info", "message":
                f"Value per seat is blank until the {SEAT_PRICE_FILE} problem above is fixed."})
        else:
            warnings.append({"level": "info", "message":
                f"No {SEAT_PRICE_FILE} found, so value per seat is blank. Add one next to the exports "
                "with your seat price per tier (see docs/team.md)."})
    elif prices:
        unpriced = [p for p in people if p["seat_price"] is None]
        if unpriced:
            # An enterprise seat is priced by its seat tier alone (its rate-limit tier is never used).
            tiers = sorted({tierid for p in unpriced
                            for tierid in ((p.get("seat_tier"),) if str(p.get("seat_tier") or "").startswith("enterprise")
                                           else (p.get("rate_limit_tier"), p.get("seat_tier"))) if tierid})
            warnings.append({"level": "warn", "message":
                f"Value per seat is blank for {_named(unpriced)}: "
                + (f"their tier ({', '.join(tiers)}) has no price" if tiers else "they have no recorded tier")
                + f" in {prices['source']}, and there is no \"default\". Add a price for "
                + (", ".join(f'"{t}"' for t in tiers) if tiers else '"default"') + "."})
    for p in people:
        gaps = p.get("gaps") or []
        if gaps:
            spans = "; ".join(f"{a} .. {b}" for a, b in gaps)
            warnings.append({"level": "info", "message":
                f"{p['display_name']} ({p['person']}): their exports leave out {spans} "
                f"({p['gap_days']} day(s) between them). Nothing is known about those days: they aren't "
                "counted as idle or paid for in the seat figures. Re-export with a window that "
                "reaches across them to fill them."})
        if p.get("no_usage_window"):
            warnings.append({"level": "warn", "message":
                f"{p['display_name']} ({p['person']}): the export holds no usage and covers only "
                f"{p['window_from']} .. {p['window_to']} ({p['window_days']} day(s)), so earlier weeks show "
                "as not covered, and the seat is not called idle (nothing here shows whether it was used). Claude Code deletes "
                "transcripts after 30 days, or its logs aren't on that machine. Re-export with "
                "`python3 collect.py --export --since YYYY-MM-DD` (the first day you want covered), or "
                "from the machine that has the logs."})
    truncated = [p for p in people if p.get("cli_history_truncated") == "yes"]
    if truncated:
        warnings.append({"level": "info", "message":
            f"CLI history cut short by Claude Code's 30-day transcript cleanup for {_named(truncated)}: "
            "their early weeks hold Desktop work only. Each can stop it by adding "
            "\"cleanupPeriodDays\": 365 to their own ~/.claude/settings.json (see docs/export.md)."})

    sources = [{
        "source": e.source, "person": keys[id(e)], "exported_at": e.exported_at,
        "window_from": _window(e)[0], "window_to": _window(e)[1],
        "schema_version": e.schema_version, "tool_version": e.context("tool_version"),
        "machine_id": e.machine_id, "files": sorted(e.files),
        "bucket_zone": e.bucket_zone,
    } for e in sorted(exports, key=lambda e: (keys[id(e)], _recency(e)))]

    return {
        "files": {k: (cols, rows) for k, (cols, rows, _) in files.items()},
        "people": people,
        "sources": sources,
        "skipped": list(skipped),
        "warnings": warnings,
        "prices": prices,
    }


# Facts about the account, not the machine: from the person's newest export.
_PERSON_CONTEXT = (
    "person_name", "claude_account_email", "person_email", "seat_tier", "rate_limit_tier",
    "extra_usage_enabled", "allowance_est_usd", "allowance_week_used_pct", "allowance_status",
    "projected_limit_hit_at", "tool_version",
)


def _covered_spans(windows):
    """The stretches the (since, until) `windows` cover between them, as sorted
    (first, last) dates: overlapping or touching windows are one stretch. A
    window that is missing a date, or ends before it starts, covers nothing
    (the dashboard's `coverageIntervals` skips such a window too)."""
    spans = []
    for since, until in windows:
        try:
            first, last = _dt.date.fromisoformat(since), _dt.date.fromisoformat(until)
        except (TypeError, ValueError):
            continue
        if first <= last:
            spans.append((first, last))
    merged = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + _dt.timedelta(days=1):
            if last > merged[-1][1]:
                merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return merged


def _covered_days(windows):
    """How many days the (since, until) `windows` cover between them: overlapping
    or touching ones count once, and the days between two don't count."""
    return sum((last - first).days + 1 for first, last in _covered_spans(windows))


def _window_days(e):
    since, until = _window(e)
    return (_dt.date.fromisoformat(until) - _dt.date.fromisoformat(since)).days + 1


def _machine_active_days(group):
    """(active days, workdays) for one machine, across all its exports.

    Exports carry a count of active days per window, not the days, so windows
    are combined without splitting any: the longest window first (then the
    newer), then each export whose window doesn't overlap one already taken.
    Exports in pieces add up; a re-export inside a longer window doesn't
    replace it. Days only an overlapping, shorter export covered are left out
    of both counts, so seat use is still a fair rate."""
    known = [e for e in group if all(_window(e))
             and e.context("person_active_days") not in (None, "")]
    if not known:
        newest = max(group, key=_recency)
        counts = (_num(newest.context(c), None) for c in ("person_active_days", "window_workdays"))
        return tuple(None if n is None else int(n) for n in counts)
    taken = []
    for e in sorted(known, key=lambda e: (_window_days(e), _recency(e)), reverse=True):
        if not any(_overlaps(_window(e), _window(t)) for t in taken):
            taken.append(e)
    return (int(sum(_num(e.context("person_active_days")) for e in taken)),
            int(sum(_num(e.context("window_workdays")) for e in taken)))


def _activity(by_machine):
    """Seat activity across a person's machines ({machine: [exports]}).
    Exports carry day counts, not days, so a day worked on two machines can't
    be told from two days: active days are the busiest machine's (with its
    windows' workdays), a floor rather than a double count. Last active is the
    latest anywhere; idle only if every machine's newest export is; history is
    cut short if it is on any machine's newest export."""
    latest = [max(group, key=_recency) for group in by_machine.values()]
    active, workdays = max((_machine_active_days(group) for group in by_machine.values()),
                           key=lambda a: _num(a[0]))
    truncated = [e for e in latest if e.context("cli_history_truncated") == "yes"]
    newest = max(latest, key=_recency)
    idle = [e.context("seat_idle") for e in latest]
    return {
        "person_active_days": active,
        "window_workdays": workdays,
        "last_active_date": max((e.context("last_active_date") or "" for e in latest), default="") or None,
        "seat_idle": "no" if "no" in idle else newest.context("seat_idle"),
        "cli_history_truncated": "yes" if truncated else newest.context("cli_history_truncated"),
        "cli_history_from": (max(e.context("cli_history_from") or "" for e in truncated) or None
                             if truncated else newest.context("cli_history_from")),
    }


_NAME_SPLIT = re.compile(r"[._-]+")


def display_name(person_key, person_name):
    """What the team page calls someone: their git name when it is a real name
    (it has a space), else the part of their email before the @ in title case,
    so one person isn't "janedoe" while the next is an email address.
    A key that isn't an email (an OS user name) is kept as it is."""
    name = (person_name or "").strip()
    if name and re.search(r"\s", name) and "@" not in name:
        return name
    if "@" not in (person_key or ""):
        return person_key or name
    local = person_key.split("@", 1)[0]
    words = [w for w in _NAME_SPLIT.split(local) if w]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words) or person_key


def _named(people):
    """People as a manager can act on them: "Name (email), Name (email)". Every
    name is listed; a notice that says "3 people" sends the manager hunting."""
    return ", ".join(f"{p.get('display_name') or p['person']} ({p['person']})" for p in people)


def _unique_display_names(people):
    """Make every person's display_name distinct, in place. "john.smith@a" and
    "john_smith@b" both read "John Smith", and a chart legend can't tell them
    apart: a shared name gets the email's domain, or the whole key when the
    domain is shared too."""
    counts = Counter(p["display_name"] for p in people)
    clashing = [p for p in people if counts[p["display_name"]] > 1]
    domains = Counter((p["display_name"], p["person"].partition("@")[2]) for p in clashing)
    for p in clashing:
        domain = p["person"].partition("@")[2]
        tag = domain if domain and domains[(p["display_name"], domain)] == 1 else p["person"]
        p["display_name"] = f"{p['display_name']} ({tag})"


def _people(keys, machines, by_person, files):
    """One row per person: account facts from their newest export, activity
    combined across their machines, usage totals from the merged rows."""
    totals = defaultdict(lambda: defaultdict(float))
    for kind, measures in (("summary", ("est_cost_usd", "est_cost_subagent_usd", "output_tokens",
                                        "thinking_tokens", "total_tokens", "requests",
                                        "sessions_started", "api_retries")),
                           ("limits", ()), ("prs", ())):
        if kind not in files:
            continue
        for e, row in files[kind][2]:
            t = totals[keys[id(e)]]
            for m in measures:
                t[m] += _num(row.get(m))
            if kind == "prs":
                t["prs"] += 1
            if kind == "limits":
                t["limit_hits"] += 1
                if row.get("locked_out") == "yes":
                    t["lockouts"] += 1
                    t["locked_out_hours"] += _num(row.get("blocked_hours"))
                if row.get("limit_type") == "weekly":
                    t["extra_usage_usd"] += _num(row.get("usage_after_hit_usd"))

    people = []
    for person, group in sorted(by_person.items()):
        newest = max(group, key=_recency)
        by_machine = defaultdict(list)
        for e in group:
            by_machine[machines[id(e)]].append(e)
        row = {"person": person}
        for c in _PERSON_CONTEXT:
            row[c] = newest.context(c)
        row.update(_activity(by_machine))
        row["window_from"] = min((w for w in (_window(e)[0] for e in group) if w), default=None)
        row["window_to"] = max((w for w in (_window(e)[1] for e in group) if w), default=None)
        spans = _covered_spans(_window(e) for e in group)
        row["window_days"] = sum((last - first).days + 1 for first, last in spans)
        row["windows"] = [[a.isoformat(), b.isoformat()] for a, b in spans]
        # The days between two stretches: in no export, so neither idle nor paid for.
        row["gaps"] = [[(a[1] + _dt.timedelta(days=1)).isoformat(), (b[0] - _dt.timedelta(days=1)).isoformat()]
                       for a, b in zip(spans, spans[1:])]
        row["gap_days"] = sum((b[0] - a[1]).days - 1 for a, b in zip(spans, spans[1:]))
        row["exports"] = len(group)
        row["machines"] = len(by_machine)
        # Two machines' active days can't be told from the same days: the figure
        # is the busier machine's, so seat use is a floor.
        row["seat_use_is_floor"] = "yes" if len(by_machine) > 1 else "no"
        row["latest_export_at"] = newest.exported_at
        row["schema_version"] = newest.schema_version
        active, workdays = _num(row.get("person_active_days"), None), _num(row.get("window_workdays"), None)
        row["seat_use"] = round(min(active / workdays, 1.0), 4) if active is not None and workdays else None
        t = totals.get(person, {})
        cost = t.get("est_cost_usd", 0.0)
        row.update({
            "est_cost_usd": round(cost, 2),
            "subagent_cost_share": round(t.get("est_cost_subagent_usd", 0.0) / cost, 4) if cost else None,
            "requests": int(t.get("requests", 0)),
            "sessions": int(t.get("sessions_started", 0)),
            "total_tokens": int(t.get("total_tokens", 0)),
            "output_tokens": int(t.get("output_tokens", 0)),
            "thinking_tokens": int(t.get("thinking_tokens", 0)),
            "api_retries": int(t.get("api_retries", 0)),
            "prs": int(t.get("prs", 0)),
            "limit_hits": int(t.get("limit_hits", 0)),
            "lockouts": int(t.get("lockouts", 0)),
            "locked_out_hours": round(t.get("locked_out_hours", 0.0), 2),
            "extra_usage_usd": round(t.get("extra_usage_usd", 0.0), 2),
        })
        row["display_name"] = display_name(person, row.get("person_name"))
        # An export from a machine with no logs holds nothing and covers only the
        # short default window (30 days from schema 13; a single day before it):
        # what the person did earlier isn't there, so the seat is idle only for
        # that stretch and the weeks before it are not covered.
        row["no_usage_window"] = bool(
            row["window_days"] <= getattr(context, "NO_LOGS_WINDOW_DAYS", 30)
            and not cost and not row.get("requests")
            and not row.get("person_active_days") and not row.get("last_active_date"))
        if row["no_usage_window"]:
            row["seat_use"] = None   # nothing to measure: not 0.0 (the page shows "—" too)
        people.append(row)
    _unique_display_names(people)
    return people


PEOPLE_COLUMNS = [
    "person", "person_name", "claude_account_email", "person_email", "seat_tier",
    "rate_limit_tier", "extra_usage_enabled", "exports", "latest_export_at", "schema_version",
    "tool_version", "window_from", "window_to", "window_days", "gap_days", "person_active_days",
    "window_workdays", "seat_use", "seat_use_is_floor", "last_active_date", "seat_idle", "cli_history_from", "cli_history_truncated",
    "sessions", "requests", "est_cost_usd", "subagent_cost_share", "total_tokens",
    "output_tokens", "thinking_tokens", "api_retries", "limit_hits", "lockouts",
    "locked_out_hours", "extra_usage_usd", "allowance_est_usd", "allowance_week_used_pct",
    "allowance_status", "projected_limit_hit_at",
    "prs", "seat_price", "seat_cost_in_window", "value_multiple", "machines",
]


# --------------------------------------------------------------------------- #
# forecast
# --------------------------------------------------------------------------- #

def _monday(day):
    d = _dt.date.fromisoformat(day[:10])
    return (d - _dt.timedelta(days=d.weekday())).isoformat()


def _covered_weeks(merged):
    """The Mondays of the weeks any export's window touches: the dashboard's
    rule (`coveredWeek`), so a week in a gap between exports, or before the
    first one, is unknown rather than a week with no extra usage."""
    windows = merged.get("sources") or merged["people"]
    spans = _covered_spans((s.get("window_from"), s.get("window_to")) for s in windows)
    weeks = set()
    for first, last in spans:
        w = _dt.date.fromisoformat(_monday(first.isoformat()))
        while w <= last:
            weeks.add(w)
            w += _dt.timedelta(days=7)
    return weeks


def overage_forecast(merged, today=None, basis_weeks=4):
    """Extra-usage spend per week, and a straight projection from the recent
    average. Deliberately simple and stated: weekly overage is spiky (it only
    happens in weeks someone hits the limit), so the projection is a run-rate,
    not a prediction, and comes with the weeks it was computed from.

    Only weeks the exports cover count, as on the dashboard: a week no export's
    window touches (after the newest export, before the first, or in a gap
    between two) is unknown, not $0, and is left out; the week an export ends
    in is shown but not averaged unless the window reaches its Sunday."""
    today = today or _dt.date.today()
    limits = (merged["files"].get("limits") or ([], []))[1]
    weekly = defaultdict(float)
    for row in limits:
        if row.get("limit_type") == "weekly" and row.get("hit_at"):
            weekly[_monday(row["hit_at"])] += _num(row.get("usage_after_hit_usd"))
    starts = [p["window_from"] for p in merged["people"] if p.get("window_from")]
    if not starts:
        return None
    ends = [p["window_to"] for p in merged["people"] if p.get("window_to")]
    covered_to = min(_dt.date.fromisoformat(max(ends)), today) if ends else today
    first = _dt.date.fromisoformat(_monday(min(starts)))
    last = _dt.date.fromisoformat(_monday(covered_to.isoformat()))
    seen = _covered_weeks(merged)
    weeks = []
    w = first
    while w <= last:
        if w in seen:
            weeks.append(w)
        w += _dt.timedelta(days=7)
    series = [[w.isoformat(), round(weekly.get(w.isoformat(), 0.0), 2)] for w in weeks]
    # The rate averages the weeks' unrounded sums, as the page does: averaging the
    # cent-rounded weeks could land a cent away from what the page shows.
    complete = [(w, weekly.get(w.isoformat(), 0.0)) for w in weeks
                if w + _dt.timedelta(days=6) <= covered_to and w + _dt.timedelta(days=6) < today]
    basis = complete[-basis_weeks:]
    rate = sum(v for _, v in basis) / len(basis) if basis else 0.0
    # The episodes the run-rate is made of: weekly-limit hits with extra usage
    # inside the basis weeks (older or part-seen weeks aren't averaged in).
    basis_starts = {w.isoformat() for w, _ in basis}
    episodes = sum(1 for row in limits
                   if row.get("limit_type") == "weekly" and row.get("hit_at")
                   and _num(row.get("usage_after_hit_usd")) > 0
                   and _monday(row["hit_at"]) in basis_starts)
    # A snapshot of the allowance describes the week of the export, so it says
    # something about this week only for people whose export ends in it.
    this_week = _monday(today.isoformat())
    current = [p for p in merged["people"] if p.get("window_to") and _monday(p["window_to"]) == this_week]
    return {
        "weekly": series,
        "basis_weeks": len(basis),
        "basis_episodes": episodes,
        "weekly_run_rate_usd": round(rate, 2),
        "next_4_weeks_usd": round(rate * 4, 2),
        "next_13_weeks_usd": round(rate * 13, 2),
        "people_on_pace_to_hit": [p["person"] for p in current if p.get("allowance_status") == "on_pace_to_hit"],
        "people_over": [p["person"] for p in current if p.get("allowance_status") == "over"],
    }


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #

# Usage-health columns the team view shows, per person and next to the team
# median (docs/team.md, "Habits by person").
_HEALTH_TEAM_COLUMNS = (
    "est_cost_usd", "est_cost_main_usd", "sessions_started", "long_context_cost_usd", "cold_cache_usd", "prompts",
    "interrupts", "auto_compactions", "tool_refusals", "error_streaks", "sessions_edited",
    "sessions_checked_after_edit", "sessions_plan_mode", "sessions_with_subagents",
    "sessions_instructions_recorded", "sessions_with_project_claude_md",
)


def _load(paths, seat_prices=None):
    exports, problems = read_exports(paths)
    prices, price_problems = load_seat_prices(paths, seat_prices)
    return merge(exports, list(problems) + price_problems, prices, skipped=problems.skipped,
                 prices_rejected=bool(price_problems))


def team_payload(paths, today=None, seat_prices=None):
    """Everything the team dashboard needs, merged fresh from `paths`."""
    merged = _load(paths, seat_prices)
    files = merged["files"]

    def rows(kind):
        return (files.get(kind) or ([], []))[1]

    def maybe(value):
        """A number, or None for a blank cell: blank is "not recorded"."""
        return None if value in (None, "") else _num(value)

    names = {p["person"]: p.get("display_name") or p["person"] for p in merged["people"]}

    def repo_of(r):
        """The repository a row is spend in: the folder's git remote. A folder that
        isn't a git repo has only its own path (`~/work/app`), which two people can
        both have for unrelated folders, so it is named with whose it is."""
        repo = r.get("project_repo")
        if repo:
            return repo
        project = r.get("project") or ""
        if not project or project == UNKNOWN_PROJECT:
            return project
        return f"{project} (folder, {names.get(r['person'], r['person'])})"

    weekly = [[r["person"], r.get("week_start"), repo_of(r),
               _num(r.get("est_cost_usd")), _num(r.get("total_tokens")), _num(r.get("requests")),
               _num(r.get("sessions_active")), _num(r.get("active_days")),
               r.get("partial_week") == "yes", r.get("cli_history_complete") == "no",
               maybe(r.get("lines_added")), maybe(r.get("lines_removed")),
               _num(r.get("cost_with_line_data_usd")), _num(r.get("sessions_started")),
               maybe(r.get("week_days_in_window")),
               _num(r.get("est_cost_subagent_usd")), _num(r.get("api_retries")),
               maybe(r.get("week_active_days")), r.get("_machine") or ""]
              for r in rows("summary")]
    # What the sessions that started in each week cost in all: the whole of each
    # session, so it is what `cost_with_line_data_usd` (also whole sessions) is a
    # share of. The week's own cost counts requests by their day, so it isn't.
    started_cost = defaultdict(float)
    for r in rows("sessions"):
        day = (r.get("started_at") or "")[:10]
        try:
            started_cost[(r["person"], _monday(day))] += _num(r.get("est_cost_usd"))
        except ValueError:
            continue
    session_costs = [[person, week, round(cost, 6)] for (person, week), cost in sorted(started_cost.items())]
    models = [[r["person"], r.get("week_start"), r.get("model_family") or "other", r.get("thread") or "main",
               _num(r.get("est_cost_usd")), _num(r.get("output_tokens")), _num(r.get("thinking_tokens")),
               r.get("model") or ""]
              for r in rows("breakdown")]
    limits = [{
        "person": r["person"], "limit_type": r.get("limit_type"), "hit_at": r.get("hit_at"), "resets_at": r.get("resets_at"),
        "locked_out": r.get("locked_out") == "yes", "blocked_hours": _num(r.get("blocked_hours")),
        "usage_before_hit_usd": _num(r.get("usage_before_hit_usd"), None),
        "usage_after_hit_usd": _num(r.get("usage_after_hit_usd"), None),
    } for r in rows("limits")]
    prs = [{
        "person": r["person"], "pr_url": r.get("pr_url"), "pr_repo": r.get("pr_repo") or "",
        "opened_on": r.get("opened_on"), "session_id": r.get("session_id"),
        "in_project_repo": r.get("in_project_repo") or "",
        # Blank is "not recorded", not a free session.
        "session_cost_usd": maybe(r.get("session_cost_usd")),
        "session_prs": _num(r.get("session_prs"), 1.0) or 1.0,
    } for r in rows("prs")]
    # Only rows from exports that carry the schema-7 health columns: an older
    # export's blank is "not measured", and must not count as a zero.
    health_rows = [dict({"person": r["person"], "week": r.get("week_start"),
                         "very_long_context_cost_usd": maybe(r.get("very_long_context_cost_usd"))},
                        **{c: _num(r.get(c)) for c in _HEALTH_TEAM_COLUMNS})
                   for r in rows("summary") if r.get("prompts") not in (None, "")]
    tools = [[r["person"], r.get("week_start"), r.get("tool") or "", r.get("integration") or "built-in",
              _num(r.get("calls")), _num(r.get("errors"))]
             for r in rows("tools")]
    return {
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "folder": [os.path.abspath(p) for p in paths],
        "people": merged["people"],
        "sources": merged["sources"],
        "warnings": merged["warnings"],
        "weekly": weekly,
        "session_costs": session_costs,
        "skipped": merged.get("skipped") or [],
        "models": models,
        "limits": limits,
        "prs": prs,
        "prices": merged["prices"],
        "forecast": overage_forecast(merged, today),
        "health": {"rows": health_rows},
        "tools": tools,
    }


def _csv_text(columns, rows):
    buf = io.StringIO()
    # Names, titles, branches and folders are people's own text, and so are
    # the column names of a hand-made file; see csv_safe.
    csv.writer(buf).writerow([csv_safe(c) for c in columns])
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writerows({k: csv_safe(v) for k, v in row.items()} for row in rows)
    return buf.getvalue()


def merged_files(merged, today=None):
    """{filename: csv text} for every merged output, plus the report."""
    out = {}
    for kind, (columns, rows) in merged["files"].items():
        out[f"team-{kind}.csv"] = _csv_text(columns, rows)
    out["team-people.csv"] = _csv_text(PEOPLE_COLUMNS, merged["people"])
    out["merge-report.txt"] = report_text(merged, today)
    return out


def report_text(merged, today=None):
    lines = ["Claude Code usage -- team merge report", ""]
    lines.append(f"{len(merged['people'])} people from {len(merged['sources'])} export(s)")
    lines.append("")
    for s in merged["sources"]:
        lines.append(f"  {s['person']}: {s['source']} (schema {s['schema_version']}, "
                     f"exported {s['exported_at']}, window {s['window_from']} .. {s['window_to']}, "
                     f"files: {', '.join(s['files'])})")
    lines.append("")
    forecast = overage_forecast(merged, today)
    if forecast:
        # One episode (or none) is too little to project: a single spike would
        # read as a steady weekly cost.
        episodes = forecast["basis_episodes"]
        if not forecast["basis_weeks"]:
            lines.append("Extra usage run-rate: no complete week in the exports yet.")
        elif episodes > 1:
            lines.append(f"Extra usage run-rate: ${forecast['weekly_run_rate_usd']:,.2f}/week from "
                         f"{episodes} episodes over the last {forecast['basis_weeks']} complete week(s) "
                         f"-> about ${forecast['next_4_weeks_usd']:,.0f} per 4 weeks, "
                         f"${forecast['next_13_weeks_usd']:,.0f} per quarter "
                         "(list price; a run-rate, not a prediction).")
        elif episodes == 1:
            lines.append("One episode so far: too little to project a run-rate. "
                         "(Extra usage only happens in weeks someone runs out of allowance.)")
        else:
            lines.append("No extra usage so far: too little to project a run-rate. "
                         "(Extra usage only happens in weeks someone runs out of allowance.)")
        # Only people whose export ends in the current week: an older snapshot
        # says nothing about this week. Over and on pace are different news.
        by_person = {p["person"]: p for p in merged["people"]}

        def as_of(people):
            return ", ".join(f"{who} (as of their export, {by_person[who]['latest_export_at'][:10]})"
                             for who in people)
        if forecast["people_over"]:
            lines.append("Already over the weekly limit this week: " + as_of(forecast["people_over"]))
        if forecast["people_on_pace_to_hit"]:
            lines.append("On pace to hit the weekly limit this week: " + as_of(forecast["people_on_pace_to_hit"]))
        lines.append("")
    valued = [p for p in merged["people"] if p.get("value_multiple") is not None]
    if valued:
        lines.append(f"Value per seat ({merged['prices']['source']}, USD): list-price usage / "
                     "(seat cost + extra usage) over the days each person's exports cover")
        for p in sorted(valued, key=lambda p: -p["value_multiple"]):
            lines.append(f"  {p['person']}: {p['value_multiple']:.2f}x "
                         f"(${p['est_cost_usd']:,.0f} usage vs ${p['seat_cost_in_window']:,.0f} seat"
                         f" + ${p['extra_usage_usd']:,.0f} extra)")
        lines.append("")
    if merged["warnings"]:
        lines.append("Notes:")
        for w in merged["warnings"]:
            lines.append(f"  [{w['level']}] {w['message']}")
    else:
        # No claim that exports are current: how old they are depends on the day
        # this is read, which the files can't say. The dashboard's "How current" does.
        lines.append("Notes: none.")
    return "\n".join(lines) + "\n"


def _merged_bytes(name, text):
    """A merged output as bytes. The team CSVs lead with a UTF-8 byte-order mark
    so Excel on Windows reads them as UTF-8, not ANSI. The per-person exports
    stay BOM-free so `cat`-joining them still works."""
    return text.encode("utf-8-sig" if name.endswith(".csv") else "utf-8")


def write_merged(paths, out_dir, seat_prices=None):
    merged = _load(paths, seat_prices)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, text in merged_files(merged).items():
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(_merged_bytes(name, text))
        written.append(path)
    return merged, written


def zip_merged(paths, seat_prices=None):
    merged = _load(paths, seat_prices)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in merged_files(merged).items():
            archive.writestr(name, _merged_bytes(name, text))
    return buf.getvalue()
