# Usage health — research and proposals

**Question.** Using only what Claude Code already writes to `~/.claude/projects/`,
can the dashboard tell someone, every time they refresh, whether they are using
Claude Code well, and give their manager a team picture without turning it into
surveillance?

**Short answer.** Yes, for the habits that cost the most. The logs carry far more
behavioural metadata than the parser reads today, and Anthropic publishes enough
guidance and benchmarks to anchor several measures to sources, not to our taste.
They do not support a single "you are a good user" score: Anthropic's own research
shows that experienced users look *worse* on the naive measures (they interrupt
more and auto-approve more). So the proposal is a **health panel**: a handful of
habits, each priced in dollars or limit share, compared against your own baseline
and against a published benchmark where one exists, each with one concrete fix.

**Status.** Phases 1 to 3 (§7) are built. The parser reads the new signals, the
dashboard shows them (a strip on the Overview, a Habits tab, and habit tags on
Sessions), the export carries them (schema 7), and the team view summarises
them. Phase 4, the remaining privacy-gated signals, is not built. §8 records
the design decisions.

How this was produced:
- External evidence: Anthropic's docs and research, plus the developer-productivity
  literature. Primary sources only, listed at the end.
- An inventory of a sample log set, using structure and numbers only and never
  message text, across a range of Claude Code versions.

Every figure labelled "Example" below is illustrative, not data from a real user.
Treat the numbers as a demonstration that each measure is computable and
discriminating, not as norms.

---

## 1. What the evidence says good use looks like

**Context is the resource to manage.** Anthropic's best-practices guide is built
on one constraint: the context window fills fast, and quality degrades as it
fills. Its named failure patterns are:

| Pattern | Anthropic's fix | Visible in logs? |
|---|---|---|
| The kitchen-sink session (unrelated tasks in one context) | `/clear` between tasks | Yes: context size, session length, compactions |
| Correcting over and over | "After two failed corrections, `/clear`" | Partly: interrupts and rejections, not the words |
| The over-specified CLAUDE.md | Prune; keep it under 200 lines | Partly: whether a project CLAUDE.md is loaded |
| The trust-then-verify gap (no tests or checks) | Always give Claude a check it can run | Only with a privacy decision (§4) |
| The infinite exploration | Scope it, or use subagents | Yes: main-thread reads vs subagent delegation |

**Cost has published anchors.** Across enterprise deployments the average is
about **$13 per developer per active day**, under **$30 for 90% of users**, and
$150–250 a month. The docs name the two usual causes of high spend: "long
sessions that were never cleared" and "Opus left as the default model".

**Claude Code already flags two habits.** `/usage` raises a behaviour flag when
**long context** or **cache misses** account for **10% or more** of recent usage.
That is the one sourced threshold for context and caching, and the proposal
reuses it.

**Expertise looks different from what a naive score expects:**
- **Delegation per prompt.** Anthropic's study of ~400k sessions (16 Jun 2026)
  found experts delegate bigger chunks: about **12** Claude actions per prompt,
  against about **5** for novices.
- **Verification.** "Verified success" (judged success plus commits, passing tests
  or explicit confirmation) was **15%** for novices and **28–33%** for everyone
  else.
- **Abandonment.** Among sessions that hit trouble, novices abandoned **19%** and
  others **5–7%**.
- **Interrupts and auto-approval.** The autonomy study (18 Feb 2026) found
  experienced users interrupt *more* (about **9%** of turns vs **5%**) and fully
  auto-approve in **over 40%** of sessions (vs about **20%**).
- **What follows.** "Fewer interrupts" and "more approvals" are not quality
  signals. Those benchmarks also come from a classifier reading whole transcripts;
  a metadata proxy will not reproduce them, so show them as context, not as
  pass/fail lines.

**Self-perception is unreliable, and activity metrics get gamed:**
- METR's 2025 trial found experienced developers 19% *slower* with AI while
  believing they were 20% faster.
- Acceptance rate tracks how productive people *feel* (Ziegler et al., 2022).
- DORA warns that metrics made into goals invite gaming.
- SPACE says activity metrics should never be used alone to reward or penalise.
- DX advises saying up front that AI metrics will not be used in performance reviews.

---

## 2. Design principles

1. **Price habits; don't grade people.** A habit shown as "$40 spent rebuilding
   caches you let go cold" is hard to argue with and has an obvious fix. Every
   measure in the first build is denominated in dollars, share of spend, or share
   of sessions.
