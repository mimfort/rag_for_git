from reviewer.app import build_components
from reviewer.config.settings import Settings

def test_build_components_returns_retriever(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.retriever is not None
