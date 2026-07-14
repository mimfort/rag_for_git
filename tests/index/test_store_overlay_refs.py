"""Integration-тест ChunkStore.list_overlay_refs (нужен поднятый Postgres)."""
import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


def _row(ref, path, fqn, vec, repo="a/x"):
    return ChunkRow(repo=repo, ref=ref, content_hash=fqn + ref + repo, path=path,
                    lang="python", symbol_fqn=fqn, kind="function", start_line=1,
                    end_line=2, text="def f(): pass", embedding=vec)


@pytest.mark.integration
def test_list_overlay_refs_returns_pr_refs_across_repos_and_skips_base():
    """Возвращает overlay всех репо; base:* не возвращает никогда."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    vec = [0.0] * s.embedding_dim
    store.upsert([
        _row("base:main", "x.py", "f_x", vec, repo="a/x"),
        _row("pr:42", "x.py", "f_x", vec, repo="a/x"),
        _row("pr:7", "y.py", "f_y", vec, repo="b/y"),
    ])

    assert store.list_overlay_refs() == [("a/x", "pr:42"), ("b/y", "pr:7")]
    store.close()
