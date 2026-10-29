"""Checks on the live price check (claude_usage/price_check.py).

The check is only useful if two things hold: a page it can't read is reported
as a failure, never as "prices match"; and it never gets in the way -- no
network when told not to, no waiting on a page load, no change to any price.
All tests here run offline against a saved page.
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_usage import price_check as pc
from claude_usage import pricing
from claude_usage.cache import FileCache
from claude_usage.scan import scan
from tests.test_pricing import _SYNTHETIC, _today

FIXTURE = Path(__file__).parent / "fixtures" / "pricing" / "pricing.md"
PAGE = FIXTURE.read_text(encoding="utf-8")
FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "projects")
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
_GONE = object()


class TestModelIds(unittest.TestCase):
    def test_names(self):
        cases = {
            "Claude Opus 5.5": "claude-opus-5-5",
            "Claude Haiku 4.5": "claude-haiku-4-5",
            "Claude Opus 4": "claude-opus-4",
            "Claude Opus 4.1 ([retired, except on Bedrock](https://x.y))": "claude-opus-4-1",
            "Claude Haiku 5.5 (for prompts up to 100,000 tokens)": "claude-haiku-5-5",
            "Claude Mythos 5.1 ([limited availability](https://x.y))": "claude-mythos-5-1",
        }
        for name, expected in cases.items():
            self.assertEqual(pc.model_id(name), expected, name)

    def test_non_models(self):
        for name in ("Model", "", ":---", "Batch input", "Claude"):
            self.assertIsNone(pc.model_id(name), name)


class TestParse(unittest.TestCase):
    def setUp(self):
        self.parsed = pc.parse(PAGE)

    def test_rates(self):
        self.assertEqual(self.parsed["rates"]["claude-opus-5-5"], {
            "input": 4.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0,
            "cache_read": 0.20, "output": 20.0})
        # Footnote markers don't leak into the number.
        self.assertEqual(self.parsed["rates"]["claude-sonnet-5"]["input"], 2.0)
        self.assertEqual(self.parsed["rates"]["claude-opus-4-1"]["output"], 75.0)

    def test_only_the_model_pricing_table(self):
        # The batch table below has the same model names at half price.
        self.assertEqual(self.parsed["rates"]["claude-opus-5-5"]["input"], 4.0)
        self.assertEqual(len(self.parsed["rates"]), 7)  # Haiku 5.5 is set aside as tiered

    def test_prompt_length_tiers_are_reported_not_guessed(self):
        self.assertEqual(self.parsed["tiered"], ["claude-haiku-5-5"])
        self.assertNotIn("claude-haiku-5-5", self.parsed["rates"])

    def test_prompt_length_tiers_are_read_with_their_threshold(self):
        tiers = self.parsed["tiers"]["claude-haiku-5-5"]
        self.assertEqual(tiers["over"], 100_000)
        self.assertEqual(tiers["up_to"], {"input": 0.10, "cache_write_5m": 0.125, "cache_write_1h": 0.20,
                                          "cache_read": 0.01, "output": 0.50})
        self.assertEqual(tiers["long"], {"input": 0.50, "cache_write_5m": 0.625, "cache_write_1h": 1.0,
                                         "cache_read": 0.05, "output": 2.50})

    def test_a_changed_page_is_a_failure_not_a_match(self):
        broken = {
            "section renamed": PAGE.replace("## Model pricing", "## Prices"),
            "column renamed": PAGE.replace("Cache hits and refreshes", "Cache reads"),
            "price unreadable": PAGE.replace("$5 / MTok             | $6.25", "five dollars | $6.25"),
            "table emptied": PAGE.split("| Claude Fable")[0],
        }
        for what, text in broken.items():
            with self.assertRaises(pc.PageChanged, msg=what):
                pc.parse(text)


class TestCompare(unittest.TestCase):
    def test_current_table_matches_the_saved_page(self):
        # The saved page has the corrected Sonnet 5.5 cache-read rate.
        result = pc.compare(pc.parse(PAGE))
        self.assertEqual(result["differences"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["tiered"], [])  # Haiku 5.5 is priced, both tiers

    def test_a_long_prompt_difference_is_reported(self):
        ours_long = pricing.current_long_prompt_rates()
        threshold, rates = ours_long["claude-haiku-5-5"]
        ours_long["claude-haiku-5-5"] = (threshold, {**rates, "output": 3.0})
        result = pc.compare(pc.parse(PAGE), ours_long=ours_long, used_models=["claude-haiku-5-5"])
        self.assertEqual(result["differences"], [{
            "model": "claude-haiku-5-5", "field": "output (prompts over 100,000 tokens)",
            "ours": 3.0, "published": 2.5, "used": True}])

    def test_a_moved_threshold_is_reported(self):
        page = PAGE.replace("over 100,000 tokens", "over 200,000 tokens")
        result = pc.compare(pc.parse(page))
        self.assertEqual([(d["model"], d["field"], d["ours"], d["published"]) for d in result["differences"]],
                         [("claude-haiku-5-5", "long-prompt threshold", 100_000, 200_000)])

    def test_a_cached_result_without_tier_rates_is_not_a_crash(self):
        published = pc.parse(PAGE)
        del published["tiers"]  # written by an older build
        result = pc.compare(published)
        self.assertEqual(result["differences"], [])

    def test_compares_against_the_rates_in_force_today_not_the_ones_at_import(self):
        # BUG-09: pricing.PRICING is frozen when the module loads, so a server
        # that outlives a dated price change would report false differences.
        page = PAGE.replace(
            "| $5 / MTok             | $6.25 / MTok    | $10 / MTok      | $0.50 / MTok             | $25 / MTok",
            "| $3 / MTok             | $3.75 / MTok    | $6 / MTok       | $0.30 / MTok             | $15 / MTok", 1)
        self.assertNotEqual(page, PAGE)
        # Only Opus 5 changes price on 2027-01-01 in this schedule.
        schedule = pricing._build_schedule({"claude-opus-5": _SYNTHETIC["claude-opus-5"]})
        with mock.patch.object(pricing, "_SCHEDULE", schedule):
            with _today(dt.date(2026, 12, 31)):
                frozen = pricing.current_rates()  # what PRICING holds if loaded then
            with mock.patch.object(pricing, "PRICING", frozen), _today(dt.date(2027, 1, 2)):
                result = pc.compare(pc.parse(page), used_models=["claude-opus-5"])
        self.assertEqual(result["differences"], [])

    def test_a_difference_is_reported_with_both_values(self):
        ours = dict(pricing.current_rates())
        ours["claude-opus-5"] = {**ours["claude-opus-5"], "output": 30.0}
        result = pc.compare(pc.parse(PAGE), ours=ours, used_models=["claude-opus-5[1m]"])
        self.assertEqual(result["differences"], [{
            "model": "claude-opus-5", "field": "output", "ours": 30.0,
            "published": 25.0, "used": True}])

    def test_published_models_we_cannot_price(self):
        ours = {k: v for k, v in pricing.current_rates().items() if k not in ("claude-opus-4-1", "claude-haiku-5-5")}
        result = pc.compare(pc.parse(PAGE), ours=ours, used_models=["claude-haiku-5-5"])
        self.assertEqual(result["missing"], [{"model": "claude-opus-4-1", "used": False}])
        self.assertEqual(result["tiered"], [{"model": "claude-haiku-5-5", "used": True}])


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache, self._env = pc.CACHE_FILE, os.environ.pop(pc.OFFLINE_ENV, None)
        pc.CACHE_FILE = Path(self.tmp.name) / "price-check.json"
        self.calls = 0

    def tearDown(self):
        pc.CACHE_FILE = self._cache
        if self._env is not None:
            os.environ[pc.OFFLINE_ENV] = self._env
        self.tmp.cleanup()

    def fetcher(self, text=PAGE):
        def fetch():
            self.calls += 1
            return text
        return fetch

    def test_ok(self):
        result = pc.check(now=NOW, fetcher=self.fetcher())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.calls, 1)

    def test_cached_for_a_day(self):
        pc.check(now=NOW, fetcher=self.fetcher())
        pc.check(now=NOW + dt.timedelta(hours=23), fetcher=self.fetcher())
        self.assertEqual(self.calls, 1)
        pc.check(now=NOW + dt.timedelta(hours=25), fetcher=self.fetcher())
        self.assertEqual(self.calls, 2)

    def test_force_ignores_the_cache(self):
        pc.check(now=NOW, fetcher=self.fetcher())
        pc.check(now=NOW, fetcher=self.fetcher(), force=True)
        self.assertEqual(self.calls, 2)

    def test_unreachable_is_failed_never_ok(self):
        def down():
            raise OSError("no route to host")
        result = pc.check(now=NOW, fetcher=down)
        self.assertEqual(result["status"], "failed")
        self.assertIn("couldn't reach", result["error"])
        self.assertTrue(pc.describe(result))

    def test_failure_is_retried_within_the_hour_not_the_day(self):
        def down():
            self.calls += 1
            raise OSError("down")
        pc.check(now=NOW, fetcher=down)
        pc.check(now=NOW + dt.timedelta(minutes=30), fetcher=down)
        self.assertEqual(self.calls, 1)
        pc.check(now=NOW + dt.timedelta(minutes=70), fetcher=down)
        self.assertEqual(self.calls, 2)

    def _write_cache(self, checked_at, published=True):
        import json
        data = {"checked_at": checked_at, "url": pc.PRICING_URL}
        if published:
            data["published"] = pc.parse(PAGE)
        pc.CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")

    def test_a_cache_timestamp_without_a_zone_is_not_a_crash(self):
        for stamp in ("2026-10-13T11:00:00", "2026-10-13"):
            with self.subTest(stamp=stamp):
                self._write_cache(stamp)
                pc.needs_refresh(now=NOW)
                result = pc.check(now=NOW, allow_fetch=False)
                self.assertIn(result["status"], ("ok", "differs", "pending"))

    def test_a_naive_timestamp_is_read_as_utc(self):
        # NOW is 12:00 UTC on 2026-10-08.
        self._write_cache("2026-10-08T11:00:00")
        self.assertFalse(pc.needs_refresh(now=NOW))
        self._write_cache("2026-10-06T11:00:00")
        self.assertTrue(pc.needs_refresh(now=NOW))

    def test_a_z_suffixed_timestamp_is_read(self):
        self._write_cache("2026-10-08T11:00:00Z")
        self.assertFalse(pc.needs_refresh(now=NOW))

    def test_a_malformed_published_block_is_treated_as_an_unreadable_cache(self):
        import copy
        import json
        good = pc.parse(PAGE)

        def published(**change):
            p = copy.deepcopy(good)
            p.update(change)
            return p

        def rates(**change):
            r = copy.deepcopy(good["rates"])
            for k, v in change.items():
                if v is _GONE:
                    del r["claude-opus-5-5"][k]
                else:
                    r["claude-opus-5-5"][k] = v
            return r

        def without(key):
            p = copy.deepcopy(good)
            del p[key]
            return p

        cases = {
            "no tiered": without("tiered"),
            "no rates": without("rates"),
            "rate str": published(rates=rates(input="3")),
            "rate None": published(rates=rates(input=None)),
            "rate missing": published(rates=rates(output=_GONE)),
            "rate inf": published(rates=rates(input=float("inf"))),
            "rates a list": published(rates=[1]),
            "rates entry a list": published(rates={"claude-opus-5-5": [1]}),
            "tiered [['x']]": published(tiered=[["x"]]),
            "published a string": "garbage",
            "published a list": [1, 2],
            "published None": None,
        }
        for label, pub in cases.items():
            with self.subTest(label):
                data = {"checked_at": NOW.isoformat(), "url": pc.PRICING_URL, "published": pub}
                pc.CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")  # inf / nan as bare literals
                result = pc.check(used_models=["claude-opus-5-5"], now=NOW, allow_fetch=False)
                # As for an unreadable cache: nothing to show until the next fetch.
                self.assertEqual(result["status"], "pending")
                json.dumps(result, allow_nan=False)
                pc.describe(result)  # the caller must be able to print it
                healed = pc.check(used_models=["claude-opus-5-5"], now=NOW, fetcher=self.fetcher())
                self.assertEqual(healed["status"], "ok")  # the next fetch replaces it

    def _write_published(self, published):
        import json
        pc.CACHE_FILE.write_text(json.dumps(
            {"checked_at": NOW.isoformat(), "url": pc.PRICING_URL, "published": published}),
            encoding="utf-8")

    def test_a_fresh_cache_that_cannot_be_compared_needs_a_refresh(self):
        # The server only fetches when needs_refresh() says so; a malformed
        # cache that looks fresh would otherwise show "pending" for a day.
        import copy
        good = pc.parse(PAGE)
        no_tiered = copy.deepcopy(good)
        del no_tiered["tiered"]
        str_rate = copy.deepcopy(good)
        str_rate["rates"]["claude-opus-5-5"]["input"] = "4"
        for label, pub in (("no tiered", no_tiered), ("string rate", str_rate), ("None", None)):
            with self.subTest(label):
                self._write_published(pub)
                self.assertTrue(pc.needs_refresh(now=NOW))
        self._write_published(good)
        self.assertFalse(pc.needs_refresh(now=NOW))

    def test_a_fetched_result_that_cannot_be_compared_is_not_kept_as_fresh(self):
        import copy
        import json
        broken = copy.deepcopy(pc.parse(PAGE))
        del broken["tiered"]
        with mock.patch.object(pc, "parse", return_value=broken):
            result = pc.check(used_models=["claude-opus-5-5"], now=NOW, fetcher=self.fetcher())
        self.assertEqual(result["status"], "failed")
        saved = json.loads(pc.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("published", saved)  # a failed record, retried within the hour
        self.assertFalse(pc.needs_refresh(now=NOW + dt.timedelta(minutes=30)))
        self.assertTrue(pc.needs_refresh(now=NOW + dt.timedelta(minutes=70)))

    def test_an_unparseable_timestamp_is_stale(self):
        for stamp in ("yesterday", "", None, 12345):
            with self.subTest(stamp=stamp):
                self._write_cache(stamp)
                self.assertTrue(pc.needs_refresh(now=NOW))
                self.assertEqual(pc.check(now=NOW, allow_fetch=False)["status"], "pending")

    def test_a_timestamp_far_in_the_future_is_stale(self):
        self._write_cache((NOW + dt.timedelta(days=365)).isoformat(timespec="seconds"))
        self.assertTrue(pc.needs_refresh(now=NOW))
        self.assertEqual(pc.check(now=NOW, allow_fetch=False)["status"], "pending")

    def test_a_timestamp_within_an_hour_ahead_is_still_fresh(self):
        # Clock skew between machines shouldn't force a refetch.
        self._write_cache((NOW + dt.timedelta(minutes=59)).isoformat(timespec="seconds"))
        self.assertFalse(pc.needs_refresh(now=NOW))
        self._write_cache((NOW + dt.timedelta(hours=1, minutes=1)).isoformat(timespec="seconds"))
        self.assertTrue(pc.needs_refresh(now=NOW))

    def test_changed_page_is_failed(self):
        result = pc.check(now=NOW, fetcher=self.fetcher(PAGE.replace("## Model pricing", "")))
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed shape", result["error"])

    def test_page_load_never_fetches(self):
        result = pc.check(now=NOW, fetcher=self.fetcher(), allow_fetch=False)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.calls, 0)
        pc.check(now=NOW, fetcher=self.fetcher())
        self.assertEqual(pc.check(now=NOW, allow_fetch=False)["status"], "ok")

    def test_offline_means_no_request(self):
        os.environ[pc.OFFLINE_ENV] = "1"
        result = pc.check(now=NOW, fetcher=self.fetcher(), force=True)
        self.assertEqual(result["status"], "offline")
        self.assertEqual(self.calls, 0)
        self.assertEqual(pc.describe(result), [])

    def test_differences_are_described(self):
        rates = pricing.current_rates()
        rates["claude-opus-5"] = {**rates["claude-opus-5"], "output": 30.0}
        with mock.patch.object(pricing, "current_rates", return_value=rates):
            result = pc.check(now=NOW, fetcher=self.fetcher(), used_models=["claude-opus-5"])
        self.assertEqual(result["status"], "differs")
        text = "\n".join(pc.describe(result))
        self.assertIn("claude-opus-5 output: table $30/MTok, published $25/MTok", text)

    def test_a_used_model_we_cannot_price_is_not_ok(self):
        # collect.py --check-prices exits 1 on anything but "ok": a model in
        # your logs that the table can't price is shown at $0, so it counts.
        ours = {k: v for k, v in pricing.current_rates().items() if k != "claude-opus-4-1"}
        with mock.patch.object(pricing, "current_rates", return_value=ours):
            result = pc.check(now=NOW, fetcher=self.fetcher(), used_models=["claude-opus-4-1-20250805"])
        self.assertEqual(result["status"], "differs")
        self.assertIn("claude-opus-4-1", "\n".join(pc.describe(result)))

    def test_an_unused_model_we_cannot_price_is_still_ok(self):
        ours = {k: v for k, v in pricing.current_rates().items() if k != "claude-opus-4-1"}
        with mock.patch.object(pricing, "current_rates", return_value=ours):
            result = pc.check(now=NOW, fetcher=self.fetcher(), used_models=["claude-opus-5"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["missing"], [{"model": "claude-opus-4-1", "used": False}])

    def test_a_used_tiered_model_we_cannot_price_is_not_ok(self):
        with mock.patch.object(pricing, "current_long_prompt_rates", return_value={}):
            result = pc.check(now=NOW, fetcher=self.fetcher(), used_models=["claude-haiku-5-5"])
        self.assertEqual(result["status"], "differs")

    # BUG-06: a page the parser only partly understood is never "ok".
    HAIKU_ROW = next(l for l in PAGE.splitlines() if "Claude Haiku 5.5" in l and "over 100,000" in l)

    def _check(self, page, **kw):
        return pc.check(now=NOW, fetcher=self.fetcher(page), used_models=["claude-haiku-5-5"], **kw)

    def test_a_reworded_long_prompt_row_is_not_ok(self):
        page = PAGE.replace("(for prompts over 100,000 tokens)", "(prompts > 100K)")
        self.assertNotEqual(page, PAGE)
        result = self._check(page)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["unparsed_tiers"], ["claude-haiku-5-5"])
        self.assertIn("unparsed tiers: claude-haiku-5-5", result["error"])
        self.assertIn("unparsed tiers: claude-haiku-5-5", "\n".join(pc.describe(result)))

    def test_a_third_row_for_a_tiered_model_is_not_ok(self):
        page = PAGE.replace(self.HAIKU_ROW, self.HAIKU_ROW + "\n" + self.HAIKU_ROW.replace("over 100,000", "over 500,000"))
        self.assertNotEqual(page, PAGE)
        result = self._check(page)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["unparsed_tiers"], ["claude-haiku-5-5"])

    def test_two_rows_that_are_both_over_rows_are_not_ok(self):
        page = PAGE.replace("(for prompts up to 100,000 tokens)", "(for prompts over 50,000 tokens)")
        self.assertEqual(self._check(page)["status"], "failed")

    def test_an_unrecognised_model_row_is_reported_and_not_ok(self):
        row = next(l for l in PAGE.splitlines() if l.startswith("| Claude Opus 5 "))
        page = PAGE.replace(row, row + "\n" + row.replace("Claude Opus 5 ", "Claude Opus 5 Preview"))
        self.assertNotEqual(page, PAGE)
        result = self._check(page)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["skipped"], ["Claude Opus 5 Preview"])
        self.assertIn("Claude Opus 5 Preview", result["error"])
        self.assertIn("Claude Opus 5 Preview", "\n".join(pc.describe(result)))
        # A model that isn't in the logs doesn't make it any safer.
        self.assertEqual(pc.check(now=NOW, fetcher=self.fetcher(page), used_models=[], force=True)["status"], "failed")

    def test_a_row_that_is_not_a_claude_model_is_not_skipped(self):
        row = next(l for l in PAGE.splitlines() if l.startswith("| Claude Opus 5 "))
        page = PAGE.replace(row, row + "\n" + row.replace("Claude Opus 5 ", "Some Other Model"))
        parsed = pc.parse(page)
        self.assertEqual(parsed["skipped"], [])
        self.assertEqual(self._check(page)["status"], "ok")

    def test_the_fixture_page_has_nothing_skipped_or_unparsed(self):
        result = self._check(PAGE)
        self.assertEqual((result["status"], result["skipped"], result["unparsed_tiers"]), ("ok", [], []))

    def test_a_partly_understood_page_is_retried_within_the_hour(self):
        page = PAGE.replace("(for prompts over 100,000 tokens)", "(prompts > 100K)")
        self._check(page)
        self.assertFalse(pc.needs_refresh(now=NOW + dt.timedelta(minutes=30)))
        self.assertTrue(pc.needs_refresh(now=NOW + dt.timedelta(minutes=70)))
        # Served from the cache within the hour, still not "ok".
        cached = pc.check(now=NOW + dt.timedelta(minutes=30), allow_fetch=False, used_models=["claude-haiku-5-5"])
        self.assertEqual((cached["status"], cached["unparsed_tiers"]), ("failed", ["claude-haiku-5-5"]))

    def test_rate_differences_are_still_listed_when_the_page_is_partly_understood(self):
        page = PAGE.replace("(for prompts over 100,000 tokens)", "(prompts > 100K)").replace("$4 / MTok ", "$400 / MTok ")
        result = self._check(page)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(d["model"] == "claude-opus-5-5" for d in result["differences"]))
        text = "\n".join(pc.describe(result))
        self.assertIn("claude-opus-5-5 input", text)

    # BUG-06 (review): markup around a model name must not hide its row.
    OPUS_ROW = next(l for l in PAGE.splitlines() if l.startswith("| Claude Opus 5 "))

    def _opus5(self, name=None, output="$25 / MTok"):
        row = self.OPUS_ROW
        if name:
            row = row.replace("Claude Opus 5      ", name + "      ", 1)
        return PAGE.replace(self.OPUS_ROW, row.replace("$25 / MTok", output))

    def _opus5_check(self, page):
        return pc.check(used_models=["claude-opus-5"], force=True, now=NOW, fetcher=self.fetcher(page))

    def test_markup_around_a_model_name_does_not_hide_a_price_change(self):
        names = ["**Claude Opus 5**", "*Claude Opus 5*", "_Claude Opus 5_", "`Claude Opus 5`",
                 "Claude&nbsp;Opus 5", "Claude\u00a0Opus 5", "Anthropic Claude Opus 5",
                 "Claude Opus 5<sup>4</sup>", "Claude Opus 5[^4]", "Claude Opus 5 <sup>[4]</sup>",
                 "<b>Claude Opus 5</b>", "[Claude Opus 5](https://x.y/a_(b))"]
        for name in names:
            with self.subTest(name=name):
                result = self._opus5_check(self._opus5(name, "$30 / MTok"))
                self.assertEqual(result["status"], "differs", result.get("error"))
                self.assertEqual([(d["model"], d["field"], d["published"]) for d in result["differences"]],
                                 [("claude-opus-5", "output", 30.0)])
                self.assertEqual((result["skipped"], result["missing"], result["unverified"]), ([], [], []))

    def test_markup_around_an_unchanged_model_name_is_still_ok(self):
        for name in ("**Claude Opus 5**", "Claude&nbsp;Opus 5", "Claude Opus 5<sup>4</sup>", "Claude Opus 5 (new)"):
            with self.subTest(name=name):
                self.assertEqual(self._opus5_check(self._opus5(name))["status"], "ok")

    def test_the_footnote_marker_does_not_become_part_of_the_id(self):
        parsed = pc.parse(self._opus5("Claude Opus 5<sup>4</sup>"))
        self.assertIn("claude-opus-5", parsed["rates"])
        self.assertNotIn("claude-opus-54", parsed["rates"])

    def test_a_model_row_that_is_still_unrecognised_after_cleaning_is_skipped(self):
        for name, shown in (("**Claude Opus 5 Fast**", "Claude Opus 5 Fast"), ("Anthropic Claude Opus 5 Preview", "Claude Opus 5 Preview")):
            with self.subTest(name=name):
                page = PAGE.replace(self.OPUS_ROW, self.OPUS_ROW + "\n" + self.OPUS_ROW.replace("Claude Opus 5      ", name + "      ", 1))
                self.assertEqual(pc.parse(page)["skipped"], [shown])

    def test_a_row_naming_claude_mid_cell_is_skipped(self):
        page = PAGE.replace(self.OPUS_ROW, self.OPUS_ROW + "\n" + self.OPUS_ROW.replace("Claude Opus 5 ", "The Claude Opus 5 "))
        self.assertEqual(pc.parse(page)["skipped"], ["The Claude Opus 5"])

    # BUG-21: a price cell holding more than one amount is a page change, not
    # the first amount.
    MULTI_CELLS = ("~~$20~~ $25 / MTok", "<s>$20</s> $25 / MTok", "<del>$20</del> $25 / MTok",
                   "$20 \u2192 $25 / MTok", "$25 / MTok (was $20)", "$20 / MTok, now $25 / MTok")

    def test_a_cell_with_two_prices_is_a_changed_page(self):
        for cell in self.MULTI_CELLS:
            with self.subTest(cell=cell):
                result = self._opus5_check(self._opus5(output=cell))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["multi_price"], ["claude-opus-5 / output"])
                self.assertIn("claude-opus-5 / output", result["error"])
                # Neither amount is taken as the price, and the model isn't "not listed".
                self.assertEqual((result["differences"], result["unverified"]), ([], []))
                self.assertIn("claude-opus-5 / output", "\n".join(pc.describe(result)))

    def test_a_multi_price_cell_fails_whether_or_not_the_model_is_used(self):
        page = self._opus5(output="~~$20~~ $25 / MTok")
        result = pc.check(used_models=[], force=True, now=NOW, fetcher=self.fetcher(page))
        self.assertEqual((result["status"], result["multi_price"]), ("failed", ["claude-opus-5 / output"]))

    def test_parse_lists_every_multi_price_cell_and_drops_the_model(self):
        page = self._opus5(output="~~$20~~ $25 / MTok")
        row = next(l for l in page.splitlines() if l.startswith("| Claude Opus 5 "))
        page = page.replace(row, row.replace("$5 / MTok", "$5 \u2192 $6 / MTok", 1))
        self.assertNotEqual(page, self._opus5(output="~~$20~~ $25 / MTok"))
        parsed = pc.parse(page)
        self.assertEqual(parsed["multi_price"], ["claude-opus-5 / input", "claude-opus-5 / output"])
        self.assertNotIn("claude-opus-5", parsed["rates"])
        self.assertIn("claude-opus-5-5", parsed["rates"])

    def test_a_multi_price_cell_does_not_hide_other_rate_differences(self):
        page = self._opus5(output="~~$20~~ $25 / MTok").replace("$4 / MTok ", "$400 / MTok ")
        result = self._opus5_check(page)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(d["model"] == "claude-opus-5-5" for d in result["differences"]))

    def test_a_multi_price_cell_is_retried_within_the_hour(self):
        self._opus5_check(self._opus5(output="~~$20~~ $25 / MTok"))
        self.assertFalse(pc.needs_refresh(now=NOW + dt.timedelta(minutes=30)))
        self.assertTrue(pc.needs_refresh(now=NOW + dt.timedelta(minutes=70)))
        cached = pc.check(now=NOW + dt.timedelta(minutes=30), allow_fetch=False, used_models=["claude-opus-5"])
        self.assertEqual((cached["status"], cached["multi_price"]), ("failed", ["claude-opus-5 / output"]))

    def test_one_price_per_cell_is_unaffected(self):
        parsed = pc.parse(PAGE)
        self.assertEqual(parsed["multi_price"], [])
        self.assertEqual(self._opus5_check(PAGE)["status"], "ok")

    # BUG-20: an alias in your logs is checked against the page's canonical row.
    def test_an_alias_in_the_logs_is_verified_by_its_canonical_row(self):
        row = next(l for l in PAGE.splitlines() if l.startswith("| Claude Opus 4.1 "))
        page = PAGE.replace(row, row + "\n" + row.replace("Claude Opus 4.1 ([retired, except on Bedrock](https://example.com))",
                                                           "Claude Opus 4"))
        self.assertIn("claude-opus-4", pc.parse(page)["rates"])
        for raw in ("claude-opus-4-0", "claude-opus-4-0-20250514", "claude-opus-4-latest"):
            with self.subTest(raw=raw):
                result = pc.check(used_models=[raw], force=True, now=NOW, fetcher=self.fetcher(page))
                self.assertEqual((result["status"], result["unverified"]), ("ok", []), result.get("error"))
        # The honest case: the page has no Opus 4 row, so nothing was compared.
        result = pc.check(used_models=["claude-opus-4-0"], force=True, now=NOW, fetcher=self.fetcher(PAGE))
        self.assertEqual((result["status"], result["unverified"]), ("failed", ["claude-opus-4"]))

    def test_a_used_model_missing_from_the_page_is_not_ok(self):
        page = PAGE.replace(self.OPUS_ROW + "\n", "")
        self.assertNotEqual(page, PAGE)
        result = self._opus5_check(page)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["unverified"], ["claude-opus-5"])
        self.assertIn("claude-opus-5", result["error"])
        self.assertIn("claude-opus-5", "\n".join(pc.describe(result)))
        # Not in the logs, so nothing in them goes unchecked.
        unused = pc.check(used_models=["claude-opus-5-5"], force=True, now=NOW, fetcher=self.fetcher(page))
        self.assertEqual((unused["status"], unused["unverified"]), ("ok", []))

    def test_an_unmodified_page_has_nothing_unverified(self):
        result = self._opus5_check(PAGE)
        self.assertEqual((result["status"], result["unverified"]), ("ok", []))

    def test_a_used_tiered_model_on_the_page_is_verified(self):
        result = self._check(PAGE)
        self.assertEqual(result["unverified"], [])

    def test_a_used_model_we_do_not_price_is_not_unverified(self):
        result = pc.check(used_models=["claude-nonesuch-9"], force=True, now=NOW, fetcher=self.fetcher())
        self.assertEqual((result["status"], result["unverified"]), ("ok", []))

    def test_an_unverified_model_keeps_the_daily_cadence(self):
        # The page parsed fine; fetching again within the hour won't change it.
        page = PAGE.replace(self.OPUS_ROW + "\n", "")
        self._opus5_check(page)
        self.assertFalse(pc.needs_refresh(now=NOW + dt.timedelta(minutes=70)))
        self.assertFalse(pc.needs_refresh(now=NOW + dt.timedelta(hours=23)))
        self.assertTrue(pc.needs_refresh(now=NOW + dt.timedelta(hours=25)))
        cached = pc.check(now=NOW + dt.timedelta(hours=2), allow_fetch=False, used_models=["claude-opus-5"])
        self.assertEqual((cached["status"], cached["unverified"]), ("failed", ["claude-opus-5"]))

    def test_the_check_never_changes_a_price(self):
        before = {k: dict(v) for k, v in pricing.PRICING.items()}
        pc.check(now=NOW, fetcher=self.fetcher(PAGE.replace("$4 / MTok ", "$400 / MTok ")))
        self.assertEqual(pricing.PRICING, before)


class TestDashboardNotice(unittest.TestCase):
    def test_a_partly_read_page_is_shown_not_hidden(self):
        from tests.jsrun import run_js
        payload = {"price_check": {"status": "failed", "url": pc.PRICING_URL,
                                   "unparsed_tiers": ["claude-haiku-5-5"],
                                   "skipped": ["Claude Opus 5 <b>Preview"],
                                   "unverified": ["claude-opus-5<i>"],
                                   "multi_price": ["claude-opus-5 / <u>output"],
                                   "differences": [], "missing": [], "tiered": []},
                   "summary": {}, "history": {}, "unknown_models": [], "pricing": {}}
        html = run_js(["static/habits.js", "static/app.js"],
                      f"PAYLOAD = {json.dumps(payload)}; renderNotices(); out(el('notices').innerHTML)")
        self.assertIn("unparsed tiers: claude-haiku-5-5", html)
        self.assertIn("Claude Opus 5 &lt;b&gt;Preview", html)  # escaped, not markup
        self.assertIn("claude-opus-5&lt;i&gt;", html)
        self.assertIn("claude-opus-5 / &lt;u&gt;output", html)
        self.assertNotIn("<i>", html)


class TestNoNetworkInNormalRuns(unittest.TestCase):
    def test_scanning_makes_no_request(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            payload = scan(FIXTURES_ROOT, cache=FileCache(os.path.join(tempfile.mkdtemp(), "c.json")))
        self.assertIn("usage", payload)

    def test_suite_runs_offline(self):
        self.assertEqual(pc.check()["status"], "offline")


if __name__ == "__main__":
    unittest.main()
