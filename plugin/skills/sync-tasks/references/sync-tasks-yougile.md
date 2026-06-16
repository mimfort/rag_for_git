# Sync playbook — Yougile

Use when `task_board.type == "yougile"`. Tools are `mcp__<task_board.mcp>__<tool>`.

Goal: enumerate the board's tasks, normalize each one via the mapping in
`../../review-pr/references/task-context-yougile.md` (same `TaskBrief` mapping), then call
`index_tasks_batch` once with all tasks.

## 1. Enumerate tasks

1. `get_projects` → projects; if `--board <name>` is given, keep the matching project/board only.
2. For each project, `get_boards` → boards; for each board, `get_columns` → columns (also gives you
   column titles to resolve `status` without an extra `get_column` per task).
3. For each column, list its tasks. Use the available listing tool (e.g. `get_tasks` /
   `get_user_tasks` / the column's task ids); fetch each task with `get_task` to get the full object.

Apply `--limit N`: stop after N tasks total.

## 2. Normalize each task

Build the `TaskBrief` exactly as in `task-context-yougile.md`:
- `key` ← `idTaskCommon` (`ID-N`); `aliases` ← `[idTaskProject]` (`PRI-N`);
- `title` ← `title`; `description` ← `description`;
- `status` ← the column title from step 1.2 (you already have it — no extra call);
- `criteria[]` ← inline checklist in `description` if any, else `[]`;
- `links[]` ← union of two sources (deduplicate by key):
  - one `{type:"subtask", key, title}` per `subtasks[]` UUID (resolve title via `get_task`,
    best-effort; a failed fetch is skipped);
  - one `{type:"related", key}` per match of `task_board.key_pattern` found anywhere in
    `description`, excluding the task's own `key`/`aliases` and any keys already covered by
    subtasks above. No extra `get_task` needed — the key alone is sufficient for the graph edge.
- `url` ← `task_board.url_template` with the project code (`PRI-N`) if a template is configured, else
  `null`.

## 3. Index

Collect all normalized `TaskBrief` objects into a list, then make a **single call**:
`index_tasks_batch([...all TaskBriefs...])`.

The result is `list[{key, embedded, links_upserted, warnings}]` in input order. Accumulate the
counters for the final report (`embedded` true/false per entry, `warnings`). A failure on one task
is reflected in that entry's `warnings` field — keep going with the rest.
