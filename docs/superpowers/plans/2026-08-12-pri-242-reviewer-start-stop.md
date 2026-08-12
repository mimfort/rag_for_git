# PRI-242 — `reviewer start` / `reviewer stop` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать `reviewer` две команды — `start` и `stop` — которые поднимают и останавливают локальную инфраструктуру (ParadeDB + Neo4j) из управляемого `~/.config/rag-reviewer/docker-compose.yml`, не удаляя тома.

**Architecture:** Новый модуль `reviewer/compose_lifecycle.py` собирает argv и классифицирует исход `docker compose`; `reviewer/entrypoints/cli.py` формулирует русские сообщения и код возврата. Готовность обеспечивается healthcheck'ами dev-сервисов в `docker-compose.yml` плюс `--wait --wait-timeout 300`.

**Tech Stack:** Python 3.11+ (`StrEnum`), Click, pytest, `subprocess.run` с инъекцией, PyYAML (только в тестах).

Спека: `docs/superpowers/specs/2026-08-12-pri-242-reviewer-start-stop-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Docstring каждой новой публичной функции — на русском.
- Коммиты: Conventional Commits на русском (`feat(cli): …`), **без self-attribution** (никаких `Co-Authored-By`, никаких упоминаний Claude).
- Ветка `feat/pri-242-reviewer-start-stop` уже создана; бриф и спека закоммичены (`e790697`). Работать в ней, в `dev` не коммитить.
- Все новые тесты — **unit**: сеть, docker и localhost-сокеты запрещены (`tests/infrastructure_policy.py` блокирует их на уровне сессии). Маркер `integration` не ставить.
- Реальный `subprocess.run` в тестах не вызывать никогда — только инъекция параметра `run`.
- `docker compose stop` не должен получать `down`, `-v` или `--volumes` ни при каких условиях (инвариант `CLAUDE.md`: dev- и test-сервисы делят Compose-проект, `-v` снесёт production named volumes).
- Каталог `plugin/` не трогать и версию в `pyproject.toml` не менять — иначе потребуется пересборка codex-манифестов, а это вне скоупа задачи.
- Прогон тестов: `.venv/bin/pytest -q` (по умолчанию исключает `integration`).
- Линт: `.venv/bin/ruff check <изменённые файлы>` — ruff по репозиторию в целом не чист, гнаться за repo-wide clean не нужно.

---

### Task 1: Модуль `compose_lifecycle` — argv и классификация исхода

**Files:**
- Create: `reviewer/compose_lifecycle.py`
- Test: `tests/test_compose_lifecycle.py`

**Interfaces:**
- Consumes: `default_config_dir()` из `reviewer/update_lifecycle.py:102-104`.
- Produces: `COMPOSE_PROJECT: str`, `WAIT_TIMEOUT_SECONDS: int`, `ComposeStatus` (StrEnum: `OK`, `COMPOSE_MISSING`, `DOCKER_MISSING`, `DAEMON_UNAVAILABLE`, `FAILED`), `ComposeResult` (frozen dataclass: `status`, `returncode`, `stdout`, `stderr`, `compose_path`), `compose_file_path(config_dir=None) -> Path`, `build_compose_argv(compose_path, *arguments) -> list[str]`, `start_services(*, config_dir=None, run=subprocess.run) -> ComposeResult`, `stop_services(*, config_dir=None, run=subprocess.run) -> ComposeResult`. Task 2 использует всё перечисленное.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_compose_lifecycle.py`:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewer.compose_lifecycle import (
    COMPOSE_PROJECT,
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
        "stop",
    ]


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
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/test_compose_lifecycle.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.compose_lifecycle'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `reviewer/compose_lifecycle.py`:

```python
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
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest -q tests/test_compose_lifecycle.py`
Expected: PASS, 11 passed

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/compose_lifecycle.py tests/test_compose_lifecycle.py
git add reviewer/compose_lifecycle.py tests/test_compose_lifecycle.py
git commit -m "feat(cli): модуль compose_lifecycle — argv и классификация исхода docker compose"
```

---

### Task 2: Команды `start` / `stop` и регистрация в launcher

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (импорты в шапке, новые команды после `check`)
- Modify: `reviewer/launcher/metadata.py:13-118` (две записи в `COMMAND_PRESENTATION`)
- Test: `tests/entrypoints/test_infra_commands.py`

**Interfaces:**
- Consumes: всё из Task 1 (`ComposeStatus`, `ComposeResult`, `COMPOSE_PROJECT`, `start_services`, `stop_services`).
- Produces: Click-команды `start` и `stop`; приватный хелпер `_report_compose_failure(result: ComposeResult) -> NoReturn` в `cli.py`.

Записи в `COMMAND_PRESENTATION` обязательны механически: `tests/launcher/test_catalog.py:145` (`test_current_commands_have_rich_metadata_without_orphans`) требует точного равенства множества презентаций и множества видимых команд — без них тест станет красным.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/entrypoints/test_infra_commands.py`:

