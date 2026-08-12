"""Жизненный цикл локальной инфраструктуры reviewer (docker compose).

Модуль отвечает только за *запуск и остановку* уже доставленного
docker-compose.yml. Доставка файла (скачивание, атомарная запись, сохранение
пользовательских правок) живёт в reviewer/update_lifecycle.py — у них разные
поводы для изменения.

Здесь исход только классифицируется; формулировки сообщений и код возврата —
ответственность CLI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reviewer.update_lifecycle import default_config_dir


COMPOSE_PROJECT = "rag-reviewer"
WAIT_TIMEOUT_SECONDS = 300

# Сигнатуры недоступного демона в stderr (сверка идёт по нижнему регистру).
# Не совпало — исход FAILED с сырым stderr: неизвестная ошибка должна быть
# видна, а не замаскирована под известную.
_DAEMON_SIGNATURES = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "docker daemon is not running",
    "error during connect",
)


class ComposeStatus(StrEnum):
    OK = "ok"
    COMPOSE_MISSING = "compose_missing"
    DOCKER_MISSING = "docker_missing"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class ComposeResult:
    """Исход одного вызова `docker compose`.

    `returncode` осмыслен только при `status in (OK, FAILED)` — это реальный
    код возврата процесса `docker compose`. При `COMPOSE_MISSING` и
    `DOCKER_MISSING` процесс вообще не запускался: там лежат заглушки (`0` и
    синтетический `127` соответственно), которых docker не возвращал. Вызывающий
    код обязан ветвиться по `status`, а не по `returncode`.
    """

    status: ComposeStatus
    returncode: int
    stdout: str
    stderr: str
    compose_path: Path


def compose_file_path(config_dir: Path | None = None) -> Path:
    """Путь к управляемому docker-compose.yml (уважает XDG_CONFIG_HOME)."""
    return (config_dir or default_config_dir()) / "docker-compose.yml"


def build_compose_argv(compose_path: Path, *arguments: str) -> list[str]:
    """Собирает argv docker compose с явным именем проекта.

    Имя задаётся явно, иначе docker выводит его из имени директории
    compose-файла: неявный побочный эффект пути, ломающийся при смене
    XDG_CONFIG_HOME и пересекающийся с проектом клона репозитория.
    """
    return [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "-f",
        str(compose_path),
        *arguments,
    ]


def _run_compose(
    arguments: tuple[str, ...],
    *,
    config_dir: Path | None,
    run: Callable,
) -> ComposeResult:
    compose_path = compose_file_path(config_dir)
    if not compose_path.is_file():
        return ComposeResult(ComposeStatus.COMPOSE_MISSING, 0, "", "", compose_path)
    try:
        result = run(
            build_compose_argv(compose_path, *arguments),
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return ComposeResult(
            ComposeStatus.DOCKER_MISSING, 127, "", str(error), compose_path
        )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode == 0:
        status = ComposeStatus.OK
    elif any(signature in stderr.lower() for signature in _DAEMON_SIGNATURES):
        status = ComposeStatus.DAEMON_UNAVAILABLE
    else:
        status = ComposeStatus.FAILED
    return ComposeResult(status, result.returncode, stdout, stderr, compose_path)


def start_services(
    *,
    config_dir: Path | None = None,
    run: Callable = subprocess.run,
) -> ComposeResult:
    """Поднимает ParadeDB и Neo4j и ждёт готовности их healthcheck."""
    return _run_compose(
        ("up", "-d", "--wait", "--wait-timeout", str(WAIT_TIMEOUT_SECONDS)),
        config_dir=config_dir,
        run=run,
    )


def stop_services(
    *,
    config_dir: Path | None = None,
    run: Callable = subprocess.run,
) -> ComposeResult:
    """Останавливает контейнеры, сохраняя named volumes.

    Именно `stop`, а не `down`: у `stop` нет флага `-v` в принципе, поэтому
    запрет из CLAUDE.md соблюдается по конструкции, а не договорённостью.
    """
    return _run_compose(("stop",), config_dir=config_dir, run=run)
