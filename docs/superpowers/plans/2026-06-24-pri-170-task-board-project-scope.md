# PRI-170 — Скоуп задач по project из `.review.yml` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Изолировать задачи по проекту доски: синк и выдача (поиск/граф) из репо работают только с проектом из `.review.yml task_board.project`; пусто = текущее глобальное поведение.

**Architecture:** Каждая задача тегируется меткой `project` (префикс кода: `PRI`, `TES`) при синке — колонка `tasks.project` + проперти `:Task.project`. Запись (`sync_board`) скоупится по одному типу доски + проекту; чтение (`search_tasks`/`get_task`/`get_task_context`) фильтруется по `project`, который клиент-скилы передают из `.review.yml`. Хранилище остаётся глобальным — изоляция через фильтр. Кросс-проект связи отсекаются на чтении.

**Tech Stack:** Python 3.11–3.13, pydantic-settings, Postgres/pgvector + pg_search (ParadeDB :5433), Neo4j (:7687), FastMCP, Click, pytest (маркер `integration` исключён по умолчанию), ruff (line-length 100).

## Global Constraints

- Язык кода/комментариев/докстрингов/CLI — **русский**.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/Claude).
- TDD: unit на фейках (внешние API не дёргают); integration — под маркером `integration` (живые Postgres+Neo4j, без Voyage; фейковый эмбеддер).
- ruff line-length 100, target py311. Прогон: `.venv/bin/ruff check .`.
- Back-compat: `project=None`/`""` в read-тулах и `board_type=None` в `sync_board` = текущее поведение (всё).
- `project` = префикс кода задачи (`PRI-5` → `PRI`); один литерал для штампа, enumerate-фильтра и read-фильтра.
- Запуск unit-тестов: `.venv/bin/pytest -q`. Integration: `.venv/bin/pytest -m integration` (нужен `docker compose up -d`).
- Спека: `docs/superpowers/specs/2026-06-24-pri-170-task-board-project-scope-design.md`.

---

## File Structure

| Файл | Ответственность изменения |
|---|---|
| `reviewer/tasks/boards/base.py` | хелпер `project_prefix(code)` |
| `reviewer/tasks/boards/yougile.py` | `normalize_yougile`: `project`; `iter_raw`: префикс-фильтр |
| `reviewer/tasks/boards/youtrack.py` | `normalize_youtrack`: `project` |
| `reviewer/index/schema.sql` | `ALTER TABLE tasks ADD COLUMN project` |
| `reviewer/tasks/store.py` | `TaskRow.project`; upsert/update_meta/get_task/search/list_keys — колонка+фильтр |
| `reviewer/tasks/graph.py` | `:Task.project`; upsert_task/task_context/list_keys/keys_with_prs — проперти+фильтр |
| `reviewer/tasks/service.py` | `index_task`/`index_batch` несут `project`; read+purge — `project`-параметр |
| `reviewer/tasks/sync.py` | `run(board_type)` — скоуп по типу + scoped purge |
| `reviewer/mcp/service.py` | `sync_board(board_type)`; read-тулы `project` |
| `reviewer/entrypoints/mcp_server.py` | сигнатуры тулов |
| `plugin/skills/{configure-review,solve-task,sync-tasks,review-pr}/SKILL.md` | вопрос про проект + проброс |
| `.review.yml`, `CLAUDE.md` | `task_board.project: PRI`; уточнение инварианта |

**Порядок задач** идёт снизу вверх по слоям (модель → стор → граф → сервис → синк → MCP → e2e → скилы), чтобы каждый слой опирался на готовый нижний.

---

### Task 1: `project_prefix` + проброс `project` в normalize

**Files:**
- Modify: `reviewer/tasks/boards/base.py`
- Modify: `reviewer/tasks/boards/yougile.py:25-68` (`normalize_yougile`), `:107-128` (`iter_raw`)
- Modify: `reviewer/tasks/boards/youtrack.py:63-88` (`normalize_youtrack`)
- Test: `tests/tasks/boards/test_base.py`, `tests/tasks/boards/test_yougile_normalize.py`, `tests/tasks/boards/test_youtrack_normalize.py`

**Interfaces:**
- Produces: `project_prefix(code: str) -> str` (в `reviewer/tasks/boards/base.py`, реэкспорт из `reviewer/tasks/boards/__init__.py`); `normalize_yougile`/`normalize_youtrack` возвращают dict с ключом `"project": str`.
- Consumes: ничего нового.

- [ ] **Step 1: Написать падающие тесты `project_prefix` (base)**

В `tests/tasks/boards/test_base.py` добавить:
```python
from reviewer.tasks.boards.base import project_prefix


def test_project_prefix_extracts_alpha_prefix():
    assert project_prefix("PRI-5") == "PRI"
    assert project_prefix("TES-1") == "TES"
    assert project_prefix("0DEV-7") == ""        # код должен начинаться с буквы
    assert project_prefix("") == ""
    assert project_prefix("UUID-NO-NUM") == ""   # хвост не число → не код задачи
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py::test_project_prefix_extracts_alpha_prefix -q`
Expected: FAIL (`ImportError: cannot import name 'project_prefix'`).

- [ ] **Step 3: Реализовать `project_prefix` в base.py**

В начало `reviewer/tasks/boards/base.py` (после `from typing import Protocol`) добавить:
```python
import re

_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-\d+$")


def project_prefix(code: str) -> str:
    """Префикс проекта из кода задачи: ``PRI-5`` → ``PRI``. ``""`` если не код вида
    ``<PREFIX>-<число>`` (метка скоупа по проекту, PRI-170)."""
    m = _PREFIX_RE.match(code or "")
    return m.group(1) if m else ""
```
И реэкспортировать в `reviewer/tasks/boards/__init__.py`:
```python
from reviewer.tasks.boards.base import RawTask, TaskBoardProvider, project_prefix

__all__ = ["RawTask", "TaskBoardProvider", "project_prefix", "make_board_provider",
           "make_board_providers"]
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `.venv/bin/pytest tests/tasks/boards/test_base.py::test_project_prefix_extracts_alpha_prefix -q`
Expected: PASS.

- [ ] **Step 5: Написать падающие тесты passthrough `project` в normalize**

В `tests/tasks/boards/test_yougile_normalize.py` добавить:
```python
def test_normalize_sets_project_prefix():
    b = normalize_yougile(_raw(project_code="PRI-10"), KP, URL)
    assert b["project"] == "PRI"
```
В `tests/tasks/boards/test_youtrack_normalize.py` добавить:
```python
def test_normalize_sets_project_prefix():
    raw = _issue_to_raw(_issue(idReadable="PRJ-7"))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["project"] == "PRJ"
```

- [ ] **Step 6: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/ -q -k project_prefix`
Expected: 2 теста FAIL (`KeyError: 'project'`).

- [ ] **Step 7: Добавить `project` в оба normalize**

В `reviewer/tasks/boards/yougile.py`: импортировать хелпер и в возвращаемый dict `normalize_yougile` (после `"links": links,`) добавить ключ:
```python
from reviewer.tasks.boards.base import RawTask, project_prefix
```
```python
    return {
        "key": key,
        "aliases": aliases,
        "title": raw.title,
        "description": raw.description,
        "criteria": [],
        "status": raw.status,
        "url": url,
        "links": links,
        "project": project_prefix(raw.project_code or key),
    }
```
В `reviewer/tasks/boards/youtrack.py`: импортировать хелпер и в возвращаемый dict `normalize_youtrack` (после `"links": links,`) добавить:
```python
from reviewer.tasks.boards.base import RawTask, project_prefix
```
```python
        "links": links,
        "project": project_prefix(raw.key),
    }
```

