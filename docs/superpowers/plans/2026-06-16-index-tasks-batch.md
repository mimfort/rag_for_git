# index_tasks_batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить `index_tasks_batch` MCP-tool, который принимает весь список задач за один вызов и делает один Voyage `embed_documents` вместо O(N).

**Architecture:** Новый метод `TaskService.index_batch` вычисляет content-hash для каждой задачи, разделяет их на изменившиеся / неизменившиеся, делает один `embedder.embed_documents(all_changed_texts)`, затем в цикле вызывает существующие `TaskStore.upsert_task` / `update_meta` и `TaskGraph.upsert_task` / `upsert_links`. MCP-слой (`mcp/service.py` + `entrypoints/mcp_server.py`) — тонкая обёртка. `sync-tasks/SKILL.md` обновляется на один вызов `index_tasks_batch`.

**Tech Stack:** Python 3.11+, psycopg3 (psycopg-pool), Neo4j driver, FastMCP, pytest.

---

## File Map

| Действие | Файл |
|---|---|
| Создать | `tests/tasks/test_service_batch.py` |
| Изменить | `reviewer/tasks/service.py` |
| Изменить | `reviewer/mcp/service.py` |
| Изменить | `reviewer/entrypoints/mcp_server.py` |
| Изменить | `plugin/skills/sync-tasks/SKILL.md` |
| Изменить | `tests/tasks/test_integration.py` |

---

## Task 1: Unit-тесты для `TaskService.index_batch` (TDD)

**Files:**
- Create: `tests/tasks/test_service_batch.py`

- [ ] **Шаг 1.1: Создать файл с фейками и первым тестом (пустой вход)**

```python
# tests/tasks/test_service_batch.py
"""Unit-тесты для TaskService.index_batch."""
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import build_task_text, task_content_hash


class _FakeStore:
    def __init__(self, hashes=None):
        self._hashes = hashes or {}
        self.upserted = []
        self.meta_updates = []

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)

    def update_meta(self, key, title, status, url, aliases):
        self.meta_updates.append((key, title, status, url, aliases))


class _FakeGraph:
    def __init__(self, raise_on=()):
        self.tasks = []
        self.links = []
        self._raise_on = set(raise_on)

    def upsert_task(self, key, aliases, title, status, url):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append(key)

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)


class _FakeEmbedder:
    def __init__(self):
        self.doc_calls = []

    def embed_documents(self, texts):
        self.doc_calls.append(list(texts))
        return [[0.1] * 8 for _ in texts]


def _brief(key="ID-1", alias="PRI-1", **over):
    b = {"key": key, "aliases": [alias], "title": "Add logout",
         "description": "Clear session", "criteria": ["redirects"],
         "status": "Open", "url": "http://example.com",
         "links": [{"key": "ID-2", "title": "child", "type": "subtask"}]}
    b.update(over)
    return b


def test_index_batch_empty_returns_empty():
    svc = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder())
    assert svc.index_batch([]) == []
```

- [ ] **Шаг 1.2: Запустить тест, убедиться что падает (метода нет)**

```bash
.venv/bin/pytest tests/tasks/test_service_batch.py::test_index_batch_empty_returns_empty -v
```

Ожидаем: `FAILED` с `AttributeError: 'TaskService' object has no attribute 'index_batch'`

- [ ] **Шаг 1.3: Добавить тест — один `embed_documents` для новых задач**

Дописать в `tests/tasks/test_service_batch.py`:

