"""Метрики полноты графа кода (PRI-252).

Чистые функции без БД и Neo4j: считают рёбра по типам, форматируют разбивку
для вывода `reviewer index` и сравнивают текущий замер с предыдущим замером
той же ветки, чтобы просадка полноты не проходила молча.

Порог — константа модуля, а не env-ключ: остаточное расхождение числа рёбер
между окружениями запуска scip-python ~0.5 %, порог лишь отделяет его от
настоящей потери сигнала.
"""
from __future__ import annotations

import collections
from collections.abc import Iterable

EDGE_REGRESSION_THRESHOLD = 0.10


def count_edges_by_rel(edges: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    """Счётчики рёбер по типу отношения: {"CALLS": N, "IMPLEMENTS": M}."""
    counter: collections.Counter[str] = collections.Counter()
    for _src, rel, _dst in edges:
        counter[rel] += 1
    return dict(counter)


def format_edge_counts(counts: dict[str, int]) -> str:
    """Человекочитаемая разбивка, по убыванию количества: "CALLS 17963, IMPLEMENTS 129"."""
    if not counts:
        return "нет"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{rel} {n}" for rel, n in items)


def detect_edge_regression(
    prev: dict[str, int] | None,
    curr: dict[str, int],
    threshold: float = EDGE_REGRESSION_THRESHOLD,
) -> list[str]:
    """Сообщения о просадке по типам рёбер относительно предыдущего замера.

    Просадкой считается падение более чем на ``threshold`` долю от предыдущего
    значения, включая полное исчезновение типа. Рост и новые типы молчат.
    Отсутствие предыдущего замера (``None``) — не просадка: сравнивать не с чем.
    """
    if not prev:
        return []
    messages: list[str] = []
    for rel, was in sorted(prev.items()):
        if was <= 0:
            continue
        now = curr.get(rel, 0)
        if now < was * (1 - threshold):
            pct = round((was - now) * 100 / was)
            messages.append(f"{rel} {was} → {now} (−{pct}%)")
    return messages