- [ ] **Step 8: Прогнать — зелёный**

Run: `.venv/bin/pytest tests/tasks/boards/ -q`
Expected: PASS (включая старые normalize-тесты — поведение related-связей не менялось).

- [ ] **Step 9: yougile `iter_raw` — префикс-фильтр вместо title-матча**

В `reviewer/tasks/boards/yougile.py` `iter_raw` (строки 107-128) заменить board-level title-фильтр на task-level префикс-фильтр. Было:
```python
    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        for proj in self._get_all("/projects"):
            for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                if board and board not in (brd.get("title", ""), proj.get("title", "")):
                    continue
                col_title = {c["id"]: c.get("title")
                             for c in self._get_all("/columns", {"boardId": brd["id"]})}
                for col_id in col_title:
                    for t in self._get_all("/tasks", {"columnId": col_id}):
                        yield RawTask(
                            key=t.get("idTaskCommon") or t["id"],
                            project_code=t.get("idTaskProject", ""),
                            title=t.get("title", ""),
                            description=t.get("description", "") or "",
                            status=col_title.get(t.get("columnId")),
                            subtask_ids=list(t.get("subtasks", []) or []),
                            timestamp=int(t.get("timestamp", 0) or 0),
                        )
                        count += 1
                        if limit and count >= limit:
                            return
```
Стало (убрать board-level `continue`, добавить task-level префикс-фильтр):
```python
    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        for proj in self._get_all("/projects"):
            for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                col_title = {c["id"]: c.get("title")
                             for c in self._get_all("/columns", {"boardId": brd["id"]})}
                for col_id in col_title:
                    for t in self._get_all("/tasks", {"columnId": col_id}):
                        project_code = t.get("idTaskProject", "")
                        # PRI-170: scoped-синк ограничивает доску одним проектом по
                        # префиксу кода (board == project_prefix), а не по title.
                        if board and project_prefix(project_code) != board:
                            continue
                        yield RawTask(
                            key=t.get("idTaskCommon") or t["id"],
                            project_code=project_code,
                            title=t.get("title", ""),
                            description=t.get("description", "") or "",
                            status=col_title.get(t.get("columnId")),
                            subtask_ids=list(t.get("subtasks", []) or []),
                            timestamp=int(t.get("timestamp", 0) or 0),
                        )
                        count += 1
                        if limit and count >= limit:
                            return
```
(Поведение `iter_raw` HTTP не юнит-тестируется; покрывается integration-синком в Task 7. Семантика `--board` теперь — префикс кода проекта, не title; это отражается в Task 8 в SKILL-доке.)

- [ ] **Step 10: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/boards/ && .venv/bin/pytest tests/tasks/boards/ -q`
Expected: чисто, PASS.
```bash
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/__init__.py \
        reviewer/tasks/boards/yougile.py reviewer/tasks/boards/youtrack.py \
        tests/tasks/boards/test_base.py tests/tasks/boards/test_yougile_normalize.py \
        tests/tasks/boards/test_youtrack_normalize.py
git commit -m "feat(tasks): project_prefix + метка project в normalize досок (PRI-170)"
```

---

### Task 2: `TaskStore` — колонка `project`, фильтры, миграция

**Files:**
- Modify: `reviewer/index/schema.sql:54-67`
- Modify: `reviewer/tasks/store.py` (`TaskRow:39-49`, `get_task:99-117`, `upsert_task:119-139`, `update_meta:141-149`, `list_keys:151-155`, `search:168-196`)
- Test: `tests/tasks/test_integration.py` (маркер `integration`)

**Interfaces:**
- Consumes: `project_prefix` (Task 1) — косвенно (значение приходит готовым в `TaskRow.project`).
- Produces: `TaskRow(..., project: str = "")`; `TaskStore.upsert_task(row)` пишет `project`; `TaskStore.update_meta(key, title, status, url, aliases, project="")`; `TaskStore.get_task(key, project=None) -> TaskRow|None`; `TaskStore.search(query_text, query_embedding, top_k=5, candidates=50, project=None)`; `TaskStore.list_keys(project=None) -> list[str]`. `TaskRow` теперь имеет атрибут `project`.

- [ ] **Step 1: Написать падающий integration-тест скоупа стора**

В `tests/tasks/test_integration.py` добавить (после существующего `test_taskstore_upsert_and_search`):
```python
def test_taskstore_search_and_get_scoped_by_project(store):
    emb = _FakeEmbedder()
    for key, proj, title in [("ID-1", "PRI", "logout flow"),
                             ("ID-2", "TES", "logout flow")]:
        text = build_task_text(title, "session logout", [])
        store.upsert_task(TaskRow(
            key=key, aliases=[], title=title, description="session logout",
            status="Open", url=None, content_hash=task_content_hash(text),
            text=text, embedding=emb.embed_query(text), project=proj))
    # search скоупнут по проекту
    hits = store.search("logout", emb.embed_query("logout"), top_k=10, project="PRI")
    assert {h.key for h in hits} == {"ID-1"}
    # get_task скоупнут: чужой проект не виден
    assert store.get_task("ID-2", project="PRI") is None
    assert store.get_task("ID-2", project="TES").project == "TES"
    # list_keys скоупнут
    assert store.list_keys(project="PRI") == ["ID-1"]
    # без фильтра — обе (back-compat)
    assert set(store.list_keys()) == {"ID-1", "ID-2"}
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `docker compose up -d && .venv/bin/pytest -m integration tests/tasks/test_integration.py::test_taskstore_search_and_get_scoped_by_project -q`
Expected: FAIL (`TypeError: upsert_task ... unexpected keyword 'project'` или колонки нет).

- [ ] **Step 3: Миграция schema.sql**

В `reviewer/index/schema.sql` сразу после блока создания таблицы `tasks` (после строки 67, перед `CREATE INDEX ... tasks_bm25`) добавить:
```sql
-- PRI-170: скоуп задач по проекту доски. Выдача и синк фильтруются по project
-- (префикс кода: PRI, TES). Пусто = вне проекта (исключается из scoped-чтения).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT '';
```

- [ ] **Step 4: `TaskRow.project` + upsert/update_meta**

В `reviewer/tasks/store.py` в dataclass `TaskRow` (после `embedding: list[float]`) добавить:
```python
    project: str = ""
```
В `upsert_task` — добавить колонку в INSERT, ON CONFLICT и params:
```python
        sql = """
        INSERT INTO tasks (key, aliases, title, description, status, url,
                           content_hash, text, embedding, project)
        VALUES (%(key)s,%(aliases)s,%(title)s,%(description)s,%(status)s,%(url)s,
                %(content_hash)s,%(text)s,%(embedding)s,%(project)s)
        ON CONFLICT (key) DO UPDATE SET
            aliases=EXCLUDED.aliases, title=EXCLUDED.title,
            description=EXCLUDED.description, status=EXCLUDED.status,
            url=EXCLUDED.url, content_hash=EXCLUDED.content_hash,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding, project=EXCLUDED.project
        """
        params = {
            "key": row.key, "aliases": row.aliases, "title": row.title,
            "description": row.description, "status": row.status, "url": row.url,
            "content_hash": row.content_hash, "text": row.text,
            "embedding": row.embedding, "project": row.project,
        }
```
В `update_meta` — добавить параметр `project` и колонку (бэкфилл метки без переэмбеда):
```python
    def update_meta(self, key: str, title: str, status: str | None,
                    url: str | None, aliases: list[str], project: str = "") -> None:
        """Обновить лёгкие метаданные без переэмбеда (когда content_hash совпал)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s, project=%s "
                "WHERE key=%s",
                (title, status, url, aliases, project, key),
            )
            conn.commit()
```

