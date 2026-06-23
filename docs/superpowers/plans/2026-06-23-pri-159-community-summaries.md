# PRI-159 — GraphRAG community summaries — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Предрасчитывать краткие summary подсистем кода (кластеры графа по модулям) оффлайн и отдавать их потребителям (`ask`, далее PR-walkthrough) как дешёвый высокоуровневый приор.

**Architecture:** Скилл-оркестрация: чистый Python кластеризует base-индекс по пути (`reviewer/graph/summaries.py`), MCP-тулы отдают кластеры и персистят summary в новую таблицу Postgres (`SummaryStore`), LLM-скилл `/reviewer_summarize-subsystems` пишет тексты, `ask` читает их через `get_subsystem_summaries`. Без LLM на сервере и без эмбеддингов в MVP (fetch-all).

**Tech Stack:** Python 3.11–3.13, psycopg + psycopg_pool + pgvector, Neo4j (GraphStore), FastMCP, pytest. Спек: `docs/superpowers/specs/2026-06-23-pri-159-community-summaries-design.md`.

## Global Constraints

- Язык проекта — **русский**: докстринги, комментарии, тексты summary, вывод скилла. Тело SKILL.md — на английском (токены), но скилл инструктирует отвечать пользователю по-русски.
- Коммиты — **Conventional Commits на русском, без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Ветка работы: `feat/graphrag-summaries-walkthrough`.
- `node_id = "path#fqn"` — единый ключ RAG↔граф.
- `ref` base-индекса ветки = `base:<branch>` (через `reviewer.index.refs.base_ref`).
- Всё скоупится по `(repo, branch)` (мульти-бранч/мульти-репо).
- Unit-тесты не трогают внешние сервисы (фейки/MagicMock). Тесты с реальным Postgres/Neo4j помечаются `@pytest.mark.integration` и не идут в дефолтном `pytest` (`addopts = -m 'not integration'`).
- Линт: `.venv/bin/ruff check .` (line-length 100, target py311).

---

### Task 1: Чистая логика кластеризации (`reviewer/graph/summaries.py`)

**Files:**
- Create: `reviewer/graph/summaries.py`
- Test: `tests/graph/test_summaries.py`

**Interfaces:**
- Produces:
  - `@dataclass Member{node_id: str, path: str, content_hash: str, start_line: int}`
  - `@dataclass Cluster{key: str, member_node_ids: list[str], files: list[str], top_symbols: list[dict], num_members: int, source_hash: str}`
  - `cluster_key(path: str, depth: int) -> str`
  - `compute_source_hash(items: list[tuple[str, str]]) -> str`  (items = `(node_id, content_hash)`)
  - `build_clusters(members: list[Member], in_degree_fn: Callable[[list[str]], dict[str,int]] | None, *, depth: int = 2, min_size: int = 1, top_n: int = 10) -> list[Cluster]`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/graph/test_summaries.py
from reviewer.graph.summaries import (
    Member, cluster_key, compute_source_hash, build_clusters,
)


def _m(node_id, path, h="h", line=1):
    return Member(node_id=node_id, path=path, content_hash=h, start_line=line)


def test_cluster_key_takes_first_depth_dir_segments():
    assert cluster_key("reviewer/index/store.py", 2) == "reviewer/index"
    assert cluster_key("reviewer/graph/store.py", 2) == "reviewer/graph"
    assert cluster_key("reviewer/index/sub/x.py", 2) == "reviewer/index"


def test_cluster_key_root_file_and_short_dir():
    assert cluster_key("setup.py", 2) == "<root>"
    assert cluster_key("reviewer/app.py", 2) == "reviewer"


def test_compute_source_hash_is_order_independent_and_changes_with_content():
    a = compute_source_hash([("x#f", "h1"), ("y#g", "h2")])
    b = compute_source_hash([("y#g", "h2"), ("x#f", "h1")])
    assert a == b                       # детерминирован, не зависит от порядка
    assert a != compute_source_hash([("x#f", "h1"), ("y#g", "CHANGED")])


