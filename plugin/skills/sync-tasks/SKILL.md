---
name: reviewer_sync-tasks
description: Warm the task graph & vector store by indexing a board into the reviewer MCP server. Use when the user asks to sync/index tasks ("sync tasks", "index the board", "просиндексируй задачи") so search_tasks/get_task_context have a corpus. Requires a connected board MCP and the reviewer MCP server.
---

# Sync Tasks

Bulk-index board tasks into the reviewer task graph + vector store so `search_tasks` and
`get_task_context` are useful before many PRs have accrued. You read the board via the connected
board MCP, normalize each task into a `TaskBrief`, and call `index_task` per task. The reviewer
Python never touches the board.

## Inputs

Parse from $ARGUMENTS (all optional):
- `--board <name>`: limit to one board by name.
- `--limit <N>`: index at most N tasks (useful for a first smoke run).
- a board type override; otherwise infer from the connected MCP (Yougile is the reference).

## Pipeline

1. **Locate config.** Resolve the board `task_board` block (`type`, `mcp`, `key_pattern`,
   `url_template`) in this order:
   1. the repo's `.review.yml` `task_board` block (the same one `review-pr` uses), if present;
   2. otherwise the deploy-wide default — call `get_board_config()` (reviewer MCP); it returns
      `{"task_board": {...} | null}` from the server's `TASK_BOARD_*` env. This is the normal case:
      the board is configured once in the reviewer deploy, so **a per-repo `.review.yml` is NOT
      required** just to sync tasks.
   3. if both are absent (`null`), ask the user which board MCP to use.
   The board's tools are `mcp__<task_board.mcp>__*`.

2. **Iterate the board.** Follow `references/sync-tasks-<type>.md` (Yougile is the reference) to
   enumerate tasks. Apply `--board` / `--limit` if given.

3. **Normalize + index.** For each task, build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}` using the SAME mapping as
   `../review-pr/references/task-context-<type>.md`, then call `index_task(TaskBrief)`.
   `index_task` is idempotent (it re-embeds only when the task text changed), so re-running is cheap.

4. **Report.** Print a summary: indexed (embedded), refreshed (unchanged → metadata only), failed,
   and any `warnings` returned by `index_task` (e.g. "graph unavailable").

## Rate limits & failure handling (fail-open)

- Voyage free tier is 3 RPM / 10K TPM; embedding inside `index_task` already retries/backs off, so a
  large board simply runs slower — that is expected, not an error. Use `--limit` for a quick first
  pass.
- A single task that fails to read or index must NOT stop the sync: log it and continue.
- If no `task_board` is configured anywhere (no `.review.yml` block AND `get_board_config()` →
  `null`) or the board MCP is not connected, stop and tell the user what to connect — do not
  partially guess. Mention the deploy-wide option: set `TASK_BOARD_*` in the reviewer `.env` once
  instead of adding `.review.yml` to every repo.
- Never write back to the board; this skill only reads it.
