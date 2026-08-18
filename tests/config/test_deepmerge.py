"""Рекурсивное слияние слоёв политики с листовой диагностикой (PRI-260)."""
from __future__ import annotations

from reviewer.config.deepmerge import leaf_paths, merge_layer


def _merge(*layers: tuple[dict, str]):
    merged: dict[str, object] = {}
    sources: dict[str, str] = {}
    shadowed: dict[str, list[str]] = {}
    for data, source in layers:
        merge_layer(merged, sources, shadowed, data, source)
    return merged, sources, shadowed


def test_partial_mapping_layer_keeps_untouched_subsections():
    """Слой, не высказавшийся о подсекции, её не стирает."""
    merged, sources, shadowed = _merge(
        ({"context_limits": {"code_section": {"max_files": 12}}}, ".review.yml"),
        ({"context_limits": {"graph": {"hops": 2}}}, "home:repos/o/r.yml"),
    )

    assert merged["context_limits"] == {
        "code_section": {"max_files": 12},
        "graph": {"hops": 2},
    }
    assert sources["context_limits.code_section.max_files"] == ".review.yml"
    assert sources["context_limits.graph.hops"] == "home:repos/o/r.yml"
    assert shadowed == {}


def test_shadowing_names_the_leaf_not_the_top_key():
    merged, sources, shadowed = _merge(
        ({"context_limits": {"code_section": {"max_files": 12, "chars_per_file": 1300}}},
         ".review.yml"),
        ({"context_limits": {"code_section": {"max_files": 20}}}, "home:repos/o/r.yml"),
    )

    assert merged["context_limits"]["code_section"] == {
        "max_files": 20, "chars_per_file": 1300,
    }
    assert shadowed == {"context_limits.code_section.max_files": [".review.yml"]}


def test_lists_and_scalars_are_replaced_whole():
    merged, sources, shadowed = _merge(
        ({"paths": {"ignore": ["a", "b"]}, "max_comments": 5}, "home:review.yml"),
        ({"paths": {"ignore": ["c"]}, "max_comments": 7}, ".review.yml"),
    )

    assert merged["paths"] == {"ignore": ["c"]}          # без слияния по элементам
    assert merged["max_comments"] == 7
    assert sources["paths.ignore"] == ".review.yml"
    assert sources["max_comments"] == ".review.yml"      # скаляр: путь = прежний плоский ключ
    assert shadowed["paths.ignore"] == ["home:review.yml"]
    assert shadowed["max_comments"] == ["home:review.yml"]


def test_task_board_is_atomic():
    """Связный контракт доски не смешивается между слоями."""
    merged, sources, shadowed = _merge(
        ({"task_board": {"type": "yougile", "project": "PRI", "done_target": "Готово"}},
         ".review.yml"),
        ({"task_board": {"type": "jira"}}, "home:repos/o/r.yml"),
    )

    assert merged["task_board"] == {"type": "jira"}
    assert sources["task_board"] == "home:repos/o/r.yml"
    assert shadowed["task_board"] == [".review.yml"]


def test_categories_merge_per_flag():
    merged, _sources, shadowed = _merge(
        ({"categories": {"sql": True, "security": True}}, ".review.yml"),
        ({"categories": {"sql": False}}, "home:repos/o/r.yml"),
    )

    assert merged["categories"] == {"sql": False, "security": True}
    assert shadowed == {"categories.sql": [".review.yml"]}


def test_type_change_clears_stale_leaf_records():
    """Mapping, заменённый скаляром, не оставляет следов своих листьев."""
    merged, sources, shadowed = _merge(
        ({"context_limits": {"graph": {"hops": 2}}}, "home:review.yml"),
        ({"context_limits": None}, ".review.yml"),
    )

    assert merged["context_limits"] is None
    assert "context_limits.graph.hops" not in sources
    assert sources["context_limits"] == ".review.yml"
    assert shadowed["context_limits.graph.hops"] == ["home:review.yml"]


def test_leaf_paths_of_empty_mapping_is_the_mapping_itself():
    assert leaf_paths({"a": {"b": 1}}, "top") == ["top.a.b"]
    assert leaf_paths({}, "top") == ["top"]
    assert leaf_paths(5, "top") == ["top"]


def test_empty_mapping_layer_clears_the_section():
    """Явный пустой mapping — высказывание слоя «секции нет», а не молчание.

    До фикса рекурсия входила в пустой словарь, тело цикла не выполнялось ни
    разу, и слой пропадал бесследно: ни значения, ни записи в shadowed.
    """
    merged, sources, shadowed = _merge(
        ({"categories": {"sql": True, "security": True}}, ".review.yml"),
        ({"categories": {}}, "home:repos/o/r.yml"),
    )

    assert merged["categories"] == {}
    assert sources["categories"] == "home:repos/o/r.yml"
    assert shadowed["categories.sql"] == [".review.yml"]
    assert shadowed["categories.security"] == [".review.yml"]


def test_non_string_keys_share_one_namespace_with_diagnostics():
    """Ключ нормализуется к str: merged и диагностика в одном пространстве."""
    merged, sources, shadowed = _merge(
        ({1: {"a": 1}}, "home:review.yml"),
        ({"1": 2}, ".review.yml"),
    )

    assert merged == {"1": 2}          # один ключ, а не int-дубль рядом со str
    assert sources == {"1": ".review.yml"}
    assert shadowed["1.a"] == ["home:review.yml"]
