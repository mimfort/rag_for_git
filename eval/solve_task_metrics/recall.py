"""core-recall: качество ретрива на суженном знаменателе.

Пустой знаменатель ядра — отдельное состояние «нет точки измерения» (None),
а НЕ нулевой recall: у задачи, чей diff состоит только из доков и тестов,
качество ретрива по ядру не определено, и подмешивать её нулём в медиану
значит систематически занижать метрику.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


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


@dataclass
class QualityAggregate:
    """Агрегат качества по корпусу."""

    n_measured: int
    no_measurement: int
    core_recall_median: float | None = None
    core_recall_mean: float | None = None
    raw_recall_median: float | None = None
    denominator_median: float | None = None


def evaluate_task(task_key: str, predicted: set, expected: set, expected_core: set) -> TaskQuality:
    """Посчитать метрики одной задачи; core_recall=None при пустом ядре."""
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
    raw_values = [r.raw_recall for r in rows if r.raw_recall is not None]
    if raw_values:
        agg.raw_recall_median = statistics.median(raw_values)
    return agg
