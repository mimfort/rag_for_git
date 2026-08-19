"""Markdown-отчёт replay: агрегат, A/B-дельта и дельта по задачам (PRI-254)."""
from __future__ import annotations

from . import replay_history

AGGREGATE_ROWS = (
    ("core_recall_median", "core-recall (медиана)"),
    ("core_recall_mean", "core-recall (среднее)"),
    ("bulk_core_recall_median", "core-recall bulk (ядро ≥ 10)"),
    ("bulk_n_measured", "bulk N"),
    ("context_recall_median", "context-recall (медиана)"),
    ("context_n_measured", "context измерено"),
    ("no_context_measurement", "context без точки измерения"),
    ("union_precision_median", "union-precision (медиана)"),
    ("precision_median", "precision (медиана)"),
    ("predicted_median", "предсказано файлов (медиана)"),
    ("n_measured", "задач измерено"),
    ("no_measurement", "без точки измерения"),
)

DISCLAIMER = (
    "Линия `replay` и линия `snapshot` **несравнимы напрямую**: snapshot считает "
    "пути, которые отобрала LLM из выдачи ретрива, а replay — всю выдачу ретрива. "
    "Сравнивать можно только replay с replay."
)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _delta(old, new) -> str:
    if old is None or new is None:
        return "—"
    return f"{new - old:+.4g}"


def _identity(snapshot: dict, label: str) -> list:
    params = snapshot.get("variant_params")
    return [
        f"- **{label}**: вариант `{snapshot['variant']}`"
        + (f", параметры `{params}`" if params else "")
        + f", коммит `{snapshot.get('commit')}`"
        + f", indexed_sha `{snapshot.get('indexed_sha')}`"
        + f", корпус {snapshot.get('corpus')}"
        + (", **частичный (--limit)**" if snapshot.get("partial") else "")
    ]


def _task_delta_rows(new: dict, old: dict) -> tuple:
    """Строки дельты по задачам и число задач без изменений."""
    old_by_key = {row["key"]: row for row in old["tasks"]}
    rows: list = []
    unchanged = 0
    for row in new["tasks"]:
        before = old_by_key.get(row["key"])
        if before is None:
            continue
        old_recall = before.get("core_recall")
        new_recall = row.get("core_recall")
        gained = sorted(set(row["predicted_paths"]) - set(before["predicted_paths"]))
        lost = sorted(set(before["predicted_paths"]) - set(row["predicted_paths"]))
        if old_recall == new_recall and not gained and not lost:
            unchanged += 1
            continue
        magnitude = abs((new_recall or 0) - (old_recall or 0))
        rows.append(
            (
                magnitude,
                [
                    row["key"],
                    row["status"],
                    _fmt(old_recall),
                    _fmt(new_recall),
                    _delta(old_recall, new_recall),
                    ", ".join(f"`{p}`" for p in gained) or "—",
                    ", ".join(f"`{p}`" for p in lost) or "—",
                ],
            )
        )
    rows.sort(key=lambda item: -item[0])
    return [cells for _, cells in rows], unchanged


def render(new: dict, old: dict | None = None) -> str:
    """Отчёт по прогону; при наличии второй стороны — с дельтами."""
    lines = [
        "# Replay-метрики ретрива solve-task",
        "",
        f"Прогон от {new['taken_at']}, репозиторий `{new.get('repo')}`, "
        f"ветка `{new.get('branch')}`.",
        "",
        "## Идентичность прогона",
        "",
    ]
    if old is not None:
        lines += _identity(old, "до")
    lines += _identity(new, "после" if old is not None else "прогон")
    lines.append("")

    if old is not None:
        warnings = replay_history.comparability_warnings(old, new)
        if warnings:
            lines += ["> **Стороны различаются не только вариантом:**", ""]
            lines += [f"> - {warning}" for warning in warnings]
            lines.append("")

    lines += ["## Агрегат", ""]
    if old is None:
        lines += ["| Метрика | Значение |", "|---|---|"]
        for key, label in AGGREGATE_ROWS:
            lines.append(f"| {label} | {_fmt(new['aggregate'].get(key))} |")
    else:
        lines += ["| Метрика | до | после | Δ |", "|---|---|---|---|"]
        for key, label in AGGREGATE_ROWS:
            before = old["aggregate"].get(key)
            after = new["aggregate"].get(key)
            lines.append(
                f"| {label} | {_fmt(before)} | {_fmt(after)} | {_delta(before, after)} |"
            )
    lines.append("")

    lines += ["## Статусы задач", "", "| Статус | Задач |", "|---|---|"]
    for status, count in new.get("statuses", {}).items():
        lines.append(f"| {status} | {count} |")
    lines.append("")

    if old is not None:
        rows, unchanged = _task_delta_rows(new, old)
        lines += [
            "## Дельта по задачам",
            "",
            "| Ключ | Статус | до | после | Δ | приобретено | потеряно |",
            "|---|---|---|---|---|---|---|",
        ]
        for cells in rows:
            lines.append("| " + " | ".join(cells) + " |")
        if unchanged:
            lines.append(f"| _и ещё {unchanged}_ | без изменений | — | — | — | — | — |")
        lines.append("")
    else:
        lines += [
            "## Задачи",
            "",
            "| Ключ | Статус | core-recall | предсказано | попало "
            "| ctx | ctx_hit | ctx_recall | u_prec |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for row in new["tasks"]:
            lines.append(
                f"| {row['key']} | {row['status']} | {_fmt(row.get('core_recall'))} "
                f"| {row['predicted']} | {row['hit_core']} "
                f"| {_fmt(row.get('context_core'))} | {_fmt(row.get('hit_context'))} "
                f"| {_fmt(row.get('context_recall'))} | {_fmt(row.get('union_precision'))} |"
            )
        lines.append("")

    lines += ["## Оговорка", "", DISCLAIMER, ""]
    return "\n".join(lines)
