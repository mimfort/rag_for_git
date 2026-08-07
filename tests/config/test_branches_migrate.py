import yaml

from reviewer.config.branches import migrate_repo_branches, resolve_repo_branches
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="dev,main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def test_migration_creates_file_with_env_branches(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    result = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert result.created is True
    data = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert data["repository"]["index_branches"] == ["dev", "main"]


def test_effective_branches_unchanged_by_migration(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    before = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    after = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert (after.primary, after.index) == (before.primary, before.index)


def test_second_call_is_noop(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    second = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert second.noop is True
    assert second.created is False


def test_existing_repository_block_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [trunk]\n", encoding="utf-8")
    result = migrate_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.noop is True
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["repository"]["index_branches"] == ["trunk"]


def test_block_appended_to_existing_file_preserving_comments(tmp_path, monkeypatch):
    path = tmp_path / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("# важный комментарий\nmax_comments: 5\n", encoding="utf-8")
    migrate_repo_branches("o/r", settings=_settings(monkeypatch), config_root=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "# важный комментарий" in text
    assert "max_comments: 5" in text
    assert yaml.safe_load(text)["repository"]["index_branches"] == ["dev", "main"]


def test_env_file_is_not_touched(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("REVIEW_BRANCHES=dev,main\n", encoding="utf-8")
    migrate_repo_branches("o/r", settings=_settings(monkeypatch), config_root=tmp_path)
    assert env.read_text(encoding="utf-8") == "REVIEW_BRANCHES=dev,main\n"