def test_build_clusters_groups_by_module_and_filters_min_size():
    members = [
        _m("reviewer/index/a.py#A", "reviewer/index/a.py"),
        _m("reviewer/index/b.py#B", "reviewer/index/b.py"),
        _m("reviewer/graph/c.py#C", "reviewer/graph/c.py"),
    ]
    clusters = build_clusters(members, None, depth=2, min_size=2)
    keys = {c.key for c in clusters}
    assert keys == {"reviewer/index"}     # graph отброшен (1 < min_size=2)
    idx = next(c for c in clusters if c.key == "reviewer/index")
    assert idx.num_members == 2
    assert idx.files == ["reviewer/index/a.py", "reviewer/index/b.py"]


def test_build_clusters_ranks_top_symbols_by_in_degree():
    members = [
        _m("reviewer/x/a.py#A", "reviewer/x/a.py", line=10),
        _m("reviewer/x/b.py#B", "reviewer/x/b.py", line=20),
    ]
    deg = {"reviewer/x/b.py#B": 5, "reviewer/x/a.py#A": 1}
    [c] = build_clusters(members, lambda ids: deg, depth=2, top_n=10)
    assert c.top_symbols[0]["node_id"] == "reviewer/x/b.py#B"   # выше in_degree → первый


def test_build_clusters_fail_soft_when_in_degree_raises():
    members = [_m("reviewer/x/a.py#A", "reviewer/x/a.py")]
    def boom(ids):
        raise RuntimeError("neo4j down")
    [c] = build_clusters(members, boom, depth=2)   # не падает
    assert c.top_symbols[0]["node_id"] == "reviewer/x/a.py#A"
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/graph/test_summaries.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.graph.summaries`).

- [ ] **Step 3: Реализовать модуль**

```python
# reviewer/graph/summaries.py
"""Кластеризация графа кода по модулям/пути и расчёт ключа свежести.

Чистая логика без I/O: членство и центральность приходят аргументами, поэтому
покрывается unit-тестами без Postgres/Neo4j. Кластер = пакет/директория
(префикс пути node_id="path#fqn"); summary каждого кластера пишет LLM-скилл.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable


@dataclass
class Member:
    node_id: str          # "path#fqn"
    path: str
    content_hash: str
    start_line: int


@dataclass
class Cluster:
    key: str
    member_node_ids: list[str]
    files: list[str]
    top_symbols: list[dict]   # [{"node_id", "file", "line"}], отсортированы по центральности
    num_members: int
    source_hash: str


def cluster_key(path: str, depth: int) -> str:
    """Ключ кластера = директория пути, обрезанная до первых ``depth`` сегментов.

    "reviewer/index/store.py", depth=2 -> "reviewer/index".
    Файл в корне -> "<root>"; директория короче depth -> вся директория.
    """
    dir_parts = path.split("/")[:-1]      # отбросить имя файла
    if not dir_parts:
        return "<root>"
    return "/".join(dir_parts[:depth])


def compute_source_hash(items: list[tuple[str, str]]) -> str:
    """sha256 от sorted("node_id:content_hash") — детерминированный ключ свежести.

    Меняется только при изменении состава кластера или содержимого его файлов
    (content_hash — тот же дедуп-инвариант, что у чанков)."""
    joined = "\n".join(sorted(f"{nid}:{h}" for nid, h in items))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_clusters(
    members: list[Member],
    in_degree_fn: Callable[[list[str]], dict[str, int]] | None,
    *,
    depth: int = 2,
    min_size: int = 1,
    top_n: int = 10,
) -> list[Cluster]:
    """Сгруппировать членов по cluster_key; top_symbols — по in_degree (fail-soft)."""
    groups: dict[str, list[Member]] = {}
    for m in members:
        groups.setdefault(cluster_key(m.path, depth), []).append(m)

    degrees: dict[str, int] = {}
    if in_degree_fn is not None:
        try:
            degrees = in_degree_fn([m.node_id for m in members]) or {}
        except Exception:
            degrees = {}                  # граф недоступен → порядок по (path, line)

    clusters: list[Cluster] = []
    for key, ms in sorted(groups.items()):
        if len(ms) < min_size:
            continue
        ranked = sorted(
            ms, key=lambda m: (-degrees.get(m.node_id, 0), m.path, m.start_line))
        top = [{"node_id": m.node_id, "file": m.path, "line": m.start_line}
               for m in ranked[:top_n]]
        clusters.append(Cluster(
            key=key,
            member_node_ids=sorted(m.node_id for m in ms),
            files=sorted({m.path for m in ms}),
            top_symbols=top,
            num_members=len(ms),
            source_hash=compute_source_hash([(m.node_id, m.content_hash) for m in ms]),
        ))
    return clusters
```

- [ ] **Step 4: Прогнать тесты — должны пройти**

Run: `.venv/bin/pytest tests/graph/test_summaries.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/graph/summaries.py tests/graph/test_summaries.py
git add reviewer/graph/summaries.py tests/graph/test_summaries.py
git commit -m "feat(graph): кластеризация графа по модулям для community summaries (PRI-159)"
```

---

### Task 2: Таблица `subsystem_summaries` + `SummaryStore` + настройка глубины

**Files:**
- Modify: `reviewer/index/schema.sql` (добавить DDL в конец)
- Modify: `reviewer/config/settings.py` (поле `summary_cluster_depth`)
- Create: `reviewer/index/summary_store.py`
- Test: `tests/index/test_summary_store.py` (integration)

**Interfaces:**
- Produces `SummaryStore(dsn, *, min_size=1, max_size=4)` с методами:
  - `upsert_summary(repo, branch, cluster_key, title, summary, member_node_ids: list[str], source_hash) -> None`
  - `get_source_hashes(repo, branch) -> dict[str, str]`
  - `get_summaries(repo, branch) -> list[dict]`  (`{cluster_key, title, summary}`)
  - `get_summary(repo, branch, cluster_key) -> dict | None`
  - `close() -> None`
- Produces `Settings.summary_cluster_depth: int = 2`.

- [ ] **Step 1: Добавить DDL таблицы в `reviewer/index/schema.sql`** (в конец файла)

```sql
-- Предрасчитанные summary подсистем (PRI-159): кластер графа по модулю → краткий обзор.
-- Отдельно от chunks — у summary нет lines/symbol и base/overlay-freshness.
CREATE TABLE IF NOT EXISTS subsystem_summaries (
    repo            text    NOT NULL DEFAULT '',
    branch          text    NOT NULL,
    cluster_key     text    NOT NULL,            -- напр. "reviewer/index"
    title           text    NOT NULL,            -- одна строка «что это»
    summary         text    NOT NULL,            -- сжатый абзац (RU)
    member_node_ids text[]  NOT NULL DEFAULT '{}',
    source_hash     text    NOT NULL,            -- ключ свежести
    embedding       vector(1024),                -- nullable; зарезервировано под вектор-поиск
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch, cluster_key)
);
```

- [ ] **Step 2: Добавить поле настройки в `reviewer/config/settings.py`**

Рядом с другими int-полями добавить:

```python
    summary_cluster_depth: int = 2   # глубина пути для кластера подсистемы (PRI-159)
```

- [ ] **Step 3: Написать падающий integration-тест `SummaryStore`**

```python
# tests/index/test_summary_store.py
import os
import pytest

from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DSN = os.getenv("PG_DSN", "postgresql://postgres:postgres@localhost:5433/postgres")


@pytest.fixture()
def store():
    ChunkStore(DSN).init_schema()        # создаёт subsystem_summaries (schema.sql)
    s = SummaryStore(DSN)
    yield s
    with s._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo='t/t'")
        conn.commit()
    s.close()


def test_upsert_then_get_roundtrip(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "Индекс",
                         "Хранилище чанков и ретрив.", ["reviewer/index/store.py#X"], "h1")
    assert store.get_source_hashes("t/t", "dev") == {"reviewer/index": "h1"}
    rows = store.get_summaries("t/t", "dev")
    assert rows == [{"cluster_key": "reviewer/index", "title": "Индекс",
                     "summary": "Хранилище чанков и ретрив."}]
    one = store.get_summary("t/t", "dev", "reviewer/index")
    assert one["member_node_ids"] == ["reviewer/index/store.py#X"]


def test_upsert_is_idempotent_update(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "old", [], "h1")
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "new", [], "h2")
    assert store.get_source_hashes("t/t", "dev") == {"reviewer/index": "h2"}
    assert store.get_summaries("t/t", "dev")[0]["summary"] == "new"
```

- [ ] **Step 4: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_summary_store.py -m integration -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.index.summary_store`).

- [ ] **Step 5: Реализовать `reviewer/index/summary_store.py`** (зеркало `TaskStore`)

```python
# reviewer/index/summary_store.py
"""Хранилище предрасчитанных summary подсистем (таблица subsystem_summaries).

