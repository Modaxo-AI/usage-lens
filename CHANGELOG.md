# Changelog

Each release is tagged `vX.Y.Z` in git. The version is also in
`claude_usage/__init__.py` and `.claude-plugin/plugin.json`, and every export
records it in its `tool_version` column.

## Unreleased

## 1.0.2 — 2026-10-29

Every figure on the Overview tab was recomputed from raw logs by independent
code, and every claim checked against Anthropic's sources. This release fixes
what that found.

**Wrong figures, fixed**
- The panel comparing the transcripts with Claude Code's own counter (now
  "Logged here vs Claude Code's own counter") compared different time windows,
  and showed $0.00 for a session a continuation had copied. It now compares
  each counter's own window, and adds a column re-pricing the counter's tokens
  at the published rates: that reproduces Claude Code's dollars to within 0.1%
  for most models, so it is a direct check of the rate table.
- "+ $X not in transcripts" for a date range no longer credits a long session's
  whole shortfall to the range; only sessions wholly inside it count.
- Sessions no longer counts transcripts that never got a reply (84 → 81, as in
  the header).
- Tool calls repeated in Claude Code's newer `api-request-blob` records kept
  their real time; they had moved to the session's first day (web lookups in
  a 5-day range read 5 searches instead of 8). The parse cache is rebuilt once.
- Friction named an invented tool name ("bash fails 100%"); it now names the
  tool with the most errors.
- Context compactions are split into automatic and manual.

**Clearer labels**
- Cost concentration waits for more than 5 sessions (top 5 of 5 is always 100%).
- Thinking tokens is marked as a floor, with the responses that didn't log it.
- Total tokens shows the figure excluding cache reads; billions read "12.92B".
- Cache hit rate and Cache saved say what they measure.
- Median turn shows seconds and its sample size.
- The weekly allowance is a rounded rough range from its one limit hit, and
  says when a later week contradicts it.
- Habit badges say they apply Anthropic's 10% line to the tool's own measures.

**Claims brought up to date**
- Placeholder subagent output counts were fixed in Claude Code 2.1.307; the
  counter restarting on resume was fixed in 2.1.277.
- Long-context advice cites Anthropic's context-engineering post; the
  best-practices guide no longer calls the context window the main constraint.
- There is no 200k pricing tier: 200k and 500k are this tool's own lines.
- Plan-mode and subagent advice match Anthropic's current wording; the
  friction suggestion is marked as the tool's own.
- Limitations now cover US-only inference (1.1×), which isn't priced.

## 1.0.1 — 2026-10-29

Fixes from a full adversarial review of the tool and its docs.

**Costs**
- "Not in transcripts" no longer counts requests a fork or resume copy also
  holds: the counter saw them in its own files.
- "Not in transcripts" for Haiku 5.5 is no longer priced at the long-prompt
  rates when many small calls add up past 100,000 tokens.
- The price check reports a long-prompt tier the page no longer publishes,
  and shows thresholds in tokens rather than as a price.
- A log file starting with a byte-order mark no longer loses its first record.
  The parse cache is rebuilt once.

**Team merge**
- A newer export that lacks an older one's sessions on days both cover, under
  the same machine id (a copied data folder), counts as a second machine
  instead of being dropped.
- Two machine ids for one person, with overlapping windows and no shared
  session, now get a warning naming the weeks that may be counted twice.
- Value per seat in `team-people.csv` and the merge report charges only the
  days the exports cover, as the dashboard does.
- The newest export is chosen by its real time, not its text, across time
  zones.
- UTC, GMT and their aliases count as one zone.
- A cap on cells read across a merge keeps a crafted zip from using gigabytes
  of memory.

**Machine id**
- An old copy's `.cache/machine-id` is carried over on the first run (not the
  first export) and then renamed, so deleting the data folder's id gives a
  genuinely new one.
- Two processes starting together can no longer end up with different ids.
- A relative `CLAUDE_USAGE_DATA_DIR` is made absolute.

**Other**
- `serve.py` prints its URL straight away when run in the background, so the
  plugin's skills can report it, and starts without a reverse-DNS lookup of
  127.0.0.1 that took many seconds on some Macs. Idle connections time out
  after 30 seconds.
- The skills quote folder paths, so a path with a space works.
- A git remote's `?query` or `#fragment` is dropped from `project_repo`.
- CI tests Python 3.10 too, and doesn't keep its token in the checkout.
- The privacy test plants a sentinel in every kind of transcript record and
  checks the cache, `data.json` and the standalone dashboard as well.
- Docs: privacy lists match what is kept (project paths, subagent
  descriptions, reply lengths), the CLI reference has `--version`, and the
  machine-id carry-over is described as it works.

## 1.0.0 — 2026-10-28

The first public release, as Usage Lens.

- **A data folder outside the code.** The parse cache, the daily price check,
  the machine id, and `collect.py`'s `data.json` and `dashboard.html` now live
  in `~/.usage-lens`. Updating, re-cloning or installing the tool as a plugin
  keeps the same machine id, so the team merge no longer counts one machine
  twice. `CLAUDE_USAGE_DATA_DIR` or `--data-dir` moves it. A copy that kept its
  machine id in `.cache/` beside the code carries it over on first run; the
  parse cache rebuilds itself once.
- **Installable as a Claude Code plugin**, with `/usage-lens:dashboard`,
  `/usage-lens:team` and `/usage-lens:export`.
- **A version number.** `serve.py --version` and `collect.py --version` print
  it, and the export's `tool_version` column carries it, plus the commit when
  run from a git checkout (`1.0.0+3f2a1bc`). Before, a copy without git
  recorded `unknown`.
- **Fix:** the daily price check reported Anthropic's pricing page as changed
  since the page began writing retired models with brackets inside their note.
- **Fix:** a dashboard test failed on machines outside the US locale.
