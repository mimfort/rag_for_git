"""CLI офлайн-харнесса метрик solve-task.

Запуск: python -m eval.solve_task_metrics {snapshot|compare|forecast}
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from . import endtoend, ground_truth, history, report, snapshot as snapshot_mod

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIEFS_DIR = REPO_ROOT / "docs" / "superpowers" / "briefs"
EVAL_DIR = REPO_ROOT / "eval"
HISTORY_PATH = EVAL_DIR / history.HISTORY_PATH_NAME
REPORT_PATH = EVAL_DIR / "solve_task_metrics_report.md"
TRANSCRIPTS_ROOT = pathlib.Path.home() / ".claude" / "projects"


def _head_commit(run_git) -> str:
    try:
        return run_git(["rev-parse", "HEAD"]).strip()
    except ground_truth.GitError:
        return "unknown"


def cmd_snapshot(_args) -> int:
    run_git = ground_truth.git_runner(REPO_ROOT)
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    transcripts = endtoend.scan_transcripts(TRANSCRIPTS_ROOT)
    snap, rows = snapshot_mod.build_snapshot(
        briefs_dir=BRIEFS_DIR,
        run_git=run_git,
        commit=_head_commit(run_git),
        taken_at=taken_at,
        transcripts=transcripts,
    )
    history.append_snapshot(HISTORY_PATH, snap)
    REPORT_PATH.write_text(report.render(snap, rows), encoding="utf-8")
    print(f"Срез сохранён: {HISTORY_PATH}")
    print(f"Отчёт записан: {REPORT_PATH}")
    print(
        f"Полная цена «под ключ» измерена для {snap['endtoend']['measured']} задач "
        "(остальные — транскрипт локально недоступен)"
    )
    return 0


def cmd_compare(_args) -> int:
    snapshots = history.load_snapshots(HISTORY_PATH)
    if len(snapshots) < 2:
        print("Нужно минимум два среза; сначала прогоните snapshot.")
        return 1
    deltas = history.diff_snapshots(snapshots[-2], snapshots[-1])
    print(f"Сравнение: {snapshots[-2]['taken_at']} → {snapshots[-1]['taken_at']}")
    for delta in deltas:
        old = "—" if delta.old is None else delta.old
        new = "—" if delta.new is None else delta.new
        change = "—" if delta.delta is None else f"{delta.delta:+.4g}"
        print(f"  {delta.metric}: {old} → {new} ({change}) — {delta.direction}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.solve_task_metrics",
        description="Офлайн-метрики этапа solve-task: цена, качество ретрива, тренд.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot", help="пересчитать метрики и сохранить срез")
    subparsers.add_parser("compare", help="дельты последнего среза против предыдущего")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
