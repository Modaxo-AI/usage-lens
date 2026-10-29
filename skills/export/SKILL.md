---
name: export
description: Export this machine's Claude Code usage as the Usage Lens business-review CSVs, to send to a manager for the team dashboard. Use when the user wants to export or share their usage.
argument-hint: "[output folder] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--tz ZONE]"
---

Write the export to a folder the user chooses. If `$ARGUMENTS` doesn't name
one, ask where to save it rather than writing into the current project.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/collect.py" --export $ARGUMENTS
```

On Windows, use `py` in place of `python3`.

Useful options, all optional:

- `--since` / `--until YYYY-MM-DD` to limit the dates.
- `--tz ZONE` to use the team's time zone for days and weeks (default UTC, or
  the `CLAUDE_USAGE_EXPORT_TZ` variable). Everyone on a team should use the
  same zone.
- `--no-titles` to leave session titles out.

Tell the user where the files were written. Before they send them anyone,
point them to "What is in the export file" in
`${CLAUDE_PLUGIN_ROOT}/docs/export.md`: the export carries their email, project
and repository names, costs and session titles (unless `--no-titles`), but
never prompts, replies or code.
