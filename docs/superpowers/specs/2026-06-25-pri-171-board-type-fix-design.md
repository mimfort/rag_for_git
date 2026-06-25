# PRI-171: Фикс `type` vs `mcp` в конфиге доски + per-board статистика sync-tasks

**Дата:** 2026-06-25  
**Задача:** PRI-171 — Проблема sync_task и get_task  
**Статус:** Дизайн согласован

## Проблема

Два независимых дефекта:

**A. Путаница `type` vs `mcp`.** `TASK_BOARD_TYPE=yougile, youtrack` в env →
`get_board_config()` возвращает `{"type": "yougile, youtrack", "mcp": "yougile", ...}`.
Слабые модели (воспроизведено на deepseek v4 pro max) видят `mcp: "yougile"` и передают
`sync_board(board_type="yougile")` вместо `"youtrack"` из `.review.yml`. Сервер синкает не ту
доску.

**B. Агрегированная статистика.** `SyncService.run()` суммирует задачи всех досок в одну цифру.
Пользователь не видит сколько задач пришло именно с нужной доски/проекта.

## Решение

### A. `.review.yml` — источник истины для `board_type`

**Принцип:** скилл явно читает `task_board.type` из `.review.yml` и передаёт его в
`sync_board(board_type=...)`. Сервер фильтрует провайдеров по типу. LLM никогда не угадывает тип.

**Структура `.review.yml` не меняется** — поле `task_board.type` уже существует.

#### Поток данных

До (сломано):
```
TASK_BOARD_TYPE=yougile, youtrack в env
get_board_config() → {type: "yougile, youtrack", mcp: "yougile"}
LLM угадывает → sync_board(board_type="yougile") → синкает не ту доску ❌
```

После (исправлено):
```
.review.yml: task_board.type: youtrack
скилл читает type → sync_board(board_type="youtrack") → синкает YouTrack ✅
get_board_config() → {type: "youtrack", mcp: "yougile"}
  (type из configured_board_types() по факту наличия YOUTRACK_TOKEN)
```

#### Изменения Python

**`reviewer/tasks/sync.py` — `SyncService`:**
- `run(board_type=None, ...)`: при задан `board_type` — фильтрует `self._providers` по
  `provider.board_type == board_type`; `board_type=None` → синк всех (backward-compat)

**`reviewer/mcp/service.py` — `MCPReviewService`:**
- `sync_board(board_type=None, ...)`: принимает и прокидывает `board_type` в `sync.run()`

**`reviewer/entrypoints/mcp_server.py` — MCP-тул `sync_board`:**
- Добавить параметр `board_type: str | None = None`
- Docstring: явно указать что `board_type` берётся из `task_board.type` репо (`.review.yml`)

**`reviewer/config/settings.py` — `task_board_default()`:**
- Убрать `cfg["type"] = self.task_board_type` (из env-переменной)
- Заменить на вывод из `configured_board_types()`:
  - один тип → `cfg["type"] = "youtrack"` (строка)
  - несколько → `cfg["type"] = ["yougile", "youtrack"]` (список)
  - ноль → ключ `type` не добавляется

**`reviewer/install.py` — env template:**
- Убрать строку `TASK_BOARD_TYPE=` из шаблона
- Оставить комментарий: тип доски задаётся в `.review.yml` каждого репо, а не в env

Поле `task_board_type: str = ""` в pydantic **оставить** — молча игнорировать в
`task_board_default()`, чтобы старые деплои с заполненным `TASK_BOARD_TYPE` не ломались.

#### Изменения скиллов

**`plugin/skills/solve-task/SKILL.md` — step 0.3 (preflight):**

```
sync_board(
    board_type=<task_board.type or null>,   # ← добавить
    board=null,
    limit=null,
    purge_orphaned=false,
)
```

`task_board.type` резолвится из `.review.yml` репо (уже читается в step 1 — Config).
`null` если не задан (синк всех).

**`plugin/skills/sync-tasks/SKILL.md` — Pipeline:**
- Step 1: дополнить резолв `task_board` — извлечь `type` из `.review.yml` CWD-репо
  (аналогично step 1 solve-task)
- Step 2: добавить `board_type=<type or null>` в вызов `sync_board`

### B. Per-board breakdown в ответе `sync_board`

#### Формат ответа `SyncService.run()`

Добавить ключ `by_board` — список per-provider статистики:

```json
{
  "enumerated": 64,
  "changed": 2,
  "embedded": 0,
  "refreshed": 2,
  "unchanged": 62,
  "failed": 0,
  "warnings": [],
  "cursor_advanced": true,
  "purge": null,
  "by_board": [
    {
      "board_type": "youtrack",
      "board": "PRI",
      "enumerated": 64,
      "changed": 2,
      "embedded": 0,
      "refreshed": 2,
      "unchanged": 62,
      "failed": 0
    }
  ]
}
```

`by_board` — backward-compatible новое поле. Агрегаты в корне остаются.

#### Изменения `sync-tasks` SKILL.md — step 2 (вывод)

Текущий формат: `«N задач на доске, изменено M ...»`

Новый формат при наличии `by_board`:
```
Синк завершён:
  youtrack / PRI: 64 задачи, изменено 2 (эмбеддинги: 0), без изменений 62
Итого: 64 задачи, изменено 2.
```

Если `by_board` отсутствует (старый сервер) — fallback к текущему формату (агрегаты).

## Граничные случаи

| Ситуация | Поведение |
|---|---|
| `.review.yml` нет / `task_board` не задан | `board_type=null` → синк всех провайдеров (backward-compat) |
| `configured_board_types()` → один тип | `get_board_config().type = "youtrack"` (строка, чисто) |
| Оба типа настроены, `.review.yml` нет | `type = ["yougile", "youtrack"]`, скилл передаёт `board_type=null` → синк обоих |
| `TASK_BOARD_TYPE` в старом `.env` | Поле pydantic молча игнорируется в `task_board_default()` |
| `sync_board` без `board_type` | Фильтрации нет — синк всех (текущее поведение) |

## Тесты

**`tests/tasks/test_sync.py`:**
- Два провайдера (mock yougile + youtrack), `run(board_type="youtrack")` → вызывается только
  youtrack-провайдер
- `by_board` присутствует в ответе, содержит одну запись с правильными counts
- `run(board_type=None)` → вызываются оба провайдера (backward-compat)

**`tests/config/test_settings.py`:**
- `task_board_default()` без `TASK_BOARD_TYPE`: тип из `configured_board_types()`
  (один тип → строка, два → список, ноль → нет ключа `type`)
- `TASK_BOARD_TYPE` задан в env → игнорируется (не попадает в `task_board_default()`)

**`tests/skills/` (структурные):**
- `sync_tasks_guardrail`: скилл упоминает `board_type` и читает `.review.yml` / `task_board`
- `solve_task_brief` (preflight-секция): `sync_board` вызывается с `board_type`

## Scope

Этот спек **не** реализует полный scoping задач по `.review.yml` (PRI-170: ограничение
`search_tasks`/`get_task_context` по проекту). Только:
- фикс `board_type` при синке (запись)
- per-board статистика в выводе
