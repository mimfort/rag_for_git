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


def test_review_verdict_log_default_empty():
    s = Settings(_env_file=None)
    assert s.review_verdict_log == ""


def test_review_verdict_log_reads_from_env(monkeypatch):
    monkeypatch.setenv("REVIEW_VERDICT_LOG", "./review_verdicts.jsonl")
    s = Settings(_env_file=None)
    assert s.review_verdict_log == "./review_verdicts.jsonl"


def test_verify_oneshot_threshold_default():
    s = Settings(_env_file=None)
    assert s.verify_oneshot_threshold == 10


def test_max_verify_tokens_default():
    s = Settings(_env_file=None)
    assert s.max_verify_tokens == 0


def test_verify_oneshot_threshold_reads_from_env(monkeypatch):
    monkeypatch.setenv("VERIFY_ONESHOT_THRESHOLD", "5")
    s = Settings(_env_file=None)
    assert s.verify_oneshot_threshold == 5


def test_max_verify_tokens_reads_from_env(monkeypatch):
    monkeypatch.setenv("MAX_VERIFY_TOKENS", "5000")
    s = Settings(_env_file=None)
    assert s.max_verify_tokens == 5000


def test_review_bundle_max_files_default():
    s = Settings(_env_file=None)
    assert s.review_bundle_max_files == 50


def test_review_bundle_max_lines_default():
    s = Settings(_env_file=None)
    assert s.review_bundle_max_lines == 1500


def test_max_tool_result_chars_default():
    s = Settings(_env_file=None)
    assert s.max_tool_result_chars == 8000
