from __future__ import annotations

from contextlib import contextmanager
import inspect
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
from types import MethodType

import psycopg
import pytest
import tests.infrastructure_policy as infrastructure_policy
import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import ConfigurationError
from psycopg_pool import ConnectionPool, PoolTimeout
from pytest_socket import SocketBlockedError

from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.store import ChunkStore
from tests.infrastructure_policy import (
    InfrastructureSafetyError,
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


@pytest.mark.parametrize("outer_context", ["unit", "integration"])
def test_nested_pytest_restores_outer_policy_and_originals(
    tmp_path: Path, outer_context: str
) -> None:
    inner = tmp_path / "test_inner_session.py"
    inner.write_text("def test_inner():\n    pass\n", encoding="utf-8")
    outer = tmp_path / "test_outer_session.py"
    marker = "@pytest.mark.integration\n" if outer_context == "integration" else ""
    socket_check = (
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.close()\n"
        "with pytest.raises(pytest.fail.Exception, match='Integration-тест'):\n"
        "    psycopg.connect('not-a-conninfo')"
        if outer_context == "integration"
        else
        "with pytest.warns(UserWarning, match='socket.socket'):\n"
        "    with pytest.raises(SocketBlockedError):\n"
        "        socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "with pytest.raises(pytest.fail.Exception, match='Unit-тесту'):\n"
        "    psycopg.connect('not-a-conninfo')"
    )
    policy_body = textwrap.indent(socket_check, "    ")
    outer.write_text(
        f"""import socket

import psycopg
import pytest
from neo4j import GraphDatabase
from psycopg_pool import ConnectionPool
from pytest_socket import SocketBlockedError

def state():
    return (
        socket.socket,
        "connect" in socket.socket.__dict__,
        socket.socket.__dict__.get("connect"),
        psycopg.connect,
        psycopg.Connection.__dict__["connect"],
        ConnectionPool.__dict__["__init__"],
        GraphDatabase.__dict__["driver"],
    )

def assert_policy():
{policy_body}

{marker}def test_outer_policy_survives_nested_session():
    before = state()
    assert_policy()
    result = pytest.main([
        "-q", "-p", "tests.conftest", {str(inner)!r}, "-m", "not integration"
    ])
    assert result == pytest.ExitCode.OK
    assert state() == before
    assert_policy()
""",
        encoding="utf-8",
    )
    driver = tmp_path / "nested_driver.py"
    selection = "integration" if outer_context == "integration" else "not integration"
    project_root = Path(__file__).parents[1]
    driver.write_text(
        f"""import socket
import sys

sys.path.insert(0, {str(project_root)!r})

import psycopg
import pytest
from neo4j import GraphDatabase
from psycopg_pool import ConnectionPool

def state():
    return (
        socket.socket,
        "connect" in socket.socket.__dict__,
        socket.socket.__dict__.get("connect"),
        psycopg.connect,
        psycopg.Connection.__dict__["connect"],
        ConnectionPool.__dict__["__init__"],
        GraphDatabase.__dict__["driver"],
    )

before = state()
result = pytest.main([
    "-q", "-p", "tests.conftest", {str(outer)!r}, "-m", {selection!r}
])
assert result == pytest.ExitCode.OK
assert state() == before
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(driver)],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


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


def test_compose_defines_isolated_test_profile_services() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    paradedb = compose["services"]["paradedb-test"]
    assert paradedb["profiles"] == ["test"]
    assert paradedb["environment"] == {
        "POSTGRES_USER": "reviewer_test",
        "POSTGRES_PASSWORD": "reviewer_test",
        "POSTGRES_DB": "reviewer_test",
    }
    assert paradedb["ports"] == ["127.0.0.1:55433:5432"]
    assert paradedb["healthcheck"] == {
        "test": ["CMD-SHELL", "pg_isready -U reviewer_test -d reviewer_test"],
        "interval": "2s",
        "timeout": "2s",
        "retries": 30,
    }

    neo4j = compose["services"]["neo4j-test"]
    assert neo4j["profiles"] == ["test"]
    assert neo4j["environment"] == {"NEO4J_AUTH": "neo4j/reviewer_test_pass"}
    assert neo4j["ports"] == ["127.0.0.1:17474:7474", "127.0.0.1:17687:7687"]
    assert neo4j["healthcheck"] == {
        "test": [
            "CMD-SHELL",
            "cypher-shell -u neo4j -p reviewer_test_pass 'RETURN 1'",
        ],
        "interval": "2s",
        "timeout": "3s",
        "retries": 30,
    }


def test_compose_pins_only_test_service_images_by_digest() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["paradedb"]["image"] == "paradedb/paradedb:latest"
    assert services["neo4j"]["image"] == "neo4j:5"
    assert services["paradedb-test"]["image"].startswith("paradedb/paradedb@sha256:")
    assert services["neo4j-test"]["image"].startswith("neo4j@sha256:")


def test_compose_test_services_use_only_disposable_tmpfs() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    paradedb = compose["services"]["paradedb-test"]
    assert paradedb["tmpfs"] == ["/var/lib/postgresql"]
    assert "volumes" not in paradedb

    neo4j = compose["services"]["neo4j-test"]
    assert neo4j["tmpfs"] == ["/data", "/logs"]
    assert "volumes" not in neo4j


def test_compose_documents_only_safe_test_profile_teardown() -> None:
    root = Path(__file__).parents[1]
    compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    normalized = "\n".join(line.lstrip() for line in compose_text.splitlines())

    assert (
        "# Безопасное удаление: docker compose --profile test rm -sfv "
        "paradedb-test neo4j-test"
    ) in normalized
    assert (
        "# НИКОГДА не используйте `docker compose --profile test down -v`: это остановит\n"
        "# dev-сервисы и удалит production named volumes."
    ) in normalized


def test_env_example_documents_exact_test_endpoint_defaults() -> None:
    root = Path(__file__).parents[1]
    values = {
        key.strip(): value.strip()
        for line in (root / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
        if key.strip().startswith("TEST_")
    }

    assert values == {
        "TEST_PG_DSN": (
            "postgresql://reviewer_test:reviewer_test@localhost:55433/"
            "reviewer_test?connect_timeout=2"
        ),
        "TEST_NEO4J_URI": "neo4j://localhost:17687",
        "TEST_NEO4J_USER": "neo4j",
        "TEST_NEO4J_PASSWORD": "reviewer_test_pass",
    }


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


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001"])
def test_rejects_noncanonical_numeric_postgres_host(host: str) -> None:
    test = _test_settings(
        pg_dsn=f"postgresql://u:p@{host}:55433/reviewer_test?connect_timeout=2"
    )
    production = _production_settings(
        pg_dsn="postgresql://prod:prod@production.example:5432/reviewer"
    )

    with pytest.raises(InfrastructureSafetyError, match="Non-canonical numeric host"):
        validate_test_endpoints(test, production)


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


@pytest.mark.parametrize(
    ("production_host", "test_host"),
    [("db.example.", "db.example"), ("db.example", "db.example.")],
)
def test_rejects_postgres_root_dot_alias_equal_to_production(
    production_host: str, test_host: str
) -> None:
    production = _production_settings(
        pg_dsn=(
            f"postgresql://prod:prod@{production_host}:55433/"
            "shared_test?connect_timeout=2"
        )
    )
    test = _test_settings(
        pg_dsn=(
            f"postgresql://test:test@{test_host}:55433/"
            "shared_test?connect_timeout=2"
        )
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


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001"])
def test_rejects_noncanonical_numeric_neo4j_host(host: str) -> None:
    test = _test_settings(neo4j_uri=f"neo4j://{host}:17687")
    production = _production_settings(neo4j_uri="neo4j://production.example:7687")

    with pytest.raises(InfrastructureSafetyError, match="Non-canonical numeric host"):
        validate_test_endpoints(test, production)


def test_rejects_noncanonical_numeric_production_neo4j_host() -> None:
    test = _test_settings(neo4j_uri="neo4j://localhost:17687")
    production = _production_settings(neo4j_uri="neo4j://127.1:17687")

    with pytest.raises(InfrastructureSafetyError, match="Non-canonical numeric host"):
        validate_test_endpoints(test, production)


def test_rejects_production_neo4j_routing_uri_at_same_location() -> None:
    test = _test_settings(neo4j_uri="neo4j://db.example:17687")
    production = _production_settings(
        neo4j_uri="neo4j://db.example:17687?region=eu"
    )

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


@pytest.mark.parametrize("scheme", ["neo4j", "neo4j+s", "neo4j+ssc"])
def test_valid_production_routing_context_matches_driver_and_location(scheme: str) -> None:
    uri = f"{scheme}://db.example:17687?region=eu"
    driver = infrastructure_policy._ORIGINAL_GRAPH_DRIVER(
        GraphDatabase, uri, auth=("user", "password")
    )
    driver.close()
    test = _test_settings(neo4j_uri=f"{scheme}://db.example:17687")
    production = _production_settings(neo4j_uri=uri)

    with pytest.raises(ValueError, match="production"):
        validate_test_endpoints(test, production)


@pytest.mark.parametrize("scheme", ["bolt", "bolt+s", "bolt+ssc"])
def test_production_direct_neo4j_query_matches_driver_rejection(scheme: str) -> None:
    uri = f"{scheme}://db.example:17687?region=eu"

    with pytest.raises(ConfigurationError):
        infrastructure_policy._ORIGINAL_GRAPH_DRIVER(
            GraphDatabase, uri, auth=("user", "password")
        )
    with pytest.raises(InfrastructureSafetyError):
        validate_test_endpoints(
            _test_settings(neo4j_uri=f"{scheme}://test.example:17687"),
            _production_settings(neo4j_uri=uri),
        )


def test_duplicate_production_routing_keys_match_driver_rejection_without_secrets() -> None:
    secret = "never-print-routing-secret"
    uri = f"neo4j://db.example:17687?region={secret}&region=other"

    with pytest.raises(ConfigurationError):
        infrastructure_policy._ORIGINAL_GRAPH_DRIVER(
            GraphDatabase, uri, auth=("user", "password")
        )
    with pytest.raises(InfrastructureSafetyError) as exc_info:
        validate_test_endpoints(
            _test_settings(neo4j_uri="neo4j://test.example:17687"),
            _production_settings(neo4j_uri=uri),
        )

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize("query", ["region=", "region"])
def test_blank_production_routing_values_match_driver_rejection(query: str) -> None:
    uri = f"neo4j://db.example:17687?{query}"

    with pytest.raises(ConfigurationError):
        infrastructure_policy._ORIGINAL_GRAPH_DRIVER(
            GraphDatabase, uri, auth=("user", "password")
        )
    with pytest.raises(InfrastructureSafetyError):
        validate_test_endpoints(
            _test_settings(neo4j_uri="neo4j://test.example:17687"),
            _production_settings(neo4j_uri=uri),
        )


def test_malformed_production_neo4j_uri_fails_closed_without_secrets() -> None:
    secret = "never-print-production-secret"
    test = _test_settings(neo4j_uri="neo4j://test.example:17687")
    production = _production_settings(
        neo4j_uri=f"https://db.example:17687?token={secret}"
    )

    with pytest.raises(InfrastructureSafetyError) as exc_info:
        validate_test_endpoints(test, production)

    assert secret not in str(exc_info.value)


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


@pytest.mark.parametrize(
    ("production_host", "test_host"),
    [("db.example.", "db.example"), ("db.example", "db.example.")],
)
def test_rejects_neo4j_root_dot_alias_equal_to_production(
    production_host: str, test_host: str
) -> None:
    production = _production_settings(
        neo4j_uri=f"neo4j://{production_host}:17687"
    )
    test = _test_settings(neo4j_uri=f"neo4j://{test_host}:17687")

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
def test_integration_neo4j_guard_allows_root_dot_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test = _test_settings(neo4j_uri="neo4j://db.example:17687")
    production = _production_settings(
        pg_dsn="postgresql://prod:prod@production.example:5432/reviewer",
        neo4j_uri="neo4j://production.example:7687",
    )
    endpoints = validate_test_endpoints(test, production)
    calls: list[str] = []

    def original_driver(cls, uri, *, auth=None, **config):
        calls.append(uri)
        return object()

    monkeypatch.setattr(infrastructure_policy, "_ORIGINAL_GRAPH_DRIVER", original_driver)
    infrastructure_policy.install_integration_guards(monkeypatch, endpoints)

    GraphDatabase.driver(
        "neo4j://db.example.:17687",
        auth=(test.neo4j_user, test.neo4j_password),
    )

    assert calls == ["neo4j://db.example.:17687"]


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
