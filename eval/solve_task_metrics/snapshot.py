"""Сборка среза метрик по всему корпусу брифов."""
from __future__ import annotations

import pathlib
import statistics
from collections import Counter

from . import briefs, classify, cost, ground_truth, history, recall

# Режим окна замера цены этапа. 'sealed' — брифы, размеченные после починки
# повторного срабатывания brief_cost (окно = до первой записи брифа);
# 'legacy' — до неё. Смешивать значения в сравнении нельзя.
WINDOW_MODE = "sealed"


def _median(values: list):
    return statistics.median(values) if values else None


def build_snapshot(
    briefs_dir: pathlib.Path,
    run_git,
    commit: str,
    taken_at: str,
    config,
    transcripts=None,
) -> tuple:
    """Посчитать срез по корпусу брифов.

    Args:
        briefs_dir: каталог брифов.
        run_git: GitRunner (инъектируется, чтобы тесты шли без git-репозитория).
        commit: sha HEAD на момент прогона.
        taken_at: ISO-8601 метка времени прогона.
        config: BriefQualityConfig репозитория — обязателен, передаётся вызывающим
            явно; молчаливый дефолт (ядро rag_for_git) — тот тихий провал, ради
            починки которого затевалась вся задача.
        transcripts: результат endtoend.scan_transcripts() или None.

    Returns:
        (срез, per-task строки для отчёта).
    """
    records = briefs.load_briefs(briefs_dir, config)
    with_tokens = [r for r in records if r.token_block]
    # Один ключ = одна задача: два брифа с одним ключом (переписанный бриф,
    # вторая итерация) иначе дали бы задаче двойной вес в агрегате.
    seen_keys: set = set()
    with_key: list = []
    for record in records:
        if not record.task_key or record.task_key in seen_keys:
            continue
        seen_keys.add(record.task_key)
        with_key.append(record)

    weighted_values: list = []
    raw_values: list = []
    for record in with_tokens:
        block = record.token_block
        buckets = cost.sum_buckets(
            list(block.main_by_model.values()) + list(block.sidechain_by_model.values())
        )
        weighted_values.append(cost.weighted(buckets))
        raw_values.append(cost.raw(buckets))

    quality_rows: list = []
    report_rows: list = []
    misses: Counter = Counter()
    sync_skipped = 0
    diff_failures = 0

    for record in with_key:
        truth = ground_truth.collect(record.task_key, run_git)
        sync_skipped += truth.sync_merges_skipped
        diff_failures += truth.diff_failures
        if not truth.changed:
            continue
        # Один путь проверяется дважды — как кандидат в ядро и как промах;
        # кэш на задачу убирает лишний git cat-file на каждый такой файл.
        existed_cache: dict = {}

        def existed(path: str, _truth=truth, _cache=existed_cache) -> bool:
            if path not in _cache:
                _cache[path] = ground_truth.path_existed(
                    _truth.parent_ref, path, run_git
                )
            return _cache[path]

        expected_core = {
            path
            for path in truth.changed
            if classify.is_core_production_path(path, config) and existed(path)
        }
        row = recall.evaluate_task(
            record.task_key, record.relevant_paths, truth.changed, expected_core
        )
        quality_rows.append(row)
        for missed in truth.changed - record.relevant_paths:
            misses[classify.categorize_miss(missed, existed(missed), config)] += 1
        report_rows.append(
            {
                "key": row.task_key,
                "file": record.filename,
                "expected": row.expected,
                "expected_core": row.expected_core,
                "predicted": row.predicted,
                "hit_core": row.hit_core,
                "core_recall": row.core_recall,
                "raw_recall": row.raw_recall,
                "precision": row.precision,
            }
        )

    aggregate = recall.aggregate(quality_rows)
    weighted_median = _median(weighted_values)
    raw_median = _median(raw_values)

    snapshot = {
        "schema": history.SCHEMA,
        "taken_at": taken_at,
        "commit": commit,
        "window_mode": WINDOW_MODE,
        "corpus": {
            "briefs": len(records),
            "with_tokens": len(with_tokens),
            "with_key": len(with_key),
            "with_ground_truth": len(quality_rows),
            "sync_merges_skipped": sync_skipped,
            "diff_failures": diff_failures,
        },
        "cost": {
            "weighted_median": weighted_median,
            "raw_median": raw_median,
            "inflation": cost.inflation(raw_median or 0.0, weighted_median or 0.0),
        },
        "quality": {
            "core_recall_median": aggregate.core_recall_median,
            "core_recall_mean": aggregate.core_recall_mean,
            "raw_recall_median": aggregate.raw_recall_median,
            "denominator_median": aggregate.denominator_median,
            "n_measured": aggregate.n_measured,
            "no_measurement": aggregate.no_measurement,
            "bulk_core_recall_median": aggregate.bulk_core_recall_median,
            "bulk_n_measured": aggregate.bulk_n_measured,
        },
        "misses": dict(misses),
    }
    if transcripts is not None:
        measured = [v["weighted"] for v in transcripts.values() if v.get("weighted")]
        snapshot["endtoend"] = {
            "measured": len(measured),
            "weighted_median": _median(measured),
        }
    return snapshot, report_rows
