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
