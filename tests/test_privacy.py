import csv
import io
import json
import os
import tempfile
import unittest

from claude_usage import export as export_mod
from claude_usage.scan import scan

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
SENTINEL = "SECRET_PROMPT_XYZ"


class TestPrivacy(unittest.TestCase):
    def test_sentinel_absent_from_full_payload(self):
        # The fixtures embed SENTINEL in user text, assistant text, tool_use
        # input, and tool_result content -- none of that may ever reach the
        # payload sent to the browser.
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        dumped = json.dumps(payload)
        self.assertNotIn(SENTINEL, dumped)

    def test_sentinel_absent_from_the_csv_export(self):
        # The export is the one output meant to leave your machine, so the same
        # guarantee has to hold there: it carries titles, paths and PR links by
        # design, but never prompts, replies, tool inputs or tool results.
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        person = ("someone@example.com", "Some One")
        with tempfile.TemporaryDirectory() as tmp:
            written = export_mod.export(payload, out_dir=tmp, person=person)
            for path, _ in written:
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn(SENTINEL, fh.read(), path)

    def test_export_rows_carry_no_message_content(self):
        payload = scan(FIXTURES_ROOT, cache=_fresh_cache())
        person = ("someone@example.com", "Some One")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(export_mod.summary_rows(payload, person))
        writer.writerows(export_mod.session_rows(payload, person))
        self.assertNotIn(SENTINEL, buf.getvalue())

    def test_sentinel_present_in_source_fixture(self):
        # Sanity check that the fixture actually contains the sentinel, so a
        # bug that made parsing silently skip the file wouldn't give a false pass.
        path = os.path.join(FIXTURES_ROOT, "-Users-x-proj1", "sess-main-1.jsonl")
        with open(path, encoding="utf-8") as fh:
            self.assertIn(SENTINEL, fh.read())


def _fresh_cache():
    from claude_usage.cache import FileCache
    return FileCache(os.path.join(tempfile.mkdtemp(), "parsed.json"))


if __name__ == "__main__":
    unittest.main()
