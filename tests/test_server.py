import contextlib
import gzip
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

import serve
from claude_usage.cache import FileCache

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_root = serve.DEFAULT_ROOT
        cls._orig_cache = serve._cache
        serve.DEFAULT_ROOT = FIXTURES_ROOT
        cls.tmpdir = tempfile.mkdtemp()
        serve._cache = FileCache(os.path.join(cls.tmpdir, "parsed.json"))

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.httpd.server_port
        serve.Handler.port = cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        serve.DEFAULT_ROOT = cls._orig_root
        serve._cache = cls._orig_cache

    def _get(self, path, host=None, headers=None):
        status, body, _ = self._get_full(path, host, headers)
        return status, body

    def _get_full(self, path, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        # What a browser sends from the dashboard's own page; pass a header as
        # None to leave it out, as a bare client (curl, an old <img>) would.
        headers = {"Host": host or f"127.0.0.1:{self.port}",
                   "Sec-Fetch-Site": "same-origin", **(headers or {})}
        headers = {k: v for k, v in headers.items() if v is not None}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        headers_out = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers_out

    def test_api_data_returns_200_json(self):
        status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("sessions", payload)
        self.assertIn("usage", payload)

    def test_api_data_carries_the_price_check_without_fetching(self):
        # The suite runs offline (tests/__init__.py); a page load only ever
        # reads the cached check, so this must answer at once.
        status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["price_check"]["status"], "offline")

    def test_every_script_the_page_loads_is_served(self):
        # A new static script has to be on serve.py's allow-list as well as in
        # the template; missing it 404s the file and the page never renders.
        import re
        with open(os.path.join(serve.STATIC_DIR, "index.html"), encoding="utf-8") as fh:
            scripts = re.findall(r'<script src="static/([^"]+)"></script>', fh.read())
        self.assertIn("habits.js", scripts)
        for name in scripts:
            status, _ = self._get("/static/" + name)
            self.assertEqual(status, 200, name)

    def test_api_data_carries_the_health_section(self):
        status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("health", payload)
        self.assertTrue(all("health" in s for s in payload["sessions"]))

    def test_index_returns_200_html(self):
        # static/index.html may not exist yet this early in implementation;
        # skip gracefully rather than fail the whole suite on ordering.
        if not os.path.isfile(os.path.join(serve.STATIC_DIR, "index.html")):
            self.skipTest("static/index.html not built yet")
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_bad_host_header_rejected(self):
        status, _ = self._get("/api/data", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_path_traversal_on_static_rejected(self):
        status, _ = self._get("/static/../serve.py")
        self.assertIn(status, (404, 403))

    def test_unlisted_static_file_rejected(self):
        status, _ = self._get("/static/collect.py")
        self.assertEqual(status, 404)

    def test_unknown_route_404(self):
        status, _ = self._get("/does-not-exist")
        self.assertEqual(status, 404)

    # A page on another site can point a request at 127.0.0.1 with a perfectly
    # valid Host header; the browser's Sec-Fetch-Site / Origin give it away.
    def test_cross_site_api_request_rejected(self):
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                status, _ = self._get("/api/data", headers={"Sec-Fetch-Site": site})
                self.assertEqual(status, 403)

    def test_foreign_origin_rejected(self):
        status, _ = self._get("/api/data", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_and_typed_url_allowed(self):
        for headers in ({"Sec-Fetch-Site": "same-origin",
                         "Origin": f"http://127.0.0.1:{self.port}"},
                        {"Sec-Fetch-Site": "none"},
                        # A browser too old to send Sec-Fetch-Site, from its own page.
                        {"Sec-Fetch-Site": None, "Origin": f"http://127.0.0.1:{self.port}"},
                        {"Sec-Fetch-Site": None, "Origin": f"http://localhost:{self.port}"}):
            with self.subTest(headers=headers):
                status, _ = self._get("/api/data", headers=headers)
                self.assertEqual(status, 200)

    def test_custom_header_alone_is_an_origin_signal(self):
        # A browser too old for Sec-Fetch-Site sends no Origin on a same-origin
        # GET either; the dashboard's own fetches add this header instead, which
        # another site's page can't do without a CORS preflight we never grant.
        for path in ("/api/data", "/api/data?full=1", "/api/export/summary.csv"):
            with self.subTest(path=path):
                status, _ = self._get(path, headers={"Sec-Fetch-Site": None,
                                                     "X-Claude-Usage": "1"})
                self.assertEqual(status, 200)

    def test_custom_header_does_not_override_a_cross_site_signal(self):
        for extra in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                      {"Sec-Fetch-Site": None, "Origin": "http://evil.com"},
                      {"Sec-Fetch-Site": None, "Origin": "null"},
                      {"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.com"}):
            with self.subTest(extra=extra):
                status, _ = self._get("/api/data", headers={"X-Claude-Usage": "1", **extra})
                self.assertEqual(status, 403)

    def test_custom_header_must_have_the_value_1(self):
        for value in ("0", "", "true"):
            with self.subTest(value=value):
                status, _ = self._get("/api/data", headers={"Sec-Fetch-Site": None,
                                                            "X-Claude-Usage": value})
                self.assertEqual(status, 403)

    def test_own_page_referer_is_an_origin_signal(self):
        # The export buttons are plain <a href> downloads, which can't carry a
        # custom header; the browser's Referer says which page clicked them.
        own = f"http://127.0.0.1:{self.port}/"
        for referer, want in ((own, 200), (own + "?x=1", 200),
                              (f"http://localhost:{self.port}/", 200),
                              ("http://evil.com/", 403),
                              (f"http://127.0.0.1:{self.port}.evil.com/", 403),
                              (f"http://127.0.0.1:{self.port}@evil.com/", 403),
                              (f"http://evil.com/?u=http://127.0.0.1:{self.port}/", 403)):
            with self.subTest(referer=referer):
                status, _ = self._get("/api/export/all.zip",
                                      headers={"Sec-Fetch-Site": None, "Referer": referer})
                self.assertEqual(status, want)

    def _raw_get(self, path, header_lines):
        import socket
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   + "".join(f"{line}\r\n" for line in header_lines) + "Connection: close\r\n\r\n")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(request.encode())
            reply = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
        return int(reply.split(b" ", 2)[1])

    def test_a_repeated_origin_header_is_refused(self):
        # Only the first value of a repeated header would otherwise be checked,
        # so a valid first could vouch for a hostile second.
        own = f"http://127.0.0.1:{self.port}"
        cases = {
            "origin": [f"Origin: {own}", "Origin: http://evil.com"],
            "origin-reversed": ["Origin: http://evil.com", f"Origin: {own}"],
            "fetch-site": ["Sec-Fetch-Site: same-origin", "Sec-Fetch-Site: cross-site"],
            "api-header": ["X-Claude-Usage: 1", "X-Claude-Usage: 1"],
            "referer": [f"Referer: {own}/", "Referer: http://evil.com/"],
        }
        for name, lines in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self._raw_get("/api/data", lines), 403)
        # One of each is still fine.
        self.assertEqual(self._raw_get("/api/data", ["Sec-Fetch-Site: same-origin"]), 200)

    def test_portless_own_origin_on_port_80(self):
        from email.message import Message

        def check(port, **hdrs):
            m = Message()
            for k, v in hdrs.items():
                m[k.replace("_", "-")] = v
            return serve._same_site_request(m, port)
        self.assertTrue(check(80, Origin="http://127.0.0.1"))
        self.assertTrue(check(80, Origin="http://localhost"))
        self.assertTrue(check(80, Referer="http://127.0.0.1/"))
        self.assertTrue(check(80, Origin="http://127.0.0.1:80"))
        self.assertFalse(check(80, Origin="http://evil.com"))
        self.assertFalse(check(80, Referer="http://127.0.0.1.evil.com/"))
        # On any other port the portless origin is some other server's.
        self.assertFalse(check(8765, Origin="http://127.0.0.1"))
        self.assertFalse(check(8765, Referer="http://127.0.0.1/"))

    def test_static_types_do_not_depend_on_the_machines_mime_table(self):
        # On Windows mimetypes reads the registry, where .js can be text/plain;
        # with nosniff the browser then refuses to run every script.
        import mimetypes
        saved = (dict(mimetypes.types_map), dict(mimetypes.common_types))
        mimetypes.add_type("text/plain", ".js")
        mimetypes.add_type("text/plain", ".css")
        mimetypes.add_type("text/plain", ".html")
        try:
            for path, want in (("/static/app.js", "text/javascript"), ("/static/team.js", "text/javascript"),
                               ("/static/app.css", "text/css"), ("/static/team.html", "text/html"),
                               ("/", "text/html")):
                with self.subTest(path=path):
                    status, _, headers = self._get_full(path)
                    self.assertEqual(status, 200)
                    self.assertTrue(headers["Content-Type"].startswith(want), headers["Content-Type"])
        finally:
            mimetypes.types_map.clear()
            mimetypes.types_map.update(saved[0])
            mimetypes.common_types.clear()
            mimetypes.common_types.update(saved[1])

    def test_every_allow_listed_static_file_has_an_explicit_type(self):
        for name in serve.ALLOWED_STATIC_FILES:
            self.assertIn(os.path.splitext(name)[1], serve.STATIC_CONTENT_TYPES, name)

    def test_cors_is_never_granted(self):
        # The custom header is only a proof of origin because a cross-origin
        # page needs a preflight to send it, and the server never approves one.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("OPTIONS", "/api/data", headers={
            "Host": f"127.0.0.1:{self.port}", "Origin": "http://evil.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-claude-usage"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 501)
        self.assertFalse([k for k, _ in resp.getheaders() if k.lower().startswith("access-control-")])
        for path in ("/", "/api/data", "/does-not-exist"):
            _, _, headers = self._get_full(path, headers={"Origin": f"http://127.0.0.1:{self.port}"})
            self.assertFalse([k for k in headers if k.lower().startswith("access-control-")], path)

    def test_api_request_with_no_origin_signal_rejected(self):
        # An old browser's cross-site <img> sends neither header, so "neither"
        # can't be told apart from an attack and must not trigger a rescan.
        for path in ("/api/data?full=1", "/api/data", "/api/export/all.zip",
                     "/api/export/summary.csv"):
            with self.subTest(path=path):
                status, _ = self._get(path, headers={"Sec-Fetch-Site": None})
                self.assertEqual(status, 403)

    def test_a_forced_rescan_needs_an_origin_signal(self):
        orig = serve.scan
        seen = []
        serve.scan = lambda *a, **kw: seen.append(kw) or orig(*a, **kw)
        try:
            self._get("/api/data?full=1", headers={"Sec-Fetch-Site": None})
        finally:
            serve.scan = orig
        self.assertEqual(seen, [])

    def test_plain_page_load_needs_no_origin_signal(self):
        # Typing the URL in an old browser sends neither header; the page
        # itself carries no data, so it must still load.
        for path in ("/", "/static/app.css"):
            with self.subTest(path=path):
                status, _ = self._get(path, headers={"Sec-Fetch-Site": None})
                self.assertEqual(status, 200)

    def test_every_response_forbids_framing_and_sniffing(self):
        want = {"X-Frame-Options": "DENY",
                "Content-Security-Policy": "frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff"}
        cases = {
            "page": ("/", {}),
            "api": ("/api/data", {}),
            "static": ("/static/app.js", {}),
            "export": ("/api/export/summary.csv", {}),
            "404": ("/does-not-exist", {}),
            "403 host": ("/", {"host": "evil.example.com"}),
            "403 origin": ("/api/data", {"headers": {"Sec-Fetch-Site": "cross-site"}}),
        }
        for name, (path, kw) in cases.items():
            with self.subTest(case=name):
                _, _, headers = self._get_full(path, **kw)
                for key, value in want.items():
                    self.assertEqual(headers.get(key), value)

    def test_a_server_error_response_carries_the_headers_too(self):
        def boom(*a, **kw):
            raise RuntimeError("scan exploded")
        orig = serve.scan
        serve.scan = boom
        try:
            with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stderr(devnull):
                status, _, headers = self._get_full("/api/data")
        finally:
            serve.scan = orig
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_host_header_must_match_exactly(self):
        port = self.port
        ok = ["127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"]
        bad = [f"127.0.0.1:{port}, evil.com", f"127.0.0.1:{port},evil", "127.0.0.1, evil.com",
               "evil.com", "localhost:1", f"[::1]:{port}", f"LOCALHOST:{port}",
               f"127.0.0.1.:{port}", f"127.0.0.1:{port} x"]
        for host in ok:
            with self.subTest(host=host):
                self.assertEqual(self._get("/", host=host)[0], 200)
        for host in bad:
            with self.subTest(host=host):
                self.assertEqual(self._get("/", host=host)[0], 403)

    def test_a_second_host_header_is_rejected(self):
        # With two Host headers the first one wins in the parser; a valid first
        # one must not vouch for an attacker's second.
        import socket
        for first, second in ((f"127.0.0.1:{self.port}", "evil.com"),
                              ("evil.com", f"127.0.0.1:{self.port}")):
            with self.subTest(first=first):
                with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
                    sock.sendall(f"GET / HTTP/1.1\r\nHost: {first}\r\nHost: {second}\r\n"
                                 f"Connection: close\r\n\r\n".encode())
                    reply = b""
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        reply += chunk
                self.assertTrue(reply.startswith(b"HTTP/1.0 403") or reply.startswith(b"HTTP/1.1 403"),
                                reply[:40])

    def test_cross_site_check_only_guards_the_api(self):
        # Static files carry no data, and a cross-site <script> can't read them.
        status, _ = self._get("/static/app.css", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 200)

    def _forced_scans(self, path):
        """The `force` flag each scan of one request to `path` was made with."""
        seen = []
        orig = serve.scan

        def spy(*a, force=False, **kw):
            seen.append(force)
            return orig(*a, force=force, **kw)
        serve.scan = spy
        try:
            status, _ = self._get(path)
        finally:
            serve.scan = orig
        self.assertEqual(status, 200)
        return seen

    def test_full_rescan_only_on_the_full_parameter(self):
        self.assertEqual(self._forced_scans("/api/data?full=1"), [True])
        self.assertEqual(self._forced_scans("/api/data?x=1&full=1"), [True])
        self.assertEqual(self._forced_scans("/api/data"), [False])
        # A parameter that merely ends in "full=1" is not a request for one.
        self.assertEqual(self._forced_scans("/api/data?nofull=1"), [False])
        self.assertEqual(self._forced_scans("/api/data?full=0"), [False])

    def test_handler_exception_returns_json_500(self):
        def boom(*a, **kw):
            raise RuntimeError("scan exploded")
        orig = serve.scan
        serve.scan = boom
        try:
            with open(os.devnull, "w", encoding="utf-8") as devnull, contextlib.redirect_stderr(devnull):
                status, body, headers = self._get_full("/api/data")
        finally:
            serve.scan = orig
        self.assertEqual(status, 500)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("scan exploded", json.loads(body)["error"])


