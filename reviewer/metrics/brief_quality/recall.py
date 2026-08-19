"""core-recall: качество ретрива на суженном знаменателе.

Пустой знаменатель ядра — отдельное состояние «нет точки измерения» (None),
а НЕ нулевой recall: у задачи, чей diff состоит только из доков и тестов,
качество ретрива по ядру не определено, и подмешивать её нулём в медиану
значит систематически занижать метрику.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

BULK_CORE_THRESHOLD = 10
"""Порог знаменателя ядра, с которого задача считается задачей-развёрткой.

Значение из анализа PRI-246: на нём разделялись выборки (при expected_core >= 10
медиана predicted 5.0, при expected_core < 10 — 6.0), и в него попадают все
четыре задачи, давшие провал core-recall.
"""


@dataclass
class TaskQuality:
    """Качество ретрива по одной задаче."""

    task_key: str
    expected: int
    expected_core: int
    predicted: int
    hit_core: int
    core_recall: float | None = None
    raw_recall: float | None = None
    precision: float | None = None
    context_core: int = 0
    hit_context: int = 0
    context_recall: float | None = None
    union_precision: float | None = None


@dataclass
class QualityAggregate:
    """Агрегат качества по корпусу."""

    n_measured: int
    no_measurement: int
    core_recall_median: float | None = None
    core_recall_mean: float | None = None
    raw_recall_median: float | None = None
    denominator_median: float | None = None
    bulk_core_recall_median: float | None = None
    bulk_n_measured: int = 0
    context_recall_median: float | None = None
    context_n_measured: int = 0
    no_context_measurement: int = 0
    union_precision_median: float | None = None


def evaluate_task(task_key: str, predicted: set, expected: set,
                  expected_core: set, context_core: set | None = None) -> TaskQuality:
    """Посчитать метрики одной задачи; core_recall=None при пустом ядре.

    context_core необязателен: без него строка тождественна доPRI-261, и именно
    это оставляет числа приёмок PRI-255…259 сравнимыми без пересчёта (критерий 3).
    """
    hit_core = predicted & expected_core
    hit_raw = predicted & expected
    row = TaskQuality(
        task_key=task_key,
        expected=len(expected),
        expected_core=len(expected_core),
        predicted=len(predicted),
        hit_core=len(hit_core),
    )
    row.core_recall = len(hit_core) / len(expected_core) if expected_core else None
    row.raw_recall = len(hit_raw) / len(expected) if expected else None
    row.precision = len(hit_raw) / len(predicted) if predicted else None
    if context_core is not None:
        hit_context = predicted & context_core
        row.context_core = len(context_core)
        row.hit_context = len(hit_context)
        row.context_recall = (
            len(hit_context) / len(context_core) if context_core else None
        )
        # Объединение по expected, а не по expected_core: новая precision обязана
        # быть надмножеством старой, иначе рычаг читается наоборот.
        union = set(expected) | set(context_core)
        row.union_precision = (
            len(predicted & union) / len(predicted) if predicted else None
        )
    return row


def aggregate(rows: list) -> QualityAggregate:
    """Свести задачи в агрегат; задачи без точки измерения считаются отдельно."""
    measured = [r for r in rows if r.core_recall is not None]
    agg = QualityAggregate(
        n_measured=len(measured),
        no_measurement=len(rows) - len(measured),
    )
    if measured:
        values = [r.core_recall for r in measured]
        agg.core_recall_median = statistics.median(values)
        agg.core_recall_mean = sum(values) / len(values)
        agg.denominator_median = statistics.median(
            [r.expected_core for r in measured]
        )
    bulk = [r for r in measured if r.expected_core >= BULK_CORE_THRESHOLD]
    agg.bulk_n_measured = len(bulk)
    if bulk:
        agg.bulk_core_recall_median = statistics.median([r.core_recall for r in bulk])
    raw_values = [r.raw_recall for r in rows if r.raw_recall is not None]
    if raw_values:
        agg.raw_recall_median = statistics.median(raw_values)
    context = [r for r in rows if r.context_recall is not None]
    agg.context_n_measured = len(context)
    agg.no_context_measurement = len(rows) - len(context)
    if context:
        agg.context_recall_median = statistics.median(
            [r.context_recall for r in context]
        )
    union_values = [r.union_precision for r in rows if r.union_precision is not None]
    if union_values:
        agg.union_precision_median = statistics.median(union_values)
    return agg
