"""Seamless Claude Code launch, randomized lane trial and exact usage report.

`claude` (a shell function installed by `lane install`) runs `lane.py wrap claude`,
which execs the real Claude Code with the lane harness injected through its own
`--settings` and `--agents` flags. Nothing under ~/.claude is edited.

Trial arms, assigned per new session:
  control  your normal Claude setup; Jev only records what it would pick
  lanes    Sonnet start, scout/builder/architect subagents, Jev delegation hints

The report joins sessions to Claude Code transcripts for exact per-request token
usage (subagents included), prices it per model, and adds the account 5h/7d limit
percentages captured through the status line.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import lane

ARMS = ("control", "lanes")
MODES = ("trial", "lanes", "shadow", "off")
MODE_FLAGS = {"--effortlane-trial": "trial", "--effortlane-auto": "lanes",
              "--effortlane-shadow": "shadow", "--effortlane-off": "off"}
SUBCOMMANDS = {"mcp", "config", "update", "upgrade", "doctor", "install", "setup-token",
               "migrate-installer", "plugin", "plugins", "agents", "auth", "remote-control"}
PASSTHROUGH = {"-v", "--version", "-h", "--help", "-p", "--print", "--settings", "--agents"}
RESUME = {"-r", "--resume", "-c", "--continue"}
MANUAL = ("--model", "--effort")
MIN_SESSIONS = 20

# USD per million tokens: input, output, cache read. Cache writes: 1.25x input (5 min
# TTL), 2x input (1 h TTL). First-party API list prices, 2026-10; edit when they change.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


def session_hash(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:12]


def _state(name: str) -> Path:
    return lane.state_dir() / name


def _rows(name: str) -> Iterable[dict]:
    try:
        lines = _state(name).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(name: str, record: dict) -> None:
    try:
        path = _state(name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a") as handle:
            handle.write(json.dumps(dict(record, ts=round(time.time(), 3)), sort_keys=True) + "\n")
    except OSError:
        pass


# --- trial state ---------------------------------------------------------------------

def trial() -> dict:
    try:
        data = json.loads(_state("trial.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def trial_active(now: float | None = None) -> bool:
    data = trial()
    now = time.time() if now is None else now
    return bool(data.get("started")) and not data.get("stopped") and now < data.get("ends", 0)


def trial_start(days: float) -> int:
    now = time.time()
    data = {"started": now, "ends": now + days * 86400, "ratio": 0.5, "price_table": "2026-10"}
    lane._atomic_write(_state("trial.json"), json.dumps(data, indent=2) + "\n", 0o600)
    print(f"Trial running for {days:g} days. Each new `claude` session is randomly control or lanes.\n"
          "`lane trial status` any time; `lane trial report` for the numbers.")
    return 0


def trial_stop() -> int:
    data = trial()
    if not data:
        print("No trial.")
        return 0
    data["stopped"] = time.time()
    lane._atomic_write(_state("trial.json"), json.dumps(data, indent=2) + "\n", 0o600)
    print("Trial stopped; new sessions use the configured mode. The data stays for `lane trial report`.")
    return 0


def trial_status() -> int:
    data = trial()
    if not data:
        print("No trial. `lane trial start` begins one (14 days by default).")
        return 0
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))  # noqa: E731
    state = "running" if trial_active() else "finished"
    print(f"Trial {state}: {fmt(data['started'])} -> {fmt(data.get('stopped') or data['ends'])}")
    counts = {arm: [0, 0] for arm in ARMS + ("manual", "unassigned")}
    arms = {}
    for row in _rows("sessions.jsonl"):
        if row.get("ts", 0) >= data["started"] and not row.get("resumed") and row.get("arm") in counts:
            counts[row["arm"]][0] += 1
            arms[row.get("session")] = row["arm"]
    for row in _rows("routes.jsonl"):
        if row.get("event") == "hook" and row.get("session") in arms:
            counts[arms[row["session"]]][1] += 1
    for arm, (sessions, prompts) in counts.items():
        if sessions:
            print(f"  {arm:<11} {sessions:>4} sessions {prompts:>6} prompts")
    need = max(0, MIN_SESSIONS - min(counts["control"][0], counts["lanes"][0]))
    print(f"  {'about ' + str(need) + ' more sessions per arm before a verdict' if need else 'enough sessions for a verdict'}"
          " - `lane trial report`")
    return 0


def resolve_mode(flag: str | None, config: dict) -> str:
    if flag:
        return flag
    if trial_active():
        return "trial"
    mode = config.get("claude_mode", "shadow")
    return mode if mode in MODES else "shadow"


# --- launch --------------------------------------------------------------------------

def _python() -> str:
    return sys.executable or "python3"


def _lane_path() -> str:
    return str(Path(lane.__file__).resolve())


def agents_json() -> dict:
    """Bundled subagent files as Claude's --agents JSON."""
    result = {}
    for path in sorted(lane.BUNDLED_AGENTS.glob("*.md")):
        _, head, body = path.read_text().split("---", 2)
        meta = {}
        for line in head.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        agent: dict[str, Any] = {"description": meta["description"], "prompt": body.strip()}
        for key in ("tools", "disallowedTools"):
            if meta.get(key):
                agent[key] = [t.strip() for t in meta[key].split(",") if t.strip()]
        for key in ("model", "effort"):
            if meta.get(key):
                agent[key] = meta[key]
        result[meta["name"]] = agent
    return result