class TestDashboardSendsTheApiHeader(unittest.TestCase):
    """The server trusts X-Claude-Usage as proof a request came from the
    dashboard's own page, so every /api fetch the page makes has to send it."""

    def _fetches(self, files, snippet):
        from tests.jsrun import run_js
        return run_js(files, "var calls = []; fetch = (u, o) => { calls.push([u, o || {}]); "
                             "return new Promise(() => {}); };" + snippet + "out(calls);")

    def test_dashboard_data_fetch_sends_it(self):
        calls = self._fetches(["static/habits.js", "static/app.js"], "loadData();")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/api/data")
        self.assertEqual(calls[0][1]["headers"]["X-Claude-Usage"], "1")

    def test_the_helper_keeps_the_callers_headers(self):
        for files in (["static/habits.js", "static/app.js"], ["static/team-findings.js", "static/team.js"]):
            calls = self._fetches(files, "apiFetch('/api/x', {cache: 'no-store', "
                                         "headers: {'Accept': 'application/json'}});")
            opts = calls[0][1]
            self.assertEqual(opts["headers"], {"Accept": "application/json", "X-Claude-Usage": "1"})
            self.assertEqual(opts["cache"], "no-store")

    def test_team_data_fetch_sends_it(self):
        calls = self._fetches(["static/team-findings.js", "static/team.js"], "load();")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/api/team")
        self.assertEqual(calls[0][1]["headers"]["X-Claude-Usage"], "1")

    def test_the_helper_is_the_only_raw_fetch(self):
        import re
        for name in ("app.js", "team.js", "team-findings.js", "habits.js", "filters.js"):
            with open(os.path.join(serve.STATIC_DIR, name), encoding="utf-8") as fh:
                src = fh.read()
            raw = re.findall(r"^[^/\n]*\bfetch\(.*$", src, re.M)
            # apiFetch's own call is the one allowed; any other page.fetch(...)
            # would skip the header.
            self.assertLessEqual(len(raw), 1, f"{name}: {raw}")
            for line in raw:
                self.assertIn("X-Claude-Usage", src)
                self.assertIn("Object.assign", line)


