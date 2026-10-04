# Reasoning effort and cache: native Codex audit

Verified 2026-10-04 against Codex CLI **0.160.0**, its release-tag source, and the native CLI bundled with ChatGPT Desktop (also 0.160.0). No production settings changed. No real model inference or paid API request was used for the wire probe.

## Finding

Keeping the model fixed does **not** by itself guarantee cache preservation when reasoning effort changes. OpenAI documents a cache-preserving mechanism: retain the original request-level `reasoning.effort`, append `configuration_update` items, and preserve the existing input prefix. See [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) and [reasoning updates](https://developers.openai.com/api/docs/guides/reasoning#change-reasoning-mid-conversation).

Native Codex already implements this. It is gated by `features.reasoning_effort_override`, the OpenAI provider, and the model catalog's `supports_reasoning_effort_updates` flag. The feature is **under development and disabled by default**, including on this installation. The authenticated catalog advertises support for GPT-6.1 Sol. Effortlane passes selected effort through `turn/start`; the native executor decides which wire representation to use. The same native mechanism handles manual effort changes.

Release-source evidence:

- [Native request baseline and update insertion](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/session/reasoning_effort.rs)
- [Provider/model capability gates and request filtering](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/client.rs)
- [Feature default](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/features/src/lib.rs)
- [Replay/compaction tests](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/session/reasoning_effort_tests.rs) — inspected, not compiled or run in this audit.

## Actual binary probe

Run the reproducible **offline** probe:

```sh
python3 scripts/probe_native_effort.py \
  --native /opt/homebrew/bin/codex \
  --catalog ~/.local/share/jev-codex-router/native-models.json
```

The probe uses isolated temporary HOME/CODEX_HOME, fake API credentials, and a local server bound to 127.0.0.1. It never forwards requests. It retains only model/effort/configuration-update metadata, never request prompts or authorization headers. Responses and usage are synthetic. It closes only its own processes/server and removes its temporary state.

For two turns selecting Low then High:

| Feature | First request | Second request |
|---|---|---|
| Disabled | Request effort Low; no update | Request effort High; no update |
| Enabled | Request effort Low; update Low | Request effort Low; updates Low, High |

After terminating the owned server, changing its temporary startup effort to High, resuming the same retained thread, and selecting Medium:

| Feature | Resumed request |
|---|---|
| Disabled | Request effort Medium; no update |
| Enabled | Request effort **Medium**; retained updates Low, High, Medium |

The enabled resumed request no longer retained the original request baseline Low in this scenario. Therefore enabling the feature is **not a complete cache-preservation guarantee across restart/resume**. This is consistent with the open upstream [resume baseline issue](https://github.com/openai/codex/issues/48802). Open upstream reports also describe [effort effectiveness problems](https://github.com/openai/codex/issues/47843); they are reports, not proof of the same behavior on this Pro account.

## Limits and recommendation

This probe establishes native **request shape**, not OpenAI backend acceptance, real cache reuse, reasoning effectiveness, quality, latency savings, or Pro allowance savings. Catalog capability is not proof of successful execution. The API guide's single-agent compatibility scope cannot be assumed to establish every Codex subscription backend behavior.

Do not enable frequent effort switching globally on the assumption that a fixed model preserves cache. Do not enable the experimental flag globally as a complete fix. A bounded CLI experiment may opt in **per process**, but must verify actual backend acceptance, effective effort, complete-turn cached/uncached input, reasoning/output tokens, latency, and resume behavior through native ChatGPT authentication. A repeated cold prefix can outweigh reasoning-token savings. Existing Shadow cache ratios do not predict counterfactual Auto performance.

No Effortlane model policy, global feature setting, authentication, or active session was changed by this audit.
