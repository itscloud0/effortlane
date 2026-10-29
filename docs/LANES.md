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
git clone https://github.com/itscloud0/effortlane.git && cd effortlane
python3 lane.py install        # Jev key (hidden prompt), `lane` launcher, `claude` shell function
lane doctor
lane trial start               # optional: measure before you trust it
```

`install` copies the runtime to `~/.local/share/effortlane-lane/`, adds
`~/.local/bin/lane`, and puts a marked `claude` function in `~/.zshrc` (or
`~/.bashrc`) after backing it up. **Nothing under `~/.claude` changes**: each launch
passes the harness to Claude Code with its own `--settings` and `--agents` flags, so
Claude's auto-updates, your settings, hooks, status line and aliases keep working.
`claude-native` (or `command claude`) bypasses Effortlane; `lane uninstall` removes
the function, the launcher and the runtime.

The key is read from `TYPESAFE_API_KEY`, else from the Effortlane key file
(`~/.config/jev-codex-router/typesafe-api-key`, owner-only). An existing Effortlane
install shares the same key. Every user supplies their own key; none ships with the repo.

`install --global` instead writes the subagents and prompt hook into `~/.claude` for
every Claude session (IDE and Desktop included), with a settings backup.
`--model sonnet` also changes Claude's own default model.

## Seamless `claude`

Type `claude` as before. One line on stderr says what this session is:

| Mode | What runs | Set with |
|---|---|---|
| shadow (default) | your normal Claude; Jev records the lane it would pick | `claude_mode` in `lane.json` |
| lanes | Sonnet · medium, `scout`/`builder`/`architect`, Jev delegation hints | `claude --effortlane-auto` |
| trial | random per new session: control (shadow) or lanes | `lane trial start` |
| off | plain Claude | `claude --effortlane-off`, `claude-native` |

`--continue`/`--resume <id>` keep the session's arm. `claude -p`, `--settings`,
`--agents` and subcommands (`claude mcp ...`, `claude update`) pass through untouched,
and so does anything run from scripts (shell functions only exist in interactive shells).
A `--model`/`--effort` you pass wins; such sessions are marked `manual` and left out of
the comparison. If planning a launch ever fails, native Claude starts.

## Trial and exact metrics

```sh
lane trial start --days 14     # each new interactive session: 50% control, 50% lanes
lane trial status              # sessions and prompts per arm so far
lane trial report              # the comparison; --json for raw numbers
lane trial stop
```

What the report measures, per arm:

- **API-equivalent cost**: every request in Claude Code's own transcripts
  (`~/.claude/projects/...`, subagent transcripts included), counted once per request ID
  and priced per model with input, output, cache-read and 5-minute/1-hour cache-write rates.
  The token counts are exact; the prices are API list prices, which rank the arms but are
  not your subscription bill. Only usage fields are read, never message text.
- **Account limit points**: the 5-hour and weekly `used_percentage` Claude Code gives the
  status line, recorded through a pass-through wrapper that still renders your own status
  line. Increases are attributed to an arm only across solo stretches (two consecutive
  samples from the same session with no other session active in between), so parallel
  sessions don't contaminate it. This is the closest available measure of the allowance.
- **Rework rate**: share of prompts that read like a failed first attempt ("still
  broken", "не работает", "revert that"). Only a boolean is stored.
- **Lines changed**: from the status line, as a sanity check of work done.

The verdict compares cost per prompt with a 95% bootstrap interval over sessions and
waits for at least 20 sessions with prompts in each arm. "Lanes save" requires the whole
interval below zero and rework no more than 5 points worse. Jev calls are made in both
arms (control records Jev's choice), so they cancel out of the comparison.

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
model it started with. The lanes arm adds what can work inside a session:

- **Subagents** (`~/.claude/agents/`): `scout` (Haiku, low, read-only lookups),
  `builder` (Sonnet, medium, bounded edits) and `architect` (Opus, high, read-only
  diagnosis and plans). Their descriptions tell the main agent when to use each, so a
  Sonnet session can borrow Opus for the hard part only.
- **Prompt hook** (`UserPromptSubmit`): asks Jev about each prompt (2 s timeout,
  3 s hard limit) and adds one line of context for light or deep prompts, pointing
  at `scout` or `architect`. Standard prompts add nothing. Disable hints with
  `"hints": false` in `lane.json`.

`/model` still switches any session by hand.

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