- [ ] **Step 5: `get_task`/`search`/`list_keys` — фильтр по project**

`get_task`:
```python
    def get_task(self, key: str, project: str | None = None) -> TaskRow | None:
        """Задача по ключу/алиасу; при project — только из этого проекта (PRI-170)."""
        sql = ("SELECT key, aliases, title, description, status, url, "
               "content_hash, text, project FROM tasks "
               "WHERE (key = %s OR %s = ANY(aliases))")
        params: list = [key, key]
        if project:
            sql += " AND project = %s"
            params.append(project)
        sql += " LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return TaskRow(
            key=row[0], aliases=list(row[1] or []), title=row[2],
            description=row[3], status=row[4], url=row[5],
            content_hash=row[6], text=row[7], embedding=[], project=row[8])
```
`list_keys`:
```python
    def list_keys(self, project: str | None = None) -> list[str]:
        """Ключи задач; при project — только этого проекта (для scoped purge)."""
        sql = "SELECT key FROM tasks"
        params: list = []
        if project:
            sql += " WHERE project = %s"
            params.append(project)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]
```
`search` — добавить фильтр в обе ветки CTE:
```python
    def search(self, query_text: str, query_embedding: list[float],
               top_k: int = 5, candidates: int = 50,
               project: str | None = None) -> list[TaskHit]:
        """Гибрид RRF (BM25 ⊕ ANN). При project — скоуп по проекту (PRI-170)."""
        proj = "AND project = %(project)s" if project else ""
        sql = f"""
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM tasks WHERE text @@@ %(q)s {proj}
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM tasks WHERE TRUE {proj}
            ORDER BY embedding <=> %(vec)s LIMIT %(cand)s
        ),
        rrf AS (
            SELECT id, 1.0/(60+rank) AS s FROM bm25
            UNION ALL SELECT id, 1.0/(60+rank) AS s FROM ann
        )
        SELECT t.key, t.title, t.status, SUM(r.s) AS score
        FROM rrf r JOIN tasks t USING (id)
        GROUP BY t.id, t.key, t.title, t.status
        ORDER BY score DESC LIMIT %(k)s
        """
        params = {"q": _bm25_query(query_text), "vec": Vector(query_embedding),
                  "cand": candidates, "k": top_k}
        if project:
            params["project"] = project
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TaskHit(key=k, title=t, status=s, score=float(sc))
                for (k, t, s, sc) in rows]
```

- [ ] **Step 6: Прогнать — зелёный (+ не сломан старый тест)**

Run: `.venv/bin/pytest -m integration tests/tasks/test_integration.py -q`
Expected: PASS (новый + `test_taskstore_upsert_and_search`).
Примечание: фикстура `store` делает `TRUNCATE tasks`; колонка появляется через `cs.init_schema()`.

- [ ] **Step 7: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/store.py reviewer/index/schema.sql`
Expected: чисто.
```bash
git add reviewer/index/schema.sql reviewer/tasks/store.py tests/tasks/test_integration.py
git commit -m "feat(tasks): колонка tasks.project + фильтры стора по проекту (PRI-170)"
```

---

### Task 3: `TaskGraph` — проперти `:Task.project`, фильтр обхода/purge

**Files:**
- Modify: `reviewer/tasks/graph.py` (`upsert_task:31-38`, `task_context:106-134`, `keys_with_prs:136-141`, `list_keys:143-150`)
- Test: `tests/tasks/test_graph.py` (unit, FakeDriver), `tests/tasks/test_integration.py` (integration)

**Interfaces:**
- Produces: `TaskGraph.upsert_task(key, aliases, title, status, url, project="")`; `TaskGraph.task_context(key, project="") -> dict`; `TaskGraph.list_keys(project="") -> set[str]`; `TaskGraph.keys_with_prs(project="") -> set[str]`.
- Consumes: ничего нового.

- [ ] **Step 1: Написать падающие unit-тесты (FakeDriver)**

В `tests/tasks/test_graph.py` добавить:
```python
def test_upsert_task_sets_project_property():
    d = _FakeDriver()
    TaskGraph(d).upsert_task("ID-1", [], "T", "Open", "u", project="PRI")
    query, params = d.calls[0]
    assert params["project"] == "PRI"
    assert "t.project=$project" in query


def test_task_context_filters_neighbors_by_project():
    d = _FakeDriver(records=[])
    TaskGraph(d).task_context("ID-1", project="PRI")
    query, params = d.calls[0]
    assert params["project"] == "PRI"
    assert "n.project = $project" in query


def test_list_keys_scoped_by_project():
    d = _FakeDriver(records=[])
    TaskGraph(d).list_keys(project="PRI")
    query, params = d.calls[0]
    assert params["project"] == "PRI"
    assert "t.project = $project" in query


def test_keys_with_prs_scoped_by_project():
    d = _FakeDriver(records=[])
    TaskGraph(d).keys_with_prs(project="PRI")
    query, params = d.calls[0]
    assert params["project"] == "PRI"
    assert "t.project = $project" in query
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -q -k "project"`
Expected: FAIL (TypeError на лишний `project` / нет фильтра в Cypher).

- [ ] **Step 3: `upsert_task` — проперти project**

В `reviewer/tasks/graph.py` `upsert_task`:
```python
    def upsert_task(self, key: str, aliases: list[str], title: str,
                    status: str | None, url: str | None, project: str = "") -> None:
        """Upsert узла :Task. codes = [key, ...aliases]; project — метка скоупа (PRI-170)."""
        codes = [key] + [a for a in (aliases or []) if a and a != key]
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) "
            "SET t.codes=$codes, t.title=$title, t.status=$status, t.url=$url, "
            "t.project=$project",
            key=key, codes=codes, title=title, status=status, url=url, project=project)
```

- [ ] **Step 4: `task_context` — фильтр соседей по project**

```python
    def task_context(self, key: str, project: str = "") -> dict:
        """Обход: задача + её PR/код + TASK_LINK-соседи и их PR. {} если не найдена.

        При project != "" соседи-задачи фильтруются по n.project (стабы без project
        и задачи чужих проектов отсекаются — PRI-170, критерий 3).
        """
        records, _, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE $k IN t.codes "
            "RETURN t.key AS key, t.title AS title, t.status AS status, t.url AS url, "
            "[ (t)-[:IMPLEMENTED_BY]->(p:PR) | "
            "  {id: p.id, url: p.url, sha: p.sha, "
            "   touched: [ (p)-[:TOUCHES]->(s:Symbol) | s.id ]} ] AS prs, "
            "[ (t)-[l:TASK_LINK]-(n:Task) WHERE ($project = '' OR n.project = $project) | "
            "  {key: n.key, title: n.title, status: n.status, type: l.type, "
            "   prs: [ (n)-[:IMPLEMENTED_BY]->(np:PR) | {id: np.id, url: np.url} ]} ] AS linked "
            "LIMIT 1",
            k=key, project=project)
        if not records:
            return {}
        r = records[0]
        linked = []
        seen = set()
        for n in r["linked"]:
            sig = (n["key"], n.get("type"))
            if sig in seen:
                continue
            seen.add(sig)
            linked.append(n)
        return {"key": r["key"], "title": r["title"], "status": r["status"],
                "url": r["url"], "prs": r["prs"], "linked": linked}
