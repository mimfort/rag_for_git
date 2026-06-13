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
