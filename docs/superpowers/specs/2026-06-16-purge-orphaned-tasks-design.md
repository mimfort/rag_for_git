# Дизайн: Purge orphaned tasks on sync (PRI-95)

Дата: 2026-06-16

## Проблема

`sync-tasks` только делает upsert задач с доски. Если задачу удалили в YouGile, она остаётся в Postgres (`tasks`) и в Neo4j (`:Task`-узлы). Со временем индекс расходится с реальным состоянием доски.

## Решение

Добавить opt-in шаг purge в `sync-tasks`: скилл передаёт серверу полный список активных ключей доски, сервер вычисляет дельту и удаляет осиротевшие задачи.

**Ключевые решения:**
- Задачи с PR-линками (`:IMPLEMENTED_BY`) защищены по умолчанию — они несут историческую информацию о том, какой код менялся по каким задачам.
- Без флага `--purge-orphaned` поведение `sync-tasks` не меняется.
- Fail-soft на каждом слое: сбой graph-слоя не блокирует store-слой.

## Архитектура

### `reviewer/tasks/store.py` — `TaskStore`

Два новых метода:

```python
def list_keys(self) -> list[str]:
    """Все ключи задач в Postgres (SELECT key FROM tasks)."""

def delete_tasks(self, keys: list[str]) -> int:
    """Удалить задачи по ключам. Возвращает кол-во удалённых строк.
    DELETE FROM tasks WHERE key = ANY(%s). Пустой список → 0, без запроса."""
```

### `reviewer/tasks/graph.py` — `TaskGraph`

Два новых метода:

```python
def keys_with_prs(self) -> set[str]:
    """Ключи :Task-узлов с хотя бы одним ребром IMPLEMENTED_BY.
    MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) RETURN t.key"""

def delete_tasks(self, keys: list[str]) -> int:
    """Удалить :Task-узлы и все их рёбра (DETACH DELETE).
    :PR и :Symbol не трогает. Возвращает кол-во удалённых узлов.
    Пустой список → 0, без запроса."""
```

`DETACH DELETE` убирает узел вместе с рёбрами `TASK_LINK` и `IMPLEMENTED_BY`, но автономные `:PR` и `:Symbol` остаются.

### `reviewer/tasks/service.py` — `TaskService`

```python
def purge_orphaned_tasks(
    self,
    active_keys: list[str],
    *,
    keep_with_prs: bool = True,
) -> dict:
    """Удалить задачи, отсутствующие в active_keys.

    Алгоритм:
    1. list_keys() из store → all_keys
    2. orphaned = all_keys - set(active_keys)
    3. Если keep_with_prs и граф доступен: вычесть keys_with_prs() (fail-soft: сбой → warning, продолжаем без защиты)
    4. delete_tasks(orphaned) из store (fail-soft)
    5. delete_tasks(orphaned) из graph (fail-soft)

    Возвращает: {deleted_store, deleted_graph, protected_prs, warnings}
    """
```

**Fail-soft детали:**
- Сбой `store.list_keys()` → warning, возвращаем `{deleted_store: 0, ...}` без удаления.
- Сбой `graph.keys_with_prs()` → warning + продолжаем без PR-защиты (conservative: лучше лишний раз не удалить).
- Сбой `store.delete_tasks()` → warning, граф не трогаем.
- Сбой `graph.delete_tasks()` → warning, результат store уже записан.

### `reviewer/mcp/service.py` — `MCPReviewService`

```python
def purge_orphaned_tasks(
    self, active_keys: list[str], keep_with_prs: bool = True
) -> dict:
    return self.components.task_service.purge_orphaned_tasks(
        active_keys, keep_with_prs=keep_with_prs
    )
```

### `reviewer/entrypoints/mcp_server.py`

15-й MCP-тул:

```python
@mcp.tool()
def purge_orphaned_tasks(
    active_keys: list[str], keep_with_prs: bool = True
) -> dict:
    """Remove tasks no longer on the board from the vector store and task graph.
    active_keys: all current board task keys (canonical ID-N form).
    keep_with_prs: if True (default), tasks with IMPLEMENTED_BY edges are protected.
    Returns {deleted_store, deleted_graph, protected_prs, warnings}."""
    return service.purge_orphaned_tasks(active_keys, keep_with_prs)
```

### `plugin/skills/sync-tasks/SKILL.md`

**Inputs** (дополнение):
- `--purge-orphaned` — включить очистку осиротевших задач (по умолчанию off).
- `--no-keep-with-prs` — снять защиту с задач, имеющих PR-историю (по умолчанию защищены).

**Шаг 4. Purge** (только при `--purge-orphaned`):

После завершения `index_tasks_batch` собери все канонические ключи (`idTaskCommon`, вида `ID-N`) задач, прочитанных с доски на шаге 2. Вызови:

```
purge_orphaned_tasks(
    active_keys=[...все ID-N с доски...],
    keep_with_prs=<True если не задан --no-keep-with-prs>
)
```

Включи результат в итоговый summary.

**Формат summary при purge:**
```
Sync complete: 42 indexed (embedded), 38 refreshed (meta only), 0 failed.
Purge: 3 deleted (store+graph), 2 protected (have PR history), 0 warnings.
```

## Тесты

### Unit (`tests/tasks/test_service.py`)

На fake-объектах `_FakeStore` / `_FakeGraph` (следуем паттерну существующих тестов):

| Тест | Что проверяет |
|---|---|
| `test_purge_deletes_orphaned` | базовый: 2 в индексе, 1 активная → 1 удалена |
| `test_purge_keeps_tasks_with_prs` | `keep_with_prs=True`: задача с PR не удаляется |
| `test_purge_no_keep_with_prs` | `keep_with_prs=False`: удаляются все включая PR-задачи |
| `test_purge_all_active_no_delete` | все задачи активны → `deleted_store=0` |
| `test_purge_empty_active_keys` | пустой список активных → все удаляются (если нет PR-защиты) |
| `test_purge_store_error_is_warning` | сбой `list_keys()` → warning, нет удалений |
| `test_purge_graph_keys_with_prs_error` | сбой `keys_with_prs()` → warning, purge продолжается без защиты |
| `test_purge_graph_delete_error_is_warning` | сбой `graph.delete_tasks()` → warning, store уже очищен |
| `test_purge_graph_none` | `graph=None` → работает только через store |

### Integration (`tests/tasks/test_integration.py`)

Маркер `integration` (требует Postgres):

- `test_purge_full_cycle`: `index_task` → `purge_orphaned_tasks(active_keys=[])` → убедиться что `store.list_keys()` возвращает пустой список и `search` не находит задачу.

## Что НЕ меняется

- Схема Postgres (`tasks`) — никаких новых колонок.
- Схема Neo4j — никаких новых лейблов/свойств.
- Поведение `sync-tasks` без флага `--purge-orphaned`.
- Все существующие тулы MCP (нумерация не ломается, тул добавляется последним).
