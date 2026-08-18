from pathlib import Path
import os
import stat
import traceback

import pytest

import reviewer.config.layers as layers
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
    # context_limits теперь сливается по подсекциям: слой, не высказавшийся о
    # search_codebase, её не стирает.
    assert data["context_limits"] == {
        "graph": {"hops": 2},
        "search_codebase": {"ceiling": 25},
    }
    assert data["task_board"] is None
    assert meta.sources["paths.ignore"] == "home:repos/o/r.yml"
    assert meta.sources["max_comments"] == ".review.yml"
    assert meta.shadowed["paths.ignore"] == ("home:review.yml", ".review.yml")
    assert meta.shadowed["max_comments"] == ("home:review.yml",)


def test_pri259_committed_code_section_pin_survives_partial_home_layer(tmp_path: Path) -> None:
    """Регресс PRI-259: домашний context_limits без code_section не уносит коммиченный пин."""
    _write(
        tmp_path / "repos/o/r.yml",
        "context_limits: {graph: {hops: 1}, search_codebase: {ceiling: 15}}\n",
    )
    committed = (
        "context_limits:\n"
        "  code_section: {max_files: 12, chars_per_file: 1300}\n"
    )

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    limits = data["context_limits"]
    assert limits["code_section"] == {"max_files": 12, "chars_per_file": 1300}
    assert limits["graph"] == {"hops": 1}
    assert meta.sources["context_limits.code_section.max_files"] == ".review.yml"
    assert meta.sources["context_limits.graph.hops"] == "home:repos/o/r.yml"
    assert "context_limits" not in meta.shadowed        # верхний ключ больше не затеняется целиком


def test_shadowed_names_the_leaf_when_subsection_is_overridden(tmp_path: Path) -> None:
    _write(tmp_path / "repos/o/r.yml", "context_limits: {code_section: {max_files: 20}}\n")
    committed = "context_limits: {code_section: {max_files: 12, chars_per_file: 1300}}\n"

    _data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    assert meta.shadowed["context_limits.code_section.max_files"] == (".review.yml",)
    assert "context_limits" not in meta.shadowed


def test_layer_order_is_unchanged(tmp_path: Path) -> None:
    """Критерий 4: приоритет источников прежний — домашний per-repo слой последний."""
    _write(tmp_path / "review.yml", "max_comments: 1\n")
    _write(tmp_path / "repos/o/r.yml", "max_comments: 3\n")

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: "max_comments: 2\n", config_root=tmp_path
    )

    assert data["max_comments"] == 3
    assert meta.sources["max_comments"] == "home:repos/o/r.yml"
    assert meta.shadowed["max_comments"] == ("home:review.yml", ".review.yml")


def test_per_repo_home_task_sync_filters_resolve_independently(tmp_path: Path) -> None:
    _write(
        tmp_path / "repos/o/first.yml",
        "task_board:\n  project: FIRST\n  sync_filter: {max_age_days: 7}\n",
    )
    _write(
        tmp_path / "repos/o/second.yml",
        "task_board:\n  project: SECOND\n  sync_filter: {include_archived: false}\n",
    )

    first, first_meta = resolve_policy_data(
        "o/first", "main", lambda ref: None, config_root=tmp_path
    )
    second, second_meta = resolve_policy_data(
        "o/second", "main", lambda ref: None, config_root=tmp_path
    )

    assert first["task_board"] == {
        "project": "FIRST",
        "sync_filter": {"max_age_days": 7},
    }
    assert second["task_board"] == {
        "project": "SECOND",
        "sync_filter": {"include_archived": False},
    }
    assert first_meta.sources["task_board"] == "home:repos/o/first.yml"
    assert second_meta.sources["task_board"] == "home:repos/o/second.yml"


def test_invalid_home_task_sync_filter_is_quarantined_or_rejected(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "repos/o/r.yml",
        "task_board:\n  project: PRI\n  sync_filter: {max_age_days: 0}\n",
    )

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )

    assert data == {"max_comments": 9}
    assert meta.warnings == (
        "home:repos/o/r.yml: policy содержит недопустимые значения",
    )
    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_subgroup_repo_uses_nested_home_path(tmp_path: Path) -> None:
    assert home_repo_path("group/sub/repo", tmp_path) == (
        tmp_path / "repos/group/sub/repo.yml"
    )


