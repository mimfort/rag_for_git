# Store-first чтение одиночной задачи в solve-task (PRI-160) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать `solve-task` read-путь по ключу в собственный стор reviewer (`get_task`), чтобы одиночная задача читалась из Postgres `tasks` (заполнен preflight-синком), а не повторно через board-MCP на стороне LLM.

**Architecture:** Новый сквозной путь `TaskStore.get_task` → `TaskService.get_task` → `MCPReviewService.get_task` → MCP-тул `get_task(key)`, возвращающий нормализованный TaskBrief из стора. Скилл `solve-task` шаг 2 становится store-first: сначала `get_task`, при miss — существующий board-MCP-фолбэк (целиком сохранён). Граф в этом пути не участвует (links/PRs остаются за `get_task_context`).

**Tech Stack:** Python 3.11–3.13, psycopg/psycopg_pool (Postgres/ParadeDB), FastMCP, pytest (маркер `integration` для тестов с живой БД).

## Global Constraints

- Python 3.11–3.13; ruff line-length 100, target py311.
- Язык кода — русский: комментарии, докстринги, CLI-сообщения. Докстринги MCP-тулов — по-английски (как существующие тулы), но содержательные строки/ноты — русские.
- Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- `pytest` по умолчанию исключает `integration` (`addopts = -m 'not integration'`). Integration-тесты требуют `docker compose up -d` (ParadeDB :5433 + Neo4j) и гоняются `pytest -m integration`.
- Ветка работы: `feat/pri-160-solve-task-store-first` (уже создана).
- Задачи **глобальны** — без repo/branch-скоупа; `get_task` принимает только `key`.
- Fail-open сквозной: любой сбой/miss на пути `get_task` → `None`/`null`, скилл фолбэкает; тул никогда не бросает исключение в LLM.

---

### Task 1: `TaskStore.get_task` — read по ключу/алиасу из Postgres

**Files:**
- Modify: `reviewer/tasks/store.py` (добавить метод в класс `TaskStore`, рядом с `existing_hash`/`list_keys`)
- Test: `tests/tasks/test_integration.py` (новый тест round-trip; маркер `integration`)

**Interfaces:**
- Consumes: существующие `TaskRow` (dataclass), `TaskStore._connect()`.
- Produces: `TaskStore.get_task(key: str) -> TaskRow | None`. Возвращает `TaskRow` с
  `embedding=[]` (эмбеддинг для брифа не нужен и не читается), либо `None`, если строки нет.
  Матч `key = %s OR %s = ANY(aliases)`.

- [ ] **Step 1: Написать падающий integration-тест**

В `tests/tasks/test_integration.py` добавить (после `test_taskstore_upsert_and_search`, использует фикстуру `store` и `_FakeEmbedder`):

```python
def test_taskstore_get_task_by_key_and_alias(store):
    emb = _FakeEmbedder()
    text = build_task_text("Add logout", "Clear the session on logout", [])
    store.upsert_task(TaskRow(
        key="ID-1", aliases=["PRI-1"], title="Add logout",
        description="Clear the session on logout", status="Open", url="u",
        content_hash=task_content_hash(text), text=text,
        embedding=emb.embed_documents([text])[0]))

    by_key = store.get_task("ID-1")
    assert by_key is not None
    assert by_key.key == "ID-1" and by_key.aliases == ["PRI-1"]
    assert by_key.title == "Add logout"
    assert by_key.description == "Clear the session on logout"
    assert by_key.status == "Open" and by_key.url == "u"

    by_alias = store.get_task("PRI-1")          # резолв по alias находит ту же задачу
    assert by_alias is not None and by_alias.key == "ID-1"

    assert store.get_task("ZZ-404") is None      # промах
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_integration.py::test_taskstore_get_task_by_key_and_alias -m integration -v`
Expected: FAIL — `AttributeError: 'TaskStore' object has no attribute 'get_task'`.
(Требует `docker compose up -d`. Если Postgres недоступен — поднять стек до прогона.)

- [ ] **Step 3: Реализовать `get_task`**

В `reviewer/tasks/store.py`, в классе `TaskStore` (например, после `existing_hash`, перед `upsert_task`):

