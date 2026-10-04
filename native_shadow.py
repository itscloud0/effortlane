"""Observe native Codex through documented hooks; never change executor state."""
from __future__ import annotations

import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import time

from core import Router, sanitize_task

OWNER = "Effortlane native Shadow"
MAX_EVENT = 30_000


def definition(root: Path) -> dict:
    from manage import python_executable
    command = shlex.join([str(python_executable()), str(root / "native_shadow.py"), "hook", "--root", str(root)])
    return {"hooks": [{"type": "command", "command": command, "async": True,
                       "timeout": 4, "statusMessage": OWNER}]}


def settings(root: Path) -> dict:
    try:
        return json.loads((root / "native-shadow-hook.json").read_text())
    except (OSError, ValueError):
        return {}


def configure(root: Path, enabled: bool, path: Path | None = None) -> dict:
    from manage import atomic_write, load_json, write_json
    path = path or Path(load_json(root / "manifest.json")["config_path"]).parent / "hooks.json"
    with (root / "native-shadow-settings.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = path.read_bytes() if path.exists() else None
        if previous is not None and len(previous) > 1_000_000:
            raise ValueError("Codex hooks file too large; refusing to modify")
        document = json.loads(previous) if previous else {"hooks": {}}
        if not isinstance(document, dict):
            raise ValueError("Invalid Codex hooks document; refusing to modify")
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("Invalid Codex hooks table; refusing to modify")
        groups = hooks.get("UserPromptSubmit", [])
        if not isinstance(groups, list):
            raise ValueError("Invalid UserPromptSubmit hooks; refusing to modify")
        expected = definition(root)
        old = settings(root)
        previous_command = old.get("definition", expected)["hooks"][0]["command"]
        owned = [group for group in groups if isinstance(group, dict) and any(
            isinstance(handler, dict) and (handler.get("statusMessage") == OWNER
                                         or handler.get("command") == previous_command)
            for handler in group.get("hooks", []))]
        if owned and (len(owned) != 1 or owned[0] != old.get("definition", expected)):
            raise ValueError("Effortlane native hook was manually modified; refusing to overwrite")
        if enabled and old.get("enabled") and owned == [expected]:
            return {"changed": False, "enabled": True, "trust": "check /hooks"}
        if not enabled and not owned:
            return {"changed": False, "enabled": False}
        backup = root / "backups" / ("native-shadow-" + str(time.time_ns()))
        backup.mkdir(mode=0o700, parents=True)
        if previous is not None:
            atomic_write(backup / "hooks.json", previous)
        if (root / "native-shadow-hook.json").exists():
            atomic_write(backup / "settings.json", (root / "native-shadow-hook.json").read_bytes())
        hooks["UserPromptSubmit"] = [group for group in groups if group not in owned]
        if enabled:
            hooks["UserPromptSubmit"].append(expected)
        if not hooks["UserPromptSubmit"]:
            del hooks["UserPromptSubmit"]
        # Preserve other hooks and settings; refuse concurrent foreign edits.
        if (path.read_bytes() if path.exists() else None) != previous:
            raise ValueError("Codex hooks changed during installation; retry")
        write_json(path, document)
        write_json(root / "native-shadow-hook.json", {"enabled": enabled, "path": str(path),
                                                       "definition": expected})
        return {"changed": True, "enabled": enabled, "backup": str(backup),
                "trust": "Review Effortlane native Shadow in Codex /hooks" if enabled else None}


def observe(root: Path, event: dict, router=None) -> dict | None:
    if not settings(root).get("enabled") or os.environ.get("EFFORTLANE_ROUTED") == "1":
        return None
    if event.get("hook_event_name") != "UserPromptSubmit":
        return None
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or len(prompt.encode()) > MAX_EVENT:
        return None
    # Reject arbitrary pasted context before touching routing credentials.
    if re.search(r"<\s*/?\s*pasted_content\b", prompt, re.I):
        return None
    task, uncertain = sanitize_task(prompt)
    if not task or uncertain:
        return None
    model = event.get("model")
    actual = model if isinstance(model, str) and re.fullmatch(r"gpt-[a-zA-Z0-9.\-]{1,80}", model) else "unknown"
    session = event.get("session_id")
    identity = session[:1024] if isinstance(session, str) else None
    router = router or Router(root / "config.json", root / "native-models.json",
                              root / "state/native-shadow-leases.json",
                              root / "state/native-shadow-routing.jsonl")
    payload = {"model": "effortlane-shadow", "input": [{"role": "user", "content": task}]}
    decision = router.decide(payload, client="native_hook", session_id=identity, mode_override="shadow")
    record = {"event": "native_shadow_proposal", "schema_version": 1, "ts": int(time.time()),
              "actual_model": actual, "actual_effort": "unknown", "usage_scope": "proposal_only",
              "proposed_model": decision.get("proposed_model"), "proposed_effort": decision.get("proposed_effort"),
              "reason": decision.get("reason"), "jev_latency_ms": decision.get("jev_ms", 0)}
    if identity:
        record["session_hash"] = hashlib.sha256(identity.encode()).hexdigest()[:32]
    router._write_record(record)
    return record


def hook(root: Path, stream=None) -> int:
    try:
        raw = (stream or sys.stdin).read(MAX_EVENT + 1)
        if len(raw.encode()) <= MAX_EVENT:
            event = json.loads(raw)
            if isinstance(event, dict):
                observe(root, event)
    except Exception:
        pass
    return 0


def report(root: Path) -> dict:
    models, efforts, actual = collections.Counter(), collections.Counter(), collections.Counter()
    count = 0
    # Read bounded lines, including rotated files, without retaining prompts.
    for path in (root / "state").glob("native-shadow-routing*.jsonl"):
        with path.open() as stream:
            while line := stream.readline(16_385):
                if len(line) > 16_384:
                    raise ValueError("Oversized native Shadow telemetry row")
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("event") != "native_shadow_proposal":
                    continue
                count += 1
                models[row.get("proposed_model") or "none"] += 1
                efforts[row.get("proposed_effort") or "none"] += 1
                actual[row.get("actual_model") or "unknown"] += 1
    return {"enabled": settings(root).get("enabled", False), "observations": count,
            "proposed_models": dict(models), "proposed_efforts": dict(efforts), "actual_models": dict(actual),
            "actual_effort": "not supplied by documented hook", "savings": "not measured",
            "trust": "Native Codex /hooks controls trust; installation alone does not activate the hook"}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1:3] != ["hook", "--root"]:
        raise SystemExit("usage: native_shadow.py hook --root ROOT")
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
    signal.setitimer(signal.ITIMER_REAL, 3)
    raise SystemExit(hook(Path(sys.argv[3])))
