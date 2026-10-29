# Known limitations

- **Subagent output is imputed for Claude Code 2.1.284 to 2.1.306.** In those
  builds (2.1.307 fixed it), 88–97% of subagent responses reached the
  transcript with only a placeholder output count (2–20 tokens), not the final
  one (anthropics/claude-code#84223).
  On one session that put Sonnet's output at about 2% of Claude Code's own
  count, and the estimate 27% low. The output of
  such a response is now estimated from how much it wrote: its text, its tool
  calls and the length of its thinking signature. See
  [Cost estimates](cost.md#subagent-output-and-calls-the-transcripts-miss).
  How close the estimate is:

  | | Imputed vs logged output |
  |---|---|
  | Opus 5, Opus 5.5, Sonnet 5 subagents, in aggregate | within ±4–5% |
  | Sonnet 5.5 subagents, in aggregate | about 8% low |
  | Main-thread responses, never fitted on | +1% to +14% |
  | One response | typically 8–22% off |
  | Haiku 4.5 | about 39% high (a small share of output, at Haiku rates) |

  Totals are reliable; a single response's output is not. Imputed tokens are
  counted in output and cost, and shown as their own figure everywhere.

- **A response that fell back to another model is priced at the fallback's
  usage only.** When a model declines a request and Claude Code retries it on
  another, the response's `usage.iterations` holds both attempts, but its
  top-level usage, which the tool prices, is only the fallback's. Whether the
  first, declined attempt is billed isn't documented, so it isn't counted. Such
  responses are rare (a handful in a heavy user's logs), but the first attempt
  can carry a large cache read.

- **Transcripts don't record every model call. Where Claude Code kept its own
  counter, the rest is shown separately; elsewhere the total is a floor.**
  Claude Code's `cost-state` counter includes calls that never reach a
  transcript. They look like calls it makes for itself (background summaries,
  titles, compaction, an auto-mode classifier); that attribution is an
  inference, not confirmed. Where a session has a counter, what the counter
  holds beyond the session's transcripts is priced at list rates and shown as
  "+ $X not in transcripts". It is never added into Total cost. On one
  full log set that was about 2.6% of estimated usage, across the roughly
  half of sessions that kept a counter.

  A session **without** a counter has no such figure. Many desktop sessions
  have none (see CLI-reported cost below), so their cost reads a few percent low
  and the export leaves `est_cost_unlogged_usd` blank rather than 0.

  Two cases where the counter figure is approximate:
  - The counter gives cache writes as one total. The missing writes are split
    5m/1h in the transcript's own proportion.
  - Fast-mode calls among the unlogged ones are priced at standard rates.

  **The counter's own dollar figure is not this tool's.** Claude Code prices
  Sonnet 5.5 cache reads at $0.20/MTok, but the published page and
  `pricing.py` use $0.10. On the session checked against, that one rate
  is the whole remaining gap: the tool reads 2.6% below the counter
  (transcripts plus not in transcripts).

  **Input from logged calls is counted correctly.** The `input_tokens` field
  is *uncached* input only: with prompt caching almost everything sent is
  billed as cache reads, so 95% of logged responses carry three or fewer input
  tokens. Those are real values, not missing ones. The input the transcripts
  miss belongs to the unlogged calls, and is in the "not in transcripts"
  figure where a counter exists.

  Any tool that reads only the transcripts shares the unlogged-call gap; ccusage
  does. The **Logged here vs Claude Code's own counter** table on the Overview tab shows the
  per-model comparison for your own logs, over each counter's own window (see
  [Cost estimates](cost.md)).

- **Claude Code's own `/stats` has been reported to read about twice this
  tool's token total** (anthropics/claude-code#91775), consistent with adding
  up every transcript line: one response is written as one line per part
  (thinking, text, each tool call), each repeating the response's full usage.
  This tool counts each response once by its `message.id`, which is Anthropic's
  documented rule ("deduplicate by ID", Agent SDK cost-tracking guide).

- **US-only inference isn't priced.** A request made with US-only inference
  (`inference_geo`) costs 1.1× on Claude 4.6 and later models. The logs don't
  show which requests used it, so they are priced at standard rates; Claude
  Code's own counter does apply it.

- **The allowance is estimated, not reported.** Claude Code's logs never record
  a running "% of weekly allowance used" — the `rateLimits` field on API errors
  is always empty — so the allowance is inferred from list-price usage at the
  moment the weekly limit was last hit. That is rough: one hit is a single data
  point, and list-price dollars aren't a steady measure of the allowance, because
  the model mix changes what a request counts for. On one real log set a
  completed week with no limit notice had used about 30% more at list price than
  the hit week, so the dashboard shows the estimate rounded (≈$1.2k, not
  $1,237.73), says how many hits it rests on ("from 1 weekly-limit hit"), and
  widens it to a range, from the usage at the hit to the highest completed week
  without a hit, when such a week used more. It then calls the estimate
  contradicted. The share used is stated, not graded ("about 5% of the estimate",
  not "on track"). It is Claude Code's share only: the same weekly limit also
  counts Claude chat usage, which isn't in these logs. Treat it as approximate
  until several weeks of hits agree, and expect it to read blank for anyone who
  has never hit the limit. Week boundaries step from the last reset by wall-clock
  time, so a daylight-saving change doesn't move them by an hour, but the zone is
  this machine's: the notice's own zone isn't kept, so a machine in a different
  zone from the notice can be an hour out across one zone's change.
- **The Skills tab charts skill-attributed work only.** A request carries an
  `attributionSkill` tag only when it ran inside a skill; everything else is
  unattributed, and on a real log set that is ~91% of cost. Charting it
  alongside real skills flattened them all to an unreadable sliver, so the
  unattributed share is reported in the tiles at the top of the tab instead —
  where "how much of our work runs inside a skill" is arguably the more useful
  number anyway. Attribution is not perfect: a couple of skills were invoked
  without any request carrying their tag, so a little real skill work lands in
  the unattributed share.
- **Median session cost and cost concentration are built from the filtered
  rows**, not from whole-session totals, so they stay consistent with Total cost
  when a date or project filter is on. Their denominator is sessions that
  recorded priced usage, which is why the caption says "with spend" and can be
  lower than the Sessions tile beside it. Concentration is trivially high when
  few sessions are in range — the caption shows the sample size so a 99% reading
  over six sessions is visibly not a finding.
- **Cache savings are net.** The figure is what the cache-read tokens would
  have cost as fresh input, *less* the premium paid to write the cache (a 5m
  write is 1.25x input, a 1h write 2x). The premium is small — about 2% of the
  gross on a real log set — but a savings number that ignores its own cost is
  one nobody trusts twice.
- **Limit reset times are read in US zone abbreviations and IANA names only.**
  Only the US abbreviations (EST/EDT, CST/CDT, MST/MDT, PST/PDT) are mapped, to
  their zones; MST is read as America/Denver, so Arizona's fixed MST is an hour
  out in summer. Any other abbreviation falls back to the machine's local zone.
  A reset time inside a daylight-saving gap or repeated hour resolves to its
  first occurrence. A reset that can't be read leaves `resets_at` blank (see
  [the limits file](export.md#the-limits-file-who-ran-out-of-allowance-and-what-it-cost-them)).
- **A few log lines carry a cache-write split that disagrees with the total.**
  On some lines the `ephemeral_5m`/`ephemeral_1h` tokens don't add up to
  `cache_creation_input_tokens`. This tool trusts the split, since it is what
  sets the write rate, and uses the total only when there is no split. On a real
  log set the difference was a negligible fraction of total cost.
- **Web lookups count tool calls, not `usage.server_tool_use`.** That usage
  field tracks the Messages API's *server-side* web search tool, where Anthropic
  runs the query. Claude Code's WebSearch/WebFetch are client-side tools — it
  fetches itself and returns a tool_result — so the server-side counters stay at
  zero in these logs. They are still parsed, in case a future build uses the
  server-side tool, but nothing displays them. The export's
  `web_search_requests` / `web_fetch_requests` columns count the same tool
  calls as the dashboard tile.


- Cost is an estimate (see above) — confirm against your actual bill before
  relying on exact figures.
- **Web lookups are almost entirely priced.** What a WebSearch or WebFetch
  returns lands in the conversation as a tool result, so it is billed as input
  tokens and is already in the cost. What is *not* in the logs is the side
  request Claude Code makes to perform the lookup — the search itself, and the
  small-model pass that condenses a fetched page — so its fee and tokens are
  left out. That gap is negligible: on a real log set, 141 searches at the
  published $10 per 1,000 came to about 0.03% of estimated usage.
- **CLI history is usually only 30 days deep.** Claude Code deletes CLI
  transcripts after `cleanupPeriodDays` (30 by default); Desktop ones are kept
  much longer. The dashboard and export flag it when it has happened — see
  [Short history](export.md#short-history-cli-transcripts-are-deleted-after-30-days).
  The flag is inferred: it is raised when the oldest surviving CLI transcript
  is 23–31 days old, the age deletion leaves behind. History starting more
  recently is taken as someone new to the CLI and not flagged, so someone who
  genuinely started 23–31 days ago is flagged when nothing was deleted.
- "Desktop vs CLI" is exactly that: the logs only ever record `claude-desktop` or
  `cli` as an entrypoint.
- Branch/version-by-cost attribution picks each session's single most-frequent
  branch/version rather than splitting per-request. Branch cost is keyed by
  project *and* branch, so the same branch name in two repos stays separate;
  `HEAD` is shown as `(detached)`, since that is what git records for a worktree
  or a rebase rather than a real branch name.
- **Lines added/removed count the edits Claude made, and nothing else.** Each
  Edit, Write, MultiEdit or NotebookEdit result carries the diff it made; the
  tool counts its `+` and `-` lines in memory (for a new file, its length) and
  dates them by the edit, so a date range holds the lines written inside it.
  Changes made outside Claude's edit tools — a shell command that rewrites a
  file, a formatter, a `git` checkout, or your own editor — aren't counted, and a
  failed edit counts nothing. A subagent's edits count for its parent session.
  Versions before 1.1.0 read Claude Code's `cost-state` line counter instead,
  which has no timestamp, was written mostly when a session closed and is
  missing from many desktop sessions; this tool no longer uses it for lines.
- **CLI-reported cost depends on whether the session kept a counter.** Claude
  Code's `cost-state` counter is written from the CLI and from many, not all,
  desktop sessions (62% of desktop sessions on one log set). A counter written
  only when a session was reopened, with no cost in it, is treated as no
  counter: the session shows no CLI cost rather than $0.00.
- **A transcript line of an unexpected shape is skipped, not fatal.** A line
  that is valid JSON but not a record (`null`, an array), or whose `message`,
  `usage` or timestamp has the wrong type, costs that one record; the rest of
  the file and the scan carry on. A partial last line on a live session is
  skipped the same way.
- **Windows is supported but less exercised.** Everything reads and writes
  UTF-8 explicitly, because Python otherwise uses the platform default — cp1252
  on Windows — and a log containing any non-ASCII character then fails to parse
  at all. CI runs the suite on Windows and macOS as well as Linux to keep that
  honest. Project paths recorded with backslashes are normalised to forward
  slashes for display.
- A cold scan (empty cache) takes a few seconds for ~500 files; a warm scan is
  well under a second.

---

[← Back to the README](../README.md)
