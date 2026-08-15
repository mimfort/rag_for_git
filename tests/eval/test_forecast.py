"""Unit-тесты прогноза: интервал вместо точечного числа."""
import pytest

from eval.solve_task_metrics import forecast


def _row(core, recall):
    return {"expected_core": core, "core_recall": recall}


def test_bucket_labels_by_core_size():
    assert forecast.bucket_label(1) == "1–3"
    assert forecast.bucket_label(3) == "1–3"
    assert forecast.bucket_label(4) == "4–9"
    assert forecast.bucket_label(9) == "4–9"
    assert forecast.bucket_label(10) == "10+"
    assert forecast.bucket_label(25) == "10+"


def test_build_returns_interval_for_large_enough_bucket():
    rows = [_row(2, value) for value in (0.2, 0.4, 0.5, 0.6, 0.9)]

    items = {item.label: item for item in forecast.build(rows)}

    small = items["1–3"]
    assert small.n == 5
    assert small.enough_data is True
    assert small.recall_median == pytest.approx(0.5)
    assert small.recall_p25 < small.recall_median < small.recall_p75


def test_build_marks_small_bucket_as_insufficient():
    rows = [_row(12, 0.3), _row(15, 0.4)]

    items = {item.label: item for item in forecast.build(rows)}

    assert items["10+"].enough_data is False
    assert items["10+"].recall_median is None


def test_build_skips_rows_without_measurement():
    rows = [_row(0, None), _row(2, 0.5)]

    items = {item.label: item for item in forecast.build(rows)}

    assert items["1–3"].n == 1
    assert "0" not in items


def test_describe_never_gives_a_bare_point_estimate():
    rows = [_row(2, value) for value in (0.2, 0.4, 0.5, 0.6, 0.9)]
    item = {i.label: i for i in forecast.build(rows)}["1–3"]

    text = forecast.describe(item)

    assert "–" in text  # интервал, а не одно число
    assert "N=5" in text


def test_describe_of_insufficient_bucket_says_so():
    item = {i.label: i for i in forecast.build([_row(12, 0.3)])}["10+"]

    assert "недостаточно данных" in forecast.describe(item)


def test_bucket_label_rejects_nonpositive_core_size():
    """Знаменатель < 1 — не маленькая задача, а отсутствие точки измерения."""
    for size in (0, -5):
        with pytest.raises(ValueError):
            forecast.bucket_label(size)
