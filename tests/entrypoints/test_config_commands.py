import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod
from reviewer.config.settings import Settings


def _install_fake_vcs(monkeypatch, committed, *, branch: str = "main"):
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = committed
    vcs.close = MagicMock()
    components = SimpleNamespace(
        store=MagicMock(),
        graph=MagicMock(),
        task_store=MagicMock(),
        summary_store=MagicMock(),
    )
    monkeypatch.setattr(
        cli_mod, "Settings", lambda: Settings(_env_file=None, review_branches=branch)
    )
    monkeypatch.setattr(
        cli_mod, "build_components", lambda settings, **kwargs: components
    )
    monkeypatch.setattr(
        cli_mod.ReviewService,
        "_create_vcs_provider",
        lambda self, owner, name: vcs,
    )
    return vcs, components


def test_config_show_json_reports_effective_sources_and_shadowing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    global_path = tmp_path / "rag-reviewer/review.yml"
    repo_path = tmp_path / "rag-reviewer/repos/o/r.yml"
    global_path.parent.mkdir(parents=True)
    repo_path.parent.mkdir(parents=True)
    global_path.write_text("max_comments: 5\npaths: {ignore: [global]}\n", encoding="utf-8")
    repo_path.write_text("paths: {ignore: [home]}\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\npaths: {ignore: [repo]}\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "show", "--repo", "o/r", "--branch", "main", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective"]["max_comments"] == 7
    assert payload["effective"]["paths"]["ignore"] == ["home"]
    assert payload["sources"]["paths"] == "home:repos/o/r.yml"
    assert payload["shadowed"]["paths"] == ["home:review.yml", ".review.yml"]


def test_config_show_rejects_malformed_home_yaml_in_strict_mode(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = tmp_path / "rag-reviewer/review.yml"
    home.parent.mkdir(parents=True)
    home.write_text("[not-a-mapping]\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code != 0
    assert "home:review.yml" in result.output


def test_config_show_skips_credential_home_yaml_without_echoing_secret(
    monkeypatch, tmp_path
) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = tmp_path / "rag-reviewer/review.yml"
    home.parent.mkdir(parents=True)
    home.write_text(f"github_token: {secret}\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code == 0, result.output
    assert "credential key github_token" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)


def test_config_show_rejects_invalid_known_home_value_without_echoing_literal(
    monkeypatch, tmp_path
) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = tmp_path / "rag-reviewer/review.yml"
    home.parent.mkdir(parents=True)
    home.write_text(f"max_comments: {secret}\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    # Ошибка policy-слоя больше не роняет команду целиком (Task 6): секция
    # веток печатается, а policy-часть уходит в policy_error — но код возврата
    # остаётся ненулевым, чтобы `config show; echo $?` не терял сигнал об ошибке.
    assert result.exit_code != 0
    assert "branches:" in result.output
    assert "home:review.yml" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)


def test_config_migrate_creates_home_file_and_reports_shadowing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    source = "# repo policy\npaths: {ignore: [vendor]}\n"
    _install_fake_vcs(monkeypatch, source)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "migrate", "--repo", "o/r", "--branch", "main"],
    )

    assert result.exit_code == 0, result.output
    written = (tmp_path / "rag-reviewer/repos/o/r.yml").read_text(encoding="utf-8")
    # Policy-блок сохранён как есть; branch-миграция дописывает repository ниже.
    assert written.startswith(source)
    assert "repository:" in written
    assert "index_branches: [main]" in written
    assert "shadowed" in result.output
    assert "Ветки перенесены" in result.output


def test_config_migrate_refuses_conflicting_home_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    destination = tmp_path / "rag-reviewer/repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("max_comments: 3\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "migrate", "--repo", "o/r", "--branch", "main"],
    )

    assert result.exit_code != 0
    assert "max_comments" in result.output
    # Policy-часть падает на конфликте, но branch-миграция всё равно выполняется
    # и дописывается в тот же файл до того, как код возврата станет ненулевым.
    written = destination.read_text(encoding="utf-8")
    assert written.startswith("max_comments: 3\n")
    assert "index_branches: [main]" in written
    assert "Ветки перенесены" in result.output


