"""Персист счётчиков рёбер графа в index_meta (PRI-252)."""
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

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


@pytest.mark.integration
def test_get_graph_edge_counts_returns_none_on_undefined_column():
    """Ветка ``except (UndefinedTable, UndefinedColumn)`` в get_graph_edge_counts:
    индекс, построенный версией до PRI-252, имеет таблицу ``index_meta`` БЕЗ
    колонки ``graph_edges`` — чтение обязано отдать None, а не упасть.

    Боевую таблицу ``index_meta`` общей БД не трогаем: подкладываем отдельную
    временную схему с одноимённой таблицей без колонки graph_edges и шадовим
    её только для соединений ЭТОГО store через ``search_path`` (libpq-опция
    ``-c search_path=...``) — unqualified ``index_meta`` в запросе резолвится
    в неё, публичная таблица не задета вообще. Схема дропается в finally
    независимо от исхода теста.
    """
    settings = Settings()
    schema = f"test_edge_counts_shadow_{uuid4().hex}"
    admin_conn = psycopg.connect(settings.pg_dsn, autocommit=True)
    try:
        admin_conn.execute(f'CREATE SCHEMA "{schema}"')
        admin_conn.execute(
            f'CREATE TABLE "{schema}".index_meta '
            "(repo text, ref text, sha text, PRIMARY KEY (repo, ref))"
        )
        shadow_dsn = make_conninfo(settings.pg_dsn, options=f"-c search_path={schema},public")
        store = ChunkStore(shadow_dsn)
        try:
            assert store.get_graph_edge_counts("test/shadow-schema", "base:main") is None
        finally:
            store.close()
    finally:
        admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin_conn.close()
