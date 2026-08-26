"""Классификация недоступности хранилищ и совет по лечению (PRI-268).

Модуль лежит в корне пакета, а не в `mcp/` или `tasks/`: его потребители —
`reviewer/mcp/task_context.py`, `reviewer/tasks/service.py` и
`reviewer/entrypoints/cli.py`, общего подпакета ниже них нет, а импорт
`entrypoints.cli` из сервисного слоя развернул бы направление зависимости.

Решение принимается по ТИПУ исключения, а не по его тексту: в тексте
`psycopg.OperationalError` живёт DSN с паролем. Этим модуль отличается от
соседнего `reviewer/config/fetch_errors.py`, который судит по именам классов в
MRO: там исключения приходят от сменного VCS-клиента и модуль обязан остаться
без зависимостей, здесь — от жёстко закреплённых драйверов, без которых проект
не работает вовсе, и точность важнее развязки.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import psycopg
from neo4j.exceptions import ServiceUnavailable, SessionExpired

CAUSE_STORAGE_UNAVAILABLE = "storage_unavailable"
CAUSE_UNKNOWN = "unknown"
REMEDY_START = "reviewer start"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


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

    `psycopg_pool.PoolTimeout` является подклассом `psycopg.OperationalError`,
    поэтому одна проверка покрывает и таймаут пула, и обрыв соединения.
    `psycopg.ProgrammingError` подклассом не является и под «хранилище лежит»
    не маскируется: настоящий баг SQL обязан остаться видимым. У neo4j по той же
    причине берутся только `ServiceUnavailable`/`SessionExpired` (ветка
    `DriverError`), но не `AuthError` — неверные креды лечатся не запуском
    контейнеров.
    """
    return isinstance(exc, (psycopg.OperationalError, ServiceUnavailable, SessionExpired))


def storage_remedy(*endpoints: str) -> str | None:
    """Команда-лекарство, если хоть один эндпоинт локальный, иначе None."""
    if any(is_loopback_endpoint(endpoint) for endpoint in endpoints if endpoint):
        return REMEDY_START
    return None
