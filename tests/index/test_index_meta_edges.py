"""Персист счётчиков рёбер графа в index_meta (PRI-252)."""
from uuid import uuid4

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore


@pytest.mark.integration
def test_graph_edge_counts_round_trip():
    settings = Settings()
    repo = f"test/edge-counts-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        # Замера ещё не было — сравнивать не с чем.
        assert store.get_graph_edge_counts(repo, "base:main") is None

        store.set_index_meta(repo, "base:main", "cafe1234")
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 17963, "IMPLEMENTS": 129})
        assert store.get_graph_edge_counts(repo, "base:main") == \
            {"CALLS": 17963, "IMPLEMENTS": 129}

        # Повторная запись перетирает предыдущий замер.
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 10})
        assert store.get_graph_edge_counts(repo, "base:main") == {"CALLS": 10}

        # SHA при этом не теряется.
        row = store.get_index_meta_row(repo, "base:main")
        assert row is not None and row[0] == "cafe1234"

        # Другая ветка того же репо изолирована.
        assert store.get_graph_edge_counts(repo, "base:dev") is None
    finally:
        store.clear(repo)
        store.close()


@pytest.mark.integration
def test_init_schema_is_idempotent_for_edge_counts():
    """Повторный init_schema не ломает уже записанные счётчики."""
    settings = Settings()
    repo = f"test/edge-counts-idem-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        store.set_index_meta(repo, "base:main", "cafe1234")
        store.set_graph_edge_counts(repo, "base:main", {"CALLS": 5})
        store.init_schema()
        assert store.get_graph_edge_counts(repo, "base:main") == {"CALLS": 5}
    finally:
        store.clear(repo)
        store.close()