```python
    def get_task(self, key: str) -> TaskRow | None:
        """Задача по ключу или алиасу (для store-first одиночного чтения в /solve-task).

        Матч по каноническому ``key`` ИЛИ по ``aliases`` (стор ключует по ID-N, а
        вызов часто передаёт проектный PRI-N). Эмбеддинг не читается (не нужен для
        брифа) — в TaskRow ставится []. None, если задачи нет.
        """
        sql = """
        SELECT key, aliases, title, description, status, url, content_hash, text
        FROM tasks WHERE key = %s OR %s = ANY(aliases) LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(sql, (key, key)).fetchone()
        if row is None:
            return None
        return TaskRow(
            key=row[0], aliases=list(row[1] or []), title=row[2],
            description=row[3], status=row[4], url=row[5],
            content_hash=row[6], text=row[7], embedding=[])
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/test_integration.py::test_taskstore_get_task_by_key_and_alias -m integration -v`
Expected: PASS.

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/tasks/store.py tests/tasks/test_integration.py`
Expected: без новых ошибок.

- [ ] **Step 6: Commit**

```bash
git add reviewer/tasks/store.py tests/tasks/test_integration.py
git commit -m "feat(tasks): TaskStore.get_task — чтение задачи по ключу/алиасу (PRI-160)"
```

---

### Task 2: `TaskService.get_task` — нормализация TaskRow → TaskBrief (fail-soft)

**Files:**
- Modify: `reviewer/tasks/service.py` (метод в `TaskService`, рядом с `get_task_context`)
- Test: `tests/tasks/test_service.py` (unit, фейк-стор)

**Interfaces:**
- Consumes: `TaskStore.get_task(key) -> TaskRow | None` (Task 1).
- Produces: `TaskService.get_task(key: str) -> dict | None`. Хит → словарь
  `{key, aliases, title, description, criteria: [], status, url}`. Miss/ошибка стора → `None`.

- [ ] **Step 1: Расширить `_FakeStore` и написать падающие unit-тесты**

В `tests/tasks/test_service.py` обновить `_FakeStore` — добавить хранилище строк и `get_task`:

```python
class _FakeStore:
    def __init__(self, hashes=None, search_result=None, rows=None):
        self._hashes = dict(hashes or {})
        self.upserted = []
        self.meta_updates = []
        self.deleted = []
        self._search_result = search_result or []
        self._rows = list(rows or [])      # list[TaskRow] для get_task

    # ... существующие методы без изменений ...

    def get_task(self, key):
        for r in self._rows:
            if r.key == key or key in (r.aliases or []):
                return r
        return None
```

Затем добавить тесты (в конец файла):

```python
def test_get_task_hit_returns_normalized_brief():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=["PRI-1"], title="Add logout",
                  description="Clear session", status="Open", url="u",
                  content_hash="h", text="t", embedding=[])
    svc = TaskService(_FakeStore(rows=[row]), _FakeGraph(), _FakeEmbedder())
    out = svc.get_task("ID-1")
    assert out == {"key": "ID-1", "aliases": ["PRI-1"], "title": "Add logout",
                   "description": "Clear session", "criteria": [],
                   "status": "Open", "url": "u"}


def test_get_task_resolves_by_alias():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=["PRI-1"], title="T", description="d",
                  status=None, url=None, content_hash="h", text="t", embedding=[])
    out = TaskService(_FakeStore(rows=[row]), _FakeGraph(), _FakeEmbedder()).get_task("PRI-1")
    assert out is not None and out["key"] == "ID-1"


def test_get_task_miss_returns_none():
    out = TaskService(_FakeStore(rows=[]), _FakeGraph(), _FakeEmbedder()).get_task("ZZ-9")
    assert out is None


def test_get_task_store_error_returns_none_not_raise():
    class _BrokenStore(_FakeStore):
        def get_task(self, key):
            raise RuntimeError("pg down")
    out = TaskService(_BrokenStore(), _FakeGraph(), _FakeEmbedder()).get_task("ID-1")
    assert out is None
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k get_task_hit -v`
Expected: FAIL — `AttributeError: 'TaskService' object has no attribute 'get_task'`.

- [ ] **Step 3: Реализовать `TaskService.get_task`**

В `reviewer/tasks/service.py`, в `TaskService` (после `get_task_context`):

```python
    def get_task(self, key: str) -> dict | None:
        """Нормализованный TaskBrief задачи из стора (store-first одиночное чтение).

        Источник — Postgres ``tasks`` (заполнен sync_board). Граф не трогаем: links/PRs
        остаются за get_task_context. criteria=[] — требования несёт description
        (как в board-MCP-пути). Miss/сбой стора → None, чтобы вызывающий фолбэкнул.
        """
        try:
            row = self._store.get_task(key)
        except Exception:
            log.warning("get_task: сбой стора для %s", key, exc_info=True)
            return None
        if row is None:
            return None
        return {
            "key": row.key,
            "aliases": list(row.aliases or []),
            "title": row.title,
            "description": row.description,
            "criteria": [],
            "status": row.status,
            "url": row.url,
        }
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k get_task -v`
Expected: PASS (4 теста: hit, by_alias, miss, store_error).

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/tasks/service.py tests/tasks/test_service.py`
Expected: без новых ошибок.

- [ ] **Step 6: Commit**

