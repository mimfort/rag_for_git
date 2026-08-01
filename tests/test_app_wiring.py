from unittest.mock import MagicMock

import pytest

from reviewer.app import Components, build_components
from reviewer.config.settings import Settings


def test_build_components_returns_retriever(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.retriever is not None


def test_build_components_wires_task_components(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.task_store is not None
    assert c.task_service is not None
    assert c.task_graph is None  # connect=False → graph None → task_graph None
    assert c.subtask_operation_store is not None
    assert c.subtask_service is not None
    assert c.subtask_operation_store._pool is None


def test_build_components_wires_summary_store(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.summary_store is not None


def test_components_close_closes_operation_store_once_with_other_stores():
    store = MagicMock()
    task_store = MagicMock()
    operation_store = MagicMock()
    summary_store = MagicMock()
    graph = MagicMock()
    components = Components(
        settings=MagicMock(),
        store=store,
        graph=graph,
        embedder=MagicMock(),
        reranker=MagicMock(),
        retriever=MagicMock(),
        task_store=task_store,
        task_graph=MagicMock(),
        task_service=MagicMock(),
        subtask_operation_store=operation_store,
        subtask_service=MagicMock(),
        sync_service=None,
        summary_store=summary_store,
    )

    components.close()

    store.close.assert_called_once_with()
    graph.close.assert_called_once_with()
    task_store.close.assert_called_once_with()
    operation_store.close.assert_called_once_with()
    summary_store.close.assert_called_once_with()


def test_components_close_attempts_every_store_when_one_close_fails():
    store = MagicMock()
    store.close.side_effect = RuntimeError("close failed")
    operation_store = MagicMock()
    components = Components(
        settings=MagicMock(),
        store=store,
        graph=MagicMock(),
        embedder=MagicMock(),
        reranker=MagicMock(),
        retriever=MagicMock(),
        task_store=MagicMock(),
        task_graph=MagicMock(),
        task_service=MagicMock(),
        subtask_operation_store=operation_store,
        subtask_service=MagicMock(),
        sync_service=None,
        summary_store=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="close failed"):
        components.close()

    operation_store.close.assert_called_once_with()
