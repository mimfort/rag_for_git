from pathlib import Path

import pytest

from reviewer.config.layers import (
    HomeConfigError,
    ResolutionMeta,
    build_config_report,
    home_repo_path,
    migrate_repo_config,
    policy_to_public_data,
    resolve_policy_data,
)
from reviewer.config.settings import Settings
from reviewer.policy.policy import ReviewPolicy


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_layers_replace_top_level_values_and_report_sources(tmp_path: Path) -> None:
    _write(
        tmp_path / "review.yml",
        "paths: {ignore: [global]}\nmax_comments: 5\ncontext_limits: {graph: {hops: 2}}\n",
    )
    _write(
        tmp_path / "repos/o/r.yml",
        "paths: {ignore: [home-repo]}\ntask_board:\n",
    )
    committed = (
        "paths: {ignore: [committed]}\n"
        "max_comments: 7\n"
        "context_limits: {search_codebase: {ceiling: 25}}\n"
    )

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    assert data["paths"] == {"ignore": ["home-repo"]}
    assert data["context_limits"] == {"search_codebase": {"ceiling": 25}}
    assert data["task_board"] is None
    assert meta.sources["paths"] == "home:repos/o/r.yml"
    assert meta.sources["max_comments"] == ".review.yml"
    assert meta.shadowed["paths"] == ("home:review.yml", ".review.yml")
    assert meta.shadowed["max_comments"] == ("home:review.yml",)


def test_subgroup_repo_uses_nested_home_path(tmp_path: Path) -> None:
    assert home_repo_path("group/sub/repo", tmp_path) == (
        tmp_path / "repos/group/sub/repo.yml"
    )


def test_runtime_skips_bad_home_but_strict_mode_raises(tmp_path: Path) -> None:
    _write(tmp_path / "review.yml", "[not-a-mapping]\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )
    assert data == {"max_comments": 9}
    assert len(meta.warnings) == 1
    assert "home:review.yml" in meta.warnings[0]

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_runtime_warns_when_home_file_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_config = tmp_path / "review.yml"
    _write(home_config, "max_comments: 4\n")
    original_stat = Path.stat

    def inaccessible_probe(path: Path, *args, **kwargs):
        if path == home_config:
            raise PermissionError("denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible_probe)

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )
    assert data == {"max_comments": 9}
    assert len(meta.warnings) == 1
    assert "home:review.yml" in meta.warnings[0]

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_runtime_skips_recursion_during_home_yaml_parse(tmp_path: Path) -> None:
    deep_yaml = "".join(
        "  " * depth + "nested:\n" for depth in range(500)
    ) + "  " * 500 + "value: true\n"
    _write(tmp_path / "review.yml", deep_yaml)

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )
    assert data == {"max_comments": 9}
    assert len(meta.warnings) == 1

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_credential_file_is_skipped_without_echoing_value(tmp_path: Path) -> None:
    secret = "do-not-echo"
    _write(
        tmp_path / "repos/o/r.yml",
        f"max_comments: 4\nnested:\n  github_token: {secret}\n",
    )

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 8\n", config_root=tmp_path
    )

    assert data["max_comments"] == 8
    rendered = "\n".join(meta.warnings)
    assert "github_token" in rendered
    assert secret not in rendered


def test_max_tokens_is_not_misclassified_as_secret(tmp_path: Path) -> None:
    _write(tmp_path / "review.yml", "future: {max_tokens: 100}\n")
    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: None, config_root=tmp_path
    )
    assert data["future"] == {"max_tokens": 100}
    assert meta.warnings == ()


def test_runtime_skips_home_config_with_recursive_yaml_alias(tmp_path: Path) -> None:
    _write(tmp_path / "review.yml", "loop: &loop\n  next: *loop\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )

    assert data == {"max_comments": 9}
    assert len(meta.warnings) == 1

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_migrate_creates_file_and_second_call_is_noop(tmp_path: Path) -> None:
    source = "# keep comment\npaths:\n  ignore: [vendor]\n"
    first = migrate_repo_config(
        "o/r", "main", lambda ref: source, config_root=tmp_path
    )
    second = migrate_repo_config(
        "o/r", "main", lambda ref: source, config_root=tmp_path
    )

    destination = tmp_path / "repos/o/r.yml"
    assert first.created is True
    assert second.noop is True
    assert destination.read_text(encoding="utf-8") == source


def test_migrate_refuses_different_destination(tmp_path: Path) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, "max_comments: 3\n")

    result = migrate_repo_config(
        "o/r",
        "main",
        lambda ref: "max_comments: 7\npaths: {ignore: [vendor]}\n",
        config_root=tmp_path,
    )

    assert result.created is False
    assert result.noop is False
    assert result.conflicting_keys == ("max_comments", "paths")
    assert destination.read_text(encoding="utf-8") == "max_comments: 3\n"


def test_migrate_rejects_secret_candidate_before_write(tmp_path: Path) -> None:
    with pytest.raises(HomeConfigError) as exc:
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "github_token: do-not-write\n",
            config_root=tmp_path,
        )
    assert "do-not-write" not in str(exc.value)
    assert not (tmp_path / "repos/o/r.yml").exists()


def test_policy_to_public_data_excludes_settings_secrets_and_unknown_yaml() -> None:
    secret = "do-not-serialize"
    policy = ReviewPolicy.load_data(
        Settings(_env_file=None, github_token=secret, neo4j_password=secret),
        {"max_comments": 4, "unknown_yaml_value": secret},
    )

    data = policy_to_public_data(policy)

    assert set(data) == {
        "categories",
        "enabled_only",
        "severity_threshold",
        "paths",
        "max_comments",
        "min_confidence",
        "output_language",
        "task_board",
        "grounding_max_distance",
        "summary_cluster_depth",
        "summary_topk_threshold",
        "summary_cluster_depth_overrides",
        "context_limits",
    }
    assert data["max_comments"] == 4
    assert secret not in repr(data)


def test_build_config_report_marks_policy_defaults_as_env() -> None:
    report = build_config_report(
        "O/R",
        "main",
        Settings(_env_file=None, review_max_comments=12),
        {"paths": {"ignore": ["vendor"]}},
        ResolutionMeta({"paths": "home:repos/o/r.yml"}, {}, ("safe warning",)),
    )

    assert report["repo"] == "o/r"
    assert report["branch"] == "main"
    assert report["effective"]["max_comments"] == 12
    assert report["sources"]["max_comments"] == "env"
    assert report["sources"]["paths"] == "home:repos/o/r.yml"
    assert report["warnings"] == ["safe warning"]
