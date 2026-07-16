from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from neo4j import GraphDatabase
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic_settings import BaseSettings, SettingsConfigDict
from pytest_socket import disable_socket, enable_socket

from reviewer.config.settings import Settings, _resolve_env_file

_ORIGINAL_PSYCOPG_CONNECT = psycopg.connect
_ORIGINAL_CONNECTION_CONNECT = psycopg.Connection.connect.__func__
_ORIGINAL_POOL_INIT = ConnectionPool.__init__
_ORIGINAL_POOL_CONNECTION = ConnectionPool.connection
_ORIGINAL_GRAPH_DRIVER = GraphDatabase.driver.__func__

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_NEO4J_SCHEMES = {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
_START_TEST_SERVICES = (
    "docker compose --profile test up -d --wait paradedb-test neo4j-test"
)


class InfrastructureTestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(), env_prefix="TEST_", extra="ignore", frozen=True
    )

    pg_dsn: str = (
        "postgresql://reviewer_test:reviewer_test@localhost:55433/"
        "reviewer_test?connect_timeout=2"
    )
    neo4j_uri: str = "neo4j://localhost:17687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "reviewer_test_pass"


@dataclass(frozen=True)
class PostgresTestEndpoint:
    dsn: str = field(repr=False)
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    connect_timeout: int

    @property
    def target(self) -> tuple[str, int, str]:
        return self.host, self.port, self.dbname

    @property
    def connection_identity(self) -> tuple[str, int, str, str, str]:
        return self.host, self.port, self.dbname, self.user, self.password