```python
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
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/entrypoints/test_infra_commands.py`
Expected: FAIL — `AttributeError: module 'reviewer.entrypoints.cli' has no attribute 'start_services'`

- [ ] **Step 3: Добавить импорт в `reviewer/entrypoints/cli.py`**

В блок импортов `from reviewer...` (рядом с `from reviewer.config.settings import Settings`, порядок в файле не алфавитный — вставить одной группой):

```python
from reviewer.compose_lifecycle import (
    COMPOSE_PROJECT,
    ComposeResult,
    ComposeStatus,
    start_services,
    stop_services,
)
```

- [ ] **Step 4: Добавить команды после `check`**

Вставить сразу после тела команды `check` (после строки `click.echo("Готово к работе.")`, `reviewer/entrypoints/cli.py:842`):

```python
def _report_compose_failure(result: ComposeResult) -> None:
    """Печатает русское объяснение неуспеха и завершает процесс кодом 1."""
    if result.status is ComposeStatus.COMPOSE_MISSING:
        click.echo(f"✗ {result.compose_path} не найден — выполните reviewer update")
    elif result.status is ComposeStatus.DOCKER_MISSING:
        click.echo("✗ docker не найден в PATH — установите Docker")
    elif result.status is ComposeStatus.DAEMON_UNAVAILABLE:
        click.echo(
            "✗ docker установлен, но демон не отвечает — запустите Docker и повторите"
        )
    else:
        click.echo(f"✗ docker compose завершился с кодом {result.returncode}")
        if result.stderr.strip():
            click.echo(result.stderr.strip())
    raise SystemExit(1)


@cli.command()
def start() -> None:
    """Запустить локальную инфраструктуру (ParadeDB + Neo4j)."""
    result = start_services()
    if result.status is ComposeStatus.OK:
        click.echo(
            f"✓ Инфраструктура запущена (проект {COMPOSE_PROJECT}): ParadeDB, Neo4j"
        )
        return
    _report_compose_failure(result)


@cli.command()
def stop() -> None:
    """Остановить локальную инфраструктуру, сохранив тома и индекс."""
    result = stop_services()
    if result.status is ComposeStatus.OK:
        click.echo("✓ Инфраструктура остановлена; тома и индекс сохранены")
        return
    _report_compose_failure(result)
```

- [ ] **Step 5: Добавить записи в `reviewer/launcher/metadata.py`**

В словарь `COMMAND_PRESENTATION`, сохраняя алфавитный порядок ключей — `("start",)` между `("serve",)` и `("status",)`, `("stop",)` между `("status",)` и `("update",)`:

```python
    ("start",): CommandPresentation(
        summary="Запустить локальную инфраструктуру",
        details=(
            "Поднимает ParadeDB и Neo4j из управляемого docker-compose "
            "и ждёт готовности их healthcheck."
        ),
        effects=(Effect.NETWORK, Effect.WRITE),
        scenarios=("Перед индексацией", "После перезагрузки машины"),
        keywords=("docker", "compose", "postgres", "neo4j"),
    ),
    ("stop",): CommandPresentation(
        summary="Остановить локальную инфраструктуру",
        details=(
            "Останавливает контейнеры reviewer, сохраняя named volumes "
            "и построенный индекс."
        ),
        effects=(Effect.WRITE,),
        scenarios=("Освободить ресурсы машины",),
        keywords=("docker", "compose", "stop"),
    ),
```

`stop` помечен `WRITE`, а не `DESTRUCTIVE`: данные переживают команду, а `DESTRUCTIVE` в этом каталоге закреплён за `gc`, который действительно удаляет.

- [ ] **Step 6: Запустить тесты — новые и каталог launcher**

Run: `.venv/bin/pytest -q tests/entrypoints/test_infra_commands.py tests/launcher/`
Expected: PASS — 7 новых тестов зелёные, каталог launcher не сломан

