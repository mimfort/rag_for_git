"""Пересчёт качества брифов по PR-мержам клона (PRI-270).

Поиск PR-мержей и разбор диффов идут через GitRunner. Список брифов
берётся из дерева HEAD. Содержимое брифа читается из дерева измеряемого PR.
Эмбеддинги для пересчёта не нужны.
"""
from __future__ import annotations


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

    summary = {"briefs": 0, "skipped_no_key": 0, "skipped_no_merges": 0, "rows": 0}
    try:
        paths = brief_quality.corpus_brief_paths(clone_path, config)
    except (OSError, ValueError):
        summary["brief_corpus_unavailable"] = 1
        return summary
    seen_keys = set()
    for path in paths:
        summary["briefs"] += 1
        task_key = briefs_mod.extract_task_key(path.name, config)
        if not task_key:
            summary["skipped_no_key"] += 1
            continue
        if task_key in seen_keys:
            continue
        seen_keys.add(task_key)
        merges, _ = ground_truth.filter_pr_merges(ground_truth.merge_rows(task_key, run_git))
        if not merges:
            summary["skipped_no_merges"] += 1
            continue
        seen_prs = set()
        for merge in merges:
            if merge.number in seen_prs:
                continue
            seen_prs.add(merge.number)
            try:
                status_map = ground_truth.changed_status(merge.sha, run_git)
            except ground_truth.GitError:
                summary["diff_failed"] = summary.get("diff_failed", 0) + 1
                continue
            status = brief_quality.measure_and_record(
                task_key=task_key, repo=repo, pr_number=merge.number,
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
