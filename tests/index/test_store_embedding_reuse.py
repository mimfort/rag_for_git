"""Cross-branch reuse векторов не зависит от типа, которым драйвер отдаёт vector.

`register_vector` из pgvector-python возвращает разные типы в разных версиях:
до 0.4 — numpy-массив, с 0.4 — `pgvector.vector.Vector`, у которого нет
`__iter__`. Зависимость в pyproject закреплена только снизу, поэтому обе формы
достижимы в проде, и `find_embeddings_by_hashes` обязана нормализовать любую.

Тест unit-уровня намеренно: баг воспроизводится на реальной БД, но integration
маркер вывел бы его из обычного прогона — а именно обычный прогон должен ловить
регресс, ломающий каждый `prepare_review`.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from pgvector import Vector

from reviewer.index.store import ChunkStore


def _store_returning(rows: list[tuple]) -> ChunkStore:
    store = ChunkStore("postgresql://unused/unused")
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows

    @contextmanager
    def _connect():
        yield conn

    store._connect = _connect  # type: ignore[method-assign]
    return store


def test_find_embeddings_normalizes_pgvector_vector():
    """`Vector` (pgvector >= 0.4) конвертируется в list[float], а не падает."""
    store = _store_returning([("hash-a", Vector([0.5, -0.25, 1.0]))])

    result = store.find_embeddings_by_hashes("owner/name", ["hash-a"])

    assert result == {"hash-a": [0.5, -0.25, 1.0]}
    assert isinstance(result["hash-a"], list)
    assert all(isinstance(x, float) for x in result["hash-a"])


def test_find_embeddings_accepts_numpy_array():
    """numpy-массив (pgvector < 0.4) продолжает работать."""
    numpy = pytest.importorskip("numpy")
    store = _store_returning([("hash-b", numpy.array([1.0, 2.0], dtype=numpy.float32))])

    result = store.find_embeddings_by_hashes("owner/name", ["hash-b"])

    assert result == {"hash-b": [1.0, 2.0]}
    assert isinstance(result["hash-b"], list)


def test_find_embeddings_accepts_plain_list():
    """Обычный список проходит без изменений."""
    store = _store_returning([("hash-c", [3.0, 4.0])])

    assert store.find_embeddings_by_hashes("owner/name", ["hash-c"]) == {
        "hash-c": [3.0, 4.0]
    }


def test_find_embeddings_short_circuits_without_hashes():
    """Пустой список хэшей не ходит в БД."""
    store = ChunkStore("postgresql://unused/unused")
    store._connect = MagicMock(side_effect=AssertionError("не должно быть запроса"))

    assert store.find_embeddings_by_hashes("owner/name", []) == {}


def test_vector_roundtrip_accepts_faithful_driver():
    """Драйвер, вернувший вектор без искажений, проверку проходит."""
    store = ChunkStore("postgresql://unused/unused")
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (
        Vector(ChunkStore._PROBE_VECTOR),
    )

    @contextmanager
    def _connect():
        yield conn

    store._connect = _connect  # type: ignore[method-assign]

    store.check_vector_roundtrip()  # не поднимает


def test_vector_roundtrip_rejects_distorted_value():
    """Искажённый вектор — отказ, а не молчаливое согласие."""
    store = ChunkStore("postgresql://unused/unused")
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ([0.0, 0.0, 0.0],)

    @contextmanager
    def _connect():
        yield conn

    store._connect = _connect  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="искажённ"):
        store.check_vector_roundtrip()
