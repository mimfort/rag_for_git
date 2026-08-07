from reviewer.config.branches import resolve_repo_branches
from reviewer.config.settings import Settings


def test_review_branches_default_is_main(monkeypatch):
    monkeypatch.delenv("REVIEW_BRANCHES", raising=False)
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]
    assert s.primary_branch() == "main"


def test_review_branches_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "main, master , release/v1")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main", "master", "release/v1"]
    assert s.primary_branch() == "main"


def test_review_branches_empty_falls_back_to_main(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "   ")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]


def test_env_behaviour_unchanged_without_home_files(tmp_path, monkeypatch):
    """Деплой без домашних файлов ведёт себя ровно как раньше."""
    monkeypatch.setenv("REVIEW_BRANCHES", "dev,main")
    settings = Settings(_env_file=None)
    resolved = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert list(resolved.index) == settings.review_branches_list()
    assert resolved.primary == settings.primary_branch()
