import psycopg
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow


def _row(ref, path, fqn):
    return ChunkRow(repo="a/x", ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


@pytest.mark.integration
def test_migrate_legacy_base_to_primary():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    store.upsert([_row("base", "a.py", "f")])
    store.set_index_meta("a/x", "base", "deadbeef")
    store.migrate_legacy_base("main")
    with psycopg.connect(s.pg_dsn) as conn:
        refs = {r[0] for r in conn.execute("SELECT DISTINCT ref FROM chunks").fetchall()}
        meta = conn.execute("SELECT ref FROM index_meta WHERE repo='a/x'").fetchone()
    assert refs == {"base:main"}
    assert meta == ("base:main",)