def test_dotted_repo_name_appends_yaml_suffix_without_colliding(
    tmp_path: Path,
) -> None:
    dotted = tmp_path / "repos/o/api.v2.yml"
    plain = tmp_path / "repos/o/api.yml"
    _write(dotted, "max_comments: 7\n")
    _write(plain, "max_comments: 3\n")

    data, meta = resolve_policy_data(
        "o/api.v2",
        "main",
        lambda ref: "max_comments: 5\n",
        config_root=tmp_path,
    )

    assert home_repo_path("o/api.v2", tmp_path) == dotted
    assert data["max_comments"] == 7
    assert meta.sources["max_comments"] == "home:repos/o/api.v2.yml"
    assert meta.shadowed["max_comments"] == (".review.yml",)


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


def test_categories_null_is_a_valid_home_override(tmp_path: Path) -> None:
    _write(tmp_path / "repos/o/r.yml", "categories:\n")

    data, meta = resolve_policy_data(
        "o/r",
        "main",
        lambda ref: "categories: {security: false}\n",
        config_root=tmp_path,
        strict_home=True,
    )

    assert data["categories"] is None
    assert meta.sources["categories"] == "home:repos/o/r.yml"
    # Коммиченное значение было mapping'ом ({security: false}) — затенённый
    # лист называется по своему dotted-пути, а не по верхнему ключу.
    assert meta.shadowed["categories.security"] == (".review.yml",)
    assert meta.warnings == ()


def test_migrate_accepts_categories_null_as_effective_no_change(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "review.yml", "categories: {style: false}\n")

    result = migrate_repo_config(
        "o/r",
        "main",
        lambda ref: "categories:\n",
        config_root=tmp_path,
        settings=Settings(_env_file=None, review_categories="security"),
    )

    assert result.created is True
    assert result.data["categories"] is None
    assert (tmp_path / "repos/o/r.yml").read_text(encoding="utf-8") == "categories:\n"


def test_runtime_quarantines_invalid_home_layer_even_when_value_is_shadowed(
    tmp_path: Path,
) -> None:
    secret = "do-not-echo"
    _write(
        tmp_path / "review.yml",
        f"max_comments: {secret}\nfuture_policy: enabled\n",
    )

    data, meta = resolve_policy_data(
        "o/r",
        "main",
        lambda ref: "max_comments: 9\n",
        config_root=tmp_path,
    )

    assert data == {"max_comments": 9}
    assert meta.sources == {"max_comments": ".review.yml"}
    assert meta.shadowed == {}
    assert meta.warnings == (
        "home:review.yml: policy содержит недопустимые значения",
    )
    assert secret not in repr(meta)


def test_strict_home_rejects_invalid_known_value_without_echoing_literal(
    tmp_path: Path,
) -> None:
    secret = "do-not-echo"
    _write(
        tmp_path / "repos/o/r.yml",
        f"summary_cluster_depth: {secret}\nfuture_policy: enabled\n",
    )

    with pytest.raises(HomeConfigError) as captured:
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )

    assert str(captured.value) == (
        "home:repos/o/r.yml: policy содержит недопустимые значения"
    )
    assert secret not in repr(captured.value)


def test_strict_home_exception_chain_does_not_echo_malformed_literal(
    tmp_path: Path,
) -> None:
    secret = "do-not-echo"
    _write(tmp_path / "review.yml", f"max_comments: [{secret}\n")

    with pytest.raises(HomeConfigError) as captured:
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: None,
            config_root=tmp_path,
            strict_home=True,
        )

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert secret not in rendered


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


def test_migrate_report_carries_skipped_global_home_layer(tmp_path: Path) -> None:
    """M-1: `_simulated_repo_layer` не должен терять meta.skipped."""
    _write(tmp_path / "review.yml", "github_token: leaked-value\n")
    source = "paths:\n  ignore: [vendor]\n"

    result = migrate_repo_config(
        "o/r", "main", lambda ref: source, config_root=tmp_path
    )

    assert result.created is True
    assert [(item.layer, item.category) for item in result.meta.skipped] == [
        ("home:review.yml", "credential")
    ]


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


@pytest.mark.parametrize(
    ("source", "existing"),
    [
        ("future: true\n", "future: 1\n"),
        ("future: 1\n", "future: 1.0\n"),
        (
            "future:\n  nested: [true, {value: 2}]\n",
            "future:\n  nested: [1, {value: 2.0}]\n",
        ),
    ],
)
def test_migrate_semantic_equality_is_type_sensitive_recursively(
    tmp_path: Path,
    source: str,
    existing: str,
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, existing)

    result = migrate_repo_config(
        "o/r",
        "main",
        lambda ref: source,
        config_root=tmp_path,
    )

    assert result.noop is False
    assert result.conflicting_keys == ("future",)
    assert destination.read_text(encoding="utf-8") == existing


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


