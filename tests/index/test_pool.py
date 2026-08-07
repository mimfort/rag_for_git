"""Unit-тесты пула соединений ChunkStore.

Не требуют реального Postgres — пул мокается.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reviewer.index.store import ChunkStore
from reviewer.tasks.store import TaskRow, TaskStore


def test_chunk_store_creates_pool_lazily_with_configure():
    """Пул создаётся только при первом обращении и с правильными параметрами."""
    with patch("reviewer.index.store.ConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool

        store = ChunkStore(
            "postgresql://u@localhost/db",
            min_size=2,
            max_size=8,
        )
        # До первого использования пул не создаётся
        mock_pool_cls.assert_not_called()

        store._ensure_pool()

        mock_pool_cls.assert_called_once()
        _, kwargs = mock_pool_cls.call_args
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 8
        assert kwargs["open"] is False
        assert "configure" in kwargs
        mock_pool.open.assert_called_once()


def test_chunk_store_configure_registers_vector():
    """configure-колбэк регистрирует pgvector на новом соединении."""
    with (patch("reviewer.index.store.ConnectionPool") as mock_pool_cls,
          patch("reviewer.index.store.register_vector") as mock_reg):
        store = ChunkStore("postgresql://u@localhost/db")
        store._ensure_pool()
        configure = mock_pool_cls.call_args.kwargs["configure"]
        conn = MagicMock()
        configure(conn)
        mock_reg.assert_called_once_with(conn)


def test_chunk_store_close_closes_pool():
    """close() закрывает созданный пул и сбрасывает ссылку."""
    with patch("reviewer.index.store.ConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool

        store = ChunkStore("postgresql://u@localhost/db")
        store._ensure_pool()
        store.close()

        mock_pool.close.assert_called_once()
        assert store._pool is None


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.calls = []
        self.next_row = (1,)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result(self.next_row)

    def commit(self):
        pass


class _Pool:
    def __init__(self):
        self.conn = _Connection()
        self.closed = False

    def connection(self):
        return self.conn

    def close(self):
        self.closed = True


def test_task_store_lazily_migrates_links_once_and_resets_on_close():
    store = TaskStore("postgresql://unused")
    pool = _Pool()
    store._pool = pool

    assert store._links_schema_lock is not store._init_lock
    assert store.update_links("ID-1", []) is True
    assert store.update_links("ID-1", [{"key": "ID-2"}]) is True

    migrations = [sql for sql, _ in pool.conn.calls
                  if "ADD COLUMN IF NOT EXISTS links" in sql]
    assert len(migrations) == 1
    store.close()
    assert pool.closed is True
    assert store._links_schema_ready is False


def test_task_store_upsert_uses_none_as_links_omission_sentinel():
    store = TaskStore("postgresql://unused")
    pool = _Pool()
    store._pool = pool
    base = {"key": "ID-1", "aliases": [], "title": "T", "description": "d",
            "status": None, "url": None, "content_hash": "h", "text": "T\n\nd",
            "embedding": []}

    store.upsert_task(TaskRow(**base))
    store.upsert_task(TaskRow(**base, links=[]))

    upserts = [(sql, params) for sql, params in pool.conn.calls
               if "INSERT INTO tasks" in sql]
    assert upserts[0][1]["links_supplied"] is False
    assert upserts[0][1]["links"] == "[]"
    assert upserts[1][1]["links_supplied"] is True
    assert "WHEN %(links_supplied)s THEN EXCLUDED.links ELSE tasks.links" in upserts[0][0]
