from reviewer.config.settings import Settings

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


_TASK_BOARD_ENV = (
    "TASK_BOARD_TYPE", "TASK_BOARD_MCP",
    "TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE",
)


def test_task_board_default_none_when_unset(monkeypatch):
    for k in _TASK_BOARD_ENV:
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).task_board_default() is None


def test_task_board_default_from_env(monkeypatch):
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    monkeypatch.setenv("TASK_BOARD_KEY_PATTERN", r"[A-Z]+-\d+")
    monkeypatch.setenv("TASK_BOARD_URL_TEMPLATE", "https://ru.yougile.com/#{code}")
    assert Settings(_env_file=None).task_board_default() == {
        "type": "yougile",
        "mcp": "yougile",
        "key_pattern": r"[A-Z]+-\d+",
        "url_template": "https://ru.yougile.com/#{code}",
    }


def test_task_board_default_partial(monkeypatch):
    for k in _TASK_BOARD_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    assert Settings(_env_file=None).task_board_default() == {
        "type": "yougile", "mcp": "yougile",
    }


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
    s = Settings(_env_file=None, youtrack_token="perm:abc",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    assert s.board_creds("youtrack") == ("perm:abc", "https://c.youtrack.cloud/api")


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
