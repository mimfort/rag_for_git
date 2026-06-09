from reviewer.config.settings import Settings
from reviewer.llm.openrouter import OpenRouterProvider


def test_extra_body_carries_provider_block_and_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_PROMPT", "3.0")
    monkeypatch.setenv("OPENROUTER_MAX_PRICE_COMPLETION", "15.0")
    monkeypatch.setenv("OPENROUTER_MODELS_FALLBACK", "openai/gpt-5-mini")
    prov = OpenRouterProvider(Settings(_env_file=None))   # игнорировать локальный .env — тест герметичен
    llm = prov.chat_model()
    eb = llm.extra_body
    assert eb["provider"]["max_price"] == {"prompt": 3.0, "completion": 15.0}
    assert eb["provider"]["require_parameters"] is True
    assert eb["models"] == ["openai/gpt-5-mini"]
    assert llm.model_name == "anthropic/claude-sonnet-4.5"


def test_chat_model_without_arg_uses_settings_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    prov = OpenRouterProvider(Settings(_env_file=None))
    llm = prov.chat_model()
    assert llm.model_name == "anthropic/claude-sonnet-4.5"


def test_chat_model_with_arg_overrides_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    prov = OpenRouterProvider(Settings(_env_file=None))
    llm = prov.chat_model(model="x/y")
    assert llm.model_name == "x/y"


def test_chat_model_with_tools_passes_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    prov = OpenRouterProvider(Settings(_env_file=None))
    # bind_tools возвращает RunnableBinding, проверяем через bound.model_name
    bound = prov.chat_model_with_tools([], model="cheap/model")
    # langchain хранит оригинальный ChatOpenAI как .bound
    assert bound.bound.model_name == "cheap/model"
