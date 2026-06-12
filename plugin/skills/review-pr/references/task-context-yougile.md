# Task context playbook — Yougile

Use this when `task_board.type == "yougile"`.

Goal: read the task identified by the resolved key and build a `TaskBrief`.

1. The board MCP server is the one named by `task_board.mcp` (e.g. `yougile`). Its tools are
   exposed as `mcp__<task_board.mcp>__<tool>`.
2. Fetch the task: call `mcp__<task_board.mcp>__get_task` with the resolved key. Yougile accepts
   the human code form such as `SAI-515`.
3. Build the `TaskBrief` from the response (best-effort — omit/empty any field the response lacks):
   - `key`         ← the resolved key
   - `title`       ← task title
   - `description` ← task description text (requirements usually live here)
   - `criteria[]`  ← checklist / subtask titles, if the task has them; else `[]`
   - `status`      ← column / status name
   - `url`         ← task link
   - `links[]`     ← related tasks, if available; else `[]`
4. Optional: `mcp__<task_board.mcp>__get_task_chat` / `..._get_task_messages` add discussion
   context — use ONLY if the description is too thin to judge requirements. Not required.

Failure handling: if the board MCP server is not connected, the tool errors, or the task is not
found, do NOT build a `TaskBrief` — skip the requirements dimension and note the reason in the
summary. Never abort the review.
