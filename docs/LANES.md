# Lanes: Jev routing for Claude Code, Codex and Hermes

`lane` asks Jev which of three lanes a task needs, then starts your agent with that
lane's model and reasoning effort. It works with Claude Code, Codex and Hermes, needs
only the macOS system Python (3.9+), and uses your own TypeSafe/Jev key.

| Lane | Claude Code | Codex | Hermes (openai-codex provider) |
|---|---|---|---|
| light | `haiku`, effort low | newest Luna, low | newest Luna |
| standard | `sonnet`, effort medium | newest Terra, medium | newest Terra |
| deep | `opus`, effort high | newest Sol, high | newest Sol |

Codex roles resolve against your local Codex catalog (`~/.codex/models_cache.json`),
so a new Sol release is picked up without editing anything. Hermes has no per-launch
effort flag, so only its model changes; with a non-Codex provider set its models in
`lane.json` (see below) or Hermes keeps its own default.

## Install

```sh
python3 lane.py install                    # key prompt (hidden), launcher, Claude subagents and hook
python3 lane.py install --model sonnet     # also make new Claude sessions start on Sonnet
lane doctor
```

The key is read from `TYPESAFE_API_KEY`, else from the Effortlane key file
(`~/.config/jev-codex-router/typesafe-api-key`, owner-only). An existing Effortlane
install shares the same key. Every user supplies their own key; none ships with the repo.

`install` backs up `~/.claude/settings.json` before changing it, keeps your other
hooks, never overwrites a subagent file you edited (unless `--force`, with a backup),
and refuses to replace a `~/.local/bin/lane` it did not create. `lane uninstall`
removes what it added and leaves your model settings alone.

## Use

```sh
lane claude "why does the sync worker deadlock under load"   # Jev -> deep -> claude --model opus --effort high
lane codex "rename getUser to fetchUser"                     # Jev -> light -> codex -m gpt-6-luna ...
lane hermes "summarize this repo's architecture"
lane claude                                                  # no task: standard lane, no Jev call
lane claude --print "explain src/router.ts"                  # one-shot: claude -p / codex exec / hermes -Q -q
lane claude --tier deep "..."                                # skip Jev
lane claude "..." -- --resume                                # after --: native args, unchanged
lane route "task text"                                       # show the lane without launching
lane stats --days 7                                          # lane mix, Jev vs fallback, Jev tokens
```

A native `--model`/`--effort` (or Codex `-m`, `-c model_reasoning_effort=...`) after
`--` always wins. Starting a prompt with `!light`, `!std` or `!deep`, or writing
`ultrathink`, forces a lane without asking Jev.

## Inside a Claude Code session

No Claude Code hook can change the running model or effort, so a session keeps the
model it started with. `install` adds what can work inside a session:

- **Subagents** (`~/.claude/agents/`): `scout` (Haiku, low, read-only lookups),
  `builder` (Sonnet, medium, bounded edits) and `architect` (Opus, high, read-only
  diagnosis and plans). Their descriptions tell the main agent when to use each, so a
  Sonnet session can borrow Opus for the hard part only.
- **Prompt hook** (`UserPromptSubmit`): asks Jev about each prompt (2 s timeout,
  3 s hard limit) and adds one line of context for light or deep prompts, pointing
  at `scout` or `architect`. Standard prompts add nothing. Disable hints with
  `"hints": false` in `lane.json`.

The biggest saving is usually the default model: `lane doctor` warns when Claude
starts every session on Opus. `--model sonnet` or `--model opusplan` (Opus only in
plan mode) fixes that; `/model` still switches any session by hand.

## What is sent, what is logged

- Sent to Jev: a sanitized excerpt from `core.sanitize_task` (absolute paths, URLs,
  emails, secrets, fenced code and code appendices are redacted), the agent name and
  the three lane descriptions.
- Never sent: prompts containing pasted content, prompts under 12 characters or mostly
  redacted. These use the local rules and are logged as `local`.
- If Jev times out or errors, local keyword rules pick the lane (`fallback`).
- Logged locally to `~/.local/state/effortlane-lane/routes.jsonl`: lane, source,
  reason names, model, effort, prompt length, a hashed session ID and Jev token counts.
  No prompt text.

## Configuration

`lane config --write` creates `~/.config/effortlane/lane.json` with the defaults:

```json
{
  "tiers": {
    "claude": {"deep": {"model": "opusplan", "effort": "high"}},
    "hermes": {"light": {"model": "anthropic/claude-haiku-5-5"}}
  },
  "rules": [{"pattern": "terraform|helm", "tier": "deep"}],
  "hints": true
}
```

Tiers you list replace the default for that agent and lane; `rules` are regexes checked
before Jev, first match wins.

## Limits

- The lane is chosen once, at launch. A session that turns out harder than its first
  prompt stays on its model until you `/model` it (Claude) or restart.
- Lane routing has no outcome evidence yet. Compare `/usage` (Claude) or the Codex
  status line before and after, and watch for rework: a cheap lane that needs a second
  attempt costs more than the right lane once.
