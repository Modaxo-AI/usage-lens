# Changelog

Each release is tagged `vX.Y.Z` in git. The version is also in
`claude_usage/__init__.py` and `.claude-plugin/plugin.json`, and every export
records it in its `tool_version` column.

## Unreleased

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
