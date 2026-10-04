# Native Shadow

Recommended boundary: native ChatGPT Desktop and mobile Remote; CLI Auto through the local app-server bridge. The opt-in Desktop Auto adapter is experimental.

Native Shadow observes `UserPromptSubmit` using [documented Codex hooks](https://learn.chatgpt.com/docs/hooks). It never modifies executor selection, effort, prompts, native auth, provider, or the app executable. It emits no stdout/stderr to avoid adding context or warnings to model requests. The hook is asynchronous, with a three-second process deadline and a four-second native timeout. No background daemon is added.

## Enable

```sh
effortlane desktop-safe
effortlane native-shadow enable
```

Installation merges one handler into the user-level `hooks.json` next to the manifest's Codex config. Existing hooks remain. Previous files are backed up in Effortlane's `backups/native-shadow-*`; writes are atomic, serialized, and checked for concurrent foreign edits. Repeated installation is a no-op. Manual modifications to the owned handler are reported rather than overwritten.

Codex requires trust for the exact handler definition. Open `/hooks`, review **Effortlane native Shadow**, and trust it. Effortlane does not write hook trust or pass `--dangerously-bypass-hook-trust`. Restart Desktop after current work finishes to load native configuration. An active app-server is not hot-patched. Select an actual native model in existing Desktop/Remote chats that retain a routing alias.

## Data and limits

Only a bounded sanitized task instruction reaches the existing routing core/Jev. Pasted-content wrappers, private-key markers, oversized and ambiguous input are rejected before routing. Session IDs are hashed in telemetry. Repository files, cwd, complete events and prompts are not stored. The hook does not read conversation history or tool output.

Proposals go to `state/native-shadow-routing*.jsonl`, separate from executor usage telemetry and normal Auto leases. Native hook identity is `native_hook`; it does not distinguish Desktop, mobile, and unbridged CLI because the documented event does not provide that distinction. The CLI bridge marks its child executor so its turns do not make duplicate observer decisions.

The documented hook supplies `model`; it does not provide actual reasoning effort, tokens, cache hits, or subscription allowance changes. Missing fields remain unknown. The routing core evaluates a hypothetical Shadow route; this is not executor activity. No subscription savings, token reduction, or quality improvement can be inferred from proposals alone. Existing Auto/CLI telemetry remains separate.

```sh
effortlane native-shadow report
```

The report counts proposals by model/effort and observed native models. Zero observations may mean the hook is awaiting trust, the app has not reloaded, input was privacy-filtered, or the host has not fired the event. It does not establish successful activation. Desktop built-in tools and mobile live turns still need user-side validation after restart.

## Disable and rollback

```sh
effortlane native-shadow disable
```

This removes only the owned handler and preserves other hooks. Full `effortlane disable`/`rollback` also removes the observer. Backups and telemetry remain. A manually edited owned handler blocks automatic removal with an explicit diagnostic.

To restore experimental Desktop Auto deliberately, disable Native Shadow first, then run `effortlane desktop-enable` and restart Desktop. This is not the recommended stable path and must be validated against that app version.
