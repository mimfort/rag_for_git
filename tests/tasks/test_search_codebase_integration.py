"""Integration: search_codebase (Retriever.search_base) на живом Postgres, без Voyage.

Добавляет base-чанк на ТЕСТОВОМ пути с маркер-словом, ищет его (BM25-хит), затем удаляет
только этот путь — не трогает реальный base-индекс. Маркер integration.
"""
from __future__ import annotations

import hashlib

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore
from reviewer.retrieval.retriever import Retriever

pytestmark = pytest.mark.integration

_TEST_PATH = "__solve_task_fixture__.py"
_MARKER = "zzsolvetaskmarker"


def _vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    return [((h[i % len(h)] + i) % 17) / 17.0 for i in range(1024)]


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


def test_search_codebase_finds_base_chunk():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    emb = _FakeEmbedder()
    text = f"def logout(session):\n    session.clear()  # {_MARKER}"
    try:
        store.delete_paths("t/x", "base", [_TEST_PATH])  # гигиена от прошлых прогонов
        store.upsert([ChunkRow(
            repo="t/x", ref="base", content_hash="h_solve", path=_TEST_PATH, lang="python",
            symbol_fqn="logout", kind="function", start_line=1, end_line=2,
            text=text, embedding=emb.embed_documents([text])[0])])

        r = Retriever(store, graph=None, embedder=emb, reranker=None, max_context_chars=8000)
        ctx = r.search_base("t/x", _MARKER, top_k=5).as_context()
        assert f"{_TEST_PATH}#logout" in ctx
    finally:
        store.delete_paths("t/x", "base", [_TEST_PATH])
        store.close()
