# Gemini CLI — reviewer MCP

Плагин подключает MCP-сервер `reviewer` (RAG + граф кода + публикация ревью).
Скилы `/solve-task`, `/review-pr`, `/sync-tasks` вызывают его тулы. Чтобы они
работали без ручного подтверждения на каждый вызов, пометьте сервер `reviewer`
доверенным в Gemini CLI (`~/.gemini/settings.json`, поле `trust` у MCP-сервера)
— тогда все его тулы предодобрены.

## Pre-approved тулы reviewer

- `mcp__reviewer__prepare_review`
- `mcp__reviewer__search_code`
- `mcp__reviewer__get_related_symbols`
- `mcp__reviewer__read_file`
- `mcp__reviewer__get_definition`
- `mcp__reviewer__find_callers`
- `mcp__reviewer__get_changed_file_diff`
- `mcp__reviewer__index_task`
- `mcp__reviewer__index_tasks_batch`
- `mcp__reviewer__purge_orphaned_tasks`
- `mcp__reviewer__search_tasks`
- `mcp__reviewer__get_task_context`
- `mcp__reviewer__get_board_config`
- `mcp__reviewer__search_codebase`
- `mcp__reviewer__publish_review`

Эквивалент для целого сервера — доверять `reviewer` целиком; в Claude Code это
одно правило `mcp__reviewer__*` в `permissions.allow` (см. `.claude/settings.json`).
