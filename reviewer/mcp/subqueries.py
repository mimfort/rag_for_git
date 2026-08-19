"""Подзапросы ретрива из структуры и сущностей задачи (PRI-255).

Один эмбеддинг на весь текст задачи не близок ни к одной из её тем, а
хвостовые пункты «Что сделать» не ищутся вовсе. Модуль детерминированно
разбирает текст на подзапросы: по пункту списка требований — свой запрос,
плюс один пул технических идентификаторов из прозаической части.

Ни сети, ни БД, ни LLM: расход токенов от этого модуля не растёт, а его
поведение проверяется на текстах задач.
"""
from __future__ import annotations

import re

MAX_SUBQUERIES = 20
"""Предохранитель против вырожденного текста.

Число подзапросов производно от размера задачи (пункты требований), но
текст из шестидесяти пунктов не должен превращаться в шестьдесят обращений
к хранилищу.
"""

MAX_IDENTIFIERS = 12
"""Сколько идентификаторов попадает в пул. Дальше запрос теряет фокус."""

MIN_IDENTIFIER_LEN = 4

_SECTION_RE = re.compile(r"(?i)^#{1,6}\s*.*(что сделать|критери|приёмк|acceptance)")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./]*")


def _is_identifier(token: str) -> bool:
    """Технический идентификатор: snake_case, CamelCase, путь или файл .py."""
    if len(token) < MIN_IDENTIFIER_LEN:
        return False
    if "/" in token or token.endswith(".py"):
        return True
    if "_" in token:
        return True
    return token[:1].isupper() and any(c.isupper() for c in token[1:])


def _items(text: str) -> list[str]:
    """Пункты списков под заголовками требований и критериев приёмки."""
    items: list[str] = []
    in_section = False
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            in_section = bool(_SECTION_RE.match(line))
            continue
        if not in_section:
            continue
        match = _ITEM_RE.match(line)
        if match:
            items.append(match.group(1))
    return items


def _identifiers(text: str, items: list[str]) -> list[str]:
    """Идентификаторы прозаической части: то, что не ушло в пункты списков."""
    consumed = set(items)
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = _ITEM_RE.match(line)
        if match and match.group(1) in consumed:
            continue
        if not stripped:
            continue
        for token in _TOKEN_RE.findall(stripped):
            if _is_identifier(token) and token not in found:
                found.append(token)
    return found


def _dedup(queries: list[str]) -> list[str]:
    """Убрать повторы, сохранив порядок: пункт может дословно повторить запрос."""
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        norm = " ".join(query.split()).casefold()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(query)
    return out


def build_subqueries(task: dict | None, base_query: str) -> list[str]:
    """Набор подзапросов задачи; первый — продакшн-запрос целиком.

    base_query приходит аргументом, а не берётся из task_context: тот модуль
    импортирует этот, и обратный импорт замкнул бы цикл.

    Вырожденный вход (нет задачи, нет списков) даёт ровно [base_query] —
    поведение ретрива при этом тождественно однозапросному.
    """
    queries = [base_query]
    if not task:
        return _dedup(queries)
    text = "\n".join([
        str(task.get("title") or ""),
        str(task.get("description") or ""),
    ])
    items = _items(text)
    items.extend(str(c) for c in (task.get("criteria") or []) if str(c).strip())
    queries.extend(items)
    identifiers = _identifiers(text, items)
    if identifiers:
        queries.append(" ".join(identifiers[:MAX_IDENTIFIERS]))
    return _dedup(queries)[:MAX_SUBQUERIES]
