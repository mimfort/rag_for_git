"""Реестр вариантов ретрива и парсер оверрайдов лимитов (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import variants


class FakeProvider:
    """Провайдер ретрива: запоминает, с какими лимитами его звали."""

    def __init__(self, text: str = ""):
        self.text = text
        self.calls: list = []

    def code(self, repo: str, branch: str, query: str, limits):
        self.calls.append((repo, branch, query, limits))
        return self.text


HEADER = "// reviewer/a.py#f (reviewer/a.py:1-3)\n    1 | x = 1\n"


def _inputs():
    return (
        variants.TaskInput(key="PRI-1", task={"title": "t"}, query="q"),
        variants.ReplayTarget(repo="o/n", branch="dev", limits=None),
    )


def test_baseline_returns_paths_and_passes_no_limits():
    provider = FakeProvider(HEADER)
    task, target = _inputs()
    assert variants.get_variant("baseline")(provider, task, target) == {"reviewer/a.py"}
    assert provider.calls == [("o/n", "dev", "q", None)]


def test_limits_variant_forwards_overrides():
    provider = FakeProvider(HEADER)
    task, _ = _inputs()
    target = variants.ReplayTarget(
        repo="o/n", branch="dev", limits={"search_codebase": {"ceiling": 25}}
    )
    assert variants.get_variant("limits")(provider, task, target) == {"reviewer/a.py"}
    assert provider.calls[0][3] == {"search_codebase": {"ceiling": 25}}


def test_unknown_variant_lists_available_names():
    with pytest.raises(variants.UnknownVariant) as error:
        variants.get_variant("нет-такого")
    for name in variants.VARIANT_NAMES:
        assert name in str(error.value)


def test_parse_overrides_builds_nested_dict_with_typed_values():
    assert variants.parse_overrides(
        ["search_codebase.ceiling=25", "search_codebase.ratio=0.4", "graph.hops=2"]
    ) == {
        "search_codebase": {"ceiling": 25, "ratio": 0.4},
        "graph": {"hops": 2},
    }


def test_parse_overrides_empty_is_none():
    assert variants.parse_overrides([]) is None
    assert variants.parse_overrides(None) is None


@pytest.mark.parametrize("bad", ["ceiling=25", "search_codebase.ceiling", "a.b.c=1", "=5"])
def test_parse_overrides_rejects_malformed(bad):
    with pytest.raises(variants.BadOverride):
        variants.parse_overrides([bad])


def test_multiquery_variant_passes_subquery_list():
    """Вариант зовёт code_multi продакшн-набором подзапросов, а не одной строкой."""
    class MultiProvider(FakeProvider):
        def code_multi(self, repo, branch, queries, limits):
            self.calls.append((repo, branch, list(queries), limits))
            return HEADER

    provider = MultiProvider(HEADER)
    task = variants.TaskInput(
        key="PRI-1",
        task={"title": "T", "description": "## Что сделать\n\n1. первый\n2. хвостовой\n"},
        query="q")
    target = variants.ReplayTarget(repo="o/n", branch="dev", limits=None)

    assert variants.get_variant("multiquery")(provider, task, target) == {"reviewer/a.py"}
    _repo, _branch, queries, limits = provider.calls[0]
    assert queries[0] == "q", "продакшн-запрос идёт первым"
    assert any("хвостовой" in q for q in queries)
    assert limits is None


def test_multiquery_is_registered():
    assert "multiquery" in variants.VARIANT_NAMES


def test_augment_variants_registered():
    from eval.solve_task_metrics.variants import VARIANT_NAMES, get_variant
    assert "similar_paths" in set(VARIANT_NAMES)
    assert callable(get_variant("similar_paths"))


def test_cochange_and_augmented_variants_are_not_registered():
    """Co-change снят по итогам приёмки (step8-measurement.md) — реестр их не знает."""
    from eval.solve_task_metrics.variants import VARIANT_NAMES
    assert "cochange" not in VARIANT_NAMES
    assert "augmented" not in VARIANT_NAMES


class AugmentProvider(FakeProvider):
    """Провайдер ретрива для варианта similar_paths (PRI-257): запоминает флаг."""

    def code_multi(self, repo, branch, queries, limits, *, similar_paths=False):
        self.calls.append((repo, branch, list(queries), limits, similar_paths))
        return self.text


def test_similar_paths_variant_enables_similar_flag():
    provider = AugmentProvider(HEADER)
    task, target = _inputs()
    assert variants.get_variant("similar_paths")(provider, task, target) == \
        {"reviewer/a.py"}
    call = provider.calls[0]
    assert call[4] is True


def test_parse_overrides_accepts_code_section():
    """Файловый бюджет секции code выразим оверрайдом (PRI-256).

    Без раздела code_section в OVERRIDE_SECTIONS сторону «до» замера PRI-256
    нечем выразить: прежнюю стену «4 файла» пришлось бы сравнивать с числом из
    другого прогона, то есть другой линейкой.
    """
    assert "code_section" in variants.OVERRIDE_SECTIONS
    assert variants.parse_overrides(
        [
            "code_section.max_files=4",
            "code_section.max_chunks_per_file=1",
            "code_section.chars_per_file=2000",
        ]
    ) == {
        "code_section": {
            "max_files": 4,
            "max_chunks_per_file": 1,
            "chars_per_file": 2000,
        }
    }
