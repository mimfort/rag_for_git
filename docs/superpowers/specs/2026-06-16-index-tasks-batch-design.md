# Дизайн: index_tasks_batch — батчевая индексация задач

**Задача**: PRI-96 / ID-96  
**Статус**: Design approved  
**Дата**: 2026-06-16

## Проблема

`sync-tasks` вызывает `index_task` для каждой задачи по одной:
- O(N) вызовов Voyage API (free tier: 3 RPM / 10K TPM → 59 задач ≈ 20 минут)
- O(N) LLM tool call round-trip'ов вместо одного

## Решение (Рычаг 1 из PRI-96)

Добавить `index_tasks_batch` MCP-tool и `TaskService.index_batch`, которые принимают весь список задач за один вызов и делают **один** `embed_documents(all_new_texts)`.

## Архитектура и поток данных

```
SKILL.md (один tool call)
    │
    ▼
MCP tool: index_tasks_batch(tasks: list[dict]) → list[dict]
    │  reviewer/entrypoints/mcp_server.py  +  reviewer/mcp/service.py
    ▼
TaskService.index_batch(tasks: list[dict]) → list[dict]
    │  reviewer/tasks/service.py
    │
    ├─ 1. build_task_text + task_content_hash для каждой задачи
    ├─ 2. store.existing_hash(key) × N  → делим на to_embed / meta_only
    ├─ 3. embedder.embed_documents([texts для to_embed])  ← ОДИН Voyage-вызов
    ├─ 4. store.upsert_task(row) × len(to_embed)
    ├─ 5. store.update_meta(key, …) × len(meta_only)
    └─ 6. graph.upsert_task + graph.upsert_links × N
```

Новых методов в `TaskStore` и `TaskGraph` не добавляется — используются существующие per-task методы (батчится только Voyage-вызов).

## Компоненты и изменения

### `reviewer/tasks/service.py` — новый метод `TaskService.index_batch`

```python
def index_batch(self, tasks: list[dict]) -> list[dict]:
    """Батчевая индексация: один Voyage-вызов для всех изменившихся задач."""
```

Алгоритм:
1. Для каждой задачи вычислить `text = build_task_text(...)` и `chash = task_content_hash(text)`
2. Получить `existing_hash(key)` для каждой задачи (цикл, существующий метод)
3. Разделить на `to_embed` (hash изменился или новая) и `meta_only` (hash совпал)
4. Если `to_embed` непустой: один вызов `embedder.embed_documents([t.text for t in to_embed])`
5. `store.upsert_task(row)` для каждой из `to_embed`
6. `store.update_meta(...)` для каждой из `meta_only`
7. `graph.upsert_task + graph.upsert_links` для каждой задачи
8. Вернуть `list[{key, embedded, links_upserted, warnings}]` в исходном порядке

### `reviewer/mcp/service.py` — новый метод `MCPReviewService.index_tasks_batch`

```python
def index_tasks_batch(self, tasks: list[dict]) -> list[dict]:
    return self.components.task_service.index_batch(tasks)
```

### `reviewer/entrypoints/mcp_server.py` — регистрация MCP-tool

```python
@mcp.tool()
def index_tasks_batch(tasks: list[dict]) -> list[dict]:
    """Батчевая индексация списка TaskBrief. Один Voyage-вызов для изменившихся задач."""
    return svc.index_tasks_batch(tasks)
```

### `plugin/skills/sync-tasks/SKILL.md` — шаг 3

Вместо цикла `index_task` по одной задаче:
```
После нормализации всех задач → один вызов index_tasks_batch([...все TaskBrief...])
```

Разбор результатов для финального отчёта:
- `embedded=true` → «проиндексировано»
- `embedded=false`, `warnings=[]` → «без изменений»
- `warnings≠[]` → «предупреждения»

## Контракт API

**Вход** `index_tasks_batch`: `list[dict]` — список `TaskBrief` (`{key, aliases, title, description, criteria, status, url, links}`). Тот же формат, что у `index_task`.

**Выход**: `list[dict]` — по одному результату на задачу в исходном порядке:
```json
{"key": "ID-96", "embedded": true, "links_upserted": 2, "warnings": []}
```

Пустой вход → пустой выход `[]`.

## Обработка ошибок (fail-soft)

| Сбой | Поведение |
|---|---|
| Задача без `key` | `{key: null, embedded: false, links_upserted: 0, warnings: ["task has no key"]}`, остальные продолжают |
| Сбой `embed_documents` | Все задачи из `to_embed` → `embedded=false, warnings=["embedder: <err>"]`; задачи из `meta_only` всё равно получают `update_meta` |
| Сбой `store.upsert_task` для одной задачи | Только она получает warning; остальные продолжают |
| Neo4j недоступен | Graph-часть деградирует (warning), store-часть завершается |

## Тестирование

### Unit (`tests/tasks/test_service_batch.py`)

- `embed_documents` вызван **ровно один раз** при обработке N задач с новым контентом
- `embed_documents` **не вызван** при повторном прогоне без изменений (все hash совпали)
- При сбое одной задачи остальные получают корректные результаты (fail-soft)
- Пустой вход → пустой результат без обращений к embedder/store/graph
- Задача без `key` → warning в результате, остальные не затронуты

### Integration (`tests/tasks/test_service_batch.py`, маркер `integration`)

- `index_batch([t1, t2, t3])` даёт те же записи в Postgres + Neo4j, что последовательные `index_task(t1)`, `index_task(t2)`, `index_task(t3)`
- Повторный `index_batch` без изменений: `embedded=false` для всех, данные не перезаписаны

## Критерии приёмки (из задачи)

- Первичный sync 59 задач < 2 минуты
- Повторный sync (без изменений) < 30 секунд
- Число LLM tool calls при sync = O(1) (один batch), не O(N)
- Существующий `index_task` (single-task) остаётся рабочим
- Связи из description задачи попадают в граф (уже реализовано в playbook)

## Вне скоупа

- `get_task_hashes` MCP-tool (Рычаг 3) — возможный оверинжиниринг, оценить после
- Батчевые методы в `TaskStore` / `TaskGraph` (Рычаг 1B) — main bottleneck — Voyage, не DB
- Рычаг 2 (параллельные `index_task` в SKILL.md) — заменён Рычагом 1
