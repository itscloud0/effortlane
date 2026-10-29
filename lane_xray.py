"""`lane xray`: where Claude Code tokens go, and what would cut them.

Reads only usage numbers, timestamps, tool names and output sizes from Claude Code's
local transcripts (~/.claude/projects, subagents included). Nothing is stored or sent.
Costs are API list prices: they rank the levers, they are not a subscription bill.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import lane
import lane_claude
from lane_guard import _ts

CHEAPER = "claude-sonnet-5-5"
LONG_CONTEXT = 150_000


def _split(model: str, usage: dict) -> dict[str, float] | None:
    """Cost of one request by part: input, output, cache read, cache write."""
    rates = lane_claude.rates(model)
    if not rates:
        return None
    inp, out, read = rates
    total = lane_claude.price(model, usage) or 0.0
    parts = {"input": (usage.get("input_tokens") or 0) * inp / 1e6,
             "output": (usage.get("output_tokens") or 0) * out / 1e6,
             "read": (usage.get("cache_read_input_tokens") or 0) * read / 1e6}
    parts["write"] = max(0.0, total - sum(parts.values()))
    return parts


def _quantile(values: list[int], q: float) -> int:
    values = sorted(values)
    return values[int(q * (len(values) - 1))] if values else 0


def scan(days: float, root: Path | None = None) -> dict:
    since = time.time() - days * 86400
    projects = root or lane.claude_dir() / "projects"
    files = [(p, False) for p in projects.glob("*/*.jsonl")] + \
            [(p, True) for p in projects.glob("*/*/subagents/*.jsonl")]
    seen: set = set()
    t: dict[str, Any] = {"cost": 0.0, "requests": 0, "parts": dict.fromkeys(("input", "output", "read", "write"), 0.0),
                         "main": 0.0, "sub": 0.0, "by_model": {}, "contexts": [], "starts": [],
                         "reload": {"requests": 0, "cost": 0.0, "excess": 0.0},
                         "sub_expensive": 0.0, "sub_cheaper": 0.0, "main_expensive": 0.0, "main_cheaper": 0.0,
                         "overhead_cost": 0.0, "long_cost": 0.0, "tools": {}, "images": 0, "sessions": {}}
    for path, is_sub in files:
        try:
            if path.stat().st_mtime < since:
                continue
            handle = open(path, errors="replace")
        except OSError:
            continue
        session = path.parent.parent.name if is_sub else path.stem
        names: dict[str, str] = {}
        start = prev = None
        with handle:
            for line in handle:
                if '"usage"' not in line and '"tool_' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") if isinstance(row, dict) else None
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = str(block.get("name") or "?")
                        names[block.get("id")] = "mcp:" + name.split("__")[1] if name.startswith("mcp__") else name
                    elif block.get("type") == "tool_result":
                        tool = names.get(block.get("tool_use_id"), "?")
                        inner = block.get("content")
                        items = inner if isinstance(inner, list) else [{"type": "text", "text": inner or ""}]
                        for item in items:
                            if isinstance(item, dict) and item.get("type") == "image":
                                t["images"] += 1
                            elif isinstance(item, dict):
                                t["tools"][tool] = t["tools"].get(tool, 0) + len(str(item.get("text") or "")) // 4
                usage = message.get("usage")
                model = str(message.get("model") or "")
                stamp = _ts(row.get("timestamp"))
                if not isinstance(usage, dict) or model.startswith("<") or stamp is None:
                    continue
                key = row.get("requestId") or message.get("id")
                if key in seen:
                    continue
                seen.add(key)
                parts = _split(model, usage)
                gap, prev = (stamp - prev if prev else None), stamp
                read = usage.get("cache_read_input_tokens") or 0
                write = usage.get("cache_creation_input_tokens") or 0
                context = read + write + (usage.get("input_tokens") or 0)
                first = start is None
                if first:
                    start = context
                if stamp < since or parts is None:
                    continue
                cost = sum(parts.values())
                t["cost"] += cost
                t["requests"] += 1
                for k, v in parts.items():
                    t["parts"][k] += v
                t["sub" if is_sub else "main"] += cost
                t["by_model"][model] = t["by_model"].get(model, 0.0) + cost
                t["sessions"][session] = t["sessions"].get(session, 0.0) + cost
                if context >= LONG_CONTEXT:
                    t["long_cost"] += cost
                cheaper = lane_claude.price(CHEAPER, usage) or 0.0
                if "opus" in model or "fable" in model:
                    side = "sub" if is_sub else "main"
                    t[f"{side}_expensive"] += cost
                    t[f"{side}_cheaper"] += cheaper
                if not is_sub:
                    t["contexts"].append(context)
                    if first:
                        t["starts"].append(context)
                    t["overhead_cost"] += min(start, context) * lane_claude.rates(model)[2] / 1e6
                if not first and context > 30_000 and write > 0.5 * context and gap and gap > 300:
                    t["reload"]["requests"] += 1
                    t["reload"]["cost"] += cost
                    t["reload"]["excess"] += max(0.0, parts["write"] - write * lane_claude.rates(model)[2] / 1e6)
    costs = sorted(t["sessions"].values(), reverse=True)
    t["top_decile_share"] = sum(costs[:max(1, len(costs) // 10)]) / t["cost"] if t["cost"] else 0.0
    t["session_count"] = len(costs)
    return t


def xray(days: float = 7, as_json: bool = False) -> int:
    t = scan(days)
    if as_json:
        t = dict(t, contexts={"p50": _quantile(t["contexts"], .5), "p90": _quantile(t["contexts"], .9)},
                 starts={"p50": _quantile(t["starts"], .5)}, sessions=len(t["sessions"]))
        print(json.dumps(t, indent=2, default=round))
        return 0
    if not t["requests"]:
        print(f"No Claude Code requests in the last {days:g} days under {lane.claude_dir() / 'projects'}.")
        return 0
    total = t["cost"]
    pct = lambda x: f"{100 * x / total:.0f}%"  # noqa: E731
    money = lambda x: f"${x:,.0f}" if x >= 10 else f"${x:,.2f}"  # noqa: E731
    p = t["parts"]
    print(f"Claude Code, last {days:g} days: {money(total)} at API prices · "
          f"{t['requests']:,} requests · {t['session_count']} sessions\n")
    print("Where it goes")
    for label, key in (("re-reading history (cache read)", "read"), ("writing to cache", "write"),
                       ("answers (output)", "output"), ("new input", "input")):
        print(f"  {label:<34}{pct(p[key]):>5}  {money(p[key])}")
    models = ", ".join(f"{m.replace('claude-', '')} {pct(c)}" for m, c in
                       sorted(t["by_model"].items(), key=lambda kv: -kv[1]) if c / total >= 0.005)
    print(f"  main sessions {pct(t['main'])} · subagents {pct(t['sub'])} · {models}\n")
    print("What fills each request")
    print(f"  typical request                 {_quantile(t['contexts'], .5) // 1000}k tokens "
          f"(top 10%: {_quantile(t['contexts'], .9) // 1000}k+)")
    print(f"  loaded before your first prompt {_quantile(t['starts'], .5) // 1000}k tokens "
          "(system prompt, tools, skills, MCP, CLAUDE.md)")
    tools = sorted(t["tools"].items(), key=lambda kv: -kv[1])[:5]
    print("  tool output                     " + ", ".join(f"{k} {v / 1e6:.1f}M" for k, v in tools)
          + f" tokens · {t['images']:,} images")
    print(f"  biggest 10% of sessions         {100 * t['top_decile_share']:.0f}% of the cost\n")
    print("What would cut it (same tokens repriced; a cheaper model may take more steps)")
    levers = []
    if t["sub_expensive"]:
        levers.append((t["sub_expensive"] - t["sub_cheaper"],
                       f"Subagents on Opus cost {money(t['sub_expensive'])}; on Sonnet the same tokens cost "
                       f"{money(t['sub_cheaper'])}.", "lanes guard: generic subagents default to Sonnet"))
    if t["reload"]["requests"]:
        levers.append((t["reload"]["excess"],
                       f"{t['reload']['requests']} cache reloads after breaks cost {money(t['reload']['cost'])}, "
                       f"{money(t['reload']['excess'])} of it extra.",
                       "lanes guard: warns once before a big reload; /clear or a new session per task"))
    if t["main_expensive"]:
        levers.append((t["main_expensive"] - t["main_cheaper"],
                       f"Main sessions on Opus cost {money(t['main_expensive'])}; on Sonnet the same tokens cost "
                       f"{money(t['main_cheaper'])}.", "lanes: Sonnet main, Opus `architect` for the hard part"))
    levers.append((t["overhead_cost"] / 2,
                   f"Re-reading the start-up load costs {money(t['overhead_cost'])}; halving it saves the amount shown.",
                   "turn off skills, plugins and MCP servers a project doesn't use"))
    levers.append((t["long_cost"] * 0.3,
                   f"Requests above {LONG_CONTEXT // 1000}k tokens cost {money(t['long_cost'])} ({pct(t['long_cost'])}).",
                   "one task per session; /compact past ~150k (estimate: about a third of that)"))
    for i, (saving, what, how) in enumerate(sorted(levers, key=lambda x: -x[0]), 1):
        print(f"  {i}. up to {money(saving)}  {what}\n     -> {how}")
    print("\nRead locally from Claude Code transcripts; nothing is stored or sent. "
          "`lane trial report` measures what the lanes arm actually saves.")
    return 0
