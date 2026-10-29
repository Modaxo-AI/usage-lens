# Cost estimates

`claude_usage/pricing.py` holds a dated per-model $/MTok table (input, output,
cache read, cache write — with separate 5-minute and 1-hour cache-write rates). Cost is
an *estimate* from public list pricing, not a bill — on a seat-based Enterprise
plan it measures how much was used, not what was paid. Every export row says so
in its `cost_basis` column (`api_list_price`), because the CSV travels without
this page. Four modifiers are applied:

- **Fast mode** (`usage.speed == "fast"`, i.e. Claude Code's `/fast`) is priced at
  2× standard, matching the published fast-mode rates on the Opus models that
  offer it. A banner appears on the dashboard when any request used it.
- **Batch tier** (`usage.service_tier == "batch"`) is priced at 0.5×.
- **Prompt length**, for the one model priced by it: Claude Haiku 5.5 charges
  5× on every category, output included, when a request's prompt is over
  100,000 tokens ($0.50/$2.50 per MTok instead of $0.10/$0.50). The prompt is
  counted as Anthropic counts it — fresh input plus cache reads plus cache
  writes — and each request is priced on its own. Its row in `pricing.py`
  carries the threshold and the higher rates in a `long_prompt` block.
- The **1-hour cache-write** rate is taken as 2× the base input rate — Anthropic's
  published multiplier — and kept behind a named constant
  (`ASSUMED_1H_MULTIPLIER`) so the UI can label it as a convention.

A model with no entry in the table is counted at **$0**, which would silently
understate every total, so unpriced models are listed in a red banner on the
dashboard and as a warning from `collect.py`. If you see one, add it to
`pricing.py`.

The 1M-context mode is billed at the same rates as the base model (the CLI's own
figure for `claude-opus-5[1m]` reproduces exactly at standard Opus 5 rates), so
the `[1m]` suffix is normalized away rather than priced separately.

Model names are normalized before lookup, so the same model is priced the same
however it was reached:

| Logged as | Priced as |
|---|---|
| `claude-haiku-4-5-20251001` (dated) | `claude-haiku-4-5` |
| `claude-3-5-haiku-20241022` (Claude 3.x order) | `claude-haiku-3-5` |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `anthropic.…`, `us-gov.anthropic.…`, an inference-profile ARN (Bedrock) | `claude-sonnet-4-5` |
| `claude-opus-4-0`, `claude-sonnet-4-0` | `claude-opus-4`, `claude-sonnet-4` |
| `claude-opus-4-1-latest` (a `-latest` alias) | `claude-opus-4-1` |
| `claude-sonnet-4-5@20250929` (Vertex) | `claude-sonnet-4-5` |

An id that names no model in the table stays at $0 and is listed as unknown:
Claude 3.7 Sonnet (not on the live pricing page) and an application-inference-profile
ARN (its id is opaque, so it can't be mapped from the name).

Provider surcharges are not modelled: Bedrock and Vertex requests are priced at
Anthropic's list rates, and the 1.1× for US-only inference (`inference_geo`)
isn't applied.

## Pricing over time

Cost is never stored. The parse cache keeps token counts only, and every scan
prices each request again. So the rate table has to say *when* each rate applied,
or a price change would either go unnoticed (new usage at stale rates) or, once
the table was edited, quietly reprice everything that came before.

`RATE_HISTORY` in `pricing.py` therefore keeps, for each model, a list of
`(effective_from, rates)` rows, oldest first:

```python
"claude-sonnet-5": [
    ("2026-10-02", {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write_5m": 2.50}),
],
```

- Each request is priced by the row in force on its own date. Transcript
  timestamps are UTC, so the date is the UTC date.
- A request older than a model's first row is priced at the first row. The first
  rows are dated the day the table was first verified (2026-10-02), not each
  model's launch day, so they cover everything earlier.
- A row dated in the future is not in force until its day: a price change can
  be recorded as soon as it is announced. "Today's rates" — for a request with
  no readable timestamp, and for the price check — are the last row dated on or
  before today, decided afresh on every call, so a long-running `serve.py`
  crosses over on the day.
- The fast-mode, batch and 1-hour cache-write modifiers apply to whichever row
  is in force; a dated row can set `cache_write_1h` itself if a published rate
  ever stops following the 2× convention.
- Every export carries `pricing_version`, a fingerprint of the whole dated
  table. It changes whenever a row is added, so the team merge can tell when two
  files were priced by different tables.

### The daily price check

Anthropic publishes its prices as Markdown at
[platform.claude.com/docs/en/about-claude/pricing.md](https://platform.claude.com/docs/en/about-claude/pricing.md).
Once a day the tool fetches that page, reads its *Model pricing* table, and
compares it with the rates in force in `pricing.py`. It **reports; it never
changes a price** — costs always come from the dated table, so two people's
exports of the same month agree and history never moves.

| What it finds | What you see |
|---|---|
| A rate differs | A dashboard notice and a `collect.py` warning listing each model, field, our rate and the published one, and whether the model is in your logs |
| A published model the table doesn't price, that's in your logs | A notice naming it (it would otherwise show at $0); counts as a difference |
| A model priced by prompt length (Haiku 5.5 today) | Both rows compared with the table's `long_prompt` block, threshold included; a difference names the tier, e.g. `output (prompts over 100,000 tokens)` |
| The page can't be reached, or no longer looks as expected | A one-line note that it couldn't check — **never** "prices match". Retried after an hour |
| The page parses only in part: a model's long-prompt rows can't be read as one "over N tokens" row plus one other, or a row naming Claude has a name it doesn't recognise (markup such as `**bold**`, a footnote marker, `&nbsp;` or an "Anthropic" prefix is cleaned off first) | A "couldn't fully check" note, naming the `unparsed tiers` or the unrecognised rows, even if the model isn't in your logs. Rate differences it could read are still listed. Retried after an hour |
| A price cell holding more than one amount (`~~$4~~ $5`, `<s>$4</s> $5`, `$4 → $5`, `$5 (was $4)`) | A "couldn't fully check" note, naming the model and column (`more than one price in one cell`). Neither amount is taken, since which one is current is a guess. Retried after an hour |
| A model in your logs that the table prices isn't on the page at all | A "couldn't fully check" note, naming the model: nothing was compared for it, so "prices match" can't be said. The page itself read fine, so this is checked again the next day, not the next hour. A model Anthropic has delisted from the page makes the check fail (`unverified`) while your logs still use that model and the page doesn't list it |
| Everything matches | Nothing, and the 90-day "not checked" warning below is suppressed |

It runs in the background in `serve.py` (a page load never waits on it) and
before output in `collect.py`. `python3 collect.py --check-prices` forces a
fresh check and exits 1 if anything differs, a model in your logs is published
but unpriced here, or it couldn't check — useful for a scheduled job. A
published model that is not in your logs is listed but doesn't fail the run. `--offline` on either command, or `CLAUDE_USAGE_OFFLINE=1`,
turns it off entirely. What it sends is in [Privacy](privacy.md).

The first run of the check found a real error: Sonnet 5.5 cache reads were
priced at $0.20/MTok, but Sonnet 5.5 (like Opus 5.5) reads from the cache at
0.05× input — $0.10. On a heavy user's logs that overstated cost by a few
tenths of a percent of total. It also found six published, older models the table couldn't price
(Opus 4 / 4.1 / 4.5, Sonnet 4 / 4.5, Haiku 3.5); they are now priced. Haiku 5.5
followed on 2026-10-09, once the table could hold a prompt-length tier.

### When prices change

`PRICES_CHECKED` in `pricing.py` is the day someone last compared the table by
hand. Once it is more than 90 days old (`PRICES_STALE_AFTER_DAYS`) and no daily
check has succeeded, the dashboard shows a notice and `collect.py` prints a
warning. When the daily check reports a difference, or to re-check by hand:

1. Compare every model in `RATE_HISTORY` with the published table
   (`python3 collect.py --check-prices` does this and prints each difference).
2. If a price changed, **append one row** to that model's list, dated the day
   the new price took effect, with the full new rates. **Never edit or delete a
   past row.** Usage before that date has already been reported and exported at
   the old rate, and editing the row would silently reprice it.
3. Copy the new row into `PINNED_ROWS` in `tests/test_pricing.py`. That test pins
   every row; editing a past rate fails it with a message telling you to add a
   dated row instead.
4. A new model gets its own key with a single row, dated the day you add it. A
   model priced by prompt length adds
   `"long_prompt": {"over": <tokens>, "rates": {...}}` to its row (see
   `claude-haiku-5-5`).
5. Set `PRICES_CHECKED` to today, whether or not anything changed, and run the
   tests.

#### Reconciling against the CLI's own numbers

Claude Code writes `cost-state` entries containing both its own `totalCostUSD`
and a per-model `modelUsage` breakdown (tokens *and* dollars). The Overview tab
compares the estimate against that breakdown model by model, and each session row
expands to show the same comparison for itself. The CLI's counter belongs to the
whole session and can't be split by day, so the Overview comparison counts only
sessions that started and ended inside the selected dates. A session that began
before the range, or ran past its end, is left out rather than having its whole
total credited to the range.

Read the result with one caveat: **the CLI's counter restarts from zero whenever a
session is resumed**, so it under-reports long sessions — a large positive Δ is
expected there, not a bug.

The opposite effect is handled for you. A branched or resumed transcript can
*carry on* its parent's counter instead (same `startTime`) — on a real log set a
76-request branch carried its parent's whole counter. Counting each transcript's
snapshot made the CLI side read up to twice the truth (Opus 5.5 showed −49%;
counted once it is −3%), and inflated lines changed and CLI-reported cost in the
export by 63% and 70%. Transcripts sharing a counter are now grouped: the
counter's latest snapshot is shown once, on the session that did most of the
work, and compared with all of the group's requests. The other transcripts show
"shared" in the CLI column and carry no lines or CLI cost of their own. Where the session hasn't been resumed the two agree
closely; on a representative local log set the most recent model reconciled to
within 2.7%, and one model matched to the cent. The CLI figure includes
subagent usage. From Claude Code 2.1.284 the subagent transcripts lose their
output counts, which is handled below.

## Subagent output and calls the transcripts miss

Two things make a transcript total read low. The tool corrects the first and
shows the second separately.

**Subagent output is imputed.** From Claude Code 2.1.284 a subagent response is
written as one line per content block, and every line carries the usage from
the *start* of the stream (2–20 output tokens). The final line is never
written. That was 88–97% of subagent responses in builds 2.1.284–2.1.295, in
the desktop app and the CLI alike; main-thread responses always get their
final line. Input and cache tokens on those lines are right; only output is
wrong.

The text, tool calls and thinking signature of each response are still in the
transcript. So a response with no final line (no line with a `stop_reason`)
is given

    max(0, round(0.39 × characters written + 0.28 × thinking-signature characters) − logged output)

more output tokens. "Characters written" means text, thinking text and each
tool call's name and JSON input. The constants (`IMPUTE_CHAR_TOKENS`,
`IMPUTE_SIGNATURE_TOKENS` in `aggregate.py`) were fitted on 26,000 subagent
responses from earlier builds that did log their final count. In aggregate the
estimate is within ±4% for Opus 5, Opus 5.5 and Sonnet 5; per response it is
within 8–22%. Only the two character counts are kept (see [Privacy](privacy.md)).

Imputed output is priced like logged output and counted in every output and
cost total. It stays on its own request, so the day, model, project and
subagent type are all right. The dashboard's Total tokens tile says how much
of the total was imputed; so do the session detail and the export's
`output_tokens_imputed`. The dedupe still ranks copies of a response by the
output they logged.

**What the counter saw and the transcripts didn't is shown beside the total.**
Claude Code also makes model calls that never reach a transcript. Where a
session kept a `cost-state` counter with per-model `modelUsage`, the shortfall
is worked out per model and token category:

- It is the counter's tokens minus the tokens of the counter's group of
  transcripts (see above), with output counted as logged plus imputed.
- Only requests made since the counter's `startTime` are compared. A resumed
  session's counter restarts, and its earlier work would otherwise hide the
  shortfall.
- A category where the transcripts hold more counts as 0, never as a negative
  amount.
- The counter gives cache writes only as one total. The shortfall is split
  between 5-minute and 1-hour writes in the transcript's own proportion for
  that model.

The shortfall is priced at this table's rates on the day of the counter's last
snapshot, not at the counter's own `costUSD`, so a rate the counter disagrees
with is not imported. It is shown as "+ $X not in transcripts" under Total
cost and in each session's detail, and is exported as `est_cost_unlogged_usd`.
It is never added into Total cost or `est_cost_usd`, which every chart and
breakdown adds up to.

A session **without** a counter, which is most desktop sessions, gets the
imputation but no unlogged figure. That figure is blank, not zero, because
nothing measured it, so the session's total is a floor.

On the test session these changes were checked against (41 Opus 5.5 main
calls, 10 background subagents making 210 calls), the estimate in transcripts
rose by about 31%, and with a small amount not in transcripts it ended
**2.6% below** the counter. The remaining gap is one rate. The
counter prices Sonnet 5.5 cache reads at $0.20/MTok; this table and the
published page say $0.10, which accounts for the gap on that session's 5M
Sonnet cache reads. Imputation read slightly high on that session and offsets part of that
gap: Opus +4%, Sonnet +3.4% output.

---

[← Back to the README](../README.md)
