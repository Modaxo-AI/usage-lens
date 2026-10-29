"""Every timestamp the parser keeps is one canonical string -- UTC, millisecond
precision, `YYYY-MM-DDTHH:MM:SS.mmmZ` -- or None (BUG-11, BUG-12). A record
whose own timestamp is missing or unreadable borrows the file's nearest one,
so no request ends up in a `None` or garbage bucket."""
import os
import re
import unittest

from claude_usage import aggregate, limits, parser
from claude_usage.parser import _canon_ts, parse_file
from tests.test_input_hardening import _Scanned

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CANON = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _req(n, ts=..., **extra):
    rec = {"type": "assistant", "requestId": f"req-{n}",
           "message": {"id": f"msg-{n}", "model": "claude-sonnet-5",
                       "usage": {"input_tokens": 10, "output_tokens": 20}}}
    if ts is not ...:
        rec["timestamp"] = ts
    rec.update(extra)
    return rec


class TestCanonTs(unittest.TestCase):
    def test_native_form_passes_through_byte_for_byte(self):
        for ts in ("2026-03-01T10:05:00.123Z", "2026-03-01T10:05:00.000Z"):
            self.assertEqual(_canon_ts(ts), ts)

    def test_every_accepted_form_becomes_utc_milliseconds(self):
        cases = {
            "2026-03-01T10:05:00Z": "2026-03-01T10:05:00.000Z",
            "2026-03-01T10:05:00": "2026-03-01T10:05:00.000Z",    # no zone: UTC
            "2026-03-01 10:05:00": "2026-03-01T10:05:00.000Z",
            "2026-03-01": "2026-03-01T00:00:00.000Z",
            "2026-03-01T15:00:00+05:00": "2026-03-01T10:00:00.000Z",
            "2026-03-01T05:00:00-05:00": "2026-03-01T10:00:00.000Z",
            "2026-03-01T10:05:00.5Z": "2026-03-01T10:05:00.500Z",
            "2026-03-01T10:05:00.123456Z": "2026-03-01T10:05:00.123Z",  # truncated
            "2026-03-01T10:05:00.123456789Z": "2026-03-01T10:05:00.123Z",
            "2026-03-01T10:05:00.12+00:00": "2026-03-01T10:05:00.120Z",
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                self.assertEqual(_canon_ts(raw), want)

    def test_unreadable_or_non_string_is_none(self):
        for raw in ("garbage-string-xx", "", "2026-13-45T00:00:00Z", "2026-03-01T25:00:00Z",
                    "0001-01-01T00:00:00+05:00",  # UTC would be year 0
                    None, 5, 1.5, ["2026-03-01"], {"a": 1}, True):
            with self.subTest(repr(raw)):
                self.assertIsNone(_canon_ts(raw))

    def test_years_outside_2000_to_2100_are_none(self):
        # The extremes survive fromisoformat but break everything downstream:
        # parse_reset, local-time bucketing, exports.
        for raw in ("9999-12-31T23:59:59Z", "0001-01-01T00:00:00Z", "1999-12-31T23:59:59Z",
                    "2101-01-01T00:00:00Z", "2100-12-31T23:00:00-05:00",  # 2101 in UTC
                    "2000-01-01T00:00:00+01:00"):                       # 1999 in UTC
            with self.subTest(raw):
                self.assertIsNone(_canon_ts(raw))
        self.assertEqual(_canon_ts("2000-01-01T00:00:00Z"), "2000-01-01T00:00:00.000Z")
        self.assertEqual(_canon_ts("2100-12-31T23:59:59.999Z"), "2100-12-31T23:59:59.999Z")

    def test_hour_24_is_none_on_every_python(self):
        # 3.11+ reads it as midnight of the next day, 3.9 refuses it.
        for raw in ("2026-01-01T24:00:00Z", "2026-01-01T24:00", "2026-01-01 24:00:00",
                    "2026-01-01T24:00:00+05:00"):
            with self.subTest(raw):
                self.assertIsNone(_canon_ts(raw))
        self.assertEqual(_canon_ts("2026-01-01T23:59:59Z"), "2026-01-01T23:59:59.000Z")

    def test_canonical_strings_sort_chronologically(self):
        stamps = [_canon_ts(s) for s in ("2026-03-01T15:00:00+05:00", "2026-03-01T10:30:00Z",
                                         "2026-03-01T09:59:59.999Z", "2026-03-01")]
        self.assertEqual(sorted(stamps), [stamps[3], stamps[2], stamps[0], stamps[1]])


class TestTimestampsInTheScan(_Scanned):
    def _scan_one(self, records):
        self.write(records)
        return self.scan(), parse_file(os.path.join(self.root, "proj", "s1.jsonl"), "main")

    def test_garbage_timestamp_never_becomes_a_bucket(self):
        payload, facts = self._scan_one([
            _req(1, "2026-03-01T10:00:00.000Z"), _req(2, "garbage-string-xx")])
        self.assertEqual(sum(s["requests"] for s in payload["sessions"]), 2)
        for row in payload["usage"]:
            self.assertRegex(row[0], _DATE)
        self.assertRegex(payload["summary"]["date_max"], _DATE)
        self.assertEqual(payload["sessions"][0]["end"], "2026-03-01T10:00:00.000Z")
        self.assertEqual(payload["sessions"][0]["start"], "2026-03-01T10:00:00.000Z")
        self.assertEqual({r["ts"] for r in facts.requests}, {"2026-03-01T10:00:00.000Z"})

    def test_timestamps_without_a_zone_do_not_raise(self):
        for odd in ("2026-03-01T10:05:00", "2026-03-01 10:05:00", "2026-03-01"):
            with self.subTest(odd):
                payload, facts = self._scan_one([
                    _req(1, "2026-03-01T10:00:00.000Z"), _req(2, odd)])
                self.assertEqual(sum(s["requests"] for s in payload["sessions"]), 2)
                self.assertTrue(_CANON.match(facts.first_ts))
                self.assertTrue(_CANON.match(facts.last_ts))
                for r in facts.requests:
                    self.assertTrue(_CANON.match(r["ts"]), r["ts"])

    def test_offsets_order_by_instant_not_by_text(self):
        # "15:00+05:00" is 10:00Z, which is before 10:30Z though it sorts after as text.
        payload, facts = self._scan_one([
            _req(1, "2026-03-01T10:30:00Z"), _req(2, "2026-03-01T15:00:00+05:00")])
        self.assertEqual(facts.first_ts, "2026-03-01T10:00:00.000Z")
        self.assertEqual(facts.last_ts, "2026-03-01T10:30:00.000Z")
        session = payload["sessions"][0]
        self.assertEqual((session["start"], session["end"]),
                         ("2026-03-01T10:00:00.000Z", "2026-03-01T10:30:00.000Z"))

    def test_missing_timestamp_mid_file_takes_the_previous_record(self):
        _, facts = self._scan_one([_req(1, "2026-03-01T10:00:00.000Z"),
                                   _req(2, "2026-03-01T10:20:00.000Z"), _req(3)])
        by_id = {r["response_id"]: r["ts"] for r in facts.requests}
        self.assertEqual(by_id["msg-3"], "2026-03-01T10:20:00.000Z")

    def test_unreadable_timestamp_mid_file_takes_the_previous_record(self):
        _, facts = self._scan_one([_req(1, "2026-03-01T10:00:00.000Z"), _req(2, "garbage")])
        by_id = {r["response_id"]: r["ts"] for r in facts.requests}
        self.assertEqual(by_id["msg-2"], "2026-03-01T10:00:00.000Z")

    def test_missing_timestamp_on_the_first_line_takes_the_files_first(self):
        payload, facts = self._scan_one([_req(1), _req(2, "2026-03-01T10:00:00.000Z")])
        by_id = {r["response_id"]: r["ts"] for r in facts.requests}
        self.assertEqual(by_id["msg-1"], "2026-03-01T10:00:00.000Z")
        self.assertEqual(len(payload["usage"]), 1)  # both land in the same bucket
        for row in payload["usage"]:
            self.assertIsNotNone(row[0])
            self.assertRegex(row[0], _DATE)

    def test_a_file_with_no_timestamps_at_all_still_scans(self):
        payload, facts = self._scan_one([_req(1), _req(2)])
        self.assertEqual(sum(s["requests"] for s in payload["sessions"]), 2)
        self.assertIsNone(facts.first_ts)

    def test_other_timestamp_carriers_are_canonical_too(self):
        recs = [
            _req(1, "2026-03-01T15:00:00+05:00"),
            {"type": "pr-link", "timestamp": "2026-03-01T15:00:00+05:00",
             "prUrl": "https://example.com/pr/1", "prNumber": 1},
            {"type": "system", "subtype": "turn_duration", "uuid": "u1",
             "timestamp": "2026-03-01T15:00:00+05:00", "durationMs": 5},
            {"type": "cost-state", "totalCostUSD": 1.0, "startTime": 1},
        ]
        _, facts = self._scan_one(recs)
        self.assertEqual(facts.pr_links[0]["timestamp"], "2026-03-01T10:00:00.000Z")
        self.assertEqual(facts.friction[0][1], "2026-03-01T10:00:00.000Z")
        self.assertEqual(facts.cost_states[0]["ts"], "2026-03-01T10:00:00.000Z")

    def test_extreme_years_fall_back_like_any_unreadable_timestamp(self):
        notice = {"type": "assistant", "uuid": "n1", "isApiErrorMessage": True,
                  "error": "rate_limit", "message": {"content": [{"type": "text", "text":
                  "You've hit your limit · resets 3pm (UTC)"}]}}
        for year in ("9999-12-31T23:59:59.000Z", "0001-01-01T00:00:00.000Z"):
            with self.subTest(year):
                payload, facts = self._scan_one([
                    _req(1, "2026-03-01T10:00:00.000Z"), _req(2, year),
                    dict(notice, timestamp=year)])
                self.assertEqual(sum(s["requests"] for s in payload["sessions"]), 2)
                self.assertEqual({r["ts"] for r in facts.requests}, {"2026-03-01T10:00:00.000Z"})
                self.assertEqual(payload["summary"]["date_max"], aggregate._local_date(
                    "2026-03-01T10:00:00.000Z"))
                self.assertEqual(payload["summary"]["date_min"], payload["summary"]["date_max"])
                self.assertEqual(payload["sessions"][0]["end"], "2026-03-01T10:00:00.000Z")
                self.assertEqual([e["ts"] for e in facts.limit_events], [None])

    def test_parse_reset_survives_a_date_at_the_edge_of_the_calendar(self):
        for event in ("9999-12-31T23:59:59.000Z", "0001-01-01T00:00:00.000Z"):
            for text in ("resets 3pm (UTC)", "resets Dec 31 at 4am (America/New_York)",
                         "resets 3pm (Asia/Tokyo)"):
                with self.subTest(event=event, text=text):
                    limits.parse_reset("You've hit your limit · " + text, event)  # must not raise

    def test_facts_version_was_bumped(self):
        self.assertGreaterEqual(parser.FACTS_VERSION, 12)

    def test_facts_version_covers_the_stored_reset_time(self):
        # limit_events[].resets_at (limits.parse_notice) changed what the parser
        # stores after version 14, so caches written at 14 must be discarded.
        self.assertGreaterEqual(parser.FACTS_VERSION, 15)


if __name__ == "__main__":
    unittest.main()
