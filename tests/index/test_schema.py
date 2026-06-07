import psycopg, pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore

@pytest.mark.integration
def test_init_schema_creates_table_and_indexes():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    with psycopg.connect(s.pg_dsn) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='chunks'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert {"chunks_bm25", "chunks_hnsw"} <= names
