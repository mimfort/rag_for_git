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


def test_context_core_absent_leaves_new_fields_neutral():
    """Без context_core поведение тождественно доPRI-261: это и есть механизм,
    которым числа приёмок PRI-255…259 остаются сравнимыми без пересчёта."""
    row = recall.evaluate_task(
        "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"}
    )
    assert row.context_recall is None
    assert row.union_precision is None
    assert row.context_core == 0
    assert row.hit_context == 0


def test_context_recall_counts_read_only_files():
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py"},
        expected={"reviewer/a.py"},
        expected_core={"reviewer/a.py"},
        context_core={"reviewer/b.py", "reviewer/c.py"},
    )
    assert row.context_core == 2
    assert row.hit_context == 1
    assert row.context_recall == 0.5


def test_empty_context_denominator_is_none_not_zero():
    """Пустое контекстное ядро — «нет точки измерения», по образцу
    empty_core_denominator; ноль занижал бы медиану систематически."""
    row = recall.evaluate_task(
        "PRI-1", {"reviewer/a.py"}, {"reviewer/a.py"}, {"reviewer/a.py"},
        context_core=set(),
    )
    assert row.context_recall is None


def test_union_precision_is_never_below_old_precision():
    """Файл, который надо было ПРОЧИТАТЬ, перестаёт считаться шумом.
    Объединение идёт по expected (все изменённые), а не по expected_core:
    по одному ядру новая precision могла бы оказаться НИЖЕ старой."""
    row = recall.evaluate_task(
        "PRI-1",
        predicted={"reviewer/a.py", "reviewer/b.py"},
        expected={"reviewer/a.py", "tests/test_a.py"},
        expected_core={"reviewer/a.py"},
        context_core={"reviewer/b.py"},
    )
    assert row.precision == 0.5
    assert row.union_precision == 1.0


def test_aggregate_reports_context_medians_and_gaps():
    rows = [
        recall.evaluate_task("A", {"reviewer/b.py"}, {"reviewer/a.py"},
                             {"reviewer/a.py"}, context_core={"reviewer/b.py"}),
        recall.evaluate_task("B", {"reviewer/a.py"}, {"reviewer/a.py"},
                             {"reviewer/a.py"}, context_core=set()),
    ]
    agg = recall.aggregate(rows)
    assert agg.context_n_measured == 1
    assert agg.no_context_measurement == 1
    assert agg.context_recall_median == 1.0
