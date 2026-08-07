# Brief — PRI-175 solve-task: стандартизованные теги рисков в Constraints

## Task

- Store task `ID-175` (alias `PRI-175`), synced from the reviewer store; formal `criteria=[]` and no acceptance-criteria heading, so requirements come from the description.
- Standardize operational context gaps in `## Constraints / open questions` with the proposed six tags: `[index_stale]`, `[index_unknown]`, `[summaries_missing]`, `[board_unavailable]`, `[boardless]`, `[infra_degraded]`.
- Tags precede one gap per line; `[board_unavailable]` and `[boardless]` are mutually exclusive.
- Scope is SKILL.md-only plus guard assertions in `tests/skills/test_solve_task_brief.py`; do not change reviewer services.

## Related work

- PRI-141 — reuse its preflight triggers: drift, unknown index, board-sync failure, and fail-open infrastructure failure.
- PRI-146 — extend the existing brief skeleton/Constraints contract without weakening its adaptive relevance rule.
- PRI-163 — retain persisted briefs as the searchable artifact that makes consistent tags valuable.
- PRI-176 — preserve its existing-artifact warning semantics; reconcile its `[existing_artifacts]` marker with any closed-tag rule.
- PRI-177 — do not propagate collection-time Constraints verbatim into specs; these tags describe retrieval state, not design facts.
- (dropped 15: adjacent solve-task work concerns unrelated mechanisms such as test coverage, dependencies, dirty trees, or model selection.)

## Subsystems

- `tests/skills` — static assertions read real `plugin/skills/*/SKILL.md`; the right place for durable prompt-contract guardrails.

## Relevant code

- `plugin/skills/solve-task/SKILL.md:250` — brief skeleton currently defines free-form Constraints; add the exact tag vocabulary, trigger mapping, exclusivity, and no-invented-tags rule here.
- `plugin/skills/solve-task/SKILL.md:262-275` — existing-artifact flow already records `[existing_artifacts]`; the rule must explicitly retain it as a separate provenance marker or revise the supposed closed list.
- `tests/skills/test_solve_task_brief.py:16` — existing guard-test module reads the skill text and asserts stable contractual markers; add focused assertions here, not runtime-only tests.
- (dropped 0: this is prompt-contract-only work; reviewer Python, board providers, and graph code are not implementation targets.)

## Test exemplars

- `tests/skills/test_solve_task_brief.py:16` — direct file-read/sub-string assertions are the established pattern for testing the solve-task brief contract.
- (dropped 0: no separate test retrieval result; this local exemplar directly specifies the needed guard style.)

## Constraints / open questions

- Task data came from the reviewer store after sync; criteria are thin, so acceptance behavior must be derived from the description.
- The source task is valuable for a global multi-CLI plugin only if the vocabulary stays small and maps to already-observed control-flow outcomes; it improves brief scanability and cheap grep/CI reporting without runtime mechanics.
- Do not turn every note into a tag: untagged design questions remain prose. Add no new automation, hooks, or reviewer-side schema for this task.
- Resolve the contract conflict before implementation: the existing `[existing_artifacts]` marker is outside the proposed six. Prefer defining the six as the closed set for *operational-risk* gaps while retaining `[existing_artifacts]` as an existing provenance marker; otherwise the new rule would contradict current SKILL.md behavior and its guard test.
- `[board_unavailable]` and `[boardless]` must remain mutually exclusive; a condition with no confirmed trigger should not receive a tag.
- Codebase retrieval returned no indexed snippets for the textual query; cited file locations above were locally verified, so implementation should re-check the current skill text before editing.

Собран на: mid / gpt-5.6-terra, режим: subagent