Зеркалит паттерн TaskStore: ленивый пул, register_vector на каждое соединение.
Таблицу создаёт ChunkStore.init_schema (общий schema.sql)."""
from __future__ import annotations

import threading

import psycopg.errors
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


class SummaryStore:
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
                        open=False, configure=lambda conn: register_vector(conn))
                    self._pool.open()
        return self._pool

    def _connect(self):
        return self._ensure_pool().connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def upsert_summary(self, repo: str, branch: str, cluster_key: str, title: str,
                       summary: str, member_node_ids: list[str], source_hash: str) -> None:
        sql = """
        INSERT INTO subsystem_summaries
            (repo, branch, cluster_key, title, summary, member_node_ids, source_hash, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (repo, branch, cluster_key) DO UPDATE SET
            title=EXCLUDED.title, summary=EXCLUDED.summary,
            member_node_ids=EXCLUDED.member_node_ids,
            source_hash=EXCLUDED.source_hash, updated_at=now()
        """
        with self._connect() as conn:
            conn.execute(sql, (repo, branch, cluster_key, title, summary,
                               member_node_ids, source_hash))
            conn.commit()

    def get_source_hashes(self, repo: str, branch: str) -> dict[str, str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, source_hash FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s", (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return {}
        return {k: h for k, h in rows}

    def get_summaries(self, repo: str, branch: str) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cluster_key, title, summary FROM subsystem_summaries "
                    "WHERE repo=%s AND branch=%s ORDER BY cluster_key",
                    (repo, branch)).fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [{"cluster_key": k, "title": t, "summary": s} for k, t, s in rows]

    def get_summary(self, repo: str, branch: str, cluster_key: str) -> dict | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT cluster_key, title, summary, member_node_ids, source_hash, updated_at "
                    "FROM subsystem_summaries WHERE repo=%s AND branch=%s AND cluster_key=%s",
                    (repo, branch, cluster_key)).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        if row is None:
            return None
        return {"cluster_key": row[0], "title": row[1], "summary": row[2],
                "member_node_ids": list(row[3] or []), "source_hash": row[4],
                "updated_at": row[5].isoformat()}
```

- [ ] **Step 6: Прогнать integration-тест — должен пройти**

Run: `.venv/bin/pytest tests/index/test_summary_store.py -m integration -q`
Expected: PASS (нужен поднятый Postgres :5433 — `docker compose up -d`).

- [ ] **Step 7: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/index/summary_store.py tests/index/test_summary_store.py
git add reviewer/index/schema.sql reviewer/config/settings.py reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): таблица и SummaryStore для community summaries (PRI-159)"
```

---

### Task 3: `ChunkStore.list_base_members` — состав base-индекса для кластеризации

**Files:**
- Modify: `reviewer/index/store.py` (новый метод в `ChunkStore`)
- Test: `tests/index/test_summary_store.py` (добавить integration-тест)

**Interfaces:**
- Consumes: `reviewer.index.refs.base_ref`.
- Produces: `ChunkStore.list_base_members(repo, branch) -> list[tuple[str, str, str, int]]` = `(path, symbol_fqn, content_hash, start_line)` для `ref=base:<branch>`.

- [ ] **Step 1: Добавить падающий integration-тест** (в конец `tests/index/test_summary_store.py`)

```python
def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.store import ChunkStore, ChunkRow
    cs = ChunkStore(DSN)
    cs.init_schema()
    cs.upsert([ChunkRow(repo="t/t", ref="base:dev", content_hash="h", path="reviewer/x/a.py",
                        lang="python", symbol_fqn="A", kind="function",
                        start_line=3, end_line=9, text="def a(): ...", embedding=[0.0]*1024)])
    try:
        members = cs.list_base_members("t/t", "dev")
        assert ("reviewer/x/a.py", "A", "h", 3) in members
    finally:
        cs.delete_ref("t/t", "base:dev")
        cs.close()
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_summary_store.py::test_list_base_members_reads_base_ref_rows -m integration -q`
Expected: FAIL (`AttributeError: 'ChunkStore' object has no attribute 'list_base_members'`).

- [ ] **Step 3: Реализовать метод** (в `reviewer/index/store.py`, рядом с `count_chunks`)

```python
    def list_base_members(self, repo: str, branch: str) -> list[tuple[str, str, str, int]]:
        """Состав base-индекса ветки для кластеризации подсистем (PRI-159):
        (path, symbol_fqn, content_hash, start_line) для ref=base:<branch>."""
        from reviewer.index.refs import base_ref
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, symbol_fqn, content_hash, start_line FROM chunks "
                "WHERE repo=%s AND ref=%s", (repo, base_ref(branch))).fetchall()
        return [(p, s, h, sl) for p, s, h, sl in rows]
```

- [ ] **Step 4: Прогнать — должен пройти**

Run: `.venv/bin/pytest tests/index/test_summary_store.py -m integration -q`
Expected: PASS.

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/index/store.py
git add reviewer/index/store.py tests/index/test_summary_store.py
git commit -m "feat(index): ChunkStore.list_base_members для кластеризации подсистем (PRI-159)"
```

---

### Task 4: Wiring в `Components` + три MCP-метода + тулы

**Files:**
- Modify: `reviewer/app.py` (поле `Components.summary_store`, сборка в `build_components`)
- Modify: `reviewer/mcp/service.py` (методы `list_subsystem_clusters`, `index_subsystem_summary`, `get_subsystem_summaries`)
- Modify: `reviewer/entrypoints/mcp_server.py` (три `@mcp.tool()`-обёртки)
- Test: `tests/test_app_wiring.py` (добавить), `tests/mcp/test_subsystem_summaries.py` (создать)

**Interfaces:**
- Consumes: `build_clusters`/`Member` (Task 1), `SummaryStore` (Task 2), `ChunkStore.list_base_members` (Task 3), `MCPReviewService._resolve_repo_branch` (есть), `GraphStore.in_degree(repo, node_ids, *, branch)` (есть).
- Produces (service-методы и одноимённые тулы):
  - `list_subsystem_clusters(repo, branch=None, depth=None, min_size=None) -> dict` → `{"branch", "clusters": [{cluster_key, num_members, files, top_symbols, source_hash, stale}]}` или `{"clusters": [], "note": ...}`.
  - `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash) -> dict` → `{"cluster_key", "stored": True}`.
  - `get_subsystem_summaries(repo, branch=None, cluster_key=None) -> dict` → `{"summaries": [...]}` (или `{"summary": {...}|None}` при заданном cluster_key).

- [ ] **Step 1: Добавить падающий wiring-тест** (в `tests/test_app_wiring.py`)

```python
def test_build_components_wires_summary_store(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.summary_store is not None
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/test_app_wiring.py::test_build_components_wires_summary_store -q`
Expected: FAIL (`AttributeError: ... 'summary_store'`).

- [ ] **Step 3: Подключить `SummaryStore` в `reviewer/app.py`**

В `@dataclass Components` добавить поле (после `store`):

```python
    summary_store: "SummaryStore"
```

Добавить импорт сверху:

```python
from reviewer.index.summary_store import SummaryStore
```

В `build_components` создать рядом со `store` и передать в `Components(...)` (последним аргументом, сохранив порядок остальных):

```python
    summary_store = SummaryStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    ...
    return Components(settings, store, graph, embedder, reranker, retriever,
                      task_store, task_graph, task_service, sync_service,
                      summary_store)
```

(Поле `summary_store` добавить в конец списка полей `Components` и в конец позиционных аргументов конструктора.)

- [ ] **Step 4: Прогнать wiring-тест — проходит**

Run: `.venv/bin/pytest tests/test_app_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Написать падающий unit-тест service-методов** (`tests/mcp/test_subsystem_summaries.py`)

```python
"""Unit-тесты MCP-методов community summaries (PRI-159). Фейки вместо Postgres/Neo4j."""
from __future__ import annotations

from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.default_repo = ""
    return s


def _svc(components) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    # изолируем резолв repo/ветки от REVIEW_BRANCHES в .env
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    return svc


def test_list_subsystem_clusters_marks_stale():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1),
        ("reviewer/index/b.py", "B", "h2", 2),
    ]
    c.graph = None                                  # граф недоступен → fail-soft
    c.summary_store.get_source_hashes.return_value = {}   # ничего не сохранено → stale=True
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    [cl] = out["clusters"]
    assert cl["cluster_key"] == "reviewer/index"
    assert cl["num_members"] == 2
    assert cl["stale"] is True


def test_list_subsystem_clusters_fresh_when_hash_matches():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1)]
    c.graph = None
    # вычисляем эталонный source_hash так же, как продакшен
    from reviewer.graph.summaries import compute_source_hash
    sh = compute_source_hash([("reviewer/index/a.py#A", "h1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}
    svc = _svc(c)
    [cl] = svc.list_subsystem_clusters("o/n", "dev")["clusters"]
    assert cl["stale"] is False


def test_list_subsystem_clusters_empty_index_returns_note():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev")
    assert out["clusters"] == []
    assert "note" in out


def test_index_and_get_subsystem_summaries_roundtrip_via_store():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "..."}]
    svc = _svc(c)
    assert svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", "h1") == {
        "cluster_key": "reviewer/index", "stored": True}
    c.summary_store.upsert_summary.assert_called_once()
    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"
```

- [ ] **Step 6: Прогнать — падает**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: FAIL (`AttributeError: ... 'list_subsystem_clusters'`).

- [ ] **Step 7: Реализовать три метода в `reviewer/mcp/service.py`** (рядом с `search_codebase`)

```python
    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None,
                                min_size: int | None = None) -> dict:
        """Кластеризовать base-граф по модулям → кластеры для /summarize-subsystems."""
        from reviewer.graph.summaries import Member, build_clusters
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"clusters": [], "note": rb}
        repo, resolved = rb
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"clusters": [],
                    "note": "(base-индекс пуст — выполните /reviewer_sync-codebase)"}
        members = [Member(node_id=f"{p}#{s}", path=p, content_hash=h, start_line=sl)
                   for p, s, h, sl in raw]
        graph = self.components.graph
        in_degree_fn = (
            (lambda ids: graph.in_degree(repo, ids, branch=resolved))
            if graph is not None else None)
        clusters = build_clusters(
            members, in_degree_fn,
            depth=depth or self.settings.summary_cluster_depth,
            min_size=min_size or 1)
        stored = self.components.summary_store.get_source_hashes(repo, resolved)
        return {"branch": resolved, "clusters": [
            {"cluster_key": c.key, "num_members": c.num_members, "files": c.files,
             "top_symbols": c.top_symbols, "source_hash": c.source_hash,
             "stale": stored.get(c.key) != c.source_hash}
            for c in clusters]}

    def index_subsystem_summary(self, repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str) -> dict:
        """Персистнуть один summary подсистемы (idempotent upsert)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"stored": False, "note": rb}
        repo, resolved = rb
        self.components.summary_store.upsert_summary(
            repo, resolved, cluster_key, title, summary, [], source_hash)
        return {"cluster_key": cluster_key, "stored": True}

    def get_subsystem_summaries(self, repo: str, branch: str | None = None,
                                cluster_key: str | None = None) -> dict:
        """Дешёвый приор: предрасчитанные summary подсистем (fail-open у потребителя)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"summaries": [], "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        if cluster_key:
            return {"summary": store.get_summary(repo, resolved, cluster_key)}
        return {"summaries": store.get_summaries(repo, resolved)}
```

- [ ] **Step 8: Прогнать unit-тест — проходит**

Run: `.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q`
Expected: PASS (4 passed).

- [ ] **Step 9: Зарегистрировать три тула в `reviewer/entrypoints/mcp_server.py`** (после `get_pr_diff`, перед `publish_review`)

```python
    @mcp.tool()
    def list_subsystem_clusters(repo: str, branch: str | None = None,
                                depth: int | None = None,
                                min_size: int | None = None) -> dict:
        """Cluster the base code graph into subsystems (by module path) for the
        /reviewer_summarize-subsystems skill. Returns per cluster: cluster_key,
        num_members, files, top_symbols (by centrality), source_hash, and stale
        (true when the stored summary is missing or its source_hash differs).
        No PR session; branch defaults to the primary tracked branch."""
        return service.list_subsystem_clusters(repo, branch, depth, min_size)

    @mcp.tool()
    def index_subsystem_summary(repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str) -> dict:
        """Persist one subsystem summary (idempotent upsert keyed by
        repo+branch+cluster_key). Called by /reviewer_summarize-subsystems after the
        LLM writes title+summary for a cluster. source_hash ties the summary to the
        cluster's current content for staleness."""
        return service.index_subsystem_summary(
            repo, branch, cluster_key, title, summary, source_hash)

    @mcp.tool()
    def get_subsystem_summaries(repo: str, branch: str | None = None,
                                cluster_key: str | None = None) -> dict:
        """Cheap high-level prior for ask / PR-walkthrough: precomputed subsystem
        summaries. cluster_key=None → all {cluster_key, title, summary}; a cluster_key
        → one full summary (or null). Empty when none built (consumer is fail-open).
        No PR session; branch defaults to primary."""
        return service.get_subsystem_summaries(repo, branch, cluster_key)
