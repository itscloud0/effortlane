# Effortlane: notes for coding agents

Python standard library only. No dependency install.

- Full suite (Python 3.11+): `python3 -m unittest discover -s . -p 'test_*.py' -q`
- `lane.py`, `lane_claude.py` and the modules they import (`core.py`, `costs.py`) must also run on the
  macOS system Python 3.9: `/usr/bin/python3 -m unittest test_lane test_lane_claude -q`
- Never touch the real `~/.codex`, `~/.claude`, `~/.hermes` or key files in tests;
  point `HOME`, `XDG_*`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `HERMES_HOME` at a temp dir.
- Never log or send raw prompts, paths, source or keys. Jev input goes through
  `core.sanitize_task`; local logs hold metadata only.
- Fail open: a hook must never block or slow a native prompt past its deadline.
- Every user brings their own TypeSafe/Jev key. Never commit one.

Map: `lane.py` (Jev lanes for Claude/Codex/Hermes, see docs/LANES.md) · `lane_claude.py`
(seamless `claude` wrapper, control-vs-lanes trial, transcript-based usage report) ·
`core.py` (Codex routing policy, sanitizer, Jev client) · `manage.py` (Codex
installer and CLI) · `rpc_adapter.py`/`cli_bridge.py`/`transport.py` (Codex app-server
bridge) · `claude_shadow.py` (observe-only Claude hooks) · `metrics.py`/`trials.py`.
