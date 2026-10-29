---
name: dashboard
description: Open the Usage Lens dashboard of this machine's Claude Code usage and cost (tokens, cache hit rate, spend by model and project, habits). Use when the user asks to see their Claude Code usage, spend, tokens or cache hit rate.
argument-hint: "[--port N] [--offline]"
---

Start the Usage Lens server for the user's own logs. It runs until stopped, so
start it with the Bash tool's `run_in_background` option rather than waiting on
it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" $ARGUMENTS
```

On Windows, use `py` in place of `python3`. Python 3.9 or newer is the only
requirement; there is nothing to install.

The server prints `Serving usage dashboard at http://127.0.0.1:8765/` and opens
that page in the browser. Give the user the URL. The first run takes a few
seconds to read their logs; later runs use a cache.

- If the port is taken, run it again with `--port 8766`.
- `--offline` skips the once-a-day check of Anthropic's public pricing page,
  the tool's only network request.
- The tool reads usage numbers, model names and tool names from
  `~/.claude/projects/`, never prompts, replies or code, and serves only on
  `127.0.0.1`.

To stop it, stop the background task.
