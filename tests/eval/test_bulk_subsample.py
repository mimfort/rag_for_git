"""bulk-подвыборка: задачи с широким знаменателем ядра.

Порог взят из анализа PRI-246: на нём разделялись выборки, и в него
попадают все четыре задачи-развёртки, давшие провал core-recall
(PRI-223 — 25 файлов ядра, PRI-225 — 18, PRI-215 — 14, PRI-196 — 10).
"""
from eval.solve_task_metrics.recall import BULK_CORE_THRESHOLD, TaskQuality, aggregate


def _row(key: str, expected_core: int, core_recall: float | None) -> TaskQuality:
    row = TaskQuality(task_key=key, expected=expected_core, expected_core=expected_core,
                      predicted=5, hit_core=0)
    row.core_recall = core_recall
    return row


def test_threshold_is_ten_core_files():
    """Порог зафиксирован явно, а не подобран под текущие данные."""
    assert BULK_CORE_THRESHOLD == 10


def test_bulk_subsample_takes_only_wide_tasks():
    """В подвыборку попадают задачи со знаменателем ядра >= порога."""
    rows = [_row("A", 25, 0.24), _row("B", 4, 0.80), _row("C", 10, 0.50)]
    agg = aggregate(rows)
    assert agg.bulk_n_measured == 2
    assert agg.bulk_core_recall_median == 0.37


def test_bulk_subsample_ignores_tasks_without_measurement():
    """Задача с пустым ядром в подвыборку не попадает даже при широком diff."""
    rows = [_row("A", 25, None), _row("B", 12, 0.40)]
    agg = aggregate(rows)
    assert agg.bulk_n_measured == 1
    assert agg.bulk_core_recall_median == 0.40


def test_empty_bulk_subsample_is_none_not_zero():
    """Нет ни одной широкой задачи — метрика не определена, а не равна нулю."""
    agg = aggregate([_row("A", 3, 0.9)])
    assert agg.bulk_n_measured == 0
    assert agg.bulk_core_recall_median is None
