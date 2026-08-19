"""Граница живых зависимостей харнесса (PRI-254).

Живые зависимости reviewer тянет только `live.py`: остальные модули обязаны
оставаться импортируемыми и тестируемыми без Postgres, Neo4j и Voyage. Импорт
самого пакета `reviewer` разрешён и перечисленным в `ALLOWED` модулям — но лишь
для чистых функций без ввода-вывода (см. комментарий у списка).
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
# variants.py и subquery_stats.py (PRI-255) импортируют reviewer.mcp.subqueries —
# тоже чистую функцию без сети/БД: формула подзапросов производна от текста
# задачи, не от живой инфраструктуры. context_core.py (PRI-261) — тот же класс,
# что classify.py и recall.py: чистый ре-экспорт вывода контекстного ядра, без
# Postgres/Neo4j/Voyage — обход графа приходит инъекцией, а не живым клиентом.
# context_seeds.py (PRI-261) — того же класса: оба его импорта reviewer чистые
# функции без Postgres/Neo4j/Voyage. chunk_python — tree-sitter-разбор в памяти
# (на тексте, прочитанном инъектированным run_git, а не живым клиентом),
# is_core_production_path — та же чистая классификация путей, что у classify.py.
ALLOWED = {
    "briefs.py", "classify.py", "recall.py", "live.py", "variants.py",
    "subquery_stats.py", "context_core.py", "context_seeds.py",
}


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


def test_limits_to_yaml_serializes_code_section():
    """Раздел code_section переживает сериализацию (PRI-256).

    Без него оверрайд одной секции обнулил бы файловый бюджет до классовых
    дефолтов — то есть сторона «до» замера молча превратилась бы в «после».
    """
    from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits

    from eval.solve_task_metrics.live import limits_to_yaml

    original = ContextLimits(
        code_section=CodeSectionLimits(max_files=4, chars_per_file=2000)
    )
    block = limits_to_yaml(original)
    assert block["code_section"]["max_files"] == 4
    assert block["code_section"]["chars_per_file"] == 2000
    assert ContextLimits.from_review_yaml({"context_limits": block}) == original


def test_code_multi_with_overrides_passes_section_limits(monkeypatch):
    """Оверрайд code_section доезжает до search_multi, а не теряется молча."""
    from reviewer.policy.context_limits import ContextLimits
    from reviewer.retrieval import multiquery

    from eval.solve_task_metrics.live import LiveRetrieval

    class FakeService:
        def _resolve_context_limits(self, repo, branch):
            return ContextLimits()

    class FakeComponents:
        retriever = object()

    captured: dict = {}

    def fake_search_multi(retriever, repo, queries, **kwargs):
        captured.update(kwargs)

        class Pack:
            def as_context(self, line_numbers=True):
                return "текст"

        return Pack()

    monkeypatch.setattr(multiquery, "search_multi", fake_search_multi)
    provider = LiveRetrieval(object(), FakeComponents(), FakeService())
    provider.code_multi("o/n", "dev", ["q"], {"code_section": {"max_files": 4}})

    assert captured["section_limits"].max_files == 4


def test_code_multi_without_flags_passes_no_augment_signals():
    """Без similar_paths (PRI-257/258) _search_codebase_multi зовётся ровно как
    раньше — augment_sources остаётся None."""
    from eval.solve_task_metrics.live import LiveRetrieval

    class FakeService:
        def _search_codebase_multi(self, repo, queries, branch, include_tests,
                                   *, augment_sources=None):
            self.calls = getattr(self, "calls", [])
            self.calls.append((repo, queries, branch, include_tests, augment_sources))
            return "текст"

        def _resolve_context_limits(self, repo, branch):
            from reviewer.policy.context_limits import ContextLimits
            return ContextLimits()

    service = FakeService()
    provider = LiveRetrieval(object(), object(), service)
    out = provider.code_multi("o/n", "dev", ["q"], None)

    assert out == "текст"
    call = service.calls[0]
    assert call == ("o/n", ["q"], "dev", False, None)


def test_code_multi_similar_paths_flag_uses_production_deps(monkeypatch):
    """similar_paths=True собирает augment_sources продакшн-объектом _TaskContextDeps."""
    from eval.solve_task_metrics.live import LiveRetrieval
    from reviewer.mcp import service as service_mod

    class FakeDeps:
        def __init__(self, service, path):
            self.similar_calls: list = []

        def similar(self, query, project):
            self.similar_calls.append((query, project))

        def _augment_paths(self, repo):
            return ["a.py"]

    monkeypatch.setattr(service_mod, "_TaskContextDeps", FakeDeps)

    class FakeService:
        def _search_codebase_multi(self, repo, queries, branch, include_tests,
                                   *, augment_sources=None):
            self.captured = augment_sources
            return "текст"

        def _resolve_context_limits(self, repo, branch):
            from reviewer.policy.context_limits import ContextLimits
            return ContextLimits()

    service = FakeService()
    provider = LiveRetrieval(object(), object(), service)
    provider.code_multi("o/n", "dev", ["q1", "q2"], None, similar_paths=True)

    assert len(service.captured) == 1
    assert service.captured[0].name == "similar-diffs"
    assert service.captured[0].paths == ["a.py"]


def test_code_multi_with_overrides_forwards_augment_signals(monkeypatch):
    """Оверрайды лимитов не глушат augment_sources — он доезжает до search_multi."""
    from reviewer.policy.context_limits import ContextLimits
    from reviewer.retrieval import multiquery

    from eval.solve_task_metrics.live import LiveRetrieval
    from reviewer.mcp import service as service_mod

    class FakeDeps:
        def __init__(self, service, path):
            pass

        def similar(self, query, project):
            pass

        def _augment_paths(self, repo):
            return ["a.py"]

    monkeypatch.setattr(service_mod, "_TaskContextDeps", FakeDeps)

    class FakeService:
        def _resolve_context_limits(self, repo, branch):
            return ContextLimits()

    class FakeComponents:
        retriever = object()

    captured: dict = {}

    def fake_search_multi(retriever, repo, queries, **kwargs):
        captured.update(kwargs)

        class Pack:
            def as_context(self, line_numbers=True):
                return "текст"

        return Pack()

    monkeypatch.setattr(multiquery, "search_multi", fake_search_multi)
    provider = LiveRetrieval(object(), FakeComponents(), FakeService())
    provider.code_multi("o/n", "dev", ["q"], {"code_section": {"max_files": 4}},
                        similar_paths=True)

    sources = captured["augment_sources"]
    assert len(sources) == 1
    assert sources[0].name == "similar-diffs"
    assert sources[0].paths == ["a.py"]
    assert "cochange" not in captured


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
