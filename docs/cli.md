# Command line

The dashboard has two entry points: `serve.py` for the live server and
`collect.py` for everything that writes a file.

## Where the tool keeps its files

Everything the tool makes for itself goes in one data folder, `~/.usage-lens`,
not beside the code:

| File | What it is |
|---|---|
| `cache/parsed.json` | The parse cache, so a scan only re-reads changed logs |
| `cache/price-check.json` | Today's price check, so it runs at most once a day |
| `machine-id` | This machine's random id, for the [team merge](team.md#what-combining-handles-for-you) |
| `data.json`, `dashboard.html` | What `collect.py` and `collect.py --static` write |

Because the folder is outside the code, updating the tool, re-cloning it or
installing it as a plugin keeps the same machine id and a warm cache. A copy
older than the data folder kept its files in `.cache/` beside the code; the
first run carries its machine id over, and the parse cache rebuilds itself in a
few seconds. The old `.cache/` folder can then be deleted.

To keep the folder somewhere else, set `CLAUDE_USAGE_DATA_DIR`, or pass
`--data-dir DIR` to either command. Exports (`--export DIR`) and merges
(`--out DIR`) still go where you point them.

## serve.py

```bash
python3 serve.py                 # opens http://127.0.0.1:8765
python3 serve.py --port 8765     # change the port
python3 serve.py --no-open       # don't auto-launch a browser
python3 serve.py --offline       # no network request at all (skips the daily price check)
python3 serve.py --team DIR      # team dashboard over people's export zips
python3 serve.py --team DIR --seat-prices prices.json   # ...with value per seat
python3 serve.py --data-dir DIR  # keep the cache and machine id in DIR
```

`--team` takes one or more files or folders of export zips/CSVs and serves the
[team dashboard](team.md) instead of your own; your own logs and the personal
export routes are switched off while it runs.

It scans your logs on each `/api/data` request and re-scans when you click
**Refresh**, so a session you are actively working in shows up once you reload.
The scan is cheap because unchanged files come from the parse cache; Refresh
uses the cache too. To re-read every file from scratch, run
`python3 collect.py --force`, or load `/api/data?full=1` while the server is
running.

A log file the tool can't open (a permissions problem, a lock) is left out of
the results only if it has never been read: one already in the parse cache is
still served from it while its size and modified time are unchanged. A file left
out is not remembered as empty, so once it is readable again the next scan picks
it up with no `--force`.

The server only answers on `127.0.0.1`, and it hardens itself against the
websites you have open in other tabs:

- **Host check.** The `Host` header must be exactly `127.0.0.1`, `localhost`, or
  either with the server's port (`127.0.0.1:8765`). Anything else, including a
  list such as `127.0.0.1:8765, evil.com` or a second `Host` header, gets a 403.
  This blocks DNS rebinding.
- **`/api/*` needs proof it came from the dashboard.** A request is answered only
  if the browser sends `Sec-Fetch-Site: same-origin` (the dashboard's own page) or
  `none` (you typed the URL), or an `Origin` of this server, or the dashboard's
  own `X-Claude-Usage: 1` header (its fetches add it, for browsers too old to send
  the others), or a `Referer` from this server's own page (the Export buttons are
  plain links). A cross-site `Sec-Fetch-Site` or a foreign `Origin` gets a 403
  whatever else the request carries, and so does a request with none of these: an
  old browser's `<img>` pointed at the server looks exactly like that, and it must
  not be able to trigger a rescan (`?full=1`) or an export download. Another site's
  page can't add the custom header without a CORS preflight, and the server never
  grants one (no `Access-Control-*` header is ever sent). The page itself (`/`) and
  `static/` are not checked: they carry no data.
- **No framing, no sniffing.** Every response carries `X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'` and
  `X-Content-Type-Options: nosniff`, so no other site can embed the dashboard in
  a frame.

Scripts and `curl` aren't browsers, so they have to say so. Add one header
(`-H 'Sec-Fetch-Site: none'` works as well):

```bash
curl -H 'X-Claude-Usage: 1' http://127.0.0.1:8765/api/export/summary.csv
```

The export routes bucket days and weeks in the [export zone](export.md#the-export-zone):
the `CLAUDE_USAGE_EXPORT_TZ` variable of the server's environment, else UTC. An
invalid name stops `serve.py` at startup (exit status 2, with the reason); the
page names the zone next to the Export button, as `export_zone` in `/api/data`,
which otherwise stays in this machine's zone.

## collect.py

```bash
python3 collect.py               # writes data.json (in the data folder) and nothing else
python3 collect.py --static      # also builds a standalone dashboard.html there
python3 collect.py --export DIR  # writes the business-review CSVs
```

| Flag | What it does |
|---|---|
| `--static` | Build `dashboard.html` (see below) |
| `--redact` | Strip identifying detail from the `--static` build |
| `--force` | Ignore the parse cache and re-read every log file |
| `--export [DIR]` | Write the six CSVs described in [Export for business review](export.md) |
| `--tz ZONE` | With `--export`: the zone the export's days and weeks are in, as an IANA name (default: the `CLAUDE_USAGE_EXPORT_TZ` variable, else `UTC`). The dashboard, `data.json` and `--static` always use this machine's zone. See [the export zone](export.md#the-export-zone) |
| `--as EMAIL` | Identity to stamp on exported rows (default: `git config user.email`) |
| `--since` / `--until` | Limit the export to a date range (`YYYY-MM-DD`, days in the export zone; must be real dates, `--since` no later than `--until`) |
| `--no-titles` | Leave session titles out of the export |
| `--check-prices` | Check the rate table against Anthropic's published prices now (ignoring today's cached check); exit 1 if anything differs, the check couldn't run, the page was only partly understood, or a model in your logs isn't listed on the page (`unverified`). See [Cost estimates](cost.md#the-daily-price-check) |
| `--offline` | Make no network request at all (skips the daily price check) |
| `--data-dir DIR` | Where the tool keeps its files (default `~/.usage-lens`, or `CLAUDE_USAGE_DATA_DIR`). See [Where the tool keeps its files](#where-the-tool-keeps-its-files) |
| `--merge PATH...` | Team mode: combine people's export zips/CSVs (files or folders) into de-duplicated team CSVs and a report, instead of reading your own logs. See [Team view](team.md) |
| `--out DIR` | Where `--merge` writes (default `./team-merged`) |
| `--seat-prices FILE` | Seat price per tier for value per seat (default: a `seat-prices.json` in the `--merge` folder). See [Value per seat](team.md#value-per-seat) |

## Standalone offline file

```bash
python3 collect.py --static
```

Writes a single self-contained `dashboard.html` to the data folder (the path
is printed) — the data, the stylesheet and
the charting library are all inlined, so it opens over `file://` with no server
and no sibling files. If the build fails, `collect.py` exits with status 1.
Add `--redact` before sharing it with anyone (see
[Privacy](privacy.md)), and `--force` to ignore the parse cache and re-read every
log file from scratch.

Plain `python3 collect.py` (no flags) just writes `data.json` without touching
the HTML — useful for inspecting the aggregated data directly.

---

[← Back to the README](../README.md)