def test_config_show_uses_default_branch_and_normalized_nested_repo(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    vcs, _ = _install_fake_vcs(monkeypatch, "max_comments: 7\n", branch="trunk")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "Group/Sub/Repo", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["repo"] == "group/sub/repo"
    vcs.get_file_at_ref.assert_called_once_with(".review.yml", "trunk")


def test_config_show_sanitizes_committed_yaml_and_closes_every_resource(
    monkeypatch, tmp_path
) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    vcs, components = _install_fake_vcs(monkeypatch, f"paths: [{secret}\n")
    vcs.close.side_effect = RuntimeError(secret)
    components.store.close.side_effect = RuntimeError(secret)
    components.graph.close.side_effect = RuntimeError(secret)

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    # Ошибка policy-слоя больше не роняет команду целиком (Task 6): секция
    # веток печатается, а policy-часть уходит в policy_error; ресурсы
    # закрываются как прежде — независимо от исхода policy-части. Код
    # возврата при этом остаётся ненулевым (сигнал внешним скриптам).
    assert result.exit_code != 0
    assert "branches:" in result.output
    assert ".review.yml" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)
    vcs.close.assert_called_once()
    for component in vars(components).values():
        component.close.assert_called_once()


def test_config_show_sanitizes_invalid_policy_value(monkeypatch, tmp_path) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _install_fake_vcs(monkeypatch, f"paths: [{secret}]\n")

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "show", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code != 0
    assert "branches:" in result.output
    assert "effective policy" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)


@pytest.mark.parametrize("as_json", [False, True])
def test_config_show_rejects_invalid_public_type_without_echoing_value(
    monkeypatch, tmp_path, as_json
) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _install_fake_vcs(monkeypatch, f"max_comments: {secret}\n")
    arguments = ["config", "show", "--repo", "o/r", "--branch", "main"]
    if as_json:
        arguments.append("--json")

    result = CliRunner().invoke(cli_mod.cli, arguments)

    assert result.exit_code != 0
    assert "branches" in result.output
    assert "effective policy" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)


def test_config_migrate_handles_missing_or_malformed_committed_policy(monkeypatch, tmp_path) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _install_fake_vcs(monkeypatch, None)

    missing = CliRunner().invoke(
        cli_mod.cli, ["config", "migrate", "--repo", "o/r", "--branch", "main"]
    )

    assert missing.exit_code != 0
    assert ".review.yml" in missing.output

    _install_fake_vcs(monkeypatch, f"max_comments: [{secret}\n")
    malformed = CliRunner().invoke(
        cli_mod.cli, ["config", "migrate", "--repo", "o/r", "--branch", "main"]
    )

    assert malformed.exit_code != 0
    assert ".review.yml" in malformed.output
    assert secret not in malformed.output
    assert secret not in repr(malformed.exception)


def test_config_migrate_reports_semantic_noop_with_default_branch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    destination = tmp_path / "rag-reviewer/repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("max_comments: 7\n", encoding="utf-8")
    vcs, _ = _install_fake_vcs(monkeypatch, "# comment\nmax_comments: 7\n", branch="trunk")

    result = CliRunner().invoke(cli_mod.cli, ["config", "migrate", "--repo", "o/r"])

    assert result.exit_code == 0, result.output
    assert "уже перенесён" in result.output
    vcs.get_file_at_ref.assert_called_with(".review.yml", "trunk")


def test_config_migrate_conflict_closes_every_resource_without_masking_error(
    monkeypatch, tmp_path
) -> None:
    secret = "do-not-echo"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    destination = tmp_path / "rag-reviewer/repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("max_comments: 3\n", encoding="utf-8")
    vcs, components = _install_fake_vcs(monkeypatch, "max_comments: 7\n")
    vcs.close.side_effect = RuntimeError(secret)
    components.task_store.close.side_effect = RuntimeError(secret)

    result = CliRunner().invoke(
        cli_mod.cli, ["config", "migrate", "--repo", "o/r", "--branch", "main"]
    )

    assert result.exit_code != 0
    assert "max_comments" in result.output
    assert secret not in result.output
    assert secret not in repr(result.exception)
    vcs.close.assert_called_once()
    for component in vars(components).values():
        component.close.assert_called_once()
