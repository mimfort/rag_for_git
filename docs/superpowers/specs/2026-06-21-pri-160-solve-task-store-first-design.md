# PRI-160 / ID-160 — store-first чтение одиночной задачи в solve-task

**Дата:** 2026-06-21
**Статус:** дизайн одобрен, готов к плану
**Задача:** [PRI-160](https://ru.yougile.com/team/686c049c8af8/#PRI-160) — «solve-task: читать одиночную задачу из своего стора (после sync), а не через сторонний board-MCP»

## Проблема

В пайплайне `solve-task` шаг 2 («Identify the task») читает одиночную задачу через
сторонний board-MCP (`mcp__yougile__*`) на стороне LLM по плейбуку
`task-context-<type>.md`. При этом в preflight (шаг 0.3) **прямо перед** шагом 2 уже
отработал `sync_board(...)` — server-side ETL, который перечислил всю доску по REST и
проиндексировал её в наш стор (Postgres `tasks`). То есть задачу мы фактически
выкачиваем дважды: второй раз — лишней зависимостью от board-MCP и лишними токенами LLM.

**Что выяснили по коду:**

- Узел `:Task` в Neo4j хранит только `key/codes/title/status/url` + рёбра
  (`TASK_LINK`, `IMPLEMENTED_BY`, `TOUCHES`). **Описания и критериев в графе нет** —
  достать задачу «из графа» в чистом виде нельзя.
- Полный текст задачи **уже лежит в Postgres** — таблица `tasks`
  (`reviewer/tasks/store.py::TaskStore`) после `sync_board` содержит `description`,
  `text`, `status`, `url`, `aliases`, эмбеддинг. Но **нет read-пути по ключу**:
  `get_task_context(key)` отдаёт только граф-обход (без описания), `search_tasks` —
  только `key/title/status/score`. Данные есть, не хватает инструмента чтения.

Вывод: источник store-first — **собственный стор reviewer (Postgres `tasks`)**, граф —
только для links/PRs (и остаётся за существующим `get_task_context`).

## Решение (обзор)

Новый read-путь по ключу: `TaskStore.get_task` → `TaskService.get_task` →
`MCPReviewService.get_task` → MCP-тул `get_task(key)`. Скилл `solve-task` шаг 2 —
**store-first**: сначала reviewer `get_task`, фолбэк на board-MCP при промахе.

### Принятые решения (брейншторм)

| Развилка | Решение | Почему |
|---|---|---|
| Свежесть/устаревание | **Фолбэк только при miss** (задачи нет в сторе). Без `updated_at`. | Preflight `sync_board` обновляет стор за секунды до чтения — устаревание практически не возникает. Per-task timestamp — преждевременная сложность (YAGNI). |
| Критерии | **`criteria=[]`**, требования несёт `description`. Без колонки/парсинга. | Совпадает с текущим board-MCP-путём (плейбук сам оставляет `criteria` пустым при инлайн-критериях). Колонки `criteria` в `tasks` нет; критерии свёрнуты в `text`. |
| Объём `get_task` | **Только контент задачи из стора** (`{key, aliases, title, description, status, url, criteria:[]}`). Links/PRs/код — за существующим `get_task_context`. | Чёткая single-responsibility, нет дубля с `get_task_context` (его скилл и так зовёт в шаге 3). Минимум кода. |

## Архитектура — 4 слоя кода + правка скилла

### 1. Стор — `reviewer/tasks/store.py`

Добавить метод `TaskStore.get_task(key: str) -> TaskRow | None`:

```sql
SELECT key, aliases, title, description, status, url, content_hash, text, embedding
FROM tasks WHERE key = %s OR %s = ANY(aliases) LIMIT 1
```

- Возвращает существующий dataclass `TaskRow` (переиспользуем; `embedding` декодируется
  уже сконфигурированным `register_vector` на пуле) или `None`, если строки нет.
- **Матчинг `key OR alias` обязателен.** Стор ключует по каноническому `ID-N`
  (`idTaskCommon`), а скилл/PR обычно передаёт проектный `PRI-N` (`idTaskProject`),
  который лежит в `aliases` (`text[]`). Без alias-матчинга store-first промахивался бы
  на каждом `PRI-N`. Параметр передаётся дважды (`key = %s OR %s = ANY(aliases)`).
- Задачи **глобальны** (без repo/branch-скоупа) — метод принимает только `key`.

### 2. Сервис — `reviewer/tasks/service.py`

Добавить `TaskService.get_task(key: str) -> dict | None`:

- Зовёт `self._store.get_task(key)`; если `None` → возвращает `None`.
- Маппит `TaskRow` → нормализованный TaskBrief-словарь:
  `{key, aliases, title, description, criteria: [], status, url}`.
- **Fail-soft:** ошибка стора → `log.warning(..., exc_info=True)` + возврат `None`
  (чтобы скилл фолбэкнул). Образец — `get_task_context` (`service.py:225`).
- Граф **не** трогаем (решение store-only).

### 3. MCP-сервис — `reviewer/mcp/service.py`

Добавить `MCPReviewService.get_task(key: str) -> dict | None` — однострочный делегат:

```python
def get_task(self, key: str) -> dict | None:
    """Прочитать нормализованный TaskBrief задачи из стора (store-first /solve-task)."""
    return self.components.task_service.get_task(key)
```

Образец — соседние делегаты `index_task`/`get_task_context` (`service.py:304–318`).

### 4. MCP-сервер — `reviewer/entrypoints/mcp_server.py`

Зарегистрировать новый тул в `create_server`:

```python
@mcp.tool()
def get_task(key: str) -> dict | None:
    """Read one task's own normalized content from the reviewer store (filled by
    sync_board): {key, aliases, title, description, status, url, criteria}.
    Store-first single-task read for /solve-task — no board-MCP needed.
    Returns null if the task is not in the store (caller falls back to the board).
    For linked tasks / PRs / touched code use get_task_context instead."""
    return service.get_task(key)
```

- Докстринг **чётко отделяет** от `get_task_context` («own content из стора» vs
  «граф-окружение»), чтобы LLM не путала близкие имена.
- Обновить число тулов в докстринге `create_server` («20 тулов» → «21 тул»).
- Miss → тул возвращает `null` (сериализация `None`).

## Поток данных

```
solve-task шаг 2 (ключ совпал с key_pattern)
  → reviewer get_task(key)
    → MCPReviewService.get_task
      → TaskService.get_task
        → TaskStore.get_task  (SELECT по key/alias)
          → TaskRow | None
        → TaskBrief dict | None
  ХИТ  → скилл использует поля напрямую, index_task ПРОПУСКАЕТ (задача уже в графе/сторе)
  MISS (null) → фолбэк: board-MCP-плейбук → TaskBrief → index_task (текущее поведение)
```

## Правка скилла — `plugin/skills/solve-task/SKILL.md`, шаг 2

Текущий шаг 2 целиком уходит под **фолбэк**; перед ним вставляется store-first:

- Если `<input>` совпал с `key_pattern` доски:
  1. **Store-first:** вызвать reviewer `get_task(key)`.
     - **Хит** (вернулся объект): использовать его поля как `TaskBrief`, пометить
       provenance «данные из стора reviewer (после sync)». Задача **уже
       проиндексирована** preflight-синком → **`index_task` НЕ вызывать**.
     - **Miss** (`null`) **и** доска настроена/подключена → читать по плейбуку
       `../review-pr/references/task-context-<task_board.type>.md`, построить
       `TaskBrief`, вызвать `index_task(TaskBrief)` (**текущее поведение целиком**).
     - Оба не сработали → board-less (трактовать `<input>` как описание).
  2. Free-text вход (не ключ) — без изменений: трактовать как описание задачи.
- Board-MCP-фолбэк **сохраняется целиком** — для досок без REST-провайдера и при miss.

## Обработка ошибок (fail-open сквозной)

- Стор недоступен / SELECT упал → `TaskService.get_task` логирует и возвращает `None`
  → скилл фолбэкает на board-MCP.
- Тул **никогда не бросает** исключение в LLM — на любой miss/ошибку отдаёт `null`.
- Граф в `get_task` не участвует — Neo4j down на этот путь не влияет.

## Тестирование

- **`tests/tasks/test_service.py`** (unit, фейк-стор — существующий паттерн):
  - `get_task` хит → нормализованный TaskBrief (`criteria=[]`, поля проброшены);
  - `get_task` miss → `None`;
  - матч **по alias** (`PRI-N` находит задачу, ключованную по `ID-N`);
  - fail-soft: исключение стора → `None` (без проброса).
- **`tests/tasks/test_integration.py`** (маркер `integration`): добавить проверку
  `TaskStore.get_task` на реальном Postgres — round-trip `upsert_task` → `get_task`
  по key и по alias.
- **MCP-делегат + регистрация тула**: по образцу существующих тестов `get_task_context`
  (если есть) в `tests/mcp/` / `tests/entrypoints/` — тул зарегистрирован, делегирует в
  сервис, miss → `null`.

## Что НЕ делаем (YAGNI / границы)

- Без колонки `updated_at` и per-task детекта устаревания.
- Без колонки `criteria` и без парсинга критериев из `text`/`description`.
- Без merge графа (links/PRs) в `get_task` — остаётся за `get_task_context`.
- **Board-MCP-фолбэк не удаляется** — нужен для досок без REST-провайдера.
- Инвариант «reviewer Python не трогает доску для одиночного чтения» **сохранён**:
  читаем собственный стор, а не доску.

## Инварианты / зависимости

- Store-first требует, чтобы `sync_board` отработал (нужен `TaskBoardProvider` +
  серверные креды `TASK_BOARD_API_KEY/BASE`). Для досок без REST остаётся только
  board-MCP-путь — поэтому фолбэк обязателен.
- Имя `get_task` на reviewer-MCP свободно (коллизия только с board-MCP
  `mcp__yougile__get_task` — другой сервер); близость к `get_task_context` снимается
  докстрингом.
- Связанные работы: **ID-140** (server-side ETL `sync_board` — фундамент этой задачи),
  **ID-141** (preflight `solve-task` — образец правки того же скилла).
