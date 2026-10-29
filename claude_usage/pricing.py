"""Pricing table and cost calculation for Claude API usage.

Rates are $ per million tokens, from https://claude.com/pricing (last checked on
``PRICES_CHECKED``).

Rates are dated. Each model has a list of ``(effective_from, rates)`` rows, oldest
first, and a request is priced by the row in force on its own (UTC) date. So a
price change is one new row: usage before it keeps the old rate, usage after gets
the new one, and history never moves. Never edit a past row -- a test pins them.
A request older than a model's first row is priced at that first row (the table
starts on the day it was first verified, not on the day each model launched).

Two documented multipliers are applied on top of the base table:

* ``ASSUMED_1H_MULTIPLIER`` -- the 1-hour cache-write rate. Anthropic's published
  convention is 2x the base input rate; it is kept as a named constant so the UI
  can surface it as an assumption rather than a quoted rate.
* ``FAST_MODE_MULTIPLIER`` -- fast mode (``usage.speed == "fast"``, Claude Code's
  ``/fast``) runs the same model at premium pricing. The published input/output
  pairs are exactly 2x standard (Opus 5: $10/$50 vs $5/$25; Opus 5.5: $8/$40 vs
  $4/$20), so the whole rate row is scaled by 2. Only Opus 5.5 / 5 / 4.8 offer
  fast mode; ``speed`` on any other model is ignored.

One model is priced by prompt length: Claude Haiku 5.5 charges more on every
category, output included, once a request's prompt -- fresh input plus cache
reads plus cache writes -- is over 100,000 tokens. Its row carries a
``long_prompt`` block with the threshold and the higher rates, and each request
is priced on its own prompt length.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import re

from .sanitize import tok as _tok

ASSUMED_1H_MULTIPLIER = 2.0
FAST_MODE_MULTIPLIER = 2.0

# The day someone last compared the table below against claude.com/pricing.
# Bump it after every check, whether or not anything changed; once it is more
# than PRICES_STALE_AFTER_DAYS old, the dashboard and collect.py say so.
PRICES_CHECKED = "2026-10-09"
PRICES_STALE_AFTER_DAYS = 90

# Models for which fast mode exists at all (research preview, Claude API only).
FAST_MODE_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"})

# model -> [(effective_from "YYYY-MM-DD", rates), ...], oldest first.
# rates: input, output, cache_read, cache_write_5m ($/MTok). cache_write_1h is
# derived as input * ASSUMED_1H_MULTIPLIER unless the row sets it explicitly.
# A row may add "long_prompt": {"over": tokens, "rates": {...}} -- the rates for
# a request whose prompt is over that many tokens (see the module docstring).
# To record a price change, append a row dated the day it took effect; never
# edit or delete an existing row (tests/test_pricing.py pins them).
_V1 = "2026-10-02"
RATE_HISTORY = {
    # Corrected 2026-10-07 from 0.20: Sonnet 5.5, like Opus 5.5, reads from the
    # cache at 0.05x input, not the usual 0.1x. A wrong row, not a price change,
    # so the row itself is fixed (found by the live price check).
    "claude-sonnet-5-5": [(_V1, {"input": 2.00,  "output": 10.00, "cache_read": 0.10, "cache_write_5m": 2.50})],
    "claude-sonnet-5":   [(_V1, {"input": 2.00,  "output": 10.00, "cache_read": 0.20, "cache_write_5m": 2.50})],
    "claude-sonnet-4-6": [(_V1, {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-opus-5-5":   [(_V1, {"input": 4.00,  "output": 20.00, "cache_read": 0.20, "cache_write_5m": 5.00})],
    "claude-opus-5":     [(_V1, {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-8":   [(_V1, {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-7":   [(_V1, {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-6":   [(_V1, {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-fable-5-1":  [(_V1, {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write_5m": 12.50})],
    "claude-fable-5":    [(_V1, {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write_5m": 12.50})],
    "claude-mythos-5-1": [(_V1, {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write_5m": 12.50})],
    "claude-mythos-5":   [(_V1, {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write_5m": 12.50})],
    "claude-haiku-4-5":  [(_V1, {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write_5m": 1.25})],
    # Older models, added 2026-10-07 when the live check showed them published
    # but unpriced here: anyone still using one would have been shown $0.
    "claude-opus-4-5":   [(_V1, {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-sonnet-4-5": [(_V1, {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-opus-4-1":   [(_V1, {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write_5m": 18.75})],
    "claude-opus-4":     [(_V1, {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write_5m": 18.75})],
    "claude-sonnet-4":   [(_V1, {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-haiku-3-5":  [(_V1, {"input": 0.80,  "output": 4.00,  "cache_read": 0.08, "cache_write_5m": 1.00})],
    # Added 2026-10-09: priced by prompt length, so it had been left out and
    # every Haiku 5.5 request was shown at $0.
    "claude-haiku-5-5":  [("2026-10-09", {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write_5m": 0.125,
                                          "long_prompt": {"over": 100_000, "rates": {
                                              "input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write_5m": 0.625}}})],
}


def _full_rates(rates: dict) -> dict:
    flat = {k: v for k, v in rates.items() if k != "long_prompt"}
    return {"cache_write_1h": rates["input"] * ASSUMED_1H_MULTIPLIER, **flat}


def _long_prompt(rates: dict):
    """(threshold, full rate row) for a row priced by prompt length, else None."""
    long = rates.get("long_prompt")
    return (long["over"], _full_rates(long["rates"])) if long else None


def _build_schedule(history: dict) -> dict:
    """model -> (effective_from dates, matching full rate rows, matching
    long-prompt (threshold, rates) or None), for bisect."""
    return {
        model: ([day for day, _ in rows], [_full_rates(rates) for _, rates in rows],
                [_long_prompt(rates) for _, rates in rows])
        for model, rows in history.items()
    }


_SCHEDULE = _build_schedule(RATE_HISTORY)


def _today() -> str:
    return _dt.date.today().isoformat()


def _row_index(days: list, day: str | None) -> int:
    """Index of the row in force on `day` ("YYYY-MM-DD"), or today when None.
    A row dated in the future is not in force yet; a day before the first row
    gets the first row."""
    if day is None:
        day = str(_today())
    return max(bisect.bisect_right(days, day) - 1, 0)


def current_rates() -> dict:
    """model -> the rate row in force today."""
    return {model: rows[_row_index(days, None)] for model, (days, rows, _) in _SCHEDULE.items()}


def current_long_prompt_rates() -> dict:
    """model -> (threshold, rate row) in force today, for prompt-length-priced models."""
    out = {}
    for model, (days, _, longs) in _SCHEDULE.items():
        long = longs[_row_index(days, None)]
        if long:
            out[model] = long
    return out


# The rates in force when this module was loaded, per model: a snapshot, frozen
# at import. rates_for and current_rates() decide "today" afresh on every call,
# so a long-running server crosses a price change; anything that must follow
# today's rates (price_check) calls current_rates() rather than reading this.
PRICING = current_rates()


def prices_age_days(today: _dt.date | None = None) -> int:
    """Days since the table was last checked against the published prices."""
    today = today or _dt.date.today()
    return (today - _dt.date.fromisoformat(PRICES_CHECKED)).days


def prices_stale(today: _dt.date | None = None) -> bool:
    return prices_age_days(today) > PRICES_STALE_AFTER_DAYS


def _utc_day(ts) -> str | None:
    """"YYYY-MM-DD" (UTC) of a transcript timestamp, or None if unreadable.
    Transcripts write UTC with a "Z", so the common case is a slice."""
    if not ts:
        return None
    ts = str(ts)
    if ts.endswith("Z") and len(ts) >= 10:
        return ts[:10]
    try:
        moment = _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(_dt.timezone.utc)
    return moment.date().isoformat()

# Strips a trailing "[1m]"-style context-window suffix and a trailing
# "-YYYYMMDD" date suffix, e.g. "claude-haiku-4-5-20251001" -> "claude-haiku-4-5",
# "claude-opus-5-5[1m]" -> "claude-opus-5-5". The current model generation prices
# its full 1M context at a single rate, so the bracket suffix carries no premium.
_BRACKET_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
# Cloud-provider forms of the same ids. Bedrock: an optional region prefix,
# "anthropic.", and a "-v1:0" version ("us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
# sometimes inside an inference-profile ARN. Vertex: "@YYYYMMDD"
# ("claude-sonnet-4-5@20250929"). The region prefix may hold hyphens
# ("us-gov.anthropic.claude-...").
_ARN_PREFIX_RE = re.compile(r"^arn:[^/]*/")
_BEDROCK_PREFIX_RE = re.compile(r"^(?:[a-z-]+\.)?anthropic\.")
_BEDROCK_VERSION_RE = re.compile(r"-v\d+(?::\d+)?$")
_VERTEX_DATE_RE = re.compile(r"@\d{8}$")
# Claude 3.x put the family after the version: "claude-3-5-haiku" is the
# table's "claude-haiku-3-5".
_LEGACY_ORDER_RE = re.compile(r"^claude-(\d+(?:-\d)?)-(opus|sonnet|haiku)$")
# Aliases of a dated model: "-latest" points at the newest snapshot, and the
# first Claude 4 models were also published as "claude-opus-4-0"/"claude-sonnet-4-0".
# (Claude 3.7 Sonnet isn't on the live pricing page, so it has no row to map to
# and stays unpriced and flagged, as does any id that names no model.)
_LATEST_RE = re.compile(r"-latest$")
_FOUR_ZERO_RE = re.compile(r"^(claude-(?:opus|sonnet))-4-0$")


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def normalize_model(model: str) -> str:
    if not isinstance(model, str) or not model:
        return model
    model = _BRACKET_SUFFIX_RE.sub("", model)
    model = _ARN_PREFIX_RE.sub("", model)
    model = _BEDROCK_PREFIX_RE.sub("", model)
    model = _BEDROCK_VERSION_RE.sub("", model)
    model = _VERTEX_DATE_RE.sub("", model)
    model = _DATE_SUFFIX_RE.sub("", model)
    model = _LATEST_RE.sub("", model)
    model = _FOUR_ZERO_RE.sub(r"\1-4", model)
    model = _LEGACY_ORDER_RE.sub(r"claude-\2-\1", model)
    return model


def prompt_tokens(usage: dict) -> int:
    """A request's prompt length as Anthropic counts it for prompt-length
    pricing: fresh input plus cache reads plus cache writes."""
    cache_creation = _dict(usage.get("cache_creation"))
    writes = (_tok(cache_creation.get("ephemeral_5m_input_tokens"))
              + _tok(cache_creation.get("ephemeral_1h_input_tokens"))) \
        or _tok(usage.get("cache_creation_input_tokens"))
    return (_tok(usage.get("input_tokens")) + _tok(usage.get("cache_read_input_tokens"))
            + writes)


def rates_for(model: str, speed: str | None = None, ts: str | None = None,
              prompt_tokens: int | None = None):
    """Rate row for a model as of the request's timestamp `ts`, scaled for fast
    mode when the log says the request was served that way and the model
    actually offers fast mode. Without a readable `ts`, today's rates apply; a
    `ts` before the model's first row gets the first row. For a model priced
    by prompt length, `prompt_tokens` over its threshold selects the higher
    rates."""
    if not isinstance(model, str):
        return None
    name = normalize_model(model)
    schedule = _SCHEDULE.get(name)
    if not schedule:
        return None
    days, rows, longs = schedule
    i = _row_index(days, _utc_day(ts))
    rates = rows[i]
    if longs[i] and prompt_tokens and prompt_tokens > longs[i][0]:
        rates = longs[i][1]
    if speed == "fast" and name in FAST_MODE_MODELS:
        return {k: v * FAST_MODE_MULTIPLIER for k, v in rates.items()}
    return rates


def cost_for(model: str, usage: dict, unknown_models: set | None = None,
             ts: str | None = None, summed: bool = False) -> float:
    """Cost in USD for one usage record, at the rates in force on `ts` (see
    rates_for). Records unknown model names into `unknown_models` (if given)
    and returns 0.0 for them rather than raising.

    Honours `usage["speed"] == "fast"` (premium fast-mode pricing) and
    `usage["service_tier"] == "batch"` (Batch API, billed at 50%).

    `summed`: the usage adds up many requests, so its prompt length says
    nothing about any one of them, and a prompt-length tier isn't applied."""
    rates = rates_for(model, usage.get("speed"), ts, None if summed else prompt_tokens(usage))
    if not rates:
        if unknown_models is not None and isinstance(model, str):
            unknown_models.add(model)
        return 0.0

    cache_creation = _dict(usage.get("cache_creation"))
    cw_5m = _tok(cache_creation.get("ephemeral_5m_input_tokens"))
    cw_1h = _tok(cache_creation.get("ephemeral_1h_input_tokens"))
    if not cw_5m and not cw_1h:
        # No breakdown available -- treat the whole cache-write amount as 5m.
        cw_5m = _tok(usage.get("cache_creation_input_tokens"))

    cost = (
        _tok(usage.get("input_tokens")) / 1_000_000 * rates["input"]
        + _tok(usage.get("output_tokens")) / 1_000_000 * rates["output"]
        + _tok(usage.get("cache_read_input_tokens")) / 1_000_000 * rates["cache_read"]
        + cw_5m / 1_000_000 * rates["cache_write_5m"]
        + cw_1h / 1_000_000 * rates["cache_write_1h"]
    )
    if usage.get("service_tier") == "batch":
        cost *= 0.5
    return cost
