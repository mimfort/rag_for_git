"""Путевой сигнал-кандидат секции code: фактические diff-пути похожих задач
(PRI-257).

Co-change как второй источник снят по итогам приёмки (см.
.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md,
«Вердикт по критерию приёмки 1»): 12 % точность, просадка bulk, ноль вклада
поверх similar-diffs. `gitutil.paths_touched_by_grep` остаётся — он обслуживает
git-фолбэк similar-diffs (сообщения коммитов), а не со-изменяемость.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer import gitutil


@dataclass(frozen=True)
class AugmentResult:
    """Пути-кандидаты, их происхождение и пробелы сбора."""

    paths: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)


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
    if not ordered:
        if clone_path:
            for key in lookup:
                try:
                    for path in gitutil.paths_touched_by_grep(
                            clone_path, key, limit=GIT_GREP_COMMITS):
                        ordered.setdefault(path, None)
                except Exception as exc:  # noqa: BLE001 — сбой по одному ключу
                    # не должен лишать шанса остальные алиасы/ключи
                    gaps.append(f"git-история недоступна: {type(exc).__name__}")
                    continue
        else:
            gaps.append("клон недоступен, git-фолбэк пропущен")
    paths = list(ordered)[:limit]
    return AugmentResult(paths=paths,
                         by_source={"similar_diffs": len(paths)} if paths else {},
                         gaps=gaps)
