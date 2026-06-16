# Purge Orphaned Tasks on Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить opt-in очистку осиротевших задач в `sync-tasks`: скилл передаёт список активных ключей доски, сервер удаляет всё лишнее из Postgres и Neo4j; задачи с PR-линками защищены по умолчанию.

**Architecture:** Новые методы в `TaskStore` / `TaskGraph` / `TaskService` → делегирующий метод в `MCPReviewService` → новый MCP-тул `purge_orphaned_tasks` → шаг в скилле `sync-tasks` за флагом `--purge-orphaned`. Fail-soft на каждом слое: сбой graph-части не блокирует store.

**Tech Stack:** psycopg3 (pool), Neo4j driver 5.x, FastMCP, pytest.

---

### Task 1: `TaskStore` — `list_keys()` и `delete_tasks()`

**Files:**
- Modify: `reviewer/tasks/store.py`

- [ ] **Step 1: Добавить `list_keys()` в `TaskStore`**

Вставить после метода `update_meta` (перед `search`):

```python
def list_keys(self) -> list[str]:
    """Все ключи задач в Postgres."""
    with self._connect() as conn:
        rows = conn.execute("SELECT key FROM tasks").fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 2: Добавить `delete_tasks()` в `TaskStore`**

Вставить сразу после `list_keys`:

```python
def delete_tasks(self, keys: list[str]) -> int:
    """Удалить задачи по ключам. Возвращает кол-во удалённых строк."""
    if not keys:
        return 0
    with self._connect() as conn:
        result = conn.execute(
            "DELETE FROM tasks WHERE key = ANY(%s)", (keys,)
        )
        conn.commit()
    return result.rowcount
```

- [ ] **Step 3: Зафиксировать**

```bash
git add reviewer/tasks/store.py
git commit -m "feat(tasks): TaskStore.list_keys() и delete_tasks()"
```

---

### Task 2: `TaskGraph` — `keys_with_prs()` и `delete_tasks()`

**Files:**
- Modify: `reviewer/tasks/graph.py`

- [ ] **Step 1: Добавить `keys_with_prs()` в `TaskGraph`**

Вставить после метода `task_context`:

```python
def keys_with_prs(self) -> set[str]:
    """Ключи :Task-узлов с хотя бы одним ребром IMPLEMENTED_BY."""
    records, _, _ = self._driver.execute_query(
        "MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) RETURN t.key AS key"
    )
    return {r["key"] for r in records}
```

- [ ] **Step 2: Добавить `delete_tasks()` в `TaskGraph`**

Вставить после `keys_with_prs`:

```python
def delete_tasks(self, keys: list[str]) -> int:
    """Удалить :Task-узлы с рёбрами DETACH DELETE. :PR/:Symbol не трогает."""
    if not keys:
        return 0
    _, summary, _ = self._driver.execute_query(
        "MATCH (t:Task) WHERE t.key IN $keys DETACH DELETE t",
        keys=list(keys),
    )
    return summary.counters.nodes_deleted
```

- [ ] **Step 3: Зафиксировать**

```bash
git add reviewer/tasks/graph.py
git commit -m "feat(tasks): TaskGraph.keys_with_prs() и delete_tasks()"
```

---

### Task 3: Unit-тесты и `TaskService.purge_orphaned_tasks()`

**Files:**
- Modify: `tests/tasks/test_service.py`
- Modify: `reviewer/tasks/service.py`

- [ ] **Step 1: Расширить `_FakeStore` в `test_service.py`**

Заменить класс `_FakeStore` на расширенную версию с `list_keys()` и `delete_tasks()`:

```python
class _FakeStore:
    def __init__(self, hashes=None, search_result=None):
        self._hashes = dict(hashes or {})
        self.upserted = []
        self.meta_updates = []
        self.deleted = []
        self._search_result = search_result or []

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)

    def update_meta(self, key, title, status, url, aliases):
        self.meta_updates.append((key, title, status, url, aliases))

    def search(self, q, vec, top_k=5):
        return self._search_result

    def list_keys(self):
        return list(self._hashes.keys())

    def delete_tasks(self, keys):
        count = 0
        for k in list(keys):
            if k in self._hashes:
                del self._hashes[k]
                count += 1
        self.deleted.extend(keys)
        return count
