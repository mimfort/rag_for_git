from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from reviewer.config.onboarding import (
    RepositoryConfigPlan,
    RepositoryDetection,
    apply_repository_config,
    detect_repository,
    parse_branch_csv,
    plan_repository_config,
)
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="main", *, config_home=None):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    if config_home is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return Settings(_env_file=None)


def _detection(
    root: Path,
    *,
    repo: str = "o/r",
    primary: str = "dev",
) -> RepositoryDetection:
    return RepositoryDetection(
        root=root,
        repo=repo,
        repo_source="git:origin",
        primary=primary,
        primary_source="git:origin/HEAD",
    )


def test_repository_contracts_are_immutable(tmp_path):
    detection = _detection(tmp_path)
    plan = RepositoryConfigPlan(
        path=tmp_path / "r.yml",
        repo="o/r",
        primary="dev",
        index=("dev",),
        repo_source="git:origin",
        primary_source="git:origin/HEAD",
        action="create",
        preview="repository: {}\n",
    )

    with pytest.raises(FrozenInstanceError):
        detection.repo = "other/repo"
    with pytest.raises(FrozenInstanceError):
        plan.action = "noop"


def test_detect_repository_returns_none_outside_git(monkeypatch):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: None)
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: pytest.fail("remote must not be inspected outside git"),
    )

    assert detect_repository(".", "o/r", settings=_settings(monkeypatch)) is None


def test_detect_repository_prefers_normalized_cli_repo(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: pytest.fail("explicit repo must win before origin"),
    )
    monkeypatch.setattr("reviewer.config.onboarding.remote_default_branch", lambda _path: "dev")

    result = detect_repository(
        ".",
        " CLI/Repo ",
        settings=_settings(monkeypatch, config_home=tmp_path),
    )

    assert result == RepositoryDetection(
        root=tmp_path,
        repo="cli/repo",
        repo_source="cli",
        primary="dev",
        primary_source="git:origin/HEAD",
    )


def test_detect_repository_falls_back_to_origin_repo(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: "https://github.com/Remote/Name.git",
    )
    monkeypatch.setattr("reviewer.config.onboarding.remote_default_branch", lambda _path: "main")

    result = detect_repository(
        ".",
        None,
        settings=_settings(monkeypatch, config_home=tmp_path),
    )

    assert result is not None
    assert result.repo == "remote/name"
    assert result.repo_source == "git:origin"


def test_detect_repository_returns_none_without_repo_source(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr("reviewer.config.onboarding.remote_url", lambda _path: None)

    assert (
        detect_repository(
            ".",
            None,
            settings=_settings(monkeypatch, config_home=tmp_path),
        )
        is None
    )


@pytest.mark.parametrize(
    ("origin_head", "expected_primary", "expected_source"),
    [
        ("dev", "dev", "git:origin/HEAD"),
        (None, "release", "env"),
    ],
)
def test_detect_repository_primary_precedence(
    monkeypatch,
    tmp_path,
    origin_head,
    expected_primary,
    expected_source,
):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: "git@github.com:o/r.git",
    )
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_default_branch",
        lambda _path: origin_head,
    )

    result = detect_repository(
        ".",
        None,
        settings=_settings(monkeypatch, "release,main", config_home=tmp_path),
    )

    assert result is not None
    assert result.primary == expected_primary
    assert result.primary_source == expected_source


def test_detect_repository_uses_effective_home_primary_source(monkeypatch, tmp_path):
    config_root = tmp_path / "rag-reviewer"
    config_root.mkdir()
    (config_root / "review.yml").write_text(
        "repository:\n  index_branches: [trunk]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: "https://github.com/o/r.git",
    )
    monkeypatch.setattr("reviewer.config.onboarding.remote_default_branch", lambda _path: None)

    result = detect_repository(
        ".",
        None,
        settings=_settings(monkeypatch, "main", config_home=tmp_path),
    )

    assert result is not None
    assert result.primary == "trunk"
    assert result.primary_source == "home:review.yml"


def test_parse_branch_csv_strips_items():
    assert parse_branch_csv(" dev, main ", "dev") == ("dev", "main")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "dev,,main", ",dev", "dev,", "dev,dev", "dev, dev"],
)
def test_parse_branch_csv_rejects_empty_items_and_duplicates(raw):
    with pytest.raises(ValueError):
        parse_branch_csv(raw, "dev")


def test_parse_branch_csv_requires_primary_in_index():
    with pytest.raises(ValueError, match="primary"):
        parse_branch_csv("main,release", "dev")


