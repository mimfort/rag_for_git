"""Хранилище срезов метрик (JSONL, append-only) и режим сравнения."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

# Версия схемы среза: чтение старых срезов не должно падать при добавлении
# новых метрик, поэтому diff работает по фактически присутствующим ключам.
SCHEMA = 1

HISTORY_PATH_NAME = "solve_task_metrics_history.jsonl"

# Полярность метрики: как трактовать рост значения.
POLARITY = {
    "cost.weighted_median": "lower_better",
    "cost.raw_median": "neutral",
    "cost.inflation": "neutral",
    "quality.core_recall_median": "higher_better",
    "quality.core_recall_mean": "higher_better",
    "quality.bulk_core_recall_median": "higher_better",
    "quality.raw_recall_median": "neutral",
    "quality.denominator_median": "neutral",
    "quality.no_measurement": "lower_better",
    "quality.n_measured": "higher_better",
    "corpus.briefs": "neutral",
    "corpus.with_tokens": "neutral",
    "corpus.with_key": "neutral",
    "corpus.with_ground_truth": "neutral",
    "corpus.sync_merges_skipped": "neutral",
    "corpus.diff_failures": "lower_better",
    "endtoend.measured": "higher_better",
    "endtoend.weighted_median": "lower_better",
}


# Метрики, которые показывает тренд: то, ради чего харнесс запускают.
# Пара (ключ, человекочитаемая подпись).
TREND_METRICS = (
    ("cost.weighted_median", "цена этапа"),
    ("endtoend.weighted_median", "цена «под ключ»"),
    ("quality.core_recall_median", "core-recall"),
    ("quality.bulk_core_recall_median", "core-recall (bulk)"),
    ("quality.n_measured", "измерено"),
    ("corpus.briefs", "брифов"),
)


@dataclass
class Delta:
    """Изменение одной метрики между срезами."""

    metric: str
    old: object = None
    new: object = None
    delta: float | None = None
    direction: str = "без изменений"


def append_snapshot(path: pathlib.Path, snapshot: dict) -> None:
    """Дописать срез строкой в JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")


def load_snapshots(path: pathlib.Path) -> list:
    """Прочитать все срезы; битые строки пропускаются, а не роняют чтение."""
    snapshots: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snapshots.append(json.loads(line))
        except ValueError:
            continue
    return snapshots


def _flatten(snapshot: dict) -> dict:
    """Плоское представление 'секция.метрика' -> значение (только числа)."""
    flat: dict = {}
    for section, value in snapshot.items():
        if not isinstance(value, dict):
            continue
        for name, item in value.items():
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                flat[f"{section}.{name}"] = item
    return flat


def trend(snapshots: list, metrics=TREND_METRICS) -> list:
    """Строки тренда по срезам: метаданные плюс значения ключевых метрик.

    Отсутствующая в срезе метрика даёт None, а не ноль: старый срез, снятый до
    появления метрики, не «показал нулевое значение» — он её не мерил.
    """
    rows: list = []
    for snapshot in snapshots:
        flat = _flatten(snapshot)
        rows.append(
            {
                "taken_at": snapshot.get("taken_at"),
                "commit": snapshot.get("commit"),
                "window_mode": snapshot.get("window_mode"),
                "values": {key: flat.get(key) for key, _ in metrics},
            }
        )
    return rows


def select_pair(snapshots: list, back: int = 1) -> tuple:
    """Пара (старый, новый) для сравнения: последний срез против среза на `back` шагов назад.

    Returns:
        (old, new); (None, None), если истории не хватает на такой отступ.
    """
    if back < 1 or len(snapshots) < back + 1:
        return None, None
    return snapshots[-1 - back], snapshots[-1]


def _direction(metric: str, delta: float) -> str:
    if delta == 0:
        return "без изменений"
    polarity = POLARITY.get(metric, "neutral")
    if polarity == "neutral":
        return "рост" if delta > 0 else "падение"
    improved = delta < 0 if polarity == "lower_better" else delta > 0
    return "улучшение" if improved else "ухудшение"


def diff_snapshots(old: dict, new: dict) -> list:
    """Дельты метрик нового среза против старого.

    Метрика, которой не было в старом срезе, помечается «новая» — показывать её
    как рост с нуля значит выдумывать историю.
    """
    old_flat = _flatten(old)
    new_flat = _flatten(new)
    deltas: list = []
    for metric in sorted(set(old_flat) | set(new_flat)):
        old_value = old_flat.get(metric)
        new_value = new_flat.get(metric)
        if old_value is None or new_value is None:
            deltas.append(
                Delta(metric=metric, old=old_value, new=new_value, direction="новая")
            )
            continue
        delta = new_value - old_value
        deltas.append(
            Delta(
                metric=metric,
                old=old_value,
                new=new_value,
                delta=delta,
                direction=_direction(metric, delta),
            )
        )
    old_mode = old.get("window_mode")
    new_mode = new.get("window_mode")
    if old_mode != new_mode:
        deltas.append(
            Delta(
                metric="window_mode",
                old=old_mode,
                new=new_mode,
                direction="несопоставимо",
            )
        )
    return deltas
