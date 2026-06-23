import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DSN = Settings().pg_dsn


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