2. **No composite score.** Show a profile with a few flagged habits ("2 habits
   worth a look"), never a grade. The evidence above means any single score would
   punish good users.
3. **Compare against yourself first.** Show "the selected dates vs the same number of
   days just before them" for every measure. Use a fixed line only where a source gives one ($13/$30 per
   active day, the 10% rule, two corrections, 200 lines). Every other default is
   labelled ours and is tunable.
4. **Metadata only, by default.** The existing privacy rule stands: no prompt text,
   replies, tool inputs or outputs. Signals that would need more are listed in §4
   as explicit decisions, not slipped in.
5. **"Not recorded by this version," never zero.** Several fields only appeared
   recently: tool denials in 2.1.246, loaded-instructions records in 2.1.255,
   permission decisions in 2.1.293. A missing field must show as unavailable.
6. **Individuals see themselves; managers see the team.** See §6.

---

## 3. Proposed measures, first build

All are pure metadata. "New fields" means fields `parser.py` does not read today.

### 3.1 Context tax: the headline measure

| | |
|---|---|
| **What** | Share of main-thread cost spent on requests carrying a large context; peak context per session; automatic vs manual compactions |
| **Fields** | Tokens per request (already read). New: `isSidechain`, `compactMetadata.trigger`, `postTokens` |
| **Example** | Median request context **350k** tokens. **50%** of main-thread cost comes from requests over 500k, which cost about **4×** a small-context request. Auto-compactions outnumber manual ones, firing near the top of the window. `/clear` used in only a few sessions |
| **Flag** | Anthropic's 10% rule: long context at ≥10% of recent spend. "Long" means a request carrying more than **500k** tokens. On 1M-context models a 200k cutoff flagged everyone (nearly every user), so it stopped separating anyone. The 200k share, Anthropic's long-context pricing line, stays as a shown, unflagged figure. No source gives a cutoff for Claude Code, so 500k is ours to tune |
| **Fix shown** | `/clear` between tasks; for big features, write a spec, then start a fresh session (best practices) |

The cutoff is an absolute token count rather than a share of the model's window.
That matches what drives cost, which scales with tokens sent, and avoids needing
the window size, which the logs don't record (`stats-cache.json` stores 0).

### 3.2 Cold-cache tax

| | |
|---|---|
| **What** | Dollars spent re-writing the cache when a large session is resumed after its cache expired, broken down by Claude Code's own reason for the miss |
| **Fields** | Tokens and timestamps (already read). New: `message.diagnostics.cache_miss_reason.type` |
| **Example** | **85%** of requests after a >1 h gap rebuilt the cache. Priced at each rebuild's actual cache-write rate, using Claude Code's own count of missed tokens, expiry cost **11% of all spend**: past Anthropic's 10% line. Labelled reasons, most to fewest: `previous_message_not_found` (expiry), `messages_changed`, `system_changed`, `model_changed`, `tools_changed` |
| **Flag** | Anthropic's 10% rule, applied to expiry misses only (`previous_message_not_found`): the cache-expiry cost at ≥10% of recent spend. Misses with another reason are shown but don't count toward the flag |
| **Fix shown** | Start fresh, or resume from a summary, rather than reopening a 500k-token session next morning. Avoid `/model` switches mid-session (`model_changed`) |

The cache lifetime depends on version and plan: here the main thread used 1 hour and
subagents 5 minutes. Read it from the `ephemeral_1h`/`ephemeral_5m` split, don't
hard-code it.

### 3.3 Spend per active day against Anthropic's benchmark

| | |
|---|---|
| **What** | Estimated cost per active day, with the published average ($13) and 90th percentile ($30) drawn on the chart |
| **Fields** | Already computed |
| **Why** | The only sourced spend benchmark. It marks outliers worth a conversation, not quality. On seat plans, limit hits (already tracked) matter more than dollars |

### 3.4 Friction that isn't the user's fault

| | |
|---|---|
| **What** | Tool error rate by tool and MCP server; runs of ≥3 consecutive errors; refused tool calls by reason |
| **Fields** | Tool errors (already read). New: `toolDenialKind` (user-rejected, automode-blocked, automode-unavailable, permission-rule, interrupted), `permissionDecision.source`, per-thread order of tool results |
| **Example** | Overall **2%** error rate, but one MCP server fails **30%** of the time; a handful of error streaks of 3 or more |
| **Fix shown** | A broken integration, a missing permission rule, or a sandbox gap: often a team-level fix, which makes this the most useful single measure for managers |

### 3.5 Workflow adoption