```

- [ ] **Step 2: Расширить `_FakeGraph` в `test_service.py`**

Заменить класс `_FakeGraph` на расширенную версию с `keys_with_prs()` и `delete_tasks()`:

```python
class _FakeGraph:
    def __init__(self, context=None, raise_on=(), pr_keys=()):
        self.tasks = []
        self.links = []
        self.pr_links = []
        self.deleted_tasks = []
        self._context = context or {}
        self._raise_on = set(raise_on)
        self._pr_keys = set(pr_keys)

    def upsert_task(self, key, aliases, title, status, url):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append((key, aliases, title, status, url))

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)

    def link_pr(self, task_key, pr, touched):
        self.pr_links.append((task_key, pr, touched))

    def task_context(self, key):
        if "task_context" in self._raise_on:
            raise RuntimeError("neo4j down")
        return self._context

    def keys_with_prs(self):
        if "keys_with_prs" in self._raise_on:
            raise RuntimeError("neo4j down")
        return set(self._pr_keys)

    def delete_tasks(self, keys):
        if "delete_tasks" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.deleted_tasks.extend(keys)
        return len(list(keys))
```

- [ ] **Step 3: Написать падающие unit-тесты для `purge_orphaned_tasks`**

Добавить в конец `tests/tasks/test_service.py`:

```python
def test_purge_deletes_orphaned():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph()
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 1
    assert "ID-2" in store.deleted
    assert "ID-1" not in store.deleted


