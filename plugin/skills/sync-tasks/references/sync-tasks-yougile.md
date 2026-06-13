# Sync playbook — Yougile

Use when `task_board.type == "yougile"`. Tools are `mcp__<task_board.mcp>__<tool>`.

Goal: enumerate the board's tasks and hand each one to the normalization in
`../../review-pr/references/task-context-yougile.md` (same `TaskBrief` mapping), then `index_task`.

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
- `links[]` ← one `{type:"subtask", key, title}` per `subtasks[]` UUID (resolve title via `get_task`,
  best-effort);
- `url` ← `task_board.url_template` with the project code (`PRI-N`) if a template is configured, else
  `null`.

## 3. Index

Call `index_task(TaskBrief)`. Accumulate the result counters (`embedded` true/false, `warnings`) for
the final report. A failure on one task is logged and skipped — keep going.
