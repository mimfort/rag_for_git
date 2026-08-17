"""Путевые сигналы-кандидаты секции code: похожие задачи и co-change (PRI-257).

Модуль намеренно без I/O: и множества файлов коммитов, и строки истории
прогонов приходят параметрами. Подсчёт со-появления — чистая функция, поэтому
тестируется без git и без временного репозитория.
"""
from __future__ import annotations

from dataclasses import dataclass, field

COCHANGE_COMMITS = 300
"""Глубина истории для co-change. Модульная константа, а не ключ .review.yml:
третий регулятор рядом с max_files/max_augmented_files рассинхронизировался бы
с ними, а крутить его оператору незачем."""

MIN_COCHANGE = 2
"""Порог со-появления: один общий коммит — совпадение, два — уже сигнал."""


@dataclass(frozen=True)
class AugmentResult:
    """Пути-кандидаты, их происхождение и пробелы сбора."""

    paths: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)


def rank_cochanged(commit_files: list[set[str]], seeds: set[str], *,
                   min_count: int = MIN_COCHANGE, limit: int) -> list[str]:
    """Файлы, чаще прочих менявшиеся в одних коммитах с seeds.

    Порядок — по убыванию числа со-появлений, тай-брейк по пути, поэтому
    результат детерминирован и не зависит от порядка коммитов на входе.
    """
    if not seeds or not commit_files or limit <= 0:
        return []
    counts: dict[str, int] = {}
    for files in commit_files:
        if not files & seeds:
            continue
        for path in files - seeds:
            counts[path] = counts.get(path, 0) + 1
    ranked = sorted(
        (path for path, count in counts.items() if count >= min_count),
        key=lambda path: (-counts[path], path))
    return ranked[:limit]