def test_plan_preserves_compatible_effective_index(monkeypatch, tmp_path):
    plan = plan_repository_config(
        _detection(tmp_path),
        settings=_settings(monkeypatch, "dev,main"),
        config_root=tmp_path,
    )

    assert plan.path == tmp_path / "repos/o/r.yml"
    assert plan.primary == "dev"
    assert plan.index == ("dev", "main")
    assert plan.action == "create"
    assert not plan.path.exists()


def test_plan_replaces_incompatible_effective_index(monkeypatch, tmp_path):
    plan = plan_repository_config(
        _detection(tmp_path),
        settings=_settings(monkeypatch, "main,master"),
        config_root=tmp_path,
    )

    assert plan.primary == "dev"
    assert plan.index == ("dev",)
    assert plan.action == "create"


@pytest.mark.parametrize("index", [(), ("dev", ""), ("dev", "dev"), ("main",)])
def test_plan_validates_explicit_index(monkeypatch, tmp_path, index):
    with pytest.raises(ValueError):
        plan_repository_config(
            _detection(tmp_path),
            settings=_settings(monkeypatch),
            index=index,
            config_root=tmp_path,
        )


def test_plan_uses_authoritative_repo_file_without_rewriting(monkeypatch, tmp_path):
    path = tmp_path / "repos/o/r.yml"
    path.parent.mkdir(parents=True)
    original = (
        "# authoritative\n"
        "repository:\n"
        "  primary_branch: trunk\n"
        "  index_branches: [trunk, release]\n"
        "  future: keep\n"
    )
    path.write_text(original, encoding="utf-8")

    plan = plan_repository_config(
        _detection(tmp_path),
        settings=_settings(monkeypatch),
        primary="other",
        index=("other",),
        config_root=tmp_path,
    )
    apply_repository_config(plan)

    assert plan.action == "noop"
    assert plan.primary == "trunk"
    assert plan.index == ("trunk", "release")
    assert yaml.safe_load(plan.preview)["repository"] == {
        "primary_branch": "trunk",
        "index_branches": ["trunk", "release"],
    }
    assert path.read_text(encoding="utf-8") == original


def test_apply_creates_and_repeat_is_byte_for_byte_noop(monkeypatch, tmp_path):
    detection = _detection(tmp_path)
    plan = plan_repository_config(
        detection,
        settings=_settings(monkeypatch),
        index=("dev", "main"),
        config_root=tmp_path,
    )

    assert plan.action == "create"
    assert not plan.path.exists()
    apply_repository_config(plan)
    first = plan.path.read_bytes()
    repeated = plan_repository_config(
        detection,
        settings=_settings(monkeypatch),
        config_root=tmp_path,
    )
    apply_repository_config(repeated)

    assert repeated.action == "noop"
    assert plan.path.read_bytes() == first
    assert yaml.safe_load(first)["repository"] == {
        "primary_branch": "dev",
        "index_branches": ["dev", "main"],
    }


def test_apply_appends_without_losing_comments_and_quotes_ambiguous_names(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "repos/o/r.yml"
    path.parent.mkdir(parents=True)
    original = "# keep me\nmax_comments: 7\n"
    path.write_text(original, encoding="utf-8")
    plan = plan_repository_config(
        _detection(tmp_path, primary="2.0"),
        settings=_settings(monkeypatch),
        index=("2.0", "on", "no", "feature{x}"),
        config_root=tmp_path,
    )

    assert plan.action == "append"
    assert path.read_text(encoding="utf-8") == original
    apply_repository_config(plan)
    text = path.read_text(encoding="utf-8")

    assert text.startswith(original)
    assert "# keep me" in text
    assert yaml.safe_load(text)["repository"] == {
        "primary_branch": "2.0",
        "index_branches": ["2.0", "on", "no", "feature{x}"],
    }


def test_plan_rejects_malformed_existing_yaml(monkeypatch, tmp_path):
    path = tmp_path / "repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository: [unclosed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="YAML"):
        plan_repository_config(
            _detection(tmp_path),
            settings=_settings(monkeypatch),
            config_root=tmp_path,
        )


def test_repository_paths_are_isolated(monkeypatch, tmp_path):
    first = plan_repository_config(
        _detection(tmp_path, repo="one/service", primary="main"),
        settings=_settings(monkeypatch),
        config_root=tmp_path,
    )
    second = plan_repository_config(
        _detection(tmp_path, repo="two/service", primary="release"),
        settings=_settings(monkeypatch),
        config_root=tmp_path,
    )

    apply_repository_config(first)
    apply_repository_config(second)

    assert first.path == tmp_path / "repos/one/service.yml"
    assert second.path == tmp_path / "repos/two/service.yml"
    assert yaml.safe_load(first.path.read_text(encoding="utf-8"))["repository"][
        "primary_branch"
    ] == "main"
    assert yaml.safe_load(second.path.read_text(encoding="utf-8"))["repository"][
        "primary_branch"
    ] == "release"
