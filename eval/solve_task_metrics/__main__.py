"""CLI офлайн-харнесса метрик solve-task.

Запуск: python -m eval.solve_task_metrics {snapshot|stats|compare|forecast}
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from . import (
    endtoend,
    forecast,
    ground_truth,
    history,
    replay as replay_mod,
    replay_history,
    replay_report,
    report,
    report_merge,
    snapshot as snapshot_mod,
    steps,
    subquery_stats,
    variants,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIEFS_DIR = REPO_ROOT / "docs" / "superpowers" / "briefs"
EVAL_DIR = REPO_ROOT / "eval"
HISTORY_PATH = EVAL_DIR / history.HISTORY_PATH_NAME
REPORT_PATH = EVAL_DIR / "solve_task_metrics_report.md"
REPLAY_HISTORY_PATH = EVAL_DIR / replay_history.HISTORY_PATH_NAME
REPLAY_REPORT_PATH = EVAL_DIR / "replay_report.md"
TRANSCRIPTS_ROOT = pathlib.Path.home() / ".claude" / "projects"


def _head_commit(run_git) -> str:
    try:
        return run_git(["rev-parse", "HEAD"]).strip()
    except ground_truth.GitError:
        return "unknown"


def cmd_snapshot(_args) -> int:
    try:
        report_merge.ensure_mergeable(REPORT_PATH)
    except report_merge.MarkerMissing as error:
        print(str(error))
        return 1
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
    REPORT_PATH.write_text(
        report_merge.merge_file(REPORT_PATH, report.render(snap, rows)),
        encoding="utf-8",
    )
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


def _print_model(label: str, model_steps: tuple, totals: dict, shares: dict) -> None:
    print(label)
    for step in model_steps:
        print(f"  {step:<10} {shares[step] * 100:5.1f}%  "
              f"(cache_write {totals[step]['cache_write']:.0f})")


def cmd_steps(_args) -> int:
    """Разбивка взвешенной цены solve-task по под-шагам (baseline PRI-248)."""
    per_task = steps.scan_steps(TRANSCRIPTS_ROOT)
    if not per_task:
        print("Транскриптов solve-task не найдено")
        return 1
    totals = {
        scope: {
            "pointwise": {s: {k: 0.0 for k in steps.BUCKET_KEYS} for s in steps.STEPS},
            "sticky": {s: {k: 0.0 for k in steps.BUCKET_KEYS} for s in steps.STICKY_STEPS},
        }
        for scope in steps.SCOPES
    }
    for entry in per_task.values():
        for scope in steps.SCOPES:
            for model in steps.MODELS:
                for step, buckets in entry[scope][model].items():
                    for key in steps.BUCKET_KEYS:
                        totals[scope][model][step][key] += buckets[key]

    phase_point_shares = steps.weighted_shares(totals["phase"]["pointwise"])
    phase_sticky_shares = steps.weighted_shares(totals["phase"]["sticky"])
    session_point_shares = steps.weighted_shares(totals["session"]["pointwise"])

    print(f"Задач измерено: {len(per_task)}")
    print()
    _print_model(
        "По фазе сборки брифа — точечная модель (по факту вызова тула, нижняя оценка стадии):",
        steps.STEPS, totals["phase"]["pointwise"], phase_point_shares,
    )
    point_consolidated = phase_point_shares["preflight"] + phase_point_shares["gather"]
    print(f"Точечная: preflight+gather (фаза брифа): {point_consolidated * 100:.1f}%")
    print(f"Точечная: unattributed внутри фазы (other, нераспознанный классификатором расход): "
          f"{phase_point_shares['other'] * 100:.1f}%")
    print()
    _print_model(
        "По фазе сборки брифа — липкая модель (по активной стадии между переключениями, "
        "верхняя оценка стадии):",
        steps.STICKY_STEPS, totals["phase"]["sticky"], phase_sticky_shares,
    )
    sticky_consolidated = phase_sticky_shares["preflight"] + phase_sticky_shares["gather"]
    print(f"Липкая: preflight+gather (фаза брифа): {sticky_consolidated * 100:.1f}%")
    print(f"Липкая: startup (фаза брифа): {phase_sticky_shares['startup'] * 100:.1f}%")
    print()
    print("Истина между точечной (нижняя оценка) и липкой (верхняя оценка) моделями — "
          "ни одна не выбрана как единственно верная.")
    print()
    _print_model(
        "По всей сессии — точечная модель (включая всё после записи брифа, "
        "для сопоставимости с PRI-246):",
        steps.STEPS, totals["session"]["pointwise"], session_point_shares,
    )
    session_consolidated = session_point_shares["preflight"] + session_point_shares["gather"]
    print(f"Точечная: preflight+gather (вся сессия): {session_consolidated * 100:.1f}%")
    return 0


def _replay_side(provider, args, run_git, commit, taken_at, repo, branch,
                 variant_name, limits) -> dict:
    """Прогнать одну сторону A/B и сохранить снимок."""
    target = variants.ReplayTarget(repo=repo, branch=branch, limits=limits)
    snap = replay_mod.run_replay(
        provider=provider, run_git=run_git, briefs_dir=BRIEFS_DIR,
        target=target, variant_name=variant_name, commit=commit,
        taken_at=taken_at, limit=args.limit, seed_mode=args.context_seeds,
    )
    replay_history.append(REPLAY_HISTORY_PATH, snap)
    return snap


def cmd_replay(args) -> int:
    """Прогнать ретрив по корпусу и сравнить варианты (PRI-254)."""
    try:
        variants.get_variant(args.variant)
        limits = variants.parse_overrides(args.set)
    except (variants.UnknownVariant, variants.BadOverride) as error:
        print(str(error))
        return 1
    if args.variant == "limits" and not limits:
        print("вариант 'limits' требует хотя бы один --set <раздел>.<ключ>=<значение>")
        return 1

    baseline_snap = None
    if args.baseline:
        try:
            baseline_snap = replay_history.select(
                replay_history.load(REPLAY_HISTORY_PATH), args.baseline
            )
        except (replay_history.SnapshotNotFound,
                replay_history.PartialSnapshotRejected) as error:
            print(str(error))
            return 1

    try:
        report_merge.ensure_mergeable(REPLAY_REPORT_PATH)
    except report_merge.MarkerMissing as error:
        print(str(error))
        return 1

    from . import live  # ленивый импорт: живые зависимости только здесь

    run_git = ground_truth.git_runner(REPO_ROOT)
    commit = _head_commit(run_git)
    taken_at = dt.datetime.now(dt.timezone.utc).isoformat()
    provider, repo, branch = live.open_live(args.repo, args.branch)
    try:
        if baseline_snap is None and args.variant != "baseline":
            print("Прогон стороны «до» (baseline)…")
            baseline_snap = _replay_side(
                provider, args, run_git, commit, taken_at, repo, branch,
                "baseline", None,
            )
        print(f"Прогон варианта «{args.variant}»…")
        snap = _replay_side(
            provider, args, run_git, commit, taken_at, repo, branch,
            args.variant, limits,
        )
    finally:
        provider.close()

    REPLAY_REPORT_PATH.write_text(
        report_merge.merge_file(
            REPLAY_REPORT_PATH, replay_report.render(snap, baseline_snap)
        ),
        encoding="utf-8",
    )
    print(f"Снимок сохранён: {REPLAY_HISTORY_PATH}")
    print(f"Отчёт записан: {REPLAY_REPORT_PATH}")
    # Ветка и sha индекса печатаются рядом с числом намеренно: прогон по
    # первичной ветке при работе в другой даёт корректное, но не то число,
    # и без этой строки его легко принять за baseline своей ветки.
    print(
        f"Индекс: {snap['repo']}@{snap['branch']} "
        f"sha={snap['indexed_sha']} чанков={snap['chunks']}"
    )
    aggregate = snap["aggregate"]
    print(
        f"core-recall медиана: {aggregate['core_recall_median']}, "
        f"N={aggregate['n_measured']}, "
        f"bulk={aggregate['bulk_core_recall_median']} "
        f"(N={aggregate['bulk_n_measured']})"
    )
    if snap["partial"]:
        print("Снимок частичный (--limit): как сторона сравнения не годится.")
    return 0


def cmd_subqueries(args) -> int:
    """Распределение числа подзапросов по корпусу (PRI-255, критерий 1)."""
    from . import live  # ленивый импорт: живые зависимости только здесь

    provider, repo, branch = live.open_live(args.repo, args.branch)
    with provider:
        rows = []
        for key in replay_mod.corpus_keys(BRIEFS_DIR):
            task = provider.task(key)
            rows.append((key, task, provider.query(task, key)))
    print(f"Корпус: {len(rows)} задач, репозиторий {repo}@{branch}")
    print(subquery_stats.render(rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Собрать парсер CLI (выделено, чтобы разбор аргументов тестировался)."""
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
    replay_parser = subparsers.add_parser(
        "replay",
        help="прогнать ретрив по корпусу заново и сравнить варианты (A/B)",
    )
    replay_parser.add_argument(
        "--variant",
        default="baseline",
        help=f"вариант ретрива: {', '.join(variants.VARIANT_NAMES)}",
    )
    replay_parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="РАЗДЕЛ.КЛЮЧ=ЗНАЧЕНИЕ",
        help="оверрайд лимитов для варианта limits (повторяемый)",
    )
    replay_parser.add_argument(
        "--baseline",
        default=None,
        metavar="ССЫЛКА",
        help="переиспользовать сохранённый снимок как сторону «до»: "
             "last, -N или имя варианта",
    )
    replay_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="усечь корпус (снимок помечается частичным)",
    )
    replay_parser.add_argument(
        "--context-seeds",
        default=replay_mod.SEED_MODE_LINES,
        choices=list(replay_mod.SEED_MODES),
        help="источник разрешённых имён контекстного ядра: "
             "lines (по изменённым строкам) или lines+signature (плюс шапки сидов)",
    )
    replay_parser.add_argument(
        "--repo", default=None, help="owner/name; по умолчанию DEFAULT_REPO"
    )
    replay_parser.add_argument(
        "--branch", default=None, help="ветка; по умолчанию первичная"
    )
    subqueries_parser = subparsers.add_parser(
        "subqueries",
        help="распределение числа подзапросов ретрива по размеру задачи",
    )
    subqueries_parser.add_argument(
        "--repo", default=None, help="owner/name; по умолчанию DEFAULT_REPO"
    )
    subqueries_parser.add_argument(
        "--branch", default=None, help="ветка; по умолчанию первичная"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "snapshot":
        return cmd_snapshot(args)
    if args.command == "stats":
        return cmd_stats(args)
    if args.command == "forecast":
        return cmd_forecast(args)
    if args.command == "steps":
        return cmd_steps(args)
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "subqueries":
        return cmd_subqueries(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
