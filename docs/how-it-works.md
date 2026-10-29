# How it works

```
~/.claude/projects/*/*.jsonl                 main session transcripts
~/.claude/projects/*/*/subagents/**/*.jsonl      subagent and workflow transcripts (+ .meta.json)
                │
                ▼
  claude_usage/parser.py   — reads one file at a time; extracts only usage/tool/
                             skill/session metadata, never message text or tool inputs
                │
                ▼
  claude_usage/cache.py    — caches parsed results per file by (mtime, size),
                             so unchanged logs aren't re-parsed on every scan
                │
                ▼
  claude_usage/aggregate.py — dedupes, links subagents to parent sessions,
                              estimates cost, builds the payload sent to the browser
                │
                ▼
  claude_usage/scan.py     — discovers files, ties parser+cache+aggregate together
                │
         ┌──────┴──────┐
         ▼             ▼
     serve.py      collect.py
   (live server,   (CLI: writes
    /api/data)      data.json / dashboard.html)
                │
                ▼
     static/{index.html,app.css,app.js} + chart.umd.min.js (vendored, no CDN)
```

The team view starts from people's export files instead of transcripts:

```
export zips/CSVs (one or more per person, from collect.py --export / Export (.zip))
                │
                ▼
  claude_usage/team.py     — reads and checks each export, works out who is who and
                             which machine is which, de-duplicates re-exports, adds up
                             machines, and builds the team payload and merged CSVs
                │
         ┌──────┴──────┐
         ▼             ▼
  serve.py --team   collect.py --merge
  (/api/team,       (team-*.csv +
   /api/team/        merge-report.txt)
   export.zip)
                │
                ▼
     static/{team.html,team-findings.js,team.js} — every figure is worked out in the
                             browser for the selected people and weeks
```

## Three correctness issues this fixes vs. a naive log read

1. **One response, several lines.** Claude Code writes one transcript line per
   content block of a response (thinking, text, each tool call), and every line
   repeats the whole response's usage — about two lines per response on a real
   log set. In subagent transcripts from Claude Code 2.1.284 on, *every* line
   keeps a placeholder output count and the final one is never written; that
   output is imputed (see [Cost estimates](cost.md#subagent-output-and-calls-the-transcripts-miss)).
   The parser counts each response once by its `message.id` — Anthropic's own
   rule ("deduplicate by ID to avoid double-counting", Agent SDK cost-tracking
   guide) — falling back to `requestId` only when there is no message id, and
   keeps the record with the largest `output_tokens`. Keying on the message id
   rather than `requestId` matters because a sidechain replay can rewrite an
   existing response under a new request id. Claude Code's own `/stats` sums
   every line, which is why its token total is about twice this tool's (see
   anthropics/claude-code#91775).
2. **Subagent transcripts.** Background/forked agents write to
   `<project>/<session>/subagents/agent-*.jsonl`, a separate tree from the main
   session file, and newer Claude Code puts Workflow-agent transcripts one level
   deeper (`subagents/workflows/`) — so the whole `subagents/` tree is walked. On a representative local log set these are a *majority of
   tokens* (~54%) though a smaller share of *cost* (~36%, since subagents lean on
   cheaper models), and they're easy to miss with a naive `*/*.jsonl` glob.
3. **Fork and resume copies.** Resuming or forking a session writes a *new*
   transcript that re-copies the earlier conversation, so the same response
   appears in several files — on a representative local log set, 12.5% of request
   ids appeared in two or more transcripts. Deduping only within a file would
   multiply those requests. Requests are therefore deduped **globally**, with the
   larger transcript (the original, of which the other is a prefix copy) keeping
   the request. A transcript that ends up owning *none* of its requests is a pure
   fork/resume copy: it's marked `derived`, excluded from the session count, and
   hidden behind a toggle on the Sessions tab, because counting it would
   double-count both the session and its active time. A fork that does own
   requests still starts with a copy of the original's records, so active time
   is measured after those are removed too: each record is credited to the first
   transcript that holds its `uuid`, and a session row's `active_start` is its
   first record not copied from another transcript (`start` still shows the
   earliest record in the file). A derived (pure-copy) session holds only
   copied records, so it usually reports an `active_ms` of 0.

## Project layout

```
serve.py              entry point: local HTTP server (`/`, `/api/data`, `/api/export/*`, `/static/*`)
collect.py            entry point: writes data.json, and optionally a standalone dashboard.html

claude_usage/
  parser.py           parses one transcript file into requests/tools/skills/session facts
  sanitize.py         coercions for fields read from files this tool doesn't control
  cache.py            per-file parse cache (JSON) keyed on (mtime, size)
  aggregate.py        dedupe, subagent linking, project naming, cost — builds the payload
  health.py           usage-health measures (context tax, cold-cache cost, checks, friction)
  limits.py           usage-limit episodes and the weekly allowance estimate
  pricing.py          dated per-model $/MTok rates, cost_for(model, usage, ts=...)
  price_check.py      the daily check of the rate table against Anthropic's published prices
  scan.py             discovers log files and ties the above together
  context.py          export provenance: account plan fields, git remotes, machine id, export zone
  export.py           the six business-review CSVs (summary, sessions, breakdown, limits, PRs, tools)
  team.py             reads and merges people's exports: the team payload, merged CSVs, report

static/
  index.html          page shell
  app.css             design tokens (Modaxo brand) + all component styling
  app.js              data loading, filtering, charts, tables
  filters.js          MultiSelect — the searchable project/model filter control
  habits.js           the Habits strip and tab, and the habit tags on Sessions
  team.html           the team dashboard's page shell (serve.py --team)
  team-findings.js    the team page's figures with no DOM: per-person figures for a
                      range, spend by repo, run-rate, headline rules
  team.js             the team page: tabs, filters, charts, tables
  chart.umd.min.js    vendored Chart.js (no CDN)
tests/                unittest suite with small synthetic fixture transcripts
```

Adding a new file under `static/` for your own dashboard means updating two
places: `ALLOWED_STATIC_FILES` in `serve.py` (or the dev server 404s it) and
`VENDOR_SCRIPTS` in `collect.py` (or the standalone build silently omits it). A
test asserts both stay in sync with `index.html`. A file for the team page only
needs `ALLOWED_STATIC_FILES`: the team view has no standalone build.
---

[← Back to the README](../README.md)
