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
