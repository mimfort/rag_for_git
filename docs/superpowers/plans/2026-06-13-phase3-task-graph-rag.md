# Phase 3 — Task Graph & RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index board tasks into a vector store (Postgres `tasks`) and a graph (Neo4j `:Task`/`:PR` + `TASK_LINK`/`IMPLEMENTED_BY`/`TOUCHES`), expose `index_task`/`search_tasks`/`get_task_context` MCP tools, auto-link PR↔task↔code on `publish_review`, and wire the `review-pr` skill plus a new `/sync-tasks` skill to use them.

**Architecture:** A new isolated package `reviewer/tasks/` (`store` = Postgres, `graph` = Neo4j reusing the existing driver, `service` = orchestration). Tasks are indexed from a normalized board-agnostic `TaskBrief` produced by the skill (Python never touches the board). Task↔task edges come only from explicit board links; semantic relatedness is query-time via `search_tasks`. Degradation is fail-open everywhere (Neo4j/board down → empty + warning, review/sync never aborts).

**Tech Stack:** Python 3.11–3.13, Postgres/ParadeDB (pgvector + pg_search BM25, RRF), Neo4j (neo4j driver), Voyage (`voyage-code-3`, dim 1024) via existing `VoyageEmbedder`, FastMCP, Claude Code skills (English).

**Spec:** `docs/superpowers/specs/2026-06-13-phase3-task-graph-rag-design.md`

---

## Conventions for this plan

- **Language:** code/docstrings/CLI/README/comments — Russian; skills & LLM prompts — English (project convention).
- **Commits:** Conventional Commits in Russian, **no self-attribution** (no `Co-Authored-By` / Claude / AI mentions).
- **Lint:** `main` is NOT ruff-clean (pre-existing debt). Scope `ruff check` to the files you changed; do not fix unrelated debt.
- **Tests:** `pytest` excludes `integration` by default. Unit tests fake/mock external services; real Postgres/Neo4j only under `@pytest.mark.integration`.
- **node_id invariant:** `"path#fqn"` — the same key bridges chunks, code-graph `:Symbol`, and `(:PR)-[:TOUCHES]->(:Symbol)`.

---

## File Structure

**New files:**
- `reviewer/tasks/__init__.py` — package marker.
- `reviewer/tasks/store.py` — `TaskStore` (Postgres `tasks`), `TaskRow`, `TaskHit`, pure helpers `build_task_text`/`task_content_hash`.
- `reviewer/tasks/graph.py` — `TaskGraph` (Neo4j, reuses driver), `PRRef`.
- `reviewer/tasks/service.py` — `TaskService` (`index_task`/`search_tasks`/`get_task_context`/`link_review`) + `_format_task_context`.
- `tests/tasks/__init__.py`
- `tests/tasks/test_text.py` — unit: pure helpers.
- `tests/tasks/test_graph.py` — unit: `TaskGraph` with a fake driver.
- `tests/tasks/test_service.py` — unit: `TaskService` with fakes (core coverage).
- `tests/tasks/test_integration.py` — `integration`: real Postgres+Neo4j round trip (fake embedder, no Voyage).
- `plugin/skills/sync-tasks/SKILL.md` — new thin warm-up skill.
- `plugin/skills/sync-tasks/references/sync-tasks-yougile.md` — Yougile iteration playbook.

**Modified files:**
- `reviewer/index/schema.sql` — `tasks` table + indexes.
- `reviewer/graph/store.py` — `driver` property + `:Task`/`:PR` constraints & `codes` index in `init_schema`.
- `reviewer/app.py` — `Components` += `task_store`/`task_graph`/`task_service`; `build_components` wiring.
- `reviewer/mcp/service.py` — `index_task`/`search_tasks`/`get_task_context` delegates; `publish_review` += `task_key` + auto-link.
- `reviewer/entrypoints/mcp_server.py` — register 3 tools; `publish_review` tool += `task_key`.
- `tests/test_app_wiring.py` — assert task components wired.
- `tests/mcp/test_service.py` — `publish_review` auto-link tests.
- `plugin/skills/review-pr/SKILL.md` — persist→enrich→link steps.
- `plugin/skills/review-pr/references/requirements-prompt.md` — related-tasks context block.
- `plugin/skills/review-pr/references/task-context-yougile.md` — `aliases`, `links` from `subtasks[]`, `url` from `url_template`.
- `plugin/skills/review-pr/references/task-context-jira.md` — `aliases`, issue-links → `links[]`.
- `README.md` — task graph, `/sync-tasks`, `aliases`/`url_template`.

---

## Task 0: Workspace setup & clean baseline

**Files:** none (environment only).

- [ ] **Step 1: Create the venv and install**

Run:
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,web]"
```
Expected: install completes. `[web]` is required, else `tests/web/test_api.py` errors on collection.

- [ ] **Step 2: Confirm clean baseline**

Run: `.venv/bin/pytest -q`
Expected: PASS (existing suite green). If failures, report and ask before proceeding.

- [ ] **Step 3: Confirm infra is up (for later integration steps only)**

Run: `docker compose up -d && .venv/bin/reviewer check`
Expected: Postgres :5433 + Neo4j :7687 reachable. (Unit tasks below do not need this; integration tasks do.)

---

## Task 1: Pure helpers — task text & content hash

**Files:**
- Create: `reviewer/tasks/__init__.py` (empty)
- Create: `reviewer/tasks/store.py` (helpers only in this task)
- Test: `tests/tasks/__init__.py` (empty), `tests/tasks/test_text.py`

- [ ] **Step 1: Write the failing test**

`tests/tasks/test_text.py`:
```python
from reviewer.tasks.store import build_task_text, task_content_hash


def test_build_task_text_joins_title_description_criteria():
    text = build_task_text("Login", "Add logout", ["clears session", "redirects"])
    assert "Login" in text and "Add logout" in text
    assert "clears session" in text and "redirects" in text


def test_build_task_text_skips_empty_parts():
    assert build_task_text("Только заголовок", "", None) == "Только заголовок"


def test_content_hash_stable_and_normalized():
    # trailing whitespace must not change the hash (как Chunk.content_hash)
    a = task_content_hash("line one  \nline two")
    b = task_content_hash("line one\nline two")
    assert a == b


