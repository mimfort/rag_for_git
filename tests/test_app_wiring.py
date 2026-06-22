from reviewer.app import build_components
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


def test_build_components_wires_summary_store(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.summary_store is not None