| | |
|---|---|
| **What** | Share of sessions that use subagents, plan mode, a project CLAUDE.md, skills, and hooks (e.g. a Stop hook as an automatic check) |
| **Fields** | Subagents and skills (already read). New: per-prompt `permissionMode`, the `instructions` attachment's `files[].type` (Project / User / AutoMem; never path or content), hook summaries |
| **Example** | Subagents run most requests but appear in only a quarter of sessions. Plan mode in a few percent of sessions. A project CLAUDE.md loaded in under half of sessions |
| **Flag** | None; this is a profile. Plan mode is shown against sessions that edit many files, since the docs recommend it for multi-file work, not for one-line fixes |

### 3.6 Recent sessions, scored for habits

Each refresh shows the last ~5 sessions as cards, each tagged with whichever
habits fired: *ran past 500k for 40 turns*, *auto-compacted 3×*, *resumed cold:
$4 re-cached*, *3 errors in a row*, *ended on an interrupt*. The Sessions table
gets the same tags as a column, filterable. This is the "how have my recent
sessions behaved?" view: specific, recent, and each tag links to its fix.

---

## 4. Signals that need a privacy exception

These signals are the closest to Anthropic's own success measures, and each needs a
small, explicit exception to the no-content rule. **Interrupts** and
**verification** were approved and are built (Phase 1); both are described in
[Privacy](privacy.md). The other three are not built.

| Signal | What it needs | Why it's worth it |
|---|---|---|
| **Interrupts** | Test whether a user record starts with Claude Code's own fixed marker `[Request interrupted by user`, and keep only a count. Same class as the usage-limit exception already in `parser.py` | The only way to see mid-generation stops. In a sample log set, nearly one in five sessions *ends* on one |
| **Correction clusters** | Interrupts + user rejections + rewinds, clustered within one session | Direct proxy for "after two failed corrections, `/clear`", which is sourced. Show clusters, not the raw rate, because experts interrupt more overall |
| **Verification present** | Classify each Bash command in memory into a category (test / build / lint / git commit) and keep only the category | Closest proxy for "verified success", the strongest outcome signal in Anthropic's research. Sessions that edit code but never run a check are the trust-then-verify gap |
| **Blind retries** | In-memory hash of a failed tool call's input, compared with the next call | Catches retrying the same failing command. Usually rare, but a real thrash signal across a team |
| **Edit churn** | In-memory hash of edited file paths | One file edited 30+ times in a session. Confounded with normal iteration, so show it only alongside errors |

**Rejected:** detecting corrections or frustration from prompt text, and telling
learning apart from pure delegation. Both need the content itself.

---

## 5. Measured, then dropped

| Idea | Why it was dropped |
|---|---|
| Cache hit ratio | ~98% for everyone: it doesn't separate good from poor use. The cold-cache tax (§3.2) is the useful form |
| "Same tool after an error" | 78% after an error vs 72% after a success: Bash dominates the base rate |
| Opus on tiny turns | Under 1% of spend here; on a big context a tiny turn costs mostly cache reads, whatever the model |
| `userModified` edits | Never true in 2,312 edits |
| Server-side web search counters | 0, despite 313 WebSearch calls; count the tool calls instead |
| A single health score | Penalises experienced users (§1) |

**Profile only, never judged** (shown so people can see their own pattern):
- prompts per session and actions per prompt, with Anthropic's 5 and 12 as context
- prompts typed while Claude was busy
- long unattended turns
- how sessions end
- after-hours and weekend share, kept personal; never in the manager view

---

## 6. What managers get

The evidence opposes individual *scoring*, and this section first proposed
team aggregates only, with a minimum group of 5 (the figure Microsoft Viva
Insights is reported to use; not verified). On 2026-10-11 that was changed:
managers need to see who could use help, and aggregates can't show that. The
team view now shows each person's habits, in the open:

