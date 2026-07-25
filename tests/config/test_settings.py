from reviewer.config.settings import Settings
from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.tasks.boards.registry import BoardProviderRegistry
from tests.tasks.boards.provider_fakes import fake_provider_spec

def test_review_categories_list_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("REVIEW_CATEGORIES", "security, correctness")
    s = Settings(_env_file=None)
    assert s.review_categories_list() == ["security", "correctness"]


def test_default_repo_defaults_empty(monkeypatch):
    monkeypatch.delenv("DEFAULT_REPO", raising=False)
    from reviewer.config.settings import Settings
    assert Settings(_env_file=None).default_repo == ""


def test_default_repo_from_env(monkeypatch):
    monkeypatch.setenv("DEFAULT_REPO", "owner/name")
    from reviewer.config.settings import Settings
    assert Settings(_env_file=None).default_repo == "owner/name"



def test_task_board_default_none_when_unset(monkeypatch):
    for k in ("TASK_BOARD_MCP", "TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE",
              "YOUGILE_API_KEY", "TASK_BOARD_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).task_board_default() is None


def test_task_board_api_base_default_yougile():
    s = Settings(_env_file=None, task_board_api_key="k", task_board_api_base="")
    assert s.task_board_api_base_for("yougile") == "https://yougile.com/api-v2"


def test_task_board_api_base_explicit_overrides_default():
    s = Settings(_env_file=None, task_board_api_base="https://ru.yougile.com/api-v2")
    assert s.task_board_api_base_for("yougile") == "https://ru.yougile.com/api-v2"


def test_task_board_api_base_unknown_type_empty():
    s = Settings(_env_file=None, task_board_api_base="")
    assert s.task_board_api_base_for("jira") == ""


def test_board_creds_yougile_from_per_type(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk",
                 yougile_api_base="https://ru.yougile.com/api-v2")
    assert s.board_creds("yougile") == ("yk", "https://ru.yougile.com/api-v2")


def test_board_creds_yougile_legacy_fallback(monkeypatch):
    # старые деплои: только TASK_BOARD_API_KEY/API_BASE
    s = Settings(_env_file=None, task_board_api_key="legacy", task_board_api_base="")
    assert s.board_creds("yougile") == ("legacy", "https://yougile.com/api-v2")


def test_board_creds_youtrack(monkeypatch):
    s = Settings(_env_file=None, youtrack_token="test-token-123",
                 youtrack_base_url="https://example.youtrack.cloud/api")
    assert s.board_creds("youtrack") == ("test-token-123", "https://example.youtrack.cloud/api")


def test_board_creds_unknown_type_empty():
    assert Settings(_env_file=None).board_creds("jira") == ("", "")


def test_configured_board_types_lists_only_with_key(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="")
    assert s.configured_board_types() == ["yougile"]


def test_configured_board_types_both(monkeypatch):
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    assert s.configured_board_types() == ["yougile", "youtrack"]


def test_configured_board_types_empty_when_nothing(monkeypatch):
    for k in ("TASK_BOARD_API_KEY", "YOUGILE_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).configured_board_types() == []


def test_configured_types_are_derived_from_injected_registry_spec():
    settings = Settings(_env_file=None)
    registry = BoardProviderRegistry([fake_provider_spec()])
    source = ProviderCredentialSource(values={"FAKE_TOKEN": "x"})
    assert settings.configured_board_types(
        registry=registry,
        credential_source=source,
    ) == ["fake"]


def test_task_board_default_type_single_from_creds(monkeypatch):
    for k in ("TASK_BOARD_TYPE", "YOUGILE_API_KEY", "TASK_BOARD_API_KEY",
              "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
    monkeypatch.setenv("YOUTRACK_BASE_URL", "https://yt.example.com/api")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert result["type"] == "youtrack"


def test_task_board_default_type_list_when_both_creds(monkeypatch):
    monkeypatch.setenv("YOUGILE_API_KEY", "yg-key")
    monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
    monkeypatch.setenv("YOUTRACK_BASE_URL", "https://yt.example.com/api")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert result["type"] == ["yougile", "youtrack"]


def test_task_board_default_type_absent_when_no_creds(monkeypatch):
    for k in ("TASK_BOARD_TYPE", "YOUGILE_API_KEY", "TASK_BOARD_API_KEY",
              "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert "type" not in result


def test_task_board_default_ignores_task_board_type_env(monkeypatch):
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")   # старый env, нет кредов
    for k in ("YOUGILE_API_KEY", "TASK_BOARD_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    result = Settings(_env_file=None).task_board_default()
    # TASK_BOARD_TYPE игнорируется — type не попадает в ответ без кредов
    assert result is None or "type" not in (result or {})


def test_attachment_settings_defaults():
    from reviewer.config.settings import Settings
    s = Settings(_env_file=None)
    assert s.task_attachment_max_bytes == 10 * 1024 * 1024
    assert s.task_attachment_timeout == 10.0
    assert s.task_attachment_embed_chars == 8000
    assert s.task_attachment_store_chars == 200000
