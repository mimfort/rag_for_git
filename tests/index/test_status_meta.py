import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow


def _row(ref, path, fqn):
    return ChunkRow(repo="a/x", ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


@pytest.mark.integration
def test_count_chunks_list_refs_meta_row():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear("a/x")
    store.upsert([_row("base:main", "a.py", "f"),
                  _row("base:main", "b.py", "g"),
                  _row("pr:7", "a.py", "f")])
    store.set_index_meta("a/x", "base:main", "cafe1234")
    try:
        assert store.count_chunks("a/x", "base:main") == 2
        assert store.count_chunks("a/x", "pr:7") == 1
        assert store.count_chunks("a/x", "absent") == 0
        assert set(store.list_refs("a/x")) == {"base:main", "pr:7"}
        row = store.get_index_meta_row("a/x", "base:main")
        assert row is not None and row[0] == "cafe1234"
        assert store.get_index_meta_row("a/x", "base:absent") is None
    finally:
        store.clear("a/x")
        store.close()