@pytest.mark.parametrize("existing", [False, True])
def test_migrate_uses_one_committed_snapshot_for_create_and_noop(
    tmp_path: Path,
    existing: bool,
) -> None:
    source = "max_comments: 7\n"
    destination = tmp_path / "repos/o/r.yml"
    if existing:
        _write(destination, source)
    calls = 0

    def fetch_once(ref: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("committed policy was fetched more than once")
        return source

    result = migrate_repo_config(
        "o/r",
        "moving-branch",
        fetch_once,
        config_root=tmp_path,
    )

    assert calls == 1
    assert result.noop is existing
    assert result.created is not existing


def test_migrate_validates_candidate_before_destination_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-echo"

    def publication_must_not_run(*args, **kwargs):
        raise AssertionError("invalid candidate reached publication")

    monkeypatch.setattr(layers, "_publish_new_config", publication_must_not_run)

    with pytest.raises(HomeConfigError) as captured:
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: f"max_comments: {secret}\n",
            config_root=tmp_path,
        )

    assert secret not in repr(captured.value)
    assert not (tmp_path / "repos").exists()


def test_migrate_validates_simulated_effective_policy_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_simulation = layers._simulated_repo_layer

    def invalid_simulation(data, meta, candidate, source):
        simulated, simulated_meta = original_simulation(data, meta, candidate, source)
        simulated["context_limits"] = {"graph": {"hops": "do-not-echo"}}
        return simulated, simulated_meta

    def publication_must_not_run(*args, **kwargs):
        raise AssertionError("invalid effective policy reached publication")

    monkeypatch.setattr(layers, "_simulated_repo_layer", invalid_simulation)
    monkeypatch.setattr(layers, "_publish_new_config", publication_must_not_run)

    with pytest.raises(HomeConfigError) as captured:
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "max_comments: 7\n",
            config_root=tmp_path,
        )

    assert "do-not-echo" not in repr(captured.value)
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
        "summary_paths",
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


def test_build_config_report_accepts_code_section(tmp_path: Path) -> None:
    """PRI-256: подсекция code_section — часть whitelist'а context_limits.

    Регрессия: добавление 4-й подсекции (code_section) в CodeSectionLimits ломало
    build_config_report, потому что валидатор публичной формы политики знал только про
    3 старые подсекции (search_codebase/search_tasks/graph) и требовал точного
    совпадения набора ключей.
    """
    report = build_config_report(
        "O/R",
        "main",
        Settings(_env_file=None),
        {"context_limits": {"code_section": {"max_files": 20, "chars_per_file": 600}}},
        ResolutionMeta({}, {}, ()),
    )

    assert report["effective"]["context_limits"]["code_section"] == {
        "max_files": 20,
        "max_chunks_per_file": 1,
        "chars_per_file": 600,
        "max_augmented_files": 3,
    }


def test_build_config_report_rejects_invalid_code_section(tmp_path: Path) -> None:
    with pytest.raises(HomeConfigError) as captured:
        build_config_report(
            "O/R",
            "main",
            Settings(_env_file=None),
            {"context_limits": {"code_section": {"max_files": "много"}}},
            ResolutionMeta({}, {}, ()),
        )

    assert "недопустимые значения" in str(captured.value)


def test_resolve_policy_data_quarantines_invalid_home_code_section(tmp_path: Path) -> None:
    """Слой resolve_policy_data (_validate_context_limits_layer) тоже знает про code_section:
    невалидное значение в домашнем слое карантинится, а не проходит как валидное."""
    _write(
        tmp_path / "review.yml",
        "context_limits: {code_section: {max_files: not-an-int}}\n",
    )

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: None, config_root=tmp_path
    )

    assert "context_limits" not in data
    assert any(item.layer == "home:review.yml" for item in meta.skipped)


