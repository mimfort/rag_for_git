from unittest.mock import MagicMock

import pytest

from reviewer.app import Components, build_components
from reviewer.config.settings import Settings
from reviewer.tasks.sync import SyncProvider, SyncService


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


class _Closeable:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.close_calls = 0
        self.driver = object()

    def close(self):
        self.close_calls += 1
        self.events.append(self.name)


def test_components_close_releases_owned_providers_before_shared_stores_once():
    events = []
    store = _Closeable("store", events)
    graph = _Closeable("graph", events)
    task_store = _Closeable("task_store", events)
    operation_store = _Closeable("operation_store", events)
    summary_store = _Closeable("summary_store", events)
    provider = _Closeable("provider", events)
    sync_service = SyncService(
        [SyncProvider(provider, owned=True)],
        MagicMock(),
        store,
    )
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
        sync_service=sync_service,
        summary_store=summary_store,
    )

    components.close()

    assert events == [
        "summary_store",
        "provider",
        "operation_store",
        "task_store",
        "graph",
        "store",
    ]
    assert store.close_calls == 1
    assert provider.close_calls == 1


def _patch_build_resources(monkeypatch):
    import reviewer.app as app_module

    events = []
    resources = {}

    def factory(name):
        def build(*args, **kwargs):
            resource = _Closeable(name, events)
            resources[name] = resource
            return resource

        return build

    monkeypatch.setattr(app_module, "ChunkStore", factory("store"))
    monkeypatch.setattr(app_module, "GraphStore", factory("graph"))
    monkeypatch.setattr(app_module, "TaskStore", factory("task_store"))
    monkeypatch.setattr(
        app_module,
        "SubtaskOperationStore",
        factory("operation_store"),
    )
    monkeypatch.setattr(app_module, "SummaryStore", factory("summary_store"))
    monkeypatch.setattr(app_module, "_voyage_client", lambda settings: object())
    provider = _Closeable("provider", events)
    provider.board_type = "yougile"
    monkeypatch.setattr(app_module, "make_board_providers", lambda *args, **kwargs: [provider])
    registry = MagicMock()
    registry.get.return_value = MagicMock()
    monkeypatch.setattr(app_module, "default_board_registry", lambda: registry)
    credentials = MagicMock()
    credentials.secret_values.return_value = frozenset()
    monkeypatch.setattr(
        app_module.ProviderCredentialSource,
        "from_settings",
        lambda settings: credentials,
    )
    return app_module, events, resources


@pytest.mark.parametrize(
    ("failure_point", "expected_close_order"),
    [
        ("after_graph", ["graph", "store"]),
        (
            "after_providers",
            ["provider", "operation_store", "task_store", "graph", "store"],
        ),
        (
            "after_summary",
            [
                "summary_store",
                "provider",
                "operation_store",
                "task_store",
                "graph",
                "store",
            ],
        ),
    ],
)
def test_build_components_closes_every_created_resource_on_failure(
    monkeypatch,
    failure_point,
    expected_close_order,
):
    app_module, events, _ = _patch_build_resources(monkeypatch)
    if failure_point == "after_graph":
        monkeypatch.setattr(
            app_module,
            "Retriever",
            MagicMock(side_effect=RuntimeError("after graph")),
        )
    elif failure_point == "after_providers":
        monkeypatch.setattr(
            app_module,
            "SummaryStore",
            MagicMock(side_effect=RuntimeError("after providers")),
        )
    else:
        monkeypatch.setattr(
            app_module,
            "Components",
            MagicMock(side_effect=RuntimeError("after summary")),
        )

    with pytest.raises(RuntimeError, match=failure_point.replace("_", " ")):
        app_module.build_components(Settings(_env_file=None))

    assert events == expected_close_order
