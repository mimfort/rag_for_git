from reviewer.config.settings import Settings
from reviewer.llm.openrouter import OpenRouterProvider

def test_extra_body_carries_provider_block_and_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_PROMPT", "3.0")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_COMPLETION", "15.0")
    monkeypatch.setenv("OPENROUTER_MODELS_FALLBACK", "openai/gpt-5-mini")
    prov = OpenRouterProvider(Settings())
    llm = prov.chat_model()
    eb = llm.extra_body
    assert eb["provider"]["max_price"] == {"prompt": 3.0, "completion": 15.0}
    assert eb["provider"]["require_parameters"] is True
    assert eb["models"] == ["openai/gpt-5-mini"]
    assert llm.model_name == "anthropic/claude-sonnet-4.5"
