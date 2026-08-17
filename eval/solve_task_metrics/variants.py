"""Варианты конфигурации ретрива для replay (PRI-254).

Вариант — именованная стратегия сборки множества путей-кандидатов по задаче.
Реестр живёт в eval/, а не в reviewer/: продакшн-путь про эвал знать не должен.
Рычаги ID-311/312 добавят свои стратегии одной строкой этого реестра.
"""
from __future__ import annotations

from dataclasses import dataclass

from reviewer.mcp.subqueries import build_subqueries

from .context_paths import extract_context_paths

# Разделы блока context_limits, которые разрешено оверрайдить: форма совпадает
# с .review.yml, поэтому свёртка идёт существующим ContextLimits.from_review_yaml
# и второго парсера лимитов в проекте не появляется.
OVERRIDE_SECTIONS = ("search_codebase", "search_tasks", "graph", "code_section")


class UnknownVariant(ValueError):
    """Запрошен незарегистрированный вариант."""


class BadOverride(ValueError):
    """Оверрайд лимитов не разбирается."""


@dataclass(frozen=True)
class TaskInput:
    """Вход одной задачи корпуса: ключ, задача из стора и запрос ретрива."""

    key: str
    task: dict | None
    query: str


@dataclass(frozen=True)
class ReplayTarget:
    """Цель прогона: репозиторий, ветка и оверрайды лимитов варианта."""

    repo: str
    branch: str
    limits: dict | None = None


def _paths(provider, task: TaskInput, target: ReplayTarget, limits) -> set:
    text = provider.code(target.repo, target.branch, task.query, limits)
    return extract_context_paths(text)


def _baseline(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Продакшн-путь без изменений: лимиты репозитория, никаких оверрайдов."""
    return _paths(provider, task, target, None)


def _limits(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Тот же путь с оверрайдами лимитов из --set."""
    return _paths(provider, task, target, target.limits)


def _multiquery(provider, task: TaskInput, target: ReplayTarget) -> set:
    """Мультизапрос по структуре и сущностям задачи с RRF-слиянием (PRI-255).

    Набор подзапросов строится ПРОДАКШН-функцией: своей копии формулы здесь
    не заводится, иначе replay мерил бы не тот вход, что видит прод.
    """
    queries = build_subqueries(task.task, task.query)
    text = provider.code_multi(target.repo, target.branch, queries, target.limits)
    return extract_context_paths(text)


_REGISTRY = {
    "baseline": _baseline,
    "limits": _limits,
    "multiquery": _multiquery,
}

VARIANT_NAMES = tuple(_REGISTRY)


def get_variant(name: str):
    """Стратегия по имени; неизвестное имя — ошибка, а не тихий фолбэк."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownVariant(
            f"неизвестный вариант {name!r}; доступны: {', '.join(VARIANT_NAMES)}"
        ) from None


def _value(raw: str):
    """Привести значение оверрайда к int/float; иначе оставить строкой."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_overrides(pairs) -> dict | None:
    """Разобрать пары '<раздел>.<ключ>=<значение>' в блок context_limits."""
    if not pairs:
        return None
    out: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not raw:
            raise BadOverride(f"оверрайд {pair!r}: ожидается ключ=значение")
        section, dot, field = key.partition(".")
        if not dot or not field or "." in field:
            raise BadOverride(
                f"оверрайд {pair!r}: ожидается <раздел>.<ключ>, "
                f"раздел из {', '.join(OVERRIDE_SECTIONS)}"
            )
        if section not in OVERRIDE_SECTIONS:
            raise BadOverride(
                f"оверрайд {pair!r}: неизвестный раздел {section!r}; "
                f"доступны: {', '.join(OVERRIDE_SECTIONS)}"
            )
        out.setdefault(section, {})[field] = _value(raw)
    return out
