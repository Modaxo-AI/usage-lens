import datetime
import json
import re
import unittest
from unittest import mock

from claude_usage import aggregate, pricing
from claude_usage.pricing import PRICING, cost_for, normalize_model, rates_for


class TestNormalizeModel(unittest.TestCase):
    def test_strips_date_suffix(self):
        self.assertEqual(normalize_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_strips_bracket_suffix(self):
        self.assertEqual(normalize_model("claude-opus-5-5[1m]"), "claude-opus-5-5")

    def test_strips_both(self):
        self.assertEqual(normalize_model("claude-sonnet-5-20260101[1m]"), "claude-sonnet-5")

    def test_plain_name_unchanged(self):
        self.assertEqual(normalize_model("claude-sonnet-5"), "claude-sonnet-5")

    def test_empty_string(self):
        self.assertEqual(normalize_model(""), "")
        self.assertIsNone(normalize_model(None))


class TestRatesFor(unittest.TestCase):
    def test_known_model(self):
        self.assertIsNotNone(rates_for("claude-sonnet-5"))

    def test_haiku_present(self):
        rates = rates_for("claude-haiku-4-5-20251001")
        self.assertEqual(rates["input"], 1.00)
        self.assertEqual(rates["output"], 5.00)
        self.assertEqual(rates["cache_read"], 0.10)
        self.assertEqual(rates["cache_write_5m"], 1.25)

    def test_unknown_model(self):
        self.assertIsNone(rates_for("claude-made-up-9000"))

    def test_1h_is_2x_input_for_all_models(self):
        for model, rates in PRICING.items():
            self.assertAlmostEqual(rates["cache_write_1h"], rates["input"] * 2.0, msg=model)


class TestCostFor(unittest.TestCase):
    def test_known_model_all_fields(self):
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000},
        }
        cost = cost_for("claude-sonnet-5", usage)
        # 2 (in) + 10 (out) + 0.20 (cache read) + 2.50 (cw5m) + 4.00 (cw1h = 2*input)
        self.assertAlmostEqual(cost, 2 + 10 + 0.20 + 2.50 + 4.00)

    def test_fallback_when_no_cache_creation_breakdown(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000}
        cost = cost_for("claude-sonnet-5", usage)
        self.assertAlmostEqual(cost, 2.50)  # treated as 5m

    def test_unknown_model_returns_zero_and_records(self):
        unknown = set()
        cost = cost_for("claude-totally-unknown", {"input_tokens": 1_000_000}, unknown_models=unknown)
        self.assertEqual(cost, 0.0)
        self.assertIn("claude-totally-unknown", unknown)

    def test_unknown_model_without_tracking_set(self):
        cost = cost_for("claude-totally-unknown", {"input_tokens": 1_000_000})
        self.assertEqual(cost, 0.0)

    def test_normalizes_model_name_with_suffixes(self):
        usage = {"input_tokens": 1_000_000}
        cost_normalized = cost_for("claude-sonnet-5", usage)
        cost_suffixed = cost_for("claude-sonnet-5-20260101[1m]", usage)
        self.assertAlmostEqual(cost_normalized, cost_suffixed)

    def test_missing_usage_fields_default_to_zero(self):
        self.assertEqual(cost_for("claude-sonnet-5", {}), 0.0)



