# PRI-211 Test Infrastructure Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the default unit suite fail immediately on real network/database access and route every integration database client to isolated disposable ParadeDB and Neo4j services.

**Architecture:** Pytest owns the safety boundary: `pytest-socket` blocks Python sockets for unit tests, test-only guards stop C-backed psycopg/Neo4j clients before connection, and integration tests receive only validated `TEST_*` endpoints. Docker Compose provides separate profile-only services, while `ChunkStore.clear` becomes repo-scoped so destructive examples cannot reappear.

**Tech Stack:** Python 3.11-3.13, pytest 8+, pytest-socket 0.8, pydantic-settings, psycopg 3/psycopg-pool, Neo4j Python driver, Docker Compose, GitHub Actions, Ruff.

---

Design: `docs/superpowers/specs/2026-07-16-pri-211-test-infrastructure-isolation-design.md`

Brief: `docs/superpowers/briefs/2026-07-16-PRI-211-isolate-tests-from-infrastructure.md`

## Scope Check

The socket policy, database allowlist, disposable services, and scoped cleanup are coupled layers of
one safety boundary. Splitting them into separate plans would leave intermediate states that either
block all integration tests or still expose working data, so they stay in one plan with independent
commits.

## File Map

- Create `tests/infrastructure_policy.py`: test-only endpoint parsing, validation, and DB driver guards.
- Modify `tests/conftest.py`: map `integration` to socket permission and install the correct DB policy.
- Create `tests/test_infrastructure_policy.py`: unit and integration regression tests for the boundary.
- Modify `pyproject.toml` and `uv.lock`: add and lock `pytest-socket`; disable sockets by default.
- Modify `tests/web/test_history.py`: replace two real-network unit tests with deterministic failures.
- Modify `docker-compose.yml`: add profile-only `paradedb-test` and `neo4j-test` services.
- Modify `.env.example`: document the four test-only endpoints.
- Modify `tests/install/test_install_wizard.py`: preserve the production template mirror invariant while allowing the explicit test-only keys.
- Modify `reviewer/index/store.py`: remove global `TRUNCATE` and require a repo in `ChunkStore.clear`.
- Create `tests/index/test_store_clear.py`: verify SQL scope and statically reject no-arg callers in skipped integration files.
- Modify `tests/index/test_store_hybrid.py`: central scoped fixture and repo-filtered SQL assertions.
- Modify `tests/index/test_migrate_base.py`: scoped repo/index metadata cleanup.
- Modify `tests/integration/test_pipeline.py`: scoped cleanup and component close in `finally`.
- Modify `tests/index/test_status_meta.py`: explicit `test/` repo and metadata cleanup.
- Modify `tests/index/test_summary_store.py`: remove collection-time DSN capture and harden cleanup.
- Modify `.github/workflows/publish.yml`: add a job timeout without adding infrastructure services.
- Modify `README.md`, `README.ru.md`, and `CLAUDE.md`: document unit and isolated integration workflows.
- Modify integration test module docstrings that still instruct users to start the production profile.

### Task 1: Enforce the Unit Network and Database Policy

**Files:**
- Create: `tests/infrastructure_policy.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_infrastructure_policy.py`
- Modify: `tests/web/test_history.py:81-118`
- Modify: `pyproject.toml:54-60,76-79`
- Modify: `uv.lock`

- [ ] **Step 1: Write the failing policy tests**

Create `tests/test_infrastructure_policy.py` with the following tests. The integration-marked tests
are intentionally skipped by the default marker expression and are exercised in Task 2.

