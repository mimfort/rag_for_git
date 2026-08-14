"""Метрики полноты графа: счётчики по типам рёбер и детектор просадки (PRI-252)."""
from reviewer.graph.metrics import (
    count_edges_by_rel,
    detect_edge_regression,
    format_edge_counts,
)


def test_count_edges_by_rel_groups_by_relation():
    edges = [
        ("a.py#f", "CALLS", "b.py#g"),
        ("a.py#f", "CALLS", "b.py#h"),
        ("a.py#C", "IMPLEMENTS", "b.py#Base"),
    ]
    assert count_edges_by_rel(edges) == {"CALLS": 2, "IMPLEMENTS": 1}


def test_count_edges_by_rel_empty():
    assert count_edges_by_rel([]) == {}


def test_format_edge_counts_sorts_by_size_desc():
    assert format_edge_counts({"IMPLEMENTS": 129, "CALLS": 17963}) == \
        "CALLS 17963, IMPLEMENTS 129"


def test_format_edge_counts_empty():
    assert format_edge_counts({}) == "нет"


def test_detect_edge_regression_reports_drop_over_threshold():
    msgs = detect_edge_regression({"CALLS": 30254}, {"CALLS": 17963})
    assert msgs == ["CALLS 30254 → 17963 (−41%)"]


def test_detect_edge_regression_silent_within_threshold():
    assert detect_edge_regression({"CALLS": 1000}, {"CALLS": 950}) == []


def test_detect_edge_regression_reports_vanished_type():
    msgs = detect_edge_regression({"CALLS": 100, "IMPLEMENTS": 12}, {"CALLS": 100})
    assert msgs == ["IMPLEMENTS 12 → 0 (−100%)"]


def test_detect_edge_regression_ignores_growth_and_new_types():
    assert detect_edge_regression({"CALLS": 100}, {"CALLS": 200, "IMPLEMENTS": 5}) == []


def test_detect_edge_regression_without_previous_measurement():
    assert detect_edge_regression(None, {"CALLS": 100}) == []


def test_detect_edge_regression_respects_custom_threshold():
    assert detect_edge_regression({"CALLS": 100}, {"CALLS": 95}, threshold=0.01) == \
        ["CALLS 100 → 95 (−5%)"]
