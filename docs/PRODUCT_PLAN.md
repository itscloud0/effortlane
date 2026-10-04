# Effortlane: adoption and proof

Status: experimental, 2026-10-05. This is a delivery plan, not measured results.

## First customer and promise

Start with macOS developers using Codex heavily on a ChatGPT subscription.
Their problem is a weekly limit reached before the work is done, plus uncertainty
about model and effort choices. Keep one initial use case: native Codex CLI with
Shadow evaluation. Desktop/Remote remain native; Claude is an experimental
observer, not a production Auto router.

Positioning: **Model and effort decisions you can inspect before you trust.**
Goal: more accepted engineering work per subscription allowance window, with no
material quality or completion-time regression. Neither API-equivalent dollars,
Shadow suggestions nor GitHub stars establish that result.

## Installation acceptance

The one-command installer uses existing macOS/Python/Codex prerequisites and an
existing native login. It requests the decision-service key once with hidden
input. No sudo, per-repo setup, dependency installation or shell-profile edits.
Re-running checks the installation rather than resetting it. Keep the reviewed
checkout option and allow `EFFORTLANE_REF` to select a reviewed commit/tag.

Before describing setup as seamless, verify a clean Mac/account installation,
a second run, hidden key entry, missing prerequisites, failed download,
manual config edits, reboot, native Codex update, rollback, and native tools.
Archive/delegation and bootstrap fixtures cover only part of that matrix.
A source-upgrade command is still missing: installer reruns do not upgrade.
Do not hide that gap behind a new command name.

## Quality and economy gates

Use the existing [prospective evaluation protocol](EVALUATION.md), not another
unlinked stream of Shadow decisions. Preassign matched task groups, pin client
and policy versions, define acceptance checks first, and include follow-up fixes.
Separate routing decisions from verified executor calls and expose missing data.

Release evidence should report:

- Successful first setup and time to first recorded proposal, without counting
  fixture runs as real user installations.
- Accepted tasks, defects, rework, abandonment and completion-time distributions.
- Complete-turn token coverage; actual input/cache/output/reasoning counters;
  switches and resume/compaction cases; Jev latency, usage and actual billed cost.
- Subscription meter changes across separate reset windows, other account use,
  task mix and uncertainty. No dollar conversion of the fixed monthly fee.
- Model/effort selection by task group, with reasons for held or rejected routes.

Select a material regression tolerance before collecting comparison outcomes.
If coverage or uncertainty cannot rule out that regression, publish inconclusive
results. “Best on the market” requires a direct reproducible comparison against
named alternatives on the same tasks; no such evidence exists yet.

## First distribution loop

Recruit 5–10 heavy Codex users through the author's existing audience. Help them
try one actual task, ask whether setup succeeded, and collect opt-in aggregate
outcomes. Do not collect source, raw prompts, transcripts or credentials. No
automatic upload. Treat setup failure reports as product work before spending
on promotion.

Publish an English build note with the installer, support boundary and evidence
page. Follow with a real task walkthrough showing the native executor, route
proposal, acceptance check, latency and cache coverage. Only publish screenshots
or user quotes with permission. A success story needs the baseline and rework,
not just a cheaper proposed model.

Measure install-to-first-proposal and repeat usage after seven days. Today there
is no verified funnel/retention cohort; star growth is not product retention.
Expand channels after testers complete tasks and keep using it. No paid ads or
broad launch until installation/update and native-tool compatibility gates pass.

## Next delivery order

1. Validate the one-command setup on a clean Mac; retain idempotent recovery.
2. Implement a backed-up, ownership-safe source upgrade and version reporting.
3. Run the existing task/allowance comparison with complete coverage and outcomes.
4. Validate native Claude Shadow, then evaluate a separate Claude Auto adapter.
5. Publish reproducible results and a task walkthrough; scale promotion only
   where repeat usage shows demand.
