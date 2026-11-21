"""Пересчёт качества брифов по PR-мержам клона (PRI-270).

Git приходит инъекцией (GitRunner), история — объектом с record_brief_quality:
модуль тестируется без git-репозитория и без Postgres. Voyage не задействован
вовсе — метрика ничего не эмбеддит.
"""
from __future__ import annotations

import pathlib


def measure_corpus(clone_path, repo, config, run_git, history) -> dict:
    """Посчитать корпус брифов клона и записать строку на каждый PR-мерж.

    Строка на PR, а не на задачу: идентичность строки измерения —
    (repo, pr_number, task_key), а task-level число собирается union'ом на
    чтении (ReviewHistory.brief_quality_trend) — той же линейкой, которой
    считает офлайн-baseline.
    """
    from reviewer.metrics.brief_quality import briefs as briefs_mod
    from reviewer.metrics.brief_quality import ground_truth
    from reviewer.services import brief_quality

    directory = pathlib.Path(clone_path) / config.briefs_dir
    summary = {"briefs": 0, "skipped_no_key": 0, "skipped_no_merges": 0, "rows": 0}
    for record in briefs_mod.load_briefs(directory, config):
        summary["briefs"] += 1
        if not record.task_key:
            summary["skipped_no_key"] += 1
            continue
        truth = ground_truth.collect(record.task_key, run_git)
        if not truth.merges:
            summary["skipped_no_merges"] += 1
            continue
        for merge in truth.merges:
            status_map = ground_truth.changed_status(merge.sha, run_git)
            status = brief_quality.measure_and_record(
                task_key=record.task_key, repo=repo, pr_number=merge.number,
                head_sha=merge.sha, changed_paths=list(status_map),
                changed_status=status_map, clone_path=clone_path, config=config,
                history=history, run_id=None,
            )
            if status is None:
                # history здесь всегда задан (CLI создаёт его перед вызовом) —
                # None означает сбой самой записи, а не «истории не было».
                # rows не растёт: строка реально не записана.
                summary["write_failed"] = summary.get("write_failed", 0) + 1
            else:
                summary["rows"] += 1
                summary[status] = summary.get(status, 0) + 1
    return summary
