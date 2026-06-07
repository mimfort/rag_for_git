from reviewer.app import build_components
from reviewer.config.settings import Settings

def test_build_components_returns_retriever_and_llm(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    c = build_components(Settings(), connect=False)
    assert c.retriever is not None
    assert c.llm_provider is not None