```

- [ ] **Step 5: `keys_with_prs`/`list_keys` — фильтр по project**

```python
    def keys_with_prs(self, project: str = "") -> set[str]:
        """Ключи :Task с ребром IMPLEMENTED_BY; при project — только этого проекта."""
        if project:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) WHERE t.project = $project "
                "RETURN t.key AS key", project=project)
        else:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task)-[:IMPLEMENTED_BY]->(:PR) RETURN t.key AS key")
        return {r["key"] for r in records}

    def list_keys(self, project: str = "") -> set[str]:
        """Ключи всех :Task (включая стабы); при project — только этого проекта.

        Стабы (upsert_links/link_pr) project не имеют → при scoped purge не попадают
        в скоуп проекта и не вычищаются чужим синком (PRI-170)."""
        if project:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task) WHERE t.project = $project RETURN t.key AS key",
                project=project)
        else:
            records, _, _ = self._driver.execute_query(
                "MATCH (t:Task) RETURN t.key AS key")
        return {r["key"] for r in records}
```

- [ ] **Step 6: Прогнать unit — зелёный**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -q`
Expected: PASS (новые + старые; старые зовут `upsert_task`/`task_context` без project — дефолты сохраняют поведение).

- [ ] **Step 7: Integration-тест отсечения чужого соседа**

В `tests/tasks/test_integration.py` добавить (нужен фикстура графа; добавить рядом с существующими graph-тестами — использовать `TaskGraph` на живом Neo4j, см. паттерн `tests/tasks/test_graph.py` integration-части / `GraphStore`). Если в `test_integration.py` нет graph-фикстуры, добавить:
```python
from reviewer.graph.store import GraphStore


@pytest.fixture()
def tgraph():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.driver.execute_query("MATCH (t:Task) DETACH DELETE t")
    tg = TaskGraph(g.driver)
    yield tg
    g.driver.execute_query("MATCH (t:Task) DETACH DELETE t")
    g.close()


def test_task_context_excludes_foreign_project_neighbor(tgraph):
    tgraph.upsert_task("PRI-1", [], "наша", "Open", None, project="PRI")
    tgraph.upsert_task("TES-9", [], "чужая", "Open", None, project="TES")
    tgraph.upsert_links("PRI-1", [{"type": "related", "key": "TES-9"},
                                  {"type": "related", "key": "ABC-7"}])  # ABC-7 — стаб без project
    ctx = tgraph.task_context("PRI-1", project="PRI")
    assert {n["key"] for n in ctx["linked"]} == set()   # чужой проект и стаб отсечены
    ctx_all = tgraph.task_context("PRI-1")               # без скоупа — видно всё
    assert {"TES-9", "ABC-7"} <= {n["key"] for n in ctx_all["linked"]}
```

- [ ] **Step 8: Прогнать integration — зелёный**

Run: `.venv/bin/pytest -m integration tests/tasks/test_integration.py -q -k "project or context"`
Expected: PASS.

- [ ] **Step 9: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/graph.py`
Expected: чисто.
```bash
git add reviewer/tasks/graph.py tests/tasks/test_graph.py tests/tasks/test_integration.py
git commit -m "feat(tasks): :Task.project + фильтр обхода/purge графа по проекту (PRI-170)"
```

---

### Task 4: `TaskService` — проброс `project` (индексация, чтение, purge)

**Files:**
- Modify: `reviewer/tasks/service.py` (`index_task:26-83`, `index_batch:85-209`, `search_tasks:211-226`, `get_task_context:228-239`, `get_task:241-263`, `purge_orphaned_tasks:274-334`)
- Test: `tests/tasks/test_service.py`, `tests/tasks/test_service_batch.py`

**Interfaces:**
- Consumes: `TaskStore`/`TaskGraph` сигнатуры из Task 2/3.
- Produces: `TaskService.search_tasks(query, top_k=5, project=None)`; `get_task_context(key, project=None)`; `get_task(key, project=None)`; `purge_orphaned_tasks(active_keys, *, keep_with_prs=True, project=None)`. `index_task`/`index_batch` читают `task.get("project")` и пишут его в стор/граф.

- [ ] **Step 1: Обновить фейки в тестах (подготовка) + написать падающие тесты**

В `tests/tasks/test_service.py` обновить фейки, чтобы принимали `project`, и добавить тесты. Заменить методы `_FakeStore` и `_FakeGraph` на версии с `project`:
```python
    def update_meta(self, key, title, status, url, aliases, project=""):
        self.meta_updates.append((key, title, status, url, aliases, project))

    def search(self, q, vec, top_k=5, project=None):
        self.search_project = project
        return self._search_result

    def list_keys(self, project=None):
        self.list_keys_project = project
        return list(self._hashes.keys())

    def get_task(self, key, project=None):
        self.get_task_project = project
        for r in self._rows:
            if r.key == key or key in (r.aliases or []):
                return r
        return None
```
(добавить в `_FakeStore.__init__`: `self.search_project = self.list_keys_project = self.get_task_project = "unset"`.)
В `_FakeGraph`:
```python
    def upsert_task(self, key, aliases, title, status, url, project=""):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append((key, aliases, title, status, url, project))

    def task_context(self, key, project=""):
        if "task_context" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.task_context_project = project
        return self._context

    def list_keys(self, project=""):
        self.list_keys_project = project
        return set(self._keys)

    def keys_with_prs(self, project=""):
        self.keys_with_prs_project = project
        return set(self._pr_keys)
```
(сверить с фактическими телами в `tests/tasks/test_service.py:55+` — сохранить прочую логику; добавить параметры и запись project.)
Добавить тесты:
```python
def test_search_tasks_threads_project():
    from reviewer.tasks.store import TaskHit
    store = _FakeStore(search_result=[TaskHit(key="ID-1", title="t", status="Open", score=0.1)])
    svc = TaskService(store, _FakeGraph(), _FakeEmbedder())
    svc.search_tasks("q", project="PRI")
    assert store.search_project == "PRI"


def test_get_task_context_threads_project():
    g = _FakeGraph(context={"key": "ID-1", "title": "t", "status": None,
                            "url": None, "prs": [], "linked": []})
    svc = TaskService(_FakeStore(), g, _FakeEmbedder())
    svc.get_task_context("ID-1", project="PRI")
    assert g.task_context_project == "PRI"


def test_get_task_threads_project():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=[], title="t", description="d", status=None,
                  url=None, content_hash="h", text="t", embedding=[], project="PRI")
    store = _FakeStore(rows=[row])
    svc = TaskService(store, _FakeGraph(), _FakeEmbedder())
    svc.get_task("ID-1", project="PRI")
    assert store.get_task_project == "PRI"


def test_purge_threads_project_to_store_and_graph():
    store = _FakeStore(hashes={"ID-1": "h"})
    g = _FakeGraph(keys={"ID-1"}, pr_keys=set())
    svc = TaskService(store, g, _FakeEmbedder())
    svc.purge_orphaned_tasks(["ID-1"], project="PRI")
    assert store.list_keys_project == "PRI"
    assert g.list_keys_project == "PRI"
    assert g.keys_with_prs_project == "PRI"
