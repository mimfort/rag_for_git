"""Integration: TaskStore + TaskGraph на живых Postgres+Neo4j (без Voyage).

Эмбеддер фейковый (детерминированный 1024-вектор) — проверяем SQL/Cypher, не Voyage.
Требует docker compose up -d. Маркер integration (исключён из дефолтного прогона).
"""
from __future__ import annotations

import hashlib

import pytest

from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.store import ChunkStore
from reviewer.tasks.graph import PRRef, TaskGraph
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import TaskRow, TaskStore, build_task_text, task_content_hash

pytestmark = pytest.mark.integration


def _vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    # детерминированный ненулевой 1024-вектор
    return [((h[i % len(h)] + i) % 17) / 17.0 for i in range(1024)]


class _FakeEmbedder:
    def __init__(self):
        self.doc_calls: list[list[str]] = []

    def embed_documents(self, texts):
        self.doc_calls.append(list(texts))
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


@pytest.fixture()
def store():
    s = Settings()
    cs = ChunkStore(s.pg_dsn)
    cs.init_schema()  # создаёт таблицу tasks (schema.sql)
    cs.close()
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


@pytest.fixture()
def graph():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear()
    yield TaskGraph(g.driver)
    g.clear()
    g.close()


def test_taskgraph_link_and_context(graph):
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


def test_task_service_end_to_end(store, graph):
    """index_task → search_tasks → link_review → get_task_context (PG+Neo4j, fake embed)."""
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

    ctx = svc.get_task_context("PRI-1")  # резолв по alias
    assert "ID-1" in ctx and "o/r#7" in ctx and "auth.py#logout" in ctx
    assert "ID-2" in ctx and "subtask" in ctx


def test_index_batch_matches_sequential_index_task(store, graph):
    """index_batch([t1,t2]) даёт те же записи что index_task(t1)+index_task(t2)."""
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
    assert results[0]["warnings"] == []
    assert results[1]["warnings"] == []

    # Хэши в Postgres совпадают с тем, что вычислил бы index_task
    assert store.existing_hash("ID-B1") == task_content_hash(
        build_task_text(t1["title"], t1["description"], t1["criteria"]))
    assert store.existing_hash("ID-B2") == task_content_hash(
        build_task_text(t2["title"], t2["description"], t2["criteria"]))

    # Повторный прогон: без изменений → embedded=False, embed_documents не вызывается
    emb2 = _FakeEmbedder()
    svc2 = TaskService(store, graph, emb2)
    results2 = svc2.index_batch([t1, t2])
    assert all(r["embedded"] is False for r in results2)
    assert emb2.doc_calls == []
