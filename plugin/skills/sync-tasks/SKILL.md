---
name: reviewer_sync-tasks
description: Warm the task graph & vector store by indexing a board into the reviewer MCP server. Use when the user asks to sync/index tasks ("sync tasks", "index the board", "просиндексируй задачи") so search_tasks/get_task_context have a corpus. Requires the reviewer MCP server with a board configured server-side (TASK_BOARD_* env).
---

# Sync Tasks

Index the configured task board into the reviewer task graph + vector store so
`search_tasks` and `get_task_context` are useful before many PRs have accrued.

Enumeration, normalization and indexing all happen **server-side**: the reviewer
MCP server connects to the board's REST API itself, normalizes each task into a
`TaskBrief`, and indexes it. You do **not** read the board, you do **not** pass any
task payload — you call one tool and print its summary. A sync costs O(1) LLM tokens
regardless of board size.

Always answer the user in Russian (this skill body is English to save tokens).

## Inputs

Parse from `$ARGUMENTS` (all optional):
- `--board <name>`: limit to one project/board by name.
- `--limit <N>`: index at most N tasks (a quick smoke run). Note: `--limit` also
  disables `--purge-orphaned` and watermark advance (a partial walk can't compute
  the full active-key set), so use it only for smoke runs.
- `--purge-orphaned`: after indexing, delete tasks no longer on the board from the
  store/graph. Off by default.
- `--no-keep-with-prs`: with `--purge-orphaned`, also delete tasks that have PR
  history (`:IMPLEMENTED_BY`). By default such tasks are protected.

## Pipeline

1. **Call the tool once.** Map the parsed arguments to a single call:

   ```
   sync_board(
       board=<--board or null>,
       limit=<--limit or null>,
       purge_orphaned=<True if --purge-orphaned else False>,
       keep_with_prs=<False if --no-keep-with-prs else True>,
   )
   ```

   The server enumerates the board over REST (incremental via a per-board timestamp
   watermark — a repeat sync touches ~0 tasks), normalizes every task into a
   `TaskBrief`, indexes changed ones in a single Voyage batch (dedup by
   `content_hash`), auto-links PRs found in descriptions
   (`:Task-[:IMPLEMENTED_BY]->:PR`), and optionally purges orphans.

2. **Print the summary (in Russian).** The tool returns a compact counts dict:
   `{enumerated, changed, embedded, refreshed, unchanged, failed, purge, warnings,
   cursor_advanced}`. Report these to the user, e.g. «N задач на доске, изменено M
   (эмбеддинги: K), без изменений U, ошибок F». If `purge` is present, add
   «Purge: D удалено, P защищено (есть PR-история)». Surface any `warnings`.

3. **Handle the error case.** If the tool returns `{"status": "error", "reason": ...}`,
   the board is not configured server-side. Tell the user to set `TASK_BOARD_TYPE` +
   `TASK_BOARD_API_KEY` (and optionally `TASK_BOARD_API_BASE`) in the reviewer-mcp
   environment, then retry. Do not attempt to read the board yourself.

## Notes

- The board REST credentials live only in the reviewer-mcp deploy environment
  (`TASK_BOARD_API_KEY`), never in this skill or the conversation.
- This skill is read-only on the board; it never writes back.
- `sync_board` is idempotent: re-running is safe and cheap (watermark + content_hash).
