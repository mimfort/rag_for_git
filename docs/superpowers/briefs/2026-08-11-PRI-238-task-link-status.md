# Brief — PRI-238 finish_task: отличать «ссылка на задачу уже в PR» от «не удалось добавить»
https://ru.yougile.com/team/686c049c8af8/#PRI-238

## Task
`finish_task` возвращает `task_link_added: false` в двух разных случаях — идемпотентный no-op
(ссылка уже в теле PR) и реальный сбой. Клиент их не различает, скилл finish-task рапортует
пользователю о проблеме, которой не было. Нужно: различать три исхода в payload, обновить
докстринг тула и шаг 6 скилла, покрыть юнит-тестами, сохранить обратную совместимость
`task_link_added` (семантика «мы записали сейчас») и идемпотентность.

## Relevant code
- `reviewer/tasks/pr_backlink.py:57` — `apply_backlink` возвращает `None` при двойной
  идемпотентности (маркер ИЛИ URL задачи в теле); чистая функция, менять не требуется.
- `reviewer/mcp/service.py:819` — `_backlink_pr` → `(bool, warnings)`; три ранних выхода
  (пустой task_url / неразобранный pr_url / исключение VCS) и no-op `return False, []`.
- `reviewer/mcp/service.py:1151` — вызов из `finish_task`, сборка payload `task_link_added`.
- `reviewer/entrypoints/mcp_server.py:146` — докстринг MCP-тула `finish_task`.
- `plugin/skills/finish-task/SKILL.md:32` — шаг 6, правило отчёта.

## Test exemplars
- `tests/mcp/test_finish_task.py:213-280` — оркестрация на фейках `_FakeVCS`/`_Provider`:
  добавлено / идемпотентно / сбой VCS / нет url задачи / неразобранный pr_url.
- `tests/skills/test_finish_task_skill.py:54` — guard на упоминание `task_link_added` и `PR body`.

## Constraints
- Правка `plugin/` → пересобрать codex-манифесты (`scripts/update_codex_plugin_manifest.py` + `--check`).
- Обратная совместимость: `task_link_added` остаётся bool со старой семантикой.
- Никаких миграций БД/схемы.

Собран на: session model (Opus 5), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 40 · out 7.7K · cache-write 178K · cache-read 1.4M
Всего: 1.6M токенов
