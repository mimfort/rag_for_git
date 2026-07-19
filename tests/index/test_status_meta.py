from uuid import uuid4

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


def _row(repo, ref, path, fqn):
    return ChunkRow(
        repo=repo,
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
    repo = f"test/status-meta-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        store.clear(repo)
        store.upsert(
            [
                _row(repo, "base:main", "a.py", "f"),
                _row(repo, "base:main", "b.py", "g"),
                _row(repo, "pr:7", "a.py", "f"),
            ]
        )
        store.set_index_meta(repo, "base:main", "cafe1234")
        assert store.count_chunks(repo, "base:main") == 2
        assert store.count_chunks(repo, "pr:7") == 1
        assert store.count_chunks(repo, "absent") == 0
        assert set(store.list_refs(repo)) == {"base:main", "pr:7"}
        row = store.get_index_meta_row(repo, "base:main")
        assert row is not None and row[0] == "cafe1234"
        assert store.get_index_meta_row(repo, "base:absent") is None
    finally:
        try:
            store.clear(repo)
        finally:
            try:
                with store._connect() as conn:
                    conn.execute("DELETE FROM index_meta WHERE repo=%s", (repo,))
                    conn.commit()
            finally:
                store.close()