```python
from __future__ import annotations

import socket

import psycopg
import pytest
from neo4j import GraphDatabase
from psycopg_pool import ConnectionPool
from pytest_socket import SocketBlockedError, SocketConnectBlockedError

from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.store import ChunkStore
from tests.infrastructure_policy import (
    InfrastructureTestSettings,
    InfrastructureSafetyError,
    load_test_endpoints,
)


def test_unit_create_connection_is_blocked() -> None:
    with pytest.raises((SocketBlockedError, SocketConnectBlockedError)):
        socket.create_connection(("127.0.0.1", 9), timeout=60)


def test_unit_psycopg_module_connect_is_blocked() -> None:
    with pytest.raises(pytest.fail.Exception, match="unit"):
        psycopg.connect("postgresql://user:pass@127.0.0.1:1/db")


def test_unit_psycopg_class_connect_is_blocked() -> None:
    with pytest.raises(pytest.fail.Exception, match="unit"):
        psycopg.Connection.connect("postgresql://user:pass@127.0.0.1:1/db")


def test_unit_pool_constructor_is_blocked_before_workers_start() -> None:
    with pytest.raises(pytest.fail.Exception, match="unit"):
        ConnectionPool("postgresql://user:pass@127.0.0.1:1/db", open=False)


def test_unit_chunk_store_fails_before_pool_timeout() -> None:
    store = ChunkStore("postgresql://user:pass@127.0.0.1:1/db")
    with pytest.raises(pytest.fail.Exception, match="unit"):
        store.init_schema()


def test_unit_graph_store_fails_before_driver_handshake() -> None:
    with pytest.raises(pytest.fail.Exception, match="unit"):
        GraphStore("neo4j://127.0.0.1:1", "neo4j", "password")


def test_test_postgres_must_differ_from_production() -> None:
    dsn = "postgresql://reviewer_test:secret@localhost:55433/reviewer_test?connect_timeout=2"
    production = Settings(pg_dsn=dsn, neo4j_uri="neo4j://localhost:7687")
    raw = InfrastructureTestSettings(
        test_pg_dsn=dsn,
        test_neo4j_uri="neo4j://localhost:17687",
        test_neo4j_user="neo4j",
        test_neo4j_password="secret",
    )

    with pytest.raises(InfrastructureSafetyError, match="PG_DSN"):
        load_test_endpoints(production, raw)


def test_test_postgres_database_requires_test_suffix() -> None:
    production = Settings(
        pg_dsn="postgresql://reviewer:reviewer@localhost:5433/reviewer",
        neo4j_uri="neo4j://localhost:7687",
    )
    raw = InfrastructureTestSettings(
        test_pg_dsn="postgresql://u:p@localhost:55433/reviewer?connect_timeout=2",
        test_neo4j_uri="neo4j://localhost:17687",
        test_neo4j_user="neo4j",
        test_neo4j_password="secret",
    )

    with pytest.raises(InfrastructureSafetyError, match="_test"):
        load_test_endpoints(production, raw)


def test_test_neo4j_must_differ_from_production() -> None:
    production = Settings(
        pg_dsn="postgresql://reviewer:reviewer@localhost:5433/reviewer",
        neo4j_uri="neo4j://localhost:17687",
    )
    raw = InfrastructureTestSettings(
        test_pg_dsn=(
            "postgresql://reviewer_test:secret@localhost:55433/"
            "reviewer_test?connect_timeout=2"
        ),
        test_neo4j_uri="neo4j://127.0.0.1:17687",
        test_neo4j_user="neo4j",
        test_neo4j_password="secret",
    )

    with pytest.raises(InfrastructureSafetyError, match="NEO4J_URI"):
        load_test_endpoints(production, raw)


@pytest.mark.integration
def test_integration_routes_settings_and_enables_socket() -> None:
    settings = Settings()
    raw = InfrastructureTestSettings()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.close()

    assert settings.pg_dsn == raw.test_pg_dsn
    assert settings.neo4j_uri == raw.test_neo4j_uri
    assert settings.neo4j_user == raw.test_neo4j_user
    assert settings.neo4j_password == raw.test_neo4j_password


@pytest.mark.integration
def test_integration_rejects_direct_postgres_outside_allowlist() -> None:
    with pytest.raises(pytest.fail.Exception, match="TEST_PG_DSN"):
        psycopg.connect("postgresql://reviewer:reviewer@localhost:5433/reviewer")


@pytest.mark.integration
def test_integration_rejects_pool_outside_allowlist() -> None:
    with pytest.raises(pytest.fail.Exception, match="TEST_PG_DSN"):
        ConnectionPool("postgresql://reviewer:reviewer@localhost:5433/reviewer", open=False)


@pytest.mark.integration
def test_integration_rejects_neo4j_outside_allowlist() -> None:
    with pytest.raises(pytest.fail.Exception, match="TEST_NEO4J"):
        GraphDatabase.driver(
            "neo4j://localhost:7687",
            auth=("neo4j", "reviewerpass"),
        )
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run:

```bash
.venv/bin/pytest -q tests/test_infrastructure_policy.py -m "not integration"
```

Expected: collection fails because `pytest_socket` and `tests.infrastructure_policy` do not exist.
No database or network service should be contacted.

- [ ] **Step 3: Add the socket dependency and default socket policy**

Change the dev extra and pytest options in `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "pytest-socket>=0.8,<0.9",
    "ruff>=0.5",
    "grpcio-tools>=1.64",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: требует изолированный Docker Compose profile test"]
addopts = "--disable-socket -m 'not integration'"
```

Regenerate the lock and install the updated dev environment:

```bash
uv lock
uv sync --extra dev --extra web
```

Expected: `uv.lock` contains `pytest-socket` and `.venv/bin/python -c 'import pytest_socket'` exits
with status 0.

- [ ] **Step 4: Implement endpoint validation and driver guards**

Create `tests/infrastructure_policy.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import psycopg
import pytest
from neo4j import GraphDatabase
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic_settings import BaseSettings, SettingsConfigDict

from reviewer.config.settings import Settings

_UNIT_DENIED = (
    "Сеть и инфраструктурные БД запрещены в unit-тесте. "
    "Добавьте @pytest.mark.integration и используйте TEST_* endpoints."
)
_START_TEST_PROFILE = (
    "docker compose --profile test up -d --wait paradedb-test neo4j-test"
)
_CONNECT_CONTROL_KWARGS = {
    "autocommit",
    "prepare_threshold",
    "context",
    "row_factory",
    "cursor_factory",
}

_REAL_PSYCOPG_CONNECT = psycopg.connect
_REAL_CONNECTION_CONNECT = psycopg.Connection.connect.__func__
_REAL_POOL_INIT = ConnectionPool.__init__
_REAL_POOL_GETCONN = ConnectionPool.getconn
_REAL_NEO4J_DRIVER = GraphDatabase.driver.__func__


class InfrastructureSafetyError(RuntimeError):
    """Test infrastructure points outside the isolated test boundary."""


class InfrastructureTestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Settings.model_config["env_file"],
        extra="ignore",
    )

    test_pg_dsn: str = (
        "postgresql://reviewer_test:reviewer_test@localhost:55433/"
        "reviewer_test?connect_timeout=2"
    )
    test_neo4j_uri: str = "neo4j://localhost:17687"
    test_neo4j_user: str = "neo4j"
    test_neo4j_password: str = "reviewer_test_pass"


@dataclass(frozen=True)
class PgTarget:
    host: str
    port: str
    database: str
    user: str
    password: str

    @property
    def location(self) -> tuple[str, str, str]:
        return self.host, self.port, self.database


@dataclass(frozen=True)
class Neo4jTarget:
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class TestEndpoints:
    pg_dsn: str
    pg: PgTarget
    neo4j_uri: str
    neo4j: Neo4jTarget
    neo4j_user: str
    neo4j_password: str


