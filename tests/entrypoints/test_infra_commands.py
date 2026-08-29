from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
from neo4j.exceptions import ServiceUnavailable
import psycopg
import pytest

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


def test_start_warns_before_result_that_first_pull_takes_minutes(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "start_services", lambda: _result(ComposeStatus.OK))

    result = CliRunner().invoke(cli_mod.cli, ["start"])

    assert result.exit_code == 0
    assert "несколько минут" in result.output
    assert result.output.index("несколько минут") < result.output.index("Инфраструктура запущена")


def test_stop_reports_that_volumes_survived(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "stop_services", lambda: _result(ComposeStatus.OK))

    result = CliRunner().invoke(cli_mod.cli, ["stop"])

    assert result.exit_code == 0
    assert "тома и индекс сохранены" in result.output


def test_stop_warns_before_result(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "stop_services", lambda: _result(ComposeStatus.OK))

    result = CliRunner().invoke(cli_mod.cli, ["stop"])

    assert result.exit_code == 0
    assert "Останавливаю инфраструктуру" in result.output
    assert (
        result.output.index("Останавливаю инфраструктуру")
        < result.output.index("тома и индекс сохранены")
    )


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


def _settings(pg_dsn: str, neo4j_uri: str) -> SimpleNamespace:
    return SimpleNamespace(
        voyage_api_key="test-key",
        pg_dsn=pg_dsn,
        pg_pool_min_size=1,
        pg_pool_max_size=2,
        neo4j_uri=neo4j_uri,
        neo4j_user="neo4j",
        neo4j_password="password",
    )


class DeadChunkStore:
    def __init__(self, *args, **kwargs) -> None:
        raise psycopg.OperationalError("connection refused")


class DeadGraphStore:
    def __init__(self, *args, **kwargs) -> None:
        raise ServiceUnavailable("connection refused")


def _arrange_dead_storages(monkeypatch, pg_dsn: str, neo4j_uri: str) -> None:
    # classify_storage_failure (PRI-277) решает по ТИПУ исключения, поэтому
    # дублёры обязаны бросать те же классы, что реальные драйверы при
    # обрыве соединения — общий RuntimeError под "хранилище недоступно"
    # больше не маскируется.
    monkeypatch.setattr(cli_mod, "Settings", lambda: _settings(pg_dsn, neo4j_uri))
    monkeypatch.setattr(cli_mod, "ChunkStore", DeadChunkStore)
    monkeypatch.setattr(cli_mod, "GraphStore", DeadGraphStore)
    monkeypatch.setattr(cli_mod, "_check_vcs_providers", lambda settings: False)
    # _check_board_providers принимает board_projects keyword-only
    # (reviewer/entrypoints/cli.py:599-604) — мок глотает любые kwargs.
    monkeypatch.setattr(cli_mod, "_check_board_providers", lambda settings, **kwargs: False)


@pytest.mark.parametrize(
    "pg_dsn",
    [
        "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "postgresql://reviewer:reviewer@127.0.0.1:5433/reviewer",
    ],
)
def test_check_suggests_start_when_local_storages_are_down(monkeypatch, pg_dsn: str) -> None:
    _arrange_dead_storages(monkeypatch, pg_dsn, "neo4j://localhost:7687")

    result = CliRunner().invoke(cli_mod.cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" in result.output


def test_check_stays_silent_for_remote_storages(monkeypatch) -> None:
    _arrange_dead_storages(
        monkeypatch,
        "postgresql://reviewer:reviewer@db.internal:5432/reviewer",
        "neo4j://graph.internal:7687",
    )

    result = CliRunner().invoke(cli_mod.cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
