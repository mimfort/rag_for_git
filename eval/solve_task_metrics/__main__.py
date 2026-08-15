"""CLI офлайн-харнесса метрик solve-task.

Запуск: python -m eval.solve_task_metrics {snapshot|stats|compare|forecast}
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from . import endtoend, forecast, ground_truth, history, report, snapshot as snapshot_mod, steps

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


def _fmt(value) -> str:
    """Компактное представление числа метрики для таблицы."""
    if value is None:
        return "—"
    if isinstance(value, float):
        if value and abs(value) >= 1000:
            return f"{value / 1_000_000:.2f}M" if abs(value) >= 1_000_000 else f"{value / 1000:.1f}K"
        return f"{value:.4g}"
    return str(value)


def cmd_compare(args) -> int:
    snapshots = history.load_snapshots(HISTORY_PATH)
    old, new = history.select_pair(snapshots, args.back)
    if old is None:
        print(
            f"В истории {len(snapshots)} срез(ов) — для отступа {args.back} нужно "
            f"минимум {args.back + 1}; сначала прогоните snapshot."
        )
        return 1
    deltas = history.diff_snapshots(old, new)
    if args.only_changed:
        deltas = [d for d in deltas if d.direction != "без изменений"]
    print(f"Сравнение: {old['taken_at']} → {new['taken_at']}")
    if not deltas:
        print("  Изменившихся метрик нет.")
        return 0
    for delta in deltas:
        old_value = "—" if delta.old is None else delta.old
        new_value = "—" if delta.new is None else delta.new
        change = "—" if delta.delta is None else f"{delta.delta:+.4g}"
        print(f"  {delta.metric}: {old_value} → {new_value} ({change}) — {delta.direction}")
    return 0


def cmd_stats(args) -> int:
    """Тренд последних N срезов одной таблицей — без пересчёта метрик."""
    snapshots = history.load_snapshots(HISTORY_PATH)
    if not snapshots:
        print("История пуста; сначала прогоните snapshot.")
        return 1
    rows = history.trend(snapshots[-args.last :])
    headers = ["дата", "коммит", "окно"] + [label for _, label in history.TREND_METRICS]
    table = [headers]
    for row in rows:
        table.append(
            [
                (row["taken_at"] or "—")[:19],
                (row["commit"] or "—")[:7],
                row["window_mode"] or "—",
                *[_fmt(row["values"][key]) for key, _ in history.TREND_METRICS],
            ]
        )
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    for index, row in enumerate(table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))
    if len(snapshots) > args.last:
        print(f"\nПоказаны последние {args.last} из {len(snapshots)} срезов (--last N).")
    return 0


def cmd_forecast(args) -> int:
    run_git = ground_truth.git_runner(REPO_ROOT)
    _, rows = snapshot_mod.build_snapshot(
        briefs_dir=BRIEFS_DIR,
        run_git=run_git,
        commit=_head_commit(run_git),
        taken_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    items = forecast.build(rows)
    if args.core_files is None:
        print("Прогноз core-recall по размеру знаменателя ядра:")
        for item in items:
            print(f"  {forecast.describe(item)}")
        return 0
    try:
        label = forecast.bucket_label(args.core_files)
    except ValueError as error:
        print(f"Некорректный --core-files: {error}")
        return 1
    for item in items:
        if item.label == label:
            print(forecast.describe(item))
    return 0


def _print_scope(label: str, totals: dict, shares: dict) -> None:
    print(label)
    for step in steps.STEPS:
        print(f"  {step:<10} {shares[step] * 100:5.1f}%  "
              f"(cache_write {totals[step]['cache_write']:.0f})")


def cmd_steps(_args) -> int:
    """Разбивка взвешенной цены solve-task по под-шагам (baseline PRI-248)."""
    per_task = steps.scan_steps(TRANSCRIPTS_ROOT)
    if not per_task:
        print("Транскриптов solve-task не найдено")
        return 1
    totals = {
        scope: {s: {k: 0.0 for k in steps.BUCKET_KEYS} for s in steps.STEPS}
        for scope in steps.SCOPES
    }
    for entry in per_task.values():
        for scope in steps.SCOPES:
            for step, buckets in entry[scope].items():
                for key in steps.BUCKET_KEYS:
                    totals[scope][step][key] += buckets[key]
    phase_shares = steps.weighted_shares(totals["phase"])
    session_shares = steps.weighted_shares(totals["session"])

    print(f"Задач измерено: {len(per_task)}")
    print()
    _print_scope(
        "По фазе сборки брифа (до первой записи под docs/superpowers/briefs/, основная метрика):",
        totals["phase"], phase_shares,
    )
    phase_consolidated = phase_shares["preflight"] + phase_shares["gather"]
    print(f"Доля preflight+gather (фаза брифа): {phase_consolidated * 100:.1f}%")
    print(f"Доля unattributed внутри фазы (other, нераспознанный классификатором расход): "
          f"{phase_shares['other'] * 100:.1f}%")
    print()
    _print_scope(
        "По всей сессии (включая всё после записи брифа — для сопоставимости с PRI-246):",
        totals["session"], session_shares,
    )
    session_consolidated = session_shares["preflight"] + session_shares["gather"]
    print(f"Доля preflight+gather (вся сессия): {session_consolidated * 100:.1f}%")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.solve_task_metrics",
        description="Офлайн-метрики этапа solve-task: цена, качество ретрива, тренд.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot", help="пересчитать метрики и сохранить срез")
    stats_parser = subparsers.add_parser(
        "stats", help="тренд последних срезов таблицей (без пересчёта)"
    )
    stats_parser.add_argument(
        "--last", type=int, default=10, help="сколько последних срезов показать"
    )
    compare_parser = subparsers.add_parser(
        "compare", help="дельты последнего среза против предыдущего"
    )
    compare_parser.add_argument(
        "--back",
        type=int,
        default=1,
        help="на сколько срезов назад сравнивать (1 — предыдущий)",
    )
    compare_parser.add_argument(
        "--only-changed",
        action="store_true",
        help="печатать только изменившиеся метрики",
    )
    subparsers.add_parser(
        "steps", help="разбивка взвешенной цены solve-task по под-шагам (baseline PRI-248)"
    )
    forecast_parser = subparsers.add_parser(
        "forecast", help="прогноз core-recall с разбросом"
    )
    forecast_parser.add_argument(
        "--core-files",
        type=int,
        default=None,
        help="предполагаемое число файлов ядра у задачи",
    )
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    if args.command == "stats":
        return cmd_stats(args)
    if args.command == "forecast":
        return cmd_forecast(args)
    if args.command == "steps":
        return cmd_steps(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
