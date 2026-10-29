"""Check the rate table against Anthropic's published prices.

Anthropic publishes its API pricing as Markdown (``PRICING_URL``), with one row
per model. This module fetches that page, reads the "Model pricing" table, and
compares it with the rates ``pricing.py`` has in force today.

It **reports**; it never changes a price. Costs keep coming from the dated table
in pricing.py, so two people's exports of the same month always agree and
history never moves. A difference is surfaced on the dashboard and by
collect.py so someone can add a dated row.

Network behaviour, deliberately narrow:

* one HTTPS GET to a public page, carrying nothing about your usage;
* at most once a day -- the result is cached in ``price-check.json`` in the
  tool's data folder (see paths.py);
* a short timeout, and any failure (offline, proxy, page changed) is reported
  as "couldn't check", never as "prices match", and never stops a run;
* off entirely with ``--offline`` or ``CLAUDE_USAGE_OFFLINE=1``.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
from pathlib import Path

from . import paths, pricing
from .sanitize import sid

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
TIMEOUT_SECONDS = 5
CACHE_HOURS = 24
FAILED_RETRY_HOURS = 1
# Tests point this elsewhere; None means the data folder.
CACHE_FILE = None
OFFLINE_ENV = "CLAUDE_USAGE_OFFLINE"

# Published column header (lower-cased, prefix match) -> our rate field.
_COLUMNS = {
    "base input": "input",
    "5m cache write": "cache_write_5m",
    "1h cache write": "cache_write_1h",
    "cache hit": "cache_read",
    "output": "output",
}
_PRICE_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")
# A link's URL may hold one level of parentheses: [name](https://x.y/a_(b)).
_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_FOOTNOTE_RE = re.compile(r"<sup\b[^>]*>.*?</sup>|\[\^[^\]]*\]", re.IGNORECASE | re.DOTALL)
_OVER_RE = re.compile(r"\bover\s+([0-9][0-9,]*)\s+tokens", re.IGNORECASE)
_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PageChanged(Exception):
    """The page no longer looks the way the parser expects. Treated as a failed
    check, never as "no differences"."""


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _clean_name(cell: str) -> str:
    """A name cell as plain text: entities decoded, footnote markers (and what's
    in them), links, tags, emphasis and code marks dropped, whitespace tidied,
    and a leading "Anthropic " removed. Parenthesised notes are kept."""
    name = html.unescape(cell).replace("\xa0", " ")
    name = _FOOTNOTE_RE.sub("", name)
    name = _LINK_RE.sub(r"\1", name)
    name = re.sub(r"<[^>]+>", "", name)
    name = re.sub(r"\\([*_`])", r"\1", name)           # \* -> *
    name = re.sub(r"[*`]+|(?<!\w)_+|_+(?!\w)", "", name)  # emphasis, code; not snake_case
    name = re.sub(r"\s+", " ", name).strip()
    return re.sub(r"^anthropic\s+", "", name, flags=re.IGNORECASE)


def model_id(display_name: str):
    """"Claude Opus 5.5" -> "claude-opus-5-5". None for something that isn't a
    plain model name. Markup around the name ("**Claude Opus 5**", a footnote
    marker, a non-breaking space) is cleaned first, and parenthesised notes
    ("(retired, ...)", "(for prompts up to 100,000 tokens)") are dropped --
    innermost first, so a note with its own brackets inside it goes whole. A
    bracket left without its partner means it isn't a plain name."""
    name = _clean_name(display_name)
    while True:
        bare = re.sub(r"\([^()]*\)", "", name)
        if bare == name:
            break
        name = bare
    name = name.strip().lower()
    if not name.startswith("claude "):
        return None
    slug = re.sub(r"[\s.]+", "-", name).strip("-")
    return slug if re.fullmatch(r"claude-[a-z]+(-[0-9]+)+", slug) else None


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(markdown: str):
    """{"rates": {model_id: {field: $/MTok}}, "tiered": [model_id, ...],
        "tiers": {model_id: {"over": tokens, "up_to": rates, "long": rates}},
        "unparsed_tiers": [model_id, ...], "skipped": [name, ...],
        "multi_price": ["model_id / field", ...]}.

    A model listed on more than one row -- priced by prompt length, like Haiku
    5.5 -- goes in "tiered" rather than "rates". When its rows read "(for
    prompts over N tokens)" and one other row, "tiers" has both sets of rates
    and the threshold, to compare with the table's long_prompt block. Any other
    shape (reworded row, a third row) is listed in "unparsed_tiers": its prices
    can't be compared, so the page isn't fully understood.

    A row whose cleaned name mentions Claude but isn't a plain model name
    ("Claude Opus 5 Preview") is listed in "skipped", for the same reason.
    Other rows are not.

    A price cell holding more than one dollar amount ("~~$4~~ $5", "$4 -> $5",
    "$5 (was $4)") is a page change: which amount is current is a guess, so
    neither is taken. The model's rows are left out of "rates"/"tiers" and the
    cell is listed in "multi_price" as "model_id / field".
    """
    lines = markdown.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == "## model pricing")
    except StopIteration:
        raise PageChanged('no "## Model pricing" section')
    header_at = next((i for i in range(start, min(start + 40, len(lines)))
                      if lines[i].lstrip().startswith("|")), None)
    if header_at is None:
        raise PageChanged("no table under Model pricing")
    header = [h.lower() for h in _cells(lines[header_at])]
    fields = {}
    for col, h in enumerate(header):
        for prefix, field in _COLUMNS.items():
            if h.startswith(prefix):
                fields[col] = field
    if sorted(fields.values()) != sorted(_COLUMNS.values()) or not header[0].startswith("model"):
        raise PageChanged(f"unexpected columns: {header}")

    rows, seen, by_model, skipped, multi = {}, {}, {}, [], []
    for line in lines[header_at + 2:]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        mid = model_id(cells[0]) if cells else None
        if not mid:
            name = _clean_name(cells[0]) if cells else ""
            if re.search(r"\bclaude\b", name, re.IGNORECASE) and name not in skipped:
                skipped.append(name)
            continue
        seen[mid] = seen.get(mid, 0) + 1
        rates = {}
        for col, field in fields.items():
            amounts = _PRICE_RE.findall(cells[col]) if col < len(cells) else []
            if not amounts:
                raise PageChanged(f"no price for {mid} / {field}: {cells[col] if col < len(cells) else ''!r}")
            if len(amounts) > 1 and f"{mid} / {field}" not in multi:
                multi.append(f"{mid} / {field}")
            rates[field] = float(amounts[0])
        rows[mid] = rates
        by_model.setdefault(mid, []).append((cells[0], rates))
    if len(rows) < 5:
        raise PageChanged(f"only {len(rows)} models parsed")
    for m in {c.split(" / ")[0] for c in multi}:
        rows.pop(m, None)
        seen.pop(m, None)
        by_model.pop(m, None)
    tiered = sorted(m for m, n in seen.items() if n > 1)
    tiers, unparsed = {}, []
    for m in tiered:
        rows.pop(m, None)
        over = [(int(o.group(1).replace(",", "")), rates)
                for name, rates in by_model[m] for o in [_OVER_RE.search(name)] if o]
        rest = [rates for name, rates in by_model[m] if not _OVER_RE.search(name)]
        if len(by_model[m]) == 2 and len(over) == 1 and len(rest) == 1:
            tiers[m] = {"over": over[0][0], "up_to": rest[0], "long": over[0][1]}
        else:
            unparsed.append(m)
    return {"rates": rows, "tiered": tiered, "tiers": tiers,
            "unparsed_tiers": unparsed, "skipped": skipped, "multi_price": sorted(multi)}


