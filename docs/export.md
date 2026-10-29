# Export for business review

Click **Export (.zip)** in the dashboard header. One click downloads one archive
holding all six CSVs, for the date range currently selected on screen — project and
model filters are deliberately *not* applied, because a filter left on by
accident would produce a partial file that still looks complete.

It is a zip rather than separate CSV downloads because browsers refuse multiple
automatic downloads arising from a single user gesture: a button that fired both
delivered the summary and had the sessions file silently dropped. One archive is
one download, so the limit never applies.

The same thing from the command line, for scripting or a cron job:

```bash
python3 collect.py --export                    # six CSVs in the current directory
python3 collect.py --export ~/Desktop          # ...or somewhere else
python3 collect.py --export . --since 2026-07-01 --until 2026-09-30
python3 collect.py --export . --tz Europe/London   # bucket days and weeks in a named zone
```

### The export zone

An export puts every day and week in **one zone that the team chooses**, not in
your computer's zone. Without it, the same logs exported from a laptop in Tokyo
and again from one in Los Angeles land in different weeks, and the team merge
cannot repair that afterwards (weekly rows can't be re-bucketed). The zone is,
in order:

1. `--tz ZONE` on `collect.py --export`, an IANA name such as `Europe/London` or
   `America/New_York`;
2. else the `CLAUDE_USAGE_EXPORT_TZ` environment variable (this is also what the
   dashboard's **Export (.zip)** button uses, since it has no flag);
3. else **UTC**.

`UTC` (also `Z` and `Etc/UTC`) needs no zone data. A named zone needs the
system's zone database; on Windows without it the export stops and asks you to
use `UTC` or run `pip install tzdata`. An unknown name is an error, not a silent
fallback. **Everyone whose exports are merged should use the same zone**: set it
once (a shared `CLAUDE_USAGE_EXPORT_TZ`, or `--tz` in the cron job) and weeks
line up across people.

What follows the export zone: every week and day bucket, `started_at` and
`ended_at`, the day `--since` / `--until` and the date-range buttons are
interpreted in, "today" (the cap on the window), `exported_at`, the date in the
file name, and `person_active_days` and the seat-use figures built on it.
`bucket_zone` on every row names it; `timezone` is its UTC offset at export time.

Things to know:

- **With UTC, work near midnight lands in the UTC day.** A Monday morning in
  Tokyo falls in the previous week; late evening in Los Angeles falls in the next
  day. Pick the zone most of the team works in if that matters.
- **The personal dashboard stays in your computer's zone** (the live page,
  `data.json` and `--static`), so its days can differ from the export's. The
  **Export (.zip)** button shows the export zone beside it (and in its tooltip),
  because the dates picked on screen are read in that zone, not yours.
- **Exports made before schema 8 were bucketed in the exporting machine's own
  zone** and have a blank `bucket_zone`. Old and new exports of one machine can
  still double count or lose work while their windows overlap, so re-export
  after upgrading.

Writes six files, stamped with your git identity:

| File | One row per |
|---|---|
| `claude-usage-summary-<you>-<date>.csv` | person x project x week |
| `claude-usage-sessions-<you>-<date>.csv` | session |
| `claude-usage-breakdown-<you>-<date>.csv` | person x week x project x model x thread |
| `claude-usage-limits-<you>-<date>.csv` | usage-limit episode |
| `claude-usage-prs-<you>-<date>.csv` | pull request |
| `claude-usage-tools-<you>-<date>.csv` | person x week x project x tool |

### Usage-health columns and the tools file (schema 7)

The [usage-health measures](usage-health.md) travel in the export, so a team
view can summarise habits across people. They are counts and dollars only.

**Summary file**, per person x project x week. Per-request measures land in the
week they happened. The `sessions_*` counts land in the week each session
started, like lines and PRs.

| Column | What it is |
|---|---|
| `long_context_cost_usd` | Main-conversation cost of requests carrying more than 200k tokens. The dashboards show this over `est_cost_main_usd` as the unflagged "Over 200k" figure, not over `est_cost_usd`: subagents start with their own, smaller contexts |
| `very_long_context_cost_usd` | Main-conversation cost of requests carrying more than 500k tokens (schema 10). The flagged context tax is this over `est_cost_main_usd`. Absent in exports made before schema 10 (blank once merged) |
| `cold_cache_usd` | Extra paid to rebuild the prompt cache after it expired (Claude Code's own label for the miss), over reading the same tokens warm |
| `cache_miss_usd` | The same, for every labelled cache miss whatever the reason |
| `prompts`, `interrupts` | Prompts you typed (not slash commands or their output), and requests you stopped |
| `auto_compactions`, `manual_compactions` | Context compactions by how they were triggered |
| `tool_refusals` | Tool calls refused by you, auto mode or a permission rule |
| `error_streaks` | Runs of 3 or more consecutive failed tool calls |
| `test_runs`, `lint_runs`, `build_runs`, `commits` | Shell commands Claude ran, by category. Only the category is kept, never the command |
| `sessions_edited`, `sessions_checked_after_edit` | Sessions that edited code, and those that ran a test, lint or build after their last code edit. Edits to prose (`.md`, `.txt` and the like) and to Claude Code's own files under `.claude` don't count: nothing checks them |
| `sessions_plan_mode`, `sessions_with_subagents` | Sessions that used plan mode, or spawned a subagent |
| `sessions_instructions_recorded`, `sessions_with_project_claude_md` | Sessions whose Claude Code version records which instruction files it loaded, and those where a project CLAUDE.md was loaded. The first is the denominator for the second |

**Sessions file**, one row per session:
- `peak_context_tokens`, `long_context_cost_usd` and `very_long_context_cost_usd`
- `cold_cache_usd`
- `prompts`, `interrupts`, `auto_compactions` and `tool_refusals`
- `edits`, `checked_after_last_edit` and `max_error_streak`
- `plan_mode`, `project_claude_md` and `ended_on_interrupt`

A measure the session's Claude Code version doesn't record is blank, not 0.

`session_id` is the id Claude Code wrote. When the same id is found in more than one
project folder (a copied or moved project), the first keeps it and the others are
`<id>#2`, `<id>#3`, ... in the order they were read -- never the folder name -- so the
team merge, which keys sessions on this column, keeps every one of them. The limits and
PRs files name the same ids. A log set with no repeated id is exported unchanged.

**Tools file**, one row per person x week x project x tool:
- `calls` and `errors`
- `integration`: the MCP server a tool belongs to, or `built-in`

This is what the team view's *Friction by integration* table is built from.

### Imputed output and unlogged cost (schema 9)

From Claude Code 2.1.284 until 2.1.307 fixed it, most subagent responses
reached the transcript with only a placeholder output count (2–20 tokens),
never the final one. From
schema 9:

| Column | Sheets | What it is |
|---|---|---|
| `output_tokens_imputed` | summary, sessions, breakdown | Output tokens estimated for those responses from how much each one wrote. They are **already inside** `output_tokens`, `total_tokens` and every `est_cost_*` column; this column says how much. 0 where every response logged its final count |
| `est_cost_unlogged_usd` | summary, sessions | What Claude Code's own cost counter recorded and no transcript did, priced at list rates. It is **not** inside `est_cost_usd`; add the two together for the fuller figure. Like `cli_reported_cost_usd`, it is a whole-counter figure, so it lands in the week the session started. **Blank** where no session in the row kept a counter (most desktop sessions): nothing measured it, so it is not zero |

So `est_cost_usd` in a schema-9 file is higher than the same work in an older
file, by the imputed output. See [Cost estimates](cost.md#subagent-output-and-calls-the-transcripts-miss).

### The PRs file: which repo each PR actually went to

A session started in one folder regularly opens PRs in another repo — on a real
log set, PRs opened from one folder went to three different repos, and nine from
another went to a personal repo. Counting PRs by `project` puts all of those in
the wrong place. This file has one row per PR, keyed by the repo in the PR's own
URL:

| Column | Meaning |
|---|---|
| `pr_url`, `pr_number`, `opened_on` | The PR |
| `pr_repo` | The repo it was opened in, as a credential-free `host/owner/repo` key — the same form as `project_repo` |
| `project`, `project_repo`, `session_id` | Where the session that opened it ran |
| `in_project_repo` | `yes` / `no` — whether those are the same repo. Blank when the folder isn't a git repo, so it can't be told |
| `session_cost_usd`, `session_prs` | The cost of the session that opened it, and how many PRs that session opened. **Divide, don't sum**: a session that opened three PRs carries its cost on all three rows |

The sessions file also gains `pr_repos`, the repos that session's PRs went to.
PRs only appear where Claude Code logged a `pr-link` entry, so treat counts as a
floor, not a census.

### The limits file: who ran out of allowance, and what it cost them

On a seat plan the binding constraint is the usage allowance, not dollars. When a
limit stops a request, Claude Code writes a notice into the transcript — *"You've
hit your weekly limit · resets Oct 2 at 4am (America/New_York)"*. Those notices
are the only record of the allowance running out (the logs carry no running
"% used"), so this file is built from them.

One row per **episode**: Claude Code repeats the notice on every attempt while a
limit is in force, so every notice of one limit with one reset time is one row.

| Column | Meaning |
|---|---|
| `limit_type` | `weekly`, `five_hour`, `spend_cap` (your extra-usage cap), `opus`, or `other` |
| `hit_at`, `last_notice_at`, `resets_at`, `notices` | When it started, the last notice, when it lifts, and how many notices |
| `locked_out` | `yes` when **nothing got through until the reset** — the person could not use Claude Code at all |
| `blocked_hours` | For a lock-out: first notice to reset. Otherwise only how long the notices kept coming — someone who hits a limit at 5pm and is back at 9am was away, not blocked |
| `resumed_at` | First request that succeeded after the notices, if before the reset |
| `usage_before_hit_usd` | For a weekly or 5-hour limit: list-price usage in that window up to the hit — roughly what the allowance absorbed |
| `usage_after_hit_usd`, `requests_after_hit` | Usage between the hit and the reset. After a weekly limit with extra usage on, this is the **extra-usage spend**, at list price |
| `project`, `project_repo`, `session_id` | Where the person was working when it happened |

`resets_at` is blank when the reset time can't be read: an hour outside 1-12, a
dated reset that isn't within 8 days after the notice (including one already
past), or a notice with no reset time. A zone abbreviation the tool doesn't
recognise is read as the machine's local time instead. With a blank `resets_at`
the episode has `locked_out` `no` (and is never shown as ongoing), is left out of
the allowance estimate, and is not combined across machines in a team merge.

What this can show, for example: the weekly limit was hit after a large amount
of list-price usage that week; only a small amount of extra usage ran before the
individual spend cap was hit; then **more than two days locked out** until the
weekly reset. One setting — the spend cap — can cost days of work.

Two caveats. The weekly limit also counts Claude chat usage on claude.ai, which
isn't in these logs, so `usage_before_hit_usd` is Claude Code's share of the
allowance, not all of it. And list price is an estimate of what extra usage is
billed at, not the invoice.

### The breakdown file: tokens and cost by model and by subagent

The summary and sessions files give tokens as totals. The breakdown file splits
them — and cost — by **model** and by **thread**: the main conversation, or
which kind of subagent did the work. It is in long format (one row per
combination, every measure a column), which is what an Excel pivot table or
Power BI wants. A new model or subagent type becomes new rows, never a new
column.

| Column | Meaning |
|---|---|
| `model`, `model_family` | The exact model id, and `opus` / `sonnet` / `haiku` / `fable` / `mythos` for grouping |
| `thread` | `main` (the conversation you typed into) or `subagent` |
| `subagent_type` | e.g. `general-purpose`, `Explore`; blank for `main` |
| `requests`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `thinking_tokens` | As in the other files |
| `cache_write_5m_tokens`, `cache_write_1h_tokens`, `cache_write_tokens` | Cache writes by lifetime (they're priced differently), and their sum |
| `est_cost_usd`, `cache_saved_usd`, `cost_basis` | As in the other files |
| `project_repo` and the shared context columns | As in the summary file |

It is dated by request, like the summary, so **for any week and project its
rows sum exactly to that summary row** — tokens, requests and cost. Weeks with
no activity have no rows here; the summary file carries those.

Why it matters: tokens and cost tell different stories. On a real log set,
subagents were 36% of cost but 59% of output tokens and 70% of thinking tokens,
because they mostly run on cheaper models. And Fable was 21% of cost for 7% of
output. On a seat plan, tokens are what use up the allowance, so look at both.

### How the summary and sessions files differ

Same data, two grains. Over your full history their cost, token and request
totals are identical; what differs is what a row means and how a date range
cuts it.

| | Summary | Sessions |
|---|---|---|
| **Use it for** | trends, week-over-week, per-project totals, forecasting | finding the expensive sessions and why they cost what they did |
| **Usage is dated by** | when each *request* happened — a session running Friday into Monday is split across two weeks | when the *session started* — the session stays whole |
| **Only here** | `sessions_started` / `sessions_active`, `subagent_cost_share`, web lookups, the `sessions_with_*` and `cost_with_*` coverage columns | `session_id`, `title`, start and end times, subagent count, `pr_urls`, entrypoint, CLI version, branch, permission mode, `is_fork_copy` |

**With a date range, the two files' totals can legitimately differ.** A session
that started before the window but ran into it has its in-window requests in the
summary and no row in the sessions file. A session that started inside the
window but ran past its end is counted whole in the sessions file and only up to
the end date in the summary. For a period total, use the summary; for "which
sessions did this person run", use the sessions file.

The sessions file can also total less than the summary with no date range at all:
a subagent whose parent transcript has been deleted has no session of its own, so
its cost appears in the summary, breakdown and tools files (under the project of
its working directory) but in no row of the sessions file.

Every row carries `person_email`, so a manager can collect several people's
files and concatenate them (Excel, Power BI, `cat`) to get a team view. This
tool only ever reads one machine's logs, so the per-person file is the unit that
travels — there is no central collection and nothing is uploaded. The export
writes files to disk and stops; they reach anyone else only because you send
them.

CLI options: `--as EMAIL` to override the identity (default:
`git config user.email`, then the OS user name, then `unknown`; the team view can't
tell people apart on those, so pass `--as` if you have no Claude account login and
no git email), `--since` / `--until` to limit the window,
`--no-titles` to leave session titles out of your own export.

The button is served by `GET /api/export/all.zip`. The individual files are also
available at `/api/export/summary.csv` and `/api/export/sessions.csv` if you want
to pull one with `curl -H 'X-Claude-Usage: 1' ...` (the server refuses
`/api/*` requests that don't say they come from the dashboard; see
[cli.md](cli.md)). All three accept `?from=`/`?to=` (both `YYYY-MM-DD`) and
`?titles=0`. Like every other route they are localhost-only and reject a
non-localhost `Host` header, so no other site can pull the file from you.

## What is in the export file

**The export identifies you and describes your work in detail. This is
deliberate — it is what makes the file useful to a manager — but you should
know exactly what it contains before you send it to anyone.**

Included by default:

| Column | What it is |
|---|---|
| `person_email`, `person_name` | Your `git config` identity, on every row |
| `title` | The session title — **generated by the model from your own conversation**, so it describes what you were working on in your words |
| `project` | The **full absolute path**, e.g. `~/workspace/Acme Tender Response`, which carries directory layout and often a client or project name |
| `git_branch` | Your branch name, which often names a feature or customer |
| `pr_urls` | Full links to the pull requests you opened |
| `started_at`, `ended_at`, `main_active_minutes` (sessions); `main_active_hours` (summary) | When you were working (in the export zone, with its UTC offset) and for how long |
| cost, tokens, requests, tool calls, errors | Your spend and activity |
| the usage-health columns and the tools file | How you work: prompts and interrupts, how large your context gets, cache rebuilds, checks run after edits, refused and failing tool calls by tool name. Counts and dollars only (see above). The team dashboard shows these **per person**, next to the team median |
| `cli_version`, `entrypoint`, `permission_mode` | Which Claude Code build and mode you used |
| `claude_account_email`, `seat_tier`, `rate_limit_tier`, `extra_usage_enabled` | The Claude account signed in on this machine and its plan, from Claude Code's own account cache (`~/.claude.json`). Only these four fields are read; account and organisation ids are not |
| `project_repo` | The project's git `origin`, e.g. `github.com/example-org/example-repo`. Any `user:token@` embedded in the remote URL is stripped |

**Never included**, in the export or anywhere else in this tool: your prompts,
Claude's replies, message text, tool inputs or outputs, code, file contents, or
the contents of any file you worked on. The parser never keeps them (for a
subagent response it measures their *length*, in memory, to estimate its output;
see [Privacy](privacy.md)), and
`tests/test_privacy.py` plants a sentinel string through a fixture transcript
and asserts it reaches neither the dashboard payload nor any CSV.

The one piece of text the parser does read is Claude Code's own **usage-limit
notice** ("You've hit your weekly limit · resets …"), on records Claude Code
flags as an API error with a rate-limit status. It keeps the limit type and the
reset time and drops the text; `tests/test_limits.py` plants the same sentinel
inside a notice to prove it.

If you would rather not share session titles, use the CLI with `--no-titles`
(the dashboard button always includes them):

```bash
python3 collect.py --export . --no-titles
```

That blanks the `title` column and nothing else. Paths, branches and PR links
stay, because a leader cannot attribute cost to a project without them — that
was a deliberate decision, not an oversight. If your team needs those stripped
as well, raise it with whoever owns this repo; the change is small.

**Text that looks like a formula is quoted.** Titles, branches, folder names
and git names are your own text, and a leader will open the file in Excel. A
cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return would run
as a formula there, so it is written with a leading `'` (OWASP's advice for CSV
files) — `=HYPERLINK(...)` becomes `'=HYPERLINK(...)`. Numbers, negative ones
included, and UTC offsets like `-04:00` are left alone. The team merge does the
same for its merged files.

**Accents in Excel.** The merged `team-*.csv` files start with a UTF-8 byte-order
mark, so Excel on Windows opens "José" as written, not as ANSI. The per-person
exports have none, which keeps `cat`-joining them working.

**For whoever rolls this out:** tell people the above before asking them to run
an export. The behaviour is reasonable for team-level cost analysis; the problem
is only ever discovering it after the fact.

## Combining files from several people

Every row carries enough about its own origin that a stacked file can be
checked rather than trusted. These columns are on **every** sheet, identical on
every row of one export:

| Column | Use it to |
|---|---|
| `claude_account_email` | **Join to the seat roster.** This is the Claude account, so it matches the admin console; `person_email` is git's address and often a personal or noreply one |
| `seat_tier`, `rate_limit_tier`, `extra_usage_enabled` | Compare people on the same plan, and see who can run past their allowance |
| `schema_version` | Refuse to stack files with different versions without checking. This release is **12** (12 stopped counting transcripts that never got a reply in `sessions_started`; 11 added `week_active_days` to the summary sheet; 10 added `very_long_context_cost_usd` to the summary and sessions sheets; 9 added `output_tokens_imputed` and `est_cost_unlogged_usd`, and imputed subagent output now counts in `output_tokens` and `est_cost_*`; 8 added `bucket_zone` and moved every day and week to the export zone; 7 added the usage-health columns and the tools file; 6 added `machine_id`; 5 the PRs file and `pr_repos`; 4 the limits file and the friction and allowance columns; 3 the breakdown file; 2 these context columns); a file with no such column is 1 |
| `exported_at`, `window_from`, `window_to` | Spot **overlapping exports** — two files from one person whose windows overlap will double-count the overlap. Keep the later one, or dedupe sessions on `claude_account_email` + `session_id` |
| `bucket_zone` | The zone every date in the file was bucketed in: the [export zone](#the-export-zone) (`UTC` unless the team set one). Blank in files made before schema 8, which used the exporting machine's own zone. Last column on every sheet until schema 9, whose columns follow it on the summary, sessions and breakdown sheets; schema 10 appends `very_long_context_cost_usd` after those on the summary and sessions sheets, and schema 11 appends `week_active_days` after that on the summary sheet |
| `timezone` | The UTC offset of the `bucket_zone` at export time. Every date is in that zone — including a session's `started_at` and `ended_at`, and the day a session is filtered and counted by; weeks start at Monday midnight there. The exceptions are the instants `hit_at`, `last_notice_at`, `resets_at`, `resumed_at`, `projected_limit_hit_at` and `allowance_week_start`, which are UTC timestamps whatever the zone. (Before schema 8 it was the exporter's own offset) |
| `machine_id` | Which machine the export came from: random, made once and kept in the tool's data folder (`~/.usage-lens`), never derived from the hostname or hardware. The team merge uses it to add a person's machines together while still de-duplicating re-exports from one machine. Last column on every sheet as of schema 6; schema 7's columns and `bucket_zone` follow it |
| `tool_version`, `pricing_version` | Each request is priced at the rate in force on its date, from the dated table `pricing_version` fingerprints. Two files with different values were priced by different tables (for example, one made before a price change was recorded) |
| `person_active_days`, `window_workdays` | **Seat use:** active days ÷ workdays in the window |
| `last_active_date`, `seat_idle` | `seat_idle = yes` when nothing was recorded in the 14 days before the export, or nothing at all. A seat with no usage still exports a row for the current week, so the team view can name it |
| `cli_history_from`, `cli_history_truncated` | Whether Claude Code has already deleted this person's older CLI transcripts (see below) |

Per-row columns (`project_repo` is on every sheet; the rest are summary only):

| Column | Meaning |
|---|---|
| `project_repo` | The repo key for the project. Use it rather than `project` to group across people — one repo cloned into different folders is one key. Blank when the folder isn't a git repo, or is gone and opened no PRs |
| `sessions_active_cli` | How many of `sessions_active` ran in the CLI; the rest ran in Desktop. A subagent whose parent transcript is gone counts in `sessions_active` (not in `sessions_started`) and is reported under Desktop here |
| `active_days` | Days in this week with any request, for this project |
| `week_active_days` | The person's distinct days in this week with any request, across all projects (schema 11). The same on every row of the week, and 0 for a week without requests, so seat use can be measured for any range of weeks. `active_days` summed over projects overcounts a day worked in two projects. The team merge adds the column across exports that split a week (their days don't overlap). Blank in files made before schema 11 |
| `week_days_in_window`, `partial_week` | The first and last weeks are usually part-weeks. Normalise by `week_days_in_window` or exclude `partial_week = yes` rows before trending |
| `cli_history_complete` | `no` when this week predates `cli_history_from` and CLI history was truncated — the row holds only Desktop work |

Friction (summary and sessions; schema 4):

| Column | Meaning |
|---|---|
| `api_retries`, `retry_wait_minutes` | API calls Claude Code had to retry (network, overload, rate limit), and the time spent waiting between attempts |
| `turns`, `long_turns`, `turn_hours` / `turn_minutes` | Completed turns, those over 10 minutes, and total turn time — how long people waited on Claude |
| `context_compactions` | Times the context filled up and was summarised. Frequent compaction means long sessions that would be cheaper split up |
| `limit_hits`, `blocked_hours` | Limit episodes that started here, and their blocked time (see the limits file) |

Allowance (every sheet, same on every row; blank until the weekly limit has been hit once):

| Column | Meaning |
|---|---|
| `allowance_est_usd` | Estimated weekly allowance: list-price usage when the weekly limit was last hit |
| `allowance_week_start`, `allowance_week_used_usd`, `allowance_week_used_pct` | The current limit week and how much of the estimate it has used |
| `allowance_status`, `projected_limit_hit_at` | `on_track`, `on_pace_to_hit` (with when, at this week's average rate) or `over` |

**Weeks with no activity have a row.** It has a blank `project` and zeros, so a
person's average week includes their quiet weeks. For a per-project average,
divide by the number of weeks in the window, not by the number of rows.

**The window stops at the export date.** Asking for a range that runs into the
future exports up to today; `window_to` says so.

### Short history: CLI transcripts are deleted after 30 days

Claude Code deletes CLI transcripts that haven't been touched for
`cleanupPeriodDays` — **30 days by default**. Desktop transcripts are kept far
longer. So a CLI user's export can only reach back about a month, while a
Desktop user's reaches back months, and the two look like very different
adopters when they aren't.

The tool detects this from the data itself (the oldest surviving CLI transcript
is about 30 days old) rather than reading the setting, because the setting can
live in several places. When it happens, the dashboard shows a notice,
`collect.py` prints one, and every affected summary row says
`cli_history_complete = no`.

**The fix is one line, set once by each person.** Where managed
policy isn't pushed to every machine, this is per person, not per organisation: add it to
your own `~/.claude/settings.json` (create the file if it doesn't exist; if it
does, add the key alongside what's there):

```json
{
  "cleanupPeriodDays": 365
}
```

It stops further deletion from the day it's set. Transcripts already deleted
can't be recovered, so it's worth including in the message that asks people to
start using this tool — the sooner everyone sets it, the sooner their histories
line up.

## Read this before computing a ratio

**These logs measure spend and activity well, and delivered value poorly.** Cost,
tokens, requests and session counts are complete. Lines changed, PRs and the
CLI's own cost figure exist only where Claude Code wrote a `cost-state` or
`pr-link` entry — on a representative log set that was **14 of 50 sessions** for
lines changed. So a naive `cost / lines_added` is wrong for most of the work.

Three conventions make that visible rather than silent:

- **Lines changed and `cli_reported_cost_usd` are counted once per CLI
  counter.** A branched or resumed session that carries on its parent's counter
  inherits the parent's running totals; they appear on the session that did
  most of the work, and the branch's row is blank. Before this, summing either
  column double-counted — by 63% for lines on a real log set.
- **Missing numbers are blank, never `0`.** An empty `lines_added` means "not
  recorded", not "nothing delivered". A zero would drag down any average taken
  over it.
- **`sessions_with_line_data`, `sessions_with_pr_data` and
  `sessions_with_cli_cost`** say how many of the row's sessions actually carried
  each signal. Use them as the denominator, not `sessions_started`.
- **`cost_with_line_data_usd` and `cost_with_pr_data_usd`** say the same thing in
  money, and usually tell a kinder story: on a real log set only 30% of sessions
  report lines changed, but those sessions are 66% of spend, because the covered
  ones are the large ones. For a cost-per-line figure this is the denominator you
  want — dividing total cost by lines from a third of sessions overstates it by
  roughly half. Whether a session reports at all depends largely on whether it
  ran in the CLI or the desktop app; see
  [Known limitations](limitations.md).
- **Token and cost columns come from the transcripts, which don't record every
  model call Claude Code makes.** Subagent output that Claude Code logged only
  as a placeholder is imputed and included (`output_tokens_imputed` says how
  much). The calls no transcript records are in `est_cost_unlogged_usd`,
  separate from `est_cost_usd`, wherever a session kept Claude Code's own
  counter. Where none did, that column is blank and the cost is a floor. See
  [Known limitations](limitations.md).
- **Every cost column is priced at public API list rates**, and each row says so
  in `cost_basis` (`api_list_price`). On a seat-based Enterprise plan that is a
  measure of how much was used, not an invoice figure. The part that matters as
  real money is usage beyond the seat allowance, which is billed as extra usage
  up to each person's spend cap.
- **`est_cost_main_usd` + `est_cost_subagent_usd` = `est_cost_usd`** on every
  row of both files. Main is the conversation you typed into; subagent is work
  it delegated.
- **`cache_saved_usd`** is what prompt caching saved, net of what the cache cost
  to write. It is usually several times `est_cost_usd` — on a real log set,
  about six times as much saved as spent — so a cost conversation that omits it is
  missing most of the story. New columns are appended to the end of the schema
  rather than inserted, so a sheet that reads by position keeps working.
- **`main_active_hours` is main-thread keyboard-adjacent time**, not elapsed time
  and not including subagent execution. A session that delegated heavily to
  subagents shows little main-thread time against a large cost. Use it as a
  rough engagement signal, not as effort.

Two attribution rules worth knowing:

- **`sessions_started` vs `sessions_active`.** Cost and tokens are per-request
  and split across weeks naturally; a session's own facts (active time, lines,
  PRs) are attributed to the week it *started*. So a week can legitimately show
  cost with `sessions_started = 0` — that is a long session continuing. The
  `sessions_active` column is the count most people expect next to cost.
- **Fork and resume copies are excluded**, so no session is listed twice — with
  one exception that keeps the two sheets agreeing. A fork copy normally owns no
  work, because its requests belong to the original; a few own unique subagent
  work, and dropping those made the session sheet total less than the summary
  sheet with nothing in either file to explain the gap. Those rows are kept and
  marked `is_fork_copy = yes`, so **summing either sheet gives the same total** (the one
  exception is the cost of a subagent whose parent transcript is gone, which has no session row;
  see "How the summary and sessions files differ" above).
  Count sessions from `sessions_started` in the summary, not by counting rows.
  PRs are counted from the URL-deduped list, because a few survive only inside a
  fork transcript.

Export totals reconcile exactly with the dashboard — cost, tokens, requests,
tool calls and PRs are asserted equal in `tests/test_export.py`. If one of those
disagrees with the UI, that is a bug. A transcript with no requests (for example
a prompt that never got a reply) isn't a session: the dashboard doesn't count
it, and from schema 12 neither does `sessions_started`. The sessions sheet still
lists it, with `requests` 0, so count sessions from `sessions_started`, not by
counting rows. Files made before schema 12 counted it, so a team merge of older
exports can read a session or two high.

---

[← Back to the README](../README.md)