```

- [ ] **Step 10: Smoke — сервер регистрирует тулы**

Run: `.venv/bin/pytest tests/mcp/test_server.py tests/mcp/test_server_tools.py -q`
Expected: PASS (существующие тесты сервера не сломаны; при необходимости добавить имена новых тулов в их allowlist).

- [ ] **Step 11: Линт + коммит**

```bash
.venv/bin/ruff check reviewer/app.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py
git add reviewer/app.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/test_app_wiring.py tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): тулы list/index/get subsystem summaries + wiring (PRI-159)"
```

---

### Task 5: Скилл `/reviewer_summarize-subsystems` + guard-тест

**Files:**
- Create: `plugin/skills/summarize-subsystems/SKILL.md`
- Test: `tests/skills/test_summarize_subsystems.py`

**Interfaces:**
- Consumes тулы Task 4 (`list_subsystem_clusters`, `index_subsystem_summary`) + harness `Read`.

- [ ] **Step 1: Написать падающий guard-тест**

```python
# tests/skills/test_summarize_subsystems.py
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md")


def test_skill_exists_and_mentions_tools():
    text = SKILL.read_text(encoding="utf-8")
    assert "list_subsystem_clusters" in text
    assert "index_subsystem_summary" in text


def test_skill_includes_common_blocks_that_exist():
    text = SKILL.read_text(encoding="utf-8")
    common = SKILL.resolve().parents[1] / "_common"
    import re
    includes = re.findall(r"<!-- include: (_common/[\w\-./]+) -->", text)
    assert includes, "нет include-маркеров _common"
    for inc in includes:
        assert (common.parent / inc).is_file(), f"include не найден: {inc}"


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "russian" in text or "русск" in text
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/skills/test_summarize_subsystems.py -q`
Expected: FAIL (`FileNotFoundError`).

- [ ] **Step 3: Создать `plugin/skills/summarize-subsystems/SKILL.md`**

```markdown
---
name: reviewer_summarize-subsystems
description: Precompute concise per-subsystem summaries (GraphRAG community summaries) over the base code index, so ask / PR-walkthrough get a cheap high-level prior. Use when the user asks to build/refresh subsystem summaries ("просуммируй подсистемы", "построй обзоры модулей", "summarize subsystems"). Requires a built base index + the reviewer MCP server.
---

