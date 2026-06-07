from reviewer.config.settings import Settings

def test_openrouter_provider_block_built_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_PROMPT", "3.0")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_COMPLETION", "15.0")
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "price")
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", "deny")
    monkeypatch.setenv("OPENROUTER_MODELS_FALLBACK", "openai/gpt-5-mini, x/y")
    s = Settings(_env_file=None)   # игнорировать локальный .env разработчика — тест герметичен
    block = s.openrouter_provider_block()
    assert block["sort"] == "price"
    assert block["max_price"] == {"prompt": 3.0, "completion": 15.0}
    assert block["data_collection"] == "deny"
    assert block["require_parameters"] is True
    assert s.openrouter_models_list() == ["openai/gpt-5-mini", "x/y"]