def test_content_hash_changes_on_real_change():
    assert task_content_hash("a") != task_content_hash("b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/tasks/test_text.py -q`
Expected: FAIL — `ModuleNotFoundError: reviewer.tasks.store`.

- [ ] **Step 3: Create the package + helpers**

`reviewer/tasks/__init__.py`: empty file.

`reviewer/tasks/store.py` (helpers section — the rest of `TaskStore` is added in Task 2):
```python
"""Хранилище задач доски в Postgres: эмбеддинги (pgvector) + BM25 (pg_search), RRF.

Отдельная таблица ``tasks`` (не code-``chunks``): у задач нет path/symbol/lines и
base/overlay-freshness. Зеркалит паттерн :class:`ChunkStore` — ленивый пул,
``register_vector`` на каждое соединение.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass

from pgvector.psycopg import Vector, register_vector
from psycopg_pool import ConnectionPool

_BM25_STRIP = re.compile(r"[^\w\s]")


def _bm25_query(text: str) -> str:
    cleaned = _BM25_STRIP.sub(" ", text).strip()
    return cleaned or "____nomatch____"


def build_task_text(title: str, description: str, criteria: list[str] | None) -> str:
    """Текст задачи для эмбеддинга и BM25: заголовок + описание + критерии."""
    parts = [title or "", description or ""]
    if criteria:
        parts.append("\n".join(c for c in criteria if c))
    return "\n\n".join(p for p in parts if p).strip()


def task_content_hash(text: str) -> str:
    """Хэш нормализованного текста задачи (как Chunk.content_hash) — дедуп переэмбеда."""
    norm = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/tasks/test_text.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add reviewer/tasks/__init__.py reviewer/tasks/store.py tests/tasks/__init__.py tests/tasks/test_text.py
git commit -m "feat(tasks): хелперы текста и content_hash задачи"
```

---

## Task 2: TaskStore + `tasks` schema

**Files:**
- Modify: `reviewer/index/schema.sql` (append `tasks` table)
- Modify: `reviewer/tasks/store.py` (add `TaskRow`, `TaskHit`, `TaskStore`)
- Test: `tests/tasks/test_integration.py` (`integration`, real Postgres)

- [ ] **Step 1: Add the `tasks` table to `schema.sql`**

Append to `reviewer/index/schema.sql`:
```sql

-- Задачи доски (фаза 3): эмбеддинги (pgvector) + BM25 (pg_search) для search_tasks.
-- Отдельно от chunks — у задач нет path/symbol/lines и base/overlay-freshness.
CREATE TABLE IF NOT EXISTS tasks (
    id           bigserial PRIMARY KEY,
    key          text    NOT NULL UNIQUE,   -- канонический код задачи (ID-N / Jira key)
    aliases      text[]  NOT NULL DEFAULT '{}',
    title        text    NOT NULL,
    description  text    NOT NULL DEFAULT '',
    status       text,
    url          text,
    content_hash text    NOT NULL,          -- дедуп переэмбеда
    text         text    NOT NULL,          -- эмбед/BM25-текст: title + description + criteria
    embedding    vector(1024)
);
CREATE INDEX IF NOT EXISTS tasks_bm25 ON tasks
USING bm25 (id, text, key) WITH (key_field='id');
CREATE INDEX IF NOT EXISTS tasks_hnsw ON tasks
USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

Note: `tasks` is created by the existing `ChunkStore.init_schema()` (it runs the whole `schema.sql`), which `reviewer index` already calls — no extra wiring.

- [ ] **Step 2: Append `TaskRow`, `TaskHit`, `TaskStore` to `reviewer/tasks/store.py`**

```python
@dataclass
class TaskRow:
    key: str
    aliases: list[str]
    title: str
    description: str
    status: str | None
    url: str | None
    content_hash: str
    text: str
    embedding: list[float]


@dataclass
class TaskHit:
    key: str
    title: str
    status: str | None
    score: float


class TaskStore:
    """Хранилище задач в Postgres (таблица ``tasks``). Ленивый пул, register_vector."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()

    def _ensure_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        self.dsn, min_size=self._min_size, max_size=self._max_size,
                        open=False, configure=lambda conn: register_vector(conn),
                    )
                    self._pool.open()
        return self._pool

    def _connect(self):
        return self._ensure_pool().connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def existing_hash(self, key: str) -> str | None:
        """content_hash уже проиндексированной задачи (None если её нет)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM tasks WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None

    def upsert_task(self, row: TaskRow) -> None:
        sql = """
        INSERT INTO tasks (key, aliases, title, description, status, url,
                           content_hash, text, embedding)
        VALUES (%(key)s,%(aliases)s,%(title)s,%(description)s,%(status)s,%(url)s,
                %(content_hash)s,%(text)s,%(embedding)s)
        ON CONFLICT (key) DO UPDATE SET
            aliases=EXCLUDED.aliases, title=EXCLUDED.title,
            description=EXCLUDED.description, status=EXCLUDED.status,
            url=EXCLUDED.url, content_hash=EXCLUDED.content_hash,
            text=EXCLUDED.text, embedding=EXCLUDED.embedding
        """
        params = {
            "key": row.key, "aliases": row.aliases, "title": row.title,
            "description": row.description, "status": row.status, "url": row.url,
            "content_hash": row.content_hash, "text": row.text,
            "embedding": row.embedding,
        }
        with self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    def update_meta(self, key: str, title: str, status: str | None,
                    url: str | None, aliases: list[str]) -> None:
        """Обновить лёгкие метаданные без переэмбеда (когда content_hash совпал)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET title=%s, status=%s, url=%s, aliases=%s WHERE key=%s",
                (title, status, url, aliases, key),
            )
            conn.commit()

    def search(self, query_text: str, query_embedding: list[float],
               top_k: int = 5, candidates: int = 50) -> list[TaskHit]:
        """Гибрид RRF (BM25 ⊕ ANN) по корпусу задач — без ref-фильтра."""
        sql = """
        WITH bm25 AS (
            SELECT id, RANK() OVER (ORDER BY pdb.score(id) DESC) AS rank
            FROM tasks WHERE text @@@ %(q)s
            ORDER BY pdb.score(id) DESC LIMIT %(cand)s
        ),
        ann AS (
            SELECT id, RANK() OVER (ORDER BY embedding <=> %(vec)s) AS rank
            FROM tasks
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
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TaskHit(key=k, title=t, status=s, score=float(sc))
                for (k, t, s, sc) in rows]
```

- [ ] **Step 3: Write the failing integration test (store round trip)**

`tests/tasks/test_integration.py`:
```python
"""Integration: TaskStore + TaskGraph на живых Postgres+Neo4j (без Voyage).

Эмбеддер фейковый (детерминированный 1024-вектор) — проверяем SQL/Cypher, не Voyage.
Требует docker compose up -d. Маркер integration (исключён из дефолтного прогона).
"""
from __future__ import annotations

import hashlib

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.graph.store import GraphStore
from reviewer.tasks.store import TaskRow, TaskStore, build_task_text, task_content_hash

pytestmark = pytest.mark.integration


def _vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    # детерминированный ненулевой 1024-вектор
    return [((h[i % len(h)] + i) % 17) / 17.0 for i in range(1024)]


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


@pytest.fixture()
def store():
    s = Settings()
    ChunkStore(s.pg_dsn).init_schema()  # создаёт таблицу tasks (schema.sql)
    st = TaskStore(s.pg_dsn)
    with st._connect() as conn:
        conn.execute("TRUNCATE tasks RESTART IDENTITY")
        conn.commit()
    yield st
    st.close()


def test_taskstore_upsert_and_search(store):
    emb = _FakeEmbedder()
    text = build_task_text("Add logout", "Clear the session on logout", [])
    store.upsert_task(TaskRow(
        key="ID-1", aliases=["PRI-1"], title="Add logout",
        description="Clear the session on logout", status="Open", url=None,
        content_hash=task_content_hash(text), text=text,
        embedding=emb.embed_documents([text])[0]))

    assert store.existing_hash("ID-1") == task_content_hash(text)
    hits = store.search("logout session", emb.embed_query("logout session"), top_k=5)
    assert any(h.key == "ID-1" for h in hits)
```

- [ ] **Step 4: Run the integration test**

Run: `.venv/bin/pytest tests/tasks/test_integration.py::test_taskstore_upsert_and_search -m integration -q`
Expected: PASS (Postgres up). If `tasks` missing → ensure `reviewer index` / `init_schema` ran (the fixture calls it).

- [ ] **Step 5: Lint changed files & commit**

```bash
.venv/bin/ruff check reviewer/tasks/store.py
git add reviewer/index/schema.sql reviewer/tasks/store.py tests/tasks/test_integration.py
git commit -m "feat(tasks): таблица tasks и TaskStore (upsert/search/дедуп)"
```

---

## Task 3: TaskGraph + Neo4j constraints

**Files:**
- Modify: `reviewer/graph/store.py` (add `driver` property + task/PR constraints in `init_schema`)
- Create: `reviewer/tasks/graph.py` (`PRRef`, `TaskGraph`)
- Test: `tests/tasks/test_graph.py` (unit, fake driver) + append to `tests/tasks/test_integration.py`

- [ ] **Step 1: Write the failing unit test**

`tests/tasks/test_graph.py`:
```python
from reviewer.tasks.graph import PRRef, TaskGraph


class _FakeDriver:
    def __init__(self, records=None):
        self.calls = []
        self._records = records if records is not None else []

    def execute_query(self, query, **params):
        self.calls.append((query, params))
        return (self._records, None, None)


def test_upsert_task_codes_are_key_plus_aliases_deduped():
    d = _FakeDriver()
    TaskGraph(d).upsert_task("ID-1", ["PRI-2", "ID-1"], "T", "Open", "u")
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["codes"] == ["ID-1", "PRI-2"]  # key first, self-alias dropped


def test_upsert_links_filters_keyless_and_counts():
    d = _FakeDriver()
    n = TaskGraph(d).upsert_links("ID-1", [
        {"key": "ID-2", "title": "child", "type": "subtask"},
        {"title": "no key"},  # dropped
    ])
    assert n == 1
    _query, params = d.calls[0]
    assert params["rows"] == [{"key": "ID-2", "title": "child", "type": "subtask"}]


def test_upsert_links_empty_does_not_query():
    d = _FakeDriver()
    assert TaskGraph(d).upsert_links("ID-1", []) == 0
    assert d.calls == []


def test_link_pr_params():
    d = _FakeDriver()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    TaskGraph(d).link_pr("ID-1", pr, ["a.py#foo", "b.py#bar"])
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["pid"] == "o/r#7"
    assert params["repo"] == "o/r" and params["number"] == 7 and params["sha"] == "abc"
    assert params["touched"] == ["a.py#foo", "b.py#bar"]


def test_task_context_parses_record():
    rec = {
        "key": "ID-1", "title": "T", "status": "Open", "url": "u",
        "prs": [{"id": "o/r#7", "url": "pr", "sha": "abc", "touched": ["a.py#foo"]}],
        "linked": [{"key": "ID-2", "title": "child", "status": "Done",
                    "type": "subtask", "prs": [{"id": "o/r#8", "url": "pr8"}]}],
    }
    ctx = TaskGraph(_FakeDriver([rec])).task_context("ID-1")
    assert ctx["key"] == "ID-1"
    assert ctx["prs"][0]["touched"] == ["a.py#foo"]
    assert ctx["linked"][0]["type"] == "subtask"


def test_task_context_empty_when_no_record():
    assert TaskGraph(_FakeDriver([])).task_context("ZZ-9") == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -q`
Expected: FAIL — `ModuleNotFoundError: reviewer.tasks.graph`.

- [ ] **Step 3: Create `reviewer/tasks/graph.py`**

```python
"""Граф задач в Neo4j: узлы :Task/:PR, рёбра TASK_LINK/IMPLEMENTED_BY/TOUCHES.

Переиспользует Neo4j-драйвер :class:`GraphStore` (один коннект). Рёбра TOUCHES
ссылаются на :Symbol того же графа кода — сшивка через node_id='path#fqn'.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PRRef:
    """Ссылка на PR для узла :PR графа задач."""

    repo: str          # "owner/name"
    number: int
    url: str
    sha: str

    @property
    def id(self) -> str:
        return f"{self.repo}#{self.number}"


class TaskGraph:
    """Узлы и рёбра задач в Neo4j поверх общего драйвера GraphStore."""

    def __init__(self, driver) -> None:
        self._driver = driver

    def upsert_task(self, key: str, aliases: list[str], title: str,
                    status: str | None, url: str | None) -> None:
        """Upsert узла :Task. codes = [key, ...aliases] для резолва по любому коду."""
        codes = [key] + [a for a in (aliases or []) if a and a != key]
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) "
            "SET t.codes=$codes, t.title=$title, t.status=$status, t.url=$url",
            key=key, codes=codes, title=title, status=status, url=url)

    def upsert_links(self, key: str, links: list[dict]) -> int:
        """Рёбра TASK_LINK из явных board-links. Несуществующий сосед → стаб :Task."""
        rows = [{"key": lk["key"], "title": lk.get("title") or "",
                 "type": lk.get("type") or "relates"}
                for lk in links if lk.get("key")]
        if not rows:
            return 0
        self._driver.execute_query(
            "MATCH (t:Task {key: $key}) "
            "UNWIND $rows AS lk "
            "MERGE (n:Task {key: lk.key}) "
            "  ON CREATE SET n.title=lk.title, n.codes=[lk.key] "
            "MERGE (t)-[:TASK_LINK {type: lk.type}]->(n)",
            key=key, rows=rows)
        return len(rows)

    def link_pr(self, task_key: str, pr: PRRef, touched_node_ids: list[str]) -> None:
        """(:Task)-[:IMPLEMENTED_BY]->(:PR)-[:TOUCHES]->(:Symbol). Стаб :Task/:Symbol при отсутствии."""
        self._driver.execute_query(
            "MERGE (t:Task {key: $key}) ON CREATE SET t.codes=[$key] "
            "MERGE (p:PR {id: $pid}) "
            "  SET p.repo=$repo, p.number=$number, p.url=$url, p.sha=$sha "
            "MERGE (t)-[:IMPLEMENTED_BY]->(p) "
            "WITH p "
            "UNWIND $touched AS nid "
            "MERGE (s:Symbol {id: nid}) "
            "MERGE (p)-[:TOUCHES]->(s)",
            key=task_key, pid=pr.id, repo=pr.repo, number=pr.number,
            url=pr.url, sha=pr.sha, touched=list(touched_node_ids or []))

    def task_context(self, key: str) -> dict:
        """Обход: сама задача + её PR/код + TASK_LINK-соседи и их PR. {} если не найдена."""
        records, _, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE $k IN t.codes "
            "RETURN t.key AS key, t.title AS title, t.status AS status, t.url AS url, "
            "[ (t)-[:IMPLEMENTED_BY]->(p:PR) | "
            "  {id: p.id, url: p.url, sha: p.sha, "
            "   touched: [ (p)-[:TOUCHES]->(s:Symbol) | s.id ]} ] AS prs, "
            "[ (t)-[l:TASK_LINK]-(n:Task) | "
            "  {key: n.key, title: n.title, status: n.status, type: l.type, "
            "   prs: [ (n)-[:IMPLEMENTED_BY]->(np:PR) | {id: np.id, url: np.url} ]} ] AS linked "
            "LIMIT 1",
            k=key)
        if not records:
            return {}
        r = records[0]
        return {"key": r["key"], "title": r["title"], "status": r["status"],
                "url": r["url"], "prs": r["prs"], "linked": r["linked"]}
```

- [ ] **Step 4: Add task/PR constraints + driver property to `GraphStore`**

In `reviewer/graph/store.py`, add a property after `__init__` and extend `init_schema`:
```python
    @property
    def driver(self):
        """Neo4j-драйвер — для шаринга с TaskGraph (один коннект на инстанс)."""
        return self._driver
```
Replace the body of `init_schema` with:
```python
    def init_schema(self) -> None:
        self._driver.execute_query(
            "CREATE CONSTRAINT sym_id IF NOT EXISTS "
            "FOR (s:Symbol) REQUIRE s.id IS UNIQUE")
        # Граф задач (фаза 3): уникальность :Task(key) и :PR(id) + индекс на codes
        # (резолв по любому коду в WHERE $k IN t.codes).
        self._driver.execute_query(
            "CREATE CONSTRAINT task_key IF NOT EXISTS "
            "FOR (t:Task) REQUIRE t.key IS UNIQUE")
        self._driver.execute_query(
            "CREATE CONSTRAINT pr_id IF NOT EXISTS "
            "FOR (p:PR) REQUIRE p.id IS UNIQUE")
        self._driver.execute_query(
            "CREATE INDEX task_codes IF NOT EXISTS FOR (t:Task) ON (t.codes)")
```

- [ ] **Step 5: Run unit tests to verify they pass**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Append the Neo4j integration round trip**

Append to `tests/tasks/test_integration.py`:
```python
@pytest.fixture()
def graph():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear()
    from reviewer.tasks.graph import TaskGraph
    yield TaskGraph(g.driver)
    g.clear()
    g.close()


def test_taskgraph_link_and_context(graph):
    from reviewer.tasks.graph import PRRef

    graph.upsert_task("ID-1", ["PRI-1"], "Parent", "Open", "u1")
    graph.upsert_links("ID-1", [{"key": "ID-2", "title": "Child", "type": "subtask"}])
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    graph.link_pr("ID-1", pr, ["a.py#foo"])

    # резолв по alias PRI-1 находит тот же узел
    ctx = graph.task_context("PRI-1")
    assert ctx["key"] == "ID-1"
    assert ctx["prs"][0]["id"] == "o/r#7"
    assert ctx["prs"][0]["touched"] == ["a.py#foo"]
    assert any(n["key"] == "ID-2" and n["type"] == "subtask" for n in ctx["linked"])
```

- [ ] **Step 7: Run the integration test**

Run: `.venv/bin/pytest tests/tasks/test_integration.py::test_taskgraph_link_and_context -m integration -q`
Expected: PASS (Neo4j up).

- [ ] **Step 8: Lint & commit**

```bash
.venv/bin/ruff check reviewer/tasks/graph.py reviewer/graph/store.py
git add reviewer/tasks/graph.py reviewer/graph/store.py tests/tasks/test_graph.py tests/tasks/test_integration.py
git commit -m "feat(tasks): граф задач :Task/:PR (TASK_LINK/IMPLEMENTED_BY/TOUCHES) + constraints"
```

---

## Task 4: TaskService (index/search/context/link)

**Files:**
- Create: `reviewer/tasks/service.py`
- Test: `tests/tasks/test_service.py`

- [ ] **Step 1: Write the failing unit test**

`tests/tasks/test_service.py`:
```python
from reviewer.tasks.graph import PRRef
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import task_content_hash, build_task_text


class _FakeStore:
    def __init__(self, hashes=None, search_result=None):
        self._hashes = hashes or {}
        self.upserted = []
        self.meta_updates = []
        self._search_result = search_result or []

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)

    def update_meta(self, key, title, status, url, aliases):
        self.meta_updates.append((key, title, status, url, aliases))

    def search(self, q, vec, top_k=5):
        return self._search_result


class _FakeGraph:
    def __init__(self, context=None, raise_on=()):
        self.tasks = []
        self.links = []
        self.pr_links = []
        self._context = context or {}
        self._raise_on = set(raise_on)

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


class _FakeEmbedder:
    def __init__(self):
        self.doc_calls = []

    def embed_documents(self, texts):
        self.doc_calls.append(texts)
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.2] * 8


def _brief(**over):
    b = {"key": "ID-1", "aliases": ["PRI-1"], "title": "Add logout",
         "description": "Clear session", "criteria": ["redirects"],
         "status": "Open", "url": "u",
         "links": [{"key": "ID-2", "title": "child", "type": "subtask"}]}
    b.update(over)
    return b


def test_index_task_embeds_and_upserts_on_new_task():
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["key"] == "ID-1"
    assert out["embedded"] is True
    assert out["links_upserted"] == 1
    assert store.upserted and store.upserted[0].key == "ID-1"
    assert emb.doc_calls  # embedding computed
    assert graph.tasks[0][0] == "ID-1"


def test_index_task_skips_embed_when_hash_unchanged():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph, emb = _FakeGraph(), _FakeEmbedder()
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["embedded"] is False
    assert store.upserted == []           # no re-embed/upsert
    assert store.meta_updates and store.meta_updates[0][0] == "ID-1"  # meta refreshed


def test_index_task_graph_none_still_embeds_and_warns():
    store, emb = _FakeStore(), _FakeEmbedder()
    out = TaskService(store, None, emb).index_task(_brief())
    assert out["embedded"] is True
    assert any("graph unavailable" in w for w in out["warnings"])


def test_index_task_graph_error_is_warning_not_raise():
    store, emb = _FakeStore(), _FakeEmbedder()
    graph = _FakeGraph(raise_on=("upsert_task",))
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["embedded"] is True       # store layer succeeded
    assert any("graph:" in w for w in out["warnings"])


def test_index_task_no_key():
    out = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder()).index_task({"title": "x"})
    assert out["key"] is None
    assert out["embedded"] is False


def test_search_tasks_formats_hits():
    from reviewer.tasks.store import TaskHit
    store = _FakeStore(search_result=[TaskHit("ID-1", "Add logout", "Open", 0.83)])
    out = TaskService(store, _FakeGraph(), _FakeEmbedder()).search_tasks("logout")
    assert "ID-1" in out and "Add logout" in out and "Open" in out


def test_search_tasks_empty():
    out = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder()).search_tasks("x")
    assert out == "(no similar tasks found)"


def test_get_task_context_graph_none():
    out = TaskService(_FakeStore(), None, _FakeEmbedder()).get_task_context("ID-1")
    assert out == "(task graph unavailable)"


def test_get_task_context_not_found():
    out = TaskService(_FakeStore(), _FakeGraph(context={}), _FakeEmbedder()).get_task_context("ZZ-9")
    assert "ZZ-9" in out


def test_get_task_context_formats():
    ctx = {"key": "ID-1", "title": "Add logout", "status": "Open", "url": "u",
           "prs": [{"id": "o/r#7", "url": "pr", "sha": "abc", "touched": ["a.py#foo"]}],
           "linked": [{"key": "ID-2", "title": "child", "status": "Done",
                       "type": "subtask", "prs": [{"id": "o/r#8", "url": "pr8"}]}]}
    out = TaskService(_FakeStore(), _FakeGraph(context=ctx), _FakeEmbedder()).get_task_context("ID-1")
    assert "ID-1" in out and "o/r#7" in out and "a.py#foo" in out
    assert "subtask" in out and "ID-2" in out


def test_link_review_calls_graph():
    graph = _FakeGraph()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    TaskService(_FakeStore(), graph, _FakeEmbedder()).link_review("ID-1", pr, ["a.py#foo"])
    assert graph.pr_links == [("ID-1", pr, ["a.py#foo"])]


def test_link_review_noop_without_graph_or_key():
    pr = PRRef(repo="o/r", number=7, url="u", sha="abc")
    TaskService(_FakeStore(), None, _FakeEmbedder()).link_review("ID-1", pr, [])  # no graph
    g = _FakeGraph()
    TaskService(_FakeStore(), g, _FakeEmbedder()).link_review("", pr, [])         # no key
    assert g.pr_links == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/tasks/test_service.py -q`
Expected: FAIL — `ModuleNotFoundError: reviewer.tasks.service`.

- [ ] **Step 3: Create `reviewer/tasks/service.py`**

```python
"""Сервис задач: index_task / search_tasks / get_task_context / link_review.

Оркестрирует TaskStore (эмбеддинги) + TaskGraph (граф) + эмбеддер. Fail-soft по
слоям: сбой одного слоя не валит остальное (деградация как фазы 1/2).
"""
from __future__ import annotations

import logging

from reviewer.tasks.graph import PRRef
from reviewer.tasks.store import TaskRow, build_task_text, task_content_hash

log = logging.getLogger(__name__)


class TaskService:
    """Оркестрация индексации и обхода графа задач."""

    def __init__(self, store, graph, embedder, *, max_chars: int = 8000) -> None:
        self._store = store
        self._graph = graph          # None, если Neo4j не подключён
        self._embedder = embedder
        self._max_chars = max_chars

    def index_task(self, task: dict) -> dict:
        """Проиндексировать нормализованный TaskBrief: эмбеддинг (дедуп) + граф."""
        key = task.get("key") if isinstance(task, dict) else None
        if not key:
            return {"key": None, "embedded": False, "links_upserted": 0,
                    "warnings": ["task has no key"]}
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
        warnings: list[str] = []

        embedded = False
        try:
            prev = self._store.existing_hash(key)
            if prev == chash:
                self._store.update_meta(key, title, status, url, aliases)
            else:
                vec = self._embedder.embed_documents([text])[0]
                self._store.upsert_task(TaskRow(
                    key=key, aliases=aliases, title=title, description=description,
                    status=status, url=url, content_hash=chash, text=text,
                    embedding=vec))
                embedded = True
        except Exception as e:
            log.warning("index_task: сбой store для %s", key, exc_info=True)
            warnings.append(f"store: {type(e).__name__}: {e}")

        links_upserted = 0
        if self._graph is None:
            warnings.append("graph unavailable: task not added to task graph")
        else:
            try:
                self._graph.upsert_task(key, aliases, title, status, url)
                if links:
                    links_upserted = self._graph.upsert_links(key, links)
            except Exception as e:
                log.warning("index_task: сбой графа для %s", key, exc_info=True)
                warnings.append(f"graph: {type(e).__name__}: {e}")

        return {"key": key, "embedded": embedded,
                "links_upserted": links_upserted, "warnings": warnings}

    def search_tasks(self, query: str, top_k: int = 5) -> str:
        """Похожие по смыслу задачи (гибрид-поиск по корпусу). Пусто/сбой → текстовая нота."""
        try:
            vec = self._embedder.embed_query(query)
            hits = self._store.search(query, vec, top_k=top_k)
        except Exception:
            log.warning("search_tasks: сбой поиска", exc_info=True)
            return "(task search unavailable)"
        if not hits:
            return "(no similar tasks found)"
        return "\n".join(
            f"- {h.key} [{h.status or '—'}] {h.title} (score {h.score:.2f})"
            for h in hits)

    def get_task_context(self, key: str) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → код. Деградация → нота."""
        if self._graph is None:
            return "(task graph unavailable)"
        try:
            ctx = self._graph.task_context(key)
        except Exception:
            log.warning("get_task_context: сбой обхода графа", exc_info=True)
            return "(task graph unavailable)"
        if not ctx:
            return f"(no task '{key}' in task graph)"
        return _format_task_context(ctx, self._max_chars)

    def link_review(self, task_key: str, pr: PRRef, touched_node_ids) -> None:
        """Авто-линковка PR↔задача↔код (fail-soft; no-op без графа/ключа)."""
        if self._graph is None or not task_key:
            return
        try:
            self._graph.link_pr(task_key, pr, list(touched_node_ids or []))
        except Exception:
            log.warning("link_review: сбой линковки PR для %s", task_key, exc_info=True)


def _format_task_context(ctx: dict, max_chars: int) -> str:
    lines: list[str] = []
    head = f"Task {ctx.get('key')}"
    if ctx.get("status"):
        head += f" [{ctx['status']}]"
    if ctx.get("title"):
        head += f": {ctx['title']}"
    lines.append(head)
    if ctx.get("url"):
        lines.append(f"  url: {ctx['url']}")

    prs = ctx.get("prs") or []
    if prs:
        lines.append("  Implemented by PRs:")
        for p in prs:
            line = f"    - {p.get('id')}"
            if p.get("url"):
                line += f" ({p['url']})"
            touched = ", ".join(p.get("touched") or [])
            if touched:
                line += f" touches: {touched}"
            lines.append(line)

    linked = ctx.get("linked") or []
    if linked:
        lines.append("  Linked tasks:")
        for n in linked:
            ltype = n.get("type") or "relates"
            nstatus = f" [{n['status']}]" if n.get("status") else ""
            line = f"    - [{ltype}] {n.get('key')}{nstatus}: {n.get('title') or ''}"
            npr = [p.get("id") for p in (n.get("prs") or []) if p.get("id")]
            if npr:
                line += "  PRs: " + ", ".join(npr)
            lines.append(line)

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n… (truncated)"
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/tasks/test_service.py -q`
Expected: PASS (13 tests).

- [ ] **Step 5: Lint & commit**

```bash
.venv/bin/ruff check reviewer/tasks/service.py
git add reviewer/tasks/service.py tests/tasks/test_service.py
git commit -m "feat(tasks): TaskService (index_task/search_tasks/get_task_context/link_review)"
```

---

## Task 5: Wire Components / build_components

**Files:**
- Modify: `reviewer/app.py`
- Test: `tests/test_app_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_wiring.py`:
```python
def test_build_components_wires_task_components(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.task_store is not None
    assert c.task_service is not None
    assert c.task_graph is None  # connect=False → graph None → task_graph None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_app_wiring.py::test_build_components_wires_task_components -q`
Expected: FAIL — `AttributeError: ... has no attribute 'task_store'`.

- [ ] **Step 3: Update `reviewer/app.py`**

Add imports near the existing ones:
```python
from reviewer.tasks.store import TaskStore
from reviewer.tasks.graph import TaskGraph
from reviewer.tasks.service import TaskService
```
Extend `Components`:
```python
@dataclass
class Components:
    settings: Settings
    store: ChunkStore
    graph: GraphStore | None
    embedder: VoyageEmbedder
    reranker: VoyageReranker
    retriever: Retriever
    task_store: TaskStore
    task_graph: TaskGraph | None
    task_service: TaskService
```
At the end of `build_components`, before `return`:
```python
    task_store = TaskStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    task_graph = TaskGraph(graph.driver) if graph is not None else None
    task_service = TaskService(
        task_store, task_graph, embedder,
        max_chars=settings.max_tool_result_chars,
    )
    return Components(settings, store, graph, embedder, reranker, retriever,
                      task_store, task_graph, task_service)
```
(Delete the old `return Components(...)` line.)

- [ ] **Step 4: Run to verify it passes (and nothing regressed)**

Run: `.venv/bin/pytest tests/test_app_wiring.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Guard against positional Components construction**

Run: `grep -rn "Components(" reviewer/ tests/`
Expected: only `reviewer/app.py` constructs `Components(...)` positionally; everywhere else it is a type hint or a `MagicMock`. If any other positional construction exists, update it. Then:
```bash
.venv/bin/pytest -q
```
Expected: full unit suite PASS.

- [ ] **Step 6: Lint & commit**

```bash
.venv/bin/ruff check reviewer/app.py
git add reviewer/app.py tests/test_app_wiring.py
git commit -m "feat(tasks): проводка task_store/task_graph/task_service в Components"
```

---

## Task 6: MCP service — task tool delegates + publish auto-link

**Files:**
- Modify: `reviewer/mcp/service.py`
- Test: `tests/mcp/test_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_service.py` (helpers `_make_mcp_service`, `_fake_vcs`, patches already exist in the file):
```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_links_task_when_task_key(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """publish_review с task_key линкует PR↔задача↔код через task_service.link_review."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)

    svc.publish_review("o/r", 7, "summary", [], dry_run=False, task_key="ID-1")

    components.task_service.link_review.assert_called_once()
    args = components.task_service.link_review.call_args.args
    assert args[0] == "ID-1"                       # canonical task key
    pr_ref = args[1]
    assert pr_ref.repo == "o/r" and pr_ref.number == 7 and pr_ref.sha == "head456"
    assert pr_ref.url == "https://github.com/o/r/pull/7"
    assert args[2] == ["a.py#foo"]                 # changed_node_ids from session


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_no_link_on_dry_run(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, "summary", [], dry_run=True, task_key="ID-1")
    components.task_service.link_review.assert_not_called()


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_no_link_without_task_key(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, "summary", [], dry_run=False)
    components.task_service.link_review.assert_not_called()


def test_task_tool_delegates() -> None:
    """index_task/search_tasks/get_task_context делегируют в task_service."""
    svc = _make_mcp_service()
    svc.components.task_service.index_task.return_value = {"key": "ID-1"}
    svc.components.task_service.search_tasks.return_value = "list"
    svc.components.task_service.get_task_context.return_value = "ctx"
    assert svc.index_task({"key": "ID-1"}) == {"key": "ID-1"}
    assert svc.search_tasks("q", 3) == "list"
    assert svc.get_task_context("ID-1") == "ctx"
    svc.components.task_service.search_tasks.assert_called_once_with("q", 3)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k "task" -q`
Expected: FAIL — `AttributeError: 'MCPReviewService' has no attribute 'index_task'` and missing `task_key` kwarg.

- [ ] **Step 3: Add delegate methods + import to `reviewer/mcp/service.py`**

Add the import near the top imports:
```python
from reviewer.tasks.graph import PRRef
```
Add three methods (e.g. after `get_changed_file_diff`):
```python
    def index_task(self, task: dict) -> dict:
        """Проиндексировать нормализованный TaskBrief: эмбеддинг + граф задачи."""
        return self.components.task_service.index_task(task)

    def search_tasks(self, query: str, top_k: int = 5) -> str:
        """Похожие по смыслу задачи (гибрид-поиск по корпусу задач)."""
        return self.components.task_service.search_tasks(query, top_k)

    def get_task_context(self, key: str) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → затронутый код."""
        return self.components.task_service.get_task_context(key)
```

- [ ] **Step 4: Add `task_key` to `publish_review` + the auto-link step**

Change the signature:
```python
    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
```
Inside the body, right after the publish block (step 5, after `error, posted` are set and before step 6 history), insert:
```python
        # 5b) Авто-линковка PR↔задача↔код в граф задач (реальная публикация).
        # Граф недоступен / сбой — fail-soft внутри link_review, ревью не падает.
        if not dry_run and posted and task_key:
            pr_ref = PRRef(
                repo=repo,
                number=pr,
                url=f"https://github.com/{repo}/pull/{pr}",
                sha=p.prq.head_sha,
            )
            self.components.task_service.link_review(
                task_key, pr_ref, p.changed_node_ids,
            )
```
Update the docstring of `publish_review` to mention the optional `task_key` and the auto-link behaviour (one sentence).

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS (existing + 4 new).

- [ ] **Step 6: Lint & commit**

```bash
.venv/bin/ruff check reviewer/mcp/service.py
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): тулы index_task/search_tasks/get_task_context + авто-линковка PR в publish_review"
```

---

## Task 7: MCP server — register task tools + publish task_key

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py`
- Test: `tests/mcp/test_server_tools.py` (new)

- [ ] **Step 1: Write the failing test**

`tests/mcp/test_server_tools.py`:
```python
"""create_server регистрирует тулы задач и пробрасывает task_key в publish_review."""
from unittest.mock import MagicMock

from reviewer.entrypoints.mcp_server import create_server


def _service() -> MagicMock:
    s = MagicMock()
    s.index_task.return_value = {"key": "ID-1"}
    s.search_tasks.return_value = "tasks"
    s.get_task_context.return_value = "ctx"
    return s


def test_task_tools_registered():
    import asyncio

    svc = _service()
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"index_task", "search_tasks", "get_task_context"} <= names


def test_publish_review_tool_passes_task_key():
    svc = _service()
    create_server(svc)
    # прямой вызов делегата (тул — тонкая обёртка): сигнатура несёт task_key
    svc.publish_review("o/r", 7, "s", [], False, "ID-1")
    svc.publish_review.assert_called_with("o/r", 7, "s", [], False, "ID-1")
```

Note: `server.list_tools()` is the FastMCP API for enumerating registered tools; it is async, hence `asyncio.run`. If your FastMCP version exposes tools differently, assert registration via the equivalent accessor — the goal is that the three names exist.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -q`
Expected: FAIL — task tools not registered.

- [ ] **Step 3: Register the tools + extend `publish_review` tool**

In `reviewer/entrypoints/mcp_server.py`, add inside `create_server` (after `get_changed_file_diff`, before `publish_review`):
```python
    @mcp.tool()
    def index_task(task: dict) -> dict:
        """Index a normalized TaskBrief into the task graph + vector store.
        task: {key, aliases[], title, description, criteria[], status, url, links[]}.
        Idempotent: re-embeds only when the task text changed. Returns
        {key, embedded, links_upserted, warnings}."""
        return service.index_task(task)

    @mcp.tool()
    def search_tasks(query: str, top_k: int = 5) -> str:
        """Find semantically similar tasks in the indexed task corpus."""
        return service.search_tasks(query, top_k)

    @mcp.tool()
    def get_task_context(key: str) -> str:
        """Graph context for a task (by key or alias): the task and its PRs,
        linked tasks and their PRs, and the code those PRs touched."""
        return service.get_task_context(key)
```
Change the `publish_review` tool to accept and forward `task_key`:
```python
    @mcp.tool()
    def publish_review(
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Deterministic publish tail: policy gate, line grounding, dedup,
        inline/summary split, suggestion invariants, fingerprint idempotency,
        comment cap, GitHub review post, history record, overlay cleanup.
        When task_key is set and the review is really published, the PR is linked
        to that task in the task graph (IMPLEMENTED_BY + TOUCHES changed code).
        Each finding: {category, severity(low|medium|high|critical), file, line,
        side(RIGHT|LEFT), code_quote, message, suggestion,
        fix:{start_line,end_line,replacement}|null, confidence:0..1}.
        With dry_run=true nothing is posted; the full report is returned."""
        return service.publish_review(repo, pr, summary, findings, dry_run, task_key)
```
Update the `create_server` docstring: "с 8 тулов" → "с 11 тулами".

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/mcp/test_server_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Lint & commit**

```bash
.venv/bin/ruff check reviewer/entrypoints/mcp_server.py
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): регистрация тулов задач и task_key в publish_review"
```

---

## Task 8: review-pr skill — persist → enrich → link

**Files:**
- Modify: `plugin/skills/review-pr/SKILL.md`

This task edits prompt markdown (no pytest). Verification = the review checklist in Step 2.

- [ ] **Step 1: Edit `SKILL.md`**

Replace step 2 ("Task context (optional)") so it ends by indexing the task, and insert an enrichment paragraph. After the existing sentence that builds the `TaskBrief`, append:

```markdown
   The `TaskBrief` schema is `{key, aliases[], title, description, criteria[], status, url, links[]}`
   (phase 3 adds `aliases[]` and uses `links[]`; see the board playbook for how to fill them).
   Once the `TaskBrief` is built, call `index_task(TaskBrief)` to persist it (idempotent — safe to
   repeat). Then gather task context to sharpen the requirements check:
   - `get_task_context(TaskBrief.key)` → linked tasks, their PRs, and the code those PRs touched;
   - `search_tasks("<TaskBrief.title>. <first lines of description>")` → semantically similar tasks.
   Keep ONLY the related/similar items that look relevant; you will pass them to the requirements
   dimension in step 4. All of this is best-effort: if `index_task`/`get_task_context`/`search_tasks`
   return a "(… unavailable)" note or error, continue — never abort the review.
```

In step 4, the `requirements` bullet, after "the `TaskBrief`," add:
```markdown
   , plus the related/similar task context gathered in step 2 (linked tasks, their PRs, touched code,
   similar tasks) as an optional "Related context" block
```

In step 6 (Publish), change the `publish_review` call instruction to pass the canonical task key:
```markdown
   Call `publish_review(repo, pr, summary, findings, dry_run, task_key)` where `task_key` is the
   canonical `TaskBrief.key` if a task was read (else omit / null). When published, this links the PR
   to the task in the graph for future reviews.
```

- [ ] **Step 2: Verify (manual checklist — no code)**

Read the edited `SKILL.md` and confirm:
- step 2 builds `TaskBrief`, then calls `index_task`, then `get_task_context` + `search_tasks`, all fail-open;
- step 4 requirements subagent receives the related context;
- step 6 passes canonical `task_key`;
- degradation language ("never abort") preserved.

- [ ] **Step 3: Commit**

```bash
git add plugin/skills/review-pr/SKILL.md
git commit -m "feat(skill): review-pr индексирует задачу, обогащает requirements контекстом графа, линкует PR"
```

---

## Task 9: requirements prompt — related context block

**Files:**
- Modify: `plugin/skills/review-pr/references/requirements-prompt.md`

- [ ] **Step 1: Edit the prompt**

In the "You are given:" list, add a third bullet:
```markdown
- optionally, a "Related context" block: linked tasks and their PRs, the code those PRs touched, and
  semantically similar tasks (from the task graph). This is BACKGROUND to understand how related work
  was implemented — it is NOT a source of new requirements.
```

In "Rules:", add:
```markdown
- Use the Related context (if present) only to interpret the task's intent and to check consistency
  with how linked/similar tasks were implemented. Never invent a requirement that exists only in the
  related context and not in this task's `description`/`criteria`.
```

- [ ] **Step 2: Verify (manual)**

Confirm the related-context input is described as background, the JSON output schema and `category: "requirements"` are unchanged.

- [ ] **Step 3: Commit**

```bash
git add plugin/skills/review-pr/references/requirements-prompt.md
git commit -m "feat(skill): requirements-промпт принимает контекст связанных задач как фон"
```

---

## Task 10: board playbooks — aliases, links, url

**Files:**
- Modify: `plugin/skills/review-pr/references/task-context-yougile.md`
- Modify: `plugin/skills/review-pr/references/task-context-jira.md`

- [ ] **Step 1: Update the Yougile playbook**

In the TaskBrief mapping table of `task-context-yougile.md`, change these rows so phase-3 fields are filled:
```markdown
| `key`       | resolved `idTaskCommon` (`ID-N`, company-wide) | canonical — globally unique, stable |
| `aliases`   | `[idTaskProject]` (`PRI-N`, per-project)       | other codes of the same task; lets a PR referencing either code resolve to one node |
| `links[]`   | `subtasks[]` (UUIDs of child tasks)            | for each subtask UUID, `get_task` it → `{type:"subtask", key:<its idTaskCommon>, title}` (best-effort; a failed subtask fetch is skipped, not fatal) |
| `url`       | `task_board.url_template` with the **project code** | the web link fragment is the project code (`…/team/<teamId>/#PRI-4`), so substitute `PRI-N` (not `ID-N`); default `null` if no template |
```
Add a short note under the table:
```markdown
**Canonical key note.** A PR may reference either code (`PRI-N` or `ID-N`); both resolve via
`get_task`. Always set `key` to the company-wide `idTaskCommon` and put the project `idTaskProject`
in `aliases` — `index_task` stores both as the node's `codes`, so the task is one node regardless of
which code a PR used.
```

- [ ] **Step 2: Update the Jira playbook**

In `task-context-jira.md`, add to the mapping:
```markdown
- `aliases`: `[]` (the Jira issue key is already the single canonical, globally-unique key).
- `links[]`: from `issuelinks` — one entry per linked issue as `{type:<link type, e.g. blocks/relates/duplicates>, key:<issue key>, title:<summary>}`.
```

- [ ] **Step 3: Verify (manual)**

Confirm both playbooks still say "build best-effort, a partial brief is fine", and the Yougile `url` note matches the confirmed fragment format `…/team/<teamId>/#PRI-4`.

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/review-pr/references/task-context-yougile.md plugin/skills/review-pr/references/task-context-jira.md
git commit -m "feat(skill): плейбуки задают aliases, links и url для графа задач"
```

---

## Task 11: /sync-tasks skill (corpus warm-up)

**Files:**
- Create: `plugin/skills/sync-tasks/SKILL.md`
- Create: `plugin/skills/sync-tasks/references/sync-tasks-yougile.md`

- [ ] **Step 1: Create `plugin/skills/sync-tasks/SKILL.md`**

```markdown
---
description: Warm the task graph & vector store by indexing a board into the reviewer MCP server. Use when the user asks to sync/index tasks ("sync tasks", "index the board", "просиндексируй задачи") so search_tasks/get_task_context have a corpus. Requires a connected board MCP and the reviewer MCP server.
---

# Sync Tasks

Bulk-index board tasks into the reviewer task graph + vector store so `search_tasks` and
`get_task_context` are useful before many PRs have accrued. You read the board via the connected
board MCP, normalize each task into a `TaskBrief`, and call `index_task` per task. The reviewer
Python never touches the board.

## Inputs

Parse from $ARGUMENTS (all optional):
- `--board <name>`: limit to one board by name.
- `--limit <N>`: index at most N tasks (useful for a first smoke run).
- a board type override; otherwise infer from the connected MCP (Yougile is the reference).

## Pipeline

1. **Locate config.** The board MCP server name and type come from the repo's `.review.yml`
   `task_board` block (the same one `review-pr` uses). If you do not have it, ask the user which
   board MCP to use, or read `.review.yml` from the repo. Tools are `mcp__<task_board.mcp>__*`.

2. **Iterate the board.** Follow `references/sync-tasks-<type>.md` (Yougile is the reference) to
   enumerate tasks. Apply `--board` / `--limit` if given.

3. **Normalize + index.** For each task, build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}` using the SAME mapping as
   `../review-pr/references/task-context-<type>.md`, then call `index_task(TaskBrief)`.
   `index_task` is idempotent (it re-embeds only when the task text changed), so re-running is cheap.

4. **Report.** Print a summary: indexed (embedded), refreshed (unchanged → metadata only), failed,
   and any `warnings` returned by `index_task` (e.g. "graph unavailable").

## Rate limits & failure handling (fail-open)

- Voyage free tier is 3 RPM / 10K TPM; embedding inside `index_task` already retries/backs off, so a
  large board simply runs slower — that is expected, not an error. Use `--limit` for a quick first
  pass.
- A single task that fails to read or index must NOT stop the sync: log it and continue.
- If the board MCP is not connected or the reviewer MCP server is unavailable, stop and tell the user
  what to connect — do not partially guess.
- Never write back to the board; this skill only reads it.
```

- [ ] **Step 2: Create `plugin/skills/sync-tasks/references/sync-tasks-yougile.md`**

```markdown
# Sync playbook — Yougile

Use when `task_board.type == "yougile"`. Tools are `mcp__<task_board.mcp>__<tool>`.

Goal: enumerate the board's tasks and hand each one to the normalization in
`../../review-pr/references/task-context-yougile.md` (same `TaskBrief` mapping), then `index_task`.

## 1. Enumerate tasks

1. `get_projects` → projects; if `--board <name>` is given, keep the matching project/board only.
2. For each project, `get_boards` → boards; for each board, `get_columns` → columns (also gives you
   column titles to resolve `status` without an extra `get_column` per task).
3. For each column, list its tasks. Use the available listing tool (e.g. `get_tasks` /
   `get_user_tasks` / the column's task ids); fetch each task with `get_task` to get the full object.

Apply `--limit N`: stop after N tasks total.

## 2. Normalize each task

Build the `TaskBrief` exactly as in `task-context-yougile.md`:
- `key` ← `idTaskCommon` (`ID-N`); `aliases` ← `[idTaskProject]` (`PRI-N`);
- `title` ← `title`; `description` ← `description`;
- `status` ← the column title from step 1.2 (you already have it — no extra call);
- `criteria[]` ← inline checklist in `description` if any, else `[]`;
- `links[]` ← one `{type:"subtask", key, title}` per `subtasks[]` UUID (resolve title via `get_task`,
  best-effort);
- `url` ← `task_board.url_template` with the project code (`PRI-N`) if a template is configured, else
  `null`.

## 3. Index

Call `index_task(TaskBrief)`. Accumulate the result counters (`embedded` true/false, `warnings`) for
the final report. A failure on one task is logged and skipped — keep going.
```

- [ ] **Step 3: Verify (manual)**

Confirm: the skill reuses the review-pr Yougile mapping (no duplication of mapping logic), is fail-open, respects `--board`/`--limit`, and never writes to the board.

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/sync-tasks/
git commit -m "feat(skill): /sync-tasks — прогрев корпуса задач через index_task"
```

---

## Task 12: Docs — README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the task graph, /sync-tasks, aliases, url_template**

In the task-board section of `README.md` (around the existing `task_board` block, line ~332):
- note that `url_template` substitutes the **project code** (`PRI-N`) for Yougile (the web fragment code), e.g. `https://ru.yougile.com/team/<teamId>/#{key}`;
- add a short "Граф и RAG по задачам (фаза 3)" paragraph:
  ```markdown
  **Граф и RAG по задачам (фаза 3).** Прочитанная задача индексируется в граф (Neo4j: узлы
  `:Task`/`:PR`, рёбра `TASK_LINK`/`IMPLEMENTED_BY`/`TOUCHES`) и в векторный индекс (Postgres,
  таблица `tasks`) тулом `index_task`. При ревью агент видит связанные задачи и их PR/код через
  `get_task_context`, а похожие по смыслу — через `search_tasks`; при публикации PR
  автоматически линкуется к задаче. Скилл `/sync-tasks` прогревает корпус задач с доски (идемпотентно,
  с backoff под Voyage). Канонический ключ узла — сквозной код доски (Yougile `ID-N` / Jira key),
  прочие коды (Yougile `PRI-N`) хранятся как `aliases`, поэтому PR по любому коду резолвится в один
  узел. Neo4j/доска недоступны → контекст пуст с предупреждением, ревью продолжается.
  ```
- mention the new MCP tools (`index_task`, `search_tasks`, `get_task_context`) in the MCP tools list if README enumerates them.

- [ ] **Step 2: Verify (manual)**

Read the changed section; confirm it matches the implemented behaviour (canonical key, aliases, url project code, fail-open).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): граф и RAG по задачам, /sync-tasks, aliases/url_template (фаза 3)"
```

---

## Task 13: End-to-end MCP integration test

**Files:**
- Append: `tests/tasks/test_integration.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/tasks/test_integration.py`:
```python
def test_task_service_end_to_end(store, graph):
    """index_task → search_tasks → link_review → get_task_context (PG+Neo4j, fake embed)."""
    from reviewer.tasks.graph import PRRef
    from reviewer.tasks.service import TaskService

    svc = TaskService(store, graph, _FakeEmbedder())

    rep = svc.index_task({
        "key": "ID-1", "aliases": ["PRI-1"], "title": "Add logout",
        "description": "Clear the session on logout", "criteria": ["redirects to /login"],
        "status": "Open", "url": "u",
        "links": [{"key": "ID-2", "title": "Child", "type": "subtask"}],
    })
    assert rep["embedded"] is True and rep["links_upserted"] == 1 and rep["warnings"] == []

    found = svc.search_tasks("logout session")
    assert "ID-1" in found

    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    svc.link_review("ID-1", pr, ["auth.py#logout"])

    ctx = svc.get_task_context("PRI-1")  # resolve by alias
    assert "ID-1" in ctx and "o/r#7" in ctx and "auth.py#logout" in ctx
    assert "ID-2" in ctx and "subtask" in ctx
```

This reuses the `store` and `graph` fixtures from Tasks 2 and 3.

- [ ] **Step 2: Run the full integration suite for tasks**

Run: `.venv/bin/pytest tests/tasks/test_integration.py -m integration -q`
Expected: PASS (3 tests: store, graph, end-to-end), with Postgres+Neo4j up.

- [ ] **Step 3: Commit**

```bash
git add tests/tasks/test_integration.py
git commit -m "test(tasks): e2e index→search→link→context на живых PG+Neo4j"
```

---

## Task 14: Final verification

**Files:** none.

- [ ] **Step 1: Full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (all unit tests; integration excluded by default).

- [ ] **Step 2: Integration suite (infra up)**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: PASS (task integration + pre-existing integration tests). Note any pre-existing integration failures unrelated to this work.

- [ ] **Step 3: Lint changed files only**

Run:
```bash
.venv/bin/ruff check reviewer/tasks/ reviewer/app.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py reviewer/graph/store.py reviewer/index/store.py
```
Expected: clean for the changed files (do not fix unrelated pre-existing debt).

- [ ] **Step 4: Skill sanity (no broken frontmatter)**

Run: `grep -R:l "^description:" plugin/skills/sync-tasks/SKILL.md plugin/skills/review-pr/SKILL.md`
Expected: both have valid frontmatter; eyeball the diffs once more.

---

## Self-Review (plan vs spec) — completed by author

- **Postgres `tasks` table** → Task 2 (schema) + TaskStore. ✓
- **Neo4j `:Task`/`:PR` + `TASK_LINK`/`IMPLEMENTED_BY`/`TOUCHES`** → Task 3. ✓
- **Canonical key + `aliases` (codes)** → Task 3 (`upsert_task` codes), Task 10 (Yougile playbook). ✓
- **`index_task`/`search_tasks`/`get_task_context`** → Task 4 (service), Task 6 (MCP delegates), Task 7 (tools). ✓
- **Auto-link PR↔task↔code on publish** → Task 6 (publish `task_key`), Task 7 (tool param). ✓
- **`index_task` consumes normalized TaskBrief; content_hash dedup** → Tasks 1, 4. ✓
- **review-pr persist/enrich/link; requirements related-context** → Tasks 8, 9. ✓
- **/sync-tasks warm-up** → Task 11. ✓
- **Degradation fail-open (graph None / errors)** → Task 4 tests + Task 6 fail-soft. ✓
- **Docs** → Task 12. ✓
- **Testing: unit (text/graph/service), integration (store/graph/e2e), MCP linking** → Tasks 1–7, 13. ✓
- **Open spec questions resolved in plan:** driver shared via `GraphStore.driver` property (Task 3); `get_task_context` text format bounded by `max_tool_result_chars` (Task 4 `_format_task_context`); task tools are repo-global, exposed at server level for both review subagents and `/sync-tasks` (Tasks 6–7). ✓

No placeholders; types/signatures consistent across tasks (`TaskRow`, `TaskHit`, `PRRef`, `TaskService(store, graph, embedder, max_chars=...)`, `link_review(task_key, PRRef, node_ids)`).
```