```
Также нужен `_FakeEmbedder` в этом файле — если его нет, добавить:
```python
class _FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 8
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_service.py -q -k "project"`
Expected: FAIL (TypeError на `project` в TaskService-методах).

- [ ] **Step 3: Проброс project в read-методах + purge**

В `reviewer/tasks/service.py`:
```python
    def search_tasks(self, query: str, top_k: int = 5,
                     project: str | None = None) -> str:
        try:
            vec = self._embedder.embed_query(query)
            hits = self._store.search(query, vec, top_k=top_k, project=project)
        except Exception:
            log.warning("search_tasks: сбой поиска по запросу %r", query, exc_info=True)
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        return "\n".join(
            f"{i}. {h.key} [{h.status or '—'}] {h.title} (score {h.score:.4f})"
            for i, h in enumerate(hits, 1))

    def get_task_context(self, key: str, project: str | None = None) -> str:
        if self._graph is None:
            return "(task graph unavailable)"
        try:
            ctx = self._graph.task_context(key, project or "")
        except Exception:
            log.warning("get_task_context: сбой обхода графа для %s", key, exc_info=True)
            return "(task graph unavailable)"
        if not ctx:
            return f"(no task '{key}' in task graph)"
        return _format_task_context(ctx, self._max_chars)

    def get_task(self, key: str, project: str | None = None) -> dict | None:
        try:
            row = self._store.get_task(key, project=project)
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
В `purge_orphaned_tasks` добавить параметр `project` и прокинуть в три вызова:
```python
    def purge_orphaned_tasks(
        self,
        active_keys: list[str],
        *,
        keep_with_prs: bool = True,
        project: str | None = None,
    ) -> dict:
        """Удалить задачи вне active_keys. При project — скоуп по проекту (PRI-170)."""
        warnings: list[str] = []
        active = set(active_keys)
        all_keys: set[str] = set()
        try:
            all_keys |= set(self._store.list_keys(project=project))
        except Exception as e:
            log.warning("purge_orphaned_tasks: сбой list_keys", exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")
        if self._graph is not None:
            try:
                all_keys |= set(self._graph.list_keys(project or ""))
            except Exception as e:
                log.warning("purge_orphaned_tasks: сбой list_keys (graph)", exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        orphaned = all_keys - active
        protected: set[str] = set()

        if keep_with_prs and self._graph is not None:
            try:
                pr_keys = self._graph.keys_with_prs(project or "")
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

- [ ] **Step 4: Проброс project в индексацию (index_task + index_batch)**

В `index_task` (после `url = task.get("url")`) добавить:
```python
        project = task.get("project") or ""
```
В ветке совпадения хэша заменить `self._store.update_meta(key, title, status, url, aliases)` на:
```python
                self._store.update_meta(key, title, status, url, aliases, project)
```
В ветке переэмбеда — в `TaskRow(...)` добавить `project=project`. В вызов графа заменить
`self._graph.upsert_task(key, aliases, title, status, url)` на:
```python
                self._graph.upsert_task(key, aliases, title, status, url, project)
```
В `index_batch`: в шаге парсинга в словарь `parsed.append({...})` добавить `"project": task.get("project") or ""`. В шаге 4 (upsert) в `TaskRow(...)` добавить `project=p["project"]`. В шаге 5 (`update_meta`) добавить `p["project"]` шестым аргументом:
```python
                self._store.update_meta(p["key"], p["title"], p["status"],
                                        p["url"], p["aliases"], p["project"])
```
В шаге 6 (граф) заменить `self._graph.upsert_task(p["key"], p["aliases"], p["title"], p["status"], p["url"])` на:
```python
                    self._graph.upsert_task(p["key"], p["aliases"], p["title"],
                                            p["status"], p["url"], p["project"])
```

- [ ] **Step 5: Прогнать — зелёный**

Перед прогоном обновить фейки в `tests/tasks/test_service_batch.py` (у него отдельные `_FakeStore`/`_FakeGraph`), иначе новые позиционные/именованные `project`-аргументы упадут:
```python
    # _FakeStore.update_meta:
    def update_meta(self, key, title, status, url, aliases, project=""):
        self.meta_updates.append((key, title, status, url, aliases, project))

    # _FakeGraph.upsert_task:
    def upsert_task(self, key, aliases, title, status, url, project=""):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append(key)
```
И добавить тест, что батч штампует project (бэкфилл-путь через meta_only):
```python
def test_index_batch_stamps_project_on_meta_only():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})  # хэш совпал → meta_only
    g = _FakeGraph()
    TaskService(store, g, _FakeEmbedder()).index_batch([_brief(project="PRI")])
    assert store.meta_updates[0][-1] == "PRI"     # project прокинут в update_meta
    assert g.tasks == ["ID-1"]
```
(`_brief` уже принимает `**over` — `_brief(project="PRI")` добавит ключ `project`.)

Run: `.venv/bin/pytest tests/tasks/test_service.py tests/tasks/test_service_batch.py -q`
Expected: PASS.

- [ ] **Step 6: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/service.py`
Expected: чисто.
```bash
git add reviewer/tasks/service.py tests/tasks/test_service.py tests/tasks/test_service_batch.py
git commit -m "feat(tasks): TaskService пробрасывает project в индекс/чтение/purge (PRI-170)"
```

---

### Task 5: `SyncService` — скоуп по `board_type` + scoped purge

**Files:**
- Modify: `reviewer/tasks/sync.py` (`run:81-107`)
- Test: `tests/tasks/test_sync.py`

**Interfaces:**
- Consumes: `TaskService.purge_orphaned_tasks(..., project=...)` (Task 4).
- Produces: `SyncService.run(board=None, limit=None, purge_orphaned=False, keep_with_prs=True, board_type=None) -> dict`. При `board_type` — итерируется только провайдер этого типа; purge скоупится `project=board`.

- [ ] **Step 1: Обновить FakeTaskService + написать падающие тесты**

В `tests/tasks/test_sync.py` обновить `FakeTaskService.purge_orphaned_tasks`, чтобы принимал `project`:
```python
    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True, project=None):
        self.purged_with = (sorted(active_keys), keep_with_prs, project)
        return {"deleted_store": 1, "deleted_graph": 1, "protected_prs": 0,
                "warnings": []}
```
Обновить существующие ассерты `purged_with` (теперь кортеж из 3): в `test_purge_uses_full_active_keys` →
`assert ts.purged_with == (["ID-1", "ID-2"], False, None)`; в
`test_multi_provider_separate_cursors_and_union_purge` →
`assert ts.purged_with == (["ID-1", "ID-2"], True, None)`.
Добавить новые тесты:
```python
def test_board_type_scopes_to_one_provider():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    b = FakeProvider([_raw("ID-2", 300)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a, b], ts, meta).run(board_type="yougile")
    assert ts.indexed == [["ID-1"]]            # только yougile-провайдер
    assert summary["enumerated"] == 1
    assert ("", "tasks:youtrack:*") not in meta.store


def test_scoped_purge_passes_project():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    ts, meta = FakeTaskService(), FakeMeta({("", "tasks:yougile:PRI"): "999"})
    SyncService([a], ts, meta).run(board="PRI", board_type="yougile",
                                   purge_orphaned=True)
    assert ts.purged_with == (["ID-1"], True, "PRI")


def test_unknown_board_type_warns_and_indexes_nothing():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a], ts, meta).run(board_type="jira")
    assert summary["enumerated"] == 0
    assert any("jira" in w for w in summary["warnings"])
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL (TypeError на `board_type`; обновлённые `purged_with`-ассерты тоже до фикса упадут только если код ещё старый — это ожидаемо).

- [ ] **Step 3: Реализовать board_type-скоуп в `run`**

В `reviewer/tasks/sync.py` заменить `run` на:
```python
    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "warnings": [], "cursor_advanced": False}
        # PRI-170: scoped-синк из репо — только один тип доски (board_type), а не все.
        providers = self._providers
        if board_type is not None:
            providers = [p for p in self._providers if p.board_type == board_type]
            if not providers:
                agg["warnings"].append(
                    f"тип доски '{board_type}' не настроен на сервере")
        all_active: list[str] = []
        for provider in providers:
            active, one = self._sync_provider(provider, board, limit)
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            agg["warnings"].append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned:
            # scoped-синк (board_type задан) → purge только своего проекта (board);
            # deploy-wide → project=None, purge по объединению всех досок (как раньше).
            project = board if board_type is not None else None
            pr = self._tasks.purge_orphaned_tasks(
                all_active, keep_with_prs=keep_with_prs, project=project)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            agg["warnings"].extend(pr.get("warnings") or [])
        agg["purge"] = purge_summary
        return agg
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: PASS (все, включая обновлённые `purged_with`-ассерты).

