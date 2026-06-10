"""Unit-тесты пула соединений ReviewHistory.

Не требуют реального Postgres — пул мокается.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reviewer.web.history import ReviewHistory


def test_history_creates_pool_lazily_with_sizes():
    """Пул создаётся лениво с переданными min_size / max_size."""
    with patch("reviewer.web.history.ConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool

        history = ReviewHistory(
            "postgresql://u@localhost/db",
            min_size=1,
            max_size=4,
        )
        mock_pool_cls.assert_not_called()

        history._ensure_pool()

        mock_pool_cls.assert_called_once()
        _, kwargs = mock_pool_cls.call_args
        assert kwargs["min_size"] == 1
        assert kwargs["max_size"] == 4
        assert kwargs["open"] is False
        mock_pool.open.assert_called_once()


def test_history_close_closes_pool():
    """close() закрывает пул и сбрасывает внутреннюю ссылку."""
    with patch("reviewer.web.history.ConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool

        history = ReviewHistory("postgresql://u@localhost/db")
        history._ensure_pool()
        history.close()

        mock_pool.close.assert_called_once()
        assert history._pool is None
