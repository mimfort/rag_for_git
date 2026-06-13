"""Извлечение ключей задачи из текстов PR (title/body/head-ветка) по regex.

Чистый модуль без сетевых вызовов: на вход — паттерн и тексты, на выход —
primary-ключ и прочие найденные. Прецеденция источников: title → body → branch.
Используется в ReviewService.prepare; чтение самой задачи с доски — на стороне скилла.
"""
from __future__ import annotations

import logging
import re
from typing import TypedDict

log = logging.getLogger(__name__)

# Дефолт подходит и Yougile (SAI-515), и Jira (PROJ-123): префикс заглавными + номер.
DEFAULT_KEY_PATTERN = r"[A-Z]+-\d+"


class TaskKeys(TypedDict):
    primary: str | None
    others: list[str]


def extract_task_keys(
    pattern: str | None,
    title: str | None,
    body: str | None,
    branch: str | None,
) -> TaskKeys:
    """Извлечь ключи задачи.

    Прецеденция источников: ``title`` → ``body`` → ``branch``. ``primary`` —
    первый матч в этом порядке; ``others`` — прочие уникальные матчи (дедуп,
    порядок появления), без ``primary``.

    Невалидный ``pattern`` (или нет матчей) → ``{"primary": None, "others": []}``
    (fail-soft + warning на невалидном паттерне). ``pattern=None`` → дефолт.

    Returns:
        ``{"primary": str | None, "others": list[str]}``
    """
    try:
        rx = re.compile(pattern or DEFAULT_KEY_PATTERN)
    except re.error:
        log.warning("Невалидный key_pattern %r — ключи задачи не извлекаются", pattern)
        return {"primary": None, "others": []}

    found: list[str] = []
    for text in (title, body, branch):
        if text:
            found.extend(m.group(0) for m in rx.finditer(text))

    if not found:
        return {"primary": None, "others": []}

    primary = found[0]
    others: list[str] = []
    for key in found[1:]:
        if key != primary and key not in others:
            others.append(key)
    return {"primary": primary, "others": others}