- [ ] **Step 5: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/tasks/sync.py`
Expected: чисто.
```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py
git commit -m "feat(tasks): SyncService — скоуп синка по board_type + scoped purge (PRI-170)"
```

---

### Task 6: MCP-обвязка — `sync_board(board_type)` + read-тулы `project`

**Files:**
- Modify: `reviewer/mcp/service.py` (`search_tasks:256-258`, `get_task_context:260-262`, `get_task:264-270`, `sync_board:290-309`)
- Modify: `reviewer/entrypoints/mcp_server.py` (`search_tasks:114-117`, `get_task_context:119-123`, `get_task:125-132`, `sync_board:102-112`)
- Test: `tests/mcp/test_sync_board.py`, новый `tests/mcp/test_task_scope.py`

**Interfaces:**
- Consumes: `TaskService` read/`SyncService.run` сигнатуры (Task 4/5).
- Produces: `MCPReviewService.sync_board(board=None, limit=None, purge_orphaned=False, keep_with_prs=True, board_type=None)`; `MCPReviewService.search_tasks(query, top_k=5, project=None)`; `get_task_context(key, project=None)`; `get_task(key, project=None)`. MCP-тулы с теми же параметрами.

- [ ] **Step 1: Падающий тест board_type в sync_board**

В `tests/mcp/test_sync_board.py` обновить `FakeSync.run` и `test_sync_board_delegates_to_sync_service`, добавить новый:
```python
    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None):
        self.called_with = (board, limit, purge_orphaned, keep_with_prs, board_type)
        return {"enumerated": 3, "changed": 1, "warnings": []}
```
В существующем `test_sync_board_delegates_to_sync_service` обновить:
```python
    assert fake.called_with == ("B", 5, False, True, None)
```
Добавить:
```python
def test_sync_board_threads_board_type():
    class FakeSync:
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None):
            self.called_with = (board, board_type)
            return {"enumerated": 1, "warnings": []}
    fake = FakeSync()
    _Svc(fake).sync_board(board="PRI", board_type="yougile")
    assert fake.called_with == ("PRI", "yougile")
```

- [ ] **Step 2: Падающий тест проброса project в read-тулы**

Создать `tests/mcp/test_task_scope.py`:
```python
from reviewer.mcp.service import MCPReviewService


class _FakeTaskService:
    def __init__(self):
        self.calls = {}

    def search_tasks(self, query, top_k=5, project=None):
        self.calls["search"] = (query, top_k, project)
        return "ok"

    def get_task_context(self, key, project=None):
        self.calls["context"] = (key, project)
        return "ok"

    def get_task(self, key, project=None):
        self.calls["get"] = (key, project)
        return {"key": key}


class _Svc(MCPReviewService):
    def __init__(self, task_service):
        self.components = type("C", (), {"task_service": task_service})()


def test_read_tools_thread_project():
    ts = _FakeTaskService()
    svc = _Svc(ts)
    svc.search_tasks("q", project="PRI")
    svc.get_task_context("ID-1", project="PRI")
    svc.get_task("ID-1", project="PRI")
    assert ts.calls["search"] == ("q", 5, "PRI")
    assert ts.calls["context"] == ("ID-1", "PRI")
    assert ts.calls["get"] == ("ID-1", "PRI")
```

- [ ] **Step 3: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_sync_board.py tests/mcp/test_task_scope.py -q`
Expected: FAIL (TypeError на `project`/`board_type`).

- [ ] **Step 4: Реализовать в `reviewer/mcp/service.py`**

```python
    def search_tasks(self, query: str, top_k: int = 5,
                     project: str | None = None) -> str:
        """Похожие по смыслу задачи (гибрид-поиск). При project — скоуп по проекту."""
        return self.components.task_service.search_tasks(query, top_k, project=project)

    def get_task_context(self, key: str, project: str | None = None) -> str:
        """Граф-контекст задачи. При project — соседи только этого проекта."""
        return self.components.task_service.get_task_context(key, project=project)

    def get_task(self, key: str, project: str | None = None) -> dict | None:
        """Нормализованный TaskBrief из стора. При project — только из этого проекта."""
        return self.components.task_service.get_task(key, project=project)
```
В `sync_board` — добавить параметр и прокинуть:
```python
    def sync_board(self, board=None, limit=None, purge_orphaned=False,
                   keep_with_prs=True, board_type=None) -> dict:
        """Server-side ETL: перечислить доску по REST, нормализовать, проиндексировать.

        board_type ограничивает синк одним типом доски (yougile|youtrack); board —
        проектом (префикс кода). Доска/ключ не настроены → error-summary (fail-soft).
        """
        sync = getattr(self.components, "sync_service", None)
        if sync is None:
            return {"status": "error",
                    "reason": "task board REST not configured — set TASK_BOARD_TYPE + "
                              "TASK_BOARD_API_KEY in the reviewer-mcp env "
                              "(~/.config/rag-reviewer/.env), then reconnect. Yougile key: "
                              "configurator (Ctrl+~ → API) or POST /api-v2/auth/keys"}
        try:
            return sync.run(board=board, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs, board_type=board_type)
        except Exception as e:
            log.warning("sync_board: сбой синка", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 5: Реализовать в `reviewer/entrypoints/mcp_server.py`**

```python
    @mcp.tool()
    def search_tasks(query: str, top_k: int = 5, project: str | None = None) -> str:
        """Find semantically similar tasks in the indexed task corpus.
        project scopes results to one board project (code prefix, e.g. PRI); empty = all."""
        return service.search_tasks(query, top_k, project=project)

    @mcp.tool()
    def get_task_context(key: str, project: str | None = None) -> str:
        """Graph context for a task (by key or alias): the task and its PRs,
        linked tasks and their PRs, and the code those PRs touched.
        project scopes linked tasks to one board project (code prefix); empty = all."""
        return service.get_task_context(key, project=project)

    @mcp.tool()
    def get_task(key: str, project: str | None = None) -> dict | None:
        """Read one task's own normalized content from the reviewer store (filled by
        sync_board): {key, aliases, title, description, status, url, criteria}.
        project scopes the lookup to one board project (code prefix); empty = all.
        Returns null if the task is not in the store (caller falls back to the board)."""
        return service.get_task(key, project=project)
