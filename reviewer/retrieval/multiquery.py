"""Мультизапросный ретрив с RRF-слиянием для секции code solve-task (PRI-255).

Путь параллелен Retriever.search_base и не меняет его: search_base общий для
/ask, грунтовки и ревью PR. Здесь зовётся то, что лежит ниже него —
store.hybrid_search, — а его хвост (дедуп, фильтр тестов) переиспользуется
импортом, а не копией.

Финальный ранкер — RRF, не реранкер. Причина измерена: cliff-отсечка считается
по скорам реранкера против того же многотемного запроса, на размытом запросе
все скоры низкие, отсечка падает до floor — отсюда медиана «2 файла» в
eval/replay_report.md. Сохранить реранк на исходном запросе значило бы
сохранить сам механизм потери.
"""
from __future__ import annotations

import dataclasses
import logging

log = logging.getLogger(__name__)

RRF_K = 60
"""Константа RRF — та же, что в store.hybrid_search и TaskStore.search."""

MAX_BLOCK_CHARS = 2000
"""Потолок символов на блок выдачи — четверть бюджета max_tool_result_chars.

Без него один чанк-класс на 400 строк выжигает весь символьный бюджет
as_context (text[:8000]), и остальные файлы до сборщика брифа не доезжают.
Файловые квоты и диверсификация — не здесь, это ID-310.
"""


def rrf_merge(runs: list[list], k: int = RRF_K) -> list:
    """Слить выдачи подзапросов: score(node) = Σ 1/(k + rank_в_прогоне).

    Файл, найденный несколькими подзапросами, поднимается наверх; найденный
    одним — всё равно остаётся в выдаче. Тай-брейк по node_id, поэтому
    порядок прогонов на результат не влияет.
    """
    scores: dict[str, float] = {}
    items: dict[str, object] = {}
    for run in runs:
        for rank, item in enumerate(run, start=1):
            scores[item.node_id] = scores.get(item.node_id, 0.0) + 1.0 / (k + rank)
            items.setdefault(item.node_id, item)
    ordered = sorted(items, key=lambda node_id: (-scores[node_id], node_id))
    return [dataclasses.replace(items[node_id], score=scores[node_id])
            for node_id in ordered]


def cap_block(item, max_chars: int = MAX_BLOCK_CHARS):
    """Обрезать текст блока по границе строк, поправив end_line.

    Границу строк держим не ради красоты: as_context нумерует строки от
    start_line, а extract_context_paths требует в заголовке диапазон
    ':\\d+-\\d+'. Обрезка по середине строки сделала бы заголовок ложью, а
    усечение уже в as_context — вовсе съело бы заголовок и потеряло путь.
    """
    if len(item.text) <= max_chars:
        return item
    kept: list[str] = []
    used = 0
    for line in item.text.split("\n"):
        if kept and used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return dataclasses.replace(
        item, text="\n".join(kept),
        end_line=item.start_line + max(len(kept) - 1, 0))