```python
def test_index_batch_single_embed_call_for_new_tasks():
    """N новых задач → ровно один вызов embed_documents со всеми текстами."""
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="Fix bug",
                                              description="desc", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert len(results) == 2
    assert all(r["embedded"] is True for r in results)
    assert len(emb.doc_calls) == 1          # ровно один Voyage-вызов
    assert len(emb.doc_calls[0]) == 2       # оба текста в одном вызове
    assert len(store.upserted) == 2


def test_index_batch_no_embed_when_all_unchanged():
    """Все задачи без изменений → embed_documents не вызывается."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    t2 = build_task_text("Fix bug", "desc", [])
    store = _FakeStore(hashes={
        "ID-1": task_content_hash(t1),
        "ID-2": task_content_hash(t2),
    })
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="Fix bug",
                                              description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert all(r["embedded"] is False for r in results)
    assert emb.doc_calls == []              # нет Voyage-вызовов
    assert len(store.meta_updates) == 2


def test_index_batch_embeds_only_changed():
    """Одна задача изменилась, одна нет → embed_documents с одним текстом."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(t1)})
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert results[0]["embedded"] is False   # без изменений
    assert results[1]["embedded"] is True    # новая
    assert len(emb.doc_calls) == 1
    assert len(emb.doc_calls[0]) == 1        # только один текст


def test_index_batch_task_no_key_gets_warning_others_continue():
    """Задача без key → warning в результате; остальные продолжают."""
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [{"title": "no key"}, _brief("ID-2", "PRI-2", title="Fix bug",
                                          description="desc", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert results[0]["key"] is None
    assert any("has no key" in w for w in results[0]["warnings"])
    assert results[1]["embedded"] is True    # вторая задача обработана


def test_index_batch_embed_error_marks_changed_warns_but_meta_only_ok():
    """Сбой embed_documents → changed-задачи получают warning; unchanged — update_meta."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(t1)})

    class _BrokenEmbedder(_FakeEmbedder):
        def embed_documents(self, texts):
            super().embed_documents(texts)
            raise RuntimeError("voyage down")

    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), _BrokenEmbedder()).index_batch(tasks)

    assert results[0]["embedded"] is False   # unchanged → update_meta, не затронута
    assert results[0]["warnings"] == []      # нет ошибки у unchanged
    assert store.meta_updates                # meta обновлена
    assert results[1]["embedded"] is False
    assert any("embedder:" in w for w in results[1]["warnings"])


def test_index_batch_store_error_one_task_others_continue():
    """Сбой upsert_task для одной задачи → warning только у неё."""
    call_count = {"n": 0}

    class _PartiallyBrokenStore(_FakeStore):
        def upsert_task(self, row):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("pg write error")
            super().upsert_task(row)

    store = _PartiallyBrokenStore()
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert any("store:" in w for w in results[0]["warnings"])
    assert results[1]["embedded"] is True


def test_index_batch_graph_none_adds_warning():
    """graph=None → warning для каждой задачи, store-слой работает."""
    store, emb = _FakeStore(), _FakeEmbedder()
    results = TaskService(store, None, emb).index_batch([_brief()])

    assert results[0]["embedded"] is True
    assert any("graph unavailable" in w for w in results[0]["warnings"])


def test_index_batch_result_order_matches_input():
    """Порядок результатов совпадает с порядком входного списка."""
    store, emb = _FakeStore(), _FakeEmbedder()
    keys = ["ID-10", "ID-20", "ID-30"]
    tasks = [_brief(k, k.replace("ID-", "PRI-"), title=f"T{k}",
                    description="d", links=[]) for k in keys]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert [r["key"] for r in results] == keys
```

- [ ] **Шаг 1.4: Запустить все тесты файла, убедиться что падают (метода нет)**

```bash
.venv/bin/pytest tests/tasks/test_service_batch.py -v
```

Ожидаем: все `FAILED` с `AttributeError: 'TaskService' object has no attribute 'index_batch'`

---

## Task 2: Реализовать `TaskService.index_batch`

**Files:**
- Modify: `reviewer/tasks/service.py`

- [ ] **Шаг 2.1: Добавить метод `index_batch` в `TaskService`**

В `reviewer/tasks/service.py` после метода `index_task` (строка 72) добавить:

