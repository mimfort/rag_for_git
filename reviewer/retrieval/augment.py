"""Путевые сигналы-кандидаты секции code: похожие задачи и co-change (PRI-257).

Модуль намеренно без I/O: и множества файлов коммитов, и строки истории
прогонов приходят параметрами. Подсчёт со-появления — чистая функция, поэтому
тестируется без git и без временного репозитория.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer import gitutil

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


GIT_GREP_COMMITS = 200
"""Сколько последних коммитов просматривает фолбэк по ключу задачи."""


def _human_keys(key: str, aliases_by_key: dict[str, list[str]]) -> list[str]:
    """Ключ и его алиасы: стор ключует ID-N, доска и git знают PRI-N."""
    return [key, *(aliases_by_key.get(key) or [])]


def collect_similar_task_paths(*, keys, aliases_by_key, history, clone_path,
                               limit: int) -> AugmentResult:
    """Фактические diff-пути похожих задач: история прогонов, фолбэк — git.

    Табличный источник точнее (пути уже классифицированы как core), но
    появляется только у задачи с опубликованным ревью и брифом. Фолбэк по
    сообщениям коммитов даёт покрытие на репозитории без истории прогонов.
    """
    if not keys or limit <= 0:
        return AugmentResult()
    lookup: list[str] = []
    for key in keys:
        lookup.extend(_human_keys(key, aliases_by_key))
    gaps: list[str] = []
    ordered: dict[str, None] = {}
    if history is not None:
        try:
            by_key = history.diff_paths_for_tasks(lookup)
            for key in lookup:
                for path in by_key.get(key) or []:
                    ordered.setdefault(path, None)
        except Exception as exc:  # noqa: BLE001 — источник недоступен, это штатный случай
            gaps.append(f"история прогонов недоступна: {type(exc).__name__}")
    if not ordered and clone_path:
        for key in lookup:
            try:
                for path in gitutil.paths_touched_by_grep(
                        clone_path, key, limit=GIT_GREP_COMMITS):
                    ordered.setdefault(path, None)
            except Exception as exc:  # noqa: BLE001
                gaps.append(f"git-история недоступна: {type(exc).__name__}")
                break
    paths = list(ordered)[:limit]
    return AugmentResult(paths=paths,
                         by_source={"similar_diffs": len(paths)} if paths else {},
                         gaps=gaps)
