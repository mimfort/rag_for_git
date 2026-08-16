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
def test_open_live_wires_and_closes_components():
    """Проводка живого провайдера: компоненты собираются, preflight отвечает, close не падает.

    Сквозной прогон с непустой выдачей здесь невозможен принципиально:
    integration-тесты принудительно направлены на ИЗОЛИРОВАННУЮ тестовую
    инфраструктуру, где base-индекса нет по построению. Поэтому проверяется
    именно проводка, а полнота выдачи — ручной приёмкой `replay` на живом
    деплое (см. финальную приёмку плана PRI-254).
    """
    from eval.solve_task_metrics.live import open_live

    provider, repo, branch = open_live("owner/replay-smoke", "dev")
    try:
        # Тестовая БД пустая: схему заводит сам тест (идемпотентно).
        provider._components.store.init_schema()
        assert repo == "owner/replay-smoke" and branch == "dev"
        preflight = provider.preflight(repo, branch)
        assert set(preflight) >= {"branch", "indexed_sha", "chunks", "graph_nodes"}
        assert preflight["branch"] == "dev"
        # Индекса у выдуманного репо нет — это ноль чанков, а не исключение.
        assert preflight["chunks"] == 0
    finally:
        provider.close()
