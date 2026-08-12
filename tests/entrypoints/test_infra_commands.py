from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod
from reviewer.compose_lifecycle import ComposeResult, ComposeStatus
from reviewer.launcher.metadata import COMMAND_PRESENTATION


def _result(status: ComposeStatus, *, returncode: int = 0, stderr: str = "") -> ComposeResult:
    return ComposeResult(
        status=status,
        returncode=returncode,
        stdout="",
        stderr=stderr,
        compose_path=Path("/home/user/.config/rag-reviewer/docker-compose.yml"),
    )


def test_start_reports_success_and_project_name(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "start_services", lambda: _result(ComposeStatus.OK))

    result = CliRunner().invoke(cli_mod.cli, ["start"])

    assert result.exit_code == 0
    assert "Инфраструктура запущена" in result.output
    assert "rag-reviewer" in result.output


def test_stop_reports_that_volumes_survived(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "stop_services", lambda: _result(ComposeStatus.OK))

    result = CliRunner().invoke(cli_mod.cli, ["stop"])

    assert result.exit_code == 0
    assert "тома и индекс сохранены" in result.output


def test_missing_compose_points_at_update_without_traceback(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod, "start_services", lambda: _result(ComposeStatus.COMPOSE_MISSING)
    )

    result = CliRunner().invoke(cli_mod.cli, ["start"])

    assert result.exit_code == 1
    assert "reviewer update" in result.output
    assert "docker-compose.yml" in result.output
    assert "Traceback" not in result.output


def test_missing_docker_binary_is_explained(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod, "start_services", lambda: _result(ComposeStatus.DOCKER_MISSING, returncode=127)
    )

    result = CliRunner().invoke(cli_mod.cli, ["start"])

    assert result.exit_code == 1
    assert "docker не найден в PATH" in result.output
    assert "Traceback" not in result.output


def test_unavailable_daemon_is_explained(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod,
        "start_services",
        lambda: _result(ComposeStatus.DAEMON_UNAVAILABLE, returncode=1),
    )

    result = CliRunner().invoke(cli_mod.cli, ["start"])

    assert result.exit_code == 1
    assert "демон не отвечает" in result.output


def test_unknown_failure_surfaces_code_and_stderr(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod,
        "stop_services",
        lambda: _result(ComposeStatus.FAILED, returncode=14, stderr="no such service"),
    )

    result = CliRunner().invoke(cli_mod.cli, ["stop"])

    assert result.exit_code == 1
    assert "14" in result.output
    assert "no such service" in result.output


def test_both_commands_are_registered_in_launcher_catalog() -> None:
    assert ("start",) in COMMAND_PRESENTATION
    assert ("stop",) in COMMAND_PRESENTATION
    assert "инфраструктуру" in COMMAND_PRESENTATION[("start",)].summary.lower()
