"""Unit-тесты core-recall и состояния «нет точки измерения»."""
import pytest

from reviewer.metrics.brief_quality import recall


def test_evaluate_task_core_recall():
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py", "docs/x.md"},
        expected={"reviewer/a.py", "reviewer/b.py", "reviewer/c.py", "tests/t.py"},
        expected_core={"reviewer/a.py", "reviewer/b.py", "reviewer/c.py"},
    )

    assert row.hit_core == 2
    assert row.core_recall == pytest.approx(2 / 3)
    assert row.raw_recall == pytest.approx(2 / 4)
    assert row.precision == pytest.approx(2 / 3)


def test_evaluate_task_empty_core_denominator_is_no_measurement():
    row = recall.evaluate_task(
        "PRI-2",
        predicted={"reviewer/a.py"},
        expected={"docs/x.md", "tests/t.py"},
        expected_core=set(),
    )

    assert row.core_recall is None
    assert row.expected_core == 0


def test_evaluate_task_without_predictions_has_no_precision():
    row = recall.evaluate_task(
        "PRI-3", predicted=set(), expected={"reviewer/a.py"},
        expected_core={"reviewer/a.py"},
    )

    assert row.precision is None
    assert row.core_recall == 0.0


def test_aggregate_excludes_no_measurement_from_medians():
    rows = [
        recall.evaluate_task(
            "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"}
        ),
        recall.evaluate_task(
            "PRI-2", {"reviewer/a.py"}, {"reviewer/a.py", "reviewer/b.py"},
            {"reviewer/a.py", "reviewer/b.py"},
        ),
        recall.evaluate_task("PRI-3", {"reviewer/a.py"}, {"docs/x.md"}, set()),
    ]

    agg = recall.aggregate(rows)

    assert agg.n_measured == 2
    assert agg.no_measurement == 1
    # медиана из [1.0, 0.5] = 0.75; пустой знаменатель нулём НЕ считается
    assert agg.core_recall_median == pytest.approx(0.75)
    assert agg.denominator_median == pytest.approx(1.5)


def test_aggregate_of_only_no_measurement_rows():
    rows = [recall.evaluate_task("PRI-9", set(), {"docs/x.md"}, set())]

    agg = recall.aggregate(rows)

    assert agg.n_measured == 0
    assert agg.no_measurement == 1
    assert agg.core_recall_median is None
