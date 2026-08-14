"""Прогноз core-recall по размеру знаменателя ядра — интервалом, не числом.

Точечная оценка на выборке в десятки задач с разбросом от 0 до 100% —
это выдача шума за достоверность. Поэтому прогноз всегда интервальный
(медиана + межквартильный размах) и честно объявляет размер выборки.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# Границы бакетов по размеру знаменателя ядра. Выбраны по baseline-распределению
# спайка PRI-246: медиана 4 файла, максимум 25.
BUCKETS = (("1–3", 1, 3), ("4–9", 4, 9), ("10+", 10, None))

# Бакет меньше этого размера не даёт интервала: сообщаем «недостаточно данных».
MIN_SAMPLE = 5


@dataclass
class BucketForecast:
    """Прогноз по одному бакету размера."""

    label: str
    n: int
    enough_data: bool
    recall_median: float | None = None
    recall_p25: float | None = None
    recall_p75: float | None = None


def bucket_label(core_size: int) -> str:
    """Метка бакета для заданного размера знаменателя ядра."""
    for label, low, high in BUCKETS:
        if core_size >= low and (high is None or core_size <= high):
            return label
    return BUCKETS[0][0]


def _quantile(values: list, q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def build(rows: list) -> list:
    """Прогноз по всем бакетам. Задачи без точки измерения не участвуют."""
    grouped: dict = {label: [] for label, _, _ in BUCKETS}
    for row in rows:
        recall = row.get("core_recall")
        core = row.get("expected_core") or 0
        if recall is None or core < 1:
            continue
        grouped[bucket_label(core)].append(recall)
    items: list = []
    for label, _, _ in BUCKETS:
        values = grouped[label]
        item = BucketForecast(
            label=label, n=len(values), enough_data=len(values) >= MIN_SAMPLE
        )
        if item.enough_data:
            item.recall_median = statistics.median(values)
            item.recall_p25 = _quantile(values, 0.25)
            item.recall_p75 = _quantile(values, 0.75)
        items.append(item)
    return items


def describe(item: BucketForecast) -> str:
    """Человекочитаемый прогноз бакета — всегда с разбросом и размером выборки."""
    if not item.enough_data:
        return (
            f"{item.label} core-файлов: недостаточно данных для прогноза "
            f"(N={item.n}, нужно ≥{MIN_SAMPLE})"
        )
    return (
        f"{item.label} core-файлов: ожидаемый core-recall "
        f"{item.recall_p25:.0%}–{item.recall_p75:.0%} "
        f"(медиана {item.recall_median:.0%}, N={item.n})"
    )
