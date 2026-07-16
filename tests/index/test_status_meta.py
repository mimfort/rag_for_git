import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


_REPO = "test/status-meta"


def _row(ref, path, fqn):
    return ChunkRow(
        repo=_REPO,
        ref=ref,
        content_hash=fqn,
        path=path,
        lang="python",
        symbol_fqn=fqn,
        kind="function",
        start_line=1,
        end_line=2,
        text="code",
        embedding=[0.0] * 1024,
    )


@pytest.mark.integration
def test_count_chunks_list_refs_meta_row():
    settings = Settings()
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        store.clear(_REPO)
        store.upsert(
            [
                _row("base:main", "a.py", "f"),
                _row("base:main", "b.py", "g"),
                _row("pr:7", "a.py", "f"),
            ]
        )
        store.set_index_meta(_REPO, "base:main", "cafe1234")
        assert store.count_chunks(_REPO, "base:main") == 2
        assert store.count_chunks(_REPO, "pr:7") == 1
        assert store.count_chunks(_REPO, "absent") == 0
        assert set(store.list_refs(_REPO)) == {"base:main", "pr:7"}
        row = store.get_index_meta_row(_REPO, "base:main")
        assert row is not None and row[0] == "cafe1234"
        assert store.get_index_meta_row(_REPO, "base:absent") is None
    finally:
        try:
            store.clear(_REPO)
        finally:
            try:
                with store._connect() as conn:
                    conn.execute("DELETE FROM index_meta WHERE repo=%s", (_REPO,))
                    conn.commit()
            finally:
                store.close()
