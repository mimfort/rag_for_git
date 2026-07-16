"""Integration-тесты SummaryStore на изолированном ParadeDB.

Инфраструктура запускается командой:
`docker compose --profile test up -d --wait paradedb-test`.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DIM = 1024


def _vec(hot: int) -> list[float]:
    """Орт-подобный 1024-вектор с единицей в позиции hot — для предсказуемого ANN."""
    v = [0.0] * DIM
    v[hot] = 1.0
    return v


@pytest.fixture()
def store():
    dsn = Settings().pg_dsn
    repo = f"test/summary-store/{uuid4().hex}"
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()  # создаёт subsystem_summaries (schema.sql)
    finally:
        schema_store.close()
    summary_store = SummaryStore(dsn)
    try:
        with summary_store._connect() as conn:
            conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
            conn.commit()
        yield summary_store, repo
    finally:
        try:
            with summary_store._connect() as conn:
                conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
                conn.commit()
        finally:
            summary_store.close()


def test_upsert_then_get_roundtrip(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "Индекс",
                                 "Хранилище чанков и ретрив.",
                                 ["reviewer/index/store.py#X"], "h1")
    assert summary_store.get_source_hashes(repo, "dev") == {"reviewer/index": "h1"}
    rows = summary_store.get_summaries(repo, "dev")
    assert len(rows) == 1
    row = rows[0]
    assert row["cluster_key"] == "reviewer/index"
    assert row["title"] == "Индекс"
    assert row["summary"] == "Хранилище чанков и ретрив."
    assert "T" in row["updated_at"]        # ISO-таймстамп (зеркало единичного get_summary)
    one = summary_store.get_summary(repo, "dev", "reviewer/index")
    assert one["member_node_ids"] == ["reviewer/index/store.py#X"]


def test_upsert_is_idempotent_update(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "old", [], "h1")
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "new", [], "h2")
    assert summary_store.get_source_hashes(repo, "dev") == {"reviewer/index": "h2"}
    assert summary_store.get_summaries(repo, "dev")[0]["summary"] == "new"


def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.store import ChunkStore, ChunkRow
    from reviewer.index.chunker import symbol_skeleton_hash

    repo = f"test/summary-store/{uuid4().hex}"
    cs = ChunkStore(Settings().pg_dsn)
    try:
        cs.init_schema()
        cs.clear(repo)
        cs.upsert([ChunkRow(repo=repo, ref="base:dev", content_hash="h",
                            path="reviewer/x/a.py", lang="python", symbol_fqn="A",
                            kind="function", start_line=3, end_line=9,
                            text="def a(): ...", embedding=[0.0] * 1024)])
        members = cs.list_base_members(repo, "dev")
        # 5-кортеж: skeleton_hash считается на лету из text
        assert ("reviewer/x/a.py", "A", "h", 3, symbol_skeleton_hash("def a(): ...")) in members
    finally:
        try:
            cs.clear(repo)
        finally:
            cs.close()


def test_get_updated_ats_returns_datetime_per_cluster(store):
    from datetime import datetime

    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    ats = summary_store.get_updated_ats(repo, "dev")
    assert "reviewer/index" in ats
    assert isinstance(ats["reviewer/index"], datetime)   # сырой datetime, не isoformat


def test_delete_summaries_except_prunes_orphans(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    summary_store.upsert_summary(repo, "dev", "reviewer/graph", "B", "s", [], "h2")
    summary_store.upsert_summary(repo, "dev", "reviewer/old", "C", "s", [], "h3")
    pruned = summary_store.delete_summaries_except(
        repo, "dev", ["reviewer/index", "reviewer/graph"]
    )
    assert pruned == 1                                   # удалён только reviewer/old
    assert set(summary_store.get_source_hashes(repo, "dev")) == {
        "reviewer/index", "reviewer/graph"
    }


def test_delete_summaries_except_empty_keep_deletes_all(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    pruned = summary_store.delete_summaries_except(repo, "dev", [])
    assert pruned == 1
    assert summary_store.get_source_hashes(repo, "dev") == {}


# ── PRI-167: embedding в SummaryStore + HNSW-индекс ──────────────────────────

@pytest.fixture()
def store_pri167():
    dsn = Settings().pg_dsn
    repo = f"test/summary-store/{uuid4().hex}"
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()  # создаёт таблицу + HNSW-индекс
    finally:
        schema_store.close()
    summary_store = SummaryStore(dsn)
    try:
        with summary_store._connect() as conn:
            conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
            conn.commit()
        yield summary_store, repo
    finally:
        try:
            with summary_store._connect() as conn:
                conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
                conn.commit()
        finally:
            summary_store.close()


def test_upsert_writes_embedding_and_search_returns_nearest_first(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "...",
                                 ["auth/a.py#A"], "h-auth", embedding=_vec(0))
    summary_store.upsert_summary(repo, "dev", "index", "Индекс", "...",
                                 ["index/b.py#B"], "h-index", embedding=_vec(500))
    hits = summary_store.search_summaries(repo, "dev", _vec(0), top_k=1)
    assert [h["cluster_key"] for h in hits] == ["auth"]
    assert summary_store.count_summaries(repo, "dev") == 2


def test_upsert_none_embedding_preserves_existing(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v1",
                                 ["auth/a.py#A"], "h1", embedding=_vec(0))
    # повторный upsert с embedding=None не должен обнулить вектор
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v2",
                                 ["auth/a.py#A"], "h1", embedding=None)
    hits = summary_store.search_summaries(repo, "dev", _vec(0), top_k=1)
    assert hits and hits[0]["cluster_key"] == "auth"
    assert hits[0]["summary"] == "v2"          # текст обновился, вектор сохранён


def test_pending_and_set_embedding_backfill(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "legacy", "Легаси", "...",
                                 [], "h-legacy", embedding=None)  # без вектора
    pending = summary_store.get_pending_embeddings(repo, "dev")
    assert [p["cluster_key"] for p in pending] == ["legacy"]
    summary_store.set_embedding(repo, "dev", "legacy", _vec(3))
    assert summary_store.get_pending_embeddings(repo, "dev") == []
    hits = summary_store.search_summaries(repo, "dev", _vec(3), top_k=1)
    assert hits[0]["cluster_key"] == "legacy"
