from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewer.compose_lifecycle import (
    COMPOSE_PROJECT,
    STOP_PROFILES,
    ComposeStatus,
    build_compose_argv,
    compose_file_path,
    start_services,
    stop_services,
)


class RecordingRun:
    """Мок subprocess.run: пишет вызовы и отдаёт заранее заданный результат."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return self.result


class ExplodingRun:
    """Мок subprocess.run, имитирующий отсутствие docker в PATH."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        raise FileNotFoundError(2, "No such file or directory: 'docker'")


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


def test_start_builds_exact_argv_with_explicit_project_and_wait(config_dir: Path) -> None:
    run = RecordingRun()

    result = start_services(config_dir=config_dir, run=run)

    assert result.status is ComposeStatus.OK
    assert run.calls[0][0] == [
        "docker",
        "compose",
        "-p",
        "rag-reviewer",
        "-f",
        str(config_dir / "docker-compose.yml"),
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "300",
    ]


def test_stop_builds_exact_argv(config_dir: Path) -> None:
    run = RecordingRun()

    result = stop_services(config_dir=config_dir, run=run)

    assert result.status is ComposeStatus.OK
    assert run.calls[0][0] == [
        "docker",
        "compose",
        "-p",
        "rag-reviewer",
        "-f",
        str(config_dir / "docker-compose.yml"),
        "--profile",
        "web",
        "stop",
    ]


def test_stop_selects_profiles_before_the_subcommand(config_dir: Path) -> None:
    """Без явного профиля запущенная админка переживает «остановку инфраструктуры».

    Флаг `--profile` — опция самой `docker compose`, поэтому он обязан стоять до
    подкоманды: после `stop` docker разберёт его как аргумент подкоманды.
    """
    run = RecordingRun()

    stop_services(config_dir=config_dir, run=run)

    argv = run.calls[0][0]
    for profile in STOP_PROFILES:
        assert argv.index("--profile") < argv.index("stop")
        assert argv[argv.index(profile) - 1] == "--profile"
    # Тестовые сервисы поднимает клон в своём Compose-проекте — их профиль не наш.
    assert "test" not in argv


def test_start_does_not_select_extra_profiles(config_dir: Path) -> None:
    """`start` поднимает только хранилища: админка — отдельное явное решение."""
    run = RecordingRun()

    start_services(config_dir=config_dir, run=run)

    assert "--profile" not in run.calls[0][0]


def test_stop_never_removes_containers_or_volumes(config_dir: Path) -> None:
    """Инвариант CLAUDE.md: dev и test делят Compose-проект, `-v` снесёт production-тома."""
    run = RecordingRun()

    stop_services(config_dir=config_dir, run=run)

    argv = run.calls[0][0]
    assert "down" not in argv
    assert "-v" not in argv
    assert "--volumes" not in argv


def test_missing_compose_file_does_not_invoke_docker(tmp_path: Path) -> None:
    run = RecordingRun()

    result = start_services(config_dir=tmp_path, run=run)

    assert result.status is ComposeStatus.COMPOSE_MISSING
    assert result.compose_path == tmp_path / "docker-compose.yml"
    assert run.calls == []


def test_missing_docker_binary_is_classified(config_dir: Path) -> None:
    run = ExplodingRun()

    result = start_services(config_dir=config_dir, run=run)

    assert result.status is ComposeStatus.DOCKER_MISSING
    assert run.calls, "docker должен быть вызван — файл на месте"


@pytest.mark.parametrize(
    "stderr",
    [
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        "error during connect: this error may indicate that the docker daemon is not running",
        "Is the docker daemon running?",
    ],
)
def test_daemon_signatures_are_classified(config_dir: Path, stderr: str) -> None:
    run = RecordingRun(returncode=1, stderr=stderr)

    result = start_services(config_dir=config_dir, run=run)

    assert result.status is ComposeStatus.DAEMON_UNAVAILABLE


def test_unknown_failure_is_not_masked_as_daemon(config_dir: Path) -> None:
    run = RecordingRun(returncode=14, stderr="no such service: paradedb")

    result = start_services(config_dir=config_dir, run=run)

    assert result.status is ComposeStatus.FAILED
    assert result.returncode == 14
    assert result.stderr == "no such service: paradedb"


def test_start_is_idempotent_for_the_caller(config_dir: Path) -> None:
    run = RecordingRun()

    first = start_services(config_dir=config_dir, run=run)
    second = start_services(config_dir=config_dir, run=run)

    assert first.status is second.status is ComposeStatus.OK
    assert run.calls[0][0] == run.calls[1][0]


def test_compose_file_path_follows_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert compose_file_path() == tmp_path / "rag-reviewer" / "docker-compose.yml"


def test_build_compose_argv_uses_public_project_constant(tmp_path: Path) -> None:
    argv = build_compose_argv(tmp_path / "docker-compose.yml", "ps")

    assert argv[:4] == ["docker", "compose", "-p", COMPOSE_PROJECT]
    assert argv[-1] == "ps"
