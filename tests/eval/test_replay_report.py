"""Markdown-отчёт replay и A/B-дельта (PRI-254)."""
from __future__ import annotations

from eval.solve_task_metrics import replay_report


def _task(key, core_recall, predicted_paths, status="measured", **context):
    row = {
        "key": key,
        "status": status,
        "expected": 3,
        "expected_core": 2,
        "predicted": len(predicted_paths),
        "hit_core": 1,
        "core_recall": core_recall,
        "raw_recall": 0.33,
        "precision": 0.5,
        "predicted_paths": sorted(predicted_paths),
        "expected_core_paths": ["reviewer/a.py", "reviewer/b.py"],
        "context_status": "empty_context_denominator",
        "context_core": 0,
        "hit_context": 0,
        "context_recall": None,
        "union_precision": None,
        "context_core_paths": [],
    }
    row.update(context)
    return row


def _snap(variant, tasks, median, **over):
    snap = {
        "schema": 1,
        "taken_at": "2026-08-17T00:00:00+00:00",
        "commit": "aaaaaaa",
        "variant": variant,
        "variant_params": None,
        "repo": "o/n",
        "branch": "dev",
        "indexed_sha": "sha1",
        "chunks": 10,
        "graph_nodes": 20,
        "partial": False,
        "corpus": len(tasks),
        "statuses": {"measured": len(tasks)},
        "aggregate": {
            "core_recall_median": median,
            "core_recall_mean": median,
            "raw_recall_median": 0.33,
            "denominator_median": 2.0,
            "bulk_core_recall_median": None,
            "bulk_n_measured": 0,
            "context_recall_median": None,
            "context_n_measured": 0,
            "no_context_measurement": len(tasks),
            "union_precision_median": None,
            "n_measured": len(tasks),
            "no_measurement": 0,
            "precision_median": 0.5,
            "predicted_median": 1.0,
        },
        "tasks": tasks,
    }
    snap.update(over)
    return snap


def _snapshot_with(**agg_overrides):
    """Минимальный снимок с подменёнными ключами aggregate (не полной заменой).

    `_snap(**over)` делает `snap.update(over)` — верхнеуровневая замена, а не
    слияние; для точечных ключей aggregate нужен отдельный хелпер, который
    мержит именно в него.
    """
    snap = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    snap["aggregate"].update(agg_overrides)
    return snap


def test_single_run_report_has_no_delta_columns():
    text = replay_report.render(_snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5))
    assert "core-recall" in text
    assert "Δ" not in text


def test_ab_report_shows_aggregate_delta():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    new = _snap("limits", [_task("PRI-1", 1.0, ["reviewer/a.py", "reviewer/b.py"])], 1.0)
    text = replay_report.render(new, old)
    assert "Δ" in text
    assert "+0.5" in text


def test_ab_report_shows_per_task_delta_with_path_diff():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["reviewer/a.py"])], 0.5)
    new = _snap("limits", [_task("PRI-1", 1.0, ["reviewer/b.py"])], 1.0)
    text = replay_report.render(new, old)
    assert "PRI-1" in text
    assert "reviewer/b.py" in text     # приобретённый путь
    assert "reviewer/a.py" in text     # потерянный путь


def test_unchanged_tasks_are_collapsed_into_a_counter():
    tasks = [_task(f"PRI-{i}", 0.5, ["reviewer/a.py"]) for i in range(5)]
    old = _snap("baseline", tasks, 0.5)
    new = _snap("limits", tasks, 0.5)
    text = replay_report.render(new, old)
    assert "без изменений" in text
    assert text.count("PRI-0") <= 1


def test_index_drift_warning_is_surfaced():
    old = _snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5, indexed_sha="sha1")
    new = _snap("limits", [_task("PRI-1", 0.5, ["a.py"])], 0.5, indexed_sha="sha2")
    assert "indexed_sha" in replay_report.render(new, old)


def test_incomparability_disclaimer_always_present():
    text = replay_report.render(_snap("baseline", [_task("PRI-1", 0.5, ["a.py"])], 0.5))
    assert "snapshot" in text and "несравним" in text


def test_report_renders_context_columns():
    """Контекстные числа видны в отчёте: метрика, которую не печатают,
    не существует для читателя приёмки."""
    snap = _snapshot_with(
        context_recall_median=0.5, union_precision_median=0.8,
        context_n_measured=3, no_context_measurement=1,
    )
    text = replay_report.render(snap)
    assert "context_recall_median" in text or "ctx_recall" in text
    assert "0.5" in text
    assert "union_precision" in text or "u_prec" in text


def test_empty_context_denominator_renders_as_dash_not_zero():
    """None (пустой знаменатель контекста) — это «—», а не «0»: неопределённость
    и ноль различаются на протяжении всей метрики, и таблица не должна их
    смешивать."""
    task = _task("PRI-1", 0.5, ["reviewer/a.py"])  # context-поля по дефолту None/0
    snap = _snap("baseline", [task], 0.5)
    text = replay_report.render(snap)
    assert "—" in text
    row_line = next(line for line in text.splitlines() if line.startswith("| PRI-1"))
    assert "0" not in row_line.split("|")[-3]  # ctx_recall не превратился в 0
    assert "—" in row_line