class TestSendJson(unittest.TestCase):
    """The payload goes to a browser's JSON.parse, which rejects the bare NaN and
    Infinity literals Python's json.dumps writes by default."""

    def _send(self, obj, accept_encoding=""):
        handler = mock.MagicMock()
        handler.headers = {"Accept-Encoding": accept_encoding}
        handler.wfile = io.BytesIO()
        serve.Handler._send_json(handler, obj)
        body = handler.wfile.getvalue()
        if accept_encoding == "gzip":
            body = gzip.decompress(body)
        return handler, body

    def test_non_finite_numbers_become_null(self):
        obj = {"a": float("nan"), "b": [1.5, float("inf"), {"c": -float("inf")}],
               "d": (float("nan"), 2), "ok": 0.25, "s": "nan", "n": None}
        for enc in ("", "gzip"):
            _, body = self._send(obj, enc)
            # Strict parse: a bare NaN/Infinity literal is an error here.
            def refuse(token):
                raise AssertionError(f"non-JSON literal {token}")
            parsed = json.loads(body, parse_constant=refuse)
            self.assertEqual(parsed, {"a": None, "b": [1.5, None, {"c": None}], "d": [None, 2],
                                      "ok": 0.25, "s": "nan", "n": None})

    def test_finite_payload_is_untouched_and_status_is_200(self):
        handler, body = self._send({"x": [1, 2.5, "y"]})
        self.assertEqual(json.loads(body), {"x": [1, 2.5, "y"]})
        handler.send_response.assert_called_once_with(200)