```
И `sync_board`:
```python
    @mcp.tool()
    def sync_board(board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True,
                   board_type: str | None = None) -> dict:
        """Server-side ETL: enumerate the configured task board via REST, normalize,
        and index it (vector store + task graph). board_type limits the sync to one
        board type (yougile|youtrack); board limits to one project by code prefix
        (e.g. PRI). Incremental via a per-(type,board) timestamp watermark; --limit
        disables purge and cursor advance. Returns a compact counts summary."""
        return service.sync_board(board, limit, purge_orphaned, keep_with_prs, board_type)
```

- [ ] **Step 6: Прогнать — зелёный**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS (новые + существующие server-tools тесты).

- [ ] **Step 7: Линт + коммит**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py`
Expected: чисто.
```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
        tests/mcp/test_sync_board.py tests/mcp/test_task_scope.py
git commit -m "feat(mcp): sync_board(board_type) + project в read-тулах задач (PRI-170)"
```

---

### Task 7: Integration — изоляция двух проектов end-to-end

**Files:**
- Modify: `tests/tasks/test_integration.py` (маркер `integration`)

**Interfaces:**
- Consumes: `TaskService.index_batch`, `search_tasks`, `get_task`, `get_task_context` (project-aware из Task 2-4).

- [ ] **Step 1: Написать падающий holistic-тест изоляции**

В `tests/tasks/test_integration.py` добавить (использует и стор, и граф; собрать `TaskService` на живых стор+граф + фейковый эмбеддер):
```python
def test_two_projects_isolated_end_to_end(store, tgraph):
    emb = _FakeEmbedder()
    svc = TaskService(store, tgraph, emb)
    svc.index_batch([
        {"key": "PRI-1", "aliases": [], "title": "logout", "description": "session",
         "criteria": [], "status": "Open", "url": None, "links": [], "project": "PRI"},
        {"key": "TES-9", "aliases": [], "title": "logout", "description": "session",
         "criteria": [], "status": "Open", "url": None, "links": [], "project": "TES"},
    ])
    # search скоупнут
    out = svc.search_tasks("logout", top_k=10, project="PRI")
    assert "PRI-1" in out and "TES-9" not in out
    # get_task скоупнут
    assert svc.get_task("TES-9", project="PRI") is None
    assert svc.get_task("TES-9", project="TES")["key"] == "TES-9"
    # связь в чужой проект не вылезает на чтении
    tgraph.upsert_links("PRI-1", [{"type": "related", "key": "TES-9"}])
    ctx = svc.get_task_context("PRI-1", project="PRI")
    assert "TES-9" not in ctx
```
(Фикстуры `store` и `tgraph` — из Task 2 и Task 3. `store`-фикстура truncate-ит `tasks`; `tgraph` чистит `:Task`.)

- [ ] **Step 2: Прогнать integration**

Run: `.venv/bin/pytest -m integration tests/tasks/test_integration.py::test_two_projects_isolated_end_to_end -q`
Expected: PASS (вся машинерия project уже на месте из Task 2-4).

- [ ] **Step 3: Полный integration-прогон подсистемы задач (регрессия)**

Run: `.venv/bin/pytest -m integration tests/tasks/ -q`
Expected: PASS (включая `test_sync_integration.py` — проверяет, что yougile iter_raw-фильтр и батч-синк не сломаны).

- [ ] **Step 4: Коммит**
```bash
git add tests/tasks/test_integration.py
git commit -m "test(tasks): integration изоляция двух проектов end-to-end (PRI-170)"
```

---

### Task 8: Скилы, конфиг репо, документация

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md:104-114` (шаг 5b)
- Modify: `plugin/skills/sync-tasks/SKILL.md:20-42` (`--board`/`--project` + вызов)
- Modify: `plugin/skills/solve-task/SKILL.md` (проброс project в read-тулы + scoped preflight sync)
- Modify: `plugin/skills/review-pr/SKILL.md` (проброс project в task-чтение)
- Modify: `.review.yml` (добавить `task_board.project: PRI`)
- Modify: `CLAUDE.md` (уточнить инвариант)
- Test: `tests/skills/test_configure_review_skill.py`, `tests/skills/test_sync_tasks_guardrail.py`, `tests/skills/test_solve_task_brief.py`, `tests/policy/test_policy.py`

**Note:** `reviewer/policy/policy.py` НЕ меняется — `from_yaml`/`load` кладут блок `task_board` verbatim (стр. 42, 88), поэтому ключ `project` доезжает в `policy.task_board` без кода. Добавляем только guard-тест, фиксирующий это (критерий 1).

**Interfaces:**
- Consumes: MCP-тулы с `project`/`board_type` (Task 6).
- Produces: скилы передают `project` (и `board_type` для синка) из `.review.yml task_board`.

- [ ] **Step 1: Падающие guard-тесты скилов**

В `tests/skills/test_configure_review_skill.py` расширить `test_skill_manages_task_board_block`:
```python
def test_skill_asks_for_project_scope():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board.project" in text          # пишет выбор проекта
    assert "project" in text
    # предупреждение про пустой project (тянет все проекты)
    assert "все проект" in text.lower() or "all project" in text.lower()
```
В `tests/skills/test_sync_tasks_guardrail.py` добавить:
```python
def test_skill_passes_project_and_board_type():
    text = SKILL.read_text(encoding="utf-8")
    assert "board_type" in text                  # синк скоупится по типу доски
    assert ".review.yml" in text                 # источник — конфиг репо
```
В `tests/skills/test_solve_task_brief.py` добавить:
```python
def test_solve_task_passes_project_scope():
    text = SOLVE.read_text(encoding="utf-8")
    assert "project=" in text or "task_board.project" in text
```
В `tests/policy/test_policy.py` добавить (критерий 1 — project доезжает в политику verbatim):
```python
def test_task_board_project_parsed_from_yaml():
    p = ReviewPolicy.from_yaml(
        "task_board: {type: yougile, mcp: yougile, project: PRI}")
    assert p.task_board["project"] == "PRI"
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py tests/skills/test_sync_tasks_guardrail.py tests/skills/test_solve_task_brief.py tests/policy/test_policy.py -q -k "project or task_board"`
Expected: FAIL (новые ассерты; policy-тест упадёт на отсутствии ключа `project`, пока `.review.yml`-пример не задаёт его — но это чистый парс-тест, он зелёный сразу после добавления, т.к. policy уже пробрасывает dict verbatim; если зелёный сразу — это ок, фиксируем поведение).

- [ ] **Step 3: configure-review шаг 5b — вопрос про проект**

В `plugin/skills/configure-review/SKILL.md` шаг 5b (строки 104-114) добавить после строки про `key_pattern`/`url_template` пункт про проект. Заменить блок 5b на:
```markdown
5b. **Task board selection (ask before writing).** Read the existing `task_board` block (keep it
   verbatim if present). Ask the user which board this repo uses:
   - `yougile` → write `{type: yougile, mcp: yougile, key_pattern: '[A-Z]+-\d+', url_template: <ask>}`.
   - `youtrack` → write `{type: youtrack, key_pattern: '[A-Z]+-\d+'}` (NO `url_template` — youtrack derives
     the link from its base URL; NO `mcp` — youtrack is read server-side via sync, not board-MCP).
   - off / none → write an empty `task_board:` (disables the board for this repo).
   - leave unchanged → skip.

   **Then ask which PROJECT this repo uses** (PRI-170) and write it to `task_board.project` — the task
   **code prefix** (e.g. `PRI`, `TES`), the part before the dash in task codes. Warn the user (in Russian):
   если `project` пуст — и синк, и выдача/граф затянут **все проекты** аккаунта/инстанса вперемешку
   (напр. чужой `TES-1` всплывёт в связях задачи `PRI`); один аккаунт с несколькими проектами без
   `project` смешивает их. Пустой `task_board.project` = текущее глобальное поведение.

   **Never write credentials.** Remind the user (in Russian): ключи доски (`YOUTRACK_TOKEN`/
   `YOUTRACK_BASE_URL` для youtrack, `YOUGILE_API_KEY` для yougile) задаются в env деплоя reviewer-mcp,
   не в `.review.yml`. Грабли youtrack: `YOUTRACK_BASE_URL` обязан оканчиваться на `/api`. Changing the
   board has no effect until those env keys are set and the board is synced (`/reviewer_sync-tasks`).
