"""Путевой сигнал-кандидат секции code: фактические diff-пути похожих задач
(PRI-257).

Co-change как второй источник снят по итогам приёмки (см.
.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md,
«Вердикт по критерию приёмки 1»): 12 % точность, просадка bulk, ноль вклада
поверх similar-diffs. `gitutil.paths_touched_by_grep` остаётся — он обслуживает
git-фолбэк similar-diffs (сообщения коммитов), а не со-изменяемость.

Состав кластеров сводок подсистем как источник опробован и снят тем же порядком
(PRI-258, см. eval/replay_report.md, «Приёмка PRI-258»): 2 попадания в ядро на 92
подмешанных пути, вытеснено 9 core-хитов гибрида, медиана core-recall
0.75 → 0.6667. Механика бюджета проверялась отдельно и оказалась ни при чём —
кластер описывает подсистему, а не ядро конкретной правки. От PRI-258 остались
список именованных источников (ниже) и приоритет кандидатов по рангу сырого
пула; в проде источник по-прежнему один — similar-diffs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer import gitutil
from reviewer.config.committed import validate_clone
from reviewer.services.repo_id import normalize_repo


@dataclass(frozen=True)
class AugmentSource:
    """Именованный источник подмешанных путей со своей файловой квотой.

    Имя не косметика: нота видимости секции перечисляет источники поимённо —
    иначе вклад источников в выдачу неотличим на глаз (и в отчёте replay).
    """

    name: str
    paths: list[str] = field(default_factory=list)
    quota: int = 0


@dataclass(frozen=True)
class AugmentResult:
    """Пути-кандидаты, их происхождение и пробелы сбора."""

    paths: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)


GIT_GREP_COMMITS = 200
"""Сколько совпавших коммитов берёт фолбэк по одному ключу задачи."""


def collect_similar_task_paths(*, repo: str, keys: list[str],
                               aliases_by_key: dict[str, list[str]], history,
                               clone_path: str | None, indexed_sha: str | None,
                               limit: int) -> AugmentResult:
    """Фактические diff-пути похожих задач: история прогонов, фолбэк — git.

    Табличный источник точнее (пути уже классифицированы как core), но
    появляется только у задачи с опубликованным ревью и брифом. Фолбэк по
    сообщениям коммитов читает историю до SHA индекса.
    """
    if not keys or limit <= 0:
        return AugmentResult()
    repo = normalize_repo(repo)
    lookup = list(dict.fromkeys(
        alias for key in keys for alias in (key, *(aliases_by_key.get(key) or []))
    ))
    gaps: list[str] = []
    ordered: dict[str, None] = {}
    if history is not None:
        try:
            by_key = history.diff_paths_for_tasks(lookup, repo=repo)
            for key in lookup:
                for path in by_key.get(key) or []:
                    ordered.setdefault(path, None)
        except Exception as exc:  # noqa: BLE001 — источник недоступен, это штатный случай
            gaps.append(f"история прогонов недоступна: {type(exc).__name__}")
    if not ordered:
        clone = validate_clone(clone_path, repo) if indexed_sha else clone_path
        if not clone:
            gaps.append("клон недоступен, git-фолбэк пропущен")
        elif not indexed_sha:
            gaps.append("git-снимок индекса недоступен, git-фолбэк пропущен")
        else:
            for key in lookup:
                try:
                    for path in gitutil.paths_touched_by_grep(
                            clone, key, limit=GIT_GREP_COMMITS, indexed_sha=indexed_sha):
                        ordered.setdefault(path, None)
                except Exception as exc:  # noqa: BLE001 — сбой по одному ключу
                    # не должен лишать шанса остальные алиасы/ключи
                    reason = f"git-история снимка недоступна: {type(exc).__name__}"
                    if reason not in gaps:
                        gaps.append(reason)
    paths = list(ordered)[:limit]
    return AugmentResult(paths=paths,
                         by_source={"similar_diffs": len(paths)} if paths else {},
                         gaps=gaps)
