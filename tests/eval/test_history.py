"""Unit-тесты хранилища срезов и режима сравнения."""
import json

from eval.solve_task_metrics import history

OLD = {
    "schema": history.SCHEMA,
    "taken_at": "2026-08-01T00:00:00+00:00",
    "commit": "aaa",
    "window_mode": "legacy",
    "corpus": {"briefs": 50, "with_tokens": 28},
    "cost": {"weighted_median": 654_000.0, "raw_median": 2_810_000.0},
    "quality": {"core_recall_median": 0.61, "no_measurement": 10},
}
NEW = {
    "schema": history.SCHEMA,
    "taken_at": "2026-08-14T00:00:00+00:00",
    "commit": "bbb",
    "window_mode": "sealed",
    "corpus": {"briefs": 57, "with_tokens": 34},
    "cost": {"weighted_median": 600_000.0, "raw_median": 2_700_000.0},
    "quality": {"core_recall_median": 0.70, "no_measurement": 9},
    "endtoend": {"measured": 4, "weighted_median": 1_200_000.0},
}


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / "history.jsonl"

    history.append_snapshot(path, OLD)
    history.append_snapshot(path, NEW)

    loaded = history.load_snapshots(path)
    assert [s["commit"] for s in loaded] == ["aaa", "bbb"]
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_load_snapshots_missing_file_is_empty(tmp_path):
    assert history.load_snapshots(tmp_path / "нет.jsonl") == []


def test_load_snapshots_skips_broken_line(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps(OLD) + "\nне json\n", encoding="utf-8")

    assert len(history.load_snapshots(path)) == 1


def test_diff_marks_cost_drop_as_improvement():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["cost.weighted_median"].delta == -54_000.0
    assert deltas["cost.weighted_median"].direction == "улучшение"


def test_diff_marks_recall_growth_as_improvement():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["quality.core_recall_median"].direction == "улучшение"


def test_diff_marks_new_metric_as_new_not_growth_from_zero():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["endtoend.weighted_median"].old is None
    assert deltas["endtoend.weighted_median"].direction == "новая"
    assert deltas["endtoend.weighted_median"].delta is None


def test_diff_marks_equal_values_as_unchanged():
    deltas = {d.metric: d for d in history.diff_snapshots(NEW, NEW)}

    assert deltas["cost.weighted_median"].direction == "без изменений"


def test_diff_flags_window_mode_change():
    deltas = {d.metric: d for d in history.diff_snapshots(OLD, NEW)}

    assert deltas["window_mode"].old == "legacy"
    assert deltas["window_mode"].new == "sealed"
    assert deltas["window_mode"].direction == "несопоставимо"


def test_trend_marks_absent_metric_as_none_not_zero():
    """Срез, снятый до появления метрики, её не мерил — это не ноль."""
    old = {"taken_at": "t1", "commit": "aaa", "window_mode": "sealed", "cost": {"weighted_median": 100.0}}
    new = {
        "taken_at": "t2",
        "commit": "bbb",
        "window_mode": "sealed",
        "cost": {"weighted_median": 90.0},
        "endtoend": {"weighted_median": 5.0},
    }

    rows = history.trend([old, new])

    assert rows[0]["values"]["endtoend.weighted_median"] is None
    assert rows[1]["values"]["endtoend.weighted_median"] == 5.0
    assert rows[0]["commit"] == "aaa"


def test_bulk_core_recall_is_higher_better_and_trended():
    """Minor 8: bulk-метрика зарегистрирована в реестре полярности и тренда.

    Без этого `compare` печатал бы рост как нейтральный «рост», а не
    «улучшение», и метрика не показывалась бы в тренде — критерий 4 требует
    подтверждать рост именно через сравнение срезов.
    """
    old = {"taken_at": "t1", "commit": "aaa", "window_mode": "sealed",
           "quality": {"bulk_core_recall_median": 0.37}}
    new = {"taken_at": "t2", "commit": "bbb", "window_mode": "sealed",
           "quality": {"bulk_core_recall_median": 0.56}}

    deltas = {d.metric: d for d in history.diff_snapshots(old, new)}
    assert deltas["quality.bulk_core_recall_median"].direction == "улучшение"

    trend_keys = [key for key, _ in history.TREND_METRICS]
    assert "quality.bulk_core_recall_median" in trend_keys
    rows = history.trend([old, new])
    assert rows[1]["values"]["quality.bulk_core_recall_median"] == 0.56


def test_select_pair_walks_back_and_refuses_short_history():
    snapshots = [{"taken_at": "t1"}, {"taken_at": "t2"}, {"taken_at": "t3"}]

    assert history.select_pair(snapshots, 1) == (snapshots[1], snapshots[2])
    assert history.select_pair(snapshots, 2) == (snapshots[0], snapshots[2])
    assert history.select_pair(snapshots, 3) == (None, None)
    assert history.select_pair(snapshots, 0) == (None, None)
