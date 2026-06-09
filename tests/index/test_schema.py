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


@pytest.mark.integration
def test_init_schema_creates_index_meta_table():
    """init_schema создаёт таблицу index_meta (идемпотентно)."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.init_schema()  # второй вызов — idempotent
    with psycopg.connect(s.pg_dsn) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()}
    assert "index_meta" in tables


@pytest.mark.integration
def test_index_meta_get_none_before_set():
    """get_index_meta возвращает None, если запись отсутствует."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    with psycopg.connect(s.pg_dsn) as conn:
        conn.execute("DELETE FROM index_meta WHERE ref = 'test-ref-unit'")
        conn.commit()
    assert store.get_index_meta("test-ref-unit") is None


@pytest.mark.integration
def test_index_meta_set_then_get():
    """set_index_meta записывает sha; get_index_meta его возвращает."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    with psycopg.connect(s.pg_dsn) as conn:
        conn.execute("DELETE FROM index_meta WHERE ref = 'test-ref-unit'")
        conn.commit()
    store.set_index_meta("test-ref-unit", "abc123")
    assert store.get_index_meta("test-ref-unit") == "abc123"


@pytest.mark.integration
def test_index_meta_upsert_updates_sha():
    """Повторный set_index_meta обновляет sha (UPSERT)."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    with psycopg.connect(s.pg_dsn) as conn:
        conn.execute("DELETE FROM index_meta WHERE ref = 'test-ref-unit'")
        conn.commit()
    store.set_index_meta("test-ref-unit", "first-sha")
    store.set_index_meta("test-ref-unit", "second-sha")
    assert store.get_index_meta("test-ref-unit") == "second-sha"
