import time
import unittest

from metrics import metrics_report


class MetricsReportTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.route = {"event": "route", "ts": self.now, "route_id": "a" * 24,
                      "session": "b" * 24, "client": "desktop", "model": "gpt-6-sol",
                      "effort": "high", "proposed_model": "gpt-6-luna", "proposed_effort": "medium",
                      "reason": "fallback", "jev_ms": 3, "router_ms": 5}

    def test_cache_guard_projection_counts_are_not_cost_savings(self):
        rows = [{**self.route, 'cache_guard_basis': 'projection', 'reason': 'cache_hysteresis'},
                {**self.route, 'cache_guard_basis': 'projection', 'reason': 'jev'},
                {**self.route, 'cache_guard_basis': 'heuristic'},
                {**self.route, 'cache_guard_basis': 'secret-text'}]
        view = metrics_report(rows)['routes']['cache_guard']
        self.assertEqual(view['bases'], {'projection': 2, 'heuristic': 1})
        self.assertEqual(view['projected_holds'], 1)
        self.assertNotIn('saved_usd', view)

    def test_linked_tokens_and_switches_have_explicit_coverage(self):
        route = {**self.route, "switched": True}
        usage = {**route, "event": "usage", "input_tokens": 100, "cached_input_tokens": 75,
                 "output_tokens": 10, "cached_input_observed": True, "manual_override": True}
        report = metrics_report([route, usage])
        self.assertEqual(report["routes"]["model_switches"], 1)
        self.assertEqual(report["usage"]["token_totals"],
                         {"input_tokens": 100, "cached_input_tokens": 75, "output_tokens": 10})
        self.assertEqual(report["usage"]["token_observation_counts"]["cached_input_tokens"], 1)
        self.assertEqual(report["usage"]["weak_quality_signals"], {"manual_override": 1})

    def test_subscription_windows_are_isolated_and_negative_delta_is_not_savings(self):
        rows = [
            {"event": "subscription", "ts": self.now - 20, "limit_id": "codex", "plan_type": "pro",
             "primary": {"used_percent": 70, "window_minutes": 300, "resets_at": 1000}, "secondary": None},
            {"event": "subscription", "ts": self.now - 10, "limit_id": "codex", "plan_type": "pro",
             "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 1000}, "secondary": None},
            {"event": "subscription", "ts": self.now, "limit_id": "codex", "plan_type": "pro",
             "primary": {"used_percent": 20, "window_minutes": 10080, "resets_at": 2000}, "secondary": None},
        ]
        report = metrics_report(rows)
        groups = report["subscriptions"]["groups"]
        self.assertEqual(len(groups), 2)
        reset = next(group for group in groups if group["window_minutes"] == 300)
        self.assertEqual((reset["window_minutes"], reset["observations"], reset["delta_percentage_points"]), (300, 2, -60))
        self.assertTrue(reset["reset_or_correction_observed"])
        self.assertIsNone(report["subscription_allowance_savings"])

    def test_subscription_deduplicates_account_snapshot_and_accepts_hashed_quota(self):
        snapshot = {"event": "subscription", "ts": self.now, "limit_id": "quota-" + "c" * 24,
                    "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1000}}
        report = metrics_report([snapshot, {**snapshot, "client": "cli"}])
        group = report["subscriptions"]["groups"][0]
        self.assertEqual((group["limit_id"], group["observations"]), ("quota-" + "c" * 24, 1))

    def test_subscription_accepts_native_model_limit_and_detects_intermediate_reset(self):
        rows = [
            {"event": "subscription", "ts": self.now - 3, "limit_id": "gpt-6.1-sol", "plan_type": "enterprise",
             "primary": {"used_percent": 10, "window_minutes": 60, "resets_at": 42}},
            {"event": "subscription", "ts": self.now - 2, "limit_id": "gpt-6.1-sol", "plan_type": "private-plan",
             "primary": {"used_percent": 5, "window_minutes": 60, "resets_at": 42}},
            {"event": "subscription", "ts": self.now - 1, "limit_id": "gpt-6.1-sol", "plan_type": "enterprise",
             "primary": {"used_percent": 12, "window_minutes": 60, "resets_at": 42}},
        ]
        report = metrics_report(rows)
        self.assertTrue(report["subscriptions"]["groups"][0]["reset_or_correction_observed"])
        self.assertEqual(report["subscriptions"]["plan_type_observations"], {"enterprise": 2})

    def test_malformed_link_values_do_not_crash_or_link(self):
        bad_route = {**self.route, "session": {"private": "value"}}
        bad_usage = {"event": "usage", "ts": self.now, "route_id": "a" * 24, "session": [],
                     "client": "desktop", "model": "gpt-6-sol", "effort": "high", "mode": "native"}
        report = metrics_report([bad_route, bad_usage])
        self.assertEqual(report["usage"]["linked_calls"], 0)
        self.assertEqual(report["usage"]["unlinked_native_calls_excluded_from_routed_claims"], 1)
        self.assertEqual(report["routes"]["executed_pairs"], {"gpt-6-sol/high": 1})

    def test_subscription_groups_are_capped_and_omission_is_disclosed(self):
        rows = [
            {"event": "subscription", "ts": self.now, "limit_id": "codex", "plan_type": "pro",
             "primary": {"used_percent": 1, "window_minutes": 1, "resets_at": reset}}
            for reset in range(0, 201 * 11, 11)
        ]
        subscriptions = metrics_report(rows)["subscriptions"]
        self.assertEqual((len(subscriptions["groups"]), subscriptions["omitted_groups"]), (200, 1))
        self.assertEqual(subscriptions["groups"][0]["resets_at"], 2200)

    def test_subscription_coalesces_small_reset_timestamp_jitter(self):
        rows = [
            {"event": "subscription", "ts": self.now - 2, "limit_id": "codex",
             "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 1000}},
            {"event": "subscription", "ts": self.now - 1, "limit_id": "codex",
             "primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 1009}},
        ]
        group = metrics_report(rows)["subscriptions"]["groups"][0]
        self.assertEqual((group["observations"], group["reset_at_min"], group["reset_at_max"]), (2, 1000, 1009))
        self.assertEqual(group["reset_tolerance_seconds"], 10)

    def test_subscription_reset_groups_do_not_chain_or_cross_real_boundary(self):
        rows = [
            {"event": "subscription", "ts": self.now - 3, "limit_id": "codex",
             "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 1000}},
            {"event": "subscription", "ts": self.now - 2, "limit_id": "codex",
             "primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 1009}},
            {"event": "subscription", "ts": self.now - 1, "limit_id": "codex",
             "primary": {"used_percent": 5, "window_minutes": 300, "resets_at": 1018}},
        ]
        groups = metrics_report(rows)["subscriptions"]["groups"]
        self.assertEqual(sorted(group["observations"] for group in groups), [1, 2])
        self.assertEqual(sorted((group["reset_at_min"], group["reset_at_max"]) for group in groups), [(1000, 1009), (1018, 1018)])

    def test_subscription_flags_conflicting_same_timestamp_samples_without_counting_duplicates(self):
        snapshot = {"event": "subscription", "ts": self.now, "limit_id": "codex",
                    "primary": {"used_percent": 30, "window_minutes": 300, "resets_at": 1000}}
        lower = {**snapshot, "primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 1000}}
        group = metrics_report([snapshot, snapshot, lower])["subscriptions"]["groups"][0]
        self.assertEqual(group["observations"], 2)
        self.assertTrue(group["ambiguous_same_timestamp_samples"])
        self.assertTrue(group["decreasing_same_timestamp_samples"])
        self.assertIsNone(group["delta_percentage_points"])
        self.assertIsNone(group["first"]["used_percent"])
        self.assertEqual(group["first"]["used_percent_range"], {"min": 20, "max": 30})

    def test_subscription_separates_samples_on_opposite_sides_of_reset_epoch(self):
        reset = int(self.now) - 10
        rows = [
            {"event": "subscription", "ts": reset - 2, "limit_id": "codex",
             "primary": {"used_percent": 90, "window_minutes": 300, "resets_at": reset}},
            {"event": "subscription", "ts": reset + 2, "limit_id": "codex",
             "primary": {"used_percent": 2, "window_minutes": 300, "resets_at": reset}},
        ]
        groups = metrics_report(rows)["subscriptions"]["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(group["observations"] for group in groups), [1, 1])

    def test_only_exactly_linked_usage_contributes_metrics_and_cache_coverage(self):
        linked = {"event": "usage", "ts": self.now, "route_id": "a" * 24, "session": "b" * 24,
                  "client": "desktop", "model": "gpt-6-sol", "effort": "high", "status": "error",
                  "cache_observed": True, "input_tokens": 100, "cached_input_tokens": 40,
                  "turn_duration_ms": 20, "first_response_ms": 5, "tool_calls": 2, "command_failures": 1,
                  "compactions": 1, "reasoning_output_tokens": 9}
        native = {**linked, "route_id": "c" * 24, "mode": "native", "turn_duration_ms": 999}
        report = metrics_report([self.route, linked, native])
        usage = report["usage"]
        self.assertEqual((usage["linked_calls"], usage["unlinked_native_calls_excluded_from_routed_claims"]), (1, 1))
        self.assertEqual(usage["statuses"], {"error": 1})
        self.assertEqual(usage["cache"], {"observed_calls": 1, "missing_coverage_calls": 0, "cached_input_ratio": 0.4})
        self.assertEqual(usage["turn_metrics"]["totals"]["turn_duration_ms"], 20)
        self.assertEqual(usage["turn_metrics"]["totals"]["reasoning_output_tokens"], 9)
        self.assertEqual(report["routes"]["latency_ms"]["jev"], {"observations": 1, "p50": 3, "p95": 3})

    def test_usage_reports_complete_turn_totals_separately_from_fallback_scope(self):
        complete = {**self.route, "event": "usage", "usage_scope": "turn_total",
                    "usage_coverage_reason": "cumulative_delta", "input_tokens": 100,
                    "cached_input_tokens": 20, "cached_input_observed": True, "output_tokens": 30}
        fallback = {**self.route, "event": "usage", "usage_scope": "last_model_call",
                    "usage_coverage_reason": "total_missing", "input_tokens": 4, "output_tokens": 2}
        usage = metrics_report([self.route, complete, fallback])["usage"]
        self.assertEqual(usage["complete_turns"], 1)
        self.assertTrue(usage["overall_totals_may_mix_usage_scopes"])
        self.assertEqual(usage["token_totals"], {"input_tokens": 104, "cached_input_tokens": 20, "output_tokens": 32})
        self.assertEqual(usage["token_totals_by_scope"]["turn_total"],
                         {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30})
        self.assertEqual(usage["cache_by_scope"]["turn_total"]["cached_input_ratio"], 0.2)
        self.assertEqual(usage["usage_coverage_reasons"], {"cumulative_delta": 1, "total_missing": 1})

    def test_usage_ignores_invalid_coverage_metadata_and_missing_token_fields(self):
        usage = {**self.route, "event": "usage", "usage_scope": "secret-scope",
                 "usage_coverage_reason": "private-detail", "input_tokens": -1,
                 "cached_input_tokens": "not-a-count", "output_tokens": None}
        report = metrics_report([self.route, usage])
        usage_report = report["usage"]
        self.assertEqual(usage_report["usage_scope"], {"unknown": 1})
        self.assertEqual(usage_report["usage_coverage_reasons"], {})
        self.assertEqual(usage_report["token_observation_counts"],
                         {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        self.assertNotIn("secret-scope", str(report))
        self.assertNotIn("private-detail", str(report))

    def test_usage_metadata_containers_do_not_crash_or_leak(self):
        usage = {**self.route, "event": "usage", "usage_scope": ["turn_total"],
                 "usage_coverage_reason": {"reason": "cumulative_delta"}}
        report = metrics_report([self.route, usage])
        self.assertEqual(report["usage"]["usage_scope"], {"unknown": 1})
        self.assertEqual(report["usage"]["usage_coverage_reasons"], {})
        self.assertNotIn("cumulative_delta", str(report))

    def test_complete_turns_require_reliable_complete_usage(self):
        valid = {**self.route, "event": "usage", "usage_scope": "turn_total",
                 "usage_coverage_reason": "cumulative_delta", "input_tokens": 10, "output_tokens": 1}
        missing_output = {**valid, "output_tokens": None}
        incoherent_cache = {**valid, "cached_input_observed": True, "cached_input_tokens": 11}
        fallback_reason = {**valid, "usage_coverage_reason": "total_missing"}
        report = metrics_report([self.route, valid, missing_output, incoherent_cache, fallback_reason])
        self.assertEqual(report["usage"]["complete_turns"], 1)

    def test_malformed_input_is_skipped_without_leaking_or_counting_it(self):
        malformed_subscription = {"event": "subscription", "ts": self.now, "limit_id": "not-safe",
                                  "primary": {"used_percent": 101, "window_minutes": 1, "resets_at": 1}}
        report = metrics_report([None, {"event": "route"}, malformed_subscription, self.route])
        self.assertEqual(report["window"]["malformed_rows_skipped"], 2)
        self.assertEqual(report["subscriptions"]["groups"], [])
        self.assertEqual(report["routes"]["reasons"], {"fallback": 1})
        self.assertNotIn("not-safe", str(report))
        with self.assertRaises(ValueError):
            metrics_report({}, hours=1)
        with self.assertRaises(ValueError):
            metrics_report([], hours=0)


if __name__ == "__main__":
    unittest.main()
