# PRI-222 Runtime Web Container Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести host/port web-контейнера в runtime/deploy-слой, сохранив единый Python startup и opt-in Compose.

**Architecture:** Лёгкий `reviewer.web.serve` владеет созданием FastAPI app и запуском Uvicorn; Click-команда и Docker module CLI только получают host/port разными способами. Dockerfile не знает номер порта, Compose раздельно задаёт внутренний listen-port и loopback host mapping, а реальный integration smoke повторно использует один image на двух портах.

**Tech Stack:** Python 3.11+, argparse, Click, FastAPI/Uvicorn, Docker multi-stage build, Docker Compose, pytest, PyYAML.

---

## File Map

- Create `reviewer/web/serve.py`: единственный runner Uvicorn и лёгкий module CLI с args/env.
- Modify `reviewer/entrypoints/cli.py`: делегирование `reviewer serve` общему runner.
- Create `tests/web/test_serve.py`: unit-контракт args/env, runner и Click delegation.
- Modify `web/Dockerfile`: минимальные runtime dependencies и port-agnostic exec `CMD`.
- Modify `docker-compose.yml`: opt-in `web` profile с раздельными listen/publish ports.
- Modify `tests/test_infrastructure_policy.py`: декларативные guards Dockerfile и Compose.
- Modify `README.md`, `README.ru.md`: docker run и Compose runtime examples.
- Modify `tests/docs/test_readme_onboarding.py`: двуязычный documentation guard.
- Create `tests/web/test_container_smoke.py`: один image, два внутренних порта, HTTP smoke.

### Task 1: Единый web server runner

**Files:**
- Create: `reviewer/web/serve.py`
- Create: `tests/web/test_serve.py`
- Modify: `reviewer/entrypoints/cli.py:14-20,994-1005`

- [ ] **Step 1: Написать failing unit-тесты module CLI, runner и Click delegation**

Создать `tests/web/test_serve.py`:

```python
from __future__ import annotations

from click.testing import CliRunner
import pytest

from reviewer.entrypoints.cli import cli
from reviewer.web import serve


def test_module_cli_uses_container_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.delenv("REVIEWER_WEB_HOST", raising=False)
    monkeypatch.delenv("REVIEWER_WEB_PORT", raising=False)
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    serve.main([])

    assert calls == [("0.0.0.0", 8000)]


def test_module_cli_reads_env_and_arguments_override_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setenv("REVIEWER_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("REVIEWER_WEB_PORT", "8080")
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    serve.main(["--host", "127.0.0.2", "--port", "9090"])

    assert calls == [("127.0.0.2", 9090)]


def test_module_cli_rejects_invalid_env_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEWER_WEB_PORT", "not-a-port")

    with pytest.raises(SystemExit) as exc_info:
        serve.main([])

    assert exc_info.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_run_server_builds_app_and_passes_host_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = object()
    app = object()
    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr("reviewer.config.settings.Settings", lambda: settings)
    monkeypatch.setattr(
        "reviewer.web.app.create_app",
        lambda actual: app if actual is settings else None,
    )
    monkeypatch.setattr(
        "uvicorn.run",
        lambda actual, *, host, port: calls.append((actual, host, port)),
    )

    serve.run_server("127.0.0.2", 9090)

    assert calls == [(app, "127.0.0.2", 9090)]
    assert "http://127.0.0.2:9090" in capsys.readouterr().out


def test_click_serve_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0", "--port", "8080"])

    assert result.exit_code == 0
    assert calls == [("0.0.0.0", 8080)]
```

- [ ] **Step 2: Запустить тест и подтвердить RED**

Run: `.venv/bin/pytest tests/web/test_serve.py -q`

Expected: collection FAIL с `ImportError: cannot import name 'serve' from 'reviewer.web'`.

- [ ] **Step 3: Реализовать минимальный общий runner**

Создать `reviewer/web/serve.py`:

```python
"""Общий runtime-запуск web-админки для CLI и контейнера."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
CONTAINER_DEFAULT_HOST = "0.0.0.0"


def run_server(host: str, port: int) -> None:
    """Создать приложение и запустить Uvicorn на переданном runtime-адресе."""
    import uvicorn

    from reviewer.config.settings import Settings
    from reviewer.web.app import create_app

    app = create_app(Settings())
    print(f"Запуск веб-сервера на http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Запустить веб-админку reviewer")
    parser.add_argument(
        "--host",
        default=os.getenv("REVIEWER_WEB_HOST", CONTAINER_DEFAULT_HOST),
        help="Хост для uvicorn (env: REVIEWER_WEB_HOST)",
    )
    parser.add_argument(
        "--port",
        default=os.getenv("REVIEWER_WEB_PORT", str(DEFAULT_PORT)),
        type=int,
        help="Порт для uvicorn (env: REVIEWER_WEB_PORT)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Разобрать container args/env и запустить общий server runner."""
    args = _parser().parse_args(argv)
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
```

