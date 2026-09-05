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


def test_redacted_never_carries_keyword_style_dsn_literals():
    """Тот же критерий 3, но для keyword-формы DSN (`host=... user=... password=...`).

    `_endpoint_secrets` разбирает её отдельной веткой (не через `urlsplit`) — без
    отдельного теста этот путь остаётся непроверенным.
    """
    endpoint = "host=db.example.com port=5432 user=reviewer password=s3cretpw dbname=prod"
    exc = psycopg.OperationalError(
        "connection to server failed: odd failure for user reviewer "
        "in database prod at db.example.com port 5432 password s3cretpw")
    diagnosis = sh.classify_storage_failure(exc, endpoint)
    for secret in ("s3cretpw", "db.example.com", "reviewer", "prod", "5432"):
        assert secret not in repr(diagnosis), secret


def test_short_literals_do_not_mangle_the_message():
    """Односимвольные логин/пароль не должны выесть свои буквы из обычных слов.

    Литерал короче трёх символов не вымарывается: без порога замена `u`/`p` на
    `[REDACTED]` изуродовала бы каждое слово, где эти буквы встречаются как
    часть текста, а не как секрет (замер PRI-269 показал, что пароль в текст
    libpq и не попадает — риск принят сознательно).
    """
    exc = psycopg.OperationalError(
        "connection to server at 127.0.0.1 failed: superuser setup incomplete")
    diagnosis = sh.classify_storage_failure(exc, "postgresql://u:p@127.0.0.1:5433/reviewer")
    assert "superuser setup incomplete" in diagnosis.redacted


def test_auth_pattern_wins_over_missing_database():
    """Порядок паттернов важен: сообщение может нести оба маркера сразу."""
    exc = psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"\n'
        'FATAL:  database "reviewer" does not exist')
    assert sh.classify_storage_failure(exc, _LOCAL_DSN).detail == sh.DETAIL_AUTH_FAILED


# ---------------------------------------------------------------------------
# Guard: DETAIL_* константы не должны расходиться со словарями формулировок
# ---------------------------------------------------------------------------

def _detail_constants() -> set[str]:
    """Все значения `DETAIL_*` модуля, интроспекцией — не перечислением руками.

    Перечисление руками не заметило бы появления новой константы; guard обязан
    ловить именно этот случай (PRI-277, находка ревью 3).
    """
    return {
        value for name, value in vars(sh).items()
        if name.startswith("DETAIL_") and isinstance(value, str)
    }


def test_detail_constants_are_known_to_task_context_reasons():
    """`reviewer/mcp/task_context.py::DETAIL_REASONS` обязан знать каждый DETAIL_*.

    Иначе `DETAIL_REASONS[diagnosis.detail]` бросит `KeyError` прямо из
    `except`-ветки `_safe` и обрушит весь `build_task_context` — модуль,
    написанный ради fail-open гарантии.
    """
    from reviewer.mcp import task_context

    constants = _detail_constants()
    assert constants, "DETAIL_* константы не найдены — тест ничего не проверяет"
    missing = constants - set(task_context.DETAIL_REASONS)
    assert not missing, f"DETAIL_REASONS не знает про: {missing}"


def test_detail_constants_are_known_to_cli_messages():
    """`reviewer/entrypoints/cli.py::_STORAGE_DETAIL_MESSAGES` обязан знать каждый DETAIL_*.

    Иначе `_STORAGE_DETAIL_MESSAGES[diagnosis.detail]` бросит `KeyError` прямо
    из команды `reviewer check`.
    """
    from reviewer.entrypoints import cli

    constants = _detail_constants()
    assert constants, "DETAIL_* константы не найдены — тест ничего не проверяет"
    missing = constants - set(cli._STORAGE_DETAIL_MESSAGES)
    assert not missing, f"_STORAGE_DETAIL_MESSAGES не знает про: {missing}"


def test_no_endpoints_means_no_redacted_excerpt_even_with_secret():
    """Находка ревью: без эндпоинтов вымарывать нечем, отрывок обязан остаться немым.

    Раньше `_redact(text)` без эндпоинтов не находил секретов и возвращал текст
    как есть — пароль из conninfo уходил наружу целиком. Пустой набор
    эндпоинтов — штатный случай (провайдер без `storage_endpoints` или
    вырожденный `Settings`), поэтому критерий 3 не должен зависеть от него.
    """
    exc = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused"
        " (conninfo: postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer)")
    diagnosis = sh.classify_storage_failure(exc)
    assert diagnosis == sh.StorageDiagnosis(None, None, None)
    assert "s3cretpw" not in repr(diagnosis)


