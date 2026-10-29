#!/usr/bin/env python3
"""Local-only live server for the usage dashboard.

Binds 127.0.0.1 only, rejects any request whose Host header isn't exactly
localhost or 127.0.0.1 (blocks DNS-rebinding), refuses /api/* requests that
don't prove they come from the dashboard's own page (so another site can't
trigger a scan or an export download), marks every response un-frameable,
serves static/ from a fixed allow-list (no path traversal), and re-scans
~/.claude/projects on every /api/data request -- cheaply, since FileCache
skips files that haven't changed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import re
import threading
import traceback
import webbrowser
from datetime import date
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_usage import context as context_mod
from claude_usage import export as export_mod
from claude_usage import paths, price_check
from claude_usage.sanitize import finite_json
from claude_usage.scan import DEFAULT_ROOT, default_cache, scan

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
ALLOWED_STATIC_FILES = {"index.html", "app.css", "app.js", "filters.js", "habits.js", "chart.umd.min.js",
                        "team.html", "team.js", "team-findings.js"}

# Set explicitly: mimetypes reads the OS table (the registry on Windows), where
# .js can map to text/plain, and with X-Content-Type-Options: nosniff a browser
# then refuses to run the script. The dashboard must not depend on that table.
STATIC_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def _static_content_type(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return (STATIC_CONTENT_TYPES.get(ext) or mimetypes.guess_type(name)[0]
            or "application/octet-stream")


_cache_lock = threading.Lock()
# Same cache file collect.py uses, so the two entry points warm each other.
# Made in main(), once --data-dir has been read.
_cache = None

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_day(value: str) -> bool:
    # The shape alone lets "2026-09-31" through to fail deep inside the export.
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# The daily price check runs in the background: a page load reads only its
# cached result, so a slow or blocked network never delays the dashboard.
_price_lock = threading.Lock()
_price_thread = None


def _refresh_prices_in_background():
    global _price_thread
    with _price_lock:
        if _price_thread is not None and _price_thread.is_alive():
            return
        if not price_check.needs_refresh():
            return
        _price_thread = threading.Thread(target=price_check.check, daemon=True)
        _price_thread.start()


def _valid_host(header_value, port: int) -> bool:
    # Exact match on the whole value: a list ("127.0.0.1:P, evil.com") or any
    # other decoration is not a host this server answers to.
    return header_value in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost")


def _same_site_request(headers, port: int) -> bool:
    """False unless the request provably comes from this server's own page (or a
    URL typed into the address bar). The Host check alone doesn't stop another
    site's page: it can point an <img> or a link at http://127.0.0.1:<port>/api/...
    and the Host header is then correct.

    Browsers send Sec-Fetch-Site on every request ("none" when you typed the
    URL, "same-origin" from the dashboard itself). A browser too old to send it
    may send Origin, and the dashboard's own fetches add X-Claude-Usage: 1 (a
    cross-origin page can't set a custom header without a CORS preflight, which
    this server never answers). Its plain-link downloads can't carry a custom
    header, so a Referer from this server's own origin counts too: another site
    can't forge one, and a request that hides its Referer is refused.
    A request with none of these can't be told apart from a cross-site <img> in
    an old browser, so it is refused. curl and scripts send X-Claude-Usage: 1
    (docs/cli.md).

    A cross-site Sec-Fetch-Site or a foreign Origin refuses the request whatever
    else it carries.
    """
    # Only the first value of a repeated header would be checked, so a valid
    # first could vouch for a hostile second: refuse any repeat.
    for name in ("Sec-Fetch-Site", "Origin", "Referer", "X-Claude-Usage"):
        if len(headers.get_all(name) or []) > 1:
            return False
    fetch_site = headers.get("Sec-Fetch-Site")
    origin = headers.get("Origin")
    own = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    if port == 80:  # a browser leaves the default port out of the origin
        own += ["http://127.0.0.1", "http://localhost"]
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False
    if origin is not None and origin not in own:
        return False
    if fetch_site is not None or origin is not None:
        return True
    if headers.get("X-Claude-Usage") == "1":
        return True
    referer = headers.get("Referer")
    return referer is not None and any(referer == o or referer.startswith(o + "/") for o in own)


class Handler(BaseHTTPRequestHandler):
    server_version = "UsageDashboard/2.0"
    # Seconds a connection may sit without sending: a request that never
    # finishes can't hold a thread forever.
    timeout = 30
    port = 8765  # overridden by main() before the server starts
    # Set by --team: the folders/files holding people's exports. In team mode
    # the server reads those instead of this machine's own logs.
    team_paths = None
    seat_prices = None  # --seat-prices; else a seat-prices.json among team_paths

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; errors still raise

    def end_headers(self):
        # Every response -- pages, JSON, downloads, 403s, 404s -- ends its
        # headers here, so this is the one place the dashboard is made
        # un-frameable (a page on another site can't overlay it for clickjacking)
        # and the browser is told not to guess a content type.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send_json(self, obj: dict, status=200):
        try:
            body = json.dumps(obj, allow_nan=False).encode("utf-8")
        except ValueError:  # a NaN or infinity somewhere in the payload
            body = json.dumps(finite_json(obj), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        accept_enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body, filename: str,
                       content_type: str = "text/csv; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # Quoted filename: the identity in it can contain characters that would
        # otherwise terminate the header value early.
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: str, content_type: str):
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forbidden(self, msg="forbidden"):
        body = msg.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A second Host header is as suspect as a list in the first: only a
        # single, exact one is accepted.
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or not _valid_host(hosts[0], self.port):
            self._forbidden("bad host header")
            return

        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path.startswith("/api/") and not _same_site_request(self.headers, self.port):
            self._forbidden("cross-site request")
            return

        try:
            return self._route(path, query)
        except (BrokenPipeError, ConnectionResetError):
            return  # the browser went away; nothing to answer
        except Exception as exc:  # noqa: BLE001 -- answer instead of dropping the connection
            traceback.print_exc()
            # Only possible while nothing has been sent yet; every route builds
            # its whole body before writing the status line.
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _route(self, path, query):
        if self.team_paths is not None:
            team_response = self._team(path)
            if team_response is not False:
                return team_response

        if path == "/":
            return self._send_file(os.path.join(STATIC_DIR, "index.html"),
                                   _static_content_type("index.html"))

        if path == "/api/data":
            force = (parse_qs(query).get("full") or [None])[0] == "1"
            with _cache_lock:
                payload = scan(DEFAULT_ROOT, cache=_cache, force=force)
            payload["price_check"] = price_check.check(
                used_models=payload["dims"]["models"], allow_fetch=False)
            # The page shows this machine's days, but the Export button's files
            # are bucketed in the export zone: the page says which. None when
            # the setting is invalid (the export itself then answers with the reason).
            try:
                payload["export_zone"] = context_mod.export_zone()[1]
            except ValueError:
                payload["export_zone"] = None
            _refresh_prices_in_background()
            return self._send_json(payload)

        # /api/export/all.zip (what the dashboard button uses) and the
        # individual /api/export/summary.csv | sessions.csv, kept for scripting.
        if path.startswith("/api/export/"):
            name = path[len("/api/export/"):]
            params = parse_qs(query)
            since = (params.get("from") or [None])[0]
            until = (params.get("to") or [None])[0]
            for value in (since, until):
                if value is not None and not _valid_day(value):
                    return self._forbidden("bad date")
            if since and until and since > until:
                return self._forbidden("bad date range")
            include_titles = (params.get("titles") or ["1"])[0] != "0"

            kind = name[: -len(".csv")] if name.endswith(".csv") else None
            if name != "all.zip" and kind not in export_mod.KINDS:
                return self._not_found()

            # The export's days and weeks are in the export zone (CLAUDE_USAGE_EXPORT_TZ,
            # else UTC), not this machine's: /api/data above stays machine-local.
            try:
                zone = context_mod.export_zone()
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, status=500)
            with _cache_lock:
                payload = scan(DEFAULT_ROOT, cache=_cache, tz=zone[0])
            person = export_mod.resolve_identity()
            if name == "all.zip":
                return self._send_download(
                    export_mod.zip_bytes(payload, person, since, until, include_titles, zone=zone),
                    export_mod.zip_filename_for(person, zone=zone), "application/zip")
            body = export_mod.csv_text(payload, kind, person, since, until, include_titles, zone=zone)
            return self._send_download(body, export_mod.filename_for(kind, person, zone=zone))

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            # Allow-list only -- rejects traversal attempts like /static/../serve.py
            # even before touching the filesystem.
            if name not in ALLOWED_STATIC_FILES:
                return self._not_found()
            full_path = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(full_path):
                return self._not_found()
            return self._send_file(full_path, _static_content_type(name))

        return self._not_found()


    def _team(self, path):
        """Team-mode routes. Returns False for a path team mode doesn't own."""
        from claude_usage import team
        if path == "/":
            return self._send_file(os.path.join(STATIC_DIR, "team.html"),
                                   _static_content_type("team.html"))
        if path == "/api/team":
            # Re-read on every request: dropping another zip into the folder and
            # pressing Refresh is the whole workflow.
            return self._send_json(team.team_payload(self.team_paths, seat_prices=self.seat_prices))
        if path == "/api/team/export.zip":
            return self._send_download(team.zip_merged(self.team_paths, self.seat_prices),
                                       "claude-usage-team-merged.zip", "application/zip")
        if path == "/api/data" or path.startswith("/api/export/"):
            # This machine's own logs are not what a team view is about; serving
            # them here would mix one person's raw data into a manager's session.
            return self._not_found()
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"Usage Lens {context_mod.tool_version()}")
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default: 8765)")
    parser.add_argument("--no-open", action="store_true", help="don't open the dashboard in a browser")
    parser.add_argument("--offline", action="store_true",
                        help="make no network request at all (skips the daily price check)")
    parser.add_argument("--team", nargs="+", metavar="PATH",
                        help="team dashboard: read people's export zips/CSVs from these "
                             "files or folders instead of your own logs")
    parser.add_argument("--seat-prices", metavar="FILE",
                        help="with --team: seat price per tier, for value per seat "
                             "(default: a seat-prices.json in the team folder)")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="where the tool keeps its cache and machine id "
                             f"(default: {paths.DEFAULT_DATA_DIR}, or {paths.DATA_DIR_ENV})")
    args = parser.parse_args()

    global _cache
    if args.data_dir:
        paths.set_data_dir(args.data_dir)
    context_mod.adopt_legacy_machine_id()
    _cache = default_cache()

    if not args.team:
        # The export zone is read on every export click; a bad setting should
        # stop the server here, not 500 on the first click. (Team mode never exports.)
        try:
            context_mod.export_zone()
        except ValueError as exc:
            parser.error(str(exc))

    Handler.port = args.port
    if args.offline:
        os.environ[price_check.OFFLINE_ENV] = "1"
    elif not args.team:
        _refresh_prices_in_background()
    if args.team:
        missing = [p for p in args.team if not os.path.exists(p)]
        if missing:
            parser.error(f"not found: {', '.join(missing)}")
        Handler.team_paths = [os.path.abspath(p) for p in args.team]
        Handler.seat_prices = os.path.abspath(args.seat_prices) if args.seat_prices else None
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    if args.team:
        # flush: run in the background (as the plugin's skills do), stdout is
        # a pipe and would hold this line until the server stops.
        print(f"Serving TEAM dashboard for {', '.join(Handler.team_paths)} at {url}", flush=True)
    else:
        print(f"Serving usage dashboard at {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
