from reviewer.graph.summaries import (
    canonicalize_layout,
    compute_layout_token,
    normalize_summary_paths_ignore,
)


def test_normalize_strips_slashes_dedupes_and_sorts():
    assert normalize_summary_paths_ignore(["/tests/", "test", "tests", "", None]) == [
        "test", "tests",
    ]


def test_normalize_none_is_empty():
    assert normalize_summary_paths_ignore(None) == []


def test_canonicalize_returns_normalized_ignore_and_token():
    overrides, ignore, token = canonicalize_layout(2, {"reviewer/index": 3}, ["/tests/"])
    assert overrides == {"reviewer/index": 3}
    assert ignore == ["tests"]
    assert len(token) == 64


def test_ignore_order_and_slashes_do_not_change_token():
    assert compute_layout_token(2, {}, ["tests", "docs"]) == compute_layout_token(
        2, {}, ["/docs/", "tests"]
    )


def test_different_ignore_changes_token():
    assert compute_layout_token(2, {}, ["tests"]) != compute_layout_token(2, {}, [])


def test_empty_ignore_differs_from_default_ignore_token():
    """Выключение фильтра — тоже смена layout: token обязан отличаться."""
    assert compute_layout_token(2, {}, []) != compute_layout_token(2, {}, ["tests", "test"])


def test_ignore_is_independent_of_depth_component():
    assert compute_layout_token(2, {}, ["tests"]) != compute_layout_token(3, {}, ["tests"])