# Summarize subsystems (community summaries)

Cluster the base code graph into subsystems (by module path) and write a short, **grounded**
summary for each, persisted for `ask` / PR-walkthrough to use as a cheap high-level prior. This
skill reads code and writes summaries to the reviewer store; it does NOT modify code or post to
GitHub.

**Always write summaries and answer the user in Russian** (the project language), regardless of this
file's language. Tool calls, code identifiers and `path:line` stay verbatim.

## Tools

<!-- include: _common/tool-usage.md -->
Plus `list_subsystem_clusters` and `index_subsystem_summary` (reviewer MCP), and the harness `Read`.

## Pipeline

1. **Resolve repo/branch.**

<!-- include: _common/branch-selection.md -->

2. **List clusters.** Call `list_subsystem_clusters(repo, branch)`. Empty / `note` about an empty
   index → tell the user (in Russian) to run `/reviewer_sync-codebase` first, then stop.

3. **Summarize only STALE clusters.** For each cluster with `stale == true` (fresh ones are already
   up to date — skip them, this keeps the pass incremental and cheap):
   - `Read` a few representative files (from `files` / `top_symbols`) to ground the summary — do NOT
     invent behavior the code does not show.
   - Write, in Russian:
     - `title` — one line: what this subsystem is.
     - `summary` — a compact paragraph: what it does, its key symbols (from `top_symbols`) and
       invariants. No `path:line` required in the text; it is a high-level prior.
   - Persist: `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash)` —
     pass back the cluster's own `source_hash` from step 2.

