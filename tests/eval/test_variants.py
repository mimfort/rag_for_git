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
