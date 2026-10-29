"""Usage-limit episodes and allowance burn, from Claude Code's own limit notices.

On a seat plan the binding constraint is the usage allowance, not dollars.
Claude Code writes a notice into the transcript when a limit stops a request --
"You've hit your weekly limit · resets Oct 2 at 4am (America/New_York)". Nothing
else in the logs measures the allowance: the ``rateLimits`` field on API error
records is always empty. So this module works from those notices alone.

Privacy: the parser hands this module the notice text of records Claude Code
flags ``isApiErrorMessage`` with a rate-limit error, and nothing else. Only the
limit type and the reset time are kept; the text is discarded.

Pure: no file I/O.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import re

WEEKLY = "weekly"
FIVE_HOUR = "five_hour"
SPEND_CAP = "spend_cap"
OPUS = "opus"
OTHER = "other"

# How long each limit's window runs, for "how much did the allowance absorb
# before it ran out". The spend cap has no window of its own -- it is reached
# while running on extra usage, inside a weekly window.
WINDOW_HOURS = {WEEKLY: 7 * 24, OPUS: 7 * 24, FIVE_HOUR: 5}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# A reset is "[<date> ][at ]<time>[ (<zone>)]". The date is "Oct 2", "Oct 2nd" or
# "tomorrow", optionally followed by a comma. Claude Code writes "Oct 2 at 4am"
# and "3pm"; the rest is accepted in case the wording drifts.
_RESET_RE = re.compile(
    r"resets\s+(?:(?:(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"|(?P<tomorrow>tomorrow))(?:\s*,)?\s+)?(?:at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)

# US zone abbreviations name a zone, not an offset: "(EST)" in July means New
# York time, which is EDT. ZoneInfo("EST") is a fixed UTC-5 and would be an hour out.
_ABBREVIATIONS = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
}

# A weekly window is 7 days; a dated reset further out than this isn't one.
_MAX_AHEAD = _dt.timedelta(days=8)


def classify(text: str) -> str:
    t = (text or "").lower()
    # Spend cap first: its notice also mentions when the weekly limit resets.
    if "spend limit" in t or "spending limit" in t:
        return SPEND_CAP
    if "opus" in t:
        return OPUS
    if "weekly" in t:
        return WEEKLY
    if "5-hour" in t or "5 hour" in t or "five-hour" in t or "session limit" in t:
        return FIVE_HOUR
    return OTHER


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _zone(name):
    """The named IANA zone, or None for the machine's local zone.

    None also stands for a zone that can't be loaded (Windows without the tzdata
    package has no zone database). The notice names the user's own zone, so local
    time is nearly always the same answer.
    """
    key = (name or "").strip()
    if key.upper() in ("UTC", "ETC/UTC", "GMT", "Z"):
        return _dt.timezone.utc
    if key:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(_ABBREVIATIONS.get(key.upper(), key))
        except Exception:
            pass
    return None


def _wall_clock(naive, tz):
    """The UTC moment a naive wall-clock time is in ``tz`` (None: machine local).

    Local time goes through the C library, which applies the daylight-saving
    offset in force on that date -- not today's offset, as a fixed zone taken from
    ``now()`` would.
    """
    aware = naive.astimezone() if tz is None else naive.replace(tzinfo=tz)
    return aware.astimezone(_dt.timezone.utc)


def parse_reset(text: str, event_ts: str):
    """ISO UTC time the limit resets, or None if the notice doesn't say.

    The notice gives a wall-clock time in a named zone, with a date only when
    the reset isn't within the day ("resets 3pm" vs "resets Oct 2 at 4am"). The
    year is never given: it is the first such moment after the notice, and a
    dated reset that doesn't land within a weekly window of the notice is not a
    reset time at all.
    """
    m = _RESET_RE.search(text or "")
    event = _parse_iso(event_ts)
    if not m or event is None:
        return None
    hour12 = int(m.group("hour"))
    if not 1 <= hour12 <= 12:
        return None
    hour = hour12 % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    minute = int(m.group("minute") or 0)
    tz = _zone(m.group("tz"))
    try:
        event = event.astimezone(_dt.timezone.utc)
        # The notice's own wall clock: "tomorrow" and the year are read in the zone.
        local = event.astimezone(tz) if tz is not None else event.astimezone()
        local = local.replace(tzinfo=None)
        if m.group("mon") or m.group("tomorrow"):
            if m.group("tomorrow"):
                day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
                when = (day_start + _dt.timedelta(days=1)).replace(hour=hour, minute=minute)
            else:
                month = _MONTHS.get(m.group("mon").lower()[:3])
                if not month:
                    return None
                when = _dt.datetime(local.year, month, int(m.group("day")), hour, minute)
            moment = _wall_clock(when, tz)
            if moment <= event:
                moment = _wall_clock(when.replace(year=when.year + 1), tz)
            if moment - event > _MAX_AHEAD:
                return None
        else:
            when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            moment = _wall_clock(when, tz)
            if moment <= event:
                moment = _wall_clock(when + _dt.timedelta(days=1), tz)
        return moment.isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        # OverflowError/OSError: a date at the edge of the calendar
        return None


def parse_notice(text: str, event_ts: str):
    """(limit kind, reset ISO or None). The only thing kept from a notice."""
    return classify(text), parse_reset(text, event_ts)


# --------------------------------------------------------------------------- #
# episodes
# --------------------------------------------------------------------------- #

class _Requests:
    """Successful requests in time order, for "what happened around a hit"."""

    def __init__(self, requests):
        pairs = sorted((t, c) for t, c in ((_parse_iso(ts), cost) for ts, cost in requests) if t)
        self.times = [t for t, _ in pairs]
        self.costs = [c for _, c in pairs]
        self.cum = [0.0]
        for c in self.costs:
            self.cum.append(self.cum[-1] + c)

    def cost_between(self, start, end):
        """Cost of requests with start < t < end."""
        i = bisect.bisect_right(self.times, start)
        j = bisect.bisect_left(self.times, end)
        return (self.cum[j] - self.cum[i]) if j > i else 0.0, max(0, j - i)

    def first_after(self, moment):
        i = bisect.bisect_right(self.times, moment)
        return self.times[i] if i < len(self.times) else None


def _iso(moment):
    return moment.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z") if moment else None


def build_episodes(events, requests, now=None):
    """Collapse limit notices into episodes and measure each one.

    events:   [{"ts", "kind", "resets_at", "session_id"}], already de-duplicated
    requests: [(ts, cost)] for every successful request, de-duplicated

    Claude Code repeats the notice on every attempt while a limit is in force,
    so one episode is every notice of the same kind with the same reset time.
    Notices of different kinds interleave -- the weekly limit, then the spend
    cap while on extra usage, then the weekly notice again -- so each notice is
    matched against the latest episode of its own kind, not the latest overall.

    An episode is a **lock-out** when no request succeeded between its last
    notice and the reset: the person could not use Claude Code at all until
    then, so blocked_hours runs from the first notice to the reset. Otherwise
    requests got through -- typically as extra usage after the weekly limit --
    and blocked_hours is only how long the notices kept coming. "Until the next
    successful request" would be wrong here: someone who hits a limit at 5pm
    and resumes at 9am was away for the night, not blocked for sixteen hours.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    reqs = _Requests(requests)
    episodes = []
    open_by_kind = {}  # kind -> its latest episode
    timed = [(ts, ev) for ts, ev in ((_parse_iso(ev.get("ts")), ev) for ev in events) if ts]
    for ts, ev in sorted(timed, key=lambda pair: str(pair[1]["ts"])):
        last = open_by_kind.get(ev["kind"])
        same = (last is not None
                and (last["resets_at"] == ev["resets_at"] if ev["resets_at"] or last["resets_at"]
                     else ts - last["_last"] < _dt.timedelta(hours=6)))
        if same:
            last["_last"] = ts
            last["notices"] += 1
            continue
        episode = {"kind": ev["kind"], "resets_at": ev["resets_at"],
                   "session_id": ev.get("session_id"),
                   "_first": ts, "_last": ts, "notices": 1}
        episodes.append(episode)
        open_by_kind[ev["kind"]] = episode

    out = []
    for ep in episodes:
        hit, reset = ep["_first"], _parse_iso(ep["resets_at"])
        resumed = reqs.first_after(ep["_last"])
        if reset is not None and resumed is not None and resumed > reset:
            resumed = None  # answered only after the reset: a lock-out
        locked_out = reset is not None and resumed is None
        end = min(reset, now) if locked_out else ep["_last"]
        blocked = max(0.0, (end - hit).total_seconds() / 3600)

        before = after = None
        after_requests = 0
        window = WINDOW_HOURS.get(ep["kind"])
        if reset is not None and window:
            # What the allowance absorbed before it ran out, at list price...
            before, _ = reqs.cost_between(reset - _dt.timedelta(hours=window), hit)
            # ...and what ran after it, before the reset: extra usage, if on.
            after, after_requests = reqs.cost_between(hit, min(reset, now))
        out.append({
            "kind": ep["kind"],
            "hit_at": _iso(hit),
            "last_notice_at": _iso(ep["_last"]),
            "resets_at": ep["resets_at"],
            "notices": ep["notices"],
            "session_id": ep["session_id"],
            "resumed_at": _iso(resumed),
            "locked_out": locked_out,
            "blocked_hours": round(blocked, 2),
            "usage_before_hit_usd": round(before, 6) if before is not None else None,
            "usage_after_hit_usd": round(after, 6) if after is not None else None,
            "requests_after_hit": after_requests if after is not None else None,
            "ongoing": bool(reset is not None and reset > now),
        })
    return out


