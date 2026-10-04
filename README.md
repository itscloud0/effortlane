<div align="center">

<img src="assets/router-banner.png" alt="Effortlane — model and reasoning-effort routing for coding agents" width="1200">

# Effortlane

**Choose model and reasoning effort. Keep your native coding agent.**

Open-source LLM routing for Codex on macOS, with Shadow evaluation and local usage telemetry.

[![Tests](https://img.shields.io/github/actions/workflow/status/itscloud0/effortlane/tests.yml?branch=main&style=flat-square&label=tests)](https://github.com/itscloud0/effortlane/actions/workflows/tests.yml)
[![MIT](https://img.shields.io/badge/license-MIT-94a3b8?style=flat-square)](LICENSE)
![macOS](https://img.shields.io/badge/platform-macOS-94a3b8?style=flat-square)
![Experimental](https://img.shields.io/badge/status-experimental-fbbf24?style=flat-square)

[**Try Shadow →**](#quick-start) · [See the evidence](#what-we-have-measured) · [How it works](#how-it-works) · [Get help](#help-build-effortlane)

</div>

## What is Effortlane?

Effortlane is a local model and reasoning-effort router for coding agents. It selects from your authenticated Codex model catalog before a user turn, preserves native ChatGPT authentication, and records routing evidence locally. In **Shadow mode**, you keep the real executor and effort while Effortlane records what it would recommend.

The goal: **less subscription allowance per correctly completed task**. A cheap call that causes retries or rework is not a saving.

| What you get | Why it matters |
|---|---|
| Model **and** effort selection | Adjust reasoning depth as well as model capability |
| Shadow before Auto | Inspect recommendations before letting them change execution |
| Decisions between turns | Keep one executor through a turn's thoughts and tool calls |
| Native login and context | Continue using your existing Codex account and full executor context |
| Local evidence and recovery | Inspect routes, cache counters and failures; return to native Codex |

> [!IMPORTANT]
> **Experimental. Subscription savings and quality improvements are not yet proven.** Codex CLI on macOS is implemented. Desktop and mobile Remote should stay native. Native Shadow uses documented hooks; the Desktop Auto adapter remains experimental and opt-in. The external decision service may have its own cost.

## Quick start

You need **macOS, Python 3.11+, native Codex installed and signed in, and a TypeSafe/Jev API key**. No per-project setup or Python dependency install is required.

Install with one command:

```sh
curl -fsSL https://raw.githubusercontent.com/itscloud0/effortlane/main/install.sh | sh
```

Then start the evaluation:

```sh
~/.local/bin/codex --effortlane-shadow
```

The script downloads source into a temporary directory, runs the same user-level
installer, and cleans up. It does not install Python/Codex, change shell profiles,
or sign you in. Existing healthy installations are checked without resetting
settings; this command does **not** upgrade installed Effortlane source.
[Inspect the script](install.sh) or use a reviewed checkout:

```sh
git clone https://github.com/itscloud0/effortlane.git
cd effortlane
python3 bootstrap.py
```

The installer asks for the decision-service key with hidden input, backs up Codex configuration, and installs under your user account. It preserves unrelated settings and native login. Follow any PATH instruction it prints to use the short commands below.

**This example explicitly starts Shadow.** Fresh installation otherwise defaults the CLI wrapper to Auto. Existing concrete-model choices remain manual overrides.

### Keep Desktop and Remote native

```sh
effortlane desktop-safe
effortlane native-shadow enable
```

Review **Effortlane native Shadow** once in Codex `/hooks`, then restart the host app after active work finishes. The observer runs asynchronously and emits no prompt context. It records sanitized routing proposals without changing your native model or reasoning effort. CLI Auto continues through its separate app-server bridge; bridged CLI turns skip the observer to avoid duplicate decisions.

```sh
effortlane native-shadow report
effortlane native-shadow disable
```

The documented hook supplies the active model but not reasoning effort, executor token usage, or cache counters. These observations alone **cannot establish subscription savings**. Native Desktop/Remote do not offer an Effortlane model-picker entry in this setup. Existing frontend alias selections must be replaced with a concrete native model. Hook installation does not bypass Codex trust or prove it has run. [Details](docs/NATIVE_SHADOW.md).

### Choose your mode

| Mode | What actually runs | Who chooses effort? |
|---|---|---|
| **Effortlane Shadow** | Latest Sol in that client's account catalog | You; Effortlane independently records a proposal |
| **Effortlane Auto** | An allowed native Codex model | Effortlane, subject to local policy |
| **Concrete model** | The model you select | You; automatic routing is bypassed |

```sh
codex --effortlane-shadow             # evaluate proposals without applying them
codex --effortlane-auto               # apply model and effort routing
codex exec --effortlane-shadow "Explain this module"
effortlane metrics --hours 168        # inspect coverage, cache and latency
effortlane trace THREAD_UUID          # inspect one thread's routing evidence
```

**Desktop stays native by default.** To opt in, run `effortlane desktop-enable` and restart the app. Select Effortlane Auto or Effortlane Shadow in its picker. [Compatibility and recovery](docs/OPERATIONS.md#limits) · [Legacy identifier compatibility](docs/COMPATIBILITY.md).

## What we have measured

**One Mac · September 29–October 2, 2026 · snapshot at 18:18 UTC · includes diagnostic interactions.**

<img src="assets/shadow-evidence.svg" alt="Of 312 Shadow proposals, 286 selected Sol and 26 Luna. Effort recommendations were lower in 114 cases, unchanged in 163, and higher in 35. Proposals were not executed; savings are unproven." width="1200">

The real Shadow executor was **GPT-6.1 Sol at the user-selected effort**. Of 360 decisions, 312 had a proposal; the other 48 were privacy fallbacks, a timeout, or lease reuse. Sixteen Luna proposals were held by the cache guard.

| Observed | What it means |
|---|---|
| 114 / 312 proposals suggested lower effort | A hypothesis to test; **not 36.5% savings** |
| 350 linked usage records | Linkage exists; historical records do not cover complete turns |
| 94.9% cached input on 342 records with cache detail | Actual Sol cache behavior, not a router-attributable benefit |
| 361 ms median decision-service latency | Measured additional routing overhead |

**What is still unknown:** subscription allowance saved, quality-equivalent completion cost, and effect on rework. The historical sample has 261 last-call records and 89 of unknown scope. New instrumentation can record complete native-thread turns when both cumulative counters are available; it cannot repair that old sample.

[Read the dated evidence](SHADOW_COMPARISON.md) · [Inspect the chart data](assets/shadow-evidence.json) · [Run a task comparison](docs/EVALUATION.md#local-task-ledger)

## How it works

<img src="assets/architecture.png" alt="A bounded sanitized task dossier goes to the Jev decision service. Effortlane validates model and effort locally; native Codex executes with its full context." width="1200">

1. **Build a small routing dossier.** Bound and sanitize task information; omit full conversations, repository contents, images and tool outputs.
2. **Request a recommendation.** TypeSafe/Jev supplies typed work-shape and effort choices. It does not execute the coding task.
3. **Apply local policy.** Check available models, allowlists, risk and route continuity. A cache guard delays some downgrades; needed capability upgrades remain possible.
4. **Execute with native Codex.** Preserve canonical context and ChatGPT authentication. Tool calls within the turn do not trigger model switching.
5. **Record metadata locally.** Track available model, effort, token/cache counters, latency, failures and receipt coverage. No automatic telemetry upload.

If the decision service is unavailable or the dossier lacks safe signal, Effortlane uses a conservative native fallback. Concrete model choices always override Auto. Effortlane does not split tasks or decide when to spawn subagents; native Codex controls delegation.

## Supported clients

| Integration | Current status |
|---|---|
| Codex interactive CLI, `exec`, resume · macOS | Implemented; validate against your installed Codex release |
| Codex Desktop · macOS | Experimental opt-in adapter; uses an undocumented app override |
| Claude Code · macOS | [Experimental Shadow hooks](docs/CLAUDE_CODE.md); proposals and native lifecycle metadata; live validation pending |
| Windows / Linux | Not supported by the installer |

## Common questions

### Does Effortlane use my ChatGPT subscription?

Coding execution stays on native Codex with your existing ChatGPT login. Effortlane does not silently replace it with separately billed OpenAI or OpenRouter model execution. Jev routing decisions use a separate TypeSafe key and may incur provider charges.

### Does Shadow save subscription allowance?

Changing effort can also affect cache reuse; see the [native Codex cache/effort audit](docs/CACHE_EFFORT_AUDIT.md), including its restart/resume limitation.

Shadow does not apply its model or effort proposals. It helps identify candidate policies to test. An API-price counterfactual or lower-effort recommendation cannot establish Pro allowance savings. Compare accepted tasks, usage coverage, elapsed time and rework using the [evaluation protocol](docs/EVALUATION.md).

### Can I control expensive models and reasoning effort?

Yes. Configure model roles and policy globally; Astra is excluded from automatic selection by default. Shadow applies your selected effort to Sol. Auto chooses actual effort independently of its visible picker value. See the [policy reference](docs/OPERATIONS.md#policy-configuration) and [example configuration](policy.example.json).

### Does it preserve every native feature?

The integration aims to preserve native tools, but Desktop compatibility has regressed across updates before. Revalidate the tools you rely on after updates; use `effortlane desktop-safe` and restart to restore native Desktop. See [known limits](docs/OPERATIONS.md#limits).

### How do I update Codex or remove Effortlane?

`codex update` updates supported native CLI installations and refreshes their account catalog. It does **not** update Effortlane's Python source or ChatGPT.app. See [installation and update boundaries](docs/OPERATIONS.md#install-and-controls).

Effortlane does not yet have a one-command source upgrader. Pulling this repository alone does not update the installed copy; the fresh-install bootstrap refuses an existing installation.

```sh
effortlane doctor          # inspect installation and measurement health
codex-native               # bypass routing for a native CLI session
effortlane desktop-safe    # restore native Desktop; restart the app
effortlane disable         # disable routing
effortlane rollback        # restore owned installation settings
```

Backups and ownership checks protect unrelated edits. Sessions are not deleted and you are not logged out.

## Help build Effortlane

- **Try Shadow** with the [quick start](#quick-start), then inspect your local metrics.
- **Report a reproducible bug:** [open the bug template](https://github.com/itscloud0/effortlane/issues/new?template=bug_report.md).
- **Contribute real evidence:** [share aggregate task outcomes](https://github.com/itscloud0/effortlane/issues/new?template=measurement.md), including rework and missing coverage.
- **Improve an adapter or policy:** read [CONTRIBUTING.md](CONTRIBUTING.md). Claude live validation and update compatibility are useful starting points.

A star helps others find the project. Never post API keys, auth files, proprietary prompts, or complete rollouts in an issue.

## Documentation

[Operations and policy](docs/OPERATIONS.md) · [Metrics](docs/METRICS.md) · [Evaluation](docs/EVALUATION.md) · [Claude Code](docs/CLAUDE_CODE.md) · [Evidence history](SHADOW_COMPARISON.md) · [Security](SECURITY.md) · [Related work](docs/OPERATIONS.md#related-work)

Maintained by [@itscloud0](https://github.com/itscloud0), with contributions welcome under the [MIT license](LICENSE). Independent community project; not affiliated with OpenAI, Anthropic, or TypeSafe.
