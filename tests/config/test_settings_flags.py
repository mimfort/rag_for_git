from reviewer.config.settings import Settings


def test_tier3_flags_have_defaults():
    s = Settings(_env_file=None)
    assert s.review_agentic_verify is True
    assert s.review_synthesis is True
    assert s.review_verify_min_severity == "high"
    assert s.review_verify_max_iterations == 2
    assert s.review_max_tool_iterations == 12


def test_openrouter_model_verify_default_empty():
    s = Settings(_env_file=None)
    assert s.openrouter_model_verify == ""


def test_openrouter_prompt_cache_default_true():
    s = Settings(_env_file=None)
    assert s.openrouter_prompt_cache is True


def test_openrouter_model_verify_reads_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL_VERIFY", "anthropic/claude-haiku-4.5")
    s = Settings(_env_file=None)
    assert s.openrouter_model_verify == "anthropic/claude-haiku-4.5"


def test_openrouter_prompt_cache_reads_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROMPT_CACHE", "false")
    s = Settings(_env_file=None)
    assert s.openrouter_prompt_cache is False
