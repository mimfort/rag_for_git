"""Integration-тесты изолированных TaskStore/TaskGraph с фейковым эмбеддером.

Инфраструктура запускается командой:
`docker compose --profile test up -d --wait paradedb-test neo4j-test`.
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


def test_link_pr_empty_sha_preserves_existing(graph):
    """Линковка того же PR с пустым sha не затирает реальный sha."""
    pr_real = PRRef(repo="o/r", number=20,
                    url="https://github.com/o/r/pull/20", sha="realsha")
    graph.link_pr("ID-A", pr_real, ["a.py#foo"])

    pr_empty = PRRef(repo="o/r", number=20,
                     url="https://github.com/o/r/pull/20", sha="")
    graph.link_pr("ID-B", pr_empty, [])  # другая задача, тот же PR, пустой sha

    ctx = graph.task_context("ID-A")
    assert ctx["prs"][0]["id"] == "o/r#20"
    assert ctx["prs"][0]["sha"] == "realsha"  # sha сохранён


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


def test_update_meta_batch_backfills_project(store):
    emb = _FakeEmbedder()
    text = build_task_text("T1", "d1", [])
    store.upsert_task(TaskRow(
        key="ID-1", aliases=["PRI-1"], title="T1", description="d1",
        status="Open", url=None, content_hash=task_content_hash(text),
        text=text, embedding=emb.embed_documents([text])[0], project=""))
    assert store.get_task("ID-1").project == ""

    store.update_meta_batch([{"key": "ID-1", "title": "T1", "status": "Done",
                              "url": "u", "aliases": ["PRI-1"], "project": "PRI"}])
    row = store.get_task("ID-1")
    assert row.project == "PRI"
    assert row.status == "Done"

    # задача не в сторе → no-op, ничего не создаётся
    store.update_meta_batch([{"key": "ID-404", "project": "PRI"}])
    assert store.get_task("ID-404") is None

    # пустой батч → без ошибок
    store.update_meta_batch([])


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


def test_two_projects_isolated_end_to_end(store, tgraph):
    """Holistic-тест: два проекта (PRI, TES) изолированы по всему стеку write+read.

    Проверяет: index_batch → search_tasks → get_task → get_task_context.
    Тест доказывает, что store-фильтр и граф-фильтр project работают согласованно.
    """
    emb = _FakeEmbedder()
    svc = TaskService(store, tgraph, emb)
    svc.index_batch([
        {"key": "PRI-1", "aliases": [], "title": "logout", "description": "session",
         "criteria": [], "status": "Open", "url": None, "links": [], "project": "PRI"},
        {"key": "TES-9", "aliases": [], "title": "logout", "description": "session",
         "criteria": [], "status": "Open", "url": None, "links": [], "project": "TES"},
    ])
    # search скоупнут по проекту: PRI видит только PRI-1
    out = svc.search_tasks("logout", top_k=10, project="PRI")
    assert "PRI-1" in out and "TES-9" not in out

    # get_task скоупнут: чужой проект не виден
    assert svc.get_task("TES-9", project="PRI") is None
    assert svc.get_task("TES-9", project="TES")["key"] == "TES-9"

    # связь в чужой проект не вылезает на чтении get_task_context
    tgraph.upsert_links("PRI-1", [{"type": "related", "key": "TES-9"}])
    ctx = svc.get_task_context("PRI-1", project="PRI")
    assert "TES-9" not in ctx


def test_purge_removes_link_only_stub_from_graph(store, graph):
    """Стаб :Task, созданный upsert_links (нет в сторе), вычищается из графа.

    Регрессия бага: вселенная ключей бралась только из Postgres-стора, поэтому
    link-стабы (соседи TASK_LINK) никогда не попадали в orphaned и оседали в
    графе навсегда, несмотря на --purge-orphaned."""
    svc = TaskService(store, graph, _FakeEmbedder())

    # Реальная задача линкуется на STUB-1, которого нет на доске → создаётся стаб.
    svc.index_task({
        "key": "ID-P4", "aliases": [], "title": "Has dangling link",
        "description": "Links to a non-board task", "criteria": [],
        "status": "Open", "url": None,
        "links": [{"key": "STUB-1", "title": "", "type": "relates"}],
    })
    assert "STUB-1" not in store.list_keys()      # стаб только в графе
    assert "STUB-1" in graph.list_keys()

    # Синк с активной только ID-P4: стаб STUB-1 должен уйти из графа.
    result = svc.purge_orphaned_tasks(["ID-P4"])
    assert "STUB-1" not in graph.list_keys()
    assert result["deleted_graph"] == 1
    assert "ID-P4" in store.list_keys()           # активная задача не тронута