def test_migrate_does_not_clobber_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "repos/o/r.yml"

    def race(temp_path, target_path) -> None:
        Path(target_path).write_text("max_comments: 3\n", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(layers.os, "link", race)

    result = migrate_repo_config(
        "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
    )

    assert result.conflicting_keys == ("max_comments",)
    assert destination.read_text(encoding="utf-8") == "max_comments: 3\n"


def test_migrate_precomputes_result_before_final_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = ResolutionMeta({}, {}, ())
    calls = 0

    def resolution_must_not_run_after_publish(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("resolver ran after publish")
        return {"max_comments": 7}, meta

    monkeypatch.setattr(layers, "resolve_policy_data", resolution_must_not_run_after_publish)

    result = migrate_repo_config(
        "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
    )

    assert result.created is True
    assert calls == 1
    assert (tmp_path / "repos/o/r.yml").read_text(encoding="utf-8") == "max_comments: 7\n"


def test_migrate_refuses_symlink_destination(tmp_path: Path) -> None:
    destination = tmp_path / "repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    target = tmp_path / "outside.yml"
    target.write_text("max_comments: 3\n", encoding="utf-8")
    destination.symlink_to(target)

    with pytest.raises(HomeConfigError, match="symlink"):
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert target.read_text(encoding="utf-8") == "max_comments: 3\n"


def test_migrate_rejects_symlinked_parent_below_config_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "o").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HomeConfigError, match="symlink"):
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "max_comments: 7\n",
            config_root=tmp_path,
        )

    assert not (outside / "r.yml").exists()


def test_migrate_allows_symlinked_ancestor_outside_config_root(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    config_root = alias / "reviewer-config"

    result = migrate_repo_config(
        "o/r",
        "main",
        lambda ref: "max_comments: 7\n",
        config_root=config_root,
    )

    assert result.created is True
    assert (real_parent / "reviewer-config/repos/o/r.yml").read_text(
        encoding="utf-8"
    ) == "max_comments: 7\n"


def test_migrate_rejects_fifo_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    os.mkfifo(destination)

    def nonregular_must_not_be_opened(*args, **kwargs):
        raise AssertionError("FIFO was opened")

    monkeypatch.setattr(layers.os, "open", nonregular_must_not_be_opened)

    with pytest.raises(HomeConfigError, match="regular"):
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "max_comments: 7\n",
            config_root=tmp_path,
        )


def test_migrate_rejects_socket_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, "max_comments: 3\n")
    original_lstat = layers.os.lstat

    def socket_lstat(path, *args, **kwargs):
        result = original_lstat(path, *args, **kwargs)
        if Path(path) == destination:
            fields = list(result)
            fields[0] = stat.S_IFSOCK | 0o600
            return os.stat_result(fields)
        return result

    def nonregular_must_not_be_opened(*args, **kwargs):
        raise AssertionError("socket was opened")

    monkeypatch.setattr(layers.os, "lstat", socket_lstat)
    monkeypatch.setattr(layers.os, "open", nonregular_must_not_be_opened)

    with pytest.raises(HomeConfigError, match="regular"):
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "max_comments: 7\n",
            config_root=tmp_path,
        )


def test_migrate_rejects_replacement_during_existing_destination_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, "max_comments: 3\n")
    original_open = layers.os.open

    def open_then_replace(path, flags):
        descriptor = original_open(path, flags)
        destination.unlink()
        destination.write_text("max_comments: 9\n", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(layers.os, "open", open_then_replace)

    with pytest.raises(HomeConfigError, match="race"):
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert destination.read_text(encoding="utf-8") == "max_comments: 9\n"


def test_migrate_rejects_symlink_swap_without_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    target = tmp_path / "target.yml"
    _write(destination, "max_comments: 3\n")
    target.write_text("max_comments: 9\n", encoding="utf-8")
    original_open = layers.os.open
    monkeypatch.delattr(layers.os, "O_NOFOLLOW", raising=False)

    def open_after_symlink_swap(path, flags):
        destination.unlink()
        destination.symlink_to(target)
        return original_open(path, flags)

    monkeypatch.setattr(layers.os, "open", open_after_symlink_swap)

    with pytest.raises(HomeConfigError, match="race"):
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert target.read_text(encoding="utf-8") == "max_comments: 9\n"


def test_migrate_sanitizes_link_error_and_preserves_primary_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "do-not-echo"

    def fail_link(*args) -> None:
        raise OSError(secret)

    monkeypatch.setattr(layers.os, "link", fail_link)
    original_unlink = Path.unlink

    def fail_temp_cleanup(path: Path, *args, **kwargs) -> None:
        if path.parent == tmp_path / "repos/o":
            raise OSError("cleanup-" + secret)
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)

    with pytest.raises(HomeConfigError) as exc:
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert secret not in str(exc.value)


def test_migrate_sanitizes_destination_parent_mkdir_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "do-not-echo"
    original_mkdir = layers.os.mkdir

    def fail_destination_parent(path, *args, **kwargs) -> None:
        if Path(path) == tmp_path / "repos/o" or path == "o":
            raise OSError(secret)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(layers.os, "mkdir", fail_destination_parent)

    with pytest.raises(HomeConfigError) as exc:
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert secret not in str(exc.value)


