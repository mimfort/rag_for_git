import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reviewer.index.store import ChunkStore


_INTEGRATION_SOURCES = (
    "tests/index/test_store_hybrid.py",
    "tests/index/test_migrate_base.py",
    "tests/integration/test_pipeline.py",
)


def test_integration_chunk_store_clear_calls_are_repo_scoped():
    root = Path(__file__).parents[2]
    offenders = []
    for relative_path in _INTEGRATION_SOURCES:
        source = (root / relative_path).read_text()
        tree = ast.parse(source)
        offenders.extend(
            f"{relative_path}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and not node.args
            and not node.keywords
        )

    assert offenders == []


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