- [ ] **Step 7: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py reviewer/launcher/metadata.py tests/entrypoints/test_infra_commands.py
git add reviewer/entrypoints/cli.py reviewer/launcher/metadata.py tests/entrypoints/test_infra_commands.py
git commit -m "feat(cli): команды reviewer start и reviewer stop"
```

---

### Task 3: Healthcheck dev-сервисов в compose

**Files:**
- Modify: `docker-compose.yml:2-16` (сервисы `paradedb` и `neo4j`)
- Test: `tests/test_infrastructure_policy.py` (новый тест рядом с `test_compose_defines_isolated_test_profile_services`, строка 257)

**Interfaces:**
- Consumes: существующий хелпер `_assert_cheap_idle_healthcheck(healthcheck, *, probe, min_interval)` (`tests/test_infrastructure_policy.py:234-254`) — он уже проверяет `retries == 3`, `start_interval <= 5`, `start_period >= 30`, `timeout <= 10`.
- Produces: ничего для других задач; правка нужна, чтобы `--wait` из Task 1 означал готовность, а не факт запуска.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_infrastructure_policy.py` сразу после `test_compose_defines_isolated_test_profile_services`:

```python
def test_compose_dev_services_expose_readiness_healthchecks() -> None:
    """Без healthcheck `up -d --wait` отдаёт готовность по состоянию running.

    Для сервиса без пробы docker считает его готовым, как только контейнер
    запущен, — то есть до того, как Postgres начинает принимать соединения.
    `reviewer start` в этот момент рапортовал бы об успехе неверно.
    """
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    paradedb = compose["services"]["paradedb"]
    assert "profiles" not in paradedb, "dev-сервис не должен быть за профилем"
    _assert_cheap_idle_healthcheck(
        paradedb["healthcheck"],
        probe=["CMD-SHELL", "pg_isready -U reviewer -d reviewer"],
        min_interval=30,
    )

    neo4j = compose["services"]["neo4j"]
    assert "profiles" not in neo4j
    _assert_cheap_idle_healthcheck(
        neo4j["healthcheck"],
        probe=["CMD-SHELL", "cypher-shell -u neo4j -p reviewerpass 'RETURN 1'"],
        min_interval=300,
    )
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest -q tests/test_infrastructure_policy.py::test_compose_dev_services_expose_readiness_healthchecks`
Expected: FAIL — `KeyError: 'healthcheck'`

- [ ] **Step 3: Добавить healthcheck в `docker-compose.yml`**

Сервис `paradedb` (после строки `volumes: ["paradedb_data:/var/lib/postgresql/"]`, с тем же отступом в 4 пробела):

```yaml
    # Без healthcheck `up -d --wait` (и `reviewer start`) считает сервис готовым
    # по состоянию running — до того, как Postgres примет первое соединение.
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U reviewer -d reviewer"]
      start_period: 180s
      start_interval: 2s
      interval: 30s
      timeout: 2s
      retries: 3
```

Сервис `neo4j` (после строки `volumes: ["neo4j_data:/data"]`):

```yaml
    # Редкий idle-interval: cypher-shell поднимает JVM (~2.5 c CPU за пробу),
    # поэтому частые пробы допустимы только в фазе старта — через start_interval.
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p reviewerpass 'RETURN 1'"]
      start_period: 180s
      start_interval: 2s
      interval: 300s
      timeout: 3s
      retries: 3
```

Креды в пробах — те же, что в `environment` этих сервисов (`docker-compose.yml:5-7,14`): `reviewer/reviewer` для Postgres, `neo4j/reviewerpass` для Neo4j.

- [ ] **Step 4: Запустить тесты compose целиком**

Run: `.venv/bin/pytest -q tests/test_infrastructure_policy.py`
Expected: PASS — новый тест зелёный, тесты test-профиля и digest-пиннинга не сломаны

- [ ] **Step 5: Коммит**

```bash
git add docker-compose.yml tests/test_infrastructure_policy.py
git commit -m "feat(compose): healthcheck dev-сервисов paradedb и neo4j"
```

---

### Task 4: Подсказка `reviewer start` в `check`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:769-815` (блоки Postgres и Neo4j в команде `check`) и блок импортов
- Test: `tests/entrypoints/test_infra_commands.py` (дополнение файла из Task 2)

**Interfaces:**
- Consumes: ничего из предыдущих задач.
- Produces: приватный хелпер `_is_loopback_endpoint(value: str) -> bool` в `cli.py`.

Подсказка печатается, только если недоступный endpoint — loopback. Деплою с удалёнными хранилищами совет поднять локальный docker заведомо не помогает и уводит диагностику в сторону; это уточнение сверх формулировки тикета зафиксировано в спеке.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/entrypoints/test_infra_commands.py`. Два импорта — `from types import SimpleNamespace` и `import pytest` — добавить **в шапку файла**, к существующим, а не перед новыми тестами:

```python
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


class DeadStore:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("connection refused")


