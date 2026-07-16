from __future__ import annotations

from contextlib import contextmanager
import inspect
from pathlib import Path
import socket
import subprocess
import sys
from types import MethodType

import psycopg
import pytest
import tests.infrastructure_policy as infrastructure_policy
from neo4j import GraphDatabase
from psycopg_pool import ConnectionPool, PoolTimeout
from pytest_socket import SocketBlockedError

from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.store import ChunkStore
from tests.infrastructure_policy import (
    InfrastructureTestSettings,
    validate_test_endpoints,
)


def _test_settings(**overrides: str) -> InfrastructureTestSettings:
    return InfrastructureTestSettings(_env_file=None, **overrides)


def _production_settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    ("attempt", "policy_message"),
    [
        (
            "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_STREAM)",
            "A test tried to use socket.socket",
        ),
        (
            "import psycopg\npsycopg.connect('not-a-conninfo')",
            "Unit-тесту запрещено подключение к Postgres",
        ),
        (
            "from psycopg_pool import ConnectionPool\n"
            "ConnectionPool('postgresql://u:p@localhost/db', open=False)",
            "Unit-тесту запрещено создание пула Postgres",
        ),
        (
            "from neo4j import GraphDatabase\n"
            "GraphDatabase.driver('neo4j://localhost:7687', auth=('u', 'p'))",
            "Unit-тесту запрещено подключение к Neo4j",
        ),
    ],
)
def test_collection_time_infrastructure_is_blocked(
    tmp_path: Path, attempt: str, policy_message: str
) -> None:
    module = tmp_path / "test_import_time_infrastructure.py"
    module.write_text(f"{attempt}\n\ndef test_never_runs():\n    pass\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.conftest", str(module)],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert policy_message in result.stdout + result.stderr


def test_unit_test_cannot_create_python_socket() -> None:
    with pytest.warns(UserWarning, match="socket.socket"):
        with pytest.raises(SocketBlockedError):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_unit_test_allows_unix_socketpair_for_in_process_event_loops() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()


@pytest.mark.parametrize("connect", [lambda: psycopg.connect(""), lambda: psycopg.Connection.connect("")])
def test_unit_test_cannot_use_psycopg_connect_aliases(connect) -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        connect()


def test_unit_test_blocks_connection_pool_before_workers_start() -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        ConnectionPool("postgresql://user:password@localhost/db")


def test_unit_stores_fail_fast_before_db_clients_start() -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        ChunkStore("postgresql://user:password@localhost/db")._ensure_pool()

    with pytest.raises(pytest.fail.Exception, match="Neo4j"):
        GraphStore("neo4j://localhost:7687", "neo4j", "password")


def test_test_endpoint_defaults_are_isolated() -> None:
    settings = _test_settings()

    assert settings.pg_dsn == (
        "postgresql://reviewer_test:reviewer_test@localhost:55433/"
        "reviewer_test?connect_timeout=2"
    )
    assert settings.neo4j_uri == "neo4j://localhost:17687"
    assert settings.neo4j_user == "neo4j"
    assert settings.neo4j_password == "reviewer_test_pass"


def test_test_endpoint_settings_repr_hides_credentials() -> None:
    secrets = (
        "postgresql://secret-user:secret-password@localhost:55433/secret_test",
        "neo4j://secret-host:17687",
        "secret-user",
        "secret-password",
    )
    settings = _test_settings(
        pg_dsn=secrets[0],
        neo4j_uri=secrets[1],
        neo4j_user=secrets[2],
        neo4j_password=secrets[3],
    )

    rendered = repr(settings)

    assert all(secret not in rendered for secret in secrets)


@pytest.mark.parametrize(
    "pg_dsn",
    [
        "service=production",
        "host=localhost,127.0.0.1 user=u password=p dbname=reviewer_test connect_timeout=2",
        "user=u password=p dbname=reviewer_test connect_timeout=2",
        "host=localhost password=p dbname=reviewer_test connect_timeout=2",
        "host=localhost user=u dbname=reviewer_test connect_timeout=2",
        "host=localhost user=u password=p connect_timeout=2",
        "host=localhost user=u password=p dbname=reviewer connect_timeout=2",
        "host=localhost user=u password=p dbname=reviewer_test connect_timeout=0",
        "host=localhost user=u password=p dbname=reviewer_test connect_timeout=6",
        "host=localhost port=0 user=u password=p dbname=reviewer_test connect_timeout=2",
        "host=localhost port=70000 user=u password=p dbname=reviewer_test connect_timeout=2",
        (
            "host=localhost hostaddr=127.0.0.2 user=u password=p "
            "dbname=reviewer_test connect_timeout=2"
        ),
    ],
)
def test_rejects_unsafe_postgres_test_targets(pg_dsn: str) -> None:
    with pytest.raises(ValueError):
        validate_test_endpoints(_test_settings(pg_dsn=pg_dsn), _production_settings())


