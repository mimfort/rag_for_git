from unittest.mock import MagicMock

import pytest

from reviewer.index.store import ChunkStore


def test_clear_deletes_only_requested_repo_and_commits():
    store = ChunkStore("postgresql://u@localhost/db")
    conn = MagicMock()
    connection_context = MagicMock()
    connection_context.__enter__.return_value = conn
    store._connect = MagicMock(return_value=connection_context)

    store.clear("test/repo")

    conn.execute.assert_called_once_with(
        "DELETE FROM chunks WHERE repo = %s",
        ("test/repo",),
    )
    conn.commit.assert_called_once_with()


def test_clear_requires_repo():
    store = ChunkStore("postgresql://u@localhost/db")

    with pytest.raises(TypeError):
        store.clear()