# --------------------------------------------------------------------------- #
# comparing
# --------------------------------------------------------------------------- #

def compare(published, ours=None, used_models=(), ours_long=None):
    """Differences between the published rates and the rates in force today.

    `ours_long` is the table's prompt-length tiers, {model: (threshold, rates)}.

    Returns {"differences": [{model, field, ours, published}],
             "missing": [{model, used}], "tiered": [{model, used}],
             "unparsed_tiers": [model], "skipped": [name], "multi_price": [cell],
             "unverified": [model]}.
    "tiered" lists prompt-length-priced models the table can't price.
    "unparsed_tiers", "skipped" and "multi_price" are what the parser couldn't
    read; any one makes check() report a changed page, used or not. "unverified" is the
    models in your logs that the table prices but the page doesn't list at all
    (no rate, no tier), so nothing was compared for them. A cached result from
    an older build has none of the first three.
    """
    ours = pricing.current_rates() if ours is None else ours
    ours_long = pricing.current_long_prompt_rates() if ours_long is None else ours_long
    used = {pricing.normalize_model(m) for m in used_models}
    differences = []

    def diff(model, mine, rates, label="{}"):
        for field in _FIELDS:
            if abs(mine[field] - rates[field]) > 1e-9:
                differences.append({"model": model, "field": label.format(field),
                                    "ours": mine[field], "published": rates[field],
                                    "used": model in used})

    for model, rates in sorted(published["rates"].items()):
        if model in ours:
            diff(model, ours[model], rates)
    # A cached result from an older build has no "tiers": nothing to compare.
    tiers = published.get("tiers") or {}
    for model, tier in sorted(tiers.items()):
        if model not in ours or model not in ours_long:
            continue
        threshold, long_rates = ours_long[model]
        if threshold != tier["over"]:
            differences.append({"model": model, "field": "long-prompt threshold",
                                "ours": threshold, "published": tier["over"],
                                "used": model in used})
        diff(model, ours[model], tier["up_to"])
        diff(model, long_rates, tier["long"], f"{{}} (prompts over {tier['over']:,} tokens)")
    missing = [{"model": m, "used": m in used}
               for m in sorted(published["rates"]) if m not in ours]
    tiered = [{"model": m, "used": m in used} for m in published["tiered"]
              if m not in ours or (m in tiers and m not in ours_long)]
    multi = [str(c) for c in published.get("multi_price") or []]
    # A model with a multi-price cell is on the page, just unreadable: reported
    # as that, not also as "not listed".
    listed = (set(published["rates"]) | set(published["tiered"]) | set(tiers)
              | {c.split(" / ")[0] for c in multi})
    unverified = sorted(m for m in used if m in ours and m not in listed)
    return {"differences": differences, "missing": missing, "tiered": tiered,
            "unparsed_tiers": [str(m) for m in published.get("unparsed_tiers") or []],
            "skipped": [str(n) for n in published.get("skipped") or []],
            "multi_price": multi,
            "unverified": unverified}


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in ("1", "true", "yes")