В `reviewer/entrypoints/cli.py` импортировать defaults рядом с `Settings`:

```python
from reviewer.web.serve import DEFAULT_HOST, DEFAULT_PORT
```

и заменить команду `serve`:

```python
@cli.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Хост для uvicorn")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int, help="Порт для uvicorn")
def serve(host: str, port: int) -> None:
    """Запустить веб-админку наблюдаемости (FastAPI + uvicorn)."""
    from reviewer.web.serve import run_server

    run_server(host, port)
```

- [ ] **Step 4: Запустить unit-тесты и линт, подтвердить GREEN**

Run: `.venv/bin/pytest tests/web/test_serve.py -q`

Expected: `5 passed`.

Run: `.venv/bin/ruff check reviewer/web/serve.py reviewer/entrypoints/cli.py tests/web/test_serve.py`

Expected: `All checks passed!`.

- [ ] **Step 5: Закоммитить общий startup**

```bash
git add reviewer/web/serve.py reviewer/entrypoints/cli.py tests/web/test_serve.py
git commit -m "refactor(web): объединить CLI и контейнерный startup (PRI-222)"
```

### Task 2: Port-agnostic Dockerfile и opt-in Compose

**Files:**
- Modify: `web/Dockerfile:18-28`
- Modify: `docker-compose.yml:17-59`
- Modify: `tests/test_infrastructure_policy.py:257-338`

- [ ] **Step 1: Добавить failing policy-тесты Dockerfile и Compose**

После `test_compose_documents_start_interval_docker_requirement` добавить:

```python
def test_web_dockerfile_delegates_port_to_runtime() -> None:
    root = Path(__file__).parents[1]
    dockerfile = (root / "web" / "Dockerfile").read_text(encoding="utf-8")

    assert "EXPOSE" not in dockerfile
    assert "8000" not in dockerfile
    assert 'CMD ["python", "-m", "reviewer.web.serve"]' in dockerfile
    assert '"psycopg-pool>=3.2"' in dockerfile


def test_compose_web_service_is_opt_in_with_separate_runtime_ports() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    web = compose["services"]["web"]

    assert web["profiles"] == ["web"]
    assert web["build"] == {"context": ".", "dockerfile": "web/Dockerfile"}
    assert web["environment"] == {
        "PG_DSN": "postgresql://reviewer:reviewer@paradedb:5432/reviewer",
        "REVIEWER_WEB_HOST": "0.0.0.0",
        "REVIEWER_WEB_PORT": "${REVIEWER_WEB_PORT:-8000}",
    }
    assert web["ports"] == [
        "127.0.0.1:${REVIEWER_WEB_PUBLISH_PORT:-8000}:${REVIEWER_WEB_PORT:-8000}"
    ]
    assert web["depends_on"] == ["paradedb"]
```

- [ ] **Step 2: Запустить policy-тесты и подтвердить RED**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py::test_web_dockerfile_delegates_port_to_runtime tests/test_infrastructure_policy.py::test_compose_web_service_is_opt_in_with_separate_runtime_ports -q`

Expected: первый тест FAIL на `EXPOSE`, второй FAIL с `KeyError: 'web'`.

- [ ] **Step 3: Сделать Dockerfile port-agnostic**

В runtime install добавить `psycopg-pool`, удалить `EXPOSE` и заменить `CMD`; итоговый backend tail:

```dockerfile
FROM python:3.12-slim AS backend
WORKDIR /app
# Только рантайм-зависимости веб-слоя (не весь reviewer)
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "psycopg[binary]>=3.2" \
    "psycopg-pool>=3.2" "pydantic-settings>=2.3"
