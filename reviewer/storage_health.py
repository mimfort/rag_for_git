"""Классификация недоступности хранилищ и совет по лечению (PRI-268).

Модуль лежит в корне пакета, а не в `mcp/` или `tasks/`: его потребители —
`reviewer/mcp/task_context.py`, `reviewer/tasks/service.py` и
`reviewer/entrypoints/cli.py`, общего подпакета ниже них нет, а импорт
`entrypoints.cli` из сервисного слоя развернул бы направление зависимости.

Класс недоступности решается по ТИПУ исключения (`is_storage_unavailable`), а
уточнение причины внутри этого класса — тремя способами по приоритету: сперва
снова ТИПОМ (`psycopg_pool.PoolTimeout`, PRI-274), затем — на этой же ветке —
НАБЛЮДЕНИЕМ (одноразовая проба сервера, см. `_classify_pool_timeout`), и только
когда ни то ни другое причину не сузило — ТЕКСТОМ (`_DETAIL_PATTERNS` в
`classify_storage_failure`). Тип проверяется первым, потому что он конкретнее
текста: сообщение таймаута пула может случайно нести чужой маркер (например,
auth-паттерн), а свободных соединений от этого не прибавится. Но одного типа
здесь мало: прод ходит в Postgres ТОЛЬКО через пул, поэтому остановленный
контейнер приходит тем же `PoolTimeout`, что и реально занятый пул, — без пробы
лежачее хранилище называлось бы исчерпанным пулом и теряло `reviewer start`.
Разделение на
класс и причину не косметическое: при сбое на этапе установления соединения
libpq не возвращает результат, поэтому SQLSTATE пуст и ветвиться по коду ошибки
нельзя (замер PRI-269). Прежний довод «в тексте живёт DSN с паролем» этим же
замером опровергнут для данного класса ошибок: пароль в `str(exc)` не попадает,
а хост, порт, имя пользователя и имя базы — попадают, и потому вымарываются.
Наружу текст выходит только там, где класс назвать не удалось ни типом, ни
паттерном.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg
import psycopg_pool
from neo4j.exceptions import ServiceUnavailable, SessionExpired

CAUSE_STORAGE_UNAVAILABLE = "storage_unavailable"
CAUSE_UNKNOWN = "unknown"
REMEDY_START = "reviewer start"

BACKEND_GRAPH = "graph"
BACKEND_POSTGRES = "postgres"

# Эмбеддер — третий источник контекста наравне с двумя хранилищами. Класс у него
# свой, а не уточнение storage_unavailable: Voyage не хранилище, контейнеры при
# его отказе подняты, и `reviewer start` не лечит ничего.
CAUSE_EMBEDDER_UNAVAILABLE = "embedder_unavailable"
SOURCE_EMBEDDER = "embedder"

# Классы `voyageai.error`, которые недоступностью НЕ считаются. Имена, а не сами
# классы: набор резолвится лениво, и версия клиента, не знающая какого-то имени,
# обязана оставить предикат рабочим, а не уронить импорт.
_NOT_EMBEDDER_DOWN = (
    "RateLimitError",         # штатный троттлинг free tier, ретраится сам
    "InvalidRequestError",    # 400: запрос неверен (бросается и локально, без сети)
    "MalformedRequestError",  # 422: тело запроса не разобрано
    "VideoProcessingError",   # контент не обработан — сервис при этом отвечает
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

# Единственный литерал маски в модуле: и вымарывание чужого текста, и показ
# самого эндпоинта обязаны выглядеть одинаково, иначе читатель вывода решит,
# что это два разных механизма.
_MASK = "[REDACTED]"


def is_loopback_endpoint(value: str) -> bool:
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


def is_storage_unavailable(exc: BaseException) -> bool:
    """Не отвечает ли хранилище — в отличие от прочих сбоев секции.

    `psycopg_pool.PoolTimeout` является подклассом `psycopg.OperationalError` и
    остаётся внутри этого класса намеренно (PRI-274): вывести его отсюда значило
    бы лишить замыкание `_StorageState` возможности поймать его на первом же
    сбое и вернуть восемь таймаутов пула вместо одного. Отличается он не
    предикатом, а `cause_detail` — см. `classify_storage_failure`.
    `psycopg.ProgrammingError` подклассом не является и под «хранилище лежит»
    не маскируется: настоящий баг SQL обязан остаться видимым. У neo4j по той же
    причине берутся только `ServiceUnavailable`/`SessionExpired` (ветка
    `DriverError`), но не `AuthError` — неверные креды лечатся не запуском
    контейнеров.
    """
    return isinstance(exc, (psycopg.OperationalError, ServiceUnavailable, SessionExpired))


def storage_backend(exc: BaseException) -> str | None:
    """Какое из хранилищ молчит, если молчит вообще.

    Пара к `is_storage_unavailable`: та отвечает «лечится ли подъёмом
    контейнеров», эта — «кого именно поднимать». Покрытие у них общее, поэтому
    None здесь означает ровно то же, что False там.

    Решает тип исключения, а не текст: в тексте `OperationalError` живёт DSN с
    паролем (тот же мотив, что у `classify_storage_failure`).
    """
    if isinstance(exc, (ServiceUnavailable, SessionExpired)):
        return BACKEND_GRAPH
    if isinstance(exc, psycopg.OperationalError):
        return BACKEND_POSTGRES
    return None


def is_embedder_unavailable(exc: BaseException) -> bool:
    """Не отвечает ли эмбеддер — в отличие от штатного троттлинга.

    Иерархия `voyageai.error` плоская: все классы — прямые наследники
    `VoyageError`, поэтому одна проверка покрывает отказ фронтенда (403),
    неверный ключ, обрыв соединения и таймаут. Вычитаемые классы приходится
    перечислять по той же причине — порядком веток исключение не выразить.

    Вычитается `RateLimitError`: free tier — 3 RPM, троттлинг там штатное
    состояние, его уже отрабатывает `with_voyage_retry`, и звать это
    недоступностью значило бы поднимать тревогу на каждом втором прогоне.
    Вместе с ним — ошибки самого запроса: `InvalidRequestError` (400, клиент
    бросает её и локально, без сети), `MalformedRequestError` (422) и
    `VideoProcessingError`. Все три означают «запрос неверен / контент не
    обработан», а не «сервис лежит», и цена ошибки здесь несимметрична: одна
    такая ошибка внутри `warm_board` ставит `embedder_failed` и снимает четыре
    секции контекста задачи при полностью живом эмбеддере.

    Импорт ленивый: модуль зовут потребители, которым эмбеддер не нужен вовсе,
    и стоимость импорта клиента им платить незачем. Отсутствующий класс не
    ломает предикат: словарь версии клиента может не знать нового имени, и
    пропущенное вычитание безопаснее отказа классифицировать вовсе.
    """
    try:
        from voyageai import error as voyage_error
    except Exception:  # noqa: BLE001 — без клиента Voyage вопрос не стоит
        return False
    base = getattr(voyage_error, "VoyageError", None)
    if base is None:
        return False
    excluded = tuple(
        cls for cls in (getattr(voyage_error, name, None) for name in _NOT_EMBEDDER_DOWN)
        if isinstance(cls, type)
    )
    return isinstance(exc, base) and not isinstance(exc, excluded)


def storage_remedy(*endpoints: str) -> str | None:
    """Команда-лекарство, если хоть один эндпоинт локальный, иначе None."""
    if any(is_loopback_endpoint(endpoint) for endpoint in endpoints if endpoint):
        return REMEDY_START
    return None


def mask_endpoint(value: str) -> str:
    """Показать DSN/URI без пароля, сохранив всё остальное.

    Вывод `reviewer check` попадает в issue, чат и демонстрацию экрана, поэтому
    пароль до терминала не доходит. Хост, порт, пользователь и имя базы остаются
    намеренно: без них строка перестаёт отвечать на вопрос «куда я подключаюсь»,
    и печатать её было бы незачем.

    Это не то же, что `_redact`: там вымарывается ЧУЖОЙ текст (сообщение
    драйвера) по литералам эндпоинта, здесь — сам эндпоинт, поэтому пароль
    известен точно и остальные поля трогать не нужно.
    """
    rendered = value
    try:
        password = urlsplit(value).password
    except ValueError:
        password = None
    if password:
        # Замена подстроки `:<пароль>@`, а не пересборка через urlunsplit:
        # пересборка нормализует строку, и оператор увидел бы не тот DSN, что
        # у него в .env. `urlsplit` не декодирует, поэтому percent-encoded
        # пароль совпадает буквально.
        rendered = rendered.replace(f":{password}@", f":{_MASK}@", 1)
    # Вторая живая форма conninfo — keyword-style, у неё userinfo нет вовсе.
    return re.sub(r"(\bpassword=)[^\s]+", lambda m: m.group(1) + _MASK, rendered)


DETAIL_AUTH_FAILED = "auth_failed"
DETAIL_MISSING_DATABASE = "missing_database"
DETAIL_POOL_EXHAUSTED = "pool_exhausted"

# Обрезка отрывка: диагностика не должна раздувать payload MCP-тула.
_MAX_REDACTED_CHARS = 200
# Литерал короче этого не вымарывается: замена односимвольного пароля выела бы
# из сообщения все вхождения одной буквы и сделала бы его нечитаемым.
_MIN_SECRET_CHARS = 3

# Порядок значим: сообщение может нести оба маркера, и auth-сбой конкретнее
# (тот же класс граблей, что у ConnectTimeout в config/fetch_errors.py).
_DETAIL_PATTERNS = (
    (DETAIL_AUTH_FAILED, re.compile(r"password authentication failed", re.IGNORECASE)),
    (DETAIL_MISSING_DATABASE, re.compile(r"database\b.*\bdoes not exist", re.IGNORECASE)),
)


@dataclass(frozen=True)
class StorageDiagnosis:
    """Вердикт по одному сбою: класс причины, лекарство и безопасный отрывок.

    `detail` — закрытая метка либо None, если класс назвать не удалось.
    `remedy` — команда-лекарство, только когда она действительно применима.
    `redacted` — вымаранный отрывок текста; заполнен лишь при пустом `detail`.
    """

    detail: str | None
    remedy: str | None
    redacted: str | None


def _endpoint_secrets(endpoint: str) -> set[str]:
    """Чувствительные литералы одного эндпоинта: хост, порт, пользователь, пароль, база."""
    values: set[str] = set()
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        parts = None
    if parts is not None:
        for value in (parts.hostname, parts.username, parts.password,
                      parts.path.lstrip("/")):
            if value:
                values.add(str(value))
        try:
            if parts.port:
                values.add(str(parts.port))
        except ValueError:
            pass  # порт не число — вымарывать нечего, остальные поля уже собраны
    for keyword in ("host", "user", "password", "dbname", "port"):
        values.update(re.findall(rf"\b{keyword}=([^\s]+)", endpoint))
    return {value for value in values if len(value) >= _MIN_SECRET_CHARS}


def _redact(text: str, *endpoints: str) -> str:
    """Заменить литералы эндпоинтов на [REDACTED], схлопнуть пробелы и обрезать.

    Длинные значения заменяются первыми: иначе значение-префикс оставило бы хвост
    более длинного. Обрезка идёт ПОСЛЕ вымарывания — иначе литерал, попавший на
    границу, уцелел бы наполовину.
    """
    secrets: set[str] = set()
    for endpoint in endpoints:
        if endpoint:
            secrets |= _endpoint_secrets(endpoint)
    rendered = text
    for secret in sorted(secrets, key=len, reverse=True):
        rendered = rendered.replace(secret, _MASK)
    return " ".join(rendered.split())[:_MAX_REDACTED_CHARS]


# Сколько ждёт проба сервера. Цена платится один раз за вызов
# `build_task_context` (дальше держит замыкание `_StorageState`) и только на уже
# медленном пути отказа, где пул уже отдал свой 30-секундный таймаут: две
# секунды поверх тридцати — сознательный размен ради того, чтобы лежачий
# контейнер не назывался занятым пулом.
_PROBE_TIMEOUT_SECONDS = 2

# Схемы графового эндпоинта, включая `+s`/`+ssc`-варианты: у Neo4j пула нет, и
# пробовать его этим кодом нечем.
_GRAPH_SCHEMES = frozenset({"bolt", "neo4j"})


def _probe_postgres(dsn: str) -> None:
    """Соединиться с Postgres напрямую, мимо пула, и немедленно закрыть.

    Единственное наблюдение, отличающее «пул занят» от «сервера нет»: сам
    `PoolTimeout` причины не несёт (в тексте только «couldn't get a connection
    after N sec»), а состояние пула сюда не доходит.
    """
    with psycopg.connect(dsn, connect_timeout=_PROBE_TIMEOUT_SECONDS):
        pass


def _is_graph_endpoint(value: str) -> bool:
    """Графовый ли это URI (`bolt`/`neo4j`, в том числе `+s`/`+ssc`)."""
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        return False
    return scheme.split("+", 1)[0].lower() in _GRAPH_SCHEMES


def _postgres_endpoint(*endpoints: str) -> str | None:
    """Первый не-графовый эндпоинт: его и пробуем."""
    for endpoint in endpoints:
        if endpoint and not _is_graph_endpoint(endpoint):
            return endpoint
    return None


def _classify_pool_timeout(probe: Callable[[str], None],
                           *endpoints: str) -> StorageDiagnosis:
    """Занят ли пул на самом деле — решает проба, а не один тип исключения.

    Через `ChunkStore._connect()` — единственный способ, которым прод ходит в
    Postgres, — остановленный контейнер отдаёт `PoolTimeout`, а не отказ
    соединения: фоновые воркеры пула молча ретраятся, а `getconn` ждёт таймаут.
    Поэтому тип обязан быть дополнен наблюдением: соединились — пул
    действительно занят; не соединились — причина в исключении ПРОБЫ, и она
    классифицируется обычным путём, возвращая лежачему контейнеру `reviewer
    start`, а протухшему паролю — `auth_failed`.
    """
    dsn = _postgres_endpoint(*endpoints)
    if dsn is None:
        # Наблюдать нечем. Утверждать «пул занят» на одном типе — это ровно тот
        # дефект, ради которого проба и заведена, поэтому класс не называем и
        # оставляем лекарство, если оно вообще применимо.
        return StorageDiagnosis(None, storage_remedy(*endpoints), None)
    try:
        probe(dsn)
    except psycopg_pool.PoolTimeout:
        # Проба пулом не пользуется, поэтому его таймаут прийти оттуда не может;
        # ветка страхует от бесконечной рекурсии, а не описывает живой случай.
        return StorageDiagnosis(DETAIL_POOL_EXHAUSTED, None, None)
    except Exception as probe_exc:  # noqa: BLE001 — причина ищется в самом исключении
        verdict = classify_storage_failure(probe_exc, *endpoints, probe=probe)
        if verdict.detail is None and verdict.remedy is None and verdict.redacted is None:
            # «Не знаю причину» не должно отбирать единственное лекарство:
            # проба всё-таки не соединилась, и занятость пула тут ни при чём.
            return StorageDiagnosis(None, storage_remedy(*endpoints), None)
        return verdict
    # Сервер отвечает, значит свободных соединений действительно нет. Лекарство —
    # поднять PG_POOL_MAX_SIZE или снизить параллелизм, но локальной команды для
    # этого нет, поэтому remedy пуст: `reviewer start` здесь бесполезен.
    return StorageDiagnosis(DETAIL_POOL_EXHAUSTED, None, None)


def classify_storage_failure(exc: BaseException, *endpoints: str,
                             probe: Callable[[str], None] | None = None) -> StorageDiagnosis:
    """Класс причины недоступности и уместность совета `reviewer start`.

    Единственный источник обоих машинных решений: и MCP (`mcp/task_context.py`),
    и CLI (`reviewer check`) зовут её, поэтому разойтись им нечем — раньше каждый
    решал сам и оба ошибались одинаково (PRI-277).

    `probe` — проба сервера для ветки `PoolTimeout`, keyword-only и с реальным
    дефолтом: в проде её не передают, поэтому сигнатура для существующих
    вызывающих не изменилась, а unit-тесты подменяют её и не открывают сокет.

    Пустой вердикт при не-storage исключении не заглушка, а рабочая ветка: ею
    чинится Neo4j `AuthError`, который в `is_storage_unavailable` не входит.
    """
    if not is_storage_unavailable(exc):
        return StorageDiagnosis(None, None, None)
    if isinstance(exc, psycopg_pool.PoolTimeout):
        return _classify_pool_timeout(probe or _probe_postgres, *endpoints)
    text = str(exc)
    for detail, pattern in _DETAIL_PATTERNS:
        if pattern.search(text):
            # Хранилище ответило отказом, значит контейнеры подняты и лекарство
            # неприменимо; класс уже назван, поэтому текст наружу не нужен.
            return StorageDiagnosis(detail, None, None)
    if not any(endpoints):
        # Вымарывать нечем: без эндпоинтов литералы не из чего извлечь, и отрывок
        # ушёл бы наружу сырым. Возвращаемся к прежнему немому поведению —
        # критерий «секретов в диагностике нет» важнее информативности.
        return StorageDiagnosis(None, None, None)
    return StorageDiagnosis(None, storage_remedy(*endpoints), _redact(text, *endpoints))