def _arrange_dead_storages(monkeypatch, pg_dsn: str, neo4j_uri: str) -> None:
    monkeypatch.setattr(cli_mod, "Settings", lambda: _settings(pg_dsn, neo4j_uri))
    monkeypatch.setattr(cli_mod, "ChunkStore", DeadStore)
    monkeypatch.setattr(cli_mod, "GraphStore", DeadStore)
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
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest -q tests/entrypoints/test_infra_commands.py -k check`
Expected: FAIL — `assert "reviewer start" in result.output` не выполняется (подсказки ещё нет)

- [ ] **Step 3: Добавить хелпер в `reviewer/entrypoints/cli.py`**

Перед командой `check` (рядом с `_parse_board_projects`, `reviewer/entrypoints/cli.py:744`). Модуль `re` уже импортирован в шапке файла; добавить `from urllib.parse import urlsplit` в блок импортов стандартной библиотеки:

```python
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def _is_loopback_endpoint(value: str) -> bool:
    """Адресован ли DSN/URI локальной машине.

    Нужен, чтобы совет `reviewer start` не показывался деплою с удалёнными
    хранилищами: там локальный docker-стек ничего не чинит.
    """
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return False
    if host is None:
        match = re.search(r"host=([^\s]+)", value)
        host = match.group(1) if match else None
    return (host or "").lower() in _LOOPBACK_HOSTS
```

- [ ] **Step 4: Учитывать недоступность локальных хранилищ в `check`**

В теле `check` завести флаг рядом с `failed = False` (`reviewer/entrypoints/cli.py:759`):

```python
    local_storage_down = False
```

В `except` блока Postgres (`reviewer/entrypoints/cli.py:792-800`) — последней строкой ветки, после `failed = True`:

```python
        local_storage_down = local_storage_down or _is_loopback_endpoint(s.pg_dsn)
```

В `except` блока Neo4j (`reviewer/entrypoints/cli.py:813-815`) — после `failed = True`:

```python
        local_storage_down = local_storage_down or _is_loopback_endpoint(s.neo4j_uri)
```

Сразу после блока Neo4j, до проверки `scip-python`:

```python
    if local_storage_down:
        click.echo(
            "  Подсказка: локальные хранилища не отвечают — запустите reviewer start"
        )
```

Подсказка не меняет код возврата — она только объясняет уже напечатанную ошибку.

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/pytest -q tests/entrypoints/test_infra_commands.py`
Expected: PASS — 10 тестов (7 из Task 2 + 3 новых, считая параметризацию)

- [ ] **Step 6: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_infra_commands.py
git add reviewer/entrypoints/cli.py tests/entrypoints/test_infra_commands.py
git commit -m "feat(cli): check советует reviewer start при недоступных локальных хранилищах"
```

---

### Task 5: Документация (README EN + RU)

**Files:**
- Modify: `README.md:311-345` («Required services and credentials») и `README.md:536-548` («CLI reference»)
- Modify: `README.ru.md:316-349` («Сервисы и credentials») и `README.ru.md:540-552` («Справочник CLI»)

**Interfaces:**
- Consumes: поведение команд из Task 2 и healthcheck из Task 3.
- Produces: ничего для кода.

Оба README правятся синхронно — расхождение EN/RU считается дефектом документации в этом репозитории.

- [ ] **Step 1: Дополнить раздел сервисов в `README.md`**

Вставить перед абзацем `Prefer variables over editing the Compose file:` (`README.md:337`):

```markdown
`reviewer start` and `reviewer stop` manage that Compose file for you:

```bash
reviewer start   # up -d --wait, waits for the ParadeDB and Neo4j healthchecks
reviewer stop    # stops the containers; named volumes and the built index survive
```

Both run under the explicit Compose project `rag-reviewer`. A clone of this repository runs its
own stack under the project name `rag_for_git` — the two publish the same host ports and keep
separate volumes, so do not run them at the same time. Contributors working inside the clone
should keep using `docker compose up -d` there.

`reviewer stop` never removes volumes: it runs `docker compose stop`, which has no `-v` flag at
all.
```

Дописать в конец абзаца про `preserved` (`README.md:337-341`):

```markdown
A `preserved` Compose file also stops receiving new healthcheck definitions, so `reviewer start`
falls back to waiting for the `running` state instead of real readiness.
```

- [ ] **Step 2: Дополнить CLI reference в `README.md`**

В таблицу (`README.md:539-545`) добавить строку после `| Validate environment | `check` |`:

```markdown
| Manage local infrastructure | `start`, `stop` |
```

- [ ] **Step 3: Симметрично дополнить `README.ru.md`**

В разделе «Сервисы и credentials», перед абзацем про предпочтение переменных правке Compose-файла:

```markdown
`reviewer start` и `reviewer stop` управляют этим Compose-файлом:

