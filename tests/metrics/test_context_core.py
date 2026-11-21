"""Чистый вывод контекстного ядра: без графа, обход инъектируется."""
from __future__ import annotations

from reviewer.metrics.brief_quality.config import DEFAULT
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

    assert derive_context_core([], {"reviewer/a.py"}, traverse, DEFAULT) == set()
    assert calls == []


def test_derives_core_paths_of_neighbours():
    def traverse(ids):
        assert ids == ["reviewer/a.py#f"]
        return {"reviewer/b.py#g", "reviewer/c.py#h"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse, DEFAULT
    )
    assert result == {"reviewer/b.py", "reviewer/c.py"}


def test_subtracts_changed_core():
    """Файл, который задача И читала, И меняла, принадлежит старому знаменателю:
    в контекстном ядре он был бы посчитан дважды."""
    def traverse(ids):
        return {"reviewer/a.py#f", "reviewer/b.py#g"}

    result = derive_context_core(
        ["reviewer/a.py#f"], {"reviewer/a.py"}, traverse, DEFAULT
    )
    assert result == {"reviewer/b.py"}


def test_filters_non_core_paths():
    """Тесты, доки и eval/ вне ядра — та же линейка, что у core-recall."""
    def traverse(ids):
        return {"tests/test_a.py#t", "docs/x.md#d", "eval/y.py#e",
                "reviewer/b.py#g"}

    result = derive_context_core(["reviewer/a.py#f"], set(), traverse, DEFAULT)
    assert result == {"reviewer/b.py"}


def test_seeds_passed_sorted_for_determinism():
    """Порядок сидов детерминирован: снимок обязан воспроизводиться."""
    seen = []

    def traverse(ids):
        seen.append(list(ids))
        return set()

    derive_context_core({"b.py#g", "a.py#f"}, set(), traverse, DEFAULT)
    assert seen == [["a.py#f", "b.py#g"]]


def test_allowed_names_none_keeps_every_neighbour():
    """Отсутствие фильтра — прежнее поведение до символа (критерий аддитивности)."""
    def traverse(ids):
        return {"reviewer/b.py#g", "reviewer/c.py#H.m"}

    assert derive_context_core(
        ["reviewer/a.py#f"], set(), traverse, DEFAULT, allowed_names=None
    ) == {"reviewer/b.py", "reviewer/c.py"}


def test_allowed_names_filters_by_last_fqn_segment():
    """Сосед выживает, только если его имя вызывалось на ИЗМЕНЁННОЙ строке.

    Это и есть починка сценария god-модулей: нетронутое тело задетой функции
    больше не тащит своих callee (PRI-236: config_show → CommittedLayerFetcher).
    """
    def traverse(ids):
        return {"reviewer/b.py#g", "reviewer/c.py#H.m", "reviewer/d.py#junk"}

    assert derive_context_core(
        ["reviewer/a.py#f"], set(), traverse, DEFAULT, allowed_names={"g", "m"}
    ) == {"reviewer/b.py", "reviewer/c.py"}


def test_empty_allowed_names_drops_every_neighbour():
    """Пустое множество — высказывание «на изменённых строках вызовов нет»,
    а не «фильтра нет»: иначе значимая правка без вызовов вернула бы весь обход."""
    def traverse(ids):
        return {"reviewer/b.py#g"}

    assert derive_context_core(
        ["reviewer/a.py#f"], set(), traverse, DEFAULT, allowed_names=set()
    ) == set()


def test_allowed_names_none_matches_unfiltered_on_random_inputs():
    """Свойство аддитивности: allowed_names=None тождественен прежней формуле.

    Проверяется на случайных входах, как в PRI-261: ни одно существующее число
    отчёта не имеет права поехать от появления нового параметра.
    """
    import random

    rnd = random.Random(20262)
    files = [f"reviewer/m{i}.py" for i in range(6)] + [f"tests/t{i}.py" for i in range(3)]
    for _ in range(200):
        neighbours = {
            f"{rnd.choice(files)}#{rnd.choice('fgh')}{rnd.randrange(3)}"
            for _ in range(rnd.randrange(6))
        }
        seeds = [f"{rnd.choice(files)}#s{rnd.randrange(3)}" for _ in range(rnd.randrange(3))]
        changed = {rnd.choice(files) for _ in range(rnd.randrange(3))}

        def traverse(ids, _n=neighbours):
            return set(_n)

        legacy = derive_context_core(seeds, changed, traverse, DEFAULT)
        assert derive_context_core(seeds, changed, traverse, DEFAULT, allowed_names=None) == legacy
