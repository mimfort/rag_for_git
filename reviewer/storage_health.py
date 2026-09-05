"""Классификация недоступности хранилищ и совет по лечению (PRI-268).

Модуль лежит в корне пакета, а не в `mcp/` или `tasks/`: его потребители —
`reviewer/mcp/task_context.py`, `reviewer/tasks/service.py` и
`reviewer/entrypoints/cli.py`, общего подпакета ниже них нет, а импорт
`entrypoints.cli` из сервисного слоя развернул бы направление зависимости.

Класс недоступности решается по ТИПУ исключения (`is_storage_unavailable`), а
уточнение причины внутри этого класса — двумя способами по приоритету: сперва
снова ТИПОМ (`psycopg_pool.PoolTimeout` → `DETAIL_POOL_EXHAUSTED`, PRI-274), и
только когда тип не сузил причину — ТЕКСТОМ (`_DETAIL_PATTERNS` в
`classify_storage_failure`). Тип проверяется первым, потому что он конкретнее
текста: сообщение таймаута пула может случайно нести чужой маркер (например,
auth-паттерн), а свободных соединений от этого не прибавится. Разделение на
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
    неверный ключ, обрыв соединения и таймаут. `RateLimitError` вычитается
    намеренно: free tier — 3 RPM, троттлинг там штатное состояние, его уже
    отрабатывает `with_voyage_retry`, и звать это недоступностью значило бы
    поднимать тревогу на каждом втором прогоне.

    Импорт ленивый: модуль зовут потребители, которым эмбеддер не нужен вовсе,
    и стоимость импорта клиента им платить незачем.
    """
    try:
        from voyageai.error import RateLimitError, VoyageError
    except Exception:  # noqa: BLE001 — без клиента Voyage вопрос не стоит
        return False
    return isinstance(exc, VoyageError) and not isinstance(exc, RateLimitError)


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


def classify_storage_failure(exc: BaseException, *endpoints: str) -> StorageDiagnosis:
    """Класс причины недоступности и уместность совета `reviewer start`.

    Единственный источник обоих машинных решений: и MCP (`mcp/task_context.py`),
    и CLI (`reviewer check`) зовут её, поэтому разойтись им нечем — раньше каждый
    решал сам и оба ошибались одинаково (PRI-277).

    Пустой вердикт при не-storage исключении не заглушка, а рабочая ветка: ею
    чинится Neo4j `AuthError`, который в `is_storage_unavailable` не входит.
    """
    if not is_storage_unavailable(exc):
        return StorageDiagnosis(None, None, None)
    text = str(exc)
    if isinstance(exc, psycopg_pool.PoolTimeout):
        # Тип конкретнее текста: сообщение таймаута может нести чужие маркеры,
        # а свободных соединений от этого не прибавится. Лекарство — поднять
        # pg_pool_max_size или снизить параллелизм, но локальной команды для
        # этого нет, поэтому remedy пуст: `reviewer start` здесь бесполезен.
        return StorageDiagnosis(DETAIL_POOL_EXHAUSTED, None, None)
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
