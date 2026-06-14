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

1. **Locate config.** The board MCP server name and type come from the repo's `.review.yml`
   `task_board` block (the same one `review-pr` uses). If you do not have it, ask the user which
   board MCP to use, or read `.review.yml` from the repo. Tools are `mcp__<task_board.mcp>__*`.

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
- If the board MCP is not connected or the reviewer MCP server is unavailable, stop and tell the user
  what to connect — do not partially guess.
- Never write back to the board; this skill only reads it.