def test_migrate_preserves_fdopen_error_when_descriptor_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, "max_comments: 3\n")
    primary = "fdopen-secret"
    cleanup = "close-secret"
    fdopen_failed = False

    def fail_fdopen(*args, **kwargs):
        nonlocal fdopen_failed
        fdopen_failed = True
        raise OSError(primary)

    def fail_close(*args, **kwargs):
        if fdopen_failed:
            raise OSError(cleanup)
        return original_close(*args, **kwargs)

    monkeypatch.setattr(layers.os, "fdopen", fail_fdopen)
    original_close = layers.os.close
    monkeypatch.setattr(layers.os, "close", fail_close)

    with pytest.raises(HomeConfigError) as exc:
        migrate_repo_config(
            "o/r", "main", lambda ref: "max_comments: 7\n", config_root=tmp_path
        )

    assert primary not in str(exc.value)
    assert cleanup not in str(exc.value)


def test_build_config_report_rejects_non_json_public_value() -> None:
    class SecretValue:
        def __repr__(self) -> str:
            return "do-not-echo"

    with pytest.raises(HomeConfigError) as exc:
        build_config_report(
            "o/r",
            "main",
            Settings(_env_file=None),
            {"categories": {"security": SecretValue()}},
            ResolutionMeta({}, {}, ()),
        )

    assert "do-not-echo" not in str(exc.value)


def test_committed_repository_key_is_ignored_with_warning(tmp_path):
    committed = "repository:\n  index_branches: [evil]\nmax_comments: 5\n"
    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert "repository" not in data
    assert data["max_comments"] == 5
    assert any("repository" in warning for warning in meta.warnings)
    assert not any("evil" in warning for warning in meta.warnings)


def test_home_repository_block_is_not_a_policy_key(tmp_path):
    home = tmp_path / "repos" / "o" / "r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "repository:\n  index_branches: [dev]\nmax_comments: 7\n", encoding="utf-8"
    )
    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: None, config_root=tmp_path
    )
    assert "repository" not in data
    assert data["max_comments"] == 7


def test_migration_ignores_repository_block_when_comparing(tmp_path):
    committed = "max_comments: 5\n"
    home = tmp_path / "repos" / "o" / "r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "max_comments: 5\nrepository:\n  index_branches: [dev]\n", encoding="utf-8"
    )
    result = migrate_repo_config(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert result.conflicting_keys == ()
    assert result.noop is True


def test_migration_ignores_repository_block_in_committed_when_comparing(tmp_path):
    """Блок repository на committed-стороне не должен давать ложный конфликт."""
    committed = "max_comments: 5\nrepository:\n  index_branches: [evil]\n"
    home = tmp_path / "repos" / "o" / "r.yml"
    home.parent.mkdir(parents=True)
    home.write_text("max_comments: 5\n", encoding="utf-8")
    result = migrate_repo_config(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert result.conflicting_keys == ()
    assert result.noop is True


def test_migration_strips_repository_block_when_publishing_new_config(tmp_path):
    """Свежая публикация home-файла не должна содержать блок repository."""
    committed = "max_comments: 5\nrepository:\n  index_branches: [evil]\n"
    result = migrate_repo_config(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert result.created is True
    published_text = result.path.read_text(encoding="utf-8")
    assert "repository" not in published_text
    assert "evil" not in published_text


def test_home_credential_key_is_recorded_as_skipped_layer(tmp_path):
    """PRI-234: пропуск домашнего слоя виден не только строкой в warnings."""
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [x]}\ngithub_token: t\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "max_comments: 5\n", config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [item.as_dict() for item in meta.skipped] == [
        {
            "layer": "home:repos/o/r.yml",
            "repo": "o/r",
            "ref": None,
            "category": "credential",
            "transport": None,
            "http_status": None,
        }
    ]


def test_invalid_home_policy_value_is_recorded_as_skipped_layer(tmp_path):
    # Значение взято из существующего
    # test_invalid_home_task_sync_filter_is_quarantined_or_rejected: оно
    # гарантированно даёт HomePolicyError.
    _write(
        tmp_path / "review.yml",
        "task_board:\n  project: PRI\n  sync_filter: {max_age_days: 0}\n",
    )

    _data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "max_comments: 5\n", config_root=tmp_path
    )

    assert [(item.layer, item.category) for item in meta.skipped] == [
        ("home:review.yml", "invalid")
    ]


def test_resolution_meta_as_dict_exposes_skipped_and_defaults_to_empty():
    meta = ResolutionMeta(sources={}, shadowed={}, warnings=())

    assert meta.skipped == ()
    assert meta.as_dict()["skipped"] == []


def test_committed_fetch_failure_keeps_both_home_layers(tmp_path):
    """PRI-234, критерий 2: сбой доставки коммиченного слоя не обнуляет home."""
    _write(tmp_path / "review.yml", "max_comments: 5\ncontext_limits: {graph: {hops: 2}}\n")
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [home-repo]}\ntask_board:\n")

    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    data, meta = resolve_policy_data("o/r", "main", boom, config_root=tmp_path)

    assert data["paths"] == {"ignore": ["home-repo"]}
    assert data["max_comments"] == 5
    assert data["context_limits"] == {"graph": {"hops": 2}}
    assert data["task_board"] is None
    assert len(meta.warnings) == 1
    assert [item.as_dict() for item in meta.skipped] == [
        {
            "layer": ".review.yml",
            "repo": "o/r",
            "ref": "main",
            "category": "unavailable",
            "transport": "unknown",
            "http_status": None,
        }
    ]


