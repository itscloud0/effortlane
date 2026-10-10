import time
import unittest

from costs import cost_report, format_savings


class CostReportTests(unittest.TestCase):
    def test_only_exact_auto_receipts_enter_auto_counterfactual(self):
        ts = int(time.time())
        route = {"event": "route", "ts": ts, "route_id": "a" * 24,
                 "mode": "auto", "session": "b" * 24, "client": "cli",
                 "model": "gpt-6-luna", "effort": "low",
                 "jev_input_tokens": 300, "jev_output_tokens": 20}
        usage = {"event": "usage", "ts": ts, "route_id": route["route_id"],
                 "mode": "auto", "session": route["session"], "client": "cli", "model": "gpt-6-luna",
                 "effort": "low", "input_tokens": 1_000_000,
                 "cached_input_tokens": 500_000, "output_tokens": 100_000}
        unrelated = {**usage, "route_id": "c" * 24, "model": "gpt-6-sol"}
        concrete = {**usage, "mode": "native", "model": "gpt-6-luna"}
        result = cost_report([route, usage, unrelated, concrete])
        self.assertEqual(result["auto"]["linked_calls"], 1)
        credits = result["auto"]["all_clients"]["codex_credit_equivalent"]
        self.assertEqual(credits["observed_mix"], 2.625)
        self.assertEqual(credits["all_sol_same_tokens"], 52.5)
        self.assertEqual(credits["all_sol_6_1_same_tokens"], 51.25)
        self.assertEqual(credits["vs_sol"], 49.875)
        self.assertEqual(result["jev"]["input_tokens"], 300)
        self.assertEqual(result["jev"]["published_rate_estimate_usd"], 0.0000126)
        self.assertIn("typesafe.ai", result["jev"]["published_rate_source"])
        self.assertIsNone(result["jev"]["cost_usd"])
        self.assertEqual(result["observed_all_modes"]["calls"], 3)
        self.assertEqual(result["auto"]["observed_all_auto"]["calls"], 2)

    def test_unlinked_interactive_cli_turns_are_visible_separately(self):
        ts = int(time.time())
        base = {"event": "usage", "ts": ts, "mode": "auto", "client": "cli",
                "session": "a" * 24, "model": "gpt-6-sol", "effort": "medium",
                "input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}
        rows = [{**base, "turn_hash": "b" * 24, "reason": "jev"},
                {**base, "turn_hash": "b" * 24, "reason": "lease"},
                {**base, "turn_hash": "c" * 24, "reason": "requires_native_model_selection",
                 "proposed_model": "gpt-6-luna"}]
        result = cost_report(rows)
        self.assertEqual(result["auto"]["linked_calls"], 0)
        late = result["auto"]["unlinked_cli_gateway"]
        self.assertEqual(late["distinct_session_turns"], 2)
        self.assertEqual(late["by_model_calls"], {"gpt-6-sol": 3})
        self.assertEqual(late["blocked_proposals"], {"gpt-6-luna": 1})

    def test_preturn_cli_exec_concrete_usage_is_auto_only_for_same_turn(self):
        ts = int(time.time())
        route = {"event": "route", "ts": ts, "route_id": "a" * 24,
                 "mode": "auto", "session": "b" * 24, "client": "cli",
                 "model": "gpt-6-luna", "effort": "low", "turn_hash": "c" * 24}
        usage = {"event": "usage", "ts": ts, "route_id": route["route_id"],
                 "mode": "native", "reason": "concrete_model", "session": route["session"],
                 "client": "cli", "model": route["model"], "effort": "low",
                 "turn_hash": route["turn_hash"], "input_tokens": 100,
                 "cached_input_tokens": 0, "output_tokens": 10}
        side_call = {**usage, "turn_hash": "d" * 24}
        result = cost_report([route, usage, side_call])
        self.assertEqual(result["auto"]["linked_calls"], 1)
        self.assertEqual(result["auto"]["observed_all_auto"]["calls"], 1)
        self.assertEqual(result["auto"]["unlinked_auto_calls"], 0)

    def test_shadow_cli_exec_native_usage_is_baseline_only_for_exact_turn(self):
        ts = int(time.time())
        route = {"event": "route", "ts": ts, "route_id": "a" * 24,
                 "mode": "shadow", "session": "b" * 24, "client": "cli",
                 "model": "gpt-6.1-sol", "effort": "high", "turn_hash": "c" * 24,
                 "proposed_model": "gpt-6-luna", "proposed_effort": "low"}
        usage = {"event": "usage", "ts": ts, "route_id": route["route_id"],
                 "mode": "native", "session": route["session"], "client": "cli",
                 "model": route["model"], "effort": route["effort"],
                 "turn_hash": route["turn_hash"], "input_tokens": 100,
                 "cached_input_tokens": 80, "output_tokens": 10}
        wrong_turn = {**usage, "turn_hash": "d" * 24}
        wrong_effort = {**usage, "effort": "medium"}
        result = cost_report([route, usage, wrong_turn, wrong_effort])
        self.assertEqual(result["shadow"]["route_decisions"], 1)
        self.assertEqual(result["shadow"]["linked_calls"], 1)
        self.assertEqual(result["shadow"]["observed_executor"]["tokens"],
                         {"uncached_input": 20, "cached_input": 80, "output": 10})
        self.assertEqual(result["auto"]["linked_calls"], 0)
        self.assertEqual(result["auto"]["observed_all_auto"]["calls"], 0)
        self.assertIn("Shadow baseline: 1 decisions | 1 linked Sol records", format_savings(result))

    def test_summary_leads_with_all_observed_auto_not_optimistic_linked_subset(self):
        ts = int(time.time())
        route = {"event": "route", "ts": ts, "route_id": "a" * 24,
                 "mode": "auto", "session": "b" * 24, "client": "cli",
                 "model": "gpt-6-luna", "effort": "low"}
        luna = {"event": "usage", "ts": ts, "route_id": route["route_id"],
                "mode": "auto", "session": route["session"], "client": "cli",
                "model": "gpt-6-luna", "effort": "low", "input_tokens": 1_000_000,
                "cached_input_tokens": 0, "output_tokens": 0}
        sol = {**luna, "route_id": None, "model": "gpt-6-sol"}
        result = cost_report([route, luna, *[sol for _ in range(99)]])
        self.assertEqual(result["auto"]["linked_calls"], 1)
        self.assertEqual(result["auto"]["unlinked_auto_calls"], 99)
        self.assertEqual(result["auto"]["observed_all_auto"]["calls"], 100)
        summary = format_savings(result)
        self.assertIn("Auto-attributed: 100 usage records | linked to pre-turn route: 1 | unlinked (routing unproven): 99", summary)
        self.assertIn("Auto-attributed same-token credit-equivalent: observed mix 4952.5000 vs all-GPT-6-Sol 5000.0000; difference +47.5000 (+0.95%)", summary)
        self.assertIn("all-6.1-Sol 5000.0000", summary)
        self.assertIn("Linked subset difference vs Sol: +47.5000 (+95.0%)", summary)

    def test_six_point_one_sol_published_api_rates_include_cache_without_double_charging_reasoning(self):
        now = int(time.time())
        usage = {"event": "usage", "ts": now, "mode": "auto", "client": "cli",
                 "model": "gpt-6.1-sol", "input_tokens": 1_000_000,
                 "cached_input_tokens": 800_000, "output_tokens": 100_000,
                 "reasoning_output_tokens": 90_000}
        view = cost_report([usage])["auto"]["observed_all_auto"]
        self.assertEqual(view["priced_calls"], 1)
        self.assertEqual(view["codex_credit_equivalent"]["observed_mix"], 37)
        self.assertEqual(view["api_usd_equivalent"]["unpriced_calls"], 0)
        self.assertEqual(view["api_usd_equivalent"]["observed_mix"], 1.48)

    def test_missing_cache_detail_is_unpriced_not_assumed_zero(self):
        now = int(time.time())
        row = {"event": "usage", "ts": now, "mode": "auto", "model": "gpt-6-sol",
               "input_tokens": 1_000, "cached_input_tokens": 0, "output_tokens": 100,
               "cached_input_observed": False}
        view = cost_report([row])["auto"]["observed_all_auto"]
        self.assertEqual((view["priced_calls"], view["unpriced_calls"]), (0, 1))
        self.assertEqual(view["cache_metadata"]["missing_calls"], 1)

    def test_missing_tokens_and_old_jev_usage_are_explicit(self):
        ts = int(time.time())
        rows = [{"event": "route", "ts": ts, "mode": "shadow"},
                {"event": "usage", "ts": ts, "model": "gpt-6-sol",
                 "input_tokens": None, "cached_input_tokens": None, "output_tokens": None}]
        result = cost_report(rows)
        self.assertEqual(result["jev"]["metered_requests"], 0)
        self.assertEqual(result["observed_all_modes"]["unpriced_calls"], 1)
        with self.assertRaises(ValueError):
            cost_report(rows, 0)

    def test_since_excludes_older_routes_and_empty_window_has_no_savings_claim(self):
        ts = int(time.time())
        old = {"event": "route", "ts": ts - 3600, "mode": "auto", "route_id": "a" * 24}
        report = cost_report([old], hours=24, since=ts - 60)
        self.assertEqual(report["auto"]["route_decisions"], 0)
        summary = format_savings(report)
        self.assertIn("no priced linked calls", summary)
        self.assertIn("actual bill unknown", summary)
        with self.assertRaises(ValueError):
            cost_report([], since=-1)


if __name__ == "__main__":
    unittest.main()