# Исходники reviewer (импортируются только reviewer.config + reviewer.web; остальное лежит без импорта)
COPY reviewer/ ./reviewer/
# Собранный фронт из stage 1 — путь совпадает с _FRONTEND_DIST в reviewer/web/app.py
COPY --from=frontend /app/web/frontend/dist ./web/frontend/dist
ENV PYTHONPATH=/app
# create_app(Settings()) читает PG_DSN, host и port приходят только из runtime.
CMD ["python", "-m", "reviewer.web.serve"]
```

- [ ] **Step 4: Добавить opt-in Compose-сервис**

После `neo4j` и до test-profile header добавить:

```yaml
  web:
    # Web admin собирается и запускается только через явный `--profile web`.
    profiles: ["web"]
    build:
      context: .
      dockerfile: web/Dockerfile
    environment:
      PG_DSN: postgresql://reviewer:reviewer@paradedb:5432/reviewer
      REVIEWER_WEB_HOST: 0.0.0.0
      REVIEWER_WEB_PORT: ${REVIEWER_WEB_PORT:-8000}
    ports:
      - "127.0.0.1:${REVIEWER_WEB_PUBLISH_PORT:-8000}:${REVIEWER_WEB_PORT:-8000}"
    depends_on: ["paradedb"]
```

- [ ] **Step 5: Запустить policy и Compose checks, подтвердить GREEN**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py -q`

Expected: весь модуль PASS.

Run: `docker compose --profile web config --quiet`

Expected: exit code 0, пустой вывод.

Run: `docker compose config --services`

Expected: вывод содержит только `paradedb` и `neo4j`; `web` отсутствует.

Run: `.venv/bin/ruff check tests/test_infrastructure_policy.py`

Expected: `All checks passed!`.

- [ ] **Step 6: Закоммитить deploy-конфигурацию**

```bash
git add web/Dockerfile docker-compose.yml tests/test_infrastructure_policy.py
git commit -m "feat(web): вынести порты контейнера в runtime (PRI-222)"
```

### Task 3: Двуязычная документация runtime-портов

**Files:**
- Modify: `README.md:689-700`
- Modify: `README.ru.md:677-688`
- Modify: `tests/docs/test_readme_onboarding.py:226-232`

- [ ] **Step 1: Написать failing documentation guard**

Перед `test_readmes_document_configuration_ownership_and_scenarios` добавить:

```python
def test_readmes_document_web_container_runtime_ports() -> None:
    for filename, heading in (
        ("README.md", "### Web admin"),
        ("README.ru.md", "### Web admin"),
    ):
        section = _section(_read(filename), heading)
        for marker in (
            "docker build -f web/Dockerfile -t rag-reviewer-web .",
            "docker run --rm",
            "REVIEWER_WEB_PORT=8080",
            "127.0.0.1:18000:8080",
            "docker compose --profile web up -d web",
            "REVIEWER_WEB_PUBLISH_PORT=18000",
        ):
            assert marker in section, (filename, marker)
```

- [ ] **Step 2: Запустить guard и подтвердить RED**

Run: `.venv/bin/pytest tests/docs/test_readme_onboarding.py::test_readmes_document_web_container_runtime_ports -q`

Expected: FAIL на отсутствующей `docker build` команде.

- [ ] **Step 3: Дополнить английскую Web admin секцию**

После host-run code block в `README.md` добавить:

````markdown
The container keeps its internal listen port separate from the published loopback port. Build it
once and choose both at runtime (replace `database` with a Postgres host reachable from the
container):

```bash
docker build -f web/Dockerfile -t rag-reviewer-web .
docker run --rm \
  --env PG_DSN=postgresql://reviewer:reviewer@database:5432/reviewer \
  --env REVIEWER_WEB_PORT=8080 \
  --publish 127.0.0.1:18000:8080 \
  rag-reviewer-web
```

The Compose service is opt-in, so ordinary `docker compose up` still starts infrastructure only:

```bash
docker compose --profile web up -d web
REVIEWER_WEB_PORT=8080 REVIEWER_WEB_PUBLISH_PORT=18000 \
  docker compose --profile web up -d web
```

Without overrides, both the internal and published ports default to `8000`.
````

- [ ] **Step 4: Дополнить русскую Web admin секцию теми же командами**

После host-run code block в `README.ru.md` добавить:

````markdown
В контейнерном сценарии внутренний listen-port отделён от опубликованного loopback-порта. Образ
собирается один раз, оба порта выбираются при запуске (`database` замените на доступный из
контейнера Postgres host):

```bash
docker build -f web/Dockerfile -t rag-reviewer-web .
docker run --rm \
  --env PG_DSN=postgresql://reviewer:reviewer@database:5432/reviewer \
  --env REVIEWER_WEB_PORT=8080 \
  --publish 127.0.0.1:18000:8080 \
  rag-reviewer-web
```

Compose-сервис включается явно, поэтому обычный `docker compose up` по-прежнему запускает только
инфраструктуру:

```bash
docker compose --profile web up -d web
REVIEWER_WEB_PORT=8080 REVIEWER_WEB_PUBLISH_PORT=18000 \
  docker compose --profile web up -d web
```

