import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reviewer.index.store import ChunkStore


def _find_unsafe_chunk_store_clears(path: str, source: str) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and not node.args
            and not node.keywords
        ):
            continue
        receiver = node.func.value
        if (isinstance(receiver, ast.Name) and receiver.id == "store") or (
            isinstance(receiver, ast.Attribute) and receiver.attr == "store"
        ):
            offenders.append(f"{path}:{node.lineno}")
    return offenders


def test_ast_guard_finds_likely_chunk_store_receivers():
    source = """\
store.clear()
components.store.clear()
"""

    assert _find_unsafe_chunk_store_clears("tests/example.py", source) == [
        "tests/example.py:1",
        "tests/example.py:2",
    ]


def test_ast_guard_ignores_other_clear_receivers():
    source = """\
graph_store.clear()
svc._sessions.clear()
g.clear()
"""

    assert _find_unsafe_chunk_store_clears("tests/example.py", source) == []


def test_test_sources_do_not_call_chunk_store_clear_without_repo():
    root = Path(__file__).parents[2]
    offenders = []
    for source_path in sorted((root / "tests").rglob("*.py")):
        relative_path = source_path.relative_to(root).as_posix()
        offenders.extend(
            _find_unsafe_chunk_store_clears(relative_path, source_path.read_text())
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
        ChunkStore.clear(store)
