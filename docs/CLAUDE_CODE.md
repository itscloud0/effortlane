# Claude Code Shadow (experimental)

`effortlane claude [native Claude arguments...]` is an experimental Claude Code
adapter. It starts the installed `claude` binary with its existing native login
and tools. It does not modify `~/.claude/settings.json`, aliases, PATH,
environment variables, or authentication.

The adapter generates an owner-only `claude-shadow-settings.json` fragment in
the Effortlane root. The fragment registers an asynchronous `UserPromptSubmit`
hook. Claude Code continues to choose and execute its own model. The hook sends
only a bounded, sanitized task dossier to Jev, then records an offline proposal
among `haiku`, `sonnet`, and `opus`; it never applies that proposal.

This is Shadow-only: there is no model override, no account-availability claim,
and no Claude quota or real token-usage telemetry. Network errors, timeouts,
malformed events, and unavailable Jev fail open. The adapter does not
read transcripts. Prompts containing Claude's `<pasted_content ...>` wrapper,
including malformed or incomplete wrappers, are recorded only as
`privacy_filtered` and never route remotely or resolve the local key path. It
accepts no `-p`/`--print` mode in this first release.
The Jev socket timeout is two seconds and the hook process has a separate,
enforced three-second POSIX wall-time limit; the Claude async-hook timeout field
is not relied on for that deadline.

The native CLI arguments, including `--model` and `--resume`, are passed through
unchanged. A supplied `--settings` is refused so the generated fragment cannot
silently overwrite native settings. If the owned fragment is edited, launch
refuses to replace it. Claude Code **2.1.251 or newer** must already be installed and signed in. The launcher verifies the client version before writing its owned settings fragment.

The generated hook format follows Claude Code's documented
[hooks](https://code.claude.com/docs/en/hooks), and native argument behavior
follows the [CLI reference](https://code.claude.com/docs/en/cli-reference).
Authentication remains subject to Claude Code's documented
[environment-variable precedence](https://code.claude.com/docs/en/env-vars); the
adapter leaves the native authentication and billing mode to Claude Code. Print
mode is unsupported in this first release.
The adapter also observes native `SessionStart`, `PostModelSwitch`, `Stop`, and
`StopFailure` events. It records allowlisted model IDs, effective effort when supplied,
hashed prompt/session IDs, failure categories, and native switch metadata:
context tokens, likely-warm cache, cache TTL and estimated cache-write USD.
Estimates are not actual charges or subscription allowance. Model observations are
lifecycle events, not a per-turn executor catalog. Missing fields remain unknown;
no model is inferred from shell variables or transcript text.

These observers never call Jev. No `PreModelSwitch` hook is registered because a
failure or timeout there can block a native switch. Hook stdout stays empty, so
no routing receipts or instructions are injected into model context.

Live Claude Code integration has not been validated on this Mac: no installed
`claude` executable was found on 2026-10-05. Tests cover hook schemas, native
argument forwarding with a fake executable, version gating, privacy and fallbacks;
they do not establish live authentication, backend acceptance or savings.

## Use with an existing Effortlane installation

```sh
effortlane claude --model sonnet
effortlane claude --resume
effortlane claude-report --hours 168
```

The existing TypeSafe key is reused locally. No Claude package or account is
installed by these commands. To run without the hook, launch ordinary `claude`.
No persistent Claude setting needs to be undone. An already-open Effortlane
Claude session keeps its hook until that session exits.

Optional `claude_shadow_candidates` in the existing Effortlane `config.json`
restricts proposals, for example `{"haiku":["default"],"sonnet":["medium","high"]}`.
An explicitly empty/invalid allowlist disables proposals. Haiku `default` means
no effort override; other defaults use low/medium/high. These are semantic
families, not a discovered account catalog. Conversation history is never read. Actual model and effort are recorded only
where native lifecycle hooks explicitly supply them; proposal records still do
not claim a verified per-turn executor. Follow-up prompts without enough task
context may produce weak proposals: do not promote this adapter to Auto based
on agreement counts alone.

Reports show allowlisted proposal pairs, latency, failures, filtered-input counts,
and whether the bounded local log scan was truncated. Hook process deadlines may
end before a receipt is written; event count is not proof of complete coverage.
No raw prompts, paths, source, transcript, assistant output, error details, or auth
data are logged. Native prompt IDs and session IDs are hashed before storage. This release
requires the existing POSIX Python runtime; Windows support remains future work.

## Why this is not Auto

The documented command-hook `UserPromptSubmit` output can add context or block a
prompt; it cannot set the running model or effort. `PreModelSwitch` can allow,
ask or deny an already requested switch, not replace the target. Programmatic
`set_model` control requires a separate host/Agent SDK integration. This adapter
therefore leaves native `/model` and `/effort` in charge instead of editing files
and pretending the current turn changed. [Native hook contract](https://code.claude.com/docs/en/hooks).

Claude's optional [OpenTelemetry monitoring](https://code.claude.com/docs/en/monitoring-usage)
can provide actual request token/cache counters. Effortlane does not enable an
exporter or collector implicitly. Those counters and accepted-task outcomes are
needed before estimating the benefit of proposed routes. Native switch estimates
and agreement rates cannot establish subscription savings.
