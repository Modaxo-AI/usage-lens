# Team view

Each person runs the tool on their own machine and clicks **Export (.zip)**. A
manager collects the zips in one folder and opens them together:

```bash
python3 serve.py --team ~/Downloads/team-exports      # the team dashboard
python3 collect.py --merge ~/Downloads/team-exports   # combined CSVs + a report
```

Both read every `claude-usage-export-*.zip` (and any loose `claude-usage-*.csv`)
under the folders or files given. Nothing is extracted to disk, and nothing
leaves the machine. In team mode the server shows **only** the exports; your own
logs and the personal export routes are switched off. Drop in another zip and
press **Refresh**.

## Set one export zone for the team

Exports bucket every day and week in a **team-set zone** (`--tz`, else the
`CLAUDE_USAGE_EXPORT_TZ` variable, else UTC; see [the export zone](export.md#the-export-zone)),
not in each person's own zone, so everyone's weeks line up and re-exporting
from anywhere gives the same buckets. Ask everyone to use the same one.

- With UTC, work near midnight lands in the UTC week: a Monday morning in Tokyo
  is still the previous week. `person_active_days` and seat use are counted in
  the export zone too.
- The personal dashboard stays in each person's own zone, so its days can
  differ from the exported ones.
- Files made before schema 8 were bucketed in the machine's own zone and have a
  blank `bucket_zone`. Old and new exports of one machine can still double
  count or lose work while their windows overlap, so ask people to re-export
  after upgrading: the report's mixed-schema-versions warning names who.
- The merge reads `bucket_zone` as follows. Two exports of one machine naming
  the **same** zone are never treated as a time-zone disagreement, even when
  their `timezone` offsets differ (daylight saving moves a named zone's offset).
  Exports without one keep the [mixed-zone handling](#what-combining-handles-for-you)
  below. If different exports name **different** zones, the report warns once,
  listing each zone with how many people used it. Exports with a blank
  `bucket_zone` are not counted as a zone.

## What combining handles for you

Stacking people's files by hand goes wrong in predictable ways. The merge fixes
each one and **says so** — in a notice on the dashboard and in
`merge-report.txt` — rather than fixing it silently:

| Problem | What the merge does |
|---|---|
| Someone exported twice, with overlapping windows | Every row is kept once, by its natural key (person + week + project, person + session, ...). Where two exports from one machine cover the same days of a week, one export supplies that whole week: the one with **complete CLI history** (so a re-export made after Claude Code's 30-day cleanup can't replace a week that still had its CLI work — whether the export flags the cleanup or just lacks sessions the other has), then the one covering **more of that week**, then the newer — so a later, shorter re-export can't replace a full week with a part-week |
| One machine re-exported from another time zone | Exports from schema 8 on say which zone they bucketed in (`bucket_zone`), and **two exports of one machine naming the same `bucket_zone` are never a disagreement**, whatever their `timezone` offsets say (a named zone's offset moves with daylight saving): none of what follows applies and the report has no note. It applies to exports **without** a `bucket_zone` (schema 7 and older, which bucketed in the machine's own zone) and to a mix of those and a newer one, and to exports naming two different zones. For those, each export buckets its weeks by its own days, so work near a Sunday/Monday midnight can sit in week W of one export and W+1 of the other. Weekly rows are already summed, so they can't be re-bucketed. Two exports of one machine count as different zones when a session both list is written in different UTC offsets (with no shared session, when their `timezone` columns differ), and their windows meet; the same zone across a daylight-saving change is one zone. For such exports the **newest** supplies the stretch both cover, and an older export's week is left out only when nothing is lost by it: every day of the week it covers is inside that stretch, the newest export holds every session the older lists that week (or its continuation), and the newest export's week isn't marked as missing CLI history. Any other week is chosen as above, so a cleaned-up re-export still can't replace a week that had its CLI work, and what the newest export lacks still raises the dropped-work warning. The report adds an info note naming the person, the two UTC offsets and the weeks. Exports whose windows don't meet are unaffected, as is an export with no `timezone` (schema 1 or older): that isn't treated as a disagreement. This is best effort: see Known limits. |
| Someone exported in pieces (to Wednesday, then from Thursday) | Exports covering **different days** of a week add up: their rows for that week are summed into one row, with `week_days_in_window` the days of both and `merged_from` listing both (`sessions_active` can count a session active on both sides of the split twice; the files carry counts, not sessions). If two exports overlap within a week and neither covers all the days the other does, one has to be chosen — rows can't be split by day — so the days only the other had are missing; the report names the week |
| The breakdown would stop adding up | Breakdown rows are always taken from the same export(s) as their summary row, so per week and project they still sum exactly |
| One person, two machines | Every export carries a `machine_id`. Rows from different machines **add up**; re-exports from the same machine are de-duplicated as above. Exports that share a session are the same logs whatever their ids say (the tool was re-cloned, or `~/.claude` is synced), so they count as one machine. Because a week is taken from one export, work only one of two such joined exports holds is dropped from the summary and breakdown. The report warns, naming the person, the weeks, the cost and (when sessions are the evidence) how many sessions, if either of two things shows it: the dropped export's **weekly cost** is higher than the chosen exports' over days they cover (a floor -- the files carry cost per week, not per day, so work only the chosen export holds hides an equal amount, and the gap must exceed half a cent); or the dropped export lists **sessions active on a covered day** (start to end, so one running across Sunday night into Monday counts in both weeks) that no chosen export holds. A session whose continuation a chosen export holds is not counted, so resuming a session between two exports is not a false alarm: the continuation has the original's start and project and either runs on past it, or (an exact copy) ends with it at no less cost in a later-ending export. When the dropped export's sessions that week are all held or accounted for, the cost gap is ignored -- the same days fall in different weeks when an export is made from another timezone. The amount is the larger of the cost gap and the sessions' estimated cost; a session lost in several weeks is credited in each, up to what the dropped export had that week and what of its cost is left. A dropped session whose id a chosen export could have listed is a warning however little it cost. A session that **started before every chosen export's window** (a rolling export from the Monday a session crossed into) can't be on that export's sessions sheet even though its work on the covered days is in the weekly rows, so its absence isn't evidence on its own: it counts only if the weekly cost gap shows it, or if the chosen exports' weekly `sessions_active` (every session with work that week, listed or not, per project) less the sessions they list wholly inside the week (with requests) leaves no room for the sessions the dropped export's own `sessions_active` shows had work that week (both counts are bounded so a session whose span merely touches the week can't raise a warning; the count is per project, so when some project is short only its sessions are reported (a cost gap with no short project reports all of them), and a project whose rows have no usable `sessions_active` -- blank, or an export with no summary file -- is judged by the cost gap alone), or if the dropped export has days the chosen ones lack. Without that, an exact rolling re-export would warn although the totals are right. The sessions file still lists the dropped sessions. Exports that share an id but each have sessions on days both cover, and none of each other's, are two machines whose tool `.cache` folder was copied: they add up, and the report says so. A session whose continuation the other export holds (same start and project, running on later, or an exact copy from a later export window) doesn't count as one the other lacks, so resuming a session between two exports doesn't make them two machines. A usage limit belongs to the account, so for a Claude-account identity an episode seen on two machines is **one** episode: its earliest hit, and extra usage from both (an identity with no Claude account keeps each machine's episodes apart) |
| One person, two identities | People are keyed by Claude account email. A git email is linked to the Claude email whenever any of that person's files carries both, so an old export (git email only, from before schema 2 added the account email) joins their new one. Case is ignored. Only an export that *predates* the column is linked this way: a current export with a **blank** Claude email (an API key, Bedrock or Vertex login, no OAuth) stays under its git email, unless it provably holds the same logs as that account (same machine or a shared session; see the next row), and a git email seen with two Claude accounts is linked to neither |
| Different people who can't be told apart | Without a Claude account email the only identity is the git email on the rows. Exports with the same git email are counted as one person; when they come from **two or more machines** (distinct `machine_id`s; exports with none count as one machine among themselves) the report **warns** (once per identity) in case they are different people on a shared address. Re-exports from one machine are the same person by construction and get no warning. A current export with a blank Claude email is joined to a Claude account that shares its git email only when it provably holds the same logs (the same `machine_id`, or a session in common -- someone who switched one machine to an API key); otherwise it is kept apart from that account and the report says how to join them. The export falls back to the OS user name (`ubuntu`, `vscode`, `admin`) or `unknown` when git has no `user.email`; the export doesn't record that it did, so any name with no `@` (including one passed with `--as`) is treated that way, and warned about the same. Their limit episodes aren't combined across machines, as they are for a Claude account. The fix is on the person's side: re-export with `python3 collect.py --export --as THEIR@EMAIL`. The same applies in reverse: one person with an OAuth machine and an API-key machine now shows as two people, and the report says so and names the Claude email to pass to `--as` on the API-key machine to join them |
| Files from different tool versions | Columns are aligned by name; anything an older file lacks reads blank. You're told which schema versions are present, and a file from a *newer* tool than yours is flagged (update your tool; re-exporting the older files doesn't help) |
| People exported in different zones | Weekly rows can't be re-bucketed, so when the exports name more than one `bucket_zone` the report **warns once**: each zone with how many people used it, and to set the same `--tz` (or `CLAUDE_USAGE_EXPORT_TZ`) so weeks line up. Exports with no `bucket_zone` (schema 7 and older) are not counted as a zone, so one zone next to them doesn't warn. The merge adds up exactly as before: only which week some work falls in can differ between people |
| The same logs exported under two identities | People are keyed by Claude account email, and `--as` only changes the git email stamped on the rows. For an OAuth export that leaves the person unchanged, so an `--as` export lands on the same person as any other from that account. For an export with a blank Claude email (an API key, Bedrock or Vertex login), `--as X` becomes the person key, and joins a Claude account only if X is that account's Claude email. Logs that appear under two Claude accounts (a `~/.claude` folder shared or copied between people, or `--as` on an export too old to carry the account email) are flagged and counted under each, so you can see it |
| Short CLI history | Flagged per person; see [Short history](export.md#short-history-cli-transcripts-are-deleted-after-30-days) |
| A file that isn't an export, a broken zip, or a file re-saved in Excel | Listed and skipped whole; the rest still merge. See [What gets skipped](#what-gets-skipped) |
| Two zips with the same file name in different folders | Two exports (a folder per machine is fine); the report shows each one's path. The same goes for two folders inside one zip |
| Text that would run as a formula in Excel | Quoted with a leading `'` in every merged file; see [the export notes](export.md#what-is-in-the-export-file) |

**Exports from before machine ids** (schema 5 and older) can't say which
machine they came from. One that shares sessions with a newer export joins that
export's machine. Otherwise all of that person's exports are de-duplicated
against each other, as they were before machine ids — so if they use two
machines, only one machine's summary row per project and week survives and the
summary and breakdown undercount them. The merge report names each such
person; a fresh export from each of their machines fixes it.

**Seat facts across machines.** Seat tier, usage tier, extra usage and this
week's allowance status are the account's, so they come from the person's
newest export. Activity combines each machine's newest export: last active is
the latest on any machine, idle only if every machine is idle, and short CLI
history is flagged if any machine has it. A seat with no usage at all still
exports a row naming the person, marked idle. Active days (and so seat use)
combine each machine's exports: windows that don't overlap add up (an export to
the 16th, then one from the 17th), and a re-export inside a longer window
doesn't replace it. Where two of a machine's windows partly overlap, the longer
is used and the days only the other covered are left out of both active days
and workdays. Across machines, the busiest machine's figure is used: exports
carry a count of days, not the days themselves, so a day worked on both
machines can't be told from two days. That makes seat use a floor for someone
who splits their week across machines, never a double count.
These are the whole-export-window figures that `team-people.csv` and
`merge-report.txt` carry. On the dashboard, seat use, last active and idle are
worked out for the selected weeks instead (see **People** under People & data, below),
with the same rule across machines: the larger figure, never the sum.
The allowance estimate is per machine too — each one only sees its own usage —
so for a two-machine person it understates how much of the week is used.

The `machine_id` is random, made once per machine and kept in the tool's
`.cache` folder. It isn't derived from the hostname or hardware and identifies
nothing beyond itself. Re-cloning the tool makes a new one; shared sessions
keep that from double-counting.

## Known limits

With the same session id in two project folders, deleting one copy between exports can make the team sessions sheet list that session twice (the second copy is exported as `<id>#2`, and the first export's `<id>` row stays); the summary stays correct.

The merged sessions sheet can list both an original session and its later continuation (a resume or fork, which holds the original's work under another id); the summary stays correct.

The dropped-work warning is a floor. A session that started before the dropped export's window (`window_from`) isn't in its sessions CSV, so when every session it does list is accounted for, the weekly cost gap is ignored and that session's work can go unreported. A session that started before the chosen exports' windows is judged by the weekly cost gap and by the chosen exports' `sessions_active` count, not by id: if the chosen export has its own pre-window session in the same project that week (or a week split between exports makes `sessions_active` count a session twice), that session can make room for the missing one and the loss goes unreported. A loss under half a cent is still caught by the count.

Work near a week boundary can still be counted in two weeks when one machine is re-exported from another time zone and an export's window starts or ends mid-week (a rolling window: an older export since Monday the 26th, a newer one since Monday the 2nd). Each export holds the boundary work in its own week, the older export's week also holds days the newer lacks, so neither can be dropped, and the summary can exceed the true total by the work within a few hours either side of that midnight. Exports with matching windows, or whose windows both cover the boundary week whole, are not affected. Neither are exports that name the same `bucket_zone` (schema 8): they cut weeks at the same midnight.

## What gets skipped

Every file is checked as it's read. One that fails is named in the notices and
in `merge-report.txt` with the reason, and its **whole export** is skipped: a
zip if it came in a zip, otherwise every loose CSV of that export. Half an
export would show the person with $0, or with sessions but no cost.

| Skipped | Why |
|---|---|
| Corrupt, truncated, encrypted or unsupported-compression zips | They can't be read in full |
| A zip with more than 100 entries, or more than 64 MB of export CSVs | Far bigger than any real export; skipped before anything is decompressed |
| A CSV with more than 200 columns or 200,000 rows, or a row with more fields than its header | Real files have at most 92 columns and a few thousand rows. Files are read a line at a time and checked as they go, so one is stopped at the limit rather than after it has filled memory |
| A line, or a quoted cell spanning lines, longer than 1 MiB (1,048,576 characters) | A real row is a few hundred characters. The CSV reader parses a whole record before the column check could run, so one line of millions of commas would take hundreds of MB; a record is cut off at this length first, without reading past it |
| Zips and CSVs once the folder's total passes 256 MiB of export CSVs (uncompressed) | A cap on the memory one run can use. Files are taken in name order; the ones that would pass the total are skipped and named, the rest still merge |
| A zip member that inflates past the size it declares | A zip bomb. Members are inflated 64 kB at a time and stopped as soon as they pass their declared size, so one never fills memory |
| A CSV without the columns the merge keys on (e.g. `week_start`, `project`, `est_cost_usd` in a summary) | Not an export, or re-saved with `;` between columns (Excel in many European locales). Merged anyway, it would be a person with $0 |
| A date that isn't ISO (`2026-09-30`, or `2026-09-30T10:00:00Z` for times) in `week_start`, the window, `hit_at` and the other date columns, or a day that doesn't exist | Excel rewrites dates as `9/30/2026`. The overlap checks compare dates as text, so a rewritten one would double count, and some would crash the dashboard |
| A cost or count that isn't a finite number, or is over 10^15 | `nan`, `inf` and absurd values would break the sums and blank the dashboard |
| A cost or count below zero (`cache_saved_usd` is exempt: it is legitimately negative while a cache write hasn't been read back), or a `week_start` that isn't a Monday | The tool writes neither; a hand-edit or a bug would subtract from the team total or split a week |

Ignored rather than skipped: anything in a zip not named like an export CSV
(never decompressed), including the `__MACOSX/` folder and `._` files macOS
Finder adds when you **Compress** a folder. Columns a file has that the tool
doesn't know are kept, and their names are quoted in the merged files like any
other text that could run as a formula.

The fix for a skipped file is the same each time: ask for the zip the tool
made, unopened. Opening a CSV in Excel and saving it is what breaks most of
them.

## The merged files

`collect.py --merge DIR --out OUT` (default `./team-merged`) and the dashboard's
**Merged CSVs (.zip)** button produce the same files, byte for byte. The button
always gives the whole merged folder: the page's people and week filters don't
apply, so slice the files yourself. The CSVs open in Excel with accents intact,
and text that could run as a formula is quoted.

| File | Contents |
|---|---|
| `team-summary.csv`, `team-sessions.csv`, `team-breakdown.csv`, `team-limits.csv` | Everyone's rows, de-duplicated. Two extra columns: `person` first (the identity to pivot on) and `merged_from` last (which export each row came from; a limit episode seen on two machines lists both, as does its `machine_id`, but only for a Claude-account identity: with no Claude account, episodes stay per machine) |
| `team-people.csv` | One row per person: seat and usage tier, seat use, last active, idle flag, cost, subagent share, PRs, API retries, limit hits, lock-outs, hours locked out, extra usage, this week's allowance status, history and export provenance, value per seat, and how many machines their exports came from |
| `team-prs.csv` | Every PR once, by URL, with the repo it went to |
| `team-tools.csv` | Tool calls and errors by person, week, project and tool, with the MCP integration each tool belongs to |
| `merge-report.txt` | Every export read, every note above, the extra-usage run-rate, and value per seat |

## The team dashboard

The page has six tabs, one per decision. The people and week filters apply to
every tab, and every usage figure follows the selected weeks; the tiles stay on
Overview. People are shown by name: the git name
when it contains a space, otherwise the email's local part, title-cased. Two
people who would share a name get their email's domain after it (or the whole
email when the domain is shared too). The email shows on hover and in the
People table.

**Overview**
- **Tiles**: all of them follow the selected weeks: People (active, with no
  activity in these weeks, and not covered by these weeks), median seat use,
  Usage, Limit hits, Locked out, Extra usage and value per seat (the team's
  figure, with the median seat's below it).
- **What stands out**: up to six findings, each linking to the tab with the
  detail (see below).
- **Team spend by week**: list-price cost by person, with a line for the people
  who used Claude Code that week.
- **Model use by week**: everyone selected, added together, stacked by the exact
  model each request ran on ("Opus 5.5", "Sonnet 5.5"; a dated snapshot or 1M-context
  variant counts as its model), as list-price cost or output tokens. The 8 biggest
  models over the selected weeks are named and the rest are Other.
- **How current the data is**: one row per person and machine (a second
  machine's old export isn't hidden by the first one's fresh one) with its
  export date, window and `bucket_zone`. A machine whose latest export is more
  than 7 days old, counted in whole days from today's UTC midnight, is flagged.

**Spend**
- **Spend by repository**: the top 8 repositories by cost, then "Other (N repos)"
  for the rest, with each one's share of the total, its cost, and who worked in
  it (names and their cost there; someone under $0.50 in a repo isn't named). A
  session counts to the repo of its folder's git `origin`, else to the folder;
  spend tied to no folder is "(no project)".
- **Repository spend by week**: the same, stacked by week.
- **Cost by model, by person**: cost by model, main vs subagent, or output tokens.
- **Spend per active day**: cost in the selected weeks divided by active days,
  against Anthropic's reported $13 average and $30 for 90% of users. Active
  days per week are the exact `week_active_days` (schema 11) where the export
  has it. For an older export they follow the same estimate as the People table
  (the export's own total and the per-week bounds, for one machine) or, failing
  that, the projects' days added up, capped at the days the week has in the
  export window, because the count sums across projects: an upper bound on days,
  so spend per day is a lower bound. List price.

**Seats & limits**
- **Seat use by week**: a person × week heatmap of cost, requests or active
  days. Part-weeks at the edge of an export are outlined; weeks whose CLI history
  was already deleted are hatched, so a pale cell isn't mistaken for a quiet one.
  Active days follow the same per-week rule as spend per active day: the exact
  `week_active_days` where present; else, for one machine, the estimate the
  export's own total and the week's bounds give (a week that isn't pinned shows
  "≈" in its tooltip); else the projects' days capped at the
  week's days in the widest export window any of the person's machines had
  (never above 7). Across machines the larger figure is used.
- **Usage limits by person**: limit hits, lock-outs, hours locked out, extra
  usage, and where each person's current week stands against their estimated
  allowance. The hit, lock-out, hours and extra-usage columns count the episodes
  that started in the selected weeks, placed in weeks by the UTC date of the
  hit (a team exporting with `--tz` other than UTC can therefore see a hit near
  Sunday/Monday midnight in the neighbouring week), and read "—" for a person
  whose exports don't cover the selected weeks, who are listed last. The allowance columns (this week %, status,
  projected hit) are a snapshot taken at export time, so they are shown only when
  the selected weeks include the week of that person's export (the Monday of
  their `window_to`); otherwise they read "—", with a tooltip saying so. Tiers
  read as "Max 5×" or "Enterprise tier 1"; an id the page doesn't know is shown
  as it is.
- **Extra usage per week & run-rate**: weekly extra-usage spend with the
  average of the last four complete weeks, projected over 4 and 13 weeks. The
  page computes it for the selected people and weeks, so the bars and the note
  follow the filters. A week is complete when it lies inside the range and the
  selected people's exports cover it through its Sunday, and it is before today
  (UTC). Weeks after the exports end aren't counted as $0, and neither are weeks
  before the selected people's exports begin or in a gap between two exports no
  selected person covers: they are left out of the bars as unknown. With no complete week in the selected weeks the note says "No
  complete weeks in the selected weeks to compute a run-rate from." The note
  otherwise says how many weeks and episodes the run-rate rests on: the
  weekly-limit hits with extra usage that fall in those weeks. With one episode
  or fewer it reads "One episode so far: too little to project a run-rate for
  the selected people and weeks." (or "No extra usage so far") and gives no
  projection. `merge-report.txt` keeps the server's figure for the whole team and
  all its weeks. Extra usage is spiky — it only happens when
  someone runs out — so this is labelled a run-rate, not a forecast. Each
  person's extra usage also stops at their spend cap.
- **Value per seat**: see [below](#value-per-seat).

**Habits**
- **Habits by person**: from the [usage-health](usage-health.md) columns,
  one row per person who used Claude Code in the selected weeks, under a
  team-median row:
  - context tax: the share of main-conversation spend on requests over **500k**
    tokens. It is highlighted at Anthropic's 10% line. The 500k cutoff is ours:
    on 1M-context models, a 200k cutoff flagged everyone. The share over 200k
    shows beside it, in an "Over 200k" column, unhighlighted
  - cold-cache tax (share of spend re-caching after the cache expired)
  - share of edited sessions checked after their last code edit, with the
    session counts
  - interrupts per 100 prompts
  - share of sessions using plan mode, subagents, and a project CLAUDE.md

  Only the context tax and the cold-cache tax are highlighted, at Anthropic's
  10% line; the rest have no sourced line and are a profile. Hover a column
  header for what it means and when a high or low figure is fine. Someone whose
  export covers the weeks but who didn't use Claude Code in them isn't listed.
  Exports from before schema 7 have no habit data and aren't counted as zeros.
  Exports from before schema 10 don't carry the over-500k cost: the context tax
  reads "—" with a "re-export to measure" hint, and no context finding fires for
  them. A person with some weeks measured and some not is
  judged on the measured weeks only, in the table and in the finding alike.
- **Friction by integration**: tool error rates across the selected people,
  MCP tools grouped by server, for anything called at least 20 times, with a
  People column naming who hit the errors. A tool that fails for everyone is an
  integration or permission to fix, not anyone's habit. Claude Code's own tools
  (task, schedule, plan-mode and worktree tools and the like) sit in a collapsed
  table below, since their errors aren't an integration to fix. An MCP server
  whose name is a UUID shows as "MCP connector (id…)".

**Output**
- **Pull requests by repository**: grouped by the repo each PR went to, with the
  people who opened them (names, each with a PR count), how many were opened from
  another repo's folder, and the cost of the sessions that opened them (shared
  evenly across a session's PRs).
- **Output by person**: PRs, cost per PR, lines added and removed, cost per 1k
  lines, and the share of spend that has line data. Cost per 1k lines divides
  only the spend of sessions that recorded lines, so it isn't inflated by
  sessions that didn't. Blank where nothing was recorded, never 0.

**People & data**
- **People**: one row per person, with display names and readable tiers. The
  usage figures follow the selected weeks: sessions, cost, subagent share, PRs,
  API retries, seat use, seat cost, value × and last active (the latest selected week with usage). Seat use is active days
  in the selected weeks ÷ workdays (Monday to Friday) in the selected weeks that
  fall inside the person's exports' windows, capped at 100%. Active days per week
  are the exact `week_active_days` (schema 11) where the export has it. An older
  export has no exact days per week, but for a person with one machine it still
  carries their total active days over its window, and each week's projects give
  two bounds (the busiest project's days, and the projects' days added up and
  capped at the week's days in the window). The export's own total and those
  bounds decide the selected weeks: when they pin the figure it is exact and has
  no "≈" (all weeks reproduce the export's own total), otherwise the figure is
  an estimate shown with "≈" and a tooltip giving the range ("between 3 and 5
  active days"). With two machines, or a total that doesn't cover the same
  workdays as the export windows, the fallback is the projects' days capped at
  that week's days in the window, shown with "≈" and a hint to re-export. Across
  a person's machines the larger figure is used. Seat cost is the seat price per
  day times the calendar days of the selected weeks inside the exports' windows.
  Coverage is each export's own window (overlapping or touching windows merge),
  not the span from the first export's start to the last one's end: days between
  two exports are not covered and are neither counted as workdays nor paid for.
  Account facts do not change with the range: seat and usage tier, extra usage
  on, CLI history, the export window, exports and schema. A person whose
  exports' windows don't overlap the selected weeks (including weeks that fall
  between two exports) shows "—" in every usage column and is counted as "not
  covered by these weeks", never as idle.
- **Exports merged**: each file read.

### What stands out

These are fixed rules, computed in the browser over the selected people and
weeks. Each shows one sentence with the figure. They appear in this order, at
most six, then an expandable "+N more". With none firing the page says "Nothing stands out in
these weeks."

| Rule | Fires when | Detail on |
|---|---|---|
| Lock-out | Any locked-out episode in range: one finding listing everyone locked out with their hours, worst first ("Ann (59.0 h), Bo (3.0 h) were locked out until a limit reset."; more than three, then "and N more"), so a bad week can't fill the list | Seats & limits |
| On pace to hit | A person's allowance status is on pace to hit, or over, and the selected weeks include the week of their export | Seats & limits |
| Spend concentration | One repository is 40% or more of spend, with at least two repositories. "(no project)" spend is never the repository named | Spend |
| Failing integration | An MCP server with at least 20 calls and an error rate of 10% or more. Built-in tools (Bash test failures, a glob with no match) never fire it: they aren't an integration a manager fixes, and stay visible in the Friction table | Habits |
| High spend per active day | A person above $30 per active day (Anthropic's 90th percentile), at list price | Spend |
| Context tax | A person with 10% or more of main spend on requests over 500k tokens | Habits |
| Idle seat | No Claude Code activity in the selected weeks. Only people whose export window overlaps those weeks are counted: someone it doesn't cover is not idle | Seats & limits |
| Stale export | A person with any machine whose latest export's `exported_at` is more than 7 days before today (from UTC midnight; the freshness badge says "more than N days old" from the same age); named once | Overview |

Filters: people, and a week range. The week range applies to every usage figure,
from the first selected Monday to the last selected Sunday. The exceptions
describe the export, not usage: data freshness, exports merged, notices and the
Merged CSVs download. The allowance snapshot (this week %, status, projected hit
and the "on pace to hit" finding) is only meaningful for the week of a person's
export, so it is shown only when the selected weeks include that week.

**What the habit figures are for.** They show where Claude Code could help
someone more, and where training, a shared CLAUDE.md or a broken integration
would help the whole team. They are coaching signals, not a score: a long
session can be the right call for big work, experienced users interrupt more,
not less, and not every edit needs a test. Use them to start a conversation,
not to judge anyone's performance. **Tell your team you look at these, per
person, before asking for exports.** Each person sees the same figures for
themselves on their own Habits tab. Activity measures get gamed once they become targets (DORA); SPACE warns
against using them alone to reward or penalise; DX recommends saying so up
front. Tell people that before asking for exports. Monitoring guidance such as
the UK ICO's also expects that transparency, and a data-protection impact
assessment, before rollout.

## Value per seat

Is each seat worth what it costs? The tool compares what each person used, at
API list price, with what their seat cost over the same period plus any extra
usage they ran up:

    value multiple = list-price usage ÷ (seat cost + extra usage)

All three are for the selected weeks: usage and extra usage in those weeks, and
seat cost for the days of those weeks that fall inside the person's export
window (seat price per day × calendar days). A person whose export window
doesn't overlap the selected weeks has no value figure.

The **Value per seat** card lists each person's multiple. The Overview tile
headlines the team's: everyone's usage over everyone's seat cost plus extra
usage, summed before dividing. Its sub-line gives the median seat's multiple,
the typical seat. The two differ when a few people do most of the usage: when
one person does most of a team's usage, the team's multiple can be several
times the median seat's. Only people with a seat cost in
the selected weeks count toward the team's figure, on both sides of the
division. That leaves out a person whose tier has no price, and one whose
export doesn't cover those weeks.

Seat cost covers only the days the exports cover, so it is not the monthly
bill. Four weeks is 28 days, not a month. An export that ends mid-week leaves
the days after it uncounted.

Above **1×**, the seat delivered more API-equivalent usage than your organisation paid
for it. It's a measure of how much a seat is used, not of the value of the work —
pair it with the PRs view and the people who did the work.

Seat prices are your organisation's contract terms, so the tool never guesses them. Put a
`seat-prices.json` next to the exports — in a folder you pass, or beside a zip
or CSV you pass directly — or pass `--seat-prices FILE` to either command:

```json
{
  "currency": "USD",
  "per": "month",
  "prices": {
    "default_claude_max_20x": 0,
    "enterprise_tier_1": 0,
    "default": 0
  }
}
```

Replace the zeros with your real prices — a file still holding a zero is rejected,
so the unedited template can never produce a value figure. Each person is priced by their **usage
tier** (`rate_limit_tier`, e.g. `default_claude_max_5x`) if it's listed, then
their **seat tier** (`seat_tier`), then `default`. `per` is `month` or `year`.
Without the file, the value columns are blank and the dashboard says so. A file
that can't be read, or a tier with no price and no `default`, is reported
rather than guessed around.

Two caveats. Seat cost counts every day of the selected weeks inside the
person's export window, so someone whose CLI history was deleted shows less
usage against a full span of seat cost — their multiple is understated, and the
People table flags their history. And list
price is not your organisation's rate; the multiple compares like with like only in the
sense that both sides are what the usage *would* have cost.

## Why exports, not central collection

Claude Code can stream usage to a central OpenTelemetry collector, but only
once telemetry settings are pushed to every machine as managed policy. Where machines
aren't centrally managed that way, that route isn't available —
which is why this tool exists. It works in exactly that setting: each person
runs it on their own machine with no configuration, exports a zip, and a
manager merges the zips. Nothing needs installing centrally and no policy needs
pushing.

The cost of that design is that it depends on people exporting, and on each
machine still holding its logs. The tool makes both visible rather than hiding
them: every file carries its window and export date, the merge reports gaps and
overlaps, and short CLI history is flagged per person — with a fix each person
can apply themselves (see [Short history](export.md#short-history-cli-transcripts-are-deleted-after-30-days)).

---

[← Back to the README](../README.md)
