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


# ---------------------------------------------------------------------------
# Тесты PRI-277: класс причины отдельно от уместности лекарства
# ---------------------------------------------------------------------------

_LOCAL_DSN = "postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer"
_REMOTE_DSN = "postgresql://reviewer:s3cretpw@db.example.com:5432/prod"


def test_auth_failure_is_classified_and_loses_remedy():
    """Контейнеры живы, пароль неверен — reviewer start уже выполнен и не поможет."""
    exc = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "reviewer"')
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail == sh.DETAIL_AUTH_FAILED
    assert diagnosis.remedy is None
    assert diagnosis.redacted is None


def test_missing_database_is_classified_and_loses_remedy():
    exc = psycopg.OperationalError(
        'connection failed: FATAL:  database "nosuchdb" does not exist')
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail == sh.DETAIL_MISSING_DATABASE
    assert diagnosis.remedy is None
    assert diagnosis.redacted is None


def test_unrecognised_local_failure_keeps_remedy_and_redacts():
    """Нераспознанный сбой ведёт себя как прежде, но перестаёт быть немым."""
    exc = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused")
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail is None
    assert diagnosis.remedy == sh.REMEDY_START
    assert "[REDACTED]" in diagnosis.redacted


def test_unrecognised_remote_failure_has_no_remedy():
    """Удалённому деплою локальный docker-стек не помогает и здесь."""
    exc = psycopg.OperationalError("connection to server failed: Connection refused")
    diagnosis = sh.classify_storage_failure(exc, _REMOTE_DSN)
    assert diagnosis.detail is None
    assert diagnosis.remedy is None


def test_non_storage_exception_is_empty_verdict():
    """AuthError neo4j — неверные креды, не лежачее хранилище: совета быть не должно."""
    diagnosis = sh.classify_storage_failure(AuthError("unauthorized"), "bolt://localhost:7687")
    assert (diagnosis.detail, diagnosis.remedy, diagnosis.redacted) == (None, None, None)


def test_redacted_never_carries_dsn_literals():
    """Критерий 3: ни пароль, ни хост, ни имя пользователя, ни база в выдачу не попадают."""
    exc = psycopg.OperationalError(
        "connection to server at db.example.com, port 5432 failed: "
        'FATAL:  odd failure for user "reviewer" in database "prod" (password s3cretpw)')
    diagnosis = sh.classify_storage_failure(exc, _REMOTE_DSN)
    for secret in ("s3cretpw", "db.example.com", "reviewer", "prod", "5432"):
        assert secret not in repr(diagnosis), secret


def test_short_literals_do_not_mangle_the_message():
    """Односимвольный пароль не должен выесть все свои буквы из сообщения.

    Литерал короче трёх символов не вымарывается: замена превратила бы текст в
    кашу, а замер PRI-269 показал, что пароль в текст libpq и не попадает.
    """
    exc = psycopg.OperationalError("connection to server at 127.0.0.1 failed")
    diagnosis = sh.classify_storage_failure(exc, "postgresql://u:p@127.0.0.1:5433/reviewer")
    assert "connection to server" in diagnosis.redacted


def test_auth_pattern_wins_over_missing_database():
    """Порядок паттернов важен: сообщение может нести оба маркера сразу."""
    exc = psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"\n'
        'FATAL:  database "reviewer" does not exist')
    assert sh.classify_storage_failure(exc, _LOCAL_DSN).detail == sh.DETAIL_AUTH_FAILED
