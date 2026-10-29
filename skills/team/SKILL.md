---
name: team
description: Open the Usage Lens team dashboard over a folder of people's usage exports, for a manager combining a team's Claude Code usage. Use when the user wants to see or merge their team's usage exports.
argument-hint: "<folder of exports> [--seat-prices FILE]"
---

The team dashboard reads the exports people send, from one folder: zips from
the **Export (.zip)** button on their own dashboard, or the CSVs
`/usage-lens:export` writes.

If `$ARGUMENTS` doesn't name a folder, ask the user which folder holds the
exports. Then start the server with the Bash tool's `run_in_background` option,
because it runs until stopped:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" --team $ARGUMENTS
```

On Windows, use `py` in place of `python3`.

It prints `Serving TEAM dashboard for <folder> at http://127.0.0.1:8765/` and
opens the page. Give the user the URL. If the port is taken, add `--port 8766`.

- Re-exports and a person's second machine are de-duplicated for them.
- For value per seat, put a `seat-prices.json` in the folder or pass
  `--seat-prices FILE`; the format is in `${CLAUDE_PLUGIN_ROOT}/docs/team.md`.
- For the merged, de-duplicated CSVs instead of a dashboard, run
  `python3 "${CLAUDE_PLUGIN_ROOT}/collect.py" --merge <folder> --out <output folder>`.
