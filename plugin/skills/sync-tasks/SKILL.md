---
name: reviewer_sync-tasks
description: Warm the task graph & vector store by indexing a board into the reviewer MCP server. Use when the user asks to sync/index tasks ("sync tasks", "index the board", "просиндексируй задачи") so search_tasks/get_task_context have a corpus. Requires a connected board MCP and the reviewer MCP server.
---

# Sync Tasks

Bulk-index board tasks into the reviewer task graph + vector store so `search_tasks` and
`get_task_context` are useful before many PRs have accrued. You read the board via the connected
board MCP, normalize each task into a `TaskBrief`, and call `index_tasks_batch` once for all tasks. The reviewer
Python never touches the board.

## Inputs

Parse from $ARGUMENTS (all optional):
- `--board <name>`: limit to one board by name.
- `--limit <N>`: index at most N tasks (useful for a first smoke run).
- a board type override; otherwise infer from the connected MCP (Yougile is the reference).
- `--purge-orphaned`: после индексации удалить из store/графа задачи, которых нет на доске.
  По умолчанию off — без флага поведение не меняется.
- `--no-keep-with-prs`: в сочетании с `--purge-orphaned` удалять также задачи с PR-историей
  (`:IMPLEMENTED_BY`). По умолчанию такие задачи защищены.

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

3. **Normalize + index.** Build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}` for every enumerated task
   using the SAME mapping as `../review-pr/references/task-context-<type>.md`, then call
   `index_tasks_batch([...all TaskBriefs...])` in a **single tool call**.
   Result: `list[{key, embedded, links_upserted, warnings}]` in input order.
   `index_tasks_batch` is idempotent (re-embeds only tasks whose text changed) and uses a single
   Voyage embedding call for all changed tasks — O(1) Voyage API calls regardless of board size.

   > **НИКОГДА не вызывай `index_task` в цикле по задачам.** Собери все `TaskBrief` в один
   > список и сделай **один** `index_tasks_batch(...)`. Для очень больших досок — чанками по
   > ≤25 задач за вызов (всё равно O(число чанков) вызовов Voyage, а не O(N)). Одиночный
   > `index_task` существует только для сценария одной задачи (`solve-task`) — для синка он
   > запрещён: поштучный цикл упирается в Voyage 3 RPM и приводит к таймауту.

4. **Purge orphaned tasks** *(только при `--purge-orphaned`).*

   Собери все канонические ключи (`idTaskCommon`, вида `ID-N`) задач, прочитанных с доски
   на шаге 2. Вызови:

   ```
   purge_orphaned_tasks(
       active_keys=[...все ID-N с доски...],
       keep_with_prs=<True, если НЕ задан --no-keep-with-prs>
   )
   ```

   Включи результат в итоговый summary.

5. **Report.** Print a summary: indexed (embedded), refreshed (unchanged → metadata only), failed,
   and any `warnings` from each entry returned by `index_tasks_batch` (e.g. "graph unavailable").
   
   При активном `--purge-orphaned` включи в summary строку:
   ```
   Purge: N deleted (store+graph), M protected (have PR history), K warnings.
   ```

## Rate limits & failure handling (fail-open)

- Voyage free tier is 3 RPM / 10K TPM; `index_tasks_batch` makes a single `embed_documents` call
  for all changed tasks and retries/backs off internally, so a large board is fast on first sync
  and near-instant on repeat syncs (only changed tasks are re-embedded). Use `--limit` for a quick
  smoke run.
- A single task that fails to read or index must NOT stop the sync: `index_tasks_batch` returns a
  per-task result list — check each entry's `warnings` field and log failures; continue.
- If no `task_board` is configured anywhere (no `.review.yml` block AND `get_board_config()` →
  `null`) or the board MCP is not connected, stop and tell the user what to connect — do not
  partially guess. Mention the deploy-wide option: set `TASK_BOARD_*` in the reviewer `.env` once
  instead of adding `.review.yml` to every repo.
- Never write back to the board; this skill only reads it.