# --------------------------------------------------------------------------- #
# allowance burn
# --------------------------------------------------------------------------- #

def allowance_snapshot(episodes, requests, now=None):
    """Where this week stands against the level at which the weekly limit was
    hit before -- a warning before the lock-out rather than after it.

    The allowance is never reported directly, so it is estimated from what
    Claude Code had used, at list price, when the weekly limit last ran out.
    Returns None when the weekly limit has never been hit: there is nothing to
    calibrate against.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    weekly = [e for e in episodes if e["kind"] == WEEKLY
              and e["resets_at"] and e["usage_before_hit_usd"]]
    if not weekly:
        return None
    latest = max(weekly, key=lambda e: e["hit_at"])
    allowance = latest["usage_before_hit_usd"]

    # The weekly limit resets at the same moment every week; step from the
    # last known reset to the window containing now.
    anchor = _parse_iso(latest["resets_at"])
    week = _dt.timedelta(days=7)
    end = anchor
    while end <= now:
        end += week
    while end - week > now:
        end -= week
    start = end - week

    used, _ = _Requests(requests).cost_between(start - _dt.timedelta(microseconds=1), now)
    elapsed_h = max((now - start).total_seconds() / 3600, 1e-9)
    projected = None
    status = "on_track"
    if used >= allowance:
        status = "over"
    elif used > 0:
        rate = used / elapsed_h
        hours_left = (allowance - used) / rate
        hit_at = now + _dt.timedelta(hours=hours_left)
        if hit_at < end:
            projected = _iso(hit_at)
            status = "on_pace_to_hit"
    return {
        "allowance_est_usd": round(allowance, 2),
        "calibrated_from": latest["hit_at"],
        "week_start": _iso(start),
        "week_end": _iso(end),
        "used_usd": round(used, 2),
        "used_pct": round(used / allowance, 4) if allowance else None,
        "projected_hit_at": projected,
        "status": status,
    }
