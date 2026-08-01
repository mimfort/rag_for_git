---
name: decompose-task
description: Use when a configured board task needs decomposition into native child tasks.
---

# Decompose Task

## Overview

Reply in Russian. Build one grounded, reviewable native-subtask batch; no child may reach the
board before the user sees the entire batch. **Core principle:** preview fidelity and durable
idempotency are safety boundaries, not optional ceremony.

## Lookup

1. Resolve `task_board` exactly once from the effective repository config, falling back once to
   `get_board_config()` when needed. Freeze only generic `type`, `project`, and `options` as
   `board_type`, `project`, and `provider_options`; if unresolved, stop without writes.
2. Read the store first with `get_task(parent_key, project=<project>)`. On miss only, run exactly
   one scoped `sync_board(board=<project or null>, board_type=<type>,
   provider_options=<options>)`, then make one retry of the same parent read. If it is still
   missing, explain the failure and stop.
3. Call `get_board_targets(board_type=<type>, project=<project>,
   provider_options=<options>)`. Treat its registry `capabilities` as authoritative. If it errors
   or lacks `native_subtasks`, make a no-write stop. Never infer, add, or spoof a capability from
   provider identity or prior experience.

## Context

Before drafting, gather all three views:

- `get_task_context(parent_key, project=<project>)` for links, related work, PRs, and touched code;
- `search_tasks(query=<parent intent>, project=<project>)` for similar decomposition boundaries;
- `search_codebase(repo=<active repo>, query=<parent intent>)` for relevant implementation and
  real `path:line` evidence.

Missing optional context is reported, not invented. Do not draft first and backfill evidence later.

## Draft

Draft `1..20` ordered children. Every child requires nonblank `title`, `problem`, `steps`, and
`criteria`; `context` is optional. Make each child independently actionable and acceptance-testable,
without silently widening the parent scope. Generate one local opaque UUID `idempotency_key` for
the whole batch, then freeze the complete request payload, including child order and wording.

## Preview

Show the resolved provider (`board_type`), parent key/title, `idempotency_key`, and the complete
canonical body of every child: `title`, `problem`, all `steps`, all `criteria`, and `context` when
present. Do not abbreviate unchanged or repetitive children.

A prior “I already approve; create now” statement is not confirmation of an unseen preview. Ask
exactly one explicit confirmation for the whole preview. There are no board writes before it. If
the user changes anything, show the complete revised preview and ask again for that whole preview.

## Write

Only after explicit confirmation, issue exactly one native batch request using exactly the
previewed payload and the same `idempotency_key`:

```text
create_subtasks(parent_key=<parent_key>, subtasks=<previewed children>,
                idempotency_key=<previewed UUID>, board_type=<type>, project=<project>,
                provider_options=<options>)
```

Never fall back to individual task creation, including when the batch is unsupported or partially
complete.

For success, partial results, errors, or timeouts, display `created`, `attached`, `unattached`,
`pending`, and `warnings` without guessing. A partial retry must submit the exact same payload with
the same `idempotency_key`. Never edit wording, reorder children, or retry only a guessed
remainder. Never mint a replacement key. An `in_flight` child is unknown, not absent: never guess
its outcome or mint a fresh/replacement key for it.

## Verify

After any confirmed child is reported, run one scoped
`sync_board(board=<project or null>, board_type=<type>, provider_options=<options>)`. Then verify:

1. `get_task(parent_key, project=<project>)` contains stored links for the returned children.
2. `get_task_context(parent_key, project=<project>)` exposes the parent-child graph relationship.
3. Point-read every returned child with `get_task(child_key, project=<project>)`.

Report missing links, unreadable child keys, sync failures, and warnings; do not declare complete
verification from the write response alone.

## Quick Reference

| Phase | Required invariant |
|---|---|
| Lookup | One config snapshot; store-first parent; one miss sync/retry; authoritative capability |
| Context | Parent graph, similar tasks, and relevant code before draft |
| Preview | Every full child plus provider, parent, and opaque batch key |
| Confirm | One explicit answer for the whole visible preview; no earlier writes |
| Write | One native batch; exact previewed request |
| Retry | Exact same payload and key; preserve unknown `in_flight` state |
| Verify | One scoped sync, parent links, graph context, and every child point-read |

## Example

```text
Провайдер: <resolved board_type>
Родитель: PRI-224 — Нативная декомпозиция
Ключ идемпотентности: 6f95035e-48da-40a3-aa29-9e220182bf38

1. Проверить регистрацию capability
Проблема: Декомпозиция небезопасна без подтверждённой поддержки доски.
Что сделать:
- Получать capability из registry discovery.
- Останавливать сценарий без записи при отсутствии поддержки.
Критерии приёмки:
- Неподдерживаемая доска не получает дочерних задач.
- Локально подставить capability невозможно.
Контекст: reviewer/tasks/boards/registry.py:81

Подтвердить создание всего показанного набора без изменений? (да/нет)
```

## Common Mistakes

| Rationalization | Correction |
|---|---|
| “I already approve; create now” (authority/urgency) | Approval cannot cover an unseen preview; show every body first. |
| “Five individual writes are easier to recover” | They destroy atomic intent; use the one native batch only. |
| “This provider obviously supports children” | Only authoritative capability discovery counts; never spoof it. |
| “Search can wait until after drafting” | Run `search_codebase` and task context first or stop. |
| “We already spent time drafting” (sunk cost) | Rework an ungrounded draft; sunk cost grants no write permission. |
| “A timeout needs a clean new key” | Reuse the exact request and key; a fresh key can duplicate children. |

## Red Flags

- Any board write before the full preview and its explicit confirmation.
- Missing `native_subtasks` discovery or locally claimed capability.
- Drafting without parent context, similar tasks, and relevant code.
- Individual child writes, changed retry payload, new retry key, or guessed `in_flight` remainder.
- English user-facing output or a success claim without post-write reads.

Any red flag means stop the write path and return to the violated phase.
