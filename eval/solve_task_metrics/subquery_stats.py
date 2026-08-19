"""Распределение числа подзапросов по размеру задачи (PRI-255, критерий 1).

Критерий приёмки требует показать, что число подзапросов производно от
размера задачи, а не константа. Расчёт детерминированный и не трогает
ретрив: нужен только текст задачи, поэтому подкоманда дешёвая и не тратит
квоту Voyage.

Формула набора подзапросов — продакшн-функция build_subqueries; своей копии
здесь нет.
"""
from __future__ import annotations

import statistics

from reviewer.mcp.subqueries import build_subqueries

SMALL_MAX_LINES = 10
MEDIUM_MAX_LINES = 30

BUCKETS = (
    f"мелкая (≤{SMALL_MAX_LINES} строк)",
    f"средняя ({SMALL_MAX_LINES + 1}-{MEDIUM_MAX_LINES})",
    f"развёртка (>{MEDIUM_MAX_LINES})",
)


def size_bucket(task: dict | None) -> str:
    """Класс размера задачи по числу строк описания."""
    lines = len(str((task or {}).get("description") or "").splitlines())
    if lines <= SMALL_MAX_LINES:
        return BUCKETS[0]
    if lines <= MEDIUM_MAX_LINES:
        return BUCKETS[1]
    return BUCKETS[2]


def distribution(rows) -> list[dict]:
    """Сводка по классам размера: число задач, медиана/мин/макс подзапросов.

    rows — последовательность (key, task, base_query).
    """
    by_bucket: dict[str, list[int]] = {}
    for _key, task, base_query in rows:
        count = len(build_subqueries(task, base_query))
        by_bucket.setdefault(size_bucket(task), []).append(count)
    return [
        {
            "bucket": bucket,
            "tasks": len(counts),
            "median": statistics.median(counts),
            "min": min(counts),
            "max": max(counts),
        }
        for bucket in BUCKETS
        if (counts := by_bucket.get(bucket))
    ]


def render(rows) -> str:
    """Markdown-таблица распределения — то, что уходит в отчёт приёмки."""
    lines = [
        "| класс задачи | задач | медиана подзапросов | мин | макс |",
        "|---|---|---|---|---|",
    ]
    for row in distribution(rows):
        lines.append(
            f"| {row['bucket']} | {row['tasks']} | {row['median']} "
            f"| {row['min']} | {row['max']} |"
        )
    return "\n".join(lines)
