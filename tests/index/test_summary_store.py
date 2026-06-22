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