4. **Report (Russian).** How many clusters summarized vs skipped-as-fresh.

## Grounding (hard rule)

<!-- include: _common/anti-hallucination.md -->

Every summary must reflect real code you read. If a cluster is unclear, say so briefly rather than
guessing.

## Notes

- Precondition: base index built (`reviewer index`). Re-running is incremental: unchanged subsystems
  (matching `source_hash`) are skipped.
- Read-only on code and GitHub; only writes summaries to the reviewer store.
```

- [ ] **Step 4: Прогнать guard-тест + общий guard — проходят**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (новый тест зелёный; `test_common_blocks`/`test_assembled_prompts` не сломаны — include-маркеры валидны).

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/summarize-subsystems/SKILL.md tests/skills/test_summarize_subsystems.py
git commit -m "feat(skills): скилл summarize-subsystems для community summaries (PRI-159)"
```

---

### Task 6: Интеграция приора в `ask` + guard-тест

**Files:**
- Modify: `plugin/skills/ask/SKILL.md` (шаг-приор `get_subsystem_summaries`)
- Test: `tests/skills/test_ask_uses_summaries.py`

**Interfaces:**
- Consumes: `get_subsystem_summaries` (Task 4).

- [ ] **Step 1: Написать падающий guard-тест**

```python
# tests/skills/test_ask_uses_summaries.py
from pathlib import Path

ASK = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "ask" / "SKILL.md"


def test_ask_references_subsystem_summaries_prior():
    text = ASK.read_text(encoding="utf-8")
    assert "get_subsystem_summaries" in text


def test_ask_marks_prior_fail_open():
    text = ASK.read_text(encoding="utf-8").lower()
    assert "fail-open" in text and "get_subsystem_summaries" in ASK.read_text(encoding="utf-8")
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/skills/test_ask_uses_summaries.py -q`
Expected: FAIL (маркера нет в ask/SKILL.md).