- **Per person, next to the team median**, with no minimum group. No
  composite score or ranking: each habit is its own column, and only the two
  with a sourced line (Anthropic's 10% rule) are highlighted.
- **Transparent.** The team card and docs/team.md tell the manager to tell
  their team first, and each person sees the same figures for themselves on
  their own Habits tab.
- **Coaching, not evaluation.** Each column says when a high or low figure is
  fine (long sessions for big work; more interrupts with experience).
- **Friction by integration, team-wide.** If an MCP server fails 29% of the time
  for everyone, that is the manager's fix, not the developers'. This is the single
  most actionable manager measure.
- **Enablement, not evaluation.** Adoption gaps (plan mode, CLAUDE.md, subagents)
  point to training or a shared CLAUDE.md, not to a person.
- **Stated purpose.** docs/team.md and the team view should say plainly that these
  measures are not for performance reviews (DX's recommendation). UK ICO guidance
  on worker monitoring expects that transparency, and a DPIA, before rollout.
- **Export.** The health measures become columns in the summary export, with the
  schema version bumped, so they merge like everything else. The first build's
  measures are already numeric and contain no content.

---

## 7. Implementation outline

**Phase 1: parser and payload. Done.** `FACTS_VERSION` 6 (7 after the review
fixes below). The parser now reads:
- `origin.kind`, `isMeta` and `isCompactSummary`, to count human prompts, with
  the per-prompt `permissionMode`
- `toolDenialKind`
- `compactMetadata.trigger`
- `message.diagnostics.cache_miss_reason` (type and missed tokens)
- the `instructions` attachment's file *types* and `queued_command`'s
  `commandMode`
- the `system` subtypes `away_summary` and `stop_hook_summary`
- tool results in transcript order, for error streaks
- the two approved exceptions: the interrupt marker, and shell-command
  categories

`claude_usage/health.py` turns these into `payload["health"]`, bucketed by the
hour and de-duplicated across fork/resume copies, plus a `health` block on every
session row. A session from a Claude Code version too old to write a record
reports that measure as not recorded (`recorded`, or `instructions: null`), not
as zero. Tests: `tests/test_health.py`.

Not read yet, because no first-build measure uses them: `isSidechain`,
`stop_reason`, `effort`, `permissionDecision`, `postTokens` and
`model_refusal_fallback`.

**Phase 2: health panel. Done.** `static/habits.js`.
- **Overview strip:** six cards (context tax, cold-cache tax, spend per active
  day, checking work, friction, workflow). Each compares the selected dates to an
  equal run of days just before them, and gives one fix with a link
  to Anthropic's docs. The baseline is the same measure over that earlier run of days: share
  of main spend, share of spend, spend per day, share of edited sessions
  checked, tool error rate, and share of sessions using subagents.
  - A status ("Healthy" / "Worth a look") appears only on the three cards with a
    sourced line: Anthropic's 10% rule and its $13/$30 benchmark.
  - Sparklines were left out: the baseline figure carries the comparison.
- **Habits tab:** follows the dashboard's date range. It shows:
  - interaction tiles (prompts, requests per prompt, interrupts, compactions,
    refusals)
  - main-conversation spend by context size over time
  - cache misses by Claude Code's reason
  - permission mode at prompt time
  - checking work
  - refused tool calls
  - the recent-session cards (§3.6)
- **Sessions table:** a Habits column of the same tags.

**Phase 3: team. Done.**
- **Export schema 7:** the summary and sessions files carry the health columns,
  and a new tools file carries calls and errors per tool and MCP integration
  (see [Export](export.md)).
- **Team merge:** sums the new columns and de-duplicates the tools file like
  the breakdown.
- **Team dashboard:** adds *Habits across the team*, distributions only and
  hidden below five people (also when the people filter narrows the group), and
  *Friction by integration*. Replaced on 2026-10-11 by *Habits by person*; see
  §6.
- **Statement:** docs/team.md and the card say plainly that these are not a
  performance measure.
- **Exports from before schema 7** count as "no data", never as zeros.

**Review fixes. Done.** An adversarial review of phases 1–3 found these,
each now covered by `tests/test_health_review.py` or `tests/test_health_ui.py`:
- **Prompts** no longer include Claude Code's markup for slash commands, their
  output and `!` shell commands (`<command-name>`, `<local-command-stdout>`,
  `<bash-input>`, `<ci-monitor-event>` and the like), about 8% of what was
  counted. Tested as a prefix only. Nor do these records count as the session's
  last turn, so a session that ends on an interrupt and then `/exit` still
  ended on an interrupt.
- **Queued prompts** count only when the attachment's `origin` is human or
  missing and it isn't `isMeta`: most were relayed from another session.
- **One record, two events.** A refusal by interrupt is both a refusal and an
  interrupt; events are de-duplicated on (uuid, kind), not uuid alone.
- **Check categories** see through a leading path (`.venv/bin/pytest`), more
  wrappers (`timeout`, `xvfb-run`, `uv run --with x`) and options before the
  subcommand (`git -c k=v commit`, `make -C dir test`, `npm --prefix x test`),
  and know `python manage.py test` and `node --test`. They no longer count
  heredoc bodies (unless fed to a shell), dev servers and watchers (`next dev`,
  bare `vite`, `webpack serve`, `rollup -w`), `--version`, or formatters run to
  rewrite (`black .`, `ruff format`); a formatter's check mode is still lint.
- **Checked after last edit** now looks at the last *code* edit. Edits to prose
  (`.md`, `.txt`, `.rst` and the like) and to anything under a `.claude` folder
  (memory, plans, settings, `launch.json`) aren't checked by any test, so one
  written after the last test run no longer marks the session "unchecked
  edits". A session whose only edits were of that kind had nothing to check
  (`null`). On real logs, 4 of 7 "unchecked" sessions had run tests, one of
  them many times over. The path is tested in memory; only the yes/no is kept.
- **Context tax** is the main thread's share everywhere: long-context cost over
  main-conversation cost in the strip, the session tags, the export
  (`long_context_cost_usd` / `est_cost_main_usd`) and the team view. The strip
  had divided by all spend, subagents included. "Long" is more than 200k
  tokens in both the session figure and the banded rows; exactly 200k had
  counted as long in one and not the other (the flag later moved to 500k on
  2026-10-14; see §3.1).
- **Habits tab:** the context chart never goes finer than an hour (its rows are
  hourly); count charts have whole-number ticks; compactions show "—" when
  this Claude Code version doesn't record them.
- **Team view:** the floor of five was tightened (active people only, each
  figure on its own, counts for the whole team only), then replaced on
  2026-10-11 by a per-person view; see §6 and [Team](team.md).

**Phase 4, only if approved:** the remaining §4 signals (correction clusters,
blind retries, edit churn), one at a time, each documented in docs/privacy.md and
covered by the sentinel privacy test.

---

## 8. Decisions

The design choices, and where they changed.

1. **§4 signals:** the interrupt marker and shell-command categories are on.
   Correction clusters, blind retries and edit churn are not.
2. **Manager visibility:** aggregates only, with a minimum group of 5.
   *Changed 2026-10-11:* per person, in the open, next to the team median (§6).
   The floor only hid figures the browser already received, and aggregates
   couldn't show a manager who needed help.
3. **Placement:** a strip on the Overview, linking to a fuller Habits tab.
4. **Our unsourced defaults:** "long context" is more than 200k tokens, and an
   error streak is 3 failed tool calls in a row. Both are tunable and labelled as
   ours in the panel.
   *Changed 2026-10-14:* the flagged "long context" cutoff is now more than 500k
   tokens, because 200k flagged everyone on 1M-context models. The 200k share is
   still shown, unflagged. Export schema 10 carries the 500k cost
   (`very_long_context_cost_usd`).

---

## Sources

Anthropic (Claude Code docs, current as of Oct 2026):
- [Manage costs effectively](https://code.claude.com/docs/en/costs): $13 per
  active day average, <$30 for 90%, $150–250 per month; `/usage` behaviour flags at
  ≥10%; "Why usage climbs in a long session"; CLAUDE.md under 200 lines; Sonnet for
  most coding, Haiku for simple subagents.
- [Best practices for Claude Code](https://code.claude.com/docs/en/best-practices):
  context as the key constraint; verification; plan mode for multi-file or
  uncertain work; "more than twice" corrections → `/clear`; the five failure
  patterns.
- [Monitoring (OpenTelemetry)](https://code.claude.com/docs/en/monitoring-usage)
  and [Analytics](https://code.claude.com/docs/en/analytics): the metrics Anthropic
  itself exposes (sessions, lines, commits, PRs, cost, tokens, edit decisions,
  active time, compaction events).
- [Claude Code Analytics API](https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api).

Anthropic research:
- [Agentic coding and persistent returns to expertise](https://www.anthropic.com/research/claude-code-expertise),
  16 Jun 2026: actions per prompt, verified success, abandonment by expertise.
- [Measuring AI agent autonomy in practice](https://www.anthropic.com/research/measuring-agent-autonomy),
  18 Feb 2026: interrupt and auto-approve rates by experience.
- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
  29 Sep 2025.

Context degradation:
- Chroma, [Context Rot](https://www.trychroma.com/research/context-rot),
  14 Jul 2025.

Productivity measurement:
- METR, [early-2025 AI and experienced OSS developers](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/),
  10 Jul 2025, and its [Feb 2026 update](https://metr.org/blog/2026-02-24-uplift-update/).
- Ziegler et al., "Productivity assessment of neural code completion", 2022.
- [DORA metrics guide](https://dora.dev/guides/dora-metrics-four-keys/).
- Forsgren et al., "The SPACE of developer productivity", 2021. The full text
  couldn't be retrieved.
- UK ICO, "Employment practices: monitoring workers", Oct 2023.

Not verified:
- the SPACE full text and its "at least three dimensions" recommendation
- DX Core 4's metric table (image only)
- the Viva Insights minimum of 5 (from search excerpts)
- a "93% of permission prompts approved" figure that one secondary summary
  attributed to Anthropic but the autonomy study does not contain
