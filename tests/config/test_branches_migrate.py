import builtins

import yaml

from reviewer.config.branches import (
    migrate_repo_branches,
    publish_repository_block,
    render_repository_block,
    resolve_repo_branches,
)
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="dev,main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def test_migration_creates_file_with_env_branches(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    result = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert result.created is True
    data = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert data["repository"] == {
        "primary_branch": "dev",
        "index_branches": ["dev", "main"],
    }


def test_effective_branches_unchanged_by_migration(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    before = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    after = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert (after.primary, after.index) == (before.primary, before.index)


def test_second_call_is_noop(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    first = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    original = first.path.read_bytes()
    second = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert second.noop is True
    assert second.created is False
    assert second.path.read_bytes() == original


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


def test_migration_is_noop_when_global_review_yml_sets_branches(tmp_path, monkeypatch):
    """Important 1: глобальный home:review.yml стоит в порядке резолва ВЫШЕ env
    — миграция не должна молча заменить его веток на env-ветки."""
    (tmp_path / "review.yml").write_text(
        "repository:\n  index_branches: [dev]\n", encoding="utf-8"
    )
    settings = _settings(monkeypatch, branches="main,master")
    before = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    result = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    after = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert result.noop is True
    assert result.created is False
    assert not (tmp_path / "repos" / "o" / "r.yml").exists()
    assert (after.primary, after.index, after.source) == (before.primary, before.index, before.source)
    assert after.primary == "dev"


def test_migration_quotes_yaml_ambiguous_branch_names(tmp_path, monkeypatch):
    """Important 2: `2.0`/`on`/`no` — легальные git-ветки, но без кавычек YAML
    парсит их как float/bool. `feature{x}` вообще ломает flow-список."""
    settings = _settings(monkeypatch, branches="2.0,on,no,feature{x}")
    result = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert result.created is True
    text = result.path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["repository"]["index_branches"] == ["2.0", "on", "no", "feature{x}"]
    assert data["repository"]["primary_branch"] == "2.0"
    resolved = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert resolved.index == ("2.0", "on", "no", "feature{x}")
    assert resolved.primary == "2.0"


def test_env_file_is_not_touched(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("REVIEW_BRANCHES=dev,main\n", encoding="utf-8")
    migrate_repo_branches("o/r", settings=_settings(monkeypatch), config_root=tmp_path)
    assert env.read_text(encoding="utf-8") == "REVIEW_BRANCHES=dev,main\n"


def test_shared_publisher_create_race_degrades_to_noop(tmp_path, monkeypatch):
    path = tmp_path / "repos/o/r.yml"
    block = render_repository_block("main", ("main",))
    real_open = builtins.open

    def racing_open(file, mode="r", *args, **kwargs):
        if file == path and mode == "x":
            path.write_text("# created by another process\n", encoding="utf-8")
            raise FileExistsError
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", racing_open)

    action = publish_repository_block(path, "home:repos/o/r.yml", block)

    assert action == "noop"
    assert path.read_text(encoding="utf-8") == "# created by another process\n"
