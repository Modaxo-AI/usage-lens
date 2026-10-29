"""serve.py's first line reaches a reader that isn't a terminal straight away:
the plugin's skills run the server in the background and read the URL from it."""
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from unittest import mock

from claude_usage import paths, price_check

REPO = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestStartupLine(unittest.TestCase):
    def test_the_url_is_printed_before_the_server_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                   paths.DATA_DIR_ENV: str(Path(tmp) / "data"), price_check.OFFLINE_ENV: "1"}
            port = _free_port()
            proc = subprocess.Popen([sys.executable, str(REPO / "serve.py"), "--no-open", "--offline",
                                     "--port", str(port)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.wait, 10)
            self.addCleanup(proc.kill)
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
            try:
                line = lines.get(timeout=60)
            except queue.Empty:
                self.fail("no output while the server was running: stdout isn't flushed")
            self.assertIn(f"http://127.0.0.1:{port}/", line)


class TestNoNameLookup(unittest.TestCase):
    def test_binding_does_not_look_up_the_hosts_name(self):
        # HTTPServer would call socket.getfqdn("127.0.0.1"), seconds-slow on
        # some Macs, before the URL line could be printed.
        import serve
        with mock.patch("socket.getfqdn", side_effect=AssertionError("looked up")):
            server = serve.LocalServer(("127.0.0.1", 0), serve.Handler)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_name, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
