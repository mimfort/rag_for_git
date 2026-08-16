"""Граница живых зависимостей харнесса (PRI-254).

reviewer импортирует ТОЛЬКО live.py: остальные модули обязаны оставаться
импортируемыми и тестируемыми без Postgres, Neo4j и Voyage.
"""
from __future__ import annotations

import pathlib
import re

import pytest

MODULE_DIR = pathlib.Path(__file__).resolve().parents[2] / "eval" / "solve_task_metrics"

# Импорт reviewer по форме оператора, а не по подстроке: слово 'reviewer'
# встречается в прозе докстрингов и в путях-примерах. Отступ учитывается:
# ленивый импорт внутри функции — такая же живая зависимость.
IMPORT_RE = re.compile(
    r"^\s*(?:from\s+reviewer[\s.]|import\s+reviewer[\s.,]|import\s+reviewer$)", re.M
)

# Ре-экспорты расчётного ядра (PRI-249) — не живые зависимости: чистые функции
# без ввода-вывода. Плюс live.py, который и есть объявленное исключение.
ALLOWED = {"briefs.py", "classify.py", "recall.py", "live.py"}


def test_only_live_module_imports_reviewer():
    offenders = [
        path.name
        for path in MODULE_DIR.glob("*.py")
        if path.name not in ALLOWED
        and IMPORT_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"живой импорт reviewer вне live.py: {offenders}; "
        "snapshot|stats|compare|forecast обязаны работать без инфраструктуры"
    )


def test_limits_to_yaml_roundtrips_through_production_parser():
    """Сериализация лимитов совместима с ContextLimits.from_review_yaml."""
    from reviewer.policy.context_limits import ContextLimits

    from eval.solve_task_metrics.live import limits_to_yaml

    original = ContextLimits()
    block = limits_to_yaml(original)
    assert ContextLimits.from_review_yaml({"context_limits": block}) == original


def test_limits_to_yaml_preserves_non_default_values():
    from reviewer.policy.context_limits import CodebaseLimits, ContextLimits

    from eval.solve_task_metrics.live import limits_to_yaml

    original = ContextLimits(search_codebase=CodebaseLimits(ceiling=25, ratio=0.4))
    block = limits_to_yaml(original)
    assert block["search_codebase"]["ceiling"] == 25
    assert block["search_codebase"]["ratio"] == 0.4
    assert ContextLimits.from_review_yaml({"context_limits": block}) == original


def test_merge_overrides_only_the_named_keys():
    """Оверрайд одного ключа не обнуляет остальные лимиты репозитория."""
    from reviewer.policy.context_limits import CodebaseLimits, ContextLimits

    from eval.solve_task_metrics.live import _merge, limits_to_yaml

    repo_limits = ContextLimits(search_codebase=CodebaseLimits(ratio=0.4, floor=7))
    merged = _merge(limits_to_yaml(repo_limits), {"search_codebase": {"ceiling": 25}})
    effective = ContextLimits.from_review_yaml({"context_limits": merged})
    assert effective.search_codebase.ceiling == 25
    assert effective.search_codebase.ratio == 0.4   # лимит репо сохранён
    assert effective.search_codebase.floor == 7     # лимит репо сохранён


@pytest.mark.integration
def test_live_replay_smoke():
    """Живой прогон трёх задач: компоненты собираются, ретрив отдаёт пути."""
    import datetime as dt

    from eval.solve_task_metrics import ground_truth, replay, variants
    from eval.solve_task_metrics.__main__ import BRIEFS_DIR, REPO_ROOT
    from eval.solve_task_metrics.live import open_live

    provider, repo, branch = open_live(None, None)
    try:
        snap = replay.run_replay(
            provider=provider,
            run_git=ground_truth.git_runner(REPO_ROOT),
            briefs_dir=BRIEFS_DIR,
            target=variants.ReplayTarget(repo=repo, branch=branch, limits=None),
            variant_name="baseline",
            commit="test",
            taken_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            limit=3,
        )
    finally:
        provider.close()

    assert snap["partial"] is True
    assert snap["corpus"] == 3
    assert snap["indexed_sha"]
    assert any(row["predicted_paths"] for row in snap["tasks"])
