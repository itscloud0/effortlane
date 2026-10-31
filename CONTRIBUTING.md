# Contributing to Effortlane

Effortlane currently integrates native Codex on macOS. Claude Code has experimental opt-in Shadow hooks with live validation pending; Windows is future work. Small, testable changes and evidence of completed-task outcomes are welcome.

## Development

Use Python 3.11+; the runtime uses the standard library. `lane.py` must also run on the macOS system Python 3.9 (see AGENTS.md). No dependency install is needed for the unit suite.

```sh
python3 -m unittest discover -s . -p 'test_*.py' -q
python3 -m py_compile core.py manage.py rpc_adapter.py desktop_bootstrap.py cli_bridge.py transport.py
```

Socket tests need loopback access. Report sandbox failures separately from product failures. Do not alter your real Codex configuration just to run tests.

## Useful reports

Include OS and native Codex version, client (Desktop / interactive CLI / exec / resume), mode, sanitized `doctor` findings, expected behavior, actual behavior, and a minimal reproduction. Remove personal paths and identifiers. Never attach keys, auth.json, complete prompts, source, or full session rollouts.

## Measuring value

Primary objective: less subscription allowance per correctly completed task. Record dated allowance snapshots within the same reset window, active model/effort, completion and rework, elapsed time, and concurrent account activity. Shadow proposals are not executed savings. API-price counterfactuals are not Pro allowance measurements. Aggregate anonymous metadata before sharing; measurements are opt-in.

## Pull requests

Explain the concrete problem and resulting behavior, add tests for runtime behavior changes, and list validation and limits. Preserve native authentication, manual override, bounded Jev input, and clean rollback. Avoid new dependencies and unrelated refactors. New adapters must document their authentication, billing, privacy, and fail-open behavior.

Ideas and routing comparisons belong in issues; private vulnerabilities follow [SECURITY.md](SECURITY.md).

## Updating public evidence

The README chart is generated from [a dated public aggregate](assets/shadow-evidence.json), not private logs. Update the aggregate only after checking its source in [the evidence history](SHADOW_COMPARISON.md). Keep the observation date, denominators, missing coverage and limitations visible; never present unexecuted Shadow proposals as achieved savings.

```sh
python3 scripts/render_evidence.py
python3 scripts/render_evidence.py --check
```

The renderer uses only the Python standard library. Commit the data and SVG together. It does not read local telemetry, upload measurements, or install dependencies.