def test_purge_keeps_tasks_with_prs():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(pr_keys={"ID-2"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["protected_prs"] == 1
    assert "ID-2" not in store.deleted
    assert "ID-1" in store.deleted


def test_purge_no_keep_with_prs():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(pr_keys={"ID-2"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(
        [], keep_with_prs=False
    )
    assert result["deleted_store"] == 2
    assert "ID-1" in store.deleted and "ID-2" in store.deleted


def test_purge_all_active_no_delete():
    store = _FakeStore(hashes={"ID-1": "h1"})
    result = TaskService(store, _FakeGraph(), _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert result["deleted_store"] == 0
    assert result["deleted_graph"] == 0


def test_purge_empty_active_keys_deletes_all():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    result = TaskService(store, _FakeGraph(), _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 2


def test_purge_store_list_keys_error_returns_warning():
    class _BrokenStore(_FakeStore):
        def list_keys(self):
            raise RuntimeError("pg down")

    result = TaskService(
        _BrokenStore(hashes={"ID-1": "h1"}), _FakeGraph(), _FakeEmbedder()
    ).purge_orphaned_tasks([])
    assert result["deleted_store"] == 0
    assert any("store:" in w for w in result["warnings"])


def test_purge_graph_keys_with_prs_error_continues_without_protection():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(raise_on=("keys_with_prs",))
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert any("graph:" in w for w in result["warnings"])
    assert result["deleted_store"] == 2


def test_purge_graph_delete_error_is_warning_store_cleaned():
    store = _FakeStore(hashes={"ID-1": "h1"})
    graph = _FakeGraph(raise_on=("delete_tasks",))
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 0
    assert any("graph:" in w for w in result["warnings"])


def test_purge_graph_none_works_store_only():
    store = _FakeStore(hashes={"ID-1": "h1"})
    result = TaskService(store, None, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 0
    assert result["warnings"] == []
```

- [ ] **Step 4: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/tasks/test_service.py -k purge -v
```

Ожидаем: `AttributeError: 'TaskService' object has no attribute 'purge_orphaned_tasks'`

- [ ] **Step 5: Реализовать `purge_orphaned_tasks` в `TaskService`**

Добавить метод в `reviewer/tasks/service.py` после `link_review`:

```python
def purge_orphaned_tasks(
    self,
    active_keys: list[str],
    *,
    keep_with_prs: bool = True,
) -> dict:
    """Удалить задачи, отсутствующие в active_keys. Fail-soft по слоям."""
    warnings: list[str] = []
    active = set(active_keys)

    try:
        all_keys = set(self._store.list_keys())
    except Exception as e:
        log.warning("purge_orphaned_tasks: сбой list_keys", exc_info=True)
        warnings.append(f"store: {type(e).__name__}: {e}")
        return {"deleted_store": 0, "deleted_graph": 0,
                "protected_prs": 0, "warnings": warnings}

    orphaned = all_keys - active
    protected: set[str] = set()

    if keep_with_prs and self._graph is not None:
        try:
            pr_keys = self._graph.keys_with_prs()
            protected = orphaned & pr_keys
            orphaned = orphaned - protected
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой keys_with_prs", exc_info=True)
            warnings.append(f"graph: {type(e).__name__}: {e}")

    to_delete = list(orphaned)
    deleted_store = 0
    try:
        deleted_store = self._store.delete_tasks(to_delete)
    except Exception as e:
        log.warning("purge_orphaned_tasks: сбой delete_tasks (store)", exc_info=True)
        warnings.append(f"store: {type(e).__name__}: {e}")

    deleted_graph = 0
    if self._graph is not None:
        try:
            deleted_graph = self._graph.delete_tasks(to_delete)
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой delete_tasks (graph)", exc_info=True)
            warnings.append(f"graph: {type(e).__name__}: {e}")

    return {
        "deleted_store": deleted_store,
        "deleted_graph": deleted_graph,
        "protected_prs": len(protected),
        "warnings": warnings,
    }
```

- [ ] **Step 6: Убедиться, что тесты проходят**

```bash
.venv/bin/pytest tests/tasks/test_service.py -v
```

Ожидаем: все тесты PASS (включая уже существующие).

- [ ] **Step 7: Зафиксировать**

```bash
git add tests/tasks/test_service.py reviewer/tasks/service.py
git commit -m "feat(tasks): TaskService.purge_orphaned_tasks() + unit-тесты"
```

---

### Task 4: MCP-слой — метод в `MCPReviewService` и тул в `mcp_server.py`

**Files:**
- Modify: `reviewer/mcp/service.py`
- Modify: `reviewer/entrypoints/mcp_server.py`
- Modify: `tests/mcp/test_server_tools.py`

- [ ] **Step 1: Написать падающие тесты для нового тула**

Добавить в конец `tests/mcp/test_server_tools.py`:

```python
def test_purge_orphaned_tasks_tool_registered():
    import asyncio

    svc = _service()
    svc.purge_orphaned_tasks.return_value = {
        "deleted_store": 0, "deleted_graph": 0, "protected_prs": 0, "warnings": []
    }
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "purge_orphaned_tasks" in names


def test_purge_orphaned_tasks_tool_forwards_to_service():
    import asyncio

    svc = _service()
    svc.purge_orphaned_tasks.return_value = {
        "deleted_store": 2, "deleted_graph": 2, "protected_prs": 1, "warnings": []
    }
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "purge_orphaned_tasks",
        {"active_keys": ["ID-1", "ID-2"], "keep_with_prs": True},
    ))
    svc.purge_orphaned_tasks.assert_called_once_with(["ID-1", "ID-2"], True)
```

- [ ] **Step 2: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/mcp/test_server_tools.py -k purge -v
```

Ожидаем: FAIL — тул не зарегистрирован.

- [ ] **Step 3: Добавить делегирующий метод в `MCPReviewService`**

Добавить в `reviewer/mcp/service.py` после метода `board_config`:

```python
def purge_orphaned_tasks(
    self, active_keys: list[str], keep_with_prs: bool = True
) -> dict:
    """Удалить осиротевшие задачи из store и графа."""
    return self.components.task_service.purge_orphaned_tasks(
        active_keys, keep_with_prs=keep_with_prs
    )
```

- [ ] **Step 4: Добавить MCP-тул в `mcp_server.py`**

Добавить в `reviewer/entrypoints/mcp_server.py` после тула `index_tasks_batch`:

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

- [ ] **Step 5: Убедиться, что тесты проходят**

```bash
.venv/bin/pytest tests/mcp/test_server_tools.py -v
```

Ожидаем: все тесты PASS.

- [ ] **Step 6: Зафиксировать**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): MCP-тул purge_orphaned_tasks"
```

---

### Task 5: Обновить скилл `sync-tasks`

**Files:**
- Modify: `plugin/skills/sync-tasks/SKILL.md`

- [ ] **Step 1: Добавить флаги в секцию `## Inputs`**

В `plugin/skills/sync-tasks/SKILL.md` в секции `## Inputs` добавить после перечисления существующих флагов:

```markdown
- `--purge-orphaned`: после индексации удалить из store/графа задачи, которых нет на доске.
  По умолчанию off — без флага поведение не меняется.
- `--no-keep-with-prs`: в сочетании с `--purge-orphaned` удалять также задачи с PR-историей
  (`:IMPLEMENTED_BY`). По умолчанию такие задачи защищены.
```

- [ ] **Step 2: Добавить шаг 4 в `## Pipeline`**

После шага 3 (Normalize + index) добавить:

```markdown
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
```

- [ ] **Step 3: Расширить шаг 4 (`## Report`) до полного summary**

В секции `## Pipeline` шаг 4 (текущий Report, теперь шаг 5) добавить формат вывода при активном purge:

```markdown
   При активном `--purge-orphaned` включи в summary строку:
   ```
   Purge: N deleted (store+graph), M protected (have PR history), K warnings.
   ```
```

- [ ] **Step 4: Зафиксировать**

```bash
git add plugin/skills/sync-tasks/SKILL.md
git commit -m "feat(skill): sync-tasks — флаги --purge-orphaned и --no-keep-with-prs"
```

---

### Task 6: Integration-тест полного цикла

**Files:**
- Modify: `tests/tasks/test_integration.py`

- [ ] **Step 1: Написать integration-тесты**

Добавить в конец `tests/tasks/test_integration.py`:

```python
def test_purge_full_cycle(store, graph):
    """index_task → purge_orphaned_tasks(active=[]) → задача исчезает из search."""
    svc = TaskService(store, graph, _FakeEmbedder())

    svc.index_task({
        "key": "ID-P1", "aliases": ["PRI-P1"], "title": "Purge test task",
        "description": "Will be purged after sync", "criteria": [],
        "status": "Open", "url": None, "links": [],
    })
    assert store.existing_hash("ID-P1") is not None
    assert "ID-P1" in store.list_keys()

    result = svc.purge_orphaned_tasks([])  # ничего активного
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 1
    assert result["warnings"] == []
    assert store.list_keys() == []

    found = svc.search_tasks("purge test")
    assert "ID-P1" not in found


def test_purge_protects_task_with_pr_link(store, graph):
    """Задача с PR-ссылкой защищена при keep_with_prs=True (default)."""
    svc = TaskService(store, graph, _FakeEmbedder())

    svc.index_task({
        "key": "ID-P2", "aliases": [], "title": "PR-linked task",
        "description": "Has review history", "criteria": [],
        "status": "Done", "url": None, "links": [],
    })
    pr = PRRef(repo="o/r", number=10, url="https://github.com/o/r/pull/10", sha="sha1")
    svc.link_review("ID-P2", pr, ["auth.py#login"])

    result = svc.purge_orphaned_tasks([])  # не активна, но есть PR
    assert result["deleted_store"] == 0
    assert result["protected_prs"] == 1
    assert "ID-P2" in store.list_keys()


def test_purge_no_keep_with_prs_deletes_all(store, graph):
    """При keep_with_prs=False удаляются все, включая задачи с PR-историей."""
    svc = TaskService(store, graph, _FakeEmbedder())

    svc.index_task({
        "key": "ID-P3", "aliases": [], "title": "Force purge task",
        "description": "Has PR but force-purged", "criteria": [],
        "status": "Done", "url": None, "links": [],
    })
    pr = PRRef(repo="o/r", number=11, url="https://github.com/o/r/pull/11", sha="sha2")
    svc.link_review("ID-P3", pr, ["main.py#run"])

    result = svc.purge_orphaned_tasks([], keep_with_prs=False)
    assert result["deleted_store"] == 1
    assert store.list_keys() == []
```

- [ ] **Step 2: Запустить integration-тесты (требует `docker compose up -d`)**

```bash
.venv/bin/pytest tests/tasks/test_integration.py -m integration -v
```

Ожидаем: все тесты PASS, включая три новых.

- [ ] **Step 3: Зафиксировать**

```bash
git add tests/tasks/test_integration.py
git commit -m "test(tasks): integration-тесты purge_orphaned_tasks"
```

---

### Task 7: Финальная проверка

- [ ] **Step 1: Полный unit-прогон**

```bash
.venv/bin/pytest -q
```

Ожидаем: все тесты PASS, 0 ошибок.

- [ ] **Step 2: Lint**

```bash
.venv/bin/ruff check reviewer/tasks/store.py reviewer/tasks/graph.py \
    reviewer/tasks/service.py reviewer/mcp/service.py \
    reviewer/entrypoints/mcp_server.py
```

Ожидаем: 0 ошибок.

- [ ] **Step 3: Убедиться, что MCP-сервер стартует**

```bash
.venv/bin/python -c "
from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.mcp.service import MCPReviewService
from reviewer.entrypoints.mcp_server import create_server
import asyncio
s = Settings()
c = build_components(s)
srv = create_server(MCPReviewService(s, c))
tools = asyncio.run(srv.list_tools())
names = {t.name for t in tools}
assert 'purge_orphaned_tasks' in names
print('OK, tools:', len(tools))
"
```

Ожидаем: `OK, tools: 15`
