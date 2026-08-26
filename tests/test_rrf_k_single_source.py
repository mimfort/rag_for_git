"""Guard: константа RRF объявлена ровно один раз (PRI-267).

Значение k доезжает до обоих SQL именованным параметром из reviewer/rrf.py.
Тест смотрит на ФАКТИЧЕСКИ переданные драйверу sql и params, а не на текст
исходника: проверка по подстроке ловила бы форматирование, а не значение
(тот же урок, что в докстринге tests/metrics/test_reexport_guard.py).

Соединение мокается — тесту не нужны ни Postgres, ни сокет.
"""
from __future__ import annotations

import inspect
import re
from unittest.mock import patch

from reviewer.index.store import ChunkStore
from reviewer.retrieval import multiquery
from reviewer.rrf import RRF_K
from reviewer.tasks.store import TaskStore

# Числовой литерал в знаменателе RRF — ровно то, что задача убрала.
# После правки перед "+ rank" стоит каст "::int", а не цифра.
_LITERAL = re.compile(r"\d+\s*\+\s*rank")
_PLACEHOLDER = "%(rrf_k)s"


class _Cursor:
    """Курсор-заглушка: запросу нечего вернуть, важен сам факт вызова."""

    @staticmethod
    def fetchall() -> list:
        return []


class _Connection:
    """Соединение-заглушка, запоминающее переданные sql и params."""

    def __init__(self, captured: list[tuple[str, dict]]) -> None:
        self._captured = captured

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: dict) -> _Cursor:
        self._captured.append((sql, params))
        return _Cursor()


def _capture_chunk_search() -> tuple[str, dict]:
    """Вызвать ChunkStore.hybrid_search и перехватить запрос."""
    captured: list[tuple[str, dict]] = []
    store = ChunkStore("postgresql://unused")     # пул ленивый, соединения не будет
    with patch.object(store, "_connect", return_value=_Connection(captured)):
        store.hybrid_search("owner/name", "запрос", [0.0] * 8, "pr:1", [],
                            base_ref="base:dev")
    assert len(captured) == 1
    return captured[0]


def _capture_task_search() -> tuple[str, dict]:
    """Вызвать TaskStore.search и перехватить запрос."""
    captured: list[tuple[str, dict]] = []
    store = TaskStore("postgresql://unused")
    with patch.object(store, "_connect", return_value=_Connection(captured)):
        store.search("запрос", [0.0] * 8)
    assert len(captured) == 1
    return captured[0]


def test_chunk_store_passes_rrf_k_as_parameter():
    """hybrid_search берёт k параметром из reviewer.rrf, а не литералом."""
    sql, params = _capture_chunk_search()
    assert params["rrf_k"] == RRF_K
    # Ровно две ветки CTE (bm25 и ann): одна подставленная и одна забытая
    # разъехались бы молча — это и есть чинимый дефект.
    assert sql.count(_PLACEHOLDER) == 2
    assert not _LITERAL.search(sql)


def test_task_store_passes_rrf_k_as_parameter():
    """TaskStore.search берёт k параметром из reviewer.rrf, а не литералом."""
    sql, params = _capture_task_search()
    assert params["rrf_k"] == RRF_K
    assert sql.count(_PLACEHOLDER) == 2
    assert not _LITERAL.search(sql)


def test_multiquery_does_not_redeclare_rrf_k():
    """Второго объявления нет: multiquery импортирует значение, а не задаёт своё."""
    source = inspect.getsource(multiquery)
    assert not re.search(r"^RRF_K\s*=\s*\d", source, re.M)
    assert multiquery.RRF_K == RRF_K
