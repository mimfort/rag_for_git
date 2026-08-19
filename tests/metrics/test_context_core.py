"""Чистый вывод контекстного ядра: без графа, обход инъектируется."""
from __future__ import annotations

from reviewer.metrics.brief_quality.context_core import (
    derive_context_core,
    node_paths,
)


def test_node_paths_splits_node_ids():
    assert node_paths(["a/b.py#F.m", "c.py#g"]) == {"a/b.py", "c.py"}


def test_node_paths_ignores_ids_without_separator():
    """node_id без '#' — не символ; догадываться о пути по нему нельзя."""
    assert node_paths(["reviewer/x.py", "reviewer/y.py#f"]) == {"reviewer/y.py"}


def test_empty_seeds_do_not_call_traversal():
    """Пустые сиды дают пустое ядро и НЕ ходят в граф: пустой запрос в Neo4j
    стоит round-trip и на исторических задачах случается регулярно."""
    calls = []

    def traverse(ids):
        calls.append(ids)
        return set()

    assert derive_context_core([], {"reviewer/a.py"}, traverse) == set()
    assert calls == []


def test_derives_core_paths_of_neighbours():
    def traverse(ids):
        assert ids == ["reviewer/a.py#f"]
        return {"reviewer/b.py#g", "reviewer/c.py#h"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse
    )
    assert result == {"reviewer/b.py", "reviewer/c.py"}


def test_subtracts_changed_core():
    """Файл, который задача И читала, И меняла, принадлежит старому знаменателю:
    в контекстном ядре он был бы посчитан дважды."""
    def traverse(ids):
        return {"reviewer/a.py#f", "reviewer/b.py#g"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse
    )
    assert result == {"reviewer/b.py"}


def test_filters_non_core_paths():
    """Тесты, доки и eval/ вне ядра — та же линейка, что у core-recall."""
    def traverse(ids):
        return {"tests/test_a.py#t", "docs/x.md#d", "eval/y.py#e",
                "reviewer/b.py#g"}

    result = derive_context_core(["reviewer/a.py#f"], set(), traverse)
    assert result == {"reviewer/b.py"}


def test_seeds_passed_sorted_for_determinism():
    """Порядок сидов детерминирован: снимок обязан воспроизводиться."""
    seen = []

    def traverse(ids):
        seen.append(list(ids))
        return set()

    derive_context_core({"b.py#g", "a.py#f"}, set(), traverse)
    assert seen == [["a.py#f", "b.py#g"]]