Без переопределений внутренний и опубликованный порты равны `8000`.
````

- [ ] **Step 5: Запустить docs tests, подтвердить GREEN**

Run: `.venv/bin/pytest tests/docs/test_readme_onboarding.py -q`

Expected: весь модуль PASS, включая link/parity guards.

Run: `.venv/bin/ruff check tests/docs/test_readme_onboarding.py`

Expected: `All checks passed!`.

- [ ] **Step 6: Закоммитить документацию**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "docs(web): описать runtime-порты контейнера (PRI-222)"
```

### Task 4: Реальный smoke одного image на двух портах

**Files:**
- Create: `tests/web/test_container_smoke.py`

- [ ] **Step 1: Написать integration smoke**

Создать `tests/web/test_container_smoke.py`:

```python
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def web_image() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI недоступен")
    info = _docker("info", check=False)
    if info.returncode != 0:
        pytest.skip(f"Docker daemon недоступен: {info.stderr.strip()}")

    image = f"rag-reviewer-web-smoke:{uuid.uuid4().hex}"
    _docker("build", "-f", "web/Dockerfile", "-t", image, ".")
    try:
        yield image
    finally:
        _docker("image", "rm", "--force", image, check=False)


@pytest.mark.integration
@pytest.mark.parametrize("internal_port", [18080, 18081])
def test_same_image_serves_http_on_different_internal_ports(
    web_image: str,
    internal_port: int,
) -> None:
    name = f"reviewer-web-smoke-{uuid.uuid4().hex}"
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--env",
        "PG_DSN=postgresql://reviewer:reviewer@127.0.0.1:1/reviewer?connect_timeout=1",
        "--env",
        f"REVIEWER_WEB_PORT={internal_port}",
        "--publish",
        f"127.0.0.1::{internal_port}",
        web_image,
    )
    container_id = started.stdout.strip()
    try:
        published = _docker("port", container_id, f"{internal_port}/tcp").stdout.strip()
        host_port = int(published.rsplit(":", 1)[1])
        url = f"http://127.0.0.1:{host_port}/"
        last_error: Exception | None = None
        for _ in range(60):
            try:
                with urlopen(url, timeout=1) as response:
                    body = response.read()
                assert response.status == 200
                assert body
                break
            except URLError as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            logs = _docker("logs", container_id, check=False)
            pytest.fail(
                f"web image не ответил на {url}: {last_error}; logs:\n{logs.stdout}\n{logs.stderr}"
            )
    finally:
        _docker("rm", "--force", container_id, check=False)
```

- [ ] **Step 2: Запустить smoke и подтвердить оба варианта**

Run: `.venv/bin/pytest -q -m integration tests/web/test_container_smoke.py`

Expected: `2 passed`; в Docker build выполняется один раз, оба параметра используют один tag.

- [ ] **Step 3: Запустить линт smoke-теста**

Run: `.venv/bin/ruff check tests/web/test_container_smoke.py`

Expected: `All checks passed!`.

- [ ] **Step 4: Закоммитить smoke**

```bash
git add tests/web/test_container_smoke.py
git commit -m "test(web): проверить image на разных внутренних портах (PRI-222)"
```

### Task 5: Полная проверка и готовность к PR

**Files:**
- Verify all changed files

- [ ] **Step 1: Запустить целевые unit-тесты**

Run: `.venv/bin/pytest tests/web/test_serve.py tests/test_infrastructure_policy.py tests/docs/test_readme_onboarding.py -q`

Expected: PASS.

- [ ] **Step 2: Запустить весь unit suite**

Run: `.venv/bin/pytest -q`

Expected: PASS; integration tests исключены настройкой `pyproject.toml`.

- [ ] **Step 3: Запустить полный ruff**

Run: `.venv/bin/ruff check .`

Expected: `All checks passed!`.

- [ ] **Step 4: Повторить deploy и container checks**

Run: `docker compose --profile web config --quiet`

Expected: exit code 0.

Run: `docker compose config --services`

Expected: только `paradedb` и `neo4j`.

Run: `.venv/bin/pytest -q -m integration tests/web/test_container_smoke.py`

Expected: `2 passed` на одном повторно собранном image.

- [ ] **Step 5: Просмотреть итоговый diff и историю**

Run: `git status --short && git diff dev...HEAD --check && git log --oneline dev..HEAD`

Expected: нет незакоммиченных implementation-файлов, `git diff --check` без ошибок, история содержит docs/spec и четыре сфокусированных implementation-коммита.
