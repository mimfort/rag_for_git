"""Unit-тесты классификатора недоступности хранилищ (PRI-268)."""
import psycopg
import psycopg_pool
import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired

from reviewer import storage_health as sh


def test_pool_timeout_is_storage_unavailable():
    """PoolTimeout — подкласс OperationalError, ради него всё и затевалось."""
    assert sh.is_storage_unavailable(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"))


def test_operational_error_is_storage_unavailable():
    assert sh.is_storage_unavailable(psycopg.OperationalError("connection refused"))


def test_programming_error_is_not_storage_unavailable():
    """Настоящий баг SQL не должен маскироваться под «хранилище лежит»."""
    assert not sh.is_storage_unavailable(psycopg.ProgrammingError("syntax error at or near"))


@pytest.mark.parametrize("exc", [ServiceUnavailable("no routing servers"),
                                 SessionExpired("session expired")])
def test_neo4j_driver_errors_are_storage_unavailable(exc):
    assert sh.is_storage_unavailable(exc)


def test_neo4j_auth_error_is_not_storage_unavailable():
    """AuthError — это неверные креды, а не лежачее хранилище; лечится не reviewer start."""
    assert not sh.is_storage_unavailable(AuthError("unauthorized"))


def test_unrelated_exception_is_not_storage_unavailable():
    assert not sh.is_storage_unavailable(RuntimeError("boom"))


@pytest.mark.parametrize("endpoint", [
    "postgresql://u:p@127.0.0.1:5433/reviewer",
    "postgresql://u:p@localhost:5433/reviewer",
    "bolt://localhost:7687",
    "bolt://[::1]:7687",
    "host=127.0.0.1 port=5433 dbname=reviewer",
])
def test_loopback_endpoint_gets_remedy(endpoint):
    assert sh.storage_remedy(endpoint) == sh.REMEDY_START


@pytest.mark.parametrize("endpoint", [
    "postgresql://u:p@db.example.com:5432/reviewer",
    "bolt://neo4j.internal:7687",
    "host=db.example.com port=5432 dbname=reviewer",
])
def test_remote_endpoint_gets_no_remedy(endpoint):
    """Удалённому деплою локальный docker-стек ничего не чинит (критерий 3)."""
    assert sh.storage_remedy(endpoint) is None


def test_remedy_when_at_least_one_endpoint_is_local():
    assert sh.storage_remedy("postgresql://u:p@db.example.com:5432/reviewer",
                             "bolt://localhost:7687") == sh.REMEDY_START


def test_no_endpoints_means_no_remedy():
    assert sh.storage_remedy() is None
    assert sh.storage_remedy("") is None
