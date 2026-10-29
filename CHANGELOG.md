# Changelog

Each release is tagged `vX.Y.Z` in git. The version is also in
`claude_usage/__init__.py` and `.claude-plugin/plugin.json`, and every export
records it in its `tool_version` column.

## Unreleased

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