def test_rejects_postgres_target_equal_to_production_after_loopback_normalization() -> None:
    production = _production_settings(
        pg_dsn="postgresql://prod:prod@localhost:55433/shared_test?connect_timeout=2"
    )
    test = _test_settings(
        pg_dsn="postgresql://test:test@127.0.0.1:55433/shared_test?connect_timeout=2"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_postgres_loopback_alias_equal_to_production() -> None:
    production = _production_settings(
        pg_dsn="postgresql://prod:prod@localhost:55433/shared_test?connect_timeout=2"
    )
    test = _test_settings(
        pg_dsn="postgresql://test:test@127.0.0.2:55433/shared_test?connect_timeout=2"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_postgres_target_equal_to_production_without_production_credentials() -> None:
    production = _production_settings(
        pg_dsn="host=localhost port=55433 dbname=shared_test connect_timeout=2"
    )
    test = _test_settings(
        pg_dsn="postgresql://test:test@127.0.0.1:55433/shared_test?connect_timeout=2"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_equal_production_target_when_production_uses_hostaddr() -> None:
    production = _production_settings(
        pg_dsn=(
            "host=localhost hostaddr=127.0.0.1 port=55433 user=prod password=prod "
            "dbname=shared_test connect_timeout=2"
        )
    )
    test = _test_settings(
        pg_dsn="postgresql://test:test@127.0.0.1:55433/shared_test?connect_timeout=2"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_equal_production_target_with_a_long_production_timeout() -> None:
    production = _production_settings(
        pg_dsn="postgresql://prod:prod@localhost:55433/shared_test?connect_timeout=30"
    )
    test = _test_settings(
        pg_dsn="postgresql://test:test@127.0.0.1:55433/shared_test?connect_timeout=2"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


@pytest.mark.parametrize(
    "overrides",
    [
        {"neo4j_uri": "not-a-uri"},
        {"neo4j_uri": "https://localhost:17687"},
        {"neo4j_user": ""},
        {"neo4j_password": ""},
    ],
)
def test_rejects_unsafe_neo4j_test_targets(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        validate_test_endpoints(_test_settings(**overrides), _production_settings())


def test_rejects_zero_neo4j_port_as_malformed() -> None:
    production = _production_settings(neo4j_uri="neo4j://production.example:7687")

    with pytest.raises(ValueError, match="Malformed"):
        validate_test_endpoints(
            _test_settings(neo4j_uri="neo4j://localhost:0"), production
        )


def test_rejects_neo4j_target_equal_to_production_after_loopback_normalization() -> None:
    production = _production_settings(neo4j_uri="neo4j://[::1]:17687")
    test = _test_settings(neo4j_uri="neo4j://127.0.0.1:17687")

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_neo4j_target_equal_to_production_through_scheme_alias() -> None:
    production = _production_settings(neo4j_uri="neo4j://localhost:17687")
    test = _test_settings(neo4j_uri="bolt://127.0.0.1:17687")

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_neo4j_loopback_alias_equal_to_production() -> None:
    production = _production_settings(neo4j_uri="neo4j://LOCALHOST.:17687")
    test = _test_settings(neo4j_uri="bolt://[0:0:0:0:0:0:0:1]:17687")

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_rejects_neo4j_target_equal_to_production_without_production_credentials() -> None:
    production = _production_settings(
        neo4j_uri="neo4j://localhost:17687", neo4j_user="", neo4j_password=""
    )
    test = _test_settings(neo4j_uri="neo4j://127.0.0.1:17687")

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


def test_validation_errors_do_not_include_passwords() -> None:
    secret = "never-print-this-password"
    test = _test_settings(
        pg_dsn=f"postgresql://user:{secret}@localhost:55433/not_test_db?connect_timeout=2"
    )

    with pytest.raises(ValueError) as exc_info:
        validate_test_endpoints(test, _production_settings())

    assert secret not in str(exc_info.value)


def _assert_guard_signatures() -> None:
    assert inspect.signature(psycopg.connect) == inspect.signature(
        infrastructure_policy._ORIGINAL_PSYCOPG_CONNECT
    )
    assert inspect.signature(psycopg.Connection.connect) == inspect.signature(
        MethodType(infrastructure_policy._ORIGINAL_CONNECTION_CONNECT, psycopg.Connection)
    )
    assert inspect.signature(ConnectionPool.__init__) == inspect.signature(
        infrastructure_policy._ORIGINAL_POOL_INIT
    )
    assert inspect.signature(ConnectionPool.connection) == inspect.signature(
        infrastructure_policy._ORIGINAL_POOL_CONNECTION
    )
    assert inspect.signature(GraphDatabase.driver) == inspect.signature(
        MethodType(infrastructure_policy._ORIGINAL_GRAPH_DRIVER, GraphDatabase)
    )


def test_unit_guards_preserve_dependency_signatures() -> None:
    _assert_guard_signatures()


@pytest.mark.integration
def test_integration_fixture_routes_settings_to_raw_test_settings(
    infrastructure_test_settings: InfrastructureTestSettings,
) -> None:
    actual = Settings()

    if actual.pg_dsn != infrastructure_test_settings.pg_dsn:
        pytest.fail("PG_DSN не перенаправлен в TEST_PG_DSN", pytrace=False)
    if actual.neo4j_uri != infrastructure_test_settings.neo4j_uri:
        pytest.fail("NEO4J_URI не перенаправлен в TEST_NEO4J_URI", pytrace=False)
    if actual.neo4j_user != infrastructure_test_settings.neo4j_user:
        pytest.fail("NEO4J_USER не перенаправлен в TEST_NEO4J_USER", pytrace=False)
    if actual.neo4j_password != infrastructure_test_settings.neo4j_password:
        pytest.fail("NEO4J_PASSWORD не перенаправлен в TEST_NEO4J_PASSWORD", pytrace=False)


@pytest.mark.integration
def test_integration_guards_preserve_dependency_signatures() -> None:
    _assert_guard_signatures()


@pytest.mark.integration
def test_integration_psycopg_guard_rejects_hostaddr_override(
    infrastructure_test_settings: InfrastructureTestSettings,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        psycopg.connect(infrastructure_test_settings.pg_dsn, hostaddr="127.0.0.2")


@pytest.mark.integration
def test_integration_pool_guard_rejects_hostaddr_override(
    infrastructure_test_settings: InfrastructureTestSettings,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        ConnectionPool(
            infrastructure_test_settings.pg_dsn,
            kwargs={"hostaddr": "127.0.0.2"},
            open=False,
        )


@pytest.mark.integration
def test_integration_neo4j_guard_rejects_scheme_substitution_before_driver(
    infrastructure_test_settings: InfrastructureTestSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_called = False

    def original_driver(*args, **kwargs):
        nonlocal original_called
        original_called = True

    monkeypatch.setattr(infrastructure_policy, "_ORIGINAL_GRAPH_DRIVER", original_driver)

    with pytest.raises(pytest.fail.Exception, match="Neo4j"):
        GraphDatabase.driver(
            "bolt://127.0.0.1:17687",
            auth=(
                infrastructure_test_settings.neo4j_user,
                infrastructure_test_settings.neo4j_password,
            ),
        )

    assert original_called is False


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["body", "exit"])
def test_pool_timeout_after_acquisition_is_not_rewritten(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def original_connection(pool, timeout=None):
        try:
            yield object()
        finally:
            if stage == "exit":
                raise PoolTimeout("exit timeout")

    monkeypatch.setattr(
        infrastructure_policy, "_ORIGINAL_POOL_CONNECTION", original_connection
    )
    pool = object.__new__(ConnectionPool)

    with pytest.raises(PoolTimeout, match=stage):
        with pool.connection():
            if stage == "body":
                raise PoolTimeout("body timeout")


@pytest.mark.integration
def test_pool_acquisition_timeout_has_infrastructure_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def original_connection(pool, timeout=None):
        raise PoolTimeout("acquisition timeout")
        yield

    monkeypatch.setattr(
        infrastructure_policy, "_ORIGINAL_POOL_CONNECTION", original_connection
    )
    pool = object.__new__(ConnectionPool)

    with pytest.raises(pytest.fail.Exception, match="docker compose --profile test"):
        with pool.connection():
            pass


@pytest.mark.integration
def test_integration_guards_allow_only_the_configured_endpoints(
    infrastructure_test_settings: InfrastructureTestSettings,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="Postgres"):
        psycopg.connect(
            "postgresql://reviewer:reviewer@localhost:5433/reviewer?connect_timeout=2"
        )
    with pytest.raises(pytest.fail.Exception, match="Neo4j"):
        GraphDatabase.driver("neo4j://localhost:7687", auth=("neo4j", "reviewerpass"))

    with psycopg.connect(infrastructure_test_settings.pg_dsn) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    driver = GraphDatabase.driver(
        infrastructure_test_settings.neo4j_uri,
        auth=(
            infrastructure_test_settings.neo4j_user,
            infrastructure_test_settings.neo4j_password,
        ),
    )
    try:
        driver.verify_connectivity()
    finally:
        driver.close()