class TestExportRoutes(TestServer):
    """The UI's Export button is a plain download from these routes, so they
    carry the same guarantees as the rest of the server."""

    def _rows(self, body):
        return len(body.decode("utf-8").strip().splitlines()) - 1  # minus header

    def test_summary_download(self):
        status, body, headers = self._get_full("/api/export/summary.csv")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("claude-usage-summary-", headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"person_email,"))

    def test_sessions_download(self):
        status, body, headers = self._get_full("/api/export/sessions.csv")
        self.assertEqual(status, 200)
        self.assertIn("claude-usage-sessions-", headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"person_email,"))

    def test_date_range_narrows_the_download(self):
        _, every, _ = self._get_full("/api/export/sessions.csv")
        _, none_, _ = self._get_full("/api/export/sessions.csv?from=2030-01-01&to=2030-12-31")
        self.assertGreater(self._rows(every), 0)
        self.assertEqual(self._rows(none_), 0)

    def test_titles_can_be_suppressed(self):
        _, with_t, _ = self._get_full("/api/export/sessions.csv")
        _, without, _ = self._get_full("/api/export/sessions.csv?titles=0")
        self.assertIn(b"Fix the widget bug", with_t)
        self.assertNotIn(b"Fix the widget bug", without)

    def test_zip_download(self):
        import io, zipfile
        status, body, headers = self._get_full("/api/export/all.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("claude-usage-export-", headers["Content-Disposition"])
        archive = zipfile.ZipFile(io.BytesIO(body))
        self.assertIsNone(archive.testzip())
        names = archive.namelist()
        self.assertEqual(len(names), 6)
        for kind in ("summary", "sessions", "breakdown", "limits", "prs", "tools"):
            self.assertTrue(any(f"-{kind}-" in n and n.endswith(".csv") for n in names), names)
        for n in names:
            self.assertTrue(archive.read(n).startswith(b"person_email,"), n)

    def test_zip_respects_the_date_range(self):
        import io, zipfile
        status, body, _ = self._get_full("/api/export/all.zip?from=2030-01-01&to=2030-12-31")
        self.assertEqual(status, 200)
        archive = zipfile.ZipFile(io.BytesIO(body))
        for n in archive.namelist():
            rows = archive.read(n).decode().strip().splitlines()
            self.assertEqual(len(rows), 1, f"{n} should hold only a header")

    def test_zip_rejects_a_malformed_date(self):
        status, _ = self._get("/api/export/all.zip?from=nope")
        self.assertEqual(status, 403)

    def test_zip_requires_a_localhost_host_header(self):
        status, _ = self._get("/api/export/all.zip", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_breakdown_csv_route(self):
        status, body, headers = self._get_full("/api/export/breakdown.csv")
        self.assertEqual(status, 200)
        self.assertIn("-breakdown-", headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"person_email,"))

    def test_unknown_kind_404(self):
        status, _ = self._get("/api/export/salaries.csv")
        self.assertEqual(status, 404)

    def test_malformed_date_rejected(self):
        for bad in ("?from=yesterday", "?to=2026-13", "?from=../../etc/passwd"):
            status, _ = self._get("/api/export/summary.csv" + bad)
            self.assertEqual(status, 403, bad)

    def test_impossible_or_reversed_dates_rejected(self):
        # Right shape, not a date: these used to pass and crash the export.
        for bad in ("?from=2026-09-31", "?to=2026-02-30", "?from=2026-05-02&to=2026-05-01"):
            with self.subTest(bad=bad):
                status, _ = self._get("/api/export/all.zip" + bad)
                self.assertEqual(status, 403)

    def test_cross_site_export_download_rejected(self):
        status, _ = self._get("/api/export/all.zip", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_export_requires_a_localhost_host_header(self):
        # Without this a page on any site could pull the file cross-origin.
        status, _ = self._get("/api/export/summary.csv", host="evil.example.com")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
