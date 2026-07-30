import json
from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod


def _install_fake_vcs(monkeypatch, committed):
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = committed
    vcs.close = MagicMock()
    components = MagicMock()
    monkeypatch.setattr(cli_mod, "build_components", lambda settings: components)
    monkeypatch.setattr(
        cli_mod.ReviewService,
        "_create_vcs_provider",
        lambda self, owner, name: vcs,
    )
    return vcs


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
    assert (tmp_path / "rag-reviewer/repos/o/r.yml").read_text(
        encoding="utf-8"
    ) == source
    assert "shadowed" in result.output


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
    assert destination.read_text(encoding="utf-8") == "max_comments: 3\n"