@dataclass(frozen=True)
class Neo4jTestEndpoint:
    uri: str
    scheme: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)

    @property
    def target(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


@dataclass(frozen=True)
class ValidatedTestEndpoints:
    postgres: PostgresTestEndpoint
    neo4j: Neo4jTestEndpoint


def _normalize_host(host: str) -> str:
    normalized = host.strip().lower().strip("[]")
    return "loopback" if normalized in _LOOPBACK_HOSTS else normalized


def _conninfo_dict(conninfo: str, **kwargs: Any) -> dict[str, str]:
    try:
        return conninfo_to_dict(conninfo, **kwargs)
    except Exception:
        raise ValueError("Malformed Postgres test DSN") from None


def _parse_postgres(
    conninfo: str,
    *,
    connection_kwargs: dict[str, Any] | None = None,
    require_test_database: bool,
    require_timeout: bool,
    require_credentials: bool = True,
) -> PostgresTestEndpoint:
    params = _conninfo_dict(conninfo, **(connection_kwargs or {}))
    if params.get("service"):
        raise ValueError("Postgres service DSNs are not allowed in tests")

    host = params.get("host", "")
    port_text = params.get("port", "5432")
    if not host:
        raise ValueError("Postgres test DSN must include a host")
    if "," in host or "," in port_text:
        raise ValueError("Multi-host Postgres DSNs are not allowed in tests")

    user = params.get("user", "")
    password = params.get("password", "")
    dbname = params.get("dbname", "")
    if require_credentials and not user:
        raise ValueError("Postgres test DSN must include a user")
    if require_credentials and not password:
        raise ValueError("Postgres test DSN must include a password")
    if not dbname:
        raise ValueError("Postgres test DSN must include a database name")
    if require_test_database and not dbname.endswith("_test"):
        raise ValueError("Postgres test database name must end with _test")

    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise ValueError("Postgres test DSN has an invalid port") from None
    if not 1 <= port <= 65535:
        raise ValueError("Postgres test DSN has an invalid port")

    timeout_text = params.get("connect_timeout")
    if require_timeout and timeout_text is None:
        raise ValueError("Postgres test DSN must set connect_timeout between 1 and 5")
    try:
        connect_timeout = int(timeout_text) if timeout_text is not None else 2
    except (TypeError, ValueError):
        raise ValueError("Postgres test DSN has an invalid connect_timeout") from None
    if require_timeout and not 1 <= connect_timeout <= 5:
        raise ValueError("Postgres test connect_timeout must be between 1 and 5")

    return PostgresTestEndpoint(
        dsn=conninfo,
        host=_normalize_host(host),
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        connect_timeout=connect_timeout,
    )


def _parse_neo4j(
    uri: str, user: str, password: str, *, require_credentials: bool = True
) -> Neo4jTestEndpoint:
    try:
        parsed = urlsplit(uri)
        port = 7687 if parsed.port is None else parsed.port
    except ValueError:
        raise ValueError("Malformed Neo4j test URI") from None

    if not 1 <= port <= 65535:
        raise ValueError("Malformed Neo4j test URI")

    if (
        parsed.scheme.lower() not in _NEO4J_SCHEMES
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Malformed Neo4j test URI")
    if require_credentials and not user:
        raise ValueError("Neo4j test user must not be empty")
    if require_credentials and not password:
        raise ValueError("Neo4j test password must not be empty")

    return Neo4jTestEndpoint(
        uri=uri,
        scheme=parsed.scheme.lower(),
        host=_normalize_host(parsed.hostname),
        port=port,
        user=user,
        password=password,
    )


def validate_test_endpoints(
    test: InfrastructureTestSettings, production: Settings
) -> ValidatedTestEndpoints:
    postgres = _parse_postgres(
        test.pg_dsn, require_test_database=True, require_timeout=True
    )
    try:
        production_postgres = _parse_postgres(
            production.pg_dsn,
            require_test_database=False,
            require_timeout=False,
            require_credentials=False,
        )
    except ValueError:
        production_postgres = None
    if production_postgres is not None and postgres.target == production_postgres.target:
        raise ValueError("Postgres test target must differ from production")

    neo4j = _parse_neo4j(test.neo4j_uri, test.neo4j_user, test.neo4j_password)
    try:
        production_neo4j = _parse_neo4j(
            production.neo4j_uri,
            production.neo4j_user,
            production.neo4j_password,
            require_credentials=False,
        )
    except ValueError:
        production_neo4j = None
    if production_neo4j is not None and neo4j.target == production_neo4j.target:
        raise ValueError("Neo4j test target must differ from production")

    return ValidatedTestEndpoints(postgres=postgres, neo4j=neo4j)


def apply_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    test: InfrastructureTestSettings,
) -> None:
    monkeypatch.setenv("PG_DSN", test.pg_dsn)
    monkeypatch.setenv("NEO4J_URI", test.neo4j_uri)
    monkeypatch.setenv("NEO4J_USER", test.neo4j_user)
    monkeypatch.setenv("NEO4J_PASSWORD", test.neo4j_password)


def _unit_postgres_blocked(*args: Any, **kwargs: Any) -> None:
    pytest.fail("Unit-тесту запрещено подключение к Postgres", pytrace=False)


def _unit_postgres_class_blocked(cls: type, *args: Any, **kwargs: Any) -> None:
    pytest.fail("Unit-тесту запрещено подключение к Postgres", pytrace=False)


def _unit_pool_blocked(self: ConnectionPool, *args: Any, **kwargs: Any) -> None:
    pytest.fail("Unit-тесту запрещено создание пула Postgres", pytrace=False)


def _unit_neo4j_blocked(cls: type, *args: Any, **kwargs: Any) -> None:
    pytest.fail("Unit-тесту запрещено подключение к Neo4j", pytrace=False)


def install_unit_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_socket()
    disable_socket(allow_unix_socket=True)
    monkeypatch.setattr(psycopg, "connect", _unit_postgres_blocked)
    monkeypatch.setattr(
        psycopg.Connection, "connect", classmethod(_unit_postgres_class_blocked)
    )
    monkeypatch.setattr(ConnectionPool, "__init__", _unit_pool_blocked)
    monkeypatch.setattr(GraphDatabase, "driver", classmethod(_unit_neo4j_blocked))


def _fail_wrong_postgres_endpoint() -> None:
    pytest.fail("Integration-тест попытался подключиться не к test Postgres", pytrace=False)


def _fail_wrong_neo4j_endpoint() -> None:
    pytest.fail("Integration-тест попытался подключиться не к test Neo4j", pytrace=False)


def _connection_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    names = {
        "connect_timeout",
        "dbname",
        "host",
        "hostaddr",
        "password",
        "port",
        "service",
        "user",
    }
    return {key: value for key, value in kwargs.items() if key in names}


def _assert_postgres_allowed(
    conninfo: str,
    endpoint: PostgresTestEndpoint,
    connection_kwargs: dict[str, Any] | None = None,
) -> None:
    try:
        requested = _parse_postgres(
            conninfo,
            connection_kwargs=connection_kwargs,
            require_test_database=False,
            require_timeout=False,
        )
    except ValueError:
        _fail_wrong_postgres_endpoint()
    if requested.connection_identity != endpoint.connection_identity:
        _fail_wrong_postgres_endpoint()


def _capped_timeout(value: Any, cap: int) -> float:
    try:
        return min(float(value), float(cap))
    except (TypeError, ValueError):
        pytest.fail("Некорректный timeout подключения к test-инфраструктуре", pytrace=False)


def _pool_timeout() -> None:
    pytest.fail(
        f"Test-инфраструктура недоступна; запустите: {_START_TEST_SERVICES}",
        pytrace=False,
    )


def install_integration_guards(
    monkeypatch: pytest.MonkeyPatch, endpoints: ValidatedTestEndpoints
) -> None:
    postgres = endpoints.postgres
    neo4j = endpoints.neo4j

    def guarded_connect(conninfo: str = "", **kwargs: Any):
        _assert_postgres_allowed(conninfo, postgres, _connection_params(kwargs))
        kwargs["connect_timeout"] = postgres.connect_timeout
        return _ORIGINAL_PSYCOPG_CONNECT(conninfo, **kwargs)

    def guarded_class_connect(cls: type, conninfo: str = "", **kwargs: Any):
        _assert_postgres_allowed(conninfo, postgres, _connection_params(kwargs))
        kwargs["connect_timeout"] = postgres.connect_timeout
        return _ORIGINAL_CONNECTION_CONNECT(cls, conninfo, **kwargs)

    def guarded_pool_init(
        self: ConnectionPool, conninfo: str = "", *args: Any, **kwargs: Any
    ) -> None:
        pool_connection_kwargs = dict(kwargs.get("kwargs") or {})
        _assert_postgres_allowed(conninfo, postgres, pool_connection_kwargs)
        pool_connection_kwargs["connect_timeout"] = postgres.connect_timeout
        kwargs["kwargs"] = pool_connection_kwargs
        kwargs["timeout"] = _capped_timeout(
            kwargs.get("timeout", postgres.connect_timeout), postgres.connect_timeout
        )
        kwargs["reconnect_timeout"] = _capped_timeout(
            kwargs.get("reconnect_timeout", postgres.connect_timeout),
            postgres.connect_timeout,
        )
        try:
            _ORIGINAL_POOL_INIT(self, conninfo, *args, **kwargs)
        except PoolTimeout:
            _pool_timeout()

    @contextmanager
    def guarded_pool_connection(self: ConnectionPool, timeout: float | None = None):
        capped = _capped_timeout(
            timeout if timeout is not None else postgres.connect_timeout,
            postgres.connect_timeout,
        )
        try:
            with _ORIGINAL_POOL_CONNECTION(self, timeout=capped) as connection:
                yield connection
        except PoolTimeout:
            _pool_timeout()

    def guarded_graph_driver(cls: type, uri: str, *, auth: Any = None, **config: Any):
        try:
            requested = _parse_neo4j(uri, *(auth if isinstance(auth, tuple) else ("", "")))
        except (TypeError, ValueError):
            _fail_wrong_neo4j_endpoint()
        if requested.target != neo4j.target or auth != (neo4j.user, neo4j.password):
            _fail_wrong_neo4j_endpoint()
        config["connection_timeout"] = _capped_timeout(
            config.get("connection_timeout", postgres.connect_timeout),
            postgres.connect_timeout,
        )
        config["connection_acquisition_timeout"] = _capped_timeout(
            config.get("connection_acquisition_timeout", postgres.connect_timeout),
            postgres.connect_timeout,
        )
        return _ORIGINAL_GRAPH_DRIVER(cls, uri, auth=auth, **config)

    monkeypatch.setattr(psycopg, "connect", guarded_connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(guarded_class_connect))
    monkeypatch.setattr(ConnectionPool, "__init__", guarded_pool_init)
    monkeypatch.setattr(ConnectionPool, "connection", guarded_pool_connection)
    monkeypatch.setattr(GraphDatabase, "driver", classmethod(guarded_graph_driver))