```bash
git add reviewer/tasks/service.py tests/tasks/test_service.py
git commit -m "feat(tasks): TaskService.get_task — нормализация в TaskBrief, fail-soft (PRI-160)"
```

---

### Task 3: `MCPReviewService.get_task` — делегат в task_service

**Files:**
- Modify: `reviewer/mcp/service.py` (метод в `MCPReviewService`, рядом с `get_task_context`)
- Test: `tests/mcp/test_service.py` (unit, MagicMock components)

**Interfaces:**
- Consumes: `self.components.task_service.get_task(key) -> dict | None` (Task 2).
- Produces: `MCPReviewService.get_task(key: str) -> dict | None`.

- [ ] **Step 1: Написать падающий unit-тест**

В `tests/mcp/test_service.py` добавить (рядом с `test_task_tool_delegates`, использует хелпер `_make_mcp_service`):

```python
def test_get_task_delegates_to_task_service():
    """MCPReviewService.get_task делегирует в task_service.get_task."""
    svc = _make_mcp_service()
    brief = {"key": "ID-1", "aliases": ["PRI-1"], "title": "T",
             "description": "d", "criteria": [], "status": "Open", "url": "u"}
    svc.components.task_service.get_task.return_value = brief
    assert svc.get_task("PRI-1") == brief
    svc.components.task_service.get_task.assert_called_once_with("PRI-1")


def test_get_task_miss_returns_none():
    svc = _make_mcp_service()
    svc.components.task_service.get_task.return_value = None
    assert svc.get_task("ZZ-9") is None
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k "get_task_delegates or get_task_miss" -v`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'get_task'`.

- [ ] **Step 3: Реализовать делегат**

В `reviewer/mcp/service.py`, в `MCPReviewService` (сразу после `get_task_context`, ~service.py:318):

```python
    def get_task(self, key: str) -> dict | None:
        """Нормализованный TaskBrief задачи из стора (store-first /solve-task).

        В отличие от get_task_context (граф: связи/PR/код) — это собственный контент
        задачи (title/description/status/url) из Postgres. None, если задачи нет в сторе.
        """
        return self.components.task_service.get_task(key)
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k "get_task_delegates or get_task_miss" -v`
Expected: PASS.

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_service.py`
Expected: без новых ошибок.

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): MCPReviewService.get_task — делегат store-first чтения (PRI-160)"
```

---

### Task 4: MCP-тул `get_task` — регистрация в FastMCP-сервере

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py` (новый `@mcp.tool()` в `create_server`; обновить число тулов в докстринге)
- Test: `tests/mcp/test_server_tools.py` (unit, MagicMock service, `asyncio` + `list_tools`/`call_tool`)

**Interfaces:**
- Consumes: `service.get_task(key) -> dict | None` (Task 3).
- Produces: MCP-тул `get_task(key: str) -> dict | None`, имя тула — `get_task`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_server_tools.py` добавить (хелпер `_service()` уже есть в файле):

```python
def test_get_task_tool_registered():
    import asyncio
    svc = _service()
    svc.get_task.return_value = {"key": "ID-1"}
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_task" in names


