# Interface

The dashboard follows the Modaxo brand: brand purple/teal/orange chrome and
Trebuchet MS, with light and dark themes that follow the OS setting. Categorical
chart series deliberately keep a separate purpose-built palette — the brand
offers only about five usable hues, and the brand guide itself notes that orange
and amber fail contrast in the ways chart marks need. Brand colour does appear in
the charts, but only where a chart is single-series.

The charts whose categories can run long fold anything past the top few into a
grey "Other": *By project* and *Cost/Tokens by skill per …* (top 7), the skill
cost and invocation bars and *Top branches by cost* (top 8), *CLI versions over
time* (top 7 of the versions actually plotted, each session under the one it ran
most), and the *Skill × project* grid (top 7 projects, then an "Other" column).
*Calls by tool* shows only the 15 most-called tools, with no "Other" bar, so its
bars need not add up to the total; *Errors by tool* is ranked by error count, shows
the top 15 of the tools with at least 10 calls, and its note says how many more
tools (and errors) are not shown and how many smaller tools are not ranked. It is
a count of errors coloured by error rate, not a list of rates. The *MCP server
rollup* covers MCP servers only: Claude Code's own tools are left out, with their
total in the note, so they don't swamp the integrations. Every other chart --
models, token types, subagent types and depth, entry points, permission modes --
shows every category. The Overview's data table, the Skills, Tools, Agents and
Sessions tables and the pull-request table are the table views; the Habits tab and
the Output & env charts have none. The Subagents table lists the newest 500 and says
how many there are in all.

Quick ranges count back from the last day that has data, not from today, and the
buttons say so. A date chip appears only for a range that is neither the default
(the last 5 days of data) nor *All*. The filters (dates, bucket size, project, model)
are also kept in the address, so a view can be bookmarked or shared and survives a
reload. The charts re-colour when the OS switches between light and dark.

The Overview tab's **Usage limits & friction** card shows where this limit week
stands against the estimated weekly allowance (with a projected hit time when on
pace to reach it), but only when the selected dates overlap that limit week;
otherwise a note names the week and says it is outside them. Below it are limit hits, hours locked out, estimated extra usage, API
retries, context compactions and turn time for the filtered range, and a table of
every limit episode. See [The limits file](export.md#the-limits-file-who-ran-out-of-allowance-and-what-it-cost-them)
for how each figure is derived.

## Time granularity

Charts default to one bar per **8 hours** over the **last 5 days**, and the
*Bucket size* control offers 5m / 10m / 15m / 30m / 1h / 4h / 8h / Day / Week /
Month. The payload carries 5-minute buckets (`base_bucket_minutes`) and the
browser rolls them up, so the finest view is a 5-minute bar. Because requests
cluster inside a session, that costs little: on a ~500-file log set, 5-minute
buckets produce ~3.9k usage rows against ~330 for daily.

The grain applies to the four charts where a time bucket carries meaning --
*Usage over time*, *tokens/cost by skill*, *errors over time*, and *main vs
subagent*. It deliberately does **not** apply to *CLI versions over time*, which
counts sessions: a session spans hours or days rather than being a point in
time, so bucketing it at 5 minutes would be misleading.

The Habits tab's *Main-conversation spend by context size* follows the grain
too, but never goes finer than an hour: habit rows are bucketed by the hour, so
at 5–30 minutes every hour's spend would land in its first bar. Below an hour
the chart stays at one bar per hour, and its note says so.

Two behaviours worth knowing:

- **The axis is continuous**, including empty buckets. Plotting only buckets
  that hold data would close up idle periods, making a quiet weekend look like
  it never happened and distorting the shape of the series.
- **Grains that would exceed ~500 bars for the selected range are disabled**,
  and if you widen the range until the active grain no longer fits, it steps to
  the next coarser one and says so. 5 minutes across 90 days is ~26,000 bars,
  which is neither readable nor fast.

Filtering is a searchable multi-select (`static/filters.js`) rather than a native
`<select multiple>`, which needed ctrl+click, had no search, and gave no readable
indication of what was selected. It supports type-to-filter, full keyboard
operation (arrows, Enter, Escape, Home/End), select-all scoped to the current
search, and removable chips summarising everything currently applied. Quick-range
buttons that reach further back than the data disable themselves, typed dates are
clamped to the available range, and any chart or table whose filters match nothing
says so instead of rendering an empty grid.

---

[← Back to the README](../README.md)
