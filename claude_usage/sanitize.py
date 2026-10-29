"""Coercions for fields read from files this tool does not control. A wrong-typed,
non-finite or absurdly large value costs its own field (it reads as missing or
zero), never the scan."""
from __future__ import annotations

import math

# The largest count or amount taken at face value. A quadrillion tokens, dollars
# or milliseconds is far beyond anything real, while a value near float's limit
# overflows the arithmetic downstream (int / 1e6, round(), sums of two such
# values). Anything above it is garbage, and reads as zero.
_MAX = 10 ** 15


def tok(v) -> int:
    """A token count: a non-negative int no larger than 10**15. Accepts an int,
    or a float that is finite, non-negative and whole. A bool, string, NaN,
    infinity, negative or absurdly large value is 0, so one wrong-typed field
    costs its own count and not the scan."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v if 0 <= v <= _MAX else 0
    if isinstance(v, float) and 0 <= v <= _MAX and v.is_integer():  # False for NaN
        return int(v)
    return 0


def num(v) -> float:
    """The signed variant: a finite float no larger than 1e15 in magnitude, else
    0.0. A negative value passes, so durations and costs read from logs use
    `ms()` / `money()`, which also clamp below zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    # Compared before converting: float() of a 401-digit int raises OverflowError.
    return float(v) if abs(v) <= _MAX else 0.0  # False for NaN


def money(v) -> float:
    """A cost the CLI reports: `num`, but never below zero (a negative one is a
    damaged record, and would make the exported file fail the team merge)."""
    n = num(v)
    return n if n > 0 else 0.0


def ms(v):
    """A duration in milliseconds: an int when the value is whole, so valid
    input reads back as it was written, else a float; 0.0 for anything `num`
    rejects, and for a negative one (time doesn't run backwards). (Not `tok`: a
    real duration can be fractional.)"""
    n = money(v)
    return int(n) if n.is_integer() else n


def finite_json(obj):
    """`obj` with every non-finite float replaced by None. json.dumps writes
    NaN and Infinity as bare literals, which a browser's JSON.parse rejects --
    one bad number would blank the whole dashboard. The last guard before a
    payload is written; the parser and aggregator keep them out earlier."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite_json(v) for v in obj]
    return obj


def sid(v):
    """An id or name: a non-empty string, else None -- the same as a missing
    field, so each caller's existing fallback applies."""
    return v if isinstance(v, str) and v else None


def counter_key(v):
    """A cost-state startTime, which identifies the CLI's running counter. Real
    logs write it as a number (epoch ms) and some records as a string; either
    is usable as a grouping key. Anything else (a list or dict is unhashable, a
    value beyond any real timestamp is garbage) reads as missing."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if abs(v) <= _MAX else None  # False for NaN
    return sid(v)
