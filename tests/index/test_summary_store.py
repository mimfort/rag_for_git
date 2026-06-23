"""Integration-тесты SummaryStore (PRI-167): требуют Postgres/pgvector на PG_DSN."""
from __future__ import annotations

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DSN = Settings().pg_dsn

DIM = 1024


def _vec(hot: int) -> list[float]:
    """Орт-подобный 1024-вектор с единицей в позиции hot — для предсказуемого ANN."""
    v = [0.0] * DIM
    v[hot] = 1.0
    return v


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
    assert len(rows) == 1
    row = rows[0]
    assert row["cluster_key"] == "reviewer/index"
    assert row["title"] == "Индекс"
    assert row["summary"] == "Хранилище чанков и ретрив."
    assert "T" in row["updated_at"]        # ISO-таймстамп (зеркало единичного get_summary)
    one = store.get_summary("t/t", "dev", "reviewer/index")
    assert one["member_node_ids"] == ["reviewer/index/store.py#X"]


def test_upsert_is_idempotent_update(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "old", [], "h1")
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "new", [], "h2")
    assert store.get_source_hashes("t/t", "dev") == {"reviewer/index": "h2"}
    assert store.get_summaries("t/t", "dev")[0]["summary"] == "new"


def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.store import ChunkStore, ChunkRow
    from reviewer.index.chunker import symbol_skeleton_hash
    cs = ChunkStore(DSN)
    cs.init_schema()
    cs.upsert([ChunkRow(repo="t/t", ref="base:dev", content_hash="h", path="reviewer/x/a.py",
                        lang="python", symbol_fqn="A", kind="function",
                        start_line=3, end_line=9, text="def a(): ...", embedding=[0.0]*1024)])
    try:
        members = cs.list_base_members("t/t", "dev")
        # 5-кортеж: skeleton_hash считается на лету из text
        assert ("reviewer/x/a.py", "A", "h", 3, symbol_skeleton_hash("def a(): ...")) in members
    finally:
        cs.delete_ref("t/t", "base:dev")
        cs.close()


def test_get_updated_ats_returns_datetime_per_cluster(store):
    from datetime import datetime
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    ats = store.get_updated_ats("t/t", "dev")
    assert "reviewer/index" in ats
    assert isinstance(ats["reviewer/index"], datetime)   # сырой datetime, не isoformat


def test_delete_summaries_except_prunes_orphans(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    store.upsert_summary("t/t", "dev", "reviewer/graph", "B", "s", [], "h2")
    store.upsert_summary("t/t", "dev", "reviewer/old", "C", "s", [], "h3")
    pruned = store.delete_summaries_except("t/t", "dev", ["reviewer/index", "reviewer/graph"])
    assert pruned == 1                                   # удалён только reviewer/old
    assert set(store.get_source_hashes("t/t", "dev")) == {"reviewer/index", "reviewer/graph"}


def test_delete_summaries_except_empty_keep_deletes_all(store):
    store.upsert_summary("t/t", "dev", "reviewer/index", "A", "s", [], "h1")
    pruned = store.delete_summaries_except("t/t", "dev", [])
    assert pruned == 1
    assert store.get_source_hashes("t/t", "dev") == {}


# ── PRI-167: embedding в SummaryStore + HNSW-индекс ──────────────────────────

@pytest.fixture()
def store_pri167():
    dsn = Settings().pg_dsn
    ChunkStore(dsn).init_schema()                     # создаёт таблицу + HNSW-индекс
    st = SummaryStore(dsn)
    # чистим тестовый repo до и после
    with st._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", ("test/pri167",))
        conn.commit()
    yield st
    with st._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", ("test/pri167",))
        conn.commit()
    st.close()


def test_upsert_writes_embedding_and_search_returns_nearest_first(store_pri167):
    store_pri167.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "...",
                                ["auth/a.py#A"], "h-auth", embedding=_vec(0))
    store_pri167.upsert_summary("test/pri167", "dev", "index", "Индекс", "...",
                                ["index/b.py#B"], "h-index", embedding=_vec(500))
    hits = store_pri167.search_summaries("test/pri167", "dev", _vec(0), top_k=1)
    assert [h["cluster_key"] for h in hits] == ["auth"]
    assert store_pri167.count_summaries("test/pri167", "dev") == 2


def test_upsert_none_embedding_preserves_existing(store_pri167):
    store_pri167.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "v1",
                                ["auth/a.py#A"], "h1", embedding=_vec(0))
    # повторный upsert с embedding=None не должен обнулить вектор
    store_pri167.upsert_summary("test/pri167", "dev", "auth", "Авторизация", "v2",
                                ["auth/a.py#A"], "h1", embedding=None)
    hits = store_pri167.search_summaries("test/pri167", "dev", _vec(0), top_k=1)
    assert hits and hits[0]["cluster_key"] == "auth"
    assert hits[0]["summary"] == "v2"          # текст обновился, вектор сохранён


def test_pending_and_set_embedding_backfill(store_pri167):
    store_pri167.upsert_summary("test/pri167", "dev", "legacy", "Легаси", "...",
                                [], "h-legacy", embedding=None)   # без вектора
    pending = store_pri167.get_pending_embeddings("test/pri167", "dev")
    assert [p["cluster_key"] for p in pending] == ["legacy"]
    store_pri167.set_embedding("test/pri167", "dev", "legacy", _vec(3))
    assert store_pri167.get_pending_embeddings("test/pri167", "dev") == []
    hits = store_pri167.search_summaries("test/pri167", "dev", _vec(3), top_k=1)
    assert hits[0]["cluster_key"] == "legacy"