```

- [ ] **Step 4: sync-tasks — синк скоупится из `.review.yml`**

В `plugin/skills/sync-tasks/SKILL.md` заменить пункт `--board` (строка 22) и блок вызова (33-42). Описание входа:
```markdown
- `--board <project>`: limit to one project by task code prefix (e.g. `PRI`). If omitted, read
  `task_board.project` from the repo `.review.yml` (deploy default via `get_board_config()` otherwise).
- `--board-type <yougile|youtrack>`: limit the sync to one board type. If omitted, read
  `task_board.type` from the repo `.review.yml`. Empty both → deploy-wide sync of every configured board.
```
Блок вызова:
```markdown
1. **Resolve scope, then call the tool once.** Read `task_board` from the repo `.review.yml`
   (`type` → `board_type`, `project` → `board`); fall back to the deploy default via
   `get_board_config()` if there is no block. Map to a single call:

   ```
   sync_board(
       board=<--board or task_board.project or null>,
       board_type=<--board-type or task_board.type or null>,
       limit=<--limit or null>,
       purge_orphaned=<True if --purge-orphaned else False>,
       keep_with_prs=<False if --no-keep-with-prs else True>,
   )
   ```
   Scoping by `board_type` + `board` keeps this repo's sync to its own board/project (PRI-170);
   an empty project syncs everything (and mixes projects on read).
```

- [ ] **Step 5: solve-task — проброс project в чтение + scoped preflight**

В `plugin/skills/solve-task/SKILL.md`:
- В шаге 0.3 (preflight `sync_board`) заменить `sync_board(board=null, limit=null, purge_orphaned=false)` на:
  `sync_board(board=<task_board.project or null>, board_type=<task_board.type or null>, limit=null, purge_orphaned=false)` — «scoped warm-up корпуса задач своего проекта (PRI-170); пустой project → весь корпус».
- В шаге 3 (Gather context) добавить, что во все task-чтения передаётся `project` из `task_board.project` (`.review.yml`), резолвнутого в шаге 1: `get_task(key, project=<task_board.project>)`, `get_task_context(key, project=...)`, `search_tasks("...", project=...)`. Добавить строку:
  «Pass `project=<task_board.project>` (from Step 1; empty = unscoped) to `get_task`, `get_task_context`, and `search_tasks` so only this repo's project surfaces (PRI-170).»

- [ ] **Step 6: review-pr — проброс project в task-чтение**

В `plugin/skills/review-pr/SKILL.md` task-тулы зовутся в строках 51 (`get_task(key)`), 66
(`get_task_context(TaskBrief.key)`), 67 (`search_tasks(...)`). Обновить эти три вызова, добавив
`project=<task_board.project>` (из блока `task_board` целевой ветки, упомянутого в строке 33):
- строка 51: `get_task(key, project=<task_board.project>)`
- строка 66: `get_task_context(TaskBrief.key, project=<task_board.project>)`
- строка 67: `search_tasks("<TaskBrief.title>. <first lines of description>", project=<task_board.project>)`
И добавить рядом со строкой 45 (начало «Task context») пояснение:
«Task reads are scoped to this repo's project: pass `project=<task_board.project>` (from the target
branch `.review.yml`, see step with `task_board`) to `get_task`/`get_task_context`/`search_tasks`
(PRI-170; empty `project` = unscoped).»

- [ ] **Step 7: `.review.yml` этого репо — project: PRI**

В `.review.yml` в блок `task_board` (после `url_template`) добавить:
```yaml
  project: PRI                        # PRI-170: скоуп синка/выдачи задач этим проектом (префикс кода)
```

- [ ] **Step 8: CLAUDE.md — уточнить инвариант**

В `CLAUDE.md` в абзаце про `task_board` / болк-синк (раздел «Неочевидные факты») добавить предложение:
«**Скоуп задач по проекту (PRI-170):** хранилище задач (`tasks`, `:Task`) остаётся глобальным, но синк и выдача (`search_tasks`/`get_task`/`get_task_context`/обход графа) скоупятся по `task_board.project` из `.review.yml` репо (префикс кода, напр. `PRI`); пусто = всё. `sync_board(board_type, board)` ограничивает синк одним типом доски и проектом. Клиент-скилы передают `project` из `.review.yml`; сервер repo-агностичен.»

- [ ] **Step 9: Прогнать guard-тесты — зелёный**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (новые + существующие).

- [ ] **Step 10: Финальный полный прогон + линт + коммит**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: unit PASS; ruff — без НОВЫХ замечаний в тронутых файлах (репозиторий может быть не «чист» исторически — не гнаться за repo-wide clean, см. [[refactor-verification-gotchas]]).
```bash
git add plugin/skills/configure-review/SKILL.md plugin/skills/sync-tasks/SKILL.md \
        plugin/skills/solve-task/SKILL.md plugin/skills/review-pr/SKILL.md \
        .review.yml CLAUDE.md tests/skills/ tests/policy/test_policy.py
git commit -m "feat(skills): скоуп задач по project — configure-review/sync/solve/review-pr + .review.yml (PRI-170)"
```

---

## Финальная верификация (после всех задач)

- [ ] `.venv/bin/pytest -q` — все unit зелёные.
- [ ] `docker compose up -d && .venv/bin/pytest -m integration tests/tasks/ -q` — integration зелёные.
- [ ] `.venv/bin/ruff check reviewer/ tests/tasks/ tests/mcp/ tests/skills/` — без новых замечаний.
- [ ] Боевая проверка скоупа: `uvx --from rag-reviewer reviewer ...` — или через MCP: `sync_board(board="PRI", board_type="yougile")`, затем `get_task_context("PRI-170", project="PRI")` — `TES-1` больше не вылезает.
- [ ] Критерии приёмки PRI-170 (1-5) перечитать против реализованного.

## Soundness-заметки

- **Бэкфилл существующих ~62 задач**: первый scoped-синк использует новый курсор-ref `tasks:{type}:{project}` (≠ `tasks:{type}:*`), поэтому переобрабатывает все задачи; `content_hash` совпадает → `meta_only`-ветка `index_batch` → `update_meta(..., project)` + `graph.upsert_task(..., project)` тегируют без переэмбеда (без трат Voyage). Курсор не сбрасываем вручную.
- **Кросс-проект связи**: только read-фильтр (`task_context` по `n.project`). Write-side не трогаем — `normalize` по-прежнему извлекает related-ключи чужого префикса (закреплено зелёными тестами), но на чтении они невидимы.
- **Back-compat**: `project=None`/`board_type=None` всюду = текущее поведение; `tasks.project` дефолт `''`; deploy-wide синк продолжает работать (и теперь штампует project по-задачно).
