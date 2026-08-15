"""Markdown-отчёт по срезу метрик."""
from __future__ import annotations


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def render(snapshot: dict, rows: list) -> str:
    """Отчёт по срезу: охват, цена, качество, промахи, per-task таблица."""
    corpus = snapshot["corpus"]
    cost_block = snapshot["cost"]
    quality = snapshot["quality"]
    inflation = cost_block.get("inflation")
    if inflation:
        raw_line = (
            "- Сырая сумма токенов — **не пропорциональна стоимости**, справочно: "
            f"{_num(cost_block['raw_median'])} (медиана); завышает в {inflation:.1f}×"
        )
    else:
        raw_line = "- Сырая сумма токенов: нет данных"
    lines = [
        "# Метрики этапа solve-task",
        "",
        f"Срез от {snapshot['taken_at']}, коммит `{snapshot['commit']}`, "
        f"режим окна замера цены: `{snapshot['window_mode']}`.",
        "",
        "## Охват",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Брифов в корпусе | {corpus['briefs']} |",
        f"| С блоком токенов | {corpus['with_tokens']} |",
        f"| С ключом задачи | {corpus['with_key']} |",
        f"| С ground truth (PR-мерж найден) | {corpus['with_ground_truth']} |",
        f"| Отброшено sync-мержей | {corpus['sync_merges_skipped']} |",
        "",
        "## Цена этапа",
        "",
        f"- Взвешенный input-эквивалент (основная метрика): "
        f"**{_num(cost_block['weighted_median'])}** (медиана)",
        raw_line,
        "",
        "## Качество ретрива",
        "",
        f"- core-recall: медиана {_pct(quality['core_recall_median'])}, "
        f"среднее {_pct(quality['core_recall_mean'])}, N={quality['n_measured']}",
        f"- Задач без точки измерения (пустой знаменатель ядра): "
        f"{quality['no_measurement']} — не считаются нулевым recall",
        f"- Медианный размер знаменателя ядра: "
        f"{_num(quality['denominator_median'])}",
        f"- core-recall на bulk-подвыборке (ядро ≥ 10 файлов): "
        f"медиана {_pct(quality.get('bulk_core_recall_median'))}, "
        f"N={quality.get('bulk_n_measured', 0)}",
        f"- Сырой recall (справочно, измеряет выбор знаменателя, не качество "
        f"ретрива): медиана {_pct(quality['raw_recall_median'])}",
        "",
    ]
    if snapshot.get("endtoend"):
        end = snapshot["endtoend"]
        lines += [
            "## Полная цена задачи «под ключ»",
            "",
            f"- Измерено задач: {end['measured']} (остальные — транскрипт "
            "недоступен локально, это не ноль, а отсутствие замера)",
            f"- Взвешенный input-эквивалент: медиана {_num(end['weighted_median'])}",
            "",
        ]
    lines += ["## Промахи по категориям", "", "| Категория | Промахов |", "|---|---|"]
    for category, count in sorted(
        snapshot["misses"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"| {category} | {count} |")
    lines += [
        "",
        "## Per-task",
        "",
        "| Ключ | Бриф | expected | core | predicted | hit | core-recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {row['file']} | {row['expected']} | "
            f"{row['expected_core']} | {row['predicted']} | {row['hit_core']} | "
            f"{_pct(row['core_recall'])} |"
        )
    return "\n".join(lines) + "\n"