def test_committed_fetch_failure_is_loud_in_strict_committed_mode(tmp_path):
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [home-repo]}\n")

    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r", "main", boom, config_root=tmp_path, strict_committed=True
        )


def test_committed_http_failure_reports_status_and_transport(tmp_path):
    class _Response:
        status_code = 403

    class _HTTPStatusError(Exception):
        response = _Response()

    def boom(_ref):
        raise _HTTPStatusError("forbidden")

    _data, meta = resolve_policy_data("o/r", "sha123", boom, config_root=tmp_path)

    assert meta.skipped[0].transport == "http"
    assert meta.skipped[0].http_status == 403
    assert meta.skipped[0].ref == "sha123"


def test_malformed_committed_yaml_is_soft_and_categorized(tmp_path):
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "paths: [broken\n", config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [(item.layer, item.category) for item in meta.skipped] == [
        (".review.yml", "malformed")
    ]
    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda _ref: "paths: [broken\n",
            config_root=tmp_path,
            strict_committed=True,
        )


def test_absent_committed_layer_records_nothing(tmp_path):
    """Фетчер вернул None: «слоя нет» — не то же, что «слой не прочитан»."""
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: None, config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert meta.skipped == ()
    assert meta.warnings == ()


def test_committed_failure_diagnostic_never_echoes_url_or_token(tmp_path):
    secret = "do-not-echo-token-xyz"

    class _Request:
        url = f"https://api.example/repos/o/r?token={secret}"

    class _HTTPStatusError(Exception):
        request = _Request()

    def boom(_ref):
        raise _HTTPStatusError(f"GET {_Request.url} -> 500")

    _data, meta = resolve_policy_data("o/r", "main", boom, config_root=tmp_path)

    rendered = " ".join(meta.warnings) + repr([i.as_dict() for i in meta.skipped])
    assert secret not in rendered
    assert "https://" not in rendered

    with pytest.raises(HomeConfigError) as excinfo:
        resolve_policy_data(
            "o/r", "main", boom, config_root=tmp_path, strict_committed=True
        )
    assert secret not in str(excinfo.value)
    assert secret not in "".join(traceback.format_exception(excinfo.value))


def test_bare_valueerror_from_pyyaml_scalar_is_soft_and_categorized(tmp_path):
    """PyYAML бросает голый ValueError (не YAMLError) для невалидных implicit-скаляров."""
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "ignore_before: 2020-13-45\n", config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [(item.layer, item.category) for item in meta.skipped] == [
        (".review.yml", "malformed")
    ]


def test_bare_valueerror_from_pyyaml_scalar_in_home_layer_is_soft(tmp_path):
    """Тот же голый ValueError в home:review.yml мягко пропускается."""
    _write(tmp_path / "review.yml", "ignore_before: 2020-13-45\n")
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: None, config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [(item.layer, item.category) for item in meta.skipped] == [
        ("home:review.yml", "malformed")
    ]


def test_migration_is_loud_when_committed_layer_cannot_be_read(tmp_path):
    """Перенос неполной политики в home-файл необратим — миграция громкая."""
    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    with pytest.raises(HomeConfigError):
        migrate_repo_config(
            "o/r", "main", boom, config_root=tmp_path, settings=Settings(_env_file=None)
        )