def test_named_class_survives_missing_endpoints():
    """Порядок обязателен: распознанный класс называется даже без эндпоинтов."""
    exc = psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"')
    diagnosis = sh.classify_storage_failure(exc)
    assert diagnosis.detail == sh.DETAIL_AUTH_FAILED
    assert diagnosis.remedy is None
    assert diagnosis.redacted is None


# ---------------------------------------------------------------------------
# mask_endpoint: показать эндпоинт без пароля
# ---------------------------------------------------------------------------


def test_mask_endpoint_hides_password_in_url_form():
    """Пароль уходит, остальное остаётся: строка обязана отвечать «куда я подключаюсь»."""
    masked = sh.mask_endpoint("postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer")
    assert "s3cretpw" not in masked
    assert masked == "postgresql://reviewer:[REDACTED]@127.0.0.1:5433/reviewer"


def test_mask_endpoint_hides_password_in_keyword_form():
    """Keyword-DSN — вторая живая форма conninfo, её нельзя оставить незакрытой."""
    masked = sh.mask_endpoint("host=127.0.0.1 port=5433 user=reviewer password=s3cretpw")
    assert "s3cretpw" not in masked
    assert masked == "host=127.0.0.1 port=5433 user=reviewer password=[REDACTED]"


def test_mask_endpoint_keeps_endpoint_without_password_intact():
    """Без пароля вымарывать нечего — строка обязана дойти до терминала как есть."""
    assert sh.mask_endpoint("bolt://localhost:7687") == "bolt://localhost:7687"


def test_mask_endpoint_hides_percent_encoded_password():
    """Пароль со спецсимволами живёт в DSN percent-encoded; замена идёт по сырой подстроке."""
    masked = sh.mask_endpoint("postgresql://reviewer:p%40ss%3Aword@127.0.0.1:5433/db")
    assert "p%40ss%3Aword" not in masked
    assert masked == "postgresql://reviewer:[REDACTED]@127.0.0.1:5433/db"


# ---------------------------------------------------------------------------
# Тесты PRI-276: какое из хранилищ молчит
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [ServiceUnavailable("no routing servers"),
                                 SessionExpired("session expired")])
def test_storage_backend_neo4j_errors_are_graph(exc):
    assert sh.storage_backend(exc) == sh.BACKEND_GRAPH


def test_storage_backend_operational_error_is_postgres():
    assert sh.storage_backend(psycopg.OperationalError("connection refused")) == sh.BACKEND_POSTGRES


def test_storage_backend_pool_timeout_is_postgres():
    """PoolTimeout — подкласс OperationalError, покрытие обязано совпадать с is_storage_unavailable."""
    exc = psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")
    assert sh.storage_backend(exc) == sh.BACKEND_POSTGRES


@pytest.mark.parametrize("exc", [RuntimeError("no index"),
                                 psycopg.ProgrammingError("syntax error at or near"),
                                 AuthError("unauthorized")])
def test_storage_backend_is_none_for_non_storage_failures(exc):
    """Покрытие совпадает с is_storage_unavailable: что не «хранилище лежит» — то None."""
    assert sh.storage_backend(exc) is None


# ---------------------------------------------------------------------------
# Тесты PRI-274/PRI-272: пул как деталь, эмбеддер как класс
# ---------------------------------------------------------------------------


def test_pool_timeout_gets_pool_exhausted_detail():
    """Исчерпание пула отличимо от закрытого порта — по типу плюс пробе сервера.

    Проба подменена: при живом сервере вердикт тот же, что был до C1, — но
    теперь он опирается на наблюдение, а не на один тип исключения.
    """
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        "postgresql://u:p@localhost:5433/reviewer", probe=_ok_probe)
    assert d.detail == sh.DETAIL_POOL_EXHAUSTED
    assert d.remedy is None
    assert d.redacted is None


def test_pool_timeout_stays_storage_unavailable():
    """Предикат НЕ меняется: иначе замыкание перестанет ловить пул (PRI-275)."""
    assert sh.is_storage_unavailable(psycopg_pool.PoolTimeout("timeout"))


def test_pool_detail_wins_over_text_patterns():
    """Тип конкретнее текста: чужой маркер в сообщении таймаута не решает ничего.

    Решает проба: сервер отвечает, значит свободных соединений нет, а
    auth-паттерн в тексте `PoolTimeout` — совпадение, а не причина.
    """
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("password authentication failed"),
        "postgresql://u:p@localhost:5433/reviewer", probe=_ok_probe)
    assert d.detail == sh.DETAIL_POOL_EXHAUSTED