```python
    def index_batch(self, tasks: list[dict]) -> list[dict]:
        """Батчевая индексация: один Voyage-вызов для всех изменившихся задач."""
        if not tasks:
            return []

        # Шаг 1: распарсить все задачи и вычислить хэши
        parsed: list[dict | None] = []
        results: list[dict | None] = [None] * len(tasks)

        for i, task in enumerate(tasks):
            key = task.get("key") if isinstance(task, dict) else None
            if not key:
                results[i] = {"key": None, "embedded": False, "links_upserted": 0,
                              "warnings": ["task has no key"]}
                parsed.append(None)
                continue
            aliases = [a for a in (task.get("aliases") or []) if a and a != key]
            title = task.get("title") or ""
            description = task.get("description") or ""
            criteria = task.get("criteria") or []
            status = task.get("status")
            url = task.get("url")
            links = [lk for lk in (task.get("links") or [])
                     if isinstance(lk, dict) and lk.get("key")]
            text = build_task_text(title, description, criteria)
            chash = task_content_hash(text)
            parsed.append({"key": key, "aliases": aliases, "title": title,
                           "description": description, "status": status, "url": url,
                           "links": links, "text": text, "chash": chash})

        # Шаг 2: разделить на to_embed / meta_only по content-hash
        to_embed: list[int] = []
        meta_only: list[int] = []

        for i, p in enumerate(parsed):
            if p is None:
                continue
            try:
                prev = self._store.existing_hash(p["key"])
            except Exception as e:
                log.warning("index_batch: existing_hash сбой для %s", p["key"], exc_info=True)
                results[i] = {"key": p["key"], "embedded": False, "links_upserted": 0,
                              "warnings": [f"store: {type(e).__name__}: {e}"]}
                continue
            (meta_only if prev == p["chash"] else to_embed).append(i)

        # Шаг 3: один Voyage-вызов для изменившихся задач
        embed_err: str | None = None
        embeddings: dict[int, list[float]] = {}
        if to_embed:
            try:
                vecs = self._embedder.embed_documents([parsed[i]["text"] for i in to_embed])
                embeddings = {i: vecs[idx] for idx, i in enumerate(to_embed)}
            except Exception as e:
                log.warning("index_batch: сбой embed_documents", exc_info=True)
                embed_err = f"embedder: {type(e).__name__}: {e}"

        # Шаг 4: upsert изменившихся задач (или propagate embed_err)
        for i in to_embed:
            p = parsed[i]
            warnings: list[str] = []
            embedded = False
            if embed_err:
                warnings.append(embed_err)
            else:
                try:
                    self._store.upsert_task(TaskRow(
                        key=p["key"], aliases=p["aliases"], title=p["title"],
                        description=p["description"], status=p["status"], url=p["url"],
                        content_hash=p["chash"], text=p["text"], embedding=embeddings[i]))
                    embedded = True
                except Exception as e:
                    log.warning("index_batch: сбой store для %s", p["key"], exc_info=True)
                    warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": embedded,
                          "links_upserted": 0, "warnings": warnings}

        # Шаг 5: update_meta для неизменившихся задач
        for i in meta_only:
            p = parsed[i]
            warnings: list[str] = []
            try:
                self._store.update_meta(p["key"], p["title"], p["status"],
                                        p["url"], p["aliases"])
            except Exception as e:
                log.warning("index_batch: сбой update_meta для %s", p["key"], exc_info=True)
                warnings.append(f"store: {type(e).__name__}: {e}")
            results[i] = {"key": p["key"], "embedded": False,
                          "links_upserted": 0, "warnings": warnings}

        # Шаг 6: граф для всех валидных задач
        for i, p in enumerate(parsed):
            if p is None or results[i] is None:
                continue
            links_upserted = 0
            if self._graph is None:
                results[i]["warnings"].append(
                    "graph unavailable: task not added to task graph")
            else:
                try:
                    self._graph.upsert_task(p["key"], p["aliases"], p["title"],
                                            p["status"], p["url"])
                    if p["links"]:
                        links_upserted = self._graph.upsert_links(p["key"], p["links"])
                except Exception as e:
                    log.warning("index_batch: сбой графа для %s", p["key"], exc_info=True)
                    results[i]["warnings"].append(f"graph: {type(e).__name__}: {e}")
            results[i]["links_upserted"] = links_upserted

        return results
```

- [ ] **Шаг 2.2: Запустить unit-тесты, убедиться что все зелёные**