def settings_json(mode: str) -> dict:
    command = f"{shlex.quote(_python())} {shlex.quote(_lane_path())}"
    hook = {"type": "command", "command": f"{command} hook --mode {mode}", "timeout": 5}
    if mode == "shadow":
        hook["async"] = True  # recording only: never delay the prompt
    settings = {
        "hooks": {"UserPromptSubmit": [{"hooks": [hook]}]},
        "statusLine": {"type": "command", "command": f"{command} statusline"},
    }
    user = _user_statusline()
    if isinstance(user, dict) and "padding" in user:
        settings["statusLine"]["padding"] = user["padding"]
    return settings


def _project_dir(cwd: str) -> Path:
    return lane.claude_dir() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resumed_session(args: list[str], cwd: str) -> str | None:
    """Session ID a resume flag points at, when it can be known before launch."""
    for i, arg in enumerate(args):
        name, _, value = arg.partition("=")
        if name in ("-r", "--resume"):
            value = value or (args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "")
            return value if re.fullmatch(r"[0-9a-fA-F-]{36}", value or "") else None
        if name in ("-c", "--continue"):
            files = sorted(_project_dir(cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            return files[-1].stem if files else None
    return None


def arm_of(session: str) -> str | None:
    digest = session_hash(session)
    for row in reversed(list(_rows("sessions.jsonl"))):
        if row.get("session") == digest:
            return row.get("arm")
    return None


def plan_launch(args: list[str], config: dict, cwd: str, rng: random.Random | None = None) -> tuple[list[str], dict | None]:
    """Return the claude argv to exec and the session record to log (None: plain native)."""
    flag = None
    rest = []
    for arg in args:
        if arg in MODE_FLAGS:
            flag = MODE_FLAGS[arg]
        else:
            rest.append(arg)
    mode = resolve_mode(flag, config)
    if mode == "off" or (rest and rest[0] in SUBCOMMANDS) or any(a.split("=")[0] in PASSTHROUGH for a in rest):
        return ["claude"] + rest, None
    manual = any(a.split("=")[0] in MANUAL for a in rest)
    resuming = any(a.split("=")[0] in RESUME for a in rest)
    session = None if resuming else next(
        (rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--session-id"), None)
    if resuming:
        session = resumed_session(rest, cwd)
        arm = (arm_of(session) if session else None) or "unassigned"
    elif mode == "trial":
        arm = "lanes" if (rng or random.SystemRandom()).random() < trial().get("ratio", 0.5) else "control"
    else:
        arm = "lanes" if mode == "lanes" else "control"
    if manual and arm in ARMS and not resuming:
        arm = "manual"
    cmd = ["claude"]
    if not resuming and session is None:
        session = str(uuid.uuid4())
        cmd += ["--session-id", session]
    if arm == "lanes":
        model, effort = lane.choose("claude", "standard", config)
        if not manual:
            cmd += ["--model", model, "--effort", effort] if effort else ["--model", model]
        cmd += ["--agents", json.dumps(agents_json(), separators=(",", ":"))]
    cmd += ["--settings", json.dumps(settings_json("auto" if arm == "lanes" else "shadow"), separators=(",", ":"))]
    record = {"event": "session", "arm": arm, "mode": mode, "resumed": resuming,
              "session": session_hash(session) if session else None,
              "model": cmd[cmd.index("--model") + 1] if "--model" in cmd else "native"}
    return cmd + rest, record


ARM_NOTE = {"control": "your normal setup (Jev records only)",
            "lanes": "Sonnet + scout/builder/architect + Jev hints",
            "manual": "your --model/--effort; excluded from the comparison",
            "unassigned": "resumed session from before the trial; not counted"}


def wrap(args: list[str]) -> int:
    config = lane.load_config()
    binary = shutil.which("claude")
    if not binary:
        print("effortlane: `claude` is not on PATH", file=sys.stderr)
        return 127
    try:
        cmd, record = plan_launch(args, config, os.getcwd())
    except Exception as error:  # never stand between the user and Claude
        print(f"effortlane: launch planning failed ({type(error).__name__}); starting native Claude.", file=sys.stderr)
        cmd, record = ["claude"] + [a for a in args if a not in MODE_FLAGS], None
    if record:
        _append("sessions.jsonl", record)
        label = record["arm"] if record["mode"] == "trial" or record["arm"] in ("manual", "unassigned") else record["mode"]
        print(f"effortlane: {label} - {ARM_NOTE.get(record['arm'], '')}", file=sys.stderr)
        if not lane.read_key():
            print(f"effortlane: no Jev key, so hints are off. Run `lane install` or export {lane.KEY_ENV}.",
                  file=sys.stderr)
        data = trial()
        if data.get("ends") and not data.get("stopped") and time.time() >= data["ends"]:
            print("effortlane: the trial period is over - run `lane trial report`, then `lane trial stop`.",
                  file=sys.stderr)
    os.execv(binary, cmd)
    return 0  # unreachable


# --- status line recorder ------------------------------------------------------------

def _user_statusline() -> Any:
    try:
        return json.loads((lane.claude_dir() / "settings.json").read_text()).get("statusLine")
    except (OSError, ValueError, AttributeError):
        return None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def statusline(stdin=None, stdout=None) -> int:
    """Record cost and limit counters when they change, then render the user's own status line."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    raw = stdin.read()
    try:
        event = json.loads(raw)
        session = event.get("session_id")
        if isinstance(session, str) and session:
            cost = event.get("cost") or {}
            limits = event.get("rate_limits") or {}
            sample = {
                "session": session_hash(session),
                "model": (event.get("model") or {}).get("id"),
                "cost_usd": _num(cost.get("total_cost_usd")),
                "lines_added": _num(cost.get("total_lines_added")),
                "lines_removed": _num(cost.get("total_lines_removed")),
                "h5": _num((limits.get("five_hour") or {}).get("used_percentage")),
                "d7": _num((limits.get("seven_day") or {}).get("used_percentage")),
            }
            last_path = _state("statusline-last.json")
            try:
                last = json.loads(last_path.read_text())
            except (OSError, ValueError):
                last = {}
            if last.get(sample["session"]) != sample:
                _append("statusline.jsonl", sample)
                last[sample["session"]] = sample
                if len(last) > 200:
                    last = dict(list(last.items())[-100:])
                lane._atomic_write(last_path, json.dumps(last), 0o600)
    except Exception:
        pass
    user = _user_statusline()
    command = user.get("command") if isinstance(user, dict) and user.get("type") == "command" else None
    if command:
        try:
            result = subprocess.run(command, shell=True, input=raw, capture_output=True, text=True, timeout=5)
            stdout.write(result.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


# --- report --------------------------------------------------------------------------

def price(model: str, usage: dict) -> float | None:
    rates = next((v for k, v in sorted(PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), None)
    if not rates:
        return None
    inp, out, read = rates
    cache = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w1h = cache.get("ephemeral_1h_input_tokens") or 0
    w5m = cache.get("ephemeral_5m_input_tokens")
    if w5m is None:
        w5m = max(0, (usage.get("cache_creation_input_tokens") or 0) - w1h)
    return ((usage.get("input_tokens") or 0) * inp + (usage.get("output_tokens") or 0) * out
            + (usage.get("cache_read_input_tokens") or 0) * read + w5m * inp * 1.25 + w1h * inp * 2) / 1e6


def transcript_usage(wanted: set[str]) -> dict[str, dict]:
    """Exact usage per session hash from main and subagent transcripts, one count per request."""
    result: dict[str, dict] = {}
    projects = lane.claude_dir() / "projects"
    for path in projects.glob("*/*.jsonl"):
        _scan(path, session_hash(path.stem), wanted, result)
    for path in projects.glob("*/*/subagents/*.jsonl"):
        _scan(path, session_hash(path.parent.parent.name), wanted, result)
    return result


def _scan(path: Path, digest: str, wanted: set[str], result: dict) -> None:
    if digest not in wanted:
        return
    bucket = result.setdefault(digest, {"cost": 0.0, "unpriced_requests": 0, "by_model": {}, "seen": set()})
    try:
        handle = open(path, errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            key = row.get("requestId") or message.get("id") or row.get("uuid")
            if key in bucket["seen"]:
                continue
            bucket["seen"].add(key)
            model = str(message.get("model") or "")
            if model.startswith("<"):
                continue
            cost = price(model, message["usage"])
            if cost is None:
                bucket["unpriced_requests"] += 1
                continue
            bucket["cost"] += cost
            stats = bucket["by_model"].setdefault(model, {"cost": 0.0, "requests": 0, "output_tokens": 0})
            stats["cost"] += cost
            stats["requests"] += 1
            stats["output_tokens"] += message["usage"].get("output_tokens") or 0


def _bootstrap(a: list[tuple[float, float]], b: list[tuple[float, float]], rounds: int = 2000) -> tuple[float, float, float]:
    """Relative difference of ratio-of-sums (b vs a) with a 95% percentile interval."""
    def ratio(rows):
        den = sum(x[1] for x in rows)
        return sum(x[0] for x in rows) / den if den else float("nan")
    rng = random.Random(7)
    point = ratio(b) / ratio(a) - 1
    draws = []
    for _ in range(rounds):
        ra = ratio([rng.choice(a) for _ in a])
        rb = ratio([rng.choice(b) for _ in b])
        if ra and ra == ra and rb == rb:
            draws.append(rb / ra - 1)
    draws.sort()
    return point, draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]


def _diff_ci(a: list[tuple[float, float]], b: list[tuple[float, float]], rounds: int = 2000) -> tuple[float, float, float]:
    """Absolute difference of ratio-of-sums (b - a), e.g. rework rate in points."""
    def ratio(rows):
        den = sum(x[1] for x in rows)
        return sum(x[0] for x in rows) / den if den else 0.0
    rng = random.Random(11)
    point = ratio(b) - ratio(a)
    draws = sorted(ratio([rng.choice(b) for _ in b]) - ratio([rng.choice(a) for _ in a]) for _ in range(rounds))
    return point, draws[int(0.025 * rounds)], draws[int(0.975 * rounds) - 1]


def collect(since: float) -> dict:
    sessions: dict[str, dict] = {}
    for row in _rows("sessions.jsonl"):
        if row.get("ts", 0) >= since and row.get("session") and not row.get("resumed"):
            sessions.setdefault(row["session"], {"arm": row.get("arm"), "prompts": 0, "rework": 0,
                                                 "tiers": {}, "status": None})
    for row in _rows("routes.jsonl"):
        s = sessions.get(row.get("session"))
        if row.get("event") == "hook" and s is not None:
            s["prompts"] += 1
            s["rework"] += bool(row.get("rework"))
            if row.get("tier"):
                s["tiers"][row["tier"]] = s["tiers"].get(row["tier"], 0) + 1
    samples = [r for r in _rows("statusline.jsonl") if r.get("ts", 0) >= since]
    for row in samples:
        if row.get("session") in sessions:
            sessions[row["session"]]["status"] = row
    usage = transcript_usage(set(sessions))
    for digest, s in sessions.items():
        u = usage.get(digest, {})
        s["cost"] = u.get("cost", 0.0)
        s["by_model"] = u.get("by_model", {})
        s["unpriced_requests"] = u.get("unpriced_requests", 0)
    # Account limit deltas attributed only between consecutive samples of one session
    # with no other session's sample in between (a solo stretch).
    limits = {arm: {"h5": 0.0, "d7": 0.0} for arm in ARMS}
    ordered = sorted(samples, key=lambda r: r.get("ts", 0))
    for before, after in zip(ordered, ordered[1:]):
        s = sessions.get(after.get("session"))
        if not s or before.get("session") != after.get("session") or s["arm"] not in ARMS:
            continue
        for key in ("h5", "d7"):
            if before.get(key) is not None and after.get(key) is not None and after[key] >= before[key]:
                limits[s["arm"]][key] += after[key] - before[key]
    return {"sessions": sessions, "limits": limits}


def report(days: float | None = None, as_json: bool = False) -> int:
    data = trial()
    since = data.get("started", 0) if days is None else time.time() - days * 86400
    result = collect(since)
    sessions = result["sessions"]
    arms = {arm: [s for s in sessions.values() if s["arm"] == arm and s["prompts"] > 0] for arm in ARMS}
    summary = {}
    for arm, rows in arms.items():
        prompts = sum(s["prompts"] for s in rows)
        cost = sum(s["cost"] for s in rows)
        by_model: dict[str, float] = {}
        for s in rows:
            for model, stats in s["by_model"].items():
                by_model[model] = by_model.get(model, 0.0) + stats["cost"]
        lines = sum((s["status"] or {}).get("lines_added") or 0 for s in rows) + \
            sum((s["status"] or {}).get("lines_removed") or 0 for s in rows)
        summary[arm] = {
            "sessions": len(rows), "prompts": prompts, "cost_usd": round(cost, 4),
            "cost_per_prompt": cost / prompts if prompts else None,
            "cost_per_session": cost / len(rows) if rows else None,
            "rework_rate": sum(s["rework"] for s in rows) / prompts if prompts else None,
            "cost_by_model": {k: round(v, 4) for k, v in sorted(by_model.items(), key=lambda kv: -kv[1])},
            "lines_changed": lines,
            "limit_points": {k: round(v, 2) for k, v in result["limits"][arm].items()},
            "limit_points_per_100_prompts": {k: round(100 * v / prompts, 2) if prompts else None
                                             for k, v in result["limits"][arm].items()},
            "unpriced_requests": sum(s["unpriced_requests"] for s in rows),
        }
    a, b = arms["control"], arms["lanes"]
    verdict, cost_ci, rework_ci = "not enough data", None, None
    if len(a) >= 2 and len(b) >= 2:
        cost_ci = _bootstrap([(s["cost"], s["prompts"]) for s in a], [(s["cost"], s["prompts"]) for s in b])
        rework_ci = _diff_ci([(s["rework"], s["prompts"]) for s in a], [(s["rework"], s["prompts"]) for s in b])
    if len(a) >= MIN_SESSIONS and len(b) >= MIN_SESSIONS and cost_ci and rework_ci:
        if cost_ci[2] < 0 and rework_ci[2] <= 0.05:
            verdict = "lanes SAVE: cheaper per prompt, rework not meaningfully worse"
        elif cost_ci[2] < 0:
            verdict = "lanes are cheaper per prompt but rework may be worse - not a clean win"
        elif cost_ci[1] > 0:
            verdict = "lanes COST MORE"
        else:
            verdict = "no measurable difference yet"
    out = {"since": since, "summary": summary, "verdict": verdict,
           "cost_per_prompt_change": cost_ci, "rework_rate_change": rework_ci,
           "min_sessions_per_arm": MIN_SESSIONS}
    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    _print_report(out)
    return 0


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:+.1f}%"


def _print_report(out: dict) -> None:
    s = out["summary"]
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(out["since"])) if out["since"] else "beginning"
    print(f"Effortlane Claude trial since {start}\n")
    rows = [
        ("sessions", "sessions", lambda v: str(v)),
        ("prompts", "prompts", lambda v: str(v)),
        ("API-equivalent cost", "cost_usd", lambda v: f"${v:.2f}"),
        ("cost per prompt", "cost_per_prompt", lambda v: "-" if v is None else f"${v:.3f}"),
        ("cost per session", "cost_per_session", lambda v: "-" if v is None else f"${v:.2f}"),
        ("rework prompts", "rework_rate", lambda v: "-" if v is None else f"{100 * v:.1f}%"),
        ("5h limit pts / 100 prompts", "limit_points_per_100_prompts", lambda v: "-" if v["h5"] is None else f"{v['h5']:.1f}"),
        ("7d limit pts / 100 prompts", "limit_points_per_100_prompts", lambda v: "-" if v["d7"] is None else f"{v['d7']:.1f}"),
        ("lines changed", "lines_changed", lambda v: f"{v:.0f}"),
    ]
    print(f"{'':<28}{'control':>14}{'lanes':>14}")
    for label, key, fmt in rows:
        print(f"{label:<28}{fmt(s['control'][key]):>14}{fmt(s['lanes'][key]):>14}")
    for arm in ARMS:
        models = ", ".join(f"{m.replace('claude-', '')} ${c:.2f}" for m, c in s[arm]["cost_by_model"].items())
        print(f"{arm} cost by model: {models or '-'}")
    if out["cost_per_prompt_change"]:
        p, lo, hi = out["cost_per_prompt_change"]
        print(f"\nlanes vs control, cost per prompt: {_pct(p)} (95% CI {_pct(lo)} to {_pct(hi)})")
        p, lo, hi = out["rework_rate_change"]
        print(f"lanes vs control, rework rate: {100 * p:+.1f} pts (95% CI {100 * lo:+.1f} to {100 * hi:+.1f})")
    print(f"\nVerdict: {out['verdict']}")
    if "not enough" in out["verdict"]:
        print(f"Needs at least {out['min_sessions_per_arm']} sessions with prompts in each arm.")
    print("\nCost = exact transcript tokens x API list prices (subagents included); it ranks the arms,\n"
          "it is not a bill. Limit points = account 5h/7d % used during solo stretches of a session.\n"
          "Rework = follow-up prompts like 'still broken' / 'не работает'. Jev calls are extra and equal in both arms.")