# A synthetic price change: Sonnet 5 halves its rates on 2027-01-01. Patched in
# over the real table, so the tests don't depend on a real change ever happening.
_OLD = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write_5m": 2.50}
_NEW = {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write_5m": 1.25}
_SYNTHETIC = {
    "claude-sonnet-5": [("2026-10-02", _OLD), ("2027-01-01", _NEW)],
    "claude-opus-5": [("2026-10-02", {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25}),
                      ("2027-01-01", {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
}


def _price_change():
    return mock.patch.object(pricing, "_SCHEDULE", pricing._build_schedule(_SYNTHETIC))


def _today(day):
    return mock.patch.object(pricing, "_today", return_value=day)


def _rec(ts, **tokens):
    rec = {"model": "claude-sonnet-5", "ts": ts, "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_write_5m": 0, "cache_write_1h": 0,
           "cache_write_fallback": 0, "speed": None, "service_tier": None}
    rec.update(tokens)
    return rec


class TestDatedRates(unittest.TestCase):
    def test_request_before_the_change_keeps_the_old_rate(self):
        with _price_change():
            self.assertEqual(rates_for("claude-sonnet-5", ts="2026-12-31T23:59:59.999Z")["input"], 2.00)
            self.assertAlmostEqual(
                cost_for("claude-sonnet-5", {"input_tokens": 1_000_000}, ts="2026-12-31T23:59:59Z"), 2.00)

    def test_request_after_the_change_gets_the_new_rate(self):
        with _price_change():
            self.assertEqual(rates_for("claude-sonnet-5", ts="2027-01-01T00:00:00.000Z")["input"], 1.00)
            self.assertAlmostEqual(
                cost_for("claude-sonnet-5", {"input_tokens": 1_000_000}, ts="2027-03-01T12:00:00Z"), 1.00)

    def test_request_older_than_the_first_row_uses_the_first_row(self):
        with _price_change():
            self.assertEqual(rates_for("claude-sonnet-5", ts="2025-01-01T00:00:00Z")["input"], 2.00)

    def test_no_timestamp_means_todays_rates(self):
        with _price_change(), _today(datetime.date(2027, 2, 1)):
            self.assertEqual(rates_for("claude-sonnet-5")["input"], 1.00)
            self.assertEqual(rates_for("claude-sonnet-5", ts="not a date")["input"], 1.00)

    def test_a_row_dated_in_the_future_is_not_in_force_yet(self):
        # A price change is recorded ahead of the day it takes effect; until
        # then, undated requests and "today's rates" stay on the current row.
        with _price_change(), _today(datetime.date(2026, 11, 1)):
            self.assertEqual(rates_for("claude-sonnet-5")["input"], 2.00)
            self.assertEqual(rates_for("claude-sonnet-5", ts="not a date")["input"], 2.00)
            self.assertEqual(pricing.current_rates()["claude-sonnet-5"]["input"], 2.00)

    def test_todays_rates_switch_on_the_effective_day(self):
        with _price_change(), _today(datetime.date(2027, 1, 1)):
            self.assertEqual(pricing.current_rates()["claude-sonnet-5"]["input"], 1.00)

    def test_a_model_whose_only_row_is_future_dated_still_has_rates(self):
        future = {"claude-x-1": [("2999-01-01", _OLD)]}
        with mock.patch.object(pricing, "_SCHEDULE", pricing._build_schedule(future)):
            self.assertEqual(rates_for("claude-x-1")["input"], 2.00)
            self.assertEqual(pricing.current_rates()["claude-x-1"]["input"], 2.00)

    def test_offset_timestamps_are_dated_in_utc(self):
        with _price_change():
            # 2026-12-31 20:00 in New York is already 2027-01-01 in UTC.
            self.assertEqual(rates_for("claude-sonnet-5", ts="2026-12-31T20:00:00-05:00")["input"], 1.00)
            self.assertEqual(rates_for("claude-sonnet-5", ts="2026-12-31T18:00:00-05:00")["input"], 2.00)

    def test_1h_write_follows_the_dated_input_rate(self):
        with _price_change():
            self.assertEqual(rates_for("claude-sonnet-5", ts="2026-11-01T00:00:00Z")["cache_write_1h"], 4.00)
            self.assertEqual(rates_for("claude-sonnet-5", ts="2027-02-01T00:00:00Z")["cache_write_1h"], 2.00)

    def test_fast_mode_scales_the_dated_row(self):
        with _price_change():
            self.assertEqual(rates_for("claude-opus-5", "fast", ts="2026-11-01T00:00:00Z")["input"], 10.00)
            self.assertEqual(rates_for("claude-opus-5", "fast", ts="2027-02-01T00:00:00Z")["input"], 6.00)

    def test_batch_halves_the_dated_row(self):
        usage = {"input_tokens": 1_000_000, "service_tier": "batch"}
        with _price_change():
            self.assertAlmostEqual(cost_for("claude-sonnet-5", usage, ts="2026-11-01T00:00:00Z"), 1.00)
            self.assertAlmostEqual(cost_for("claude-sonnet-5", usage, ts="2027-02-01T00:00:00Z"), 0.50)


class TestAggregatePricesByRequestDate(unittest.TestCase):
    def test_request_cost_uses_the_rate_on_its_own_date(self):
        tokens = dict(input_tokens=1_000_000, output_tokens=1_000_000)
        with _price_change():
            self.assertAlmostEqual(aggregate._request_cost(_rec("2026-12-15T10:00:00Z", **tokens), set()), 12.00)
            self.assertAlmostEqual(aggregate._request_cost(_rec("2027-01-15T10:00:00Z", **tokens), set()), 6.00)

    def test_cache_saving_uses_the_rate_on_its_own_date(self):
        tokens = dict(cache_read_tokens=1_000_000, cache_write_5m=1_000_000)
        with _price_change():
            # Old: read saves 2.00-0.20, 5m write premium 2.50-2.00 -> 1.30.
            self.assertAlmostEqual(aggregate._request_cache_saving(_rec("2026-12-15T10:00:00Z", **tokens)), 1.30)
            # New: read saves 1.00-0.10, 5m write premium 1.25-1.00 -> 0.65.
            self.assertAlmostEqual(aggregate._request_cache_saving(_rec("2027-01-15T10:00:00Z", **tokens)), 0.65)


class TestStaleness(unittest.TestCase):
    def _checked(self):
        return datetime.date.fromisoformat(pricing.PRICES_CHECKED)

    def test_fresh_within_the_window(self):
        today = self._checked() + datetime.timedelta(days=pricing.PRICES_STALE_AFTER_DAYS)
        self.assertFalse(pricing.prices_stale(today))

    def test_stale_after_the_window(self):
        today = self._checked() + datetime.timedelta(days=pricing.PRICES_STALE_AFTER_DAYS + 1)
        self.assertTrue(pricing.prices_stale(today))
        self.assertEqual(pricing.prices_age_days(today), 91)

    def test_payload_flags_an_old_check(self):
        from tests.test_aggregate import _payload
        with mock.patch.object(pricing, "PRICES_CHECKED", "2020-01-01"):
            prices = _payload()["pricing"]
        self.assertTrue(prices["stale"])
        self.assertEqual(prices["checked"], "2020-01-01")
        self.assertGreater(prices["age_days"], 90)

    def test_collect_prints_the_warning(self):
        import collect
        msg = collect._stale_prices_message({"checked": "2020-01-01", "age_days": 1000})
        self.assertIn("claude.com/pricing", msg)
        self.assertIn("add a dated row", msg)


# Every dated row in pricing.RATE_HISTORY, frozen. A past rate is history: the
# usage it priced has already been reported and exported at it. When a price
# changes, append a new dated row to pricing.py -- then copy that row here.
PINNED_ROWS = {
    # cache_read corrected 0.20 -> 0.10 on 2026-10-07: the row was wrong from the
    # start (published rate is 0.05x input), not a price change.
    "claude-sonnet-5-5": [("2026-10-02", {"input": 2.00,  "output": 10.00, "cache_read": 0.10, "cache_write_5m": 2.50})],
    "claude-sonnet-5":   [("2026-10-02", {"input": 2.00,  "output": 10.00, "cache_read": 0.20, "cache_write_5m": 2.50})],
    "claude-sonnet-4-6": [("2026-10-02", {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-opus-5-5":   [("2026-10-02", {"input": 4.00,  "output": 20.00, "cache_read": 0.20, "cache_write_5m": 5.00})],
    "claude-opus-5":     [("2026-10-02", {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-8":   [("2026-10-02", {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-7":   [("2026-10-02", {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-opus-4-6":   [("2026-10-02", {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-fable-5-1":  [("2026-10-02", {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write_5m": 12.50})],
    "claude-fable-5":    [("2026-10-02", {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write_5m": 12.50})],
    "claude-mythos-5-1": [("2026-10-02", {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write_5m": 12.50})],
    "claude-mythos-5":   [("2026-10-02", {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write_5m": 12.50})],
    "claude-haiku-4-5":  [("2026-10-02", {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write_5m": 1.25})],
    "claude-opus-4-5":   [("2026-10-02", {"input": 5.00,  "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25})],
    "claude-sonnet-4-5": [("2026-10-02", {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-opus-4-1":   [("2026-10-02", {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write_5m": 18.75})],
    "claude-opus-4":     [("2026-10-02", {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write_5m": 18.75})],
    "claude-sonnet-4":   [("2026-10-02", {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75})],
    "claude-haiku-3-5":  [("2026-10-02", {"input": 0.80,  "output": 4.00,  "cache_read": 0.08, "cache_write_5m": 1.00})],
    "claude-haiku-5-5":  [("2026-10-09", {"input": 0.10,  "output": 0.50,  "cache_read": 0.01, "cache_write_5m": 0.125,
                                          "long_prompt": {"over": 100_000, "rates": {
                                              "input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write_5m": 0.625}}})],
}

_ADD_A_ROW = ("Past rates are history -- usage has already been reported and exported at them. "
              "Restore this row and append a new dated row to RATE_HISTORY in "
              "claude_usage/pricing.py instead (see docs/cost.md).")


class TestRateHistoryIsAppendOnly(unittest.TestCase):
    def test_no_past_row_was_edited_or_removed(self):
        for model, pinned in PINNED_ROWS.items():
            rows = dict(pricing.RATE_HISTORY.get(model, []))
            for day, rates in pinned:
                self.assertIn(day, rows,
                              f"{model}: the row effective {day} is gone. {_ADD_A_ROW}")
                self.assertEqual(rows[day], rates,
                                 f"{model}: the rate effective {day} was edited. {_ADD_A_ROW}")

    def test_every_row_is_pinned(self):
        for model, rows in pricing.RATE_HISTORY.items():
            pinned = dict(PINNED_ROWS.get(model, []))
            for day, rates in rows:
                self.assertEqual(pinned.get(day), rates,
                                 f"{model}: the row effective {day} isn't in PINNED_ROWS in "
                                 f"tests/test_pricing.py. Copy it there so it can't be edited later.")

    def test_rows_are_dated_and_in_order(self):
        for model, rows in pricing.RATE_HISTORY.items():
            days = [day for day, _ in rows]
            self.assertTrue(days, f"{model} has no rows")
            for day in days:
                self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$", model)
                datetime.date.fromisoformat(day)
            self.assertEqual(days, sorted(set(days)),
                             f"{model}: rows must be oldest first, one per date")

    def test_prices_checked_is_a_date(self):
        self.assertRegex(pricing.PRICES_CHECKED, r"^\d{4}-\d{2}-\d{2}$")
        datetime.date.fromisoformat(pricing.PRICES_CHECKED)


class TestNewlyPricedModels(unittest.TestCase):
    def test_current_sonnet_is_priced(self):
        # claude-sonnet-5-5 is the current Sonnet; missing it meant every
        # request on it was silently costed at $0.
        rates = rates_for("claude-sonnet-5-5")
        self.assertIsNotNone(rates)
        self.assertEqual(rates["input"], 2.00)
        self.assertEqual(rates["output"], 10.00)
        # 0.05x input, as published -- not the usual 0.1x (this read 0.20 until
        # the live price check caught it).
        self.assertEqual(rates["cache_read"], 0.10)

    def test_other_current_models_are_priced(self):
        for model in ("claude-sonnet-4-6", "claude-opus-4-7", "claude-opus-4-6",
                      "claude-mythos-5-1", "claude-fable-5-1", "claude-haiku-4-5"):
            self.assertIsNotNone(rates_for(model), f"{model} has no rates")

    def test_one_million_context_suffix_uses_base_rates(self):
        # The CLI labels the 1M-context mode "<model>[1m]" but bills it at the
        # standard rate, so the suffix must resolve to the same row.
        self.assertEqual(rates_for("claude-opus-5[1m]"), rates_for("claude-opus-5"))


class TestFastMode(unittest.TestCase):
    def test_fast_mode_doubles_the_rate(self):
        standard = rates_for("claude-opus-5-5")
        fast = rates_for("claude-opus-5-5", speed="fast")
        self.assertEqual(fast["input"], standard["input"] * 2)
        self.assertEqual(fast["output"], standard["output"] * 2)
        # Published fast-mode pricing for Opus 5.5 is $8/$40 per MTok.
        self.assertEqual((fast["input"], fast["output"]), (8.00, 40.00))

    def test_fast_mode_on_opus_5_matches_published_rate(self):
        fast = rates_for("claude-opus-5", speed="fast")
        self.assertEqual((fast["input"], fast["output"]), (10.00, 50.00))

    def test_speed_ignored_for_models_without_fast_mode(self):
        # Fast mode only exists on Opus 5.5 / 5 / 4.8.
        self.assertEqual(rates_for("claude-sonnet-5", speed="fast"),
                         rates_for("claude-sonnet-5"))

    def test_cost_honours_fast_speed(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        self.assertAlmostEqual(cost_for("claude-opus-5-5", usage), 4.00)
        self.assertAlmostEqual(cost_for("claude-opus-5-5", {**usage, "speed": "fast"}), 8.00)

    def test_standard_speed_is_not_surcharged(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "speed": "standard"}
        self.assertAlmostEqual(cost_for("claude-opus-5-5", usage), 4.00)


class TestBatchTier(unittest.TestCase):
    def test_batch_tier_is_half_price(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        full = cost_for("claude-opus-5-5", usage)
        batch = cost_for("claude-opus-5-5", {**usage, "service_tier": "batch"})
        self.assertAlmostEqual(batch, full / 2)

    def test_standard_tier_unaffected(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "service_tier": "standard"}
        self.assertAlmostEqual(cost_for("claude-opus-5-5", usage), 4.00)


class TestProviderModelIds(unittest.TestCase):
    """Bedrock, Vertex and the pre-4 "claude-3-5-haiku" ordering all name a
    model the table prices; unnormalised, each one cost $0."""

    def test_legacy_family_after_version_order(self):
        self.assertEqual(normalize_model("claude-3-5-haiku-20241022"), "claude-haiku-3-5")
        self.assertEqual(normalize_model("claude-3-7-sonnet-20250219"), "claude-sonnet-3-7")
        self.assertEqual(normalize_model("claude-3-opus-20240229"), "claude-opus-3")

    def test_bedrock_ids(self):
        cases = {
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
            "anthropic.claude-3-5-haiku-20241022-v1:0": "claude-haiku-3-5",
            "global.anthropic.claude-opus-4-1-20250805-v1:0": "claude-opus-4-1",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0[1m]": "claude-haiku-4-5",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
            "us.anthropic.claude-sonnet-4-20250514-v1:0": "claude-sonnet-4",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_model(raw), expected, raw)

    def test_vertex_ids(self):
        self.assertEqual(normalize_model("claude-sonnet-4-5@20250929"), "claude-sonnet-4-5")
        self.assertEqual(normalize_model("claude-3-5-haiku@20241022"), "claude-haiku-3-5")

    def test_provider_ids_are_priced(self):
        usage = {"input_tokens": 1_000_000}
        for raw in ("claude-3-5-haiku-20241022", "anthropic.claude-3-5-haiku-20241022-v1:0",
                    "claude-3-5-haiku@20241022"):
            self.assertAlmostEqual(cost_for(raw, usage), 0.80, msg=raw)

    def test_current_ids_unchanged(self):
        for name in ("claude-opus-5-5", "claude-fable-5-1", "claude-haiku-4-5", "<synthetic>"):
            self.assertEqual(normalize_model(name), name)

    # BUG-20: aliases and a hyphenated Bedrock prefix.
    def test_alias_ids_are_the_table_names(self):
        cases = {
            "claude-opus-4-0": "claude-opus-4",
            "claude-sonnet-4-0": "claude-sonnet-4",
            "claude-opus-4-0-20250514": "claude-opus-4",
            "claude-opus-4-1-latest": "claude-opus-4-1",
            "claude-sonnet-4-5-latest": "claude-sonnet-4-5",
            "claude-3-5-haiku-latest": "claude-haiku-3-5",
            "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
            "apac.anthropic.claude-sonnet-4-20250514-v1:0": "claude-sonnet-4",
            "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/"
            "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_model(raw), expected, raw)

    def test_alias_ids_are_priced(self):
        usage = {"input_tokens": 1_000_000}
        for raw, per_mtok in (("claude-opus-4-0", 15.0), ("claude-sonnet-4-0", 3.0),
                              ("claude-opus-4-1-latest", 15.0),
                              ("us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0", 3.0)):
            unknown = set()
            self.assertAlmostEqual(cost_for(raw, usage, unknown), per_mtok, msg=raw)
            self.assertEqual(unknown, set(), raw)

    def test_ids_the_table_has_no_row_for_stay_flagged(self):
        # Sonnet 3.7 is not on the live pricing page, so there is no row to map to;
        # an application-inference-profile ARN is an opaque id that names no model.
        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6"
        for raw in ("claude-3-7-sonnet-20250219", "claude-3-7-sonnet-latest", arn):
            unknown = set()
            self.assertEqual(cost_for(raw, {"input_tokens": 1_000_000}, unknown), 0.0, raw)
            self.assertEqual(unknown, {raw})


# Every raw form the dashboard has to recognise, with the table name it means.
# Run through both normalizers: the browser compares the model filter against
# names the Python side already normalized, so the two must agree exactly.
NORMALIZE_CASES = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-opus-5-5[1m]": "claude-opus-5-5",
    "claude-sonnet-5-20260101[1m]": "claude-sonnet-5",
    "claude-sonnet-5": "claude-sonnet-5",
    "": "",
    "claude-3-5-haiku-20241022": "claude-haiku-3-5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-3-7",
    "claude-3-opus-20240229": "claude-opus-3",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
    "anthropic.claude-3-5-haiku-20241022-v1:0": "claude-haiku-3-5",
    "global.anthropic.claude-opus-4-1-20250805-v1:0": "claude-opus-4-1",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0[1m]": "claude-haiku-4-5",
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
    "us.anthropic.claude-sonnet-4-20250514-v1:0": "claude-sonnet-4",
    "claude-sonnet-4-5@20250929": "claude-sonnet-4-5",
    "claude-3-5-haiku@20241022": "claude-haiku-3-5",
    "claude-opus-5-5": "claude-opus-5-5",
    "claude-fable-5-1": "claude-fable-5-1",
    "<synthetic>": "<synthetic>",
    "claude-opus-4-0": "claude-opus-4",
    "claude-sonnet-4-0": "claude-sonnet-4",
    "claude-opus-4-0-20250514": "claude-opus-4",
    "claude-opus-4-1-latest": "claude-opus-4-1",
    "claude-3-5-haiku-latest": "claude-haiku-3-5",
    "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6": "a1b2c3d4e5f6",
}


class TestNormalizeModelInBothLanguages(unittest.TestCase):
    """static/app.js normalizeModel() mirrors pricing.normalize_model(); when
    they disagree, filtering by a Bedrock or Vertex model empties the
    reconciliation table."""

    def test_python(self):
        for raw, expected in NORMALIZE_CASES.items():
            self.assertEqual(normalize_model(raw), expected, raw)

    def test_javascript_matches_python(self):
        from tests.jsrun import run_js
        raws = list(NORMALIZE_CASES)
        got = run_js(["static/habits.js", "static/app.js"], f"out({json.dumps(raws)}.map(normalizeModel))")
        self.assertEqual(dict(zip(raws, got)), NORMALIZE_CASES)


class TestHaiku55PromptLengthPricing(unittest.TestCase):
    """Haiku 5.5 is priced by prompt length: a request whose prompt (input +
    cache reads + cache writes) is over 100,000 tokens pays 5x on every
    category, output included."""

    def test_short_prompt_rates(self):
        rates = rates_for("claude-haiku-5-5")
        self.assertEqual((rates["input"], rates["output"], rates["cache_read"],
                          rates["cache_write_5m"], rates["cache_write_1h"]),
                         (0.10, 0.50, 0.01, 0.125, 0.20))

    def test_long_prompt_rates(self):
        rates = rates_for("claude-haiku-5-5", prompt_tokens=100_001)
        self.assertEqual((rates["input"], rates["output"], rates["cache_read"],
                          rates["cache_write_5m"], rates["cache_write_1h"]),
                         (0.50, 2.50, 0.05, 0.625, 1.00))

    def test_exactly_the_threshold_is_still_short(self):
        self.assertEqual(rates_for("claude-haiku-5-5", prompt_tokens=100_000)["input"], 0.10)

    def test_dated_suffix_is_priced(self):
        self.assertEqual(rates_for("claude-haiku-5-5-20261001")["input"], 0.10)

    def test_cost_counts_cache_reads_and_writes_toward_the_prompt(self):
        # 1k fresh input + 99.5k cache read + 1k cache write = 101.5k: long.
        usage = {"input_tokens": 1_000, "cache_read_input_tokens": 99_500,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1_000},
                 "output_tokens": 1_000_000}
        expected = (1_000 * 0.50 + 99_500 * 0.05 + 1_000 * 0.625) / 1e6 + 2.50
        self.assertAlmostEqual(cost_for("claude-haiku-5-5", usage), expected)

    def test_cost_short_prompt(self):
        usage = {"input_tokens": 50_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(cost_for("claude-haiku-5-5", usage), 50_000 * 0.10 / 1e6 + 0.50)

    def test_flat_cache_write_total_counts_toward_the_prompt(self):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 200_000}
        self.assertAlmostEqual(cost_for("claude-haiku-5-5", usage), (10 * 0.50 + 200_000 * 0.625) / 1e6)

    def test_aggregate_prices_the_long_tier(self):
        rec = _rec(None, input_tokens=150_000, output_tokens=1_000_000)
        rec["model"] = "claude-haiku-5-5"
        self.assertAlmostEqual(aggregate._request_cost(rec, set()), 150_000 * 0.50 / 1e6 + 2.50)

    def test_cache_saving_uses_the_long_tier(self):
        rec = _rec(None, cache_read_tokens=1_000_000)
        rec["model"] = "claude-haiku-5-5"
        self.assertAlmostEqual(aggregate._request_cache_saving(rec), 0.50 - 0.05)

    def test_other_models_ignore_prompt_length(self):
        self.assertEqual(rates_for("claude-opus-5-5", prompt_tokens=900_000), rates_for("claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
