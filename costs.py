"""Published rate-card sensitivity estimates for privacy-safe router telemetry.

These rates are not a statement of ChatGPT subscription debits or Jev billing.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import re


CODEX_CREDITS_URL = "https://learn.chatgpt.com/docs/pricing"
API_PRICES_URL = "https://developers.openai.com/api/docs/pricing"
# Standard, short-context published rates per million tokens, 2026-09-30.
# Tuple order: uncached input, cached input, output.
CODEX_CREDITS = {
    "gpt-6-luna": (2.5, 0.25, 12.5),
    "gpt-6.1-sol": (50, 2.5, 250),
    "gpt-6-sol": (50, 5, 250),
    "gpt-6-astra": (250, 25, 1250),
    "gpt-5.6-luna": (5, 0.5, 30),
    "gpt-5.6-terra": (50, 5, 300),
    "gpt-5.6-sol": (100, 10, 500),
}
API_USD = {
    # Published Standard short-context rate, verified 2026-10-06.
    "gpt-6.1-sol": (2, 0.10, 10),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-sol": (2, 0.20, 10),
    "gpt-6-astra": (10, 1, 50),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-terra": (2, 0.20, 12),
    "gpt-5.6-sol": (4, 0.40, 20),
}
JEV_PUBLISHED_INPUT_USD_PER_M = 0.042
JEV_RATE_URL = "https://typesafe.ai/blog/introducing-system-one-models-and-jev"


def _tokens(row: dict) -> tuple[int, int, int] | None:
    if row.get("cached_input_observed") is False:
        return None
    values = [row.get(key) for key in ("input_tokens", "cached_input_tokens", "output_tokens")]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        return None
    input_tokens, cached, output = values
    if cached > input_tokens:
        return None
    return input_tokens - cached, cached, output


def _charge(tokens: tuple[int, int, int], rates: tuple[float, float, float]) -> float:
    return sum(amount * rate for amount, rate in zip(tokens, rates)) / 1_000_000


def _view(rows: list[dict]) -> dict:
    known = [row for row in rows if _tokens(row) is not None and row.get("model") in CODEX_CREDITS]
    totals = tuple(sum(_tokens(row)[i] for row in known) for i in range(3))
    actual_credits = sum(_charge(_tokens(row), CODEX_CREDITS[row["model"]]) for row in known)
    sol_credits = _charge(totals, CODEX_CREDITS["gpt-6-sol"])
    sol_6_1_credits = _charge(totals, CODEX_CREDITS["gpt-6.1-sol"])
    astra_credits = _charge(totals, CODEX_CREDITS["gpt-6-astra"])
    api_known = [row for row in known if row["model"] in API_USD]
    api_tokens = tuple(sum(_tokens(row)[i] for row in api_known) for i in range(3))
    api_actual = sum(_charge(_tokens(row), API_USD[row["model"]]) for row in api_known)
    return {
        "calls": len(rows), "priced_calls": len(known), "unpriced_calls": len(rows) - len(known),
        "cache_metadata": {
            "observed_calls": sum(row.get("cached_input_observed") is True for row in rows),
            "missing_calls": sum(row.get("cached_input_observed") is False for row in rows),
            "legacy_unknown_calls": sum("cached_input_observed" not in row for row in rows),
        },
        "tokens": {"uncached_input": totals[0], "cached_input": totals[1], "output": totals[2]},
        "codex_credit_equivalent": {
            "observed_mix": round(actual_credits, 4),
            "all_sol_same_tokens": round(sol_credits, 4),
            "all_sol_6_1_same_tokens": round(sol_6_1_credits, 4),
            "all_astra_same_tokens": round(astra_credits, 4),
            "vs_sol": round(sol_credits - actual_credits, 4),
            "vs_sol_6_1": round(sol_6_1_credits - actual_credits, 4),
            "vs_astra": round(astra_credits - actual_credits, 4),
        },
        "api_usd_equivalent": {
            "priced_calls": len(api_known), "unpriced_calls": len(rows) - len(api_known),
            "observed_mix": round(api_actual, 4),
            "all_sol_same_tokens": round(_charge(api_tokens, API_USD["gpt-6-sol"]), 4),
            "all_astra_same_tokens": round(_charge(api_tokens, API_USD["gpt-6-astra"]), 4),
        },
    }


def cost_report(rows: list[dict], hours: int = 168, since: float | None = None) -> dict:
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 720:
        raise ValueError("hours must be between 1 and 720")
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    if since is not None:
        if not isinstance(since, (int, float)) or isinstance(since, bool) or since < 0:
            raise ValueError("since must be a Unix timestamp")
        cutoff = max(cutoff, since)
    rows = [row for row in rows if isinstance(row.get("ts"), (int, float)) and row["ts"] >= cutoff]
    routes = {row["route_id"]: row for row in rows
              if row.get("event") == "route" and row.get("mode") in ("auto", "shadow")
              and isinstance(row.get("route_id"), str) and re.fullmatch(r"[a-f0-9]{24}", row["route_id"])}
    auto_routes = {key: row for key, row in routes.items() if row["mode"] == "auto"}
    shadow_routes = {key: row for key, row in routes.items() if row["mode"] == "shadow"}
    usage = [row for row in rows if row.get("event") == "usage"]
    linked = []
    shadow_linked = []
    clients = defaultdict(list)
    for row in usage:
        route = routes.get(row.get("route_id"))
        native_cli_launch = (route and route.get("client") == "cli" and row.get("mode") == "native"
                             and isinstance(route.get("turn_hash"), str) and bool(route["turn_hash"])
                             and route["turn_hash"] == row.get("turn_hash"))
        if (route and (row.get("mode") == route.get("mode") or native_cli_launch)
                and all(route.get(key) == row.get(key) for key in ("session", "client", "model", "effort"))):
            if route["mode"] == "auto":
                linked.append(row)
                clients[str(row.get("client"))].append(row)
            else:
                shadow_linked.append(row)
    linked_ids = {id(row) for row in linked}
    # A pre-routed `codex exec` sends its concrete model to native Codex, so the
    # gateway marks that exact request native. The route ID and turn hash prove
    # it belongs to Auto; unrelated concrete-model calls remain excluded.
    auto_usage = [row for row in usage if row.get("mode") == "auto" or id(row) in linked_ids]
    late_cli = [row for row in usage if row.get("mode") == "auto" and row.get("client") == "cli"
                and id(row) not in linked_ids]
    late_turns = {(row.get("session"), row.get("turn_hash")) for row in late_cli
                  if isinstance(row.get("session"), str) and row.get("session")
                  and isinstance(row.get("turn_hash"), str) and row.get("turn_hash")}
    jev_routes = [row for row in rows if row.get("event") == "route" and row.get("mode") in ("auto", "shadow")]
    metered = [row for row in jev_routes if isinstance(row.get("jev_input_tokens"), int) and isinstance(row.get("jev_output_tokens"), int)]
    return {
        "window_hours": hours, "since": cutoff,
        "usage_scopes": dict(Counter(row.get("usage_scope") if row.get("usage_scope") in
                                     ("model_call", "last_model_call", "turn_total") else "unknown" for row in usage)),
        "usage_units_note": "Legacy calls fields count usage records. Records can cover one call, a last-call sample, or a native-thread turn total. Historical incomplete coverage cannot establish task cost or subscription savings.",
        "auto": {"route_decisions": len(auto_routes), "linked_calls": len(linked),
                 "unlinked_decisions": len(auto_routes) - len({row.get("route_id") for row in linked}),
                 "observed_all_auto": _view(auto_usage),
                 "unlinked_auto_calls": len(auto_usage) - len(linked),
                 "all_clients": _view(linked), "by_client": {key: _view(value) for key, value in sorted(clients.items())},
                 "unlinked_cli_gateway": {
                     "distinct_session_turns": len(late_turns),
                     "calls_without_turn_hash": sum(not row.get("session") or not row.get("turn_hash") for row in late_cli),
                     "by_model_calls": dict(Counter(str(row.get("model") or "unknown") for row in late_cli)),
                     "by_reason_calls": dict(Counter(str(row.get("reason") or "unknown") for row in late_cli)),
                     "blocked_proposals": dict(Counter(str(row.get("proposed_model") or "unknown") for row in late_cli
                                                       if row.get("reason") == "requires_native_model_selection")),
                     "observed": _view(late_cli),
                     "note": "Native CLI TUI/resume reaches the gateway after model setup. Calls are not independent user turns; distinct session/turn hashes are a lower-bound grouping. This path cannot safely change the executor model."}},
        "shadow": {"route_decisions": len(shadow_routes), "linked_calls": len(shadow_linked),
                   "unlinked_decisions": len(shadow_routes) - len({row.get("route_id") for row in shadow_linked}),
                   "observed_executor": _view(shadow_linked),
                   "note": "Shadow proposals were not executed. Native CLI calls are linked only by exact route, session, turn, model, and effort; this is observed Sol baseline usage, not achieved routing savings."},
        "observed_all_modes": _view(usage),
        "jev": {"route_decisions": len(jev_routes), "metered_requests": len(metered),
                "input_tokens": sum(row["jev_input_tokens"] for row in metered),
                "output_tokens": sum(row["jev_output_tokens"] for row in metered),
                "published_rate_estimate_usd": round(sum(row["jev_input_tokens"] for row in metered)
                                                     * JEV_PUBLISHED_INPUT_USD_PER_M / 1_000_000, 8),
                "published_rate_source": JEV_RATE_URL,
                "cost_usd": None, "cost_reason": "No TypeSafe account billing export verified; some old decisions did not record Jev tokens."},
        "sources": {"codex_credits": CODEX_CREDITS_URL, "api_usd": API_PRICES_URL,
                    "sol_6_1_api_rate_checked": "2026-10-06",
                    "rate_card_checked": "2026-09-30", "tier": "standard_short_context"},
        "limits": "Same-token counterfactual, not quality-equivalent savings. Desktop may record only the last model call of a turn. Pro included usage is not a dollar charge; API rates are comparison units only. Fast/long-context rates are not applied.",
    }


def format_savings(report: dict) -> str:
    """Human-readable evidence and same-token counterfactual, without a savings claim."""
    auto = report["auto"]
    view = auto["all_clients"]
    all_auto = auto["observed_all_auto"]
    all_auto_credits = all_auto["codex_credit_equivalent"]
    credits = view["codex_credit_equivalent"]
    api = view["api_usd_equivalent"]
    jev = report["jev"]
    late = auto["unlinked_cli_gateway"]
    priced = view["priced_calls"]
    sol = credits["all_sol_same_tokens"]
    pct = 100 * credits["vs_sol"] / sol if sol else None
    all_sol = all_auto_credits["all_sol_same_tokens"]
    all_pct = 100 * all_auto_credits["vs_sol"] / all_sol if all_sol else None
    all_sol_6_1 = all_auto_credits["all_sol_6_1_same_tokens"]
    all_pct_6_1 = 100 * all_auto_credits["vs_sol_6_1"] / all_sol_6_1 if all_sol_6_1 else None
    period = (dt.datetime.fromtimestamp(report["since"], dt.timezone.utc).isoformat()
              if report.get("since") is not None else f"last {report['window_hours']} hours")
    lines = [
        f"Period: {period}",
        f"Auto-attributed: {all_auto['calls']} usage records | linked to pre-turn route: {auto['linked_calls']} | unlinked (routing unproven): {auto['unlinked_auto_calls']}",
        report.get("usage_units_note", "Historical usage scope may be incomplete."),
        (f"Auto-attributed same-token credit-equivalent: observed mix {all_auto_credits['observed_mix']:.4f} vs all-GPT-6-Sol {all_sol:.4f}; difference {all_auto_credits['vs_sol']:+.4f} ({all_pct:+.2f}%)"
         if all_pct is not None else "Auto-attributed same-token credit-equivalent: unavailable (no priced calls)"),
        (f"Current GPT-6.1-Sol rate sensitivity, same tokens: all-6.1-Sol {all_sol_6_1:.4f}; difference {all_auto_credits['vs_sol_6_1']:+.4f} ({all_pct_6_1:+.2f}%). Not a historically available or quality-matched baseline."
         if all_pct_6_1 is not None else "Current GPT-6.1-Sol rate sensitivity: unavailable (no priced calls)"),
        f"Pre-turn routes: {auto['route_decisions']} | linked records: {auto['linked_calls']} | priced: {priced} | missing usage/rate: {view['unpriced_calls']}",
        (f"Shadow baseline: {report['shadow']['route_decisions']} decisions | {report['shadow']['linked_calls']} linked Sol records | "
         f"{report['shadow']['observed_executor']['tokens']['uncached_input']} uncached / "
         f"{report['shadow']['observed_executor']['tokens']['cached_input']} cached input / "
         f"{report['shadow']['observed_executor']['tokens']['output']} output tokens; proposals did not execute"),
        f"Unlinked CLI gateway: {late['distinct_session_turns']} turns, {late['observed']['calls']} calls (included above; excluded from linked subset)",
        (f"Cache detail provenance: {all_auto['cache_metadata']['observed_calls']} observed, "
         f"{all_auto['cache_metadata']['missing_calls']} missing, "
         f"{all_auto['cache_metadata']['legacy_unknown_calls']} legacy calls with unverified cache-detail provenance"),
        (f"Linked pre-turn subset, credit-equivalent: routed {credits['observed_mix']:.4f} vs all-Sol {sol:.4f} vs all-Astra {credits['all_astra_same_tokens']:.4f}"
         if priced else "Linked pre-turn subset, credit-equivalent: unavailable (no priced linked calls)"),
        f"Linked subset difference vs Sol: {credits['vs_sol']:+.4f} ({pct:+.1f}%)" if pct is not None else
            "Linked subset difference vs Sol: unavailable (no priced linked calls)",
        (f"Linked subset API price-equivalent: routed ${api['observed_mix']:.4f} vs all-Sol ${api['all_sol_same_tokens']:.4f} vs all-Astra ${api['all_astra_same_tokens']:.4f} (not billed API spend)"
         if api["priced_calls"] else "API price-equivalent: unavailable (no priced linked calls)"),
        f"Jev (Auto + Shadow): {jev['metered_requests']} metered decisions, {jev['input_tokens']} input / {jev['output_tokens']} output tokens; actual bill unknown",
        f"Jev published token-rate estimate: ${jev['published_rate_estimate_usd']:.8f} (not account billing)",
        "Limit: same observed tokens, not a paired quality-equivalent comparison or measured Pro allowance savings.",
    ]
    return "\n".join(lines)