```bash
.venv/bin/pytest tests/tasks/test_service_batch.py -v
```

Ожидаем: все `PASSED`

- [ ] **Шаг 2.3: Убедиться что существующие тесты не сломаны**

```bash
.venv/bin/pytest tests/tasks/test_service.py -v
```

Ожидаем: все `PASSED`

- [ ] **Шаг 2.4: Закоммитить**

```bash
git add tests/tasks/test_service_batch.py reviewer/tasks/service.py
git commit -m "feat(tasks): TaskService.index_batch — батчевый Voyage-вызов"
```

---

## Task 3: Зарегистрировать MCP-tool `index_tasks_batch`

**Files:**
- Modify: `reviewer/mcp/service.py` (после строки 234)
- Modify: `reviewer/entrypoints/mcp_server.py` (после строки 69)

- [ ] **Шаг 3.1: Добавить метод в `MCPReviewService`**

В `reviewer/mcp/service.py` после метода `index_task` (строки 232–234) добавить:

```python
    def index_tasks_batch(self, tasks: list[dict]) -> list[dict]:
        """Батчевая индексация списка TaskBrief: один Voyage-вызов для изменившихся задач."""
        return self.components.task_service.index_batch(tasks)
```

- [ ] **Шаг 3.2: Зарегистрировать MCP-tool в `mcp_server.py`**

В `reviewer/entrypoints/mcp_server.py` после блока `index_task` (строки 63–69) добавить:

```python
    @mcp.tool()
    def index_tasks_batch(tasks: list[dict]) -> list[dict]:
        """Batch-index a list of TaskBriefs with a single Voyage embedding call.
        tasks: list of {key, aliases[], title, description, criteria[], status, url, links[]}.
        Idempotent: re-embeds only tasks whose text changed. Returns list of
        {key, embedded, links_upserted, warnings} in input order."""
        return service.index_tasks_batch(tasks)
```

- [ ] **Шаг 3.3: Прогнать linter**

```bash
.venv/bin/ruff check reviewer/tasks/service.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py
```

Ожидаем: нет новых ошибок (предупреждения уже существующие — ок)

- [ ] **Шаг 3.4: Прогнать все unit-тесты**

```bash
.venv/bin/pytest -q
```

Ожидаем: `passed` без новых падений

- [ ] **Шаг 3.5: Закоммитить**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py
git commit -m "feat(mcp): зарегистрировать MCP-tool index_tasks_batch"
```

---

## Task 4: Обновить `sync-tasks/SKILL.md`

**Files:**
- Modify: `plugin/skills/sync-tasks/SKILL.md`

- [ ] **Шаг 4.1: Заменить шаг 3 и секцию rate limits**

Найти и заменить шаг 3 (строки `3. **Normalize + index.**` … `re-running is cheap.`):

**Было:**
```markdown
3. **Normalize + index.** For each task, build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}` using the SAME mapping as
   `../review-pr/references/task-context-<type>.md`, then call `index_task(TaskBrief)`.
   `index_task` is idempotent (it re-embeds only when the task text changed), so re-running is cheap.
```

**Стало:**
```markdown
3. **Normalize + index.** Build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}` for every enumerated task
   using the SAME mapping as `../review-pr/references/task-context-<type>.md`, then call
   `index_tasks_batch([...all TaskBriefs...])` in a **single tool call**.
   Result: `list[{key, embedded, links_upserted, warnings}]` in input order.
   `index_tasks_batch` is idempotent (re-embeds only tasks whose text changed) and uses a single
   Voyage embedding call for all changed tasks — O(1) Voyage API calls regardless of board size.
```

Найти и заменить первый bullet в секции `## Rate limits & failure handling`:

**Было:**
```markdown
- Voyage free tier is 3 RPM / 10K TPM; embedding inside `index_task` already retries/backs off, so a
  large board simply runs slower — that is expected, not an error. Use `--limit` for a quick first
  pass.
- A single task that fails to read or index must NOT stop the sync: log it and continue.
```

