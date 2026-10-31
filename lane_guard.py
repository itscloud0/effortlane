"""Cost guards for the lanes arm of a Claude Code session.

reload    The first prompt after the prompt cache expired re-sends the whole history at
          full input price. Above a size threshold, block that prompt once and say what it
          will cost; sending it again goes through.
subagent  A subagent launched without its own model inherits the session's model. Give
          general-purpose subagents a cheaper default; explicit choices are kept.

Both read only usage numbers and timestamps from the local transcript, never text.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import lane

TAIL_BYTES = 512 * 1024
GENERIC_AGENTS = ("", "general-purpose", "claude")


def settings(config: dict) -> dict:
    guards = config.get("guards")
    return guards if isinstance(guards, dict) else {}


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_request(transcript: str | Path) -> dict | None:
    """Size, model, time and cache TTL of the newest model request in a transcript."""
    try:
        with open(transcript, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    found = None
    ttl = None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        usage = message.get("usage") if isinstance(message, dict) else None
        model = str(message.get("model") or "") if isinstance(message, dict) else ""
        if not isinstance(usage, dict) or model.startswith("<"):
            continue
        cache = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        if ttl is None and (cache.get("ephemeral_1h_input_tokens") or 0) > 0:
            ttl = 3600
        elif ttl is None and (cache.get("ephemeral_5m_input_tokens") or 0) > 0:
            ttl = 300
        if found is None:
            stamp = _ts(row.get("timestamp"))
            if stamp is None:
                continue
            found = {"ts": stamp, "model": model,
                     "tokens": sum(usage.get(k) or 0 for k in
                                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))}
        if found and ttl:
            break
    if found:
        found["ttl"] = ttl or 3600  # Claude Code subscriptions write 1 h caches
    return found


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def reload_check(event: dict, config: dict, now: float | None = None) -> tuple[str, dict] | None:
    """Block message and log fields when this prompt would reload an expired large cache."""
    threshold = settings(config).get("reload_tokens")
    transcript = event.get("transcript_path")
    if not isinstance(threshold, int) or threshold <= 0 or not transcript:
        return None
    last = last_request(transcript)
    now = time.time() if now is None else now
    if not last or last["tokens"] < threshold or now - last["ts"] <= last["ttl"]:
        return None
    session = lane.session_digest(str(event.get("session_id") or ""))
    marks_path = lane.state_dir() / "guard-reload.json"
    try:
        marks = json.loads(marks_path.read_text())
    except (OSError, ValueError):
        marks = {}
    if marks.get(session) == last["ts"]:
        return None  # warned already; the user sent it again
    marks[session] = last["ts"]
    lane._atomic_write(marks_path, json.dumps(dict(list(marks.items())[-200:])), 0o600)
    import lane_claude
    rates = lane_claude.rates(last["model"])
    tokens = last["tokens"]
    cost = ""
    if rates:
        reload = tokens * rates[0] * (2 if last["ttl"] >= 3600 else 1.25) / 1e6
        step = tokens * rates[2] / 1e6
        cost = f" ≈ ${reload:.2f} at API prices, as much as ~{round(reload / step)} normal steps"
    message = (f"Effortlane: this session's cache expired (idle {_duration(now - last['ts'])}). "
               f"Sending now re-reads {tokens // 1000}k tokens at full price{cost}.\n"
               "- Send the same prompt again to continue anyway (it is shown below).\n"
               "- Cheapest: /clear and restate the task in a few lines.\n"
               "- /compact pays this reload once, then every later step is cheaper.")
    return message, {"tokens": tokens, "idle_s": round(now - last["ts"]), "ttl": last["ttl"]}


def pretool(stdin=None, stdout=None) -> int:
    """PreToolUse hook for Agent/Task: default a generic subagent to a cheaper model."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        event = json.load(stdin)
        tool_input = event.get("tool_input")
        if event.get("tool_name") not in ("Agent", "Task") or not isinstance(tool_input, dict):
            return 0
        model = settings(lane.load_config()).get("subagent_model")
        kind = str(tool_input.get("subagent_type") or "")
        if not model or tool_input.get("model") or kind not in GENERIC_AGENTS:
            return 0
        lane.log_route({"event": "guard", "kind": "subagent", "agent": "claude", "to": model,
                        "session": lane.session_digest(str(event.get("session_id") or ""))})
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"effortlane: generic subagent defaults to {model}",
            "updatedInput": dict(tool_input, model=model),
        }}, stdout)
    except Exception:
        pass
    return 0