- [ ] **Step 3: Вставить шаг-приор в `plugin/skills/ask/SKILL.md`**

После шага «1. Resolve repo/branch» и перед «2. Search» добавить пункт:

```markdown
1.5. **Subsystem prior (cheap, optional).** Call `get_subsystem_summaries(repo, branch)`. If it
   returns summaries, use the one matching the question's subsystem as a high-level orientation
   **before** `search_codebase` — this cuts exploration steps for architectural / "how does
   subsystem X work" questions. The summary is only a prior: every `path:line` you cite in the
   answer still comes from real code (`search_codebase` / `Read`), never from the summary text.
   **Fail-open:** empty / unavailable → skip this step and proceed exactly as before.
```

- [ ] **Step 4: Прогнать — проходит**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS.

- [ ] **Step 5: Полный прогон + коммит**

```bash
.venv/bin/pytest -q
git add plugin/skills/ask/SKILL.md tests/skills/test_ask_uses_summaries.py
git commit -m "feat(skills): ask использует subsystem-summaries как приор (PRI-159)"
```

---

## Self-Review

**Spec coverage:**
- Кластеризация по модулю/пути (configurable depth) → Task 1 (`cluster_key`/`build_clusters`) + `summary_cluster_depth` (Task 2).
- Членство из Postgres-чанков, центральность из графа fail-soft → Task 3 (`list_base_members`) + Task 1 (`in_degree_fn` fail-soft) + Task 4 (wiring `graph=None` → `None`).
- Хранилище — новая таблица `subsystem_summaries` per `(repo, branch)`, `source_hash`-свежесть → Task 2.
- Три MCP-тула (list/index/get) → Task 4.
- BUILD-скилл (RU, инкрементальный по `stale`, grounded) → Task 5.
- CONSUME-интеграция в `ask` (fail-open) → Task 6.
- Тесты: unit core, integration store, unit service (fakes), guard skills → Tasks 1–6.

**Placeholder scan:** нет TBD/TODO; весь код приведён, команды и ожидаемый вывод указаны.

**Type consistency:** `Member`/`Cluster` (Task 1) совпадают с использованием в `list_subsystem_clusters` (Task 4); `source_hash` пробрасывается из `list_subsystem_clusters` в `index_subsystem_summary` без переименований; `member_node_ids` в MVP хранится `[]` (колонка зарезервирована) — согласовано в Task 2/4.

**Вне объёма (из спека):** вектор-поиск по summary (колонка `embedding` создана, не заполняется), кластеризация по связности, CLI-подкоманда, server-side LLM.
