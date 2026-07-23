---
name: reviewer_create-task
description: Create a task on the connected board (YouGile / YouTrack) with a canonical, LLM-readable structure — server-side write via the reviewer MCP tool create_task. Use when the user asks to file/create a task ("заведи задачу", "создай тикет", "file a task", "create a task on the board"). Requires the reviewer MCP server + a configured board.
---

# Create Task

File a new task on the board from a structured draft. The body (Проблема / Что сделать /
Критерии приёмки / Контекст) is assembled **server-side**, so every client produces the same
shape and the board-specific markup conversion is not your problem. Reply to the user in Russian.

## Pipeline

1. **Config.** Read the `task_board` block (`type`, `project`, `status_field`) from the repo's
   `.review.yml`; if there is no block, fall back to `get_board_config()`. Nothing anywhere →
   **board-less no-op**: tell the user (in Russian) that no board is configured and stop.

2. **Draft the body.** Turn the user's request into four fields:
   - `problem` — what is broken or missing, grounded in the code: cite `path:line` from
     `search_codebase(...)` (and `callers`/`definition` when the blast radius matters) instead of
     paraphrasing. Never invent a path you have not seen in a tool result.
   - `steps` — concrete actions, one per list item.
   - `criteria` — acceptance criteria, one per item, each checkable.
   - `context` — links, related task keys, the origin of the request.
   Plain technical Russian: **no emoji**, no decorative separators, no marketing tone.

3. **Resolve the target.** Call `get_board_targets(board_type=<type>, project=<project>)` and pick
   the column (YouGile) / status value (YouTrack) that matches the task's topic; on a thematic
   board the right column is a judgment call, so propose one and let the user correct it. Empty
   discovery result → create without a target.

4. **Confirm.** Show the title, the resolved target and the full body text; write **only** after
   explicit confirmation. Never write to the board silently.

5. **Write.** Call `create_task(title=…, problem=…, steps=[…], criteria=[…], context=…,
   board_type=<type>, project=<project>, target=<column or status>,
   status_field=<status_field or null>)`. `status == "error"` → report the reason in Russian,
   fail-open.

6. **Re-index.** Call `sync_board(board=<project or null>, board_type=<type>,
   status_field=<status_field or null>)` so the new task is in the corpus for `search_tasks` /
   `get_task`. Cheap when the corpus is warm (the write-through already indexed it; this keeps
   the watermark honest).

7. **Report.** Give the user (in Russian) the task key, its URL and any `warnings` — in
   particular when the requested column was not found and the task landed elsewhere.

## Failure handling (fail-open)

- No board configured → board-less no-op with a short Russian note; never abort.
- `create_task` error (board unreachable, project unknown, key unresolved) → report the reason
  and stop.
- Read-only intent everywhere except the single confirmed `create_task` write.
