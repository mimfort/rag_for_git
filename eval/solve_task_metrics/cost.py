"""Взвешенная цена этапа: бакеты токенов неравноценны по стоимости."""
from __future__ import annotations

from .briefs import BUCKET_KEYS

# Множители относительно input-токена. Источник — тарифная структура,
# зафиксированная спайком PRI-246: сложение токенов завышает стоимость
# примерно в 4.1×, потому что 88% объёма приходится на дешёвый cache-read.
# Правка тарифа — правка этой константы, а не поиск по коду.
WEIGHTS = {
    "fresh_in": 1.0,
    "output": 5.0,
    "cache_write": 1.25,
    "cache_read": 0.1,
}


def raw(buckets: dict) -> float:
    """Сырая сумма токенов. НЕ пропорциональна стоимости — только справочно."""
    return float(sum(buckets.get(key, 0.0) for key in BUCKET_KEYS))


def weighted(buckets: dict) -> float:
    """Взвешенный input-эквивалент — основная метрика цены."""
    return float(sum(buckets.get(key, 0.0) * WEIGHTS[key] for key in BUCKET_KEYS))


def inflation(raw_value: float, weighted_value: float) -> float | None:
    """Во сколько раз сырая сумма завышает стоимость; None при нулевой цене."""
    if not weighted_value:
        return None
    return raw_value / weighted_value


def sum_buckets(blocks: list[dict]) -> dict[str, float]:
    """Поэлементная сумма нескольких наборов бакетов (например, по моделям)."""
    total = {key: 0.0 for key in BUCKET_KEYS}
    for block in blocks:
        for key in BUCKET_KEYS:
            total[key] += float(block.get(key, 0.0))
    return total