def _host(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    return normalized


def _pg_config(
    conninfo: str,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    if not isinstance(conninfo, str):
        raise InfrastructureSafetyError("Postgres conninfo должен быть статической строкой.")
    values = {key: value for key, value in (overrides or {}).items() if value is not None}
    if any(callable(value) for value in values.values()):
        raise InfrastructureSafetyError("Callable Postgres параметры запрещены в тестах.")
    try:
        config = conninfo_to_dict(conninfo, **values)
    except Exception as exc:
        raise InfrastructureSafetyError("Некорректный Postgres conninfo.") from exc
    if config.get("service"):
        raise InfrastructureSafetyError("Postgres service= запрещён в тестовом DSN.")
    if any("," in config.get(key, "") for key in ("host", "hostaddr", "port")):
        raise InfrastructureSafetyError("Multi-host Postgres DSN запрещён в тестах.")
    return config


def _pg_target(
    conninfo: str,
    overrides: Mapping[str, Any] | None = None,
) -> PgTarget:
    config = _pg_config(conninfo, overrides)
    return PgTarget(
        host=_host(config.get("hostaddr") or config.get("host")),
        port=config.get("port", "5432"),
        database=config.get("dbname", ""),
        user=config.get("user", ""),
        password=config.get("password", ""),
    )


def _neo4j_target(uri: str) -> Neo4jTarget:
    parsed = urlsplit(uri)
    if not parsed.scheme or not parsed.hostname:
        raise InfrastructureSafetyError("Некорректный TEST_NEO4J_URI.")
    return Neo4jTarget(
        scheme=parsed.scheme.lower(),
        host=_host(parsed.hostname),
        port=parsed.port or 7687,
    )


def load_test_endpoints(
    production: Settings,
    raw: InfrastructureTestSettings | None = None,
) -> TestEndpoints:
    raw = raw or InfrastructureTestSettings()
    pg = _pg_target(raw.test_pg_dsn)
    production_pg = _pg_target(production.pg_dsn)
    neo4j = _neo4j_target(raw.test_neo4j_uri)
    production_neo4j = _neo4j_target(production.neo4j_uri)
    pg_config = _pg_config(raw.test_pg_dsn)

    if not pg.host or not pg.user or not pg.password:
        raise InfrastructureSafetyError("TEST_PG_DSN должен содержать host/user/password.")
    if not pg.database.endswith("_test"):
        raise InfrastructureSafetyError("TEST_PG_DSN должен указывать на БД с суффиксом _test.")
    if pg.location == production_pg.location:
        raise InfrastructureSafetyError("TEST_PG_DSN совпадает с текущим PG_DSN.")
    timeout = int(pg_config.get("connect_timeout", "0"))
    if timeout < 1 or timeout > 5:
        raise InfrastructureSafetyError("TEST_PG_DSN требует connect_timeout от 1 до 5 секунд.")
    if neo4j == production_neo4j:
        raise InfrastructureSafetyError("TEST_NEO4J_URI совпадает с текущим NEO4J_URI.")
    if not raw.test_neo4j_user or not raw.test_neo4j_password:
        raise InfrastructureSafetyError(
            "TEST_NEO4J_USER и TEST_NEO4J_PASSWORD обязательны."
        )

    return TestEndpoints(
        pg_dsn=raw.test_pg_dsn,
        pg=pg,
        neo4j_uri=raw.test_neo4j_uri,
        neo4j=neo4j,
        neo4j_user=raw.test_neo4j_user,
        neo4j_password=raw.test_neo4j_password,
    )


def apply_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: TestEndpoints,
) -> None:
    monkeypatch.setenv("PG_DSN", endpoints.pg_dsn)
    monkeypatch.setenv("NEO4J_URI", endpoints.neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", endpoints.neo4j_user)
    monkeypatch.setenv("NEO4J_PASSWORD", endpoints.neo4j_password)


def _fail_unit(*_args: Any, **_kwargs: Any) -> None:
    pytest.fail(_UNIT_DENIED, pytrace=False)


def install_unit_db_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_connection_connect(cls: type, *_args: Any, **_kwargs: Any) -> None:
        _fail_unit()

    def deny_driver(cls: type, *_args: Any, **_kwargs: Any) -> None:
        _fail_unit()

    monkeypatch.setattr(psycopg, "connect", _fail_unit)
    monkeypatch.setattr(
        psycopg.Connection,
        "connect",
        classmethod(deny_connection_connect),
    )
    monkeypatch.setattr(ConnectionPool, "__init__", _fail_unit)
    monkeypatch.setattr(GraphDatabase, "driver", classmethod(deny_driver))


def _connection_overrides(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in kwargs.items()
        if key not in _CONNECT_CONTROL_KWARGS and value is not None
    }


def install_integration_db_guards(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: TestEndpoints,
) -> None:
    def ensure_pg(conninfo: str, overrides: Mapping[str, Any] | None = None) -> None:
        if _pg_target(conninfo, overrides) != endpoints.pg:
            pytest.fail(
                "Integration-тест попытался подключиться вне TEST_PG_DSN.",
                pytrace=False,
            )

    def guarded_connect(conninfo: str = "", *args: Any, **kwargs: Any):
        ensure_pg(conninfo, _connection_overrides(kwargs))
        kwargs.setdefault("connect_timeout", 2)
        try:
            return _REAL_PSYCOPG_CONNECT(conninfo, *args, **kwargs)
        except psycopg.OperationalError as exc:
            pytest.fail(
                f"Test ParadeDB недоступен; запустите: {_START_TEST_PROFILE}",
                pytrace=False,
            )
            raise AssertionError from exc

    def guarded_connection_connect(
        cls: type,
        conninfo: str = "",
        **kwargs: Any,
    ):
        ensure_pg(conninfo, _connection_overrides(kwargs))
        kwargs.setdefault("connect_timeout", 2)
        try:
            return _REAL_CONNECTION_CONNECT(cls, conninfo, **kwargs)
        except psycopg.OperationalError as exc:
            pytest.fail(
                f"Test ParadeDB недоступен; запустите: {_START_TEST_PROFILE}",
                pytrace=False,
            )
            raise AssertionError from exc

    def guarded_pool_init(
        pool: ConnectionPool,
        conninfo: str = "",
        *args: Any,
        **options: Any,
    ) -> None:
        pool_kwargs = options.get("kwargs") or {}
        if callable(conninfo) or callable(pool_kwargs):
            pytest.fail("Callable pool endpoints запрещены; используйте TEST_PG_DSN.")
        connection_kwargs = dict(pool_kwargs)
        ensure_pg(conninfo, connection_kwargs)
        connection_kwargs.setdefault("connect_timeout", 2)
        options["kwargs"] = connection_kwargs
        options["timeout"] = min(float(options.get("timeout", 3.0)), 3.0)
        options["reconnect_timeout"] = min(
            float(options.get("reconnect_timeout", 2.0)),
            2.0,
        )
        _REAL_POOL_INIT(pool, conninfo, *args, **options)

    def guarded_getconn(pool: ConnectionPool, *args: Any, **kwargs: Any):
        try:
            return _REAL_POOL_GETCONN(pool, *args, **kwargs)
        except PoolTimeout as exc:
            pytest.fail(
                f"Test ParadeDB недоступен; запустите: {_START_TEST_PROFILE}",
                pytrace=False,
            )
            raise AssertionError from exc

    def guarded_driver(
        cls: type,
        uri: str,
        *,
        auth: Any = None,
        **config: Any,
    ):
        expected_auth = (endpoints.neo4j_user, endpoints.neo4j_password)
        if _neo4j_target(uri) != endpoints.neo4j:
            pytest.fail(
                "Integration-тест попытался подключиться вне TEST_NEO4J_URI.",
                pytrace=False,
            )
        if auth != expected_auth:
            pytest.fail(
                "Integration-тест передал не TEST_NEO4J credentials.",
                pytrace=False,
            )
        config.setdefault("connection_timeout", 3.0)
        return _REAL_NEO4J_DRIVER(cls, uri, auth=auth, **config)

    monkeypatch.setattr(psycopg, "connect", guarded_connect)
    monkeypatch.setattr(
        psycopg.Connection,
        "connect",
        classmethod(guarded_connection_connect),
    )
    monkeypatch.setattr(ConnectionPool, "__init__", guarded_pool_init)
    monkeypatch.setattr(ConnectionPool, "getconn", guarded_getconn)
    monkeypatch.setattr(GraphDatabase, "driver", classmethod(guarded_driver))
```

The `raise AssertionError` lines are unreachable after `pytest.fail`; they exist only to satisfy
static control-flow/type analysis.

- [ ] **Step 5: Wire pytest collection and the autouse fixture**

Replace the empty `tests/conftest.py` with:

```python
from __future__ import annotations

import pytest

from reviewer.config.settings import Settings
from tests.infrastructure_policy import (
    apply_test_environment,
    install_integration_db_guards,
    install_unit_db_guards,
    load_test_endpoints,
)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(pytest.mark.enable_socket)
            continue
        if item.get_closest_marker("enable_socket") or "socket_enabled" in item.fixturenames:
            raise pytest.UsageError(
                "Сеть разрешена только тестам с маркером integration."
            )


@pytest.fixture(autouse=True)
def infrastructure_policy(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.get_closest_marker("integration") is None:
        install_unit_db_guards(monkeypatch)
        return

    production = Settings()
    endpoints = load_test_endpoints(production)
    apply_test_environment(monkeypatch, endpoints)
    install_integration_db_guards(monkeypatch, endpoints)
```

- [ ] **Step 6: Run the focused unit policy tests**

Run:

```bash
.venv/bin/pytest -q tests/test_infrastructure_policy.py -m "not integration"
```

Expected: all unit policy tests pass in well under a second without Postgres or Neo4j.

- [ ] **Step 7: Prove the old fail-soft tests now expose their real network access**

Run:

```bash
.venv/bin/pytest -q tests/web/test_history.py -m "not integration"
```

Expected: `test_record_run_fail_soft_on_bad_dsn` and `test_get_trace_fail_soft_on_bad_dsn` fail with
the unit infrastructure message instead of waiting for port 1.

- [ ] **Step 8: Replace the two real-network unit tests with deterministic failures**

Change those two tests in `tests/web/test_history.py`:

```python
def test_record_run_fail_soft_on_bad_dsn():
    """record_run() возвращает None при ошибке подключения."""
    history = ReviewHistory("postgresql://unused")
    with patch.object(history, "_connect", side_effect=OSError("database unavailable")):
        result = history.record_run(_sample_run(), _sample_findings())
    assert result is None


def test_get_trace_fail_soft_on_bad_dsn():
    """get_trace() возвращает [] при ошибке подключения."""
    history = ReviewHistory("postgresql://unused")
    with patch.object(history, "_connect", side_effect=OSError("database unavailable")):
        result = history.get_trace(1)
    assert result == []
```

- [ ] **Step 9: Run policy, history, and the full unit suite**

Run:

```bash
.venv/bin/pytest -q tests/test_infrastructure_policy.py tests/web/test_history.py
PG_DSN='postgresql://bad:bad@127.0.0.1:1/unreachable?connect_timeout=1' \
NEO4J_URI='neo4j://127.0.0.1:1' \
.venv/bin/pytest -q
```

Expected: both commands pass; the second command does not wait for database connection timeouts.

- [ ] **Step 10: Commit the unit boundary**

```bash
git add pyproject.toml uv.lock tests/conftest.py tests/infrastructure_policy.py \
  tests/test_infrastructure_policy.py tests/web/test_history.py
git commit -m "test(infra): запретить сеть unit-тестам"
```

### Task 2: Add Physically Isolated Test Services

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example:36-45`
- Modify: `tests/test_infrastructure_policy.py`
- Modify: `tests/install/test_install_wizard.py:175-184`

- [ ] **Step 1: Add failing static tests for the Compose profile and test endpoints**

Add `Path` and `yaml` to the existing import section, add `_ROOT` after the imports, then append the
two tests:

```python
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def test_compose_test_services_are_profile_only_and_volume_isolated() -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    pg = services["paradedb-test"]
    assert pg["profiles"] == ["test"]
    assert pg["environment"]["POSTGRES_DB"] == "reviewer_test"
    assert pg["environment"]["POSTGRES_USER"] == "reviewer_test"
    assert pg.get("volumes", []) == []
    assert "127.0.0.1:55433:5432" in pg["ports"]

    neo4j = services["neo4j-test"]
    assert neo4j["profiles"] == ["test"]
    assert neo4j["environment"]["NEO4J_AUTH"] == "neo4j/reviewer_test_pass"
    assert neo4j.get("volumes", []) == []
    assert "127.0.0.1:17687:7687" in neo4j["ports"]

    assert pg.get("volumes") != services["paradedb"].get("volumes")
    assert neo4j.get("volumes") != services["neo4j"].get("volumes")


def test_env_example_documents_test_endpoints() -> None:
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert (
        "TEST_PG_DSN=postgresql://reviewer_test:reviewer_test@localhost:55433/"
        "reviewer_test?connect_timeout=2"
    ) in text
    assert "TEST_NEO4J_URI=neo4j://localhost:17687" in text
    assert "TEST_NEO4J_USER=neo4j" in text
    assert "TEST_NEO4J_PASSWORD=reviewer_test_pass" in text
```

- [ ] **Step 2: Run the static tests and verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_infrastructure_policy.py -m "not integration" \
  -k "compose_test_services or env_example"
```

Expected: failures report missing `paradedb-test`, `neo4j-test`, and `TEST_*` entries.

- [ ] **Step 3: Add the profile-only services**

Add these service definitions under `services:` in `docker-compose.yml`; keep the existing
production services and named volumes unchanged:

```yaml
  paradedb-test:
    profiles: ["test"]
    image: paradedb/paradedb:latest
    environment:
      POSTGRES_USER: reviewer_test
      POSTGRES_PASSWORD: reviewer_test
      POSTGRES_DB: reviewer_test
    ports: ["127.0.0.1:55433:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U reviewer_test -d reviewer_test"]
      interval: 2s
      timeout: 2s
      retries: 30

  neo4j-test:
    profiles: ["test"]
    image: neo4j:5
    environment:
      NEO4J_AUTH: neo4j/reviewer_test_pass
    ports: ["127.0.0.1:17474:7474", "127.0.0.1:17687:7687"]
    healthcheck:
      test:
        ["CMD-SHELL", "cypher-shell -u neo4j -p reviewer_test_pass 'RETURN 1'"]
      interval: 2s
      timeout: 3s
      retries: 30
```

Do not add `volumes:` to either test service. `docker compose rm -sfv` will remove their containers
and any anonymous image volumes.

- [ ] **Step 4: Document the test-only environment values**

Add after the production Postgres/Neo4j block in `.env.example`:

```dotenv
# ============================================================================
# Изолированная test-инфраструктура — читает только tests/conftest.py
# Никогда не направлять TEST_* на dev/production endpoints.
# ============================================================================
TEST_PG_DSN=postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2
TEST_NEO4J_URI=neo4j://localhost:17687
TEST_NEO4J_USER=neo4j
TEST_NEO4J_PASSWORD=reviewer_test_pass
```

- [ ] **Step 5: Keep the installer template production-only explicitly**

Replace `test_env_template_mirrors_env_example` in `tests/install/test_install_wizard.py` with:

```python
def test_env_template_mirrors_env_example():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    example_keys = _keys_from_text((repo_root / ".env.example").read_text(encoding="utf-8"))
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    test_only = {
        "TEST_PG_DSN",
        "TEST_NEO4J_URI",
        "TEST_NEO4J_USER",
        "TEST_NEO4J_PASSWORD",
    }
    assert example_keys - template_keys == test_only
    assert template_keys - example_keys == set()
```

This preserves the mirror check for every production key without exposing local test infrastructure
through `reviewer init`.

- [ ] **Step 6: Validate Compose and the static tests**

Run:

```bash
docker compose config --quiet
docker compose --profile test config --quiet
.venv/bin/pytest -q tests/test_infrastructure_policy.py tests/install/test_install_wizard.py
```

Expected: Compose validates and all selected unit tests pass without starting containers.

- [ ] **Step 7: Start the services and run the integration policy tests**

Run:

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration tests/test_infrastructure_policy.py
```

Expected: socket creation is enabled for integration tests, `Settings` sees the four test values,
and wrong Postgres/Neo4j endpoints fail before connection.

- [ ] **Step 8: Commit the physical boundary**

```bash
git add docker-compose.yml .env.example tests/test_infrastructure_policy.py \
  tests/install/test_install_wizard.py
git commit -m "chore(infra): добавить изолированный test profile"
```

### Task 3: Remove Global ChunkStore Clearing

**Files:**
- Modify: `reviewer/index/store.py:98-105`
- Create: `tests/index/test_store_clear.py`

- [ ] **Step 1: Write the failing clear-contract tests**

Create `tests/index/test_store_clear.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reviewer.index.store import ChunkStore


def test_clear_deletes_only_requested_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    store = ChunkStore("postgresql://unused")
    monkeypatch.setattr(store, "_connect", lambda: context)

    store.clear("test/repo")

    connection.execute.assert_called_once_with(
        "DELETE FROM chunks WHERE repo = %s",
        ("test/repo",),
    )
    connection.commit.assert_called_once_with()


def test_clear_requires_explicit_repo() -> None:
    store = ChunkStore("postgresql://unused")

    with pytest.raises(TypeError):
        store.clear()
```

- [ ] **Step 2: Run the tests and verify the red state**

Run:

```bash
.venv/bin/pytest -q tests/index/test_store_clear.py
```

Expected: `test_clear_deletes_only_requested_repo` already passes because the current repo branch is
scoped; `test_clear_requires_explicit_repo` fails because the no-arg API is still accepted and
reaches the unit DB guard instead of raising `TypeError`.

- [ ] **Step 3: Replace the dangerous API with repo-scoped deletion**

Replace `ChunkStore.clear` in `reviewer/index/store.py` with:

```python
def clear(self, repo: str) -> None:
    """Удалить все чанки явно указанного репозитория."""
    with self._connect() as conn:
        conn.execute("DELETE FROM chunks WHERE repo = %s", (repo,))
        conn.commit()
```

- [ ] **Step 4: Run the contract tests**

Run:

```bash
.venv/bin/pytest -q tests/index/test_store_clear.py
```

Expected: both tests pass and `reviewer/index/store.py` contains no `TRUNCATE chunks`.

- [ ] **Step 5: Commit the API safety change**

```bash
git add reviewer/index/store.py tests/index/test_store_clear.py
git commit -m "fix(index): сделать очистку чанков repo-scoped"
```

### Task 4: Refactor Destructive Postgres Integration Tests

**Files:**
- Modify: `tests/index/test_store_clear.py`
- Modify: `tests/index/test_store_hybrid.py:1-253`
- Modify: `tests/index/test_migrate_base.py:1-66`
- Modify: `tests/integration/test_pipeline.py:1-27`
- Modify: `tests/index/test_status_meta.py:1-32`

- [ ] **Step 1: Add a CI regression test for skipped integration source**

Add `ast` and `Path` to the existing import section, then append the constants and regression test:

```python
_ROOT = Path(__file__).resolve().parents[2]
_DESTRUCTIVE_TEST_FILES = (
    "tests/index/test_store_hybrid.py",
    "tests/index/test_migrate_base.py",
    "tests/integration/test_pipeline.py",
)


def test_destructive_index_tests_never_call_clear_without_repo() -> None:
    offenders: list[str] = []
    for relative in _DESTRUCTIVE_TEST_FILES:
        tree = ast.parse((_ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear"
                and not node.args
                and not node.keywords
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == []
```

- [ ] **Step 2: Run the regression test and verify all 13 unsafe calls are found**

Run:

```bash
.venv/bin/pytest -q \
  tests/index/test_store_clear.py::test_destructive_index_tests_never_call_clear_without_repo
```

Expected: failure lists ten locations in `test_store_hybrid.py`, two in
`test_migrate_base.py`, and one in `test_pipeline.py`.

- [ ] **Step 3: Introduce scoped lifecycle in test_store_hybrid.py**

At the top of `tests/index/test_store_hybrid.py`, define explicit repos and this fixture:

```python
_REPO = "test/store-hybrid"
_OTHER_REPO = "test/store-hybrid-other"
_TEST_REPOS = (_REPO, _OTHER_REPO)


def _row(ref, path, fqn, text, vec, repo=_REPO):
    return ChunkRow(repo=repo, ref=ref, content_hash=fqn + ref + repo, path=path,
                    lang="python", symbol_fqn=fqn, kind="function",
                    start_line=1, end_line=2, text=text, embedding=vec)


@pytest.fixture()
def db():
    settings = Settings()
    store = ChunkStore(settings.pg_dsn)
    store.init_schema()
    try:
        for repo in _TEST_REPOS:
            store.clear(repo)
        yield store, settings
    finally:
        try:
            for repo in _TEST_REPOS:
                store.clear(repo)
        finally:
            store.close()
```

Add `db` to these ten test signatures: `test_overlay_shadows_base_for_changed_paths`,
`test_delete_ref_removes_only_target_ref`, `test_delete_missing_symbols_removes_stale_only`,
`test_delete_missing_symbols_empty_keep_fqns_removes_all`,
`test_delete_paths_except_removes_unlisted_paths`, `test_delete_paths_except_empty_keep_is_noop`,
`test_two_repo_isolation`, `test_fetch_nodes_at_returns_only_given_ref`,
`test_hybrid_search_surfaces_ann_distance_and_bm25_hit`, and `test_two_branch_isolation`. Begin each
body with `store, s = db`; remove its repeated `Settings`, `ChunkStore`, `init_schema`, and no-arg
`clear` lines. Preserve its existing behavior while applying the repository constants and exact
SQL replacements below.

Replace the five direct SQL statements with these exact forms:

```python
remaining = conn.execute(
    "SELECT ref, path FROM chunks WHERE repo=%s ORDER BY ref, path",
    (_REPO,),
).fetchall()

remaining = {row for row in conn.execute(
    "SELECT path, symbol_fqn FROM chunks "
    "WHERE repo=%s AND ref='base' ORDER BY path, symbol_fqn",
    (_REPO,),
).fetchall()}

remaining = {row for row in conn.execute(
    "SELECT path, symbol_fqn FROM chunks WHERE repo=%s AND ref='base'",
    (_REPO,),
).fetchall()}

remaining = {row for row in conn.execute(
    "SELECT ref, path FROM chunks WHERE repo=%s ORDER BY ref, path",
    (_REPO,),
).fetchall()}

count = conn.execute(
    "SELECT count(*) FROM chunks WHERE repo=%s AND ref='base'",
    (_REPO,),
).fetchone()[0]
```

In `test_two_repo_isolation`, replace `a/x` and `b/y` with `_REPO` and `_OTHER_REPO` in both
inserted rows and `hybrid_search` calls.

- [ ] **Step 4: Scope migration test data and metadata**

Replace the setup helpers in `tests/index/test_migrate_base.py` with:

```python
_REPO = "test/migrate-base"


def _row(ref, path, fqn):
    return ChunkRow(repo=_REPO, ref=ref, content_hash=fqn, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text="code", embedding=[0.0] * 1024)


def _cleanup(store: ChunkStore) -> None:
    store.clear(_REPO)
    with psycopg.connect(store.dsn) as conn:
        conn.execute("DELETE FROM index_meta WHERE repo=%s", (_REPO,))
        conn.commit()


@pytest.fixture()
def store():
    result = ChunkStore(Settings().pg_dsn)
    result.init_schema()
    try:
        _cleanup(result)
        yield result
    finally:
        try:
            _cleanup(result)
        finally:
            result.close()
```

Change both test signatures to accept `store`, remove local store construction and no-arg clear,
and use these scoped assertions:

```python
refs = {row[0] for row in conn.execute(
    "SELECT DISTINCT ref FROM chunks WHERE repo=%s",
    (_REPO,),
).fetchall()}
meta = conn.execute(
    "SELECT ref, sha FROM index_meta WHERE repo=%s",
    (_REPO,),
).fetchone()

dup_count = conn.execute(
    "SELECT count(*) FROM chunks "
    "WHERE repo=%s AND path='a.py' AND symbol_fqn='dup'",
    (_REPO,),
).fetchone()[0]
solo = conn.execute(
    "SELECT ref FROM chunks WHERE repo=%s AND path='b.py' AND symbol_fqn='solo'",
    (_REPO,),
).fetchone()
```

- [ ] **Step 5: Scope and close the end-to-end pipeline test**

Replace `test_index_then_hybrid_retrieve_finds_relevant_symbol` in
`tests/integration/test_pipeline.py` with:

```python
@pytest.mark.integration
def test_index_then_hybrid_retrieve_finds_relevant_symbol(tmp_path):
    repo = "test/pipeline"
    (tmp_path / "auth.py").write_text("def verify_token(t):\n    return t == 'ok'\n")
    (tmp_path / "util.py").write_text("def add(a,b):\n    return a+b\n")
    import subprocess
    for args in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "c"]):
        subprocess.run(args, cwd=tmp_path, check=True)

    settings = Settings()
    components = build_components(settings, connect=False)
    components.store.init_schema()
    try:
        components.store.clear(repo)
        from reviewer.gitutil import file_at_ref, list_python_files
        files = list_python_files(str(tmp_path), "HEAD")
        update_base(
            components.store,
            components.embedder,
            repo,
            "HEAD",
            files,
            read=lambda path: file_at_ref(str(tmp_path), path, "HEAD"),
        )
        query = components.embedder.embed_query("token verification")
        hits = components.store.hybrid_search(
            repo,
            query_text="token verification",
            query_embedding=query,
            overlay_ref="",
            changed_paths=[],
            top_k=5,
            base_ref=base_ref("HEAD"),
        )
        assert any(hit.symbol_fqn == "verify_token" for hit in hits)
    finally:
        try:
            components.store.clear(repo)
            with components.store._connect() as connection:
                connection.execute("DELETE FROM index_meta WHERE repo=%s", (repo,))
                connection.commit()
        finally:
            components.store.close()
            components.task_store.close()
            components.summary_store.close()
```

- [ ] **Step 6: Harden the already-scoped status metadata test**

In `tests/index/test_status_meta.py`, use `_REPO = "test/status-meta"` in `_row` and every store
call. Extend its `finally` block so metadata cannot leak:

```python
finally:
    try:
        store.clear(_REPO)
        with store._connect() as conn:
            conn.execute("DELETE FROM index_meta WHERE repo=%s", (_REPO,))
            conn.commit()
    finally:
        store.close()
```

- [ ] **Step 7: Run the static guard and selected real-Postgres tests**

Run:

```bash
.venv/bin/pytest -q tests/index/test_store_clear.py
.venv/bin/pytest -q -m integration \
  tests/index/test_store_hybrid.py \
  tests/index/test_migrate_base.py \
  tests/index/test_status_meta.py
```

Expected: the unit AST guard finds no no-arg calls; all selected integration tests pass against
`paradedb-test`. Do not run `test_pipeline.py` yet unless `VOYAGE_API_KEY` is configured.

- [ ] **Step 8: Commit the scoped integration fixtures**

```bash
git add tests/index/test_store_clear.py tests/index/test_store_hybrid.py \
  tests/index/test_migrate_base.py tests/integration/test_pipeline.py \
  tests/index/test_status_meta.py
git commit -m "test(index): изолировать integration данные по repo"
```

### Task 5: Remove Collection-Time Infrastructure and Verify Both Test Databases

**Files:**
- Modify: `tests/index/test_summary_store.py:1-145`

- [ ] **Step 1: Demonstrate the collection-time DSN bug under the new allowlist**

Run:

```bash
.venv/bin/pytest -q -m integration tests/index/test_summary_store.py
```

Expected: the file uses the production `DSN` captured at import time and fails the
`TEST_PG_DSN` allowlist before connecting.

- [ ] **Step 2: Move Settings reads into fixtures and use explicit repos**

Remove `DSN = Settings().pg_dsn`. Add these constants and replace the first fixture:

```python
_REPO = "test/summary-store"
_PRI167_REPO = "test/summary-store-pri167"


def _delete_summaries(store: SummaryStore, repo: str) -> None:
    with store._connect() as conn:
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
        conn.commit()


@pytest.fixture()
def store():
    dsn = Settings().pg_dsn
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()
    finally:
        schema_store.close()

    result = SummaryStore(dsn)
    try:
        _delete_summaries(result, _REPO)
        yield result
    finally:
        try:
            _delete_summaries(result, _REPO)
        finally:
            result.close()
```

Replace every `"t/t"` in the first group of tests with `_REPO`. Replace
`test_list_base_members_reads_base_ref_rows` with this complete function:

```python
def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.chunker import symbol_skeleton_hash
    from reviewer.index.store import ChunkRow

    store = ChunkStore(Settings().pg_dsn)
    store.init_schema()
    try:
        store.clear(_REPO)
        store.upsert([ChunkRow(
            repo=_REPO,
            ref="base:dev",
            content_hash="h",
            path="reviewer/x/a.py",
            lang="python",
            symbol_fqn="A",
            kind="function",
            start_line=3,
            end_line=9,
            text="def a(): ...",
            embedding=[0.0] * 1024,
        )])
        members = store.list_base_members(_REPO, "dev")
        expected = ("reviewer/x/a.py", "A", "h", 3,
                    symbol_skeleton_hash("def a(): ..."))
        assert expected in members
    finally:
        try:
            store.clear(_REPO)
        finally:
            store.close()
```

Replace `store_pri167` with:

```python
@pytest.fixture()
def store_pri167():
    dsn = Settings().pg_dsn
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()
    finally:
        schema_store.close()

    result = SummaryStore(dsn)
    try:
        _delete_summaries(result, _PRI167_REPO)
        yield result
    finally:
        try:
            _delete_summaries(result, _PRI167_REPO)
        finally:
            result.close()
```

Replace every `"test/pri167"` in the second group with `_PRI167_REPO`.

- [ ] **Step 3: Run Postgres summary tests and Neo4j destructive tests**

Run:

```bash
.venv/bin/pytest -q -m integration tests/index/test_summary_store.py
.venv/bin/pytest -q -m integration tests/graph/test_store.py
```

Expected: both suites pass; Postgres connects only to port 55433 and global graph cleanup runs only
against Neo4j port 17687.

- [ ] **Step 4: Run broader infrastructure integration tests**

Run:

```bash
.venv/bin/pytest -q -m integration \
  tests/index \
  tests/graph \
  tests/mcp/test_session_store.py \
  tests/web/test_history.py \
  tests/tasks/test_integration.py \
  tests/tasks/test_graph.py
```

Expected: all selected tests pass against the test profile and no safety guard reports a production
endpoint. Any guard failure is a blocker; do not weaken the allowlist or add a production fallback.

- [ ] **Step 5: Commit collection-safe integration setup**

```bash
git add tests/index/test_summary_store.py
git commit -m "test(index): читать test DSN после fixture setup"
```

### Task 6: Add the CI Backstop and Document the Workflow

**Files:**
- Modify: `.github/workflows/publish.yml:8-16`
- Modify: `README.md:489-499,997-1007,1037-1049`
- Modify: `README.ru.md:437-447,995-1004,1025-1038`
- Modify: `CLAUDE.md:11-27,54,132`
- Modify: `tests/mcp/test_session_store.py:1-6`
- Modify: `tests/web/test_history.py:1-7`
- Modify: `tests/tasks/test_integration.py:1-5`
- Modify: `tests/index/test_summary_store.py:1`

- [ ] **Step 1: Add a workflow timeout without service containers**

Update only the `test` job header in `.github/workflows/publish.yml`:

```yaml
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
```

Keep `pip install -e ".[dev,web]"` and `pytest -q` unchanged. Do not add Postgres or Neo4j services.

- [ ] **Step 2: Document the exact test workflow in all three primary docs**

Use this command block in `README.md`, `README.ru.md`, and `CLAUDE.md`, translating surrounding
prose but not the commands:

```bash
# Unit: no Postgres, Neo4j, localhost service, or external network required
.venv/bin/pytest -q

# Start only isolated integration infrastructure
docker compose --profile test up -d --wait paradedb-test neo4j-test

# Integration; the pipeline test also requires VOYAGE_API_KEY
.venv/bin/pytest -q -m integration

# Remove test containers and anonymous test volumes only
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

The prose in each file must state all of these invariants explicitly:

- unit tests cannot use external or localhost sockets;
- every real-network test carries `@pytest.mark.integration`;
- DB integration tests use `TEST_PG_DSN`, `TEST_NEO4J_URI`, `TEST_NEO4J_USER`, and
  `TEST_NEO4J_PASSWORD`;
- test endpoints must never equal development/production endpoints;
- production and test Compose services use different ports, credentials, and storage;
- the default `pytest -q` run starts no infrastructure.

- [ ] **Step 3: Update stale integration module instructions**

Replace the opening docstrings with these exact module-specific versions.

`tests/mcp/test_session_store.py`:

```python
"""Тесты SessionStore.

Unit fail-soft тест мокает _connect. Integration-тесты save/load/delete/TTL требуют:
docker compose --profile test up -d --wait paradedb-test neo4j-test
"""
```

`tests/web/test_history.py`:

```python
"""Тесты reviewer.web.history.ReviewHistory.

Unit fail-soft тесты мокают _connect. Integration-тесты записи/чтения требуют:
docker compose --profile test up -d --wait paradedb-test neo4j-test
"""
```

`tests/tasks/test_integration.py`:

```python
"""Integration: TaskStore + TaskGraph на изолированных Postgres+Neo4j.

Эмбеддер фейковый; тестовая инфраструктура запускается командой:
docker compose --profile test up -d --wait paradedb-test neo4j-test
"""
```

`tests/index/test_summary_store.py`:

```python
"""Integration-тесты SummaryStore на изолированном ParadeDB.

Тестовая инфраструктура запускается командой:
docker compose --profile test up -d --wait paradedb-test neo4j-test
"""
```

- [ ] **Step 4: Run documentation-adjacent and full unit checks**

Run:

```bash
.venv/bin/pytest -q tests/install/test_install_wizard.py tests/test_infrastructure_policy.py
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Expected: all commands pass; the unit suite uses no service containers.

- [ ] **Step 5: Commit CI and documentation**

```bash
git add .github/workflows/publish.yml README.md README.ru.md CLAUDE.md \
  tests/mcp/test_session_store.py tests/web/test_history.py \
  tests/tasks/test_integration.py tests/index/test_summary_store.py
git commit -m "ci(test): гарантировать изоляцию unit-прогона"
```

### Task 7: Final Safety Verification

**Files:**
- Verify only; no planned source changes.

- [ ] **Step 1: Verify lock, Compose, and dangerous-source invariants**

Run:

```bash
uv lock --check
docker compose config --quiet
docker compose --profile test config --quiet
! rg 'TRUNCATE chunks' reviewer/index/store.py
! rg 'store\.clear\(\)|c\.store\.clear\(\)' \
  tests/index/test_store_hybrid.py \
  tests/index/test_migrate_base.py \
  tests/integration/test_pipeline.py
```

Expected: every command exits 0; both negated `rg` checks find no dangerous source.

- [ ] **Step 2: Prove the complete unit suite is infrastructure-free**

Run:

```bash
PG_DSN='postgresql://invalid:invalid@127.0.0.1:1/unreachable?connect_timeout=1' \
NEO4J_URI='neo4j://127.0.0.1:1' \
.venv/bin/pytest -q
```

Expected: PASS without connection retries or pool timeouts.

- [ ] **Step 3: Prove production-like test configuration is rejected before connect**

Run:

```bash
PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2' \
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2' \
.venv/bin/pytest -q -m integration tests/test_infrastructure_policy.py
```

Expected: fixture setup fails immediately with `TEST_PG_DSN совпадает с текущим PG_DSN`; no
connection attempt appears in logs.

- [ ] **Step 4: Run the complete integration suite in the isolated profile**

Run:

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration
```

Expected: PASS when `VOYAGE_API_KEY` is configured. If it is intentionally absent, run this exact
fallback instead:

```bash
.venv/bin/pytest -q -m integration --ignore=tests/integration/test_pipeline.py
```

Expected fallback result: PASS; report `tests/integration/test_pipeline.py` as not run rather than
claiming it passed.

- [ ] **Step 5: Run final lint and remove only test infrastructure**

Run:

```bash
.venv/bin/ruff check .
docker compose --profile test rm -sfv paradedb-test neo4j-test
git status --short
```

Expected: Ruff passes; test containers and anonymous volumes are gone; `git status --short` shows
only changes intentionally made while executing this plan.
