"""Integration-тест ChunkStore.list_overlay_refs (нужен поднятый Postgres).

ВАЖНО: этот тест работает на разделяемой базе, где могут лежать чужие данные
(base-индексы других репозиториев, оставленные reviewer index). list_overlay_refs()
глобален по дизайну (см. store.py), поэтому тест НЕ имеет права на store.clear() без
аргумента (TRUNCATE всей таблицы chunks) — используем только store.clear(repo) со
скоупом по своим repo-идентификаторам и убираем за собой в finally.
"""
import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore

REPO_A = "gc-test/a"
REPO_B = "gc-test/b"


def _row(ref, path, fqn, vec, repo):
    return ChunkRow(repo=repo, ref=ref, content_hash=fqn + ref + repo, path=path,
                    lang="python", symbol_fqn=fqn, kind="function", start_line=1,
                    end_line=2, text="def f(): pass", embedding=vec)


@pytest.mark.integration
def test_list_overlay_refs_returns_pr_refs_across_repos_and_skips_base():
    """Возвращает overlay своих репо среди прочих; base:* не возвращает никогда."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    # Скоупим очистку только своими repo-идентификаторами — на базе есть чужие данные.
    store.clear(REPO_A)
    store.clear(REPO_B)
    try:
        vec = [0.0] * s.embedding_dim
        store.upsert([
            _row("base:main", "x.py", "f_x", vec, repo=REPO_A),
            _row("pr:42", "x.py", "f_x", vec, repo=REPO_A),
            _row("pr:7", "y.py", "f_y", vec, repo=REPO_B),
        ])

        refs = store.list_overlay_refs()

        # Метод видит overlay разных репозиториев (не скоупится одним repo).
        assert (REPO_A, "pr:42") in refs
        assert (REPO_B, "pr:7") in refs
        # base:* не возвращается никогда — глобально верное свойство метода,
        # поэтому проверка корректна и при наличии чужих base-рефов в базе.
        assert all(not ref.startswith("base:") for _, ref in refs)
    finally:
        store.clear(REPO_A)
        store.clear(REPO_B)
        store.close()
