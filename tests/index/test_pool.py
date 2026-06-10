"""Unit-тесты пула соединений ChunkStore.

Не требуют реального Postgres — пул мокается.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reviewer.index.store import ChunkStore


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
    with patch("reviewer.index.store.ConnectionPool") as mock_pool_cls:
        with patch("reviewer.index.store.register_vector") as mock_reg:
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