```bash
reviewer start   # up -d --wait, ждёт готовности healthcheck ParadeDB и Neo4j
reviewer stop    # останавливает контейнеры; named volumes и построенный индекс сохраняются
```

Обе работают под явным именем Compose-проекта `rag-reviewer`. Клон этого репозитория поднимает
собственный стек под именем `rag_for_git` — они публикуют одни и те же хостовые порты и держат
разные тома, поэтому одновременно их запускать нельзя. Контрибьюторам внутри клона следует
по-прежнему пользоваться `docker compose up -d`.

`reviewer stop` не удаляет тома никогда: он выполняет `docker compose stop`, у которого флага
`-v` не существует.
```

И в конец абзаца про статус `preserved`:

```markdown
Файл со статусом `preserved` перестаёт получать и новые определения healthcheck, поэтому
`reviewer start` для него сводится к ожиданию состояния `running`, а не реальной готовности.
```

В таблицу «Справочник CLI» — строку после `| Проверка окружения | `check` |`:

```markdown
| Управление локальной инфраструктурой | `start`, `stop` |
```

- [ ] **Step 4: Проверить, что тесты документации не сломаны**

Run: `.venv/bin/pytest -q tests/docs/`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add README.md README.ru.md
git commit -m "docs(readme): команды reviewer start и reviewer stop"
```

---

### Task 6: Полная верификация и PR

**Files:** без изменений кода.

- [ ] **Step 1: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS — падений нет. Если что-то красное, чинить до перехода к следующему шагу; отчёт об успехе без зелёного прогона недопустим.

- [ ] **Step 2: Проверить, что новые команды видны в CLI**

Run: `.venv/bin/reviewer --help`
Expected: в списке команд присутствуют `start` и `stop` с русскими summary из докстрингов.

- [ ] **Step 3: Живая проверка на реальном docker (единственный шаг, требующий инфраструктуры)**

```bash
.venv/bin/reviewer stop && .venv/bin/reviewer start && .venv/bin/reviewer check
```

Expected: `stop` завершается кодом 0; `start` возвращается **только после** готовности сервисов, и немедленно следующий `check` показывает Postgres и Neo4j как доступные — без `connection refused` и без ретраев. Это и есть приёмка healthcheck: до Task 3 такой прогон ловил отказ соединения.

Внимание: команда остановит стек, поднятый из клона (`rag_for_git`), только если он был поднят под тем же именем проекта — это разные стеки. Перед шагом убедиться, что запущен ровно один из них.

- [ ] **Step 4: Создать PR в `dev`**

```bash
git push -u origin feat/pri-242-reviewer-start-stop
gh pr create --base dev --title "feat(cli): reviewer start и reviewer stop" --body "$(cat <<'EOF'
PRI-242 — управление локальной инфраструктурой из CLI.

- `reviewer/compose_lifecycle.py`: сборка argv `docker compose` с явным именем проекта
  `rag-reviewer` и классификация исхода (нет файла / нет docker / демон не отвечает / прочее).
- `reviewer start` — `up -d --wait --wait-timeout 300`; `reviewer stop` — `docker compose stop`,
  у которого нет флага `-v`, поэтому named volumes сохраняются по конструкции.
- Healthcheck dev-сервисов `paradedb` и `neo4j`: без них `--wait` отдавал готовность по
  состоянию `running`, то есть до первого принятого соединения.
- `reviewer check` советует `reviewer start`, но только для loopback-адресов.
- Регистрация обеих команд в каталоге launcher; README.md и README.ru.md синхронно.
EOF
)"
```

Тело PR — без self-attribution (никаких `Co-Authored-By` и упоминаний Claude).

- [ ] **Step 5: Закрыть задачу**

После создания PR предложить пользователю скилл `rag-reviewer:finish-task` — он допишет ссылку на PR в задачу и переведёт её в `Готово`.

---

## Замечания для исполнителя

- **Не запускать `docker compose --profile test down -v`** ни при каких обстоятельствах: dev- и test-сервисы делят Compose-проект, и команда удалит контейнеры разработки вместе с именованными томами. Безопасный аналог — `docker compose --profile test rm -sfv paradedb-test neo4j-test`.
- Реальный docker нужен ровно один раз — в Task 6, шаг 3. Все остальные шаги проходят на замоканном `run`.
- Рабочее дерево может делиться с параллельными сессиями: перед коммитом сверять `git log`, не переключать ветку.
