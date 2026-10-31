"""Cost guards for the lanes arm of a Claude Code session.

reload    The first prompt after the prompt cache expired re-sends the whole history at
          full input price. Above a size threshold, block that prompt once and say what it
          will cost; sending it again goes through.
subagent  A subagent launched without its own model inherits the session's model. Give
          general-purpose subagents Sonnet and the read-only Explore agent Haiku;
          explicit choices and named agents are kept.
context   Above a size, the status line shows what every step re-sends and how much
          cheaper /clear would make a new task.

All of them read only usage numbers and timestamps, never text.
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
    marks_path, marks = _read_marks("guard-reload.json")
    if marks.get(session) == last["ts"]:
        return None  # warned already; the user sent it again
    marks[session] = last["ts"]
    _write_marks(marks_path, marks)
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


def _read_marks(name: str) -> tuple[Path, dict]:
    path = lane.state_dir() / name
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError):
        return path, {}


def _write_marks(path: Path, marks: dict) -> None:
    lane._atomic_write(path, json.dumps(dict(list(marks.items())[-200:])), 0o600)


def start_tokens(transcript: str | Path) -> int | None:
    """Context size of a transcript's first request: what a fresh session starts with."""
    try:
        with open(transcript, "rb") as handle:
            for _, raw in zip(range(400), handle):
                if b'"usage"' in raw:
                    usage = (json.loads(raw).get("message") or {}).get("usage") or {}
                    return sum(usage.get(k) or 0 for k in
                               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    except (OSError, ValueError, AttributeError):
        pass
    return None


def context_note(event: dict, config: dict) -> str | None:
    """Status line note for a large session: what each step re-sends, and what /clear saves."""
    threshold = settings(config).get("context_note_tokens")
    tokens = (event.get("context_window") or {}).get("total_input_tokens")
    if not isinstance(threshold, int) or threshold <= 0 or not isinstance(tokens, int) or tokens < threshold:
        return None
    fresh = max(start_tokens(event.get("transcript_path") or "") or 40_000, 20_000)
    return (f"effortlane: {tokens // 1000}k re-sent every step · new task? /clear starts near "
            f"{fresh // 1000}k (~{max(2, round(tokens / fresh))}x cheaper)")


def pretool(stdin=None, stdout=None) -> int:
    """PreToolUse hook for Agent/Task: default a generic subagent to a cheaper model."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        event = json.load(stdin)
        tool_input = event.get("tool_input")
        if event.get("tool_name") not in ("Agent", "Task") or not isinstance(tool_input, dict):
            return 0
        guards = settings(lane.load_config())
        kind = str(tool_input.get("subagent_type") or "")
        model = guards.get("explore_model") if kind == "Explore" else \
            guards.get("subagent_model") if kind in GENERIC_AGENTS else None
        if not model or tool_input.get("model"):
            return 0
        lane.log_route({"event": "guard", "kind": "subagent", "agent": "claude", "to": model,
                        "session": lane.session_digest(str(event.get("session_id") or ""))})
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"effortlane: {kind or 'generic'} subagent defaults to {model}",
            "updatedInput": dict(tool_input, model=model),
        }}, stdout)
    except Exception:
        pass
    return 0