def test_voyage_errors_are_embedder_unavailable():
    from voyageai.error import APIError, AuthenticationError, ServiceUnavailableError
    for exc in (APIError("HTTP code 403"), AuthenticationError("bad key"),
                ServiceUnavailableError("503")):
        assert sh.is_embedder_unavailable(exc)


def test_rate_limit_is_not_embedder_unavailable():
    """RateLimitError штатно ретраится в with_voyage_retry — это не недоступность."""
    from voyageai.error import RateLimitError
    assert not sh.is_embedder_unavailable(RateLimitError("429"))


def test_storage_errors_are_not_embedder_unavailable():
    assert not sh.is_embedder_unavailable(psycopg.OperationalError("connection refused"))
    assert not sh.is_embedder_unavailable(RuntimeError("boom"))


def test_embedder_error_is_not_storage_unavailable():
    """Эмбеддер не хранилище: reviewer start его не чинит."""
    from voyageai.error import APIError
    assert not sh.is_storage_unavailable(APIError("HTTP code 403"))


# ---------------------------------------------------------------------------
# Тесты C1 (финальное ревью PRI-274): PoolTimeout дополняется наблюдением
# ---------------------------------------------------------------------------

def _raiser(exc: BaseException):
    """Проба, падающая заданным исключением."""
    def probe(dsn: str) -> None:
        raise exc
    return probe


def _ok_probe(dsn: str) -> None:
    """Проба, которая соединилась: сервер жив, значит занят действительно пул."""


def test_pool_timeout_with_live_server_is_pool_exhausted():
    """Проба соединилась — пул действительно занят, лекарства для этого нет."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        _LOCAL_DSN, probe=_ok_probe)
    assert d.detail == sh.DETAIL_POOL_EXHAUSTED
    assert d.remedy is None


def test_pool_timeout_from_stopped_containers_keeps_remedy():
    """КОНТРОЛЬНЫЙ случай: контейнеры не подняты, и лекарство обязано остаться.

    Прод ходит в Postgres только через пул, поэтому остановленный контейнер
    приходит сюда `PoolTimeout`, а не `OperationalError`. До пробы такой сбой
    назывался исчерпанием пула и терял `reviewer start` — это и был дефект C1.
    """
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        _LOCAL_DSN,
        probe=_raiser(psycopg.OperationalError(
            "connection to server at 127.0.0.1, port 5433 failed: Connection refused")))
    assert d.detail is None
    assert d.remedy == sh.REMEDY_START


def test_pool_timeout_with_bad_password_is_auth_failed():
    """Причина ищется в исключении пробы, поэтому уточнения остаются доступны."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        _LOCAL_DSN,
        probe=_raiser(psycopg.OperationalError(
            'FATAL:  password authentication failed for user "reviewer"')))
    assert d.detail == sh.DETAIL_AUTH_FAILED
    assert d.remedy is None


def test_pool_timeout_with_unclassifiable_probe_keeps_remedy():
    """«Не знаю причину» не отбирает единственное лекарство: проба не соединилась."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        _LOCAL_DSN, probe=_raiser(RuntimeError("сокеты в unit-тестах запрещены")))
    assert d.detail is None
    assert d.remedy == sh.REMEDY_START


def test_pool_timeout_without_postgres_endpoint_makes_no_claim():
    """Пробовать нечего — и утверждать «пул занят» тоже нечем."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        "bolt://localhost:7687", probe=_ok_probe)
    assert d.detail is None
    assert d.remedy == sh.REMEDY_START


def test_pool_timeout_probe_targets_the_non_graph_endpoint():
    """Проба идёт в Postgres, а не в Neo4j: у графа своя диагностика и свой тип."""
    seen: list[str] = []

    def probe(dsn: str) -> None:
        seen.append(dsn)

    sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("timeout"),
        "bolt+s://graph.example.com:7687", _LOCAL_DSN, probe=probe)
    assert seen == [_LOCAL_DSN]


def test_pool_timeout_probe_is_not_called_for_other_failures():
    """Проба платится только на ветке PoolTimeout — остальные её не оплачивают."""
    calls: list[str] = []
    sh.classify_storage_failure(
        psycopg.OperationalError("Connection refused"), _LOCAL_DSN,
        probe=lambda dsn: calls.append(dsn))
    assert calls == []
