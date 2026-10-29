# Usage Lens

A local, private dashboard over your own Claude Code logs — tokens, cost, skills,
tools, subagents, sessions, and more. Everything runs on your machine against
`~/.claude/projects/`; nothing is uploaded anywhere.

> Usage Lens is an independent project by Modaxo. It is not affiliated with or
> endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Getting started

```bash
git clone https://github.com/Modaxo-AI/usage-lens.git
cd usage-lens
python3 serve.py
```

That is the whole setup. **Python 3.9 or newer, and nothing to install** — the
tool uses only the standard library, so there is no `pip install`, no virtualenv
and no lockfile to keep current. It reads the logs Claude Code already writes to
`~/.claude/projects/`, so there is nothing to configure and nothing to switch on
beforehand; whatever you have done so far is already there.

By platform:

- **macOS**: if `python3` asks to install the command line developer tools,
  accept. That installs Python, and is the only setup needed.
- **Windows**: `python3` is often not a command. Use `py serve.py` (or
  `python serve.py`) in place of `python3 serve.py` throughout. Time zone names
  in Claude Code's limit notices fall back to your local time, since Windows
  Python ships without a zone database; that is nearly always the same answer,
  so there is no need to install `tzdata`.
- **Linux**: any Python 3.9+ from your distribution works.

Git is optional. Without it, projects are named by folder rather than by
repository, and an export names you by your computer login rather than your
git email (the team view still matches people by Claude account). The chart
library ships in `static/`, so the only network request is a once-a-day pricing
check, which `--offline` turns off.

`serve.py` opens `http://127.0.0.1:8765` in your browser and re-scans when you
click **Refresh**. The first run takes a few seconds to read your logs (about
500 files on a heavy user); after that a cache keeps it under a second.

```bash
python3 serve.py --port 8765   # change the port
python3 serve.py --no-open     # don't auto-launch a browser
python3 collect.py --static    # a single standalone dashboard.html, no server
python3 collect.py --export    # the business-review CSVs (days and weeks in UTC)
python3 collect.py --export --tz Europe/London   # ...or in the team's zone (or set CLAUDE_USAGE_EXPORT_TZ)

# managers: everyone's export zips in one folder
python3 serve.py --team ./exports      # team dashboard
python3 collect.py --merge ./exports   # combined, de-duplicated CSVs
```

Full flag reference: [Command line](docs/cli.md).

## What you get

### Your own dashboard (`python3 serve.py`)

Seven tabs over a shared date-range / project / model filter bar, with stat tiles
pinned above them. Every figure follows the selected dates:

| Tab | What's in it |
|---|---|
| **Overview** | A habits strip (the selected dates against an equal run of days before them), then cost, tokens, sessions, cache savings, cost concentration, median session, usage over time, by project, by model, estimate-vs-CLI reconciliation. |
| **Habits** | Whether you're using Claude Code well: spend by context size, cache misses by reason, checks run after edits, refused tool calls, how you prompt, and your recent sessions tagged with the habits that cost them. See [Usage health](docs/usage-health.md). |
| **Skills** | Cost and invocations per skill, and how much of your work runs inside a skill at all. |
| **Tools & errors** | Calls and error rate per tool, MCP-server rollup, errors over time. |
| **Agents** | Subagent cost and tokens by type, spawn depth, main-vs-subagent over time. |
| **Sessions** | Sortable table with estimated vs. CLI-reported cost, lines changed and PR links, plus an active-time-vs-cost scatter. |
| **Output & env** | Lines changed, PRs, desktop vs. CLI, CLI versions, branches, permission modes. |

### The team dashboard, for managers (`python3 serve.py --team ./exports`)

Each person clicks **Export (.zip)** on their own dashboard and sends the zip; a
manager drops the zips in one folder. The team dashboard merges them (no double
counting of re-exports or second machines) into six tabs over a people and
week-range filter. Every usage figure follows the selected weeks:

| Tab | What's in it |
|---|---|
| **Overview** | What stands out (fixed rules: lock-outs, people on pace to hit their weekly limit, one repo holding most of the spend, a failing MCP integration, high spend per active day, context tax, idle seats, stale exports), tiles, team spend by week, model use by week, and how current each export is. |
| **Spend** | Spend by repository and by week, cost by model per person, spend per active day against Anthropic's benchmark. |
| **Seats & limits** | Seat use by week (heatmap), usage limits and lock-outs per person, the extra-usage run-rate, and value per seat (with a `seat-prices.json`). |
| **Habits** | Each person's habits next to the team median, as coaching signals, and tool friction by integration. |
| **Output** | Pull requests by repository with who opened them, and PRs, cost per PR and lines changed per person. |
| **People & data** | One row per person (seat use, cost, PRs, seat cost and value for the selected weeks, plus tiers and export details), and every export merged. |

**Merged CSVs (.zip)** downloads the whole merged, de-duplicated data set for
Excel or Power BI, with a merge report (the same files `collect.py --merge`
writes). See [Team view](docs/team.md).

## Documentation

| | |
|---|---|
| [Export for business review](docs/export.md) | The CSVs, what every column means, **what the file discloses**, and the coverage caveats to read before computing any ratio. |
| [Team view](docs/team.md) | Combining everyone's exports: the team dashboard tab by tab, the headline rules, value per seat, the merged CSVs, what the merge resolves for you, and why exports rather than central collection. |
| [Command line](docs/cli.md) | `serve.py` and `collect.py`, every flag, and the standalone offline build. |
| [How it works](docs/how-it-works.md) | The pipeline, the three correctness problems it solves that a naive log read doesn't, and the project layout. |
| [Cost estimates](docs/cost.md) | Where the rates come from, how a price change is recorded without repricing history, fast-mode and batch pricing, and reconciling against the CLI's own figures. |
| [Privacy](docs/privacy.md) | What is read, what is never read, and what leaves your machine. |
| [Interface](docs/interface.md) | The brand, the filter panel, and chart time granularity. |
| [Known limitations](docs/limitations.md) | What the numbers can and cannot tell you. |
| [Usage health](docs/usage-health.md) | The measures behind the habits strip and the Habits tab: the research behind each one, what the logs can measure, the privacy decisions, and how each measure is defined. |

## Privacy in one paragraph

The parser reads usage numbers, model names, timestamps, tool and skill *names*,
session titles, PR links and environment metadata. It never reads your prompts,
Claude's replies, tool outputs or any code — enforced by a test that plants a
sentinel string through a fixture transcript and asserts it reaches neither the
dashboard nor any CSV. A few narrow exceptions are decided in memory, keeping
only the result: the limit type and reset time from Claude Code's own
usage-limit notice; whether a message starts with its interrupt marker or its
markup for a slash command; the category of a shell command Claude ran (test,
lint, build, commit, PR); and whether an edited file is a note or doc rather
than code. See [Privacy](docs/privacy.md). The server binds
to `127.0.0.1` only. The one network request the tool makes is a daily check of
Anthropic's public pricing page, which sends nothing about you (`--offline`
turns it off).

The one thing designed to leave your machine is a CSV you export deliberately;
read [What is in the export file](docs/export.md#what-is-in-the-export-file)
before sending one.

## Running the tests

```bash
python3 -m unittest discover -s tests -t .
```

## Contributing and security

Issues and pull requests are welcome; see [Contributing](CONTRIBUTING.md).
To report a security problem privately, see [Security](SECURITY.md).

## License

[Apache License 2.0](LICENSE). The bundled Chart.js (`static/chart.umd.min.js`)
is MIT-licensed; see [NOTICE](NOTICE).