def test_get_task_tool_forwards_key():
    import asyncio
    svc = _service()
    svc.get_task.return_value = {"key": "ID-1", "title": "T"}
    server = create_server(svc)
    asyncio.run(server.call_tool("get_task", {"key": "PRI-1"}))
    svc.get_task.assert_called_once_with("PRI-1")
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -k get_task -v`
Expected: FAIL — `get_task` отсутствует в `names` / тул не найден при `call_tool`.

- [ ] **Step 3: Зарегистрировать тул и обновить счётчик**

В `reviewer/entrypoints/mcp_server.py`, в `create_server`, после блока `get_task_context` (~mcp_server.py:115):

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

И обновить число тулов в докстринге `create_server` (первая строка):

```python
    """Создать и вернуть сконфигурированный FastMCP-сервер с 21 тулом.
```

(было «с 20 тулами»).

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -k get_task -v`
Expected: PASS (2 теста).

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py`
Expected: без новых ошибок.

- [ ] **Step 6: Commit**

```bash
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): тул get_task — store-first одиночное чтение задачи (PRI-160)"
```

---

### Task 5: Скилл `solve-task` шаг 2 — store-first + правка доков

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (шаг 2 «Identify the task», строки ~53–58)
- Modify: `CLAUDE.md` (строка про одиночное чтение задачи в `task_board`-факте)

**Interfaces:**
- Consumes: MCP-тул `get_task(key)` (Task 4).
- Produces: ничего для кода — это документация/промпт скилла.

- [ ] **Step 1: Переписать шаг 2 на store-first**

В `plugin/skills/solve-task/SKILL.md` заменить блок шага 2 (текущие строки 53–58):

````markdown
2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern`:
     1. **Store-first.** Call reviewer `get_task(key)` — it returns the task's own normalized
        content (`{key, aliases[], title, description, criteria[], status, url}`) from the reviewer
        store, which the preflight `sync_board` (step 0.3) just refreshed.
        - **Hit** (a task object with a `key`): use it directly as the `TaskBrief`. The task is
          already indexed (the preflight sync persisted it) — do NOT call `index_task`. Note in the
          brief that the task data came from the reviewer store (after sync).
        - **Miss** (`null` / no `key`) AND a board is configured/connected: read the task via the
          playbook `../review-pr/references/task-context-<task_board.type>.md`, build a `TaskBrief`
          `{key, aliases[], title, description, criteria[], status, url, links[]}`, then call
          `index_task(TaskBrief)` to persist it (idempotent — safe to repeat).
        - **Miss** AND no board (or board MCP not connected): board-less — treat `$ARGUMENTS` as the
          task description.
   - Otherwise: treat `$ARGUMENTS` as the task description; do not read the board.

   Store-first cuts the double-fetch: the preflight `sync_board` already pulled the whole board into
   the reviewer store, so a single read of our own store avoids re-enumerating the board via board-MCP
   (fewer LLM tokens, fewer external deps). The board-MCP fallback stays for misses and for boards
   without a REST provider.
````

- [ ] **Step 2: Обновить факт в `CLAUDE.md`**

В `CLAUDE.md`, в bullet про `task_board` («Конфиг доски задач…»), найти фрагмент про клиентский скилл solve-task и заменить описание одиночного чтения. Заменить текст:

`клиентский скил solve-task (одиночное чтение задачи) — через MCP-тул get_board_config() + board-MCP на стороне LLM (фолбэк, когда в локальном .review.yml нет блока).`

на:

`клиентский скил solve-task читает одиночную задачу store-first — через MCP-тул get_task() из стора reviewer (после sync_board); board-MCP на стороне LLM остаётся фолбэком при промахе стора или для досок без REST-провайдера.`

- [ ] **Step 3: Проверка консистентности скилла (ручная)**

Перечитать `plugin/skills/solve-task/SKILL.md` шаги 2–3 и секцию «Failure handling»: убедиться, что
- store-first идёт ПЕРЕД board-MCP-плейбуком;
- board-MCP-фолбэк и board-less ветки сохранены;
- шаг 3 (`get_task_context`/`search_codebase`) не затронут (links/PRs по-прежнему через `get_task_context`).

- [ ] **Step 4: Прогнать весь unit-набор (регрессия)**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключён дефолтно). Сбоев нет.

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/solve-task/SKILL.md CLAUDE.md
git commit -m "feat(skill): solve-task — store-first одиночное чтение задачи через get_task (PRI-160)"
```

---

### Task 6: Финальная верификация и интеграция

**Files:** —

- [ ] **Step 1: Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 2: Integration-прогон (требует `docker compose up -d`)**

Run: `.venv/bin/pytest -m integration -q`
Expected: PASS (включая `test_taskstore_get_task_by_key_and_alias`).
Если Postgres/Neo4j недоступны — поднять `docker compose up -d` и повторить; зафиксировать в отчёте, если пропущено.

- [ ] **Step 3: Линт всего диффа**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок (см. заметку: ruff на main не обязан быть полностью чистым — важно не вносить новых).

- [ ] **Step 4: Завершение ветки**

Использовать `superpowers:finishing-a-development-branch` — открыть PR в `dev` (обе ветки защищены PR-required). Тело PR — на русском, без self-attribution.

---

## Self-Review (выполнено при написании плана)

**Spec coverage:**
- Стор `get_task` (key/alias) → Task 1. ✓
- Сервис `get_task` (нормализация, fail-soft, criteria=[], store-only) → Task 2. ✓
- MCP-сервис делегат → Task 3. ✓
- MCP-тул `get_task` + докстринг-отличие от `get_task_context` + счётчик тулов → Task 4. ✓
- Скилл шаг 2 store-first + фолбэк сохранён → Task 5; доковая консистентность (CLAUDE.md) → Task 5. ✓
- Тесты: unit (service, mcp-service, tool registration) + integration (store round-trip by key/alias) → Tasks 1–4. ✓
- Решения YAGNI (нет updated_at / criteria-колонки / merge графа / удаления фолбэка) — отражены в коде/доках. ✓

**Placeholder scan:** плейсхолдеров нет — весь код приведён дословно.

**Type consistency:** `get_task(key: str) -> dict | None` единообразно на всех слоях; `TaskStore.get_task -> TaskRow | None`; имя тула `get_task`; миссы → `None`/`null` сквозь все слои. ✓