def fetch(url=PRICING_URL, timeout=TIMEOUT_SECONDS) -> str:
    from urllib.request import Request, urlopen
    req = Request(url, headers={"User-Agent": "usage-lens-price-check"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000).decode("utf-8", errors="replace")


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _cache_file():
    return Path(CACHE_FILE) if CACHE_FILE else paths.cache_dir() / "price-check.json"


def load_cached():
    try:
        with open(_cache_file(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _understood(result):
    """A cached result with published rates the parser read in full. One it only
    partly read is kept (its rate differences still show) but retried within the
    hour, so a parser fix isn't hidden behind a day-old cache."""
    if "published" not in result:
        return False  # a failed check
    published = result["published"]
    # (A malformed block counts as read here; compare() rejects it.)
    return not (isinstance(published, dict)
                and (published.get("skipped") or published.get("unparsed_tiers")
                     or published.get("multi_price")))


def _fresh(result, now):
    try:
        stamp = result["checked_at"]
        # Python 3.9's fromisoformat rejects a trailing "Z".
        if isinstance(stamp, str) and stamp.endswith(("Z", "z")):
            stamp = stamp[:-1] + "+00:00"
        checked = _dt.datetime.fromisoformat(stamp)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    # A stamp from the future (clock change, hand-edited file) would otherwise
    # look fresh for as long as it stays ahead; an hour's slack covers skew.
    if checked > now + _dt.timedelta(hours=1):
        return False
    # Only the published rates are cached; the comparison is redone on every
    # run, so editing the table takes effect at once. A failure is retried
    # after an hour rather than a day -- being offline once shouldn't switch
    # the check off until tomorrow.
    hours = CACHE_HOURS if _understood(result) else FAILED_RETRY_HOURS
    return now - checked < _dt.timedelta(hours=hours)


def needs_refresh(now=None):
    if offline():
        return False
    cached = load_cached()
    if not (cached and _fresh(cached, now or _now())):
        return True
    # Fresh by the clock, but useless if it can't be compared -- fetch again,
    # or the server would show "pending" until the cache aged out.
    return "published" in cached and _try_compare(cached["published"], ()) is None


def check(used_models=(), force=False, now=None, fetcher=None, allow_fetch=True):
    """Run (or reuse today's) check. Never raises.

    With allow_fetch=False it only reads the cache -- the server uses that so a
    page load never waits on the network -- and reports "pending" when there is
    nothing fresh yet.

    Returns {"status": "ok" | "differs" | "failed" | "offline" | "pending",
             "checked_at", "url", "error", "differences", "missing", "tiered",
             "unparsed_tiers", "skipped", "multi_price", "unverified"}.

    "failed" is also the status for a page that parsed only in part (a tiered
    model whose rows can't be read, a "Claude ..." row with an unrecognised
    name, or a price cell holding more than one amount), and for a model in
    your logs that the page doesn't list: "error" says which, and the
    differences that were readable are kept.
    """
    now = now or _now()
    if offline():
        return {"status": "offline", "checked_at": None, "url": PRICING_URL,
                "differences": [], "missing": [], "tiered": [],
                "unparsed_tiers": [], "skipped": [], "multi_price": [], "unverified": []}
    cached = load_cached()
    result = diff = None
    if not force and cached and _fresh(cached, now):
        if "published" not in cached:
            result = cached  # a failed check, still within its hour
        else:
            # A cache that can't be compared (hand-edited, or from another
            # build) is as good as an unreadable one: not used, and replaced by
            # the next fetch.
            diff = _try_compare(cached["published"], used_models)
            result = cached if diff is not None else None
    if result is None:
        if not allow_fetch:
            return {"status": "pending", "checked_at": None, "url": PRICING_URL,
                    "differences": [], "missing": [], "tiered": [],
                    "unparsed_tiers": [], "skipped": [], "multi_price": [], "unverified": []}
        result = {"checked_at": now.isoformat(timespec="seconds"), "url": PRICING_URL}
        try:
            published = parse((fetcher or fetch)())
            diff = _try_compare(published, used_models)
            if diff is None:
                # Not kept as "published": a result that can't be compared would
                # read as fresh for a day. A failure is retried within the hour.
                raise PageChanged("the published prices could not be compared")
            result["published"] = published
            result["status"] = "fetched"
        except PageChanged as exc:
            result.update(status="failed", error=f"the pricing page has changed shape ({exc})")
        except Exception as exc:  # offline, proxy, TLS, timeout...
            result.update(status="failed", error=f"couldn't reach the pricing page ({exc.__class__.__name__})")
        try:
            cache_file = _cache_file()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
        except OSError:
            pass
    out = {"checked_at": result.get("checked_at"), "url": sid(result.get("url")) or PRICING_URL,
           "error": sid(result.get("error"))}
    if result.get("status") == "failed" or "published" not in result:
        out.update(status="failed", differences=[], missing=[], tiered=[],
                   unparsed_tiers=[], skipped=[], multi_price=[], unverified=[])
        return out
    # `diff` was compared fresh above: which models count as "used" depends on the logs.
    out.update(diff)
    # A page only partly understood is never "ok", whatever is in your logs: a
    # model that isn't compared is a model that could differ unseen.
    if diff["unparsed_tiers"] or diff["skipped"] or diff["multi_price"] or diff["unverified"]:
        out.update(status="failed", error=_partial_reason(diff))
        return out
    # A model in the logs that the table can't price is shown at $0 -- as much
    # a mismatch as a wrong rate, so it isn't "ok" either.
    unpriced = any(m["used"] for m in diff["missing"] + diff["tiered"])
    out["status"] = "differs" if diff["differences"] or unpriced else "ok"
    return out


def _partial_reason(diff):
    if not (diff["unparsed_tiers"] or diff["skipped"] or diff["multi_price"]):
        return ("the pricing page doesn't list models in your logs: "
                + ", ".join(diff["unverified"]))
    parts = []
    if diff["unparsed_tiers"]:
        parts.append("unparsed tiers: " + ", ".join(diff["unparsed_tiers"]))
    if diff["skipped"]:
        parts.append("unrecognised model rows: " + ", ".join(diff["skipped"]))
    if diff["multi_price"]:
        parts.append("more than one price in a cell: " + ", ".join(diff["multi_price"]))
    if diff["unverified"]:
        parts.append("not listed: " + ", ".join(diff["unverified"]))
    return f"the pricing page has changed shape ({'; '.join(parts)})"


def _try_compare(published, used_models):
    """compare(), or None when `published` isn't a usable result -- a field of
    the wrong shape, or a non-finite rate that would reach the payload."""
    try:
        diff = compare(published, used_models=used_models)
        json.dumps(diff, allow_nan=False)
        return diff
    except Exception:  # noqa: BLE001 -- check() never raises
        return None


def describe(result) -> list:
    """Human-readable lines for collect.py; the dashboard renders its own."""
    if result["status"] in ("offline", "pending"):
        return []
    lines = []
    if result["status"] == "failed":
        # A page read only in part still had its readable rates compared.
        partial = any(result.get(k) for k in ("unparsed_tiers", "skipped", "multi_price", "unverified"))
        what = "couldn't fully check prices" if partial else "couldn't check prices"
        lines.append(f"Note: {what} against {result['url']}: {result.get('error')}. "
                     "Costs use the built-in table.")
    if result["differences"]:
        lines.append("WARNING: Anthropic's published prices differ from this tool's table "
                     "(costs here still use the table):")
        for d in result["differences"]:
            lines.append(f"  {d['model']} {d['field']}: table ${d['ours']:g}/MTok, "
                         f"published ${d['published']:g}/MTok"
                         + ("" if d["used"] else "  (not in your logs)"))
        lines.append("  Pull the latest version of this tool; if it still differs, the table needs "
                     "a dated row (see docs/cost.md).")
    used_missing = [m["model"] for m in result["missing"] + result["tiered"] if m["used"]]
    if used_missing:
        lines.append("WARNING: published but not priced by this tool, and in your logs: "
                     + ", ".join(used_missing))
    return lines