**Стало:**
```markdown
- Voyage free tier is 3 RPM / 10K TPM; `index_tasks_batch` makes a single `embed_documents` call
  for all changed tasks and retries/backs off internally, so a large board is fast on first sync
  and near-instant on repeat syncs (only changed tasks are re-embedded). Use `--limit` for a quick
  smoke run.
- A single task that fails to read or index must NOT stop the sync: `index_tasks_batch` returns a
  per-task result list — check each entry's `warnings` field and log failures; continue.
```

- [ ] **Шаг 4.2: Закоммитить**

```bash
git add plugin/skills/sync-tasks/SKILL.md
git commit -m "feat(skill): sync-tasks использует index_tasks_batch вместо цикла"
```

---

## Task 5: Integration-тест batch vs single

**Files:**
- Modify: `tests/tasks/test_integration.py`

- [ ] **Шаг 5.1: Дописать integration-тест в конец `tests/tasks/test_integration.py`**

Прочитать конец файла, затем дописать:

```python
def test_index_batch_matches_sequential_index_task(store):
    """index_batch([t1,t2]) даёт те же записи что index_task(t1)+index_task(t2)."""
    from reviewer.tasks.graph import TaskGraph
    from reviewer.graph.store import GraphStore
    from reviewer.config.settings import Settings

    s = Settings()
    driver = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    graph = TaskGraph(driver)
    emb = _FakeEmbedder()
    svc = TaskService(store, graph, emb)

    t1 = {"key": "ID-B1", "aliases": ["PRI-B1"], "title": "Batch task 1",
          "description": "desc1", "criteria": [], "status": "Open",
          "url": None, "links": []}
    t2 = {"key": "ID-B2", "aliases": ["PRI-B2"], "title": "Batch task 2",
          "description": "desc2", "criteria": [], "status": "Open",
          "url": None, "links": [{"key": "ID-B1", "type": "related"}]}

    results = svc.index_batch([t1, t2])

    assert len(results) == 2
    assert all(r["embedded"] is True for r in results)
    assert results[1]["links_upserted"] == 1

    # Данные совпадают с тем, что вернул бы последовательный index_task
    assert store.existing_hash("ID-B1") == task_content_hash(
        build_task_text(t1["title"], t1["description"], t1["criteria"]))
    assert store.existing_hash("ID-B2") == task_content_hash(
        build_task_text(t2["title"], t2["description"], t2["criteria"]))

    # Повторный прогон: без изменений → embedded=False, без Voyage-вызова
    emb2 = _FakeEmbedder()
    svc2 = TaskService(store, graph, emb2)
    results2 = svc2.index_batch([t1, t2])
    assert all(r["embedded"] is False for r in results2)
    assert emb2.doc_calls == []
```

- [ ] **Шаг 5.2: Убедиться что integration-тест проходит (при поднятых docker-сервисах)**

```bash
.venv/bin/pytest tests/tasks/test_integration.py::test_index_batch_matches_sequential_index_task -v -m integration
```

Ожидаем: `PASSED`

- [ ] **Шаг 5.3: Закоммитить**

```bash
git add tests/tasks/test_integration.py
git commit -m "test(tasks): integration-тест index_batch vs sequential"
```

---

## Self-Review

**Покрытие спека:**

| Требование спека | Задача |
|---|---|
| `TaskService.index_batch` — один `embed_documents` | Task 2 |
| MCP-tool `index_tasks_batch` | Task 3 |
| Обновление SKILL.md | Task 4 |
| Список per-task результатов | Task 2 (сигнатура) |
| Fail-soft — сбой одной задачи не останавливает | Task 2 (шаги 4–6) + Unit-тест |
| `embed_documents` не вызывается для unchanged | Task 2 + Unit-тест |
| Пустой вход → `[]` | Task 2 + Unit-тест |
| Обратная совместимость `index_task` | Task 3 (не трогаем `index_task`) |
| Unit-тест `index_batch` | Task 1 |
| Integration-тест batch vs single | Task 5 |

**Типы и сигнатуры согласованы:** `index_batch(tasks: list[dict]) -> list[dict]` → `index_tasks_batch` → MCP → SKILL.md.
